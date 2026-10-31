"""Missions: big jobs Kakarot works on in the background.
It plans the steps, does them one by one (each step can use all tools), then writes a final report to the Inbox.
Missions survive a restart: they continue from the last finished step."""
import json
import logging
import threading

import db
from config import CFG
from tools import S, _cut, tool

log = logging.getLogger("kakarot.missions")
_wake = threading.Event()
_worker = None

PLAN_PROMPT = """Goal from the user: {goal}

Break this goal into 2 to {max_steps} concrete steps that an assistant with tools (web search, websites, files,
Excel/Word/PDF, the user's documents, Lal Sobuj bus reports) can do one at a time. Each step must be a clear
action. The last step should usually create the result file or final answer if the goal needs one.
Reply ONLY with JSON: {{"steps": ["step 1 ...", "step 2 ..."]}}"""

STEP_SYSTEM = """You are working on a MISSION in the background (nobody is watching, do not ask questions).
Do ONLY the current step, completely, using your tools. Then reply with a short factual report of what you found
or made for this step (include numbers, names, links and file paths) - the next steps depend on it."""

FINAL_PROMPT = """Mission goal: {goal}

Results of each step:
{results}

Write the final report for the user: the answer/outcome first, then key details, and list any files that were
created (full paths). Be clear and concise. Reply in the language the goal was written in."""


def _get(mid):
    rows = db.q("SELECT * FROM missions WHERE id=?", (mid,))
    if not rows:
        return None
    m = rows[0]
    m["plan"] = json.loads(m["plan"]) if m.get("plan") else []
    m["log"] = json.loads(m["log"]) if m.get("log") else []
    return m


def _save(mid, **kw):
    for k in ("plan", "log"):
        if k in kw:
            kw[k] = json.dumps(kw[k], ensure_ascii=False)
    kw["updated"] = db.now()
    db.x(f"UPDATE missions SET {', '.join(f'{k}=?' for k in kw)} WHERE id=?", (*kw.values(), mid))


def start(goal, kind="agent", data=None):
    mid = db.x("INSERT INTO missions(goal, status, plan, log, result, created, updated, kind, data) "
               "VALUES(?,?,?,?,?,?,?,?,?)",
               (goal.strip(), "queued", "[]", "[]", "", db.now(), db.now(), kind,
                json.dumps(data or {}, ensure_ascii=False)))
    _ensure_worker()
    _wake.set()
    return mid


def cancel(mid):
    db.x("UPDATE missions SET status='cancelled', updated=? WHERE id=? AND status IN ('queued','running')",
         (db.now(), mid))


def list_missions(limit=30):
    out = []
    for r in db.q("SELECT id FROM missions ORDER BY id DESC LIMIT ?", (limit,)):
        m = _get(r["id"])
        m["done_steps"] = len(m["log"])
        out.append(m)
    return out


def _plan(goal):
    import agent
    max_steps = int(CFG.get("mission_max_steps", 6))
    raw = agent.think_once(PLAN_PROMPT.format(goal=goal, max_steps=max_steps), fmt="json")
    try:
        steps = [str(s).strip() for s in json.loads(raw).get("steps", []) if str(s).strip()]
    except (json.JSONDecodeError, AttributeError):
        steps = []
    return steps[:max_steps] or [goal]


def _run_mission(mid):
    import agent
    import scheduler
    m = _get(mid)
    _save(mid, status="running")
    if not m["plan"]:
        m["plan"] = _plan(m["goal"])
        _save(mid, plan=m["plan"])
    plan_text = "\n".join(f"{i}. {s}" for i, s in enumerate(m["plan"], 1))
    for i in range(len(m["log"]), len(m["plan"])):
        if (_get(mid) or {}).get("status") == "cancelled":
            return
        done = "\n\n".join(f"Step {n} result: {_cut(e['result'], 1500)}" for n, e in enumerate(m["log"], 1))
        prompt = (f"MISSION GOAL: {m['goal']}\n\nPLAN:\n{plan_text}\n\n"
                  f"{'RESULTS SO FAR:' + chr(10) + done + chr(10) + chr(10) if done else ''}"
                  f"NOW DO STEP {i + 1}: {m['plan'][i]}")
        try:
            reply, steps = agent.run_agent(prompt, background=True, extra_system=STEP_SYSTEM,
                                           max_steps=int(CFG.get("mission_step_actions", 10)))
            entry = {"step": m["plan"][i], "ok": True, "result": reply,
                     "actions": [s["tool"] for s in steps]}
        except Exception as e:
            entry = {"step": m["plan"][i], "ok": False, "result": f"Failed: {e}", "actions": []}
        m["log"].append(entry)
        _save(mid, log=m["log"])
    if (_get(mid) or {}).get("status") == "cancelled":
        return
    results = "\n\n".join(f"Step {n}: {e['step']}\n{_cut(e['result'], 2500)}" for n, e in enumerate(m["log"], 1))
    try:
        final = agent.think_once(FINAL_PROMPT.format(goal=m["goal"], results=results))
    except Exception as e:
        final = f"(Could not write the summary: {e})\n\n{results}"
    _save(mid, status="done", result=final)
    db.add_inbox(f"🎯 Mission done: {m['goal'][:60]}", final)
    scheduler.notify("Mission done", m["goal"][:120])


def _loop():
    while True:
        rows = db.q("SELECT id FROM missions WHERE status IN ('running','queued') "
                    "ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, id LIMIT 1")
        if not rows:
            _wake.wait(60)
            _wake.clear()
            continue
        mid = rows[0]["id"]
        kind = (db.q("SELECT kind FROM missions WHERE id=?", (mid,)) or [{}])[0].get("kind") or "agent"
        try:
            if kind.startswith("website"):
                import sitebuilder
                sitebuilder.run(mid)
            else:
                _run_mission(mid)
        except Exception as e:
            log.exception("mission %s failed", mid)
            _save(mid, status="failed", result=f"Mission failed: {e}")
            db.add_inbox("⚠️ Mission failed", f"{e}")


def _ensure_worker():
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_loop, daemon=True, name="kakarot-missions")
        _worker.start()


def resume():
    """Called at startup: continue any mission that was running when Kakarot closed."""
    _ensure_worker()


@tool("start_mission",
      "Start a BIG multi-step job in the background (a 'mission'). Kakarot plans it, works through every step on its "
      "own and puts the final report in the Inbox. Use for long research, comparing many things, building reports "
      "from several sources. The user can keep chatting meanwhile.",
      {"goal": S("The full goal, with all details the user gave (dates, places, what file to make).")})
def start_mission(goal):
    mid = start(goal)
    return (f"Mission #{mid} started in the background. I'll plan it and work through it; the result will appear "
            f"in the Inbox (progress is in the Missions tab).")


@tool("list_missions", "Show background missions and their progress.")
def list_missions_tool():
    ms = list_missions(10)
    if not ms:
        return "No missions yet."
    return "\n".join(f"#{m['id']} [{m['status']}] {m['goal'][:80]} - step {m['done_steps']}/{len(m['plan']) or '?'}"
                     for m in ms)
