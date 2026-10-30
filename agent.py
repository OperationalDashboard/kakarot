"""Kakarot's brain: a local model (via Ollama) in a think -> use tool -> look at result loop."""
import datetime
import json
import logging
import re
import threading

import requests

import db
import tools
from config import CFG

_lock = threading.Lock()  # one task at a time so a 16 GB PC isn't overloaded
log = logging.getLogger("kakarot.agent")

SYSTEM = """You are {name}, a personal assistant running 100% locally on the user's Windows PC.
You don't just talk - you DO things using your tools: files, web search, web browsing, page monitoring,
notes, reminders, long-term memory and scheduled tasks.

Current local time: {time}
Folders you may work in: {folders}  (relative paths go into the first one)

Things you remember about the user:
{memory}

How to work:
- Break the request into steps and call tools to actually complete it. Don't just describe what you would do.
- After each tool result, decide the next step. If a tool fails, read the error and try another way.
- Never invent facts, prices or web content: look them up with web_search / fetch_page / browse_page.
- To watch a website for changes use check_page_changes; for "every day/hour..." requests use schedule_task.
- For "remind me..." use set_reminder. If the user tells you a lasting fact or preference, use remember.
- Files are never deleted, only moved to _trash with trash_file.
- When finished, reply briefly and clearly with what you did and the result. Reply in the user's language.
{extra}"""


def _system(background):
    mem = db.q("SELECT fact FROM memory ORDER BY id")
    return SYSTEM.format(
        name=CFG.get("name", "Kakarot"),
        time=datetime.datetime.now().strftime("%A %d %B %Y, %H:%M"),
        folders=", ".join(CFG["allowed_folders"]),
        memory="\n".join(f"- {m['fact']}" for m in mem) or "(nothing yet)",
        extra="- This is a scheduled background run: nobody is watching. Finish the task fully and give a "
              "short report. Do not create new schedules." if background else "",
    )


def _clean(text):
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def call_model(messages):
    body = {"model": CFG["model"], "messages": messages, "tools": tools.schemas(), "stream": False,
            "keep_alive": CFG.get("keep_model_loaded", "30m"),  # stay in memory -> later replies are fast
            "options": {"num_ctx": int(CFG.get("context_tokens", 8192)), "temperature": 0.3}}
    if not CFG.get("deep_thinking", False):
        body["think"] = False  # skip the long hidden "thinking" step: many times faster on a PC
    url = CFG["ollama_url"].rstrip("/") + "/api/chat"
    r = requests.post(url, json=body, timeout=900)
    if r.status_code == 400 and "think" in r.text.lower():  # older Ollama / model without think support
        body.pop("think", None)
        r = requests.post(url, json=body, timeout=900)
    if r.status_code != 200:
        raise RuntimeError(f"Ollama error {r.status_code}: {r.text[:300]}")
    return r.json()["message"]


def run_agent(user_text, conversation_id=None, background=False, save_history=None):
    """Returns (final_reply, steps). steps = list of tool calls made, for showing in the UI.
    With a conversation_id, the user's message is saved immediately and the reply (or error) after."""
    cid = conversation_id
    history = []
    if cid:
        rows = db.get_messages(cid, limit=int(CFG.get("history_messages", 16)))
        history = [{"role": r["role"], "content": r["content"]} for r in rows if r["role"] in ("user", "assistant")]
        db.add_message(cid, "user", user_text)
    try:
        final, steps = _run(user_text, history, background)
    except Exception as e:
        log.exception("agent failed")
        if cid:
            db.add_message(cid, "error", f"{e}")
        raise
    if cid:
        db.add_message(cid, "assistant", final, steps)
    return final, steps


def _run(user_text, history, background):
    with _lock:
        msgs = [{"role": "system", "content": _system(background)}] + history + \
               [{"role": "user", "content": user_text}]

        steps, final = [], None
        for _ in range(int(CFG.get("max_steps", 12))):
            m = call_model(msgs)
            calls = m.get("tool_calls") or []
            msg = {"role": "assistant", "content": m.get("content", "")}
            if calls:
                msg["tool_calls"] = calls
            msgs.append(msg)
            if not calls:
                final = _clean(m.get("content"))
                break
            for c in calls:
                fn = c.get("function", {})
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if background and name == "schedule_task":
                    result = "Error: scheduled runs can't create new schedules."
                else:
                    result = tools.execute(name, args)
                if result.startswith("[[DIRECT]]"):  # finished report: give it to the user as-is (no slow retyping)
                    final = result[len("[[DIRECT]]"):].strip()
                    steps.append({"tool": name, "args": args, "result": "(report shown above)"})
                    break
                steps.append({"tool": name, "args": args, "result": result[:600]})
                msgs.append({"role": "tool", "content": result, "tool_name": name})
            if final is not None:
                break
        if final is None:
            final = "I reached my step limit before finishing. Here's what I did so far - ask me to continue."
        if not final:
            final = "Done." if steps else "(no reply)"
        return final, steps


def status():
    try:
        r = requests.get(CFG["ollama_url"].rstrip("/") + "/api/tags", timeout=5)
        names = [m["name"] for m in r.json().get("models", [])]
        want = CFG["model"]
        ok = want in names or f"{want}:latest" in names
        return {"ollama": True, "model": want, "model_ready": ok,
                "hint": "" if ok else f"Model not downloaded. Run:  ollama pull {want}"}
    except Exception:
        return {"ollama": False, "model": CFG["model"], "model_ready": False,
                "hint": "Ollama isn't running. Start the Ollama app (or run 'ollama serve')."}
