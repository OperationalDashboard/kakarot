"""Kakarot improves itself day by day:
- Nightly learning: reads the day's chats and results, saves new facts about you and lessons for doing better,
  and may build one new ability (which you approve).
- New versions: checks your GitHub repo every day, installs a newer Kakarot (backup first) and restarts itself.
  If the new version fails to start, start.bat puts the old one back automatically.
- Weekly: downloads newer versions of its AI brains."""
import datetime
import difflib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile

import requests

import db
from config import BASE, CFG
from tools import tool

log = logging.getLogger("kakarot.selfupdate")
DATA = os.path.join(BASE, "data")
VERSION_FILE = os.path.join(DATA, "version.json")
PENDING_FILE = os.path.join(DATA, "pending_update.json")
NOTE_FILE = os.path.join(DATA, "rollback_note.txt")
KEEP = {"config.json", "start.bat"}  # your settings are never replaced (new settings are added); start.bat is running
SKIP_DIRS = {"data", "venv", ".venv", "plugins", "__pycache__", ".git", "workspace"}
_update_lock = threading.Lock()
RESTART_CODE = 3


def cfg():
    c = CFG.get("self_update") or {}
    return {"learn_time": c.get("learn_time", "03:30"), "update_time": c.get("update_time", "04:00"),
            "auto_update_code": c.get("auto_update_code", True), "repo": c.get("repo", "OperationalDashboard/kakarot"),
            "branch": c.get("branch", "main"), "refresh_models_day": c.get("refresh_models_day", "sun"),
            "build_tools": c.get("build_tools", True)}


# ================================================================ nightly learning
LEARN_PROMPT = """You are reviewing the last day of work of a personal assistant called {name}, to make it better.

Things already remembered about the user:
{facts}

Lessons it already follows:
{lessons}

Today's conversations and results:
{log}

Return ONLY JSON:
{{"facts": [new lasting facts or preferences the USER clearly stated (not guesses, not one-off tasks), max 6],
  "lessons": [short practical rules that would have made the assistant faster/more correct today, e.g. which tool
              to use for a kind of request, mistakes to avoid, formats the user wants; max 5; only new ones],
  "tool_idea": null or {{"name": "snake_case", "what": "what a NEW tool should do", "why": "which request needed it"}}
}}
Suggest a tool_idea ONLY if a request failed or was clumsy because no existing tool could do it.
Existing tools: {tools}"""


def _similar(a, items, cut=0.82):
    a = a.lower().strip()
    return any(difflib.SequenceMatcher(None, a, b.lower().strip()).ratio() >= cut for b in items)


def lessons_text(limit=25):
    rows = db.q("SELECT text FROM lessons ORDER BY id DESC LIMIT ?", (limit,))
    return "\n".join(f"- {r['text']}" for r in reversed(rows))


