"""Start Kakarot: python app.py  ->  open http://127.0.0.1:5050"""
import datetime
import json
import logging
import logging.handlers
import os
import queue
import re
import threading
import webbrowser

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
from werkzeug.utils import secure_filename

import agent
import auth
import coder
import db
import knowledge
import missions
import monitor
import plugins
import scheduler
import selfupdate
import deploy
import sitebuilder
import sites
import vault
import vision
import workshop
from config import BASE, CFG

os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", handlers=[
    logging.StreamHandler(),
    logging.handlers.RotatingFileHandler(os.path.join(BASE, "data", "kakarot.log"),
                                         maxBytes=2_000_000, backupCount=2, encoding="utf-8")])
app = Flask(__name__, static_folder=os.path.join(BASE, "static"))
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024  # attachments up to 40 MB
PHONE = bool(CFG.get("phone_access", False) or CFG.get("public_access", False))
PIN = auth.register(app) if PHONE else None
db.init()  # create/upgrade the database before any request


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/status")
def api_status():
    smart = CFG.get("smart_model", "qwen3:14b")
    return jsonify({**agent.status(), "name": CFG.get("name", "Kakarot"), "folders": CFG["allowed_folders"],
                    "smart_model": smart, "smart_ready": agent._has(smart, agent.installed_models()),
                    "remote": not auth.is_trusted_local()})


@app.post("/api/chat")
def api_chat():
    body = request.json or {}
    text = body.get("message", "").strip()
    if not text:
        return jsonify({"error": "empty message"}), 400
    cid = body.get("conversation_id")
    if not cid or not db.conversation_exists(cid):
        cid = db.new_conversation()
    try:
        reply, steps = agent.run_agent(text, conversation_id=cid, smart=bool(body.get("smart")))
        return jsonify({"reply": reply, "steps": steps, "conversation_id": cid})
    except Exception as e:
        logging.exception("chat failed")
        return jsonify({"error": str(e), "hint": agent.status().get("hint", ""), "conversation_id": cid}), 500


@app.post("/api/chat/stream")
def api_chat_stream():
    """Same as /api/chat but sends live progress (Server-Sent Events): words as they're written + each action."""
    body = request.json or {}
    text = body.get("message", "").strip()
    if not text:
        return jsonify({"error": "empty message"}), 400
    cid = body.get("conversation_id")
    if not cid or not db.conversation_exists(cid):
        cid = db.new_conversation()
    events = queue.Queue()

    def work():
        try:
            reply, steps = agent.run_agent(text, conversation_id=cid, smart=bool(body.get("smart")),
                                           on_event=events.put)
            events.put({"type": "done", "reply": reply, "steps": steps, "conversation_id": cid})
        except Exception as e:
            logging.exception("chat failed")
            events.put({"type": "error", "error": str(e), "hint": agent.status().get("hint", ""),
                        "conversation_id": cid})

    threading.Thread(target=work, daemon=True).start()  # keeps going (and saves the reply) even if the page closes

    def gen():
        yield "data: " + json.dumps({"type": "start", "conversation_id": cid}) + "\n\n"
        while True:
            try:
                ev = events.get(timeout=10)
            except queue.Empty:
                yield ": ping\n\n"  # keeps phone/tunnel connections open during long steps
                continue
            yield "data: " + json.dumps(ev, ensure_ascii=False, default=str) + "\n\n"
            if ev["type"] in ("done", "error"):
                break

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/upload")
def api_upload():
    """Attach files from the chat (photos from the phone camera, PDFs, Word, Excel...). Saved in workspace/uploads."""
    folder = os.path.join(CFG["allowed_folders"][0], "uploads")
    os.makedirs(folder, exist_ok=True)
    saved = []
    for f in request.files.getlist("files")[:10]:
        stem, ext = os.path.splitext(f.filename or "")
        ext = secure_filename(ext.lstrip(".")).lower()
        if not ext and f.mimetype and "/" in f.mimetype:
            ext = f.mimetype.split("/")[1].replace("jpeg", "jpg")[:5]
        name = (secure_filename(stem) or "file")[:60] + ("." + ext if ext else "")
        path = os.path.join(folder, datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + name)
        f.save(path)
        saved.append({"name": f.filename, "path": path})
    return jsonify(saved)


@app.get("/api/missions")
def api_missions():
    return jsonify(missions.list_missions())


@app.post("/api/missions")
def api_mission_new():
    goal = (request.json or {}).get("goal", "").strip()
    if not goal:
        return jsonify({"error": "empty goal"}), 400
    return jsonify({"id": missions.start(goal)})


@app.post("/api/missions/<int:mid>/cancel")
def api_mission_cancel(mid):
    missions.cancel(mid)
    return jsonify({"ok": True})


