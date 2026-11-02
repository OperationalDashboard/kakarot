"""Plans hard requests before starting and checks the work before answering.

- Plan: for a request with several parts, Kakarot first writes a short step list (shown in the chat as a checklist)
  and follows it.
- Check: when a job took several actions, a second look compares the request, the plan, what the tools ACTUALLY
  returned and the draft answer. If something is missing or the answer claims something the results don't show,
  Kakarot goes back and fixes it (once) before answering.
Settings: "plan_and_check": true.
"""
import json
import re

from config import CFG

ACTION = re.compile(
    r"\b(make|create|build|write|draft|find|search|look up|check|compare|send|download|open|fix|update|research|"
    r"calculate|count|list|analy[sz]e|summari[sz]e|book|order|convert|translate|schedule|remind|delete|remove|move|"
    r"rename|install|edit|change|prepare|collect|report|plan|design|publish|upload|save|read|extract|organi[sz]e|"
    r"clean|sort|test|run)\b|বানাও|বানিয়ে|তৈরি|খুঁজ|চেক|লিখ|পাঠাও|দেখো|দেখ|ঠিক কর|হিসাব|তুলনা|সাজাও|মুছ|"
    r"\b(banao|baniye|khujo|check koro|likho|pathao|dekho|thik koro|hisab)\b", re.I)
JOIN = re.compile(r"\b(and then|then|after that|also|finally|plus)\b|তারপর|এরপর|এবং|আর\b|\btarpor\b|\berpor\b", re.I)

PLAN = """A user asked their personal assistant (which has tools for files, Excel/Word/PDF, the web, websites,
research, the PC screen, reminders...) to do this:
\"\"\"{text}\"\"\"
{context}
Write the plan: 2 to 6 short concrete steps, in order, that fully complete EVERY part of the request. The last step
is giving the user the result. Write the steps in the user's language. Don't add steps the user didn't ask for.
Reply ONLY with JSON: {{"steps": ["...", "..."]}}"""

CHECK = """You are checking an assistant's work before the user sees it. Be strict but fair.

User's request:
\"\"\"{text}\"\"\"
{plan}
What was ACTUALLY done (tool calls and what they returned):
{log}

Draft answer to the user:
\"\"\"{draft}\"\"\"

Check:
1. Was every part of the request done (not just described or promised)?
2. Does the draft claim anything (files made, numbers, facts, "done", "sent") that the tool results above don't show?
3. Did a tool fail and the draft hides it?
Reply ONLY with JSON: {{"complete": true or false, "problem": "what is missing or wrong, one short sentence",
"fixable": true or false}}  (fixable = the assistant can fix it with its tools right now)"""


def enabled():
    return bool(CFG.get("plan_and_check", True))


def needs_plan(text, smart=False):
    if not enabled():
        return False
    t = re.sub(r"\[Attached file: [^\]]*\]", "", text or "").strip()
    if len(t) < 30:
        return False
    verbs = len(ACTION.findall(t))
    joins = len(JOIN.findall(t))
    return verbs >= 3 or (verbs >= 2 and (joins >= 1 or len(t) > 120)) or (smart and verbs >= 1 and len(t) > 80)


def make_plan(text, recent=""):
    import agent
    try:
        raw = agent.think_once(PLAN.format(text=text[:3000], context=(f"Earlier in the chat:\n{recent[-1200:]}\n"
                                                                       if recent else "")), fmt="json")
        steps = [re.sub(r"^\s*\d+[.)]\s*", "", str(s)).strip() for s in json.loads(raw or "{}").get("steps", [])]
        steps = [s for s in steps if s][:6]
        return steps if len(steps) >= 2 else []
    except Exception:
        return []


def should_check(steps, plan):
    if not enabled():
        return False
    real = [s for s in steps if not s.get("check")]
    return len(real) >= 2 or (plan and len(real) >= 1)


def check(text, plan, steps, draft):
    """Returns (ok, problem, fixable)."""
    import agent
    log = "\n".join(f"- {s['tool']}({json.dumps(s.get('args', {}), ensure_ascii=False)[:160]}) -> "
                    f"{str(s.get('result', ''))[:350]}" for s in steps[-14:]) or "(no tools were used)"
    p = ("Plan:\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(plan, 1)) + "\n") if plan else ""
    try:
        raw = agent.think_once(CHECK.format(text=text[:2500], plan=p, log=log, draft=(draft or "")[:2500]),
                               fmt="json")
        d = json.loads(raw or "{}")
        ok = d.get("complete")
        ok = True if ok is None else (ok is True or str(ok).lower() == "true")
        fixable = d.get("fixable")
        fixable = fixable is True or str(fixable).lower() == "true"
        return ok, str(d.get("problem") or "").strip(), fixable
    except Exception:
        return True, "", False
