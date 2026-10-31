"""Publishing accounts (GitHub, Netlify, Vercel, FTP/cPanel) kept safely on this PC.

- Saved in data/accounts.json. Passwords and tokens are encrypted with your Windows login (Windows DPAPI), so the
  file is useless on another PC or another Windows user.
- The AI brain NEVER sees a password or token: tools only use the account's name (e.g. "ftp:lalsobuj").
- Every error message is cleaned of secrets before it is shown or logged.
"""
import base64
import json
import os
import re
import threading

from config import BASE

PATH = os.path.join(BASE, "data", "accounts.json")
_lock = threading.Lock()

KINDS = {
    # kind: (public fields, secret fields)
    "github": (["user"], ["token"]),
    "netlify": ([], ["token"]),
    "vercel": (["team"], ["token"]),
    "ftp": (["label", "host", "port", "user", "folder", "site_url", "secure", "verify_cert", "allow_plain"],
            ["password"]),
}


# ---------------------------------------------------------------- encryption (Windows DPAPI)
def _dpapi(data, protect):
    import ctypes
    import ctypes.wintypes as wt

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if protect:
        ok = fn(ctypes.byref(inp), "Kakarot", None, None, None, 0x01, ctypes.byref(out))
    else:
        ok = fn(ctypes.byref(inp), None, None, None, None, 0x01, ctypes.byref(out))
    if not ok:
        raise OSError("Windows could not " + ("protect" if protect else "unlock") + " the saved secret")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _seal(obj):
    raw = json.dumps(obj).encode()
    if os.name == "nt":
        try:
            return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode()
        except Exception as e:  # very unusual - keep working, but say so in the log
            import logging
            logging.getLogger("kakarot.vault").warning("Windows encryption unavailable (%s); saved encoded only", e)
    return "plain:" + base64.b64encode(raw).decode()  # non-Windows (tests): no OS keystore


def _open(blob):
    if not blob:
        return {}
    kind, _, data = blob.partition(":")
    raw = base64.b64decode(data)
    if kind == "dpapi":
        raw = _dpapi(raw, False)
    return json.loads(raw.decode())


# ---------------------------------------------------------------- storage
def _load():
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"version": 1, "items": {}}


def _save(data):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, PATH)


def slug(label):
    return re.sub(r"[^a-z0-9_-]+", "-", (label or "").lower()).strip("-")[:30] or "site"


def key_for(kind, label=""):
    return f"ftp:{slug(label)}" if kind == "ftp" else kind


def _hint(v):
    v = str(v or "")
    return "" if not v else (v[:4] + "…" + v[-4:] if len(v) > 12 else "•" * min(len(v), 8))


def save(kind, fields):
    """Create/update an account. Empty secret fields keep the saved value (so editing doesn't need re-typing)."""
    if kind not in KINDS:
        raise ValueError(f"unknown account type {kind}")
    pub_f, sec_f = KINDS[kind]
    key = key_for(kind, fields.get("label", ""))
    with _lock:
        data = _load()
        item = data["items"].get(key) or {"kind": kind, "public": {}, "secret": ""}
        pub = item["public"]
        for f in pub_f:
            if f in fields:
                pub[f] = fields[f]
        if kind == "ftp":
            pub["label"] = fields.get("label") or pub.get("label") or key[4:]
            pub["host"] = re.sub(r"^s?ftps?://", "", str(pub.get("host", "")).strip()).strip("/")
            pub["port"] = int(pub.get("port") or 21)
            pub.setdefault("secure", True)
            pub.setdefault("verify_cert", True)
            pub.setdefault("allow_plain", False)
            pub["folder"] = (pub.get("folder") or "").strip() or "public_html"
            if pub.get("site_url") and not re.match(r"^https?://", pub["site_url"]):
                pub["site_url"] = "https://" + pub["site_url"].strip()
            if not pub["host"] or not pub.get("user"):
                raise ValueError("FTP needs at least the server (host) and the user name")
        sec = _open(item.get("secret"))
        for f in sec_f:
            v = fields.get(f)
            if v not in (None, ""):
                sec[f] = str(v).strip()
        if not any(sec.get(f) for f in sec_f):
            raise ValueError("Please enter the " + " / ".join(sec_f))
        item["secret"] = _seal(sec)
        item["hint"] = {f: _hint(sec.get(f)) for f in sec_f}
        data["items"][key] = item
        _save(data)
    return key


def remove(key):
    with _lock:
        data = _load()
        data["items"].pop(key, None)
        _save(data)


def get(key):
    """Full account incl. secrets - for internal use only (never return this to the AI or the browser)."""
    item = _load()["items"].get(key)
    if not item:
        return None
    out = dict(item["public"])
    out.update(_open(item.get("secret")))
    out["key"], out["kind"] = key, item["kind"]
    return out


def has(key):
    return key in _load()["items"]


def listing():
    """Safe view: no secrets, only hints like 'ghp_…9f2a'."""
    out = []
    for key, item in sorted(_load()["items"].items()):
        out.append({"key": key, "kind": item["kind"], **item["public"], "hint": item.get("hint", {})})
    return out


def ftp_for_host(host):
    """The FTP account whose website link matches this host (lalsobujdata.site -> ftp:lalsobuj)."""
    host = (host or "").lower().removeprefix("www.")
    for a in listing():
        if a["kind"] == "ftp" and a.get("site_url"):
            from urllib.parse import urlparse
            h = (urlparse(a["site_url"]).hostname or "").lower().removeprefix("www.")
            if h == host:
                return a["key"]
    return None


def scrub(text):
    """Remove every saved secret from a text (for error messages and logs)."""
    text = str(text or "")
    try:
        for key in _load()["items"]:
            acc = get(key) or {}
            for f in KINDS.get(acc.get("kind"), ([], []))[1]:
                v = acc.get(f)
                if v and len(v) >= 4:
                    text = text.replace(v, "•••")
    except Exception:
        pass
    return re.sub(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", "•••", text)
