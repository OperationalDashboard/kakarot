"""Kakarot's brain: a local model (via Ollama) in a think -> use tool -> look at result loop."""
import datetime
import json
import logging
import re
import threading
import time

import requests

import db
import tools
from config import CFG

_lock = threading.Lock()  # one task at a time so a 16 GB PC isn't overloaded
_tls = threading.local()  # marks "this thread is already inside a task"
_want = 0  # chats waiting for the brain: background jobs step aside for them
_want_guard = threading.Lock()
_stop = threading.Event()  # the Stop button in the chat
_warm = {"at": 0}
log = logging.getLogger("kakarot.agent")

SYSTEM = """You are {name}, a personal assistant running 100% locally on the user's Windows PC.
You don't just talk - you DO things using your tools: files, Excel/Word/PDF, web search, reading and
CONTROLLING websites (click, type, fill forms), running PC commands (with the user's approval), seeing
pictures, searching inside the user's own documents, background missions, website monitoring,
notes, reminders, long-term memory and scheduled tasks.

Folders you may work in: {folders}  (relative paths go into the first one)
The current time and the things you remember about the user that matter for this message are given in a note
right before the user's latest message.

Lessons you learned from past work (follow them):
{lessons}

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
- RESEARCH questions that need several sources (markets, prices, laws, competitors, products, news, "find out
  everything about...", "compare..."): deep_research (depth 'deep' when the user wants it thorough). It searches
  many sites, cross-checks them and writes a report with sources. Quick facts: just web_search / fetch_page.
- Big jobs that need many steps (research + compare + make a report, check many things): offer or use start_mission,
  which works on it in the background and posts the result back into this chat.
- Website health: website_status shows whether watched sites are up; watch_website adds one.
- CODING (programs, scripts, websites, bots, data processing): code_new_project, then write_code for each file with
  DETAILED instructions (your coding brain writes the code), run_code to run it / its tests, read the errors, fix with
  edit_code (include the exact error), and repeat until it works. Add tests for non-trivial logic (pytest). Tell the
  user the project folder and how to run it. Very large apps: use start_mission so it can work for longer.
- WEBSITES / WEB APPS ("I want a website for...", "make a site like...", "build a booking system"): call
  build_website with the user's FULL request (business, pages, features, style, colors, language, contact details,
  attached logo/photo paths in 'assets'). Ask at most one short question first only if the request is very vague
  (e.g. just "make a website"). List them: list_websites.
- EXISTING WEBSITES (the user's live site, a GitHub repo, a folder): open_website first (it copies the site into
  Kakarot and checks it). Then: check_website (deep check; also works on ANY live address without opening),
  fix_website (fixes problems itself, several rounds), update_website (any change the user describes),
  publish_website (GitHub, GitHub Pages, their FTP/cPanel hosting like Namecheap, Netlify, Vercel - the user taps
  Approve), undo_website (last change or last publish), website_history, refresh_website (get changes made on
  GitHub/the hosting). "fix my site and put it online" = fix_website with publish_to. Never ask for passwords or
  tokens in chat: accounts are added by the user in the Websites tab → Accounts (publishing_accounts lists them).
- DESIGN: new sites follow Kakarot's design rules (no AI look, real photos, smooth fast motion). A Figma link =
  build_website_from_figma. "Make my site look better / less AI / more professional" = fix_website with
  focus "design" (existing sites keep their look unless the user asks for that).
- PC SCREEN (anything in a desktop app: Excel/Word windows, File Explorer, Settings, installers, WhatsApp/Telegram
  desktop, any program): screen_look first (pass the user's task as 'goal'; it shows the screen and numbered
  buttons/fields), then screen_click / screen_type / screen_key / screen_scroll / open_app / switch_window, and
  screen_look again after each change to check it worked. Finish with screen_done. Prefer direct tools (files,
  create_excel, web_*) when they can do the job faster. Never type passwords or payment details; before sending,
  deleting, buying or submitting anything, tell the user what you are about to do (risky clicks also ask them).
  If screen control stops because the user moved the mouse, stop and report - don't fight them.
- If you need an ability you don't have and it would be useful again, use build_new_tool (the user approves it).
- For "remind me..." use set_reminder. If the user tells you a lasting fact or preference, use remember.
- Files are never deleted, only moved to _trash with trash_file.
- Every DELETE or EDIT (overwriting/changing/moving a file, trash_file, forget, cancel_scheduled, website edits,
  undo, Save/Delete/Send clicks) shows the user an Approve/Deny card and waits for their answer - just call the
  tool and wait. If the user DENIES, nothing changed: stop that action, say so, and don't try it another way.
- UNDERSTAND THE USER: read the whole conversation. "it", "that", "the file", "do it again", "same for X" refer to
  earlier messages and actions. Pick the most likely meaning and do it; if you had to assume something, say the
  assumption in one short line. Ask a question ONLY when a wrong guess would delete, send, publish or pay for
  something, or when the request truly can't be started. Never ask for things you can find out with your tools.
- If a PLAN is given in the note, follow it step by step (adapt it if a step turns out impossible).
- When finished, reply briefly and clearly with what you did and the result - only claim what your tools actually
  did. Use short Markdown (bold, lists, tables) when it helps reading. Reply in the user's language.
{extra}"""