def nightly_learn():
    import agent
    import tools
    since = db.get_kv("last_learn") or (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat(
        sep=" ", timespec="seconds")
    msgs = db.q("SELECT conversation_id, role, content, steps FROM messages WHERE created > ? ORDER BY id", (since,))
    inbox = db.q("SELECT title, body FROM inbox WHERE created > ? ORDER BY id", (since,))
    started = db.now()
    if not msgs and not inbox:
        db.set_kv("last_learn", started)
        return "Nothing new to learn from today."
    lines, cid = [], None
    for m in msgs:
        if m["conversation_id"] != cid:
            cid = m["conversation_id"]
            lines.append(f"\n=== chat {cid} ===")
        tools_used = ""
        if m["steps"]:
            try:
                tools_used = " [tools: " + ", ".join(s["tool"] for s in json.loads(m["steps"])) + "]"
            except Exception:
                pass
        lines.append(f"{m['role'].upper()}: {m['content'][:700]}{tools_used}")
    for i in inbox:
        lines.append(f"\nINBOX {i['title']}: {i['body'][:300]}")
    log_text = "\n".join(lines)[-14000:]
    facts = [r["fact"] for r in db.q("SELECT fact FROM memory")]
    lessons = [r["text"] for r in db.q("SELECT text FROM lessons")]
    prompt = LEARN_PROMPT.format(name=CFG.get("name", "Kakarot"), facts="\n".join(f"- {f}" for f in facts) or "-",
                                 lessons="\n".join(f"- {l}" for l in lessons) or "-", log=log_text,
                                 tools=", ".join(tools.TOOLS))
    smart = agent._has(CFG.get("smart_model", "qwen3:14b"), agent.installed_models())
    raw = agent.think_once(prompt, fmt="json", smart=smart)
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        out = {}
    new_facts = [f.strip() for f in out.get("facts") or [] if isinstance(f, str) and f.strip()
                 and not _similar(f, facts)][:6]
    new_lessons = [l.strip() for l in out.get("lessons") or [] if isinstance(l, str) and l.strip()
                   and not _similar(l, lessons)][:5]
    for f in new_facts:
        db.x("INSERT INTO memory(fact, created) VALUES(?,?)", (f, db.now()))
    for l in new_lessons:
        db.x("INSERT INTO lessons(text, created) VALUES(?,?)", (l, db.now()))
    keep = int(CFG.get("max_lessons", 40))  # keep the list short and fresh
    db.x("DELETE FROM lessons WHERE id NOT IN (SELECT id FROM lessons ORDER BY id DESC LIMIT ?)", (keep,))
    report = [f"Read {len(msgs)} messages and {len(inbox)} inbox items."]
    report.append("New things I know about you:\n" + "\n".join(f"- {f}" for f in new_facts) if new_facts
                  else "No new facts about you.")
    report.append("New lessons I'll follow:\n" + "\n".join(f"- {l}" for l in new_lessons) if new_lessons
                  else "No new lessons.")
    idea = out.get("tool_idea")
    if cfg()["build_tools"] and isinstance(idea, dict) and idea.get("name") and idea.get("what"):
        try:
            import plugins
            n = plugins.build(idea["name"], idea["what"], idea.get("why", ""))
            report.append(f"🧩 I built a new ability for myself: '{n}' - {idea['what']}\n"
                          f"Approve it in the Status tab to switch it on.")
        except Exception as e:
            report.append(f"I tried to build a new ability ({idea.get('name')}) but it didn't pass the checks: {e}")
    db.set_kv("last_learn", started)
    db.set_kv("last_learn_report", "\n\n".join(report))
    return "\n\n".join(report)


def run_learn_job():
    import scheduler
    try:
        text = nightly_learn()
    except Exception as e:
        log.exception("learning failed")
        text = f"Nightly learning failed: {e}"
    db.add_inbox("🌙 What I learned today", text)
    scheduler.notify("Kakarot learned something", text[:200])


# ================================================================ new versions from GitHub
def current_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def latest_remote():
    c = cfg()
    r = requests.get(f"https://api.github.com/repos/{c['repo']}/commits/{c['branch']}", timeout=30,
                     headers={"Accept": "application/vnd.github+json", "User-Agent": "Kakarot"})
    r.raise_for_status()
    j = r.json()
    return {"sha": j["sha"], "message": j["commit"]["message"].split("\n")[0],
            "date": j["commit"]["committer"]["date"]}


def _busy():
    import agent
    return agent._lock.locked() or bool(db.q("SELECT 1 FROM missions WHERE status='running'"))


def restart_when_idle(max_wait=1800):
    def go():
        t0 = time.time()
        while _busy() and time.time() - t0 < max_wait:
            time.sleep(10)
        log.info("restarting to finish the update")
        os._exit(RESTART_CODE)  # start.bat starts Kakarot again
    threading.Thread(target=go, daemon=True).start()


def _merge_config(new_cfg_text):
    path = os.path.join(BASE, "config.json")
    with open(path, encoding="utf-8") as f:
        mine = json.load(f)
    added = []
    for k, v in json.loads(new_cfg_text).items():
        if k not in mine:
            mine[k] = v
            added.append(k)
    if added:
        _write_json(path, mine)
    return added


def install_update(force=False):
    """Check GitHub; if newer, back up, install and restart. Returns a message."""
    if not _update_lock.acquire(blocking=False):
        return "An update is already in progress."
    try:
        c = cfg()
        remote = latest_remote()
        cur = current_version()
        db.set_kv("last_update_check", {"at": db.now(), "remote": remote})
        if not cur.get("sha"):  # first run: this copy is the baseline
            _write_json(VERSION_FILE, {**remote, "installed": db.now()})
            return f"Version recorded ({remote['sha'][:7]}). Future versions will install automatically."
        if remote["sha"] == cur["sha"]:
            return f"Kakarot is up to date ({cur['sha'][:7]}, {cur.get('message', '')})."
        if remote["sha"] in (cur.get("skip") or []):
            return f"Version {remote['sha'][:7]} failed before and was rolled back - skipping it."
        if not (c["auto_update_code"] or force):
            return f"A new version is available: {remote['message']} ({remote['sha'][:7]}). Tap 'Install update'."
        # download
        z = requests.get(f"https://codeload.github.com/{c['repo']}/zip/{remote['sha']}", timeout=120)
        z.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(z.content))
        root = zf.namelist()[0].split("/")[0]
        files = {}
        for n in zf.namelist():
            rel = n[len(root) + 1:]
            if not rel or n.endswith("/"):
                continue
            if rel.split("/")[0] in SKIP_DIRS:
                continue
            files[rel] = zf.read(n)
        if "app.py" not in files or "agent.py" not in files:
            return "The downloaded version looks incomplete, so it was not installed."
        for rel, data in files.items():  # every Python file must at least compile
            if rel.endswith(".py"):
                try:
                    compile(data.decode("utf-8"), rel, "exec")
                except SyntaxError as e:
                    return f"The new version has an error in {rel} (line {e.lineno}), so it was not installed."
        # back up and copy
        backup = os.path.join(DATA, "backups", datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        os.makedirs(backup, exist_ok=True)
        new_files, changed = [], []
        old_req = ""
        req_path = os.path.join(BASE, "requirements.txt")
        if os.path.exists(req_path):
            with open(req_path, encoding="utf-8") as f:
                old_req = f.read()
        for rel, data in files.items():
            if rel in KEEP:
                continue
            dst = os.path.join(BASE, *rel.split("/"))
            if os.path.exists(dst):
                with open(dst, "rb") as f:
                    if f.read() == data:
                        continue
                b = os.path.join(backup, *rel.split("/"))
                os.makedirs(os.path.dirname(b), exist_ok=True)
                shutil.copy2(dst, b)
            else:
                new_files.append(rel)
            changed.append(rel)
        _write_json(os.path.join(backup, "_new_files.json"), new_files)
        for rel in changed:
            dst = os.path.join(BASE, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(files[rel])
        added = _merge_config(files["config.json"].decode("utf-8")) if "config.json" in files else []
        pip_note = ""
        new_req = files.get("requirements.txt", b"").decode("utf-8")
        if new_req and new_req != old_req:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", req_path],
                               capture_output=True, text=True, timeout=1800)
            pip_note = "\nNew packages installed." if r.returncode == 0 else f"\nPackage install problem: {r.stderr[-300:]}"
        _write_json(PENDING_FILE, {"backup": backup, "before": cur, "after": remote, "at": db.now()})
        _write_json(VERSION_FILE, {**remote, "installed": db.now()})
        msg = (f"Installed the new version: {remote['message']} ({remote['sha'][:7]}).\n"
               f"Changed files: {', '.join(changed) or 'none'}"
               + (f"\nNew settings added: {', '.join(added)}" if added else "") + pip_note +
               "\nRestarting as soon as I'm not busy. If it fails to start, the old version comes back by itself.")
        db.add_inbox("⬆️ Kakarot updated itself", msg)
        restart_when_idle()
        return msg
    finally:
        _update_lock.release()


def run_update_job():
    try:
        msg = install_update()
        log.info(msg)
        if msg.startswith("A new version is available"):
            db.add_inbox("⬆️ Update available", msg)
    except Exception as e:
        log.warning("update check failed: %s", e)


def boot_ok():
    """Called ~90 s after a successful start: the update worked, so forget the rollback info."""
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE, encoding="utf-8") as f:
                p = json.load(f)
            os.remove(PENDING_FILE)
            db.add_inbox("✅ Update working", f"Kakarot is running the new version {p['after']['sha'][:7]}.")
        except Exception as e:
            log.warning("boot_ok: %s", e)


