"""Smarter memory: Kakarot notices lasting facts by itself and brings up only what matters for each message.

- After a chat reply, if your message looks like it told Kakarot something lasting (a preference, a person, a
  project, an important fact), it quietly writes it down - never passwords, PINs, codes or card numbers.
- Before answering, it picks the facts that matter for THIS message (all your preferences + the most related other
  facts) instead of pasting everything. That keeps the brain's short memory free for the conversation.
- Everything is visible (and can be deleted) in the Memory tab. Turn off with "auto_memory": false.
"""
import json
import logging
import math
import re
import threading

import db
from config import CFG

log = logging.getLogger("kakarot.recall")
KINDS = ("preference", "person", "project", "fact")
SECRET = re.compile(r"pass ?word|passcode|\bpin\b|\botp\b|\bcvv\b|card (no|number)|account (no|number)|nid\b|"
                    r"পাসওয়ার্ড|পিন|ওটিপি|\d[\d -]{5,}\d", re.I)
# Hints that a message says something lasting about the user (English, Banglish, Bangla)
LASTING = re.compile(r"\b(i am|i'm|im|my|mine|i (always|never|usually|prefer|like|love|hate|want|need|work|live|own)|"
                     r"call me|from now on|always|never|remember|don't ever|please always|amar|ami|amake|"
                     r"amader|our|we (are|have|run|own))\b|আমার|আমি|আমাকে|আমাদের|সবসময়|কখনো না|মনে রাখ", re.I)

EXTRACT = """Read what the user just told their personal assistant and pick out LASTING facts worth remembering for
future chats. Lasting = still true next month: how they want the assistant to behave or reply (kind "preference"),
people in their life and who they are (kind "person"), their businesses / websites / projects (kind "project"),
other stable facts about them (kind "fact": job, city, routine, tools they use).
NOT lasting: today's task, one-off requests, questions, moods, anything the assistant said.
NEVER include passwords, PINs, codes, card/account/ID numbers.
Write each fact as a short third-person sentence in English ("User's office starts at 9am").
Skip facts already known:
{known}

User's message:
\"\"\"{text}\"\"\"

Reply ONLY with JSON: {{"facts": [{{"kind": "preference|person|project|fact", "text": "..."}}]}}  (empty list if none)"""


def init():
    cols = db.q("PRAGMA table_info(memory)")
    names = {c["name"] for c in cols}
    if "kind" not in names:
        db.x("ALTER TABLE memory ADD COLUMN kind TEXT DEFAULT 'fact'")
    if "emb" not in names:
        db.x("ALTER TABLE memory ADD COLUMN emb TEXT")
    if "auto" not in names:
        db.x("ALTER TABLE memory ADD COLUMN auto INTEGER DEFAULT 0")


def _norm(s):
    return re.sub(r"[^\w\s]", "", (s or "").lower()).strip()


def _words(s):
    return {w for w in re.findall(r"\w{3,}", (s or "").lower())}


def _cos(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _embed(texts, kind="document"):
    try:
        import knowledge
        return knowledge.embed(texts, kind)
    except Exception:
        return None


def all_facts():
    try:
        return db.q("SELECT id, fact, COALESCE(kind,'fact') AS kind, emb FROM memory ORDER BY id")
    except Exception:
        return db.q("SELECT id, fact, 'fact' AS kind, NULL AS emb FROM memory ORDER BY id")


def add(fact, kind="fact", auto=False):
    """Save a fact unless it is secret-looking or (nearly) the same as one already saved. Returns the id or None."""
    fact = re.sub(r"\s+", " ", (fact or "")).strip()
    if len(fact) < 4 or SECRET.search(fact):
        return None
    kind = kind if kind in KINDS else "fact"
    facts = all_facts()
    nf = _norm(fact)
    for f in facts:
        if _norm(f["fact"]) == nf:
            return None
    vec = _embed([fact])
    vec = vec[0] if vec else None
    if vec:
        for f in facts:
            try:
                if f.get("emb") and _cos(vec, json.loads(f["emb"])) > 0.93:
                    return None
            except Exception:
                pass
    else:  # no embeddings: word overlap
        w = _words(fact)
        for f in facts:
            o = _words(f["fact"])
            if w and o and len(w & o) / len(w | o) > 0.8:
                return None
    try:
        return db.x("INSERT INTO memory(fact, created, kind, emb, auto) VALUES(?,?,?,?,?)",
                    (fact, db.now(), kind, json.dumps(vec) if vec else None, 1 if auto else 0))
    except Exception:
        return db.x("INSERT INTO memory(fact, created) VALUES(?,?)", (fact, db.now()))


def context(text, k=None):
    """The facts that matter for this message, as lines for the brain."""
    facts = all_facts()
    if not facts:
        return ""
    k = int(k or CFG.get("memory_recall", 10))
    prefs = [f for f in facts if f["kind"] == "preference"][-15:]
    others = [f for f in facts if f["kind"] != "preference"]
    if len(others) > k:
        q = _embed([text[:1500]], "query")
        qv = q[0] if q else None
        qw = _words(text)

        def score(f):
            s = 0.0
            if qv and f.get("emb"):
                try:
                    s = _cos(qv, json.loads(f["emb"]))
                except Exception:
                    pass
            fw = _words(f["fact"])
            if qw and fw:
                s += 0.5 * len(qw & fw) / len(fw)
            return s
        others = sorted(others, key=score, reverse=True)[:k]
        others.sort(key=lambda f: f["id"])
    lines = [f"- (how they like things) {f['fact']}" for f in prefs] + [f"- {f['fact']}" for f in others]
    return "\n".join(lines)


def maybe_learn(user_text, reply=""):
    """Run after a chat reply (in the background): save lasting facts the user just told."""
    if not CFG.get("auto_memory", True):
        return
    t = (user_text or "").strip()
    if len(t) < 12 or not LASTING.search(t):
        return
    t = re.sub(r"\[Attached file: [^\]]*\]", "", t)[:2500]
    threading.Thread(target=_learn, args=(t,), daemon=True, name="kakarot-recall").start()


def _learn(text):
    try:
        import agent
        known = "\n".join(f"- {f['fact']}" for f in all_facts()[-40:]) or "(nothing yet)"
        raw = agent.think_once(EXTRACT.format(known=known, text=text), fmt="json", background=True)
        data = json.loads(raw or "{}")
        saved = []
        for f in (data.get("facts") or [])[:3]:
            if isinstance(f, dict) and f.get("text"):
                if add(str(f["text"]), str(f.get("kind", "fact")).lower(), auto=True):
                    saved.append(f["text"])
        if saved:
            log.info("remembered by itself: %s", saved)
    except Exception as e:
        log.debug("auto memory skipped: %s", e)