def _system(background):
    """Kept the same from message to message (no clock, no per-message memory), so the brain can reuse its
    work on it and start answering much faster."""
    return SYSTEM.format(
        name=CFG.get("name", "Kakarot"),
        folders=", ".join(CFG["allowed_folders"]),
        lessons=_lessons() or "(none yet)",
        extra="- This is a scheduled background run: nobody is watching. Finish the task fully and give a "
              "short report. Do not create new schedules." if background else "",
    )


def _note(user_text, plan=None):
    """Per-message note placed right before the user's message: time, relevant memories, plan."""
    lines = [f"Now: {datetime.datetime.now().strftime('%A %d %B %Y, %H:%M')}."]
    try:
        import recall
        mem = recall.context(user_text)
    except Exception:
        mem = "\n".join(f"- {m['fact']}" for m in db.q("SELECT fact FROM memory ORDER BY id")[-30:])
    lines.append("What you remember about the user that may matter here:\n" + (mem or "(nothing yet)"))
    if plan:
        lines.append("PLAN for this request (follow it, then answer):\n" +
                     "\n".join(f"{i}. {s}" for i, s in enumerate(plan, 1)))
    return "\n\n".join(lines)


def _tok(s):
    """Rough token count (Bangla letters cost more tokens than English ones)."""
    s = s or ""
    a = sum(1 for ch in s if ord(ch) < 128)
    return a / 3.6 + (len(s) - a) / 1.3


def _mtok(m):
    t = _tok(m.get("content")) + 8
    if m.get("tool_calls"):
        t += _tok(json.dumps(m["tool_calls"], ensure_ascii=False))
    return t


def _fit(msgs, budget, pinned=()):
    """Make the conversation fit the brain's memory: the instructions, the user's current request (and its note)
    and the newest messages are never cut; long old messages and tool results are shortened first, then the
    oldest ones are dropped."""
    pin = {id(m) for m in pinned}
    total = sum(_mtok(m) for m in msgs)
    if total <= budget:
        return msgs
    free = lambda i: i > 0 and id(msgs[i]) not in pin and i < len(msgs) - 2
    for cap in (1500, 600, 250):
        for i in range(len(msgs)):
            c = msgs[i].get("content") or ""
            if free(i) and len(c) > cap:
                total -= _mtok(msgs[i])
                msgs[i] = {**msgs[i], "content": c[:cap] + " …(shortened)"}
                total += _mtok(msgs[i])
        if total <= budget:
            return msgs
    i = 1
    while total > budget and i < len(msgs) - 2:
        if free(i):
            total -= _mtok(msgs.pop(i))
        else:
            i += 1
    if total > budget:  # a huge request or newest result: shorten it too
        for i in range(len(msgs) - 1, 0, -1):
            c = msgs[i].get("content") or ""
            over = int((total - budget) * 3.2)
            if len(c) > 2000 and over > 0:
                keep = max(1500, len(c) - over)
                total -= _mtok(msgs[i])
                msgs[i] = {**msgs[i], "content": c[:keep] + " …(cut to fit)"}
                total += _mtok(msgs[i])
            if total <= budget:
                break
    return msgs


def _acquire(foreground):
    """The brain does one job at a time. A chat goes first: background jobs wait while a chat is waiting."""
    global _want
    if foreground:
        with _want_guard:
            _want += 1
        try:
            _lock.acquire()
        finally:
            with _want_guard:
                _want -= 1
        return
    while True:
        _lock.acquire()
        if not _want:
            return
        _lock.release()
        time.sleep(0.3)


