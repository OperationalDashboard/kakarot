"""Previews of websites Kakarot built or opened.
- Static sites (and built React/Vite sites) are served straight from their folder at /sites/<name>/
- Flask, PHP and Node.js apps run as their own small server; Kakarot passes /apps/<name>/... through to it, so the
  preview works on your phone through the normal Kakarot link (no extra ports or setup).
- Sites made for the root of a domain (links like /css/style.css) are rewritten on the fly for the preview only."""
import atexit
import contextlib
import functools
import hashlib
import http.server
import json
import mimetypes
import os
import re
import shutil
import socket
import socketserver
import subprocess
import threading
import time

import requests

import coder

# Windows sometimes maps .js to text/plain in the registry -> browsers refuse to run module scripts
for _ext, _type in ((".js", "application/javascript"), (".mjs", "application/javascript"), (".css", "text/css"),
                    (".svg", "image/svg+xml"), (".json", "application/json"), (".webp", "image/webp"),
                    (".wasm", "application/wasm"), (".woff2", "font/woff2"), (".webmanifest", "application/manifest+json")):
    mimetypes.add_type(_type, _ext)

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


RUNS = ("flask", "php", "node-server")   # these need a running server for the preview


def preview_path(name):
    return f"/{'apps' if kind_of(name) in RUNS else 'sites'}/{name}/"


def static_root(name):
    """Folder served for a static preview (builds React/Vite sites the first time)."""
    import siteproject as sp
    spec = spec_of(name) or {}
    pdir = project_dir(name)
    root = sp.web_root(pdir, spec)
    if spec.get("type") == "node-build" and not os.path.isfile(os.path.join(root, "index.html")):
        import deploy
        with _lock:
            if not os.path.isfile(os.path.join(root, "index.html")):
                root = deploy.build(pdir, spec)
    return root


def rewrite_root(body, prefix, ctype):
    """For the preview only: make links that start with '/' point inside /sites/<name>/ (or /apps/<name>/)."""
    if not body or not re.search(r"text/html|text/css", ctype or ""):
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    esc = re.escape(prefix.rstrip("/"))
    if "html" in ctype:
        text = re.sub(r"""((?:href|src|action|poster|data-src)\s*=\s*["'])/(?!/)(?!""" + esc[1:] + r")",
                      r"\1" + prefix, text, flags=re.I)
        text = re.sub(r"""(srcset\s*=\s*["'])/(?!/)""", r"\1" + prefix, text, flags=re.I)
    text = re.sub(r"""url\(\s*(['"]?)/(?!/)(?!""" + esc[1:] + r")", r"url(\1" + prefix, text)
    return text.encode("utf-8")


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


def _app_command(pdir, port, spec):
    """Command line + environment to run the site's server."""
    import siteproject as sp
    t = (spec or {}).get("type") or "flask"
    if t == "php":
        php = shutil.which("php")
        if not php:
            return None, None, ("PHP isn't installed on this PC, so this PHP site can't run here. Install PHP "
                                "(windows.php.net → VS16 x64 Thread Safe zip, add the folder to PATH) to preview and "
                                "test it. Code checks and publishing still work.")
        return [php, "-S", f"127.0.0.1:{port}", "-t", sp.web_root(pdir, spec)], dict(os.environ), ""
    if t == "node-server":
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            return None, None, "Node.js isn't installed on this PC (nodejs.org), so this app can't run here."
        if not os.path.isdir(os.path.join(pdir, "node_modules")):
            r = subprocess.run([npm, "install"], cwd=pdir, capture_output=True, text=True, timeout=1800,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0:
                return None, None, "npm install failed:\n" + (r.stderr or r.stdout)[-2000:]
        return [npm, "start"], dict(os.environ, PORT=str(port), HOST="127.0.0.1"), ""
    scripts, err = install_requirements(pdir)
    if err:
        return None, None, err
    py = os.path.join(scripts, "python.exe" if coder.WIN else "python")
    entry = next((f for f in ("app.py", "main.py", "wsgi.py", "run.py") if os.path.exists(os.path.join(pdir, f))), "app.py")
    env = dict(os.environ, PORT=str(port), PYTHONIOENCODING="utf-8", FLASK_DEBUG="0", FLASK_RUN_PORT=str(port),
               VIRTUAL_ENV=os.path.dirname(scripts), PATH=scripts + os.pathsep + os.environ.get("PATH", ""))
    return [py, entry], env, ""


def start_app(pdir, port, spec=None):
    """Start the site's server; returns (process, error_text). Output goes to server.log in the project."""
    if spec is None:
        try:
            with open(os.path.join(pdir, "kakarot-site.json"), encoding="utf-8") as f:
                spec = json.load(f)
        except (OSError, ValueError):
            spec = {"type": "flask"}
    cmd, env, err = _app_command(pdir, port, spec)
    if err:
        return None, err
    logf = open(os.path.join(pdir, "server.log"), "w", encoding="utf-8")
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if coder.WIN else {"start_new_session": True}
    proc = subprocess.Popen(cmd, cwd=pdir, env=env, stdout=logf, stderr=subprocess.STDOUT, **kw)
    t0 = time.time()
    while time.time() - t0 < 90:
        if proc.poll() is not None:
            logf.close()
            return None, "The app stopped right away:\n" + read_log(pdir)
        try:
            requests.get(f"http://127.0.0.1:{port}/", timeout=3)
            return proc, ""
        except requests.RequestException:
            time.sleep(1)
    coder._kill(proc)
    return None, "The app did not answer within 90 seconds.\n" + read_log(pdir)


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
    body = rewrite_root(r.content, f"/apps/{name}/", r.headers.get("content-type", ""))
    out = []
    for k, v in r.raw.headers.items():
        if k.lower() in HOP:
            continue
        if k.lower() == "location":
            v = v.replace(f"http://127.0.0.1:{port}", "")
            if v.startswith("/") and not v.startswith(f"/apps/{name}"):
                v = f"/apps/{name}" + v
        out.append((k, v))
    return body, r.status_code, out


class _Quiet(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".js": "application/javascript", ".mjs": "application/javascript", ".css": "text/css",
                      ".svg": "image/svg+xml", ".json": "application/json", ".webp": "image/webp", ".wasm": "application/wasm"}

    def log_message(self, *a):
        pass


def serve_folder(folder):
    handler = functools.partial(_Quiet, directory=folder)
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


@contextlib.contextmanager
def local_server(pdir, spec):
    """Run the site on this PC for testing. Yields (base_url, None) or (None, reason it can't run)."""
    import siteproject as sp
    t = spec.get("type") or "static"
    if t in RUNS:
        port = free_port()
        proc, err = start_app(pdir, port, spec)
        if not proc:
            yield None, err
            return
        try:
            yield f"http://127.0.0.1:{port}/", None
        finally:
            coder._kill(proc)
        return
    if t == "node-build":
        try:
            import deploy
            root = deploy.build(pdir, spec)
        except Exception as e:
            yield None, str(e)
            return
    else:
        root = sp.web_root(pdir, spec)
    if not os.path.isdir(root):
        yield None, f"The site folder {root} doesn't exist."
        return
    srv, base = serve_folder(root)
    try:
        yield base, None
    finally:
        srv.shutdown()
        srv.server_close()
