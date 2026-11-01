"""Your OK before Kakarot deletes or edits anything.

Every delete (trash a file, forget a memory, cancel a schedule...) and every edit (overwrite or change a file, move or
rename it, change a website, undo a change, risky clicks on the screen) shows an Approve / Deny card in the chat page.
Kakarot then WAITS - it doesn't give up or skip ahead - until you answer, however long that takes
(`approval_timeout_minutes: 0` = wait forever). While it waits you get a Windows pop-up and an Inbox message, and a
reminder every `approval_remind_minutes`.

Settings in config.json: ask_before_delete (true), ask_before_edit (true).
"""
import difflib
import json
import logging
import os
import time

import re

import db
from config import CFG

DELETE_WORDS = re.compile(r"\b(delete|remove|erase|trash|discard|clear all|empty|uninstall|unsubscribe|deactivate|"
                          r"close account)\b|মুছুন|মুছে|ডিলিট|বাতিল", re.I)
EDIT_WORDS = re.compile(r"\b(save|edit|rename|replace|overwrite|update|apply|change|modify|move|archive|reset)\b|"
                        r"সেভ|পরিবর্তন|সম্পাদনা|সংরক্ষণ", re.I)


def kind_of(label):
    """'delete' / 'edit' / None for a button or link label."""
    if DELETE_WORDS.search(label or ""):
        return "delete"
    if EDIT_WORDS.search(label or ""):
        return "edit"
    return None

log = logging.getLogger("kakarot.approve")


def _remind(title, first):
    try:
        import scheduler
        scheduler.notify("Kakarot needs your OK" if first else "Still waiting for your OK", title[:120])
    except Exception:
        pass


def wait(aid, title):
    """Wait for the answer to approval `aid`. Returns True (approved), False (denied) or None (expired)."""
    limit = int(CFG.get("approval_timeout_minutes", 0) or 0) * 60
    remind = max(1, int(CFG.get("approval_remind_minutes", 10))) * 60
    t0 = last = time.time()
    _remind(title, True)
    try:
        db.add_inbox("⏳ Waiting for your OK", f"{title}\n\nOpen the chat page and tap Approve or Deny. "
                                              f"Kakarot waits until you answer.")
    except Exception:
        pass
    while True:
        row = db.q("SELECT status FROM approvals WHERE id=?", (aid,))
        status = row[0]["status"] if row else "denied"
        if status != "pending":
            return status == "approved"
        now = time.time()
        if limit and now - t0 > limit:
            db.x("UPDATE approvals SET status='expired' WHERE id=?", (aid,))
            return None
        if now - last > remind:
            last = now
            _remind(title, False)
        time.sleep(1)


def ask(kind, title, items=None, preview="", reason=""):
    """kind = 'delete' or 'edit'. Returns True when the user approved. Waits until they answer."""
    flag = {"delete": "ask_before_delete", "edit": "ask_before_edit"}.get(kind)
    if flag and not CFG.get(flag, True):
        return True
    detail = json.dumps({"title": title, "items": [str(i) for i in (items or [])][:40],
                         "preview": (preview or "")[:6000]}, ensure_ascii=False)
    aid = db.x("INSERT INTO approvals(kind, detail, reason, status, created) VALUES(?,?,?,?,?)",
               (kind, detail, reason or "", "pending", db.now()))
    log.info("waiting for approval #%s: %s", aid, title)
    return bool(wait(aid, title))


def require(kind, title, items=None, preview="", reason=""):
    """Like ask(), but raises when denied - so the tool stops and the brain is told clearly."""
    if not ask(kind, title, items, preview, reason):
        raise PermissionError(f"The user DENIED this ({title}). Nothing was changed. Don't try it again another "
                              f"way; ask the user what they want instead.")


def text_diff(old, new, name="file", lines=60):
    d = list(difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), f"before: {name}",
                                  f"after: {name}", lineterm="", n=1))
    if len(d) > lines:
        d = d[:lines] + [f"... ({len(d) - lines} more lines)"]
    return "\n".join(d)


def before_overwrite(path, new_text=None, what="file"):
    """Ask before replacing an existing file. New files don't need an OK."""
    if not os.path.exists(path):
        return
    preview = ""
    if new_text is not None:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                old = f.read(200000)
            preview = text_diff(old, new_text, os.path.basename(path))
            if not preview:
                return  # same content - nothing changes
        except Exception:
            preview = ""
    require("edit", f"Replace the existing {what}: {os.path.basename(path)}", [path], preview,
            "The current file will be overwritten.")