def _step_aside():
    """Called by background jobs between steps: if a chat is waiting, let it use the brain first."""
    if _want:
        _lock.release()
        t0 = time.time()
        while _want and time.time() - t0 < 5:
            time.sleep(0.2)
        _acquire(False)


def stop():
    """Stop button: the running chat reply stops after the current word / action."""
    _stop.set()


def busy():
    return _lock.locked()


def warm():
    """Load the brain and pre-read the fixed instructions, so the next reply starts fast. Skipped when busy."""
    if time.time() - _warm["at"] < 120 or not _lock.acquire(blocking=False):
        return False
    try:
        ctx = int(CFG.get("context_tokens", 12288))
        msgs = [{"role": "system", "content": _system(False)}, {"role": "user", "content": "hi"}]
        body = {"model": CFG["model"], "messages": msgs, "stream": False, "think": False,
                "tools": tools.schemas_for(msgs, ctx), "keep_alive": CFG.get("keep_model_loaded", "4h"),
                "options": {"num_ctx": ctx, "temperature": 0.3, "num_predict": 1}}
        requests.post(CFG["ollama_url"].rstrip("/") + "/api/chat", json=body, timeout=600)
        _warm["at"] = time.time()
        if check_fit():  # reload once with a memory size that fits on the graphics card
            body["options"]["num_ctx"] = _ctx_size()
            body["tools"] = tools.schemas_for(msgs, _ctx_size())
            requests.post(CFG["ollama_url"].rstrip("/") + "/api/chat", json=body, timeout=600)
        return True
    except Exception as e:
        log.debug("warm-up failed: %s", e)
        return False
    finally:
        _lock.release()


def _lessons():
    try:
        import selfupdate
        return selfupdate.lessons_text()
    except Exception:
        return ""


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


def _ctx_size(smart=False):
    if smart:
        return int(CFG.get("smart_context_tokens", 12288))
    want = int(CFG.get("context_tokens", 12288))
    try:
        fit = db.get_kv(f"fit_ctx:{CFG['model']}:{want}")
        return min(want, int(fit)) if fit else want
    except Exception:
        return want


def loaded_models():
    """What Ollama has in memory now: {name: (total bytes, bytes on the graphics card)}."""
    try:
        r = requests.get(CFG["ollama_url"].rstrip("/") + "/api/ps", timeout=5)
        return {m["name"]: (int(m.get("size") or 0), int(m.get("size_vram") or 0)) for m in r.json().get("models", [])}
    except Exception:
        return {}


def _on_gpu(name):
    """1.0 = all of the brain is on the graphics card (fast); 0.5 = half runs on the processor (slow);
    None = unknown / no graphics card in use."""
    for n, (size, vram) in loaded_models().items():
        if n == name or n == f"{name}:latest":
            return (vram / size) if size and vram else None
    return None


def check_fit():
    """If part of the normal brain had to run on the (much slower) processor, give it a smaller short-term memory
    so it fits on the graphics card completely. Remembered, so it's done once."""
    share = _on_gpu(CFG["model"])
    if share is None or share >= 0.97:
        return False
    want = int(CFG.get("context_tokens", 12288))
    cur = _ctx_size()
    smaller = 8192 if cur > 8192 else 6144 if cur > 6144 else None
    if not smaller:
        return False
    db.set_kv(f"fit_ctx:{CFG['model']}:{want}", str(smaller))
    log.warning("the brain didn't fit on the graphics card (%.0f%% on it) - using %s tokens of memory instead of %s",
                share * 100, smaller, cur)
    return True


def smart_slow():
    """True when the Smart brain only partly fits on the graphics card (then it is several times slower)."""
    v = db.get_kv("smart_gpu_share")
    return v is not None and float(v) < 0.97


def _note_smart_fit():
    share = _on_gpu(CFG.get("smart_model", "qwen3:14b"))
    if share is not None:
        db.set_kv("smart_gpu_share", f"{share:.2f}")


def call_model(messages, smart=False, on_event=None, use_tools=True, fmt=None, schemas=None, stoppable=False):
    """One model turn. With on_event, the reply is streamed: on_event({'type':'token'|'thinking','text':...})."""
    model, ctx, think = CFG["model"], _ctx_size(), CFG.get("deep_thinking", False)
    if smart:  # Smart mode: bigger brain + thinking, slower but better for hard tasks
        model = CFG.get("smart_model", "qwen3:14b")
        ctx = _ctx_size(True)
        think = True
    body = {"model": model, "messages": messages, "stream": bool(on_event),
            "keep_alive": CFG.get("keep_model_loaded", "4h"),  # stay in memory -> later replies are fast
            "options": {"num_ctx": ctx, "temperature": 0.3}}
    if use_tools:
        body["tools"] = schemas if schemas is not None else tools.schemas_for(messages, ctx)
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
        if stoppable and _stop.is_set():
            r.close()
            calls = []
            break
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


