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
You don't just talk - you DO things using your tools: files, Excel/Word/PDF, web search, reading and
CONTROLLING websites (click, type, fill forms), running PC commands (with the user's approval), seeing
pictures, searching inside the user's own documents, background missions, website monitoring,
notes, reminders, long-term memory and scheduled tasks.

Current local time: {time}
Folders you may work in: {folders}  (relative paths go into the first one)

Things you remember about the user:
{memory}

How to work:
- Break the request into steps and call tools to actually complete it. Don't just describe what you would do.
- After each tool result, decide the next step. If a tool fails, read the error and try another way.
- Never invent facts, prices or web content: look them up with web_search / fetch_page / browse_page.
- To INTERACT with a website (search forms, dropdowns, buttons, logins, multi-page flows) use web_open, then
  web_click / web_type / web_select with the element numbers it shows. Re-read with web_read after changes.
  Never type passwords or payment details unless the user gave them for that exact site in this chat.
- For spreadsheets/tables use create_excel; for reports/letters use create_word or create_pdf. Save into the
  workspace and tell the user the file path. Read .xlsx/.csv/.docx/.pdf with read_file.
- run_command / open_file need the user to click Approve. Use them only when really needed, explain why in
  'reason', and never run destructive commands (deleting, formatting, registry, shutdown) unless asked.
- To watch a website for changes use check_page_changes; for "every day/hour..." requests use schedule_task.
- You can SEE: for an attached picture, photo, screenshot or scanned PDF, call look_at_image with a clear question.
- You KNOW the user's files: when they ask about something that might be in their documents, call search_my_files
  first and answer from the passages, naming the files. Attached documents: read_file (or look_at_image if scanned).
- Big jobs that need many steps (research + compare + make a report, check many things): offer or use start_mission,
  which works on it in the background and puts the result in the Inbox.
- Website health: website_status shows whether watched sites are up; watch_website adds one.
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


def installed_models():
    try:
        r = requests.get(CFG["ollama_url"].rstrip("/") + "/api/tags", timeout=5)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def _has(name, names):
    return name in names or f"{name}:latest" in names


def call_model(messages, smart=False, on_event=None, use_tools=True, fmt=None):
    """One model turn. With on_event, the reply is streamed: on_event({'type':'token'|'thinking','text':...})."""
    model, ctx, think = CFG["model"], int(CFG.get("context_tokens", 8192)), CFG.get("deep_thinking", False)
    if smart:  # Smart mode: bigger brain + thinking, slower but better for hard tasks
        model = CFG.get("smart_model", "qwen3:14b")
        ctx = int(CFG.get("smart_context_tokens", 12288))
        think = True
    body = {"model": model, "messages": messages, "stream": bool(on_event),
            "keep_alive": CFG.get("keep_model_loaded", "30m"),  # stay in memory -> later replies are fast
            "options": {"num_ctx": ctx, "temperature": 0.3}}
    if use_tools:
        body["tools"] = tools.schemas()
    if fmt:
        body["format"] = fmt
    if not think:
        body["think"] = False  # skip the long hidden "thinking" step: many times faster on a PC
    url = CFG["ollama_url"].rstrip("/") + "/api/chat"
    r = requests.post(url, json=body, timeout=900, stream=bool(on_event))
    if r.status_code == 400 and "think" in r.text.lower():  # older Ollama / model without think support
        body.pop("think", None)
        r = requests.post(url, json=body, timeout=900, stream=bool(on_event))
    if r.status_code != 200:
        raise RuntimeError(f"Ollama error {r.status_code}: {r.text[:300]}")
    if not on_event:
        return r.json()["message"]
    content, calls = [], []
    for line in r.iter_lines():
        if not line:
            continue
        chunk = json.loads(line)
        if chunk.get("error"):
            raise RuntimeError(f"Ollama error: {chunk['error']}")
        m = chunk.get("message") or {}
        if m.get("thinking"):
            on_event({"type": "thinking", "text": m["thinking"]})
        if m.get("content"):
            content.append(m["content"])
            on_event({"type": "token", "text": m["content"]})
        calls += m.get("tool_calls") or []
        if chunk.get("done"):
            break
    return {"role": "assistant", "content": "".join(content), "tool_calls": calls}


def run_agent(user_text, conversation_id=None, background=False, save_history=None, smart=False, on_event=None,
              extra_system="", max_steps=None):
    """Returns (final_reply, steps). steps = list of tool calls made, for showing in the UI.
    With a conversation_id, the user's message is saved immediately and the reply (or error) after.
    on_event(dict) receives live progress (tokens, tool start/finish) for streaming to the chat page."""
    cid = conversation_id
    history = []
    if cid:
        rows = db.get_messages(cid, limit=int(CFG.get("history_messages", 16)))
        history = [{"role": r["role"], "content": r["content"]} for r in rows if r["role"] in ("user", "assistant")]
        db.add_message(cid, "user", user_text)
    try:
        final, steps = _run(user_text, history, background, smart, on_event, extra_system, max_steps)
    except Exception as e:
        log.exception("agent failed")
        if cid:
            db.add_message(cid, "error", f"{e}")
        raise
    if cid:
        db.add_message(cid, "assistant", final, steps)
    return final, steps


def _emit(on_event, **ev):
    if on_event:
        try:
            on_event(ev)
        except Exception:
            pass


def _run(user_text, history, background, smart=False, on_event=None, extra_system="", max_steps=None):
    if not _lock.acquire(blocking=False):
        _emit(on_event, type="status", text="Waiting - Kakarot is finishing another job (a mission or schedule)…")
        _lock.acquire()
    try:
        tools.CTX["background"] = background
        note = ""
        if smart and not _has(CFG.get("smart_model", "qwen3:14b"), installed_models()):
            smart = False
            note = (f"\n\n(Smart mode needs the model {CFG.get('smart_model', 'qwen3:14b')} - run get-smart-brain.bat once. "
                    "Answered with the normal brain.)")
        system = _system(background) + ("\n" + extra_system if extra_system else "")
        msgs = [{"role": "system", "content": system}] + history + [{"role": "user", "content": user_text}]

        steps, final = [], None
        for _ in range(int(max_steps or CFG.get("max_steps", 12))):
            _emit(on_event, type="thinking_start")
            m = call_model(msgs, smart, on_event)
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
                _emit(on_event, type="step_start", tool=name, args=args)
                if background and name in ("schedule_task", "start_mission"):
                    result = f"Error: background runs can't use {name}."
                else:
                    result = tools.execute(name, args)
                direct = result.startswith("[[DIRECT]]")
                _emit(on_event, type="step_done", tool=name,
                      result="(report ready)" if direct else result[:300], ok=not result.startswith("Error"))
                if direct:  # finished report: give it to the user as-is (no slow retyping)
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
        return final + note, steps
    finally:
        _lock.release()


def think_once(prompt, system="", fmt=None):
    """A single answer with no tools (used for planning and summaries). Waits its turn like any job."""
    with _lock:
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        return _clean(call_model(msgs, use_tools=False, fmt=fmt).get("content", ""))


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
