"""Previews of websites Kakarot built.
- Static sites are served straight from their folder at /sites/<name>/
- Flask apps run as their own small server; Kakarot passes /apps/<name>/... through to it, so the preview works
  on your phone through the normal Kakarot link (no extra ports or setup)."""
import atexit
import hashlib
import json
import os
import socket
import subprocess
import threading
import time

import requests

import coder

_procs = {}
_lock = threading.Lock()


def project_dir(name):
    return coder.code_path(name)


def spec_of(name):
    try:
        with open(os.path.join(project_dir(name), "kakarot-site.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def kind_of(name):
    s = spec_of(name) or {}
    if s.get("type"):
        return s["type"]
    return "flask" if os.path.exists(os.path.join(project_dir(name), "app.py")) else "static"


def preview_path(name):
    return f"/{'apps' if kind_of(name) == 'flask' else 'sites'}/{name}/"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def install_requirements(pdir, log=None):
    """Create the project's .venv and install requirements.txt (only again when it changes)."""
    vdir, scripts = coder._venv(pdir)
    req = os.path.join(pdir, "requirements.txt")
    if not os.path.exists(req):
        return scripts, ""
    with open(req, "rb") as f:
        digest = hashlib.sha1(f.read()).hexdigest()
    stamp = os.path.join(vdir, ".kakarot-installed")
    if os.path.exists(stamp) and open(stamp).read() == digest:
        return scripts, ""
    py = os.path.join(scripts, "python.exe" if coder.WIN else "python")
    r = subprocess.run([py, "-m", "pip", "install", "-q", "-r", req], cwd=pdir, capture_output=True, text=True,
                       timeout=900)
    if r.returncode != 0:
        return scripts, "Installing packages failed:\n" + (r.stderr or r.stdout)[-2500:]
    with open(stamp, "w") as f:
        f.write(digest)
    return scripts, ""


def start_app(pdir, port):
    """Start a Flask app; returns (process, error_text). Output goes to server.log in the project."""
    scripts, err = install_requirements(pdir)
    if err:
        return None, err
    py = os.path.join(scripts, "python.exe" if coder.WIN else "python")
    env = dict(os.environ, PORT=str(port), PYTHONIOENCODING="utf-8", FLASK_DEBUG="0",
               VIRTUAL_ENV=os.path.dirname(scripts), PATH=scripts + os.pathsep + os.environ.get("PATH", ""))
    logf = open(os.path.join(pdir, "server.log"), "w", encoding="utf-8")
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if coder.WIN else {"start_new_session": True}
    proc = subprocess.Popen([py, "app.py"], cwd=pdir, env=env, stdout=logf, stderr=subprocess.STDOUT, **kw)
    t0 = time.time()
    while time.time() - t0 < 60:
        if proc.poll() is not None:
            logf.close()
            return None, "The app stopped right away:\n" + read_log(pdir)
        try:
            requests.get(f"http://127.0.0.1:{port}/", timeout=3)
            return proc, ""
        except requests.RequestException:
            time.sleep(1)
    coder._kill(proc)
    return None, "The app did not answer within 60 seconds.\n" + read_log(pdir)


def read_log(pdir, limit=4000):
    try:
        with open(os.path.join(pdir, "server.log"), encoding="utf-8", errors="replace") as f:
            return f.read()[-limit:]
    except Exception:
        return ""


def ensure_running(name):
    with _lock:
        p = _procs.get(name)
        if p and p["proc"].poll() is None:
            return p["port"]
        port = free_port()
        proc, err = start_app(project_dir(name), port)
        if not proc:
            raise RuntimeError(err)
        _procs[name] = {"proc": proc, "port": port}
        return port


def stop(name):
    with _lock:
        p = _procs.pop(name, None)
        if p:
            coder._kill(p["proc"])


@atexit.register
def _stop_all():
    for n in list(_procs):
        stop(n)


HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "content-encoding", "content-length", "host"}


def proxy(name, sub, req):
    """Pass one browser request through to the app. Returns (body, status, headers)."""
    port = ensure_running(name)
    url = f"http://127.0.0.1:{port}/{sub}"
    headers = {k: v for k, v in req.headers.items() if k.lower() not in HOP}
    headers.update({"X-Forwarded-Prefix": f"/apps/{name}", "X-Forwarded-Host": req.host,
                    "X-Forwarded-Proto": req.scheme, "X-Forwarded-For": req.remote_addr or ""})
    r = requests.request(req.method, url, params=req.args, data=req.get_data(), headers=headers,
                         cookies=req.cookies, allow_redirects=False, timeout=120)
    out = []
    for k, v in r.raw.headers.items():
        if k.lower() in HOP:
            continue
        if k.lower() == "location":
            v = v.replace(f"http://127.0.0.1:{port}", "")
            if v.startswith("/") and not v.startswith(f"/apps/{name}"):
                v = f"/apps/{name}" + v
        out.append((k, v))
    return r.content, r.status_code, out
