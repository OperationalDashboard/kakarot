"""PIN lock for phone / other devices. The PC itself (127.0.0.1) never needs the PIN."""
import hmac
import json
import os
import secrets
import socket
import time

from flask import jsonify, redirect, request, session

from config import BASE, CFG

LOCAL = {"127.0.0.1", "::1"}
PUBLIC = bool(CFG.get("public_access", False))  # reachable from the internet -> no exceptions
_fails = {}  # ip -> [count, first_fail_time]
_global = []  # times of recent failed logins from anywhere


def ensure_pin():
    """Create a random PIN/password on first start and save it into config.json.
    Internet mode needs a stronger one (10 characters)."""
    pin = str(CFG.get("pin", "")).strip()
    if pin and (not PUBLIC or len(pin) >= 8):
        return pin
    if PUBLIC:
        abc = "abcdefghjkmnpqrstuvwxyz23456789"
        pin = "".join(secrets.choice(abc) for _ in range(10))
    else:
        pin = f"{secrets.randbelow(10**6):06d}"
    path = os.path.join(BASE, "config.json")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    raw["pin"] = pin
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
    CFG["pin"] = pin
    return pin


def secret_key():
    path = os.path.join(BASE, "data", "secret.key")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "your-PC-IP"
    finally:
        s.close()


def client_ip():
    fwd = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() or request.remote_addr) if fwd else request.remote_addr


def is_trusted_local():
    """The PC itself skips the PIN - but never in internet mode, because tunnel traffic also looks local."""
    return not PUBLIC and request.remote_addr in LOCAL


def _locked(ip):
    now = time.time()
    _global[:] = [t for t in _global if now - t < 900]
    if len(_global) >= 20:  # someone is guessing from many addresses: pause all logins 15 min
        return True
    n, t = _fails.get(ip, [0, 0])
    if n >= 5 and time.time() - t < 300:
        return True
    if time.time() - t >= 300:
        _fails.pop(ip, None)
    return False


LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Kakarot - unlock</title>
<style>:root{--bg:#f6f7f5;--p:#fff;--i:#1d2320;--a:#1f7a5a;--w:#b5462f;--l:#e3e6e2}
@media(prefers-color-scheme:dark){:root{--bg:#121513;--p:#1a1e1c;--i:#e6ebe8;--a:#3fb886;--w:#ff8a70;--l:#2b312e}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--i);font:16px "Segoe UI",system-ui,sans-serif}
form{background:var(--p);border:1px solid var(--l);border-radius:16px;padding:28px 24px;width:min(320px,90vw);text-align:center}
.logo{width:48px;height:48px;border-radius:14px;background:var(--a);color:#fff;display:grid;place-items:center;font-weight:700;font-size:22px;margin:0 auto 12px}
input{width:100%;box-sizing:border-box;font-size:26px;letter-spacing:8px;text-align:center;padding:10px;border:1px solid var(--l);border-radius:10px;background:var(--bg);color:var(--i);margin:14px 0}
button{width:100%;padding:12px;border:0;border-radius:10px;background:var(--a);color:#fff;font-size:16px;font-weight:600}
.e{color:var(--w);font-size:14px;min-height:18px}</style></head><body>
<form method="post" action="/login"><div class="logo">K</div><b>__NAME__</b>
<input name="pin" type="password" inputmode="__MODE__" autocomplete="current-password" placeholder="PIN" autofocus>
<div class="e">__ERR__</div><button>Unlock</button></form></body></html>"""


def register(app):
    app.secret_key = secret_key()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_NAME="kakarot_session", SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)
    pin = ensure_pin()

    def page(err=""):
        return (LOGIN_PAGE.replace("__NAME__", CFG.get("name", "Kakarot")).replace("__ERR__", err)
                .replace("__MODE__", "numeric" if pin.isdigit() else "text")
                .replace('placeholder="PIN"', 'placeholder="PIN"' if pin.isdigit() else 'placeholder="Password" style="letter-spacing:2px;font-size:20px"'))

    @app.before_request
    def guard():
        if is_trusted_local() or session.get("ok") or request.path in ("/login", "/static/manifest.json", "/static/icon.svg"):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "locked"}), 401
        return redirect("/login")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        ip = client_ip()
        if request.method == "GET":
            return page()
        if _locked(ip):
            return page("Too many tries. Wait 5 minutes."), 429
        if hmac.compare_digest(request.form.get("pin", "").strip(), pin):
            _fails.pop(ip, None)
            session.permanent = True
            session["ok"] = True
            return redirect("/")
        n, t = _fails.get(ip, [0, time.time()])
        _fails[ip] = [n + 1, t]
        _global.append(time.time())
        return page("Wrong PIN."), 401

    @app.post("/logout")
    def logout():
        session.clear()
        return jsonify({"ok": True})

    return pin
