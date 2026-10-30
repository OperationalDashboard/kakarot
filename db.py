"""Kakarot's database (SQLite file: data/kakarot.db). Everything is saved on this PC."""
import contextlib
import datetime
import json
import os
import re
import sqlite3
import threading

from config import BASE

DB_PATH = os.path.join(BASE, "data", "kakarot.db")
_lock = threading.RLock()


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")  # safe writes even if the PC loses power
    return c


def now():
    return datetime.datetime.now().isoformat(sep=" ", timespec="seconds")


def _columns(c, table):
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}


def init():
    with _lock:
        c = _conn()
        try:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS conversations(id INTEGER PRIMARY KEY, title TEXT,
                                                     created TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, role TEXT, content TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS memory(id INTEGER PRIMARY KEY, fact TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, text TEXT, done INTEGER DEFAULT 0, created TEXT);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, name TEXT, prompt TEXT, cron TEXT,
                                            run_at TEXT, kind TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY, title TEXT, body TEXT,
                                             seen INTEGER DEFAULT 0, created TEXT);
            CREATE TABLE IF NOT EXISTS page_snapshots(url TEXT PRIMARY KEY, text TEXT, checked TEXT);
            CREATE TABLE IF NOT EXISTS approvals(id INTEGER PRIMARY KEY, kind TEXT, detail TEXT, reason TEXT,
                                                 status TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS missions(id INTEGER PRIMARY KEY, goal TEXT, status TEXT, plan TEXT,
                                                log TEXT, result TEXT, created TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS doc_files(path TEXT PRIMARY KEY, mtime REAL, size INTEGER,
                                                 chunks INTEGER, indexed TEXT);
            CREATE TABLE IF NOT EXISTS doc_chunks(id INTEGER PRIMARY KEY, path TEXT, chunk_no INTEGER,
                                                  text TEXT, emb BLOB);
            CREATE INDEX IF NOT EXISTS idx_chunk_path ON doc_chunks(path);
            CREATE TABLE IF NOT EXISTS watch_sites(url TEXT PRIMARY KEY, must_contain TEXT, added TEXT);
            CREATE TABLE IF NOT EXISTS site_state(url TEXT PRIMARY KEY, ok INTEGER, since TEXT, checked TEXT,
                                                  ms INTEGER, code INTEGER, error TEXT, fails INTEGER DEFAULT 0,
                                                  alerted INTEGER DEFAULT 0, cert_days INTEGER, cert_checked TEXT);
            CREATE TABLE IF NOT EXISTS site_checks(id INTEGER PRIMARY KEY, url TEXT, ok INTEGER, ms INTEGER,
                                                   code INTEGER, checked TEXT);
            CREATE INDEX IF NOT EXISTS idx_site_checks ON site_checks(url, checked);
            CREATE TABLE IF NOT EXISTS brief_runs(day TEXT PRIMARY KEY, data TEXT, created TEXT);
            """)
            try:  # keyword search over your files (FTS5 is built into Python's SQLite on Windows)
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(text, path UNINDEXED, "
                          "chunk_id UNINDEXED)")
            except sqlite3.OperationalError:
                pass
            # upgrade older databases
            cols = _columns(c, "messages")
            if "conversation_id" not in cols:
                c.execute("ALTER TABLE messages ADD COLUMN conversation_id INTEGER")
            if "steps" not in cols:
                c.execute("ALTER TABLE messages ADD COLUMN steps TEXT")
            orphans = c.execute("SELECT COUNT(*) FROM messages WHERE conversation_id IS NULL").fetchone()[0]
            if orphans:
                cur = c.execute("INSERT INTO conversations(title, created, updated) VALUES(?,?,?)",
                                ("Earlier chats", now(), now()))
                c.execute("UPDATE messages SET conversation_id=? WHERE conversation_id IS NULL", (cur.lastrowid,))
            c.execute("CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id)")
            c.commit()
        finally:
            c.close()


def q(sql, args=()):
    """Run a SELECT and return a list of dicts."""
    with _lock:
        c = _conn()
        try:
            return [dict(r) for r in c.execute(sql, args).fetchall()]
        finally:
            c.close()


def x(sql, args=()):
    """Run an INSERT/UPDATE/DELETE and return lastrowid."""
    with _lock:
        c = _conn()
        try:
            cur = c.execute(sql, args)
            c.commit()
            return cur.lastrowid
        finally:
            c.close()


@contextlib.contextmanager
def tx():
    """One connection + one commit for many writes (much faster for bulk work)."""
    with _lock:
        c = _conn()
        try:
            yield c
            c.commit()
        finally:
            c.close()


def add_inbox(title, body):
    return x("INSERT INTO inbox(title, body, created) VALUES(?,?,?)", (title, body, now()))


# ---------------------------------------------------------------- conversations
def new_conversation(title="New chat"):
    return x("INSERT INTO conversations(title, created, updated) VALUES(?,?,?)", (title, now(), now()))


def conversation_exists(cid):
    return bool(q("SELECT 1 FROM conversations WHERE id=?", (cid,)))


def list_conversations(limit=200):
    return q("""SELECT c.id, c.title, c.created, c.updated,
                       (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS count
                FROM conversations c ORDER BY c.updated DESC LIMIT ?""", (limit,))


def add_message(cid, role, content, steps=None):
    mid = x("INSERT INTO messages(conversation_id, role, content, steps, created) VALUES(?,?,?,?,?)",
            (cid, role, content, json.dumps(steps, ensure_ascii=False) if steps else None, now()))
    x("UPDATE conversations SET updated=? WHERE id=?", (now(), cid))
    if role == "user":  # name the chat after its first message
        x("UPDATE conversations SET title=? WHERE id=? AND title='New chat'",
          (re.sub(r"\s*\[Attached file: [^\]]*\]", " 📎", content).strip().replace("\n", " ")[:60] or "New chat", cid))
    return mid


def get_messages(cid, limit=None):
    sql = "SELECT id, role, content, steps, created FROM messages WHERE conversation_id=? ORDER BY id"
    rows = q(sql, (cid,))
    if limit:
        rows = rows[-limit:]
    for r in rows:
        r["steps"] = json.loads(r["steps"]) if r.get("steps") else []
    return rows


def rename_conversation(cid, title):
    x("UPDATE conversations SET title=? WHERE id=?", (title.strip()[:80] or "Chat", cid))


def delete_conversation(cid):
    x("DELETE FROM messages WHERE conversation_id=?", (cid,))
    x("DELETE FROM conversations WHERE id=?", (cid,))