@app.get("/api/system")
def api_system():
    names = agent.installed_models()
    want = {"Main brain": CFG["model"], "Smart brain": CFG.get("smart_model", "qwen3:14b"),
            "Coding brain": coder.code_model(), "Picture brain": vision.vision_model(),
            "File search": knowledge.embed_model()}
    last_brief = db.q("SELECT day, created FROM brief_runs ORDER BY day DESC LIMIT 1")
    return jsonify({"models": [{"role": k, "name": v, "ready": agent._has(v, names),
                                "script": {"Smart brain": "get-smart-brain.bat", "Coding brain": "get-code-brain.bat"}.get(
                                    k, "get-advanced-models.bat")}
                               for k, v in want.items()],
                    "sites": monitor.site_report(), "files": knowledge.stats(),
                    "brief": {**monitor.brief_cfg(), "last": last_brief[0]["created"] if last_brief else None,
                              "running": monitor._brief_lock.locked()},
                    "next": scheduler.system_status()})


@app.post("/api/sites")
def api_site_add():
    b = request.json or {}
    if not b.get("url"):
        return jsonify({"error": "url needed"}), 400
    return jsonify({"message": monitor.watch_website(b["url"].strip(), b.get("must_contain", ""))})


@app.post("/api/sites/remove")
def api_site_remove():
    return jsonify({"message": monitor.unwatch_website((request.json or {}).get("url", ""))})


@app.post("/api/sites/check")
def api_site_check():
    monitor.watch_all()
    return jsonify(monitor.site_report())