_ctx = threading.local()


def current_chat():
    """The chat the user is talking in right now (so background jobs can answer there), or None."""
    return getattr(_ctx, "cid", None)


def run_agent(user_text, conversation_id=None, background=False, save_history=None, smart=False, on_event=None,
              extra_system="", max_steps=None):
    """Returns (final_reply, steps). steps = list of tool calls made, for showing in the UI.
    With a conversation_id, the user's message is saved immediately and the reply (or error) after.
    on_event(dict) receives live progress (tokens, tool start/finish) for streaming to the chat page."""
    cid = conversation_id
    history = []
    if cid:
        rows = db.get_messages(cid, limit=int(CFG.get("history_messages", 20)))
        rows = [r for r in rows if r["role"] in ("user", "assistant")]
        recent_ids = {r["id"] for r in [r for r in rows if r["role"] == "assistant"][-3:]}
        for r in rows:
            content = r["content"]
            if r["id"] in recent_ids and r.get("steps"):  # what was actually done, so "that file" / "it" make sense
                acts = "; ".join(f"{s.get('tool')}({_short_args(s.get('args'))}) -> {str(s.get('result', ''))[:120]}"
                                 for s in r["steps"][:6])
                content += f"\n\n[Actions I took for this: {acts[:900]}]"
            history.append({"role": r["role"], "content": content})
        db.add_message(cid, "user", user_text)
    model_text = user_text
    prev_fg0 = getattr(_ctx, "fg", False)
    _ctx.fg = not background
    try:  # Bangla / Banglish / dialects: work out the meaning first, reply in the user's own style
        import bangla
        if bangla.needs_help(user_text):
            _emit(on_event, type="status", text="Understanding your Bangla…")
            model_text, lang_extra = bangla.prepare(user_text)
            if lang_extra:
                extra_system = (extra_system + "\n" + lang_extra).strip()
    except Exception as e:
        log.warning("bangla helper failed: %s", e)
    prev, prev_fg = current_chat(), prev_fg0
    if cid and not background:
        _ctx.cid = cid
    _ctx.fg = not background
    try:
        final, steps = _run(model_text, history, background, smart, on_event, extra_system, max_steps)
    except Exception as e:
        log.exception("agent failed")
        if cid:
            db.add_message(cid, "error", f"{e}")
        raise
    finally:
        _ctx.cid, _ctx.fg = prev, prev_fg
    if cid:
        db.add_message(cid, "assistant", final, [s for s in steps if not s.get("check")])
    steps = [s for s in steps if not s.get("check")]
    if not background:
        try:
            import recall
            recall.maybe_learn(user_text, final)
        except Exception:
            pass
    return final, steps


def _short_args(a):
    if not isinstance(a, dict):
        return ""
    return ", ".join(f"{k}={str(v)[:60]}" for k, v in list(a.items())[:3])


def _emit(on_event, **ev):
    if on_event:
        try:
            on_event(ev)
        except Exception:
            pass


