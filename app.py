"""Start Kakarot: python app.py  ->  open http://127.0.0.1:5050"""
import logging
import logging.handlers
import os
import threading
import webbrowser

from flask import Flask, jsonify, request, send_from_directory

import agent
import auth
import db
import scheduler
from config import BASE, CFG

os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", handlers=[
    logging.StreamHandler(),
    logging.handlers.RotatingFileHandler(os.path.join(BASE, "data", "kakarot.log"),
                                         maxBytes=2_000_000, backupCount=2, encoding="utf-8")])
app = Flask(__name__, static_folder=os.path.join(BASE, "static"))
PHONE = bool(CFG.get("phone_access", False) or CFG.get("public_access", False))
PIN = auth.register(app) if PHONE else None
db.init()  # create/upgrade the database before any request


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/status")
def api_status():
    return jsonify({**agent.status(), "name": CFG.get("name", "Kakarot"), "folders": CFG["allowed_folders"],
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
        reply, steps = agent.run_agent(text, conversation_id=cid)
        return jsonify({"reply": reply, "steps": steps, "conversation_id": cid})
    except Exception as e:
        logging.exception("chat failed")
        return jsonify({"error": str(e), "hint": agent.status().get("hint", ""), "conversation_id": cid}), 500


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
    if CFG.get("open_browser_on_start", True):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0" if PHONE else "127.0.0.1", port=port, threaded=True, use_reloader=False)