@app.post("/api/index")
def api_index():
    threading.Thread(target=knowledge.index_files, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/brief")
def api_brief():
    if monitor._brief_lock.locked():
        return jsonify({"message": "Already running."})
    threading.Thread(target=monitor.run_brief_job, daemon=True).start()
    return jsonify({"message": "Started - the brief appears in the Inbox in about 3 minutes."})


@app.get("/sites/<name>/")
@app.get("/sites/<name>/<path:sub>")
def site_static(name, sub="index.html"):
    """Preview of a static website (served from its folder; React/Vite sites are built the first time)."""
    import html as _html
    try:
        root = sites.static_root(name)
    except Exception as e:
        return Response(f"<h2>The preview of '{_html.escape(name)}' could not be made</h2><pre>{_html.escape(vault.scrub(e))}</pre>",
                        500, mimetype="text/html")
    fp = os.path.join(root, *sub.split("/"))
    if sub.endswith("/") or os.path.isdir(fp):
        sub = sub.rstrip("/") + "/index.html"
    elif not os.path.exists(fp) and os.path.exists(fp + ".html"):
        sub += ".html"                       # clean links like /about -> about.html
    resp = send_from_directory(root, sub.lstrip("/"))
    resp.headers["Cache-Control"] = "no-cache"
    spec = sites.spec_of(name) or {}
    if spec.get("imported") and resp.mimetype in ("text/html", "text/css"):
        resp.direct_passthrough = False
        resp.set_data(sites.rewrite_root(resp.get_data(), f"/sites/{name}/", resp.mimetype))
    return resp


@app.route("/apps/<name>/", defaults={"sub": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.route("/apps/<name>/<path:sub>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def site_app(name, sub):
    """Preview of a Flask web app Kakarot built: started on demand and passed through this link."""
    try:
        body, status, headers = sites.proxy(name, sub, request)
    except Exception as e:
        return Response(f"<h2>The app '{name}' could not start</h2><pre>{e}</pre>", 500, mimetype="text/html")
    return Response(body, status, headers)


@app.get("/api/websites")
def api_websites():
    return jsonify(workshop.site_cards())


def _ws(fn):
    try:
        return jsonify(fn())
    except (ValueError, PermissionError, FileNotFoundError, deploy.DeployError) as e:
        return jsonify({"error": vault.scrub(e)}), 400
    except Exception as e:
        logging.exception("website action failed")
        return jsonify({"error": vault.scrub(e)}), 500


@app.post("/api/websites/open")
def api_website_open():
    b = request.json or {}
    def go():
        mid, kind = workshop.start_open((b.get("source") or "").strip(), (b.get("name") or "").strip(),
                                        (b.get("account") or "").strip())
        return {"message": f"Opening it (Mission #{mid}) - the check report follows.", "mission": mid}
    return _ws(go)


@app.get("/api/websites/<name>")
def api_website_detail(name):
    return _ws(lambda: workshop.site_detail(name))


@app.post("/api/websites/<name>/<action>")
def api_website_action(name, action):
    b = request.json or {}
    def go():
        if action == "check":
            return {"message": workshop.check_website(name, bool(b.get("live")))}
        if action == "fix":
            return {"message": workshop.fix_website(name, b.get("focus", ""), b.get("publish_to", ""))}
        if action == "change":
            if not (b.get("change") or "").strip():
                raise ValueError("Describe the change.")
            return {"message": workshop.update_website(name, b["change"].strip(), b.get("publish_to", ""))}
        if action == "publish":
            opts = {k: b[k] for k in ("repo", "branch", "folder", "message", "visibility") if b.get(k)}
            if b.get("pages"):
                opts["pages"] = True
            if b.get("remove_deleted"):
                opts["remove_deleted"] = True
            return {"message": workshop.request_publish(name, b.get("target", ""), opts)}
        if action == "undo":
            return {"message": workshop.undo_website(name, b.get("what", "change"), b.get("id", ""))}
        if action == "refresh":
            return {"message": workshop.refresh_website(name)}
        raise ValueError("unknown action")
    return _ws(go)


@app.get("/api/websites/<name>/diff/<cid>")
def api_website_diff(name, cid):
    import siteproject as sp
    return Response(sp.change_diff(sp.project_dir(workshop._find(name)), re.sub(r"[^\w-]", "", cid)) or "(no diff)",
                    mimetype="text/plain; charset=utf-8")


@app.get("/api/websites/<name>/shot/<which>")
def api_website_shot(name, which):
    import siteproject as sp
    d = os.path.join(sp.project_dir(workshop._find(name)), sp.META, "screens")
    return send_from_directory(d, "phone.png" if which == "phone" else "desktop.png", max_age=0)


@app.get("/api/publish-targets/<name>")
def api_publish_targets(name):
    def go():
        import siteproject as sp
        spec = sp.load(workshop._find(name)) or {}
        acc = vault.listing()
        out = []
        if any(a["kind"] == "github" for a in acc):
            out += [{"value": "github", "label": "GitHub (code)"}, {"value": "github-pages", "label": "GitHub Pages (free hosting)"}]
        out += [{"value": a["key"], "label": f"Hosting: {a.get('label')} ({a.get('site_url') or a.get('host')})"}
                for a in acc if a["kind"] == "ftp"]
        out += [{"value": k, "label": k.capitalize()} for k in ("netlify", "vercel") if any(a["kind"] == k for a in acc)]
        if (spec.get("origin") or {}).get("type") == "folder":
            out.append({"value": "folder", "label": f"Back to {spec['origin']['path']}"})
        return {"targets": out, "default": workshop._default_target(spec)}
    return _ws(go)


@app.get("/api/accounts")
def api_accounts():
    return jsonify(vault.listing())


@app.post("/api/accounts")
def api_account_save():
    b = request.json or {}
    def go():
        kind = b.pop("kind", "")
        for f in ("secure", "verify_cert", "allow_plain"):
            if f in b:
                b[f] = bool(b[f])
        key = vault.save(kind, b)
        return {"ok": True, "key": key}
    return _ws(go)


@app.delete("/api/accounts/<path:key>")
def api_account_delete(key):
    vault.remove(key)
    return jsonify({"ok": True})


@app.post("/api/accounts/<path:key>/test")
def api_account_test(key):
    def go():
        if key == "github":
            return {"message": f"✅ Signed in to GitHub as {deploy.GitHub().login()}"}
        if key == "netlify":
            return {"message": f"✅ Signed in to Netlify as {deploy.Netlify().me().get('email', '?')}"}
        if key == "vercel":
            u = deploy.Vercel().me().get("user", {})
            return {"message": f"✅ Signed in to Vercel as {u.get('username') or u.get('email', '?')}"}
        if key.startswith("ftp:"):
            return {"message": "✅ " + deploy.test_ftp(key)}
        raise ValueError("unknown account")
    return _ws(go)


@app.post("/api/websites")
def api_website_new():
    b = request.json or {}
    if not (b.get("request") or "").strip():
        return jsonify({"error": "describe the website"}), 400
    return jsonify({"message": sitebuilder.build_website(b["request"].strip())})


@app.get("/api/selfupdate")
def api_selfupdate():
    return jsonify({"version": selfupdate.current_version(), "settings": selfupdate.cfg(),
                    "last_check": db.get_kv("last_update_check"), "last_learn": db.get_kv("last_learn"),
                    "last_learn_report": db.get_kv("last_learn_report"),
                    "lessons": db.q("SELECT * FROM lessons ORDER BY id DESC"), "tools": plugins.listing(),
                    "code_folder": coder.CODE_DIR, "next": scheduler.system_status()})


@app.post("/api/selfupdate/check")
def api_selfupdate_check():
    try:
        return jsonify({"message": selfupdate.install_update(force=bool((request.json or {}).get("install")))})
    except Exception as e:
        return jsonify({"message": f"Could not check for updates: {e}"})


@app.post("/api/selfupdate/learn")
def api_selfupdate_learn():
    threading.Thread(target=selfupdate.run_learn_job, daemon=True).start()
    return jsonify({"message": "Learning from today's chats - the result appears in the Inbox in a few minutes."})


@app.delete("/api/lessons/<int:lid>")
def api_lesson_delete(lid):
    db.x("DELETE FROM lessons WHERE id=?", (lid,))
    return jsonify({"ok": True})


@app.post("/api/mytools/<name>/remove")
def api_mytool_remove(name):
    return jsonify({"message": plugins.remove_my_tool(name)})


@app.get("/api/conversations")
def api_conversations():
    return jsonify(db.list_conversations())


@app.post("/api/conversations")
def api_conversation_new():
    return jsonify({"id": db.new_conversation()})


@app.get("/api/conversations/<int:cid>")
def api_conversation(cid):
    if not db.conversation_exists(cid):
        return jsonify({"error": "not found"}), 404
    return jsonify(db.get_messages(cid))


@app.patch("/api/conversations/<int:cid>")
def api_conversation_rename(cid):
    db.rename_conversation(cid, (request.json or {}).get("title", ""))
    return jsonify({"ok": True})


@app.delete("/api/conversations/<int:cid>")
def api_conversation_delete(cid):
    db.delete_conversation(cid)
    return jsonify({"ok": True})


@app.get("/api/approvals")
def api_approvals():
    return jsonify(db.q("SELECT * FROM approvals WHERE status='pending' ORDER BY id"))


@app.post("/api/approvals/<int:aid>")
def api_approval_answer(aid):
    ok = bool((request.json or {}).get("approve"))
    row = db.q("SELECT * FROM approvals WHERE id=? AND status='pending'", (aid,))
    db.x("UPDATE approvals SET status=? WHERE id=? AND status='pending'", ("approved" if ok else "denied", aid))
    if row and row[0]["kind"] == "publish" and ok:  # publish a website (or undo a publish)
        mid = workshop.approved(row[0]["detail"])
        return jsonify({"ok": True, "mission": mid})
    if row and row[0]["kind"] == "tool":  # a new ability Kakarot built for itself
        name = json.loads(row[0]["detail"])["name"]
        try:
            plugins.activate(name) if ok else plugins.reject(name)
        except Exception as e:
            logging.exception("plugin activation failed")
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.get("/api/inbox")
def api_inbox():
    return jsonify(db.q("SELECT * FROM inbox ORDER BY id DESC LIMIT 50"))


@app.post("/api/inbox/seen")
def api_inbox_seen():
    db.x("UPDATE inbox SET seen=1")
    return jsonify({"ok": True})


@app.get("/api/jobs")
def api_jobs():
    return jsonify(scheduler.list_jobs())


@app.delete("/api/jobs/<job_id>")
def api_job_delete(job_id):
    scheduler.remove_job(job_id)
    return jsonify({"ok": True})


@app.get("/api/notes")
def api_notes():
    return jsonify(db.q("SELECT * FROM notes ORDER BY done, id DESC LIMIT 100"))


@app.post("/api/notes/<int:nid>/toggle")
def api_note_toggle(nid):
    db.x("UPDATE notes SET done = 1 - done WHERE id=?", (nid,))
    return jsonify({"ok": True})


@app.get("/api/memory")
def api_memory():
    return jsonify(db.q("SELECT * FROM memory ORDER BY id"))


@app.delete("/api/memory/<int:mid>")
def api_memory_delete(mid):
    db.x("DELETE FROM memory WHERE id=?", (mid,))
    return jsonify({"ok": True})


if __name__ == "__main__":
    db.init()
    scheduler.start()
    missions.resume()
    selfupdate.startup()
    port = int(CFG.get("port", 5050))
    url = f"http://127.0.0.1:{port}"
    name = CFG.get("name", "Kakarot")
    print(f"\n  {name} is running.\n  On this PC:   {url}")
    if PHONE:
        print(f"  On your phone (same Wi-Fi):  http://{auth.lan_ip()}:{port}")
        print(f"  {'Password' if auth.PUBLIC else 'Phone PIN'}:     {PIN}   (change it in config.json)")
        if auth.PUBLIC:
            print("  From anywhere: run go-online.bat once, then open your https://....ts.net link")
    print("\n  Keep this window open. Close it to stop.\n")
    if CFG.get("open_browser_on_start", True) and os.environ.get("KAKAROT_RESTARTED") != "1":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0" if PHONE else "127.0.0.1", port=port, threaded=True, use_reloader=False)