def startup():
    if os.path.exists(NOTE_FILE):
        with open(NOTE_FILE, encoding="utf-8") as f:
            db.add_inbox("↩️ Update rolled back", f.read())
        os.remove(NOTE_FILE)
    threading.Timer(90, boot_ok).start()
    threading.Timer(180, run_update_job).start()  # also check for a new version shortly after every start


# ================================================================ weekly brain refresh
def refresh_models():
    import agent
    names = agent.installed_models()
    wanted = [CFG["model"], CFG.get("smart_model"), CFG.get("vision_model"), CFG.get("embed_model"),
              CFG.get("code_model")]
    done = []
    for m in dict.fromkeys(w for w in wanted if w):
        if not agent._has(m, names):
            continue  # only refresh brains that are already downloaded
        try:
            r = requests.post(CFG["ollama_url"].rstrip("/") + "/api/pull", json={"model": m, "stream": False},
                              timeout=7200)
            done.append(f"- {m}: {'up to date / refreshed' if r.status_code == 200 else 'failed ' + r.text[:100]}")
        except Exception as e:
            done.append(f"- {m}: failed ({e})")
    text = "Checked my AI brains for newer versions:\n" + ("\n".join(done) or "- none installed")
    db.add_inbox("🧠 Brains refreshed", text)
    return text


@tool("update_myself", "Check GitHub for a newer version of Kakarot and install it (backup first, then restart).")
def update_myself():
    return install_update(force=True)


@tool("learn_now", "Review today's conversations now and learn from them (new facts and lessons).")
def learn_now():
    return nightly_learn()