def _run(user_text, history, background, smart=False, on_event=None, extra_system="", max_steps=None):
    if _lock.locked():
        _emit(on_event, type="status", text="One moment - Kakarot is finishing a step of another job…")
    _acquire(not background)
    _tls.inside = True
    if not background:
        _stop.clear()
    try:
        import keepalive
        keepalive.busy(True)
    except Exception:
        keepalive = None
    try:
        tools.CTX["background"] = background
        note = ""
        if smart and not _has(CFG.get("smart_model", "qwen3:14b"), installed_models()):
            smart = False
            note = (f"\n\n(Smart mode needs the model {CFG.get('smart_model', 'qwen3:14b')} - run get-smart-brain.bat once. "
                    "Answered with the normal brain.)")
        system = _system(background)  # never changes per message -> the brain reuses its earlier work on it
        ctx = _ctx_size(smart)
        base = [{"role": "system", "content": system}] + history + [{"role": "user", "content": user_text}]
        schemas = tools.schemas_for(base, ctx)  # same abilities for the whole job -> the brain can reuse its work
        budget = ctx - int(_tok(json.dumps(schemas, ensure_ascii=False))) - (2500 if smart else 1400)

        plan = []
        try:
            import planner
            if not background and planner.needs_plan(user_text, smart):
                _emit(on_event, type="status", text="Planning the steps…")
                recent = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in history[-4:])
                plan = planner.make_plan(user_text, recent)
                if plan:
                    _emit(on_event, type="plan", steps=plan)
        except Exception as e:
            log.debug("planning skipped: %s", e)
            planner = None

        # The per-message note (time, memories, plan, language/job rules) goes INSIDE the latest user message:
        # Ollama would move any extra system message to the very top, which would make every reply start slowly.
        ctx_note = _note(user_text, plan) + (("\n\n" + extra_system) if extra_system else "")
        user_msg = {"role": "user", "content": f"[Note for you, not written by the user:\n{ctx_note}]\n\n{user_text}"}
        msgs = [{"role": "system", "content": system}] + history + [user_msg]

        steps, final = [], None
        limit, n, checked, stopped = int(max_steps or CFG.get("max_steps", 12)), 0, False, False
        while n < limit:
            n += 1
            if background:
                _step_aside()
            elif _stop.is_set():
                stopped = True
                break
            _emit(on_event, type="thinking_start")
            msgs = _fit(msgs, budget, (user_msg,))
            m = call_model(msgs, smart, on_event, schemas=schemas, stoppable=not background)
            calls = m.get("tool_calls") or []
            msg = {"role": "assistant", "content": m.get("content", "")}
            if calls:
                msg["tool_calls"] = calls
            msgs.append(msg)
            if not background and _stop.is_set():
                final, stopped = _clean(m.get("content")), True
                break
            if not calls:
                final = _clean(m.get("content"))
                # Check the work before the user sees it (only for jobs with several actions), fix once if needed
                if (planner and not background and not checked and n < limit
                        and planner.should_check(steps, plan)):
                    checked = True
                    _emit(on_event, type="status", text="Double-checking the work…")
                    ok, problem, fixable = planner.check(user_text, plan, steps, final)
                    steps.append({"tool": "self_check", "args": {}, "check": True,
                                  "result": "OK" if ok else f"Found: {problem}"})
                    if not ok and fixable and problem:
                        _emit(on_event, type="check", ok=False, text=problem)
                        msgs.append({"role": "user", "content": f"(Self-check before answering) Not finished yet: "
                                                                f"{problem} Fix this now with your tools, then give "
                                                                f"the final answer."})
                        final = None
                        continue
                    _emit(on_event, type="check", ok=True, text="")
                break
            for c in calls:
                fn = c.get("function", {})
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if name.startswith("screen_") or name in ("open_app", "switch_window"):
                    limit = max(limit, int(CFG.get("screen_max_steps", 40)))  # screen work needs many small steps
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
                if name == "screen_look":  # only the newest screen view matters; keep the brain's memory small
                    for old in msgs:
                        if old.get("role") == "tool" and old.get("tool_name") == "screen_look":
                            old["content"] = "(older screen view - see the newest one)"
                msgs.append({"role": "tool", "content": result, "tool_name": name})
                if not background and _stop.is_set():
                    break
            if final is not None:
                break
        if smart:
            try:
                _note_smart_fit()
            except Exception:
                pass
        if stopped:
            final = ((final or "").strip() + "\n\n⏹ Stopped.").strip()
        if final is None:
            final = "I reached my step limit before finishing. Here's what I did so far - ask me to continue."
        if not final:
            final = "Done." if steps else "(no reply)"
        return final + note, steps
    finally:
        _tls.inside = False
        if keepalive:
            keepalive.busy(False)
        _lock.release()


def think_once(prompt, system="", fmt=None, smart=False, background=None):
    """A single answer with no tools (used for planning, checking, summaries, learning). Waits its turn like any
    job (chats first), unless it's called from inside a running task (then it just answers)."""
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    if getattr(_tls, "inside", False):
        return _clean(call_model(msgs, smart=smart, use_tools=False, fmt=fmt).get("content", ""))
    fg = getattr(_ctx, "fg", False) if background is None else not background
    _acquire(fg)
    _tls.inside = True
    try:
        return _clean(call_model(msgs, smart=smart, use_tools=False, fmt=fmt).get("content", ""))
    finally:
        _tls.inside = False
        _lock.release()


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
