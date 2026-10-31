"""Publishing websites: GitHub (+ free GitHub Pages hosting), FTP/cPanel hosting (e.g. Namecheap), Netlify, Vercel,
or back into the folder the site came from.

Safety rules (every target):
  - only CHANGED files are sent; nothing on the server is deleted unless you ask for it
  - before a file on your hosting is replaced, the old one is downloaded as a backup -> one-click undo
  - files that look secret (.env, keys, databases, wp-config.php) are never sent to public places
  - if somebody changed the same file on GitHub/the server since Kakarot last synced, it stops and tells you
  - after publishing, the live site is opened to make sure the new version is really there and works
"""
import base64
import concurrent.futures as cf
import ftplib
import json
import os
import posixpath
import re
import shutil
import ssl
import subprocess
import time
from urllib.parse import quote, urljoin, urlparse

import requests

import siteproject as sp
import vault
from config import CFG
from tools import UA


class DeployError(Exception):
    pass


def fail(msg):
    raise DeployError(vault.scrub(msg))


# ================================================================= GitHub
class GitHub:
    def __init__(self, token=None, anonymous=False):
        if token is None and not anonymous:
            acc = vault.get("github")
            token = acc.get("token") if acc else None
        self.token = token
        self.api = CFG.get("github_api", "https://api.github.com").rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                               "User-Agent": "Kakarot-assistant"})
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"

    def call(self, method, path, timeout=60, **kw):
        url = path if path.startswith("http") else self.api + path
        for attempt in range(4):
            try:
                r = self.s.request(method, url, timeout=timeout, **kw)
            except requests.RequestException as e:
                if attempt == 3:
                    fail(f"Can't reach GitHub ({e.__class__.__name__}). Is the internet working?")
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (502, 503, 504) and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
                fail("GitHub's hourly limit is used up - try again later." +
                     ("" if self.token else " (Adding a GitHub token raises the limit a lot.)"))
            return r

    def _fail(self, r, what):
        try:
            body = r.json()
            msg = body.get("message", "")
            if body.get("errors"):
                msg += " " + json.dumps(body["errors"])[:300]
        except ValueError:
            msg = r.text[:200]
        hint = {401: "The GitHub token is wrong or expired - make a new one and save it in Websites → Accounts.",
                403: "The token isn't allowed to do this. Give it 'Contents: Read and write' (plus 'Administration' "
                     "to create repositories and 'Pages' for GitHub Pages).",
                404: "Not found - check the name, or the token has no access to that repository.",
                409: "The repository is empty or busy.",
                422: "GitHub refused it."}.get(r.status_code, "")
        fail(f"GitHub: {what or 'request'} failed (HTTP {r.status_code}): {msg}. {hint}".strip())

    def j(self, method, path, ok=(200, 201), what="", **kw):
        r = self.call(method, path, **kw)
        if r.status_code not in ok:
            self._fail(r, what)
        return r.json() if r.content else {}

    def login(self):
        if not self.token:
            fail("No GitHub token saved yet. Add one in Websites → Accounts (github.com → Settings → Developer "
                 "settings → Fine-grained tokens).")
        return self.j("GET", "/user", what="sign in")["login"]

    def repo(self, o, r):
        resp = self.call("GET", f"/repos/{o}/{r}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            self._fail(resp, "read repository")
        return resp.json()

    def create_repo(self, o, r, private, description=""):
        me = self.login()
        body = {"name": r, "private": bool(private), "description": description[:300], "auto_init": True}
        path = "/user/repos" if o.lower() == me.lower() else f"/orgs/{o}/repos"
        out = self.j("POST", path, json=body, what="create repository")
        for _ in range(10):  # the first commit takes a moment
            if self.head(o, r, out.get("default_branch") or "main"):
                break
            time.sleep(1.5)
        return out

    def head(self, o, r, branch):
        resp = self.call("GET", f"/repos/{o}/{r}/git/ref/heads/{quote(branch, safe='')}")
        if resp.status_code in (404, 409):
            return None
        if resp.status_code != 200:
            self._fail(resp, "read branch")
        obj = resp.json()
        if isinstance(obj, list):  # partial match list -> branch doesn't exist exactly
            return None
        return obj["object"]["sha"]

    def commit_tree(self, o, r, sha):
        return self.j("GET", f"/repos/{o}/{r}/git/commits/{sha}", what="read commit")["tree"]["sha"]

    def tree(self, o, r, tree_sha):
        d = self.j("GET", f"/repos/{o}/{r}/git/trees/{tree_sha}?recursive=1", what="read files")
        return {e["path"]: (e["sha"], e["mode"]) for e in d.get("tree", []) if e["type"] == "blob"}, d.get("truncated")

    def files_at(self, o, r, branch):
        head = self.head(o, r, branch)
        if not head:
            return None, None, {}
        tsha = self.commit_tree(o, r, head)
        files, truncated = self.tree(o, r, tsha)
        if truncated:
            fail("This repository is too big for Kakarot to read in one go (GitHub cut the file list).")
        return head, tsha, files

    def blob(self, o, r, data):
        return self.j("POST", f"/repos/{o}/{r}/git/blobs", what="upload file", timeout=180,
                      json={"content": base64.b64encode(data).decode(), "encoding": "base64"})["sha"]

    def read_blob(self, o, r, sha):
        d = self.j("GET", f"/repos/{o}/{r}/git/blobs/{sha}", what="download file", timeout=180)
        return base64.b64decode(d.get("content", ""))

    def write_commit(self, o, r, branch, entries, message, parent, base_tree=None, new_branch=False):
        body = {"tree": entries}
        if base_tree:
            body["base_tree"] = base_tree
        tree = self.j("POST", f"/repos/{o}/{r}/git/trees", json=body, what="build the file tree", timeout=180)["sha"]
        c = self.j("POST", f"/repos/{o}/{r}/git/commits", what="make commit",
                   json={"message": message, "tree": tree, "parents": [parent] if parent else []})["sha"]
        if new_branch:
            self.j("POST", f"/repos/{o}/{r}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": c},
                   what="create branch")
        else:
            resp = self.call("PATCH", f"/repos/{o}/{r}/git/refs/heads/{quote(branch, safe='')}",
                             json={"sha": c, "force": False})
            if resp.status_code == 422:
                raise _Moved()
            if resp.status_code != 200:
                self._fail(resp, "update branch")
        return c

    def seed_empty(self, o, r, branch):
        """Git Data API doesn't work on an empty repository: add a first README through the contents API."""
        self.j("PUT", f"/repos/{o}/{r}/contents/README.md", what="start empty repository",
               json={"message": "Start repository", "content": base64.b64encode(f"# {r}\n".encode()).decode(),
                     "branch": branch})

    def zipball(self, o, r, ref, dest):
        resp = self.s.get(f"{self.api}/repos/{o}/{r}/zipball/{ref}", stream=True, timeout=600, allow_redirects=True)
        if resp.status_code != 200:
            self._fail(resp, "download repository")
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(1 << 16):
                f.write(chunk)

    def pages(self, o, r):
        resp = self.call("GET", f"/repos/{o}/{r}/pages")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            self._fail(resp, "read GitHub Pages")
        return resp.json()

    def enable_pages(self, o, r, branch, path="/"):
        cur = self.pages(o, r)
        src = {"branch": branch, "path": path}
        if cur is None:
            resp = self.call("POST", f"/repos/{o}/{r}/pages", json={"source": src})
            if resp.status_code not in (201, 409):
                self._fail(resp, "turn on GitHub Pages")
        elif (cur.get("source") or {}) != src:
            resp = self.call("PUT", f"/repos/{o}/{r}/pages", json={"source": src})
            if resp.status_code not in (204, 200):
                self._fail(resp, "change GitHub Pages")
        for _ in range(5):
            cur = self.pages(o, r)
            if cur:
                return cur
            time.sleep(2)
        return cur or {}

    def pages_status(self, o, r):
        resp = self.call("GET", f"/repos/{o}/{r}/pages/builds/latest")
        if resp.status_code != 200:
            return None, ""
        d = resp.json()
        return d.get("status"), (d.get("error") or {}).get("message") or ""


class _Moved(Exception):
    """The branch moved while we were publishing (someone pushed) - start again."""


def parse_repo(text):
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/tree/([^/\s?#]+))?/?$",
                 (text or "").strip())
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = re.match(r"^([\w.-]+)/([\w.-]+?)(?:\.git)?(?:@([\w./-]+))?$", (text or "").strip())
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


# ================================================================= FTP / cPanel
class _SafeSSLSocket(ssl.SSLSocket):
    """Some FTP servers close the data connection without a clean TLS goodbye - the data is complete anyway."""

    def unwrap(self):
        try:
            return super().unwrap()
        except (ssl.SSLError, OSError):
            return None


class _FTPS(ftplib.FTP_TLS):
    """FTPS that re-uses the TLS session on the data connection (needed by most cPanel / vsftpd servers)."""

    def __init__(self, *a, context=None, **kw):
        if context is not None:
            context.sslsocket_class = _SafeSSLSocket
        super().__init__(*a, context=context, **kw)

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


class FTP:
    def __init__(self, key):
        self.a = vault.get(key)
        if not self.a or self.a.get("kind") != "ftp":
            fail(f"There is no FTP account called '{key}'. Add it in Websites → Accounts.")
        self.key, self.ftp, self.secure, self._dirs, self.mlsd_ok = key, None, False, set(), True

    def __enter__(self):
        return self.connect()

    def __exit__(self, *a):
        self.close()

    def connect(self):
        a = self.a
        host, port, errors = a["host"], int(a.get("port") or 21), []
        if a.get("secure", True):
            ctx = ssl.create_default_context()
            if not a.get("verify_cert", True):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            try:
                f = _FTPS(context=ctx, timeout=60)
                f.connect(host, port)
                f.auth()
                f.login(a["user"], a.get("password", ""))
                f.prot_p()
                self.ftp, self.secure = f, True
            except ssl.SSLCertVerificationError as e:
                errors.append(f"the server's certificate doesn't match ({e.verify_message}). In cPanel the FTP "
                              f"server name is often like server123.web-hosting.com - use that as the server, or "
                              f"tick 'Skip certificate check' for this account")
            except ftplib.error_perm as e:
                if str(e).startswith("530"):
                    fail("FTP login failed: wrong user name or password (in cPanel → FTP Accounts the user name "
                         "usually looks like name@yourdomain.com).")
                errors.append(str(e))
            except (OSError, ftplib.Error, EOFError) as e:
                errors.append(f"{e.__class__.__name__}: {e}")
        if not self.ftp:
            if a.get("secure", True) and not a.get("allow_plain"):
                fail(f"Could not connect securely to {host}: {'; '.join(errors)}.")
            try:
                f = ftplib.FTP(timeout=60)
                f.connect(host, port)
                f.login(a["user"], a.get("password", ""))
                self.ftp, self.secure = f, False
            except ftplib.error_perm as e:
                fail("FTP login failed: wrong user name or password." if str(e).startswith("530") else f"FTP: {e}")
            except (OSError, ftplib.Error, EOFError) as e:
                fail(f"Can't connect to the FTP server {host}:{port} ({e}).")
        self.ftp.set_pasv(True)
        try:
            self.ftp.sendcmd("OPTS UTF8 ON")
        except ftplib.Error:
            pass
        self.home = self.ftp.pwd()
        return self

    def close(self):
        if self.ftp:
            try:
                self.ftp.quit()
            except Exception:
                try:
                    self.ftp.close()
                except Exception:
                    pass
            self.ftp = None

    def base(self, folder=None):
        folder = (folder if folder is not None else self.a.get("folder") or "").strip()
        if folder.startswith("/"):
            return folder.rstrip("/") or "/"
        return posixpath.join(self.home, folder).rstrip("/") if folder else self.home

    def listdir(self, path):
        """[(name, is_dir, size, modify)]"""
        if self.mlsd_ok:
            try:
                out = []
                for name, facts in self.ftp.mlsd(path, facts=["type", "size", "modify"]):
                    t = facts.get("type", "")
                    if t in ("cdir", "pdir") or name in (".", ".."):
                        continue
                    out.append((name, t == "dir", int(facts.get("size") or 0), facts.get("modify", "")))
                return out
            except ftplib.error_perm as e:
                if not str(e).startswith(("500", "501", "502")):
                    raise
                self.mlsd_ok = False
        lines = []
        self.ftp.retrlines(f"LIST -a {path}", lines.append)
        out = []
        for ln in lines:
            m = re.match(r"^([dl-])[rwxsStT-]{9}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\w{3}\s+\d+\s+[\d:]+)\s+(.+)$", ln)
            if not m:
                continue
            name = m.group(4).split(" -> ")[0]
            if name in (".", ".."):
                continue
            out.append((name, m.group(1) == "d", int(m.group(2)), m.group(3)))
        return out

    def walk(self, base, skip=None, limit=10000):
        """Yield (rel, size, modify) for every file under base."""
        stack, n = [""], 0
        while stack:
            sub = stack.pop()
            try:
                entries = self.listdir(posixpath.join(base, sub) if sub else base)
            except ftplib.error_perm:
                continue
            for name, is_dir, size, mod in entries:
                rel = f"{sub}/{name}" if sub else name
                if skip and skip(rel, is_dir):
                    continue
                if is_dir:
                    stack.append(rel)
                else:
                    n += 1
                    yield rel, size, mod
                    if n >= limit:
                        return

    def stat(self, path):
        """(size, modify) or None if the file doesn't exist."""
        try:
            size = self.ftp.size(path)
        except ftplib.error_perm:
            return None
        except ftplib.Error:
            size = None
        try:
            mod = self.ftp.voidcmd(f"MDTM {path}")[4:].strip()
        except ftplib.Error:
            mod = ""
        return (size, mod)

    def read(self, path):
        chunks = []
        try:
            self.ftp.retrbinary(f"RETR {path}", chunks.append)
        except ftplib.error_perm:
            return None
        return b"".join(chunks)

    def download(self, path, local):
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local + ".part", "wb") as f:
            self.ftp.retrbinary(f"RETR {path}", f.write)
        os.replace(local + ".part", local)

    def mkdirs(self, d):
        if not d or d in self._dirs or d == "/":
            return
        parts = d.strip("/").split("/")
        cur = "/" if d.startswith("/") else ""
        for p in parts:
            cur = posixpath.join(cur, p) if cur else p
            if cur in self._dirs:
                continue
            try:
                self.ftp.mkd(cur)
            except ftplib.error_perm:
                pass
            self._dirs.add(cur)

    def upload(self, local, path):
        """Upload to a temporary name, then rename -> visitors never see a half-uploaded file."""
        self.mkdirs(posixpath.dirname(path))
        tmp = path + ".kakarot-tmp"
        with open(local, "rb") as f:
            self.ftp.storbinary(f"STOR {tmp}", f)
        try:
            self.ftp.rename(tmp, path)
        except ftplib.error_perm:
            try:
                self.ftp.delete(path)
                self.ftp.rename(tmp, path)
            except ftplib.error_perm:
                with open(local, "rb") as f:
                    self.ftp.storbinary(f"STOR {path}", f)
                try:
                    self.ftp.delete(tmp)
                except ftplib.Error:
                    pass

    def delete(self, path):
        try:
            self.ftp.delete(path)
            return True
        except ftplib.error_perm:
            return False


def test_ftp(key):
    with FTP(key) as f:
        base = f.base()
        names = [n for n, *_ in f.listdir(base)][:12]
        return (f"Connected {'securely (FTPS)' if f.secure else 'WITHOUT encryption (plain FTP)'} to {f.a['host']}. "
                f"Folder {base} has: {', '.join(names) or '(empty)'}")


# ================================================================= Netlify
class Netlify:
    def __init__(self):
        acc = vault.get("netlify")
        if not acc:
            fail("No Netlify token saved yet. Add one in Websites → Accounts (app.netlify.com → User settings → "
                 "Applications → Personal access tokens).")
        self.api = CFG.get("netlify_api", "https://api.netlify.com").rstrip("/") + "/api/v1"
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {acc['token']}", "User-Agent": "Kakarot-assistant"})

    def j(self, method, path, ok=(200, 201), what="", **kw):
        try:
            r = self.s.request(method, self.api + path, timeout=kw.pop("timeout", 120), **kw)
        except requests.RequestException as e:
            fail(f"Can't reach Netlify ({e.__class__.__name__}).")
        if r.status_code not in ok:
            hint = {401: " The Netlify token is wrong or expired.", 404: " Not found (wrong site?)."}.get(r.status_code, "")
            fail(f"Netlify: {what} failed (HTTP {r.status_code}): {r.text[:250]}.{hint}")
        return r.json() if r.content else {}

    def me(self):
        return self.j("GET", "/user", what="sign in")

    def create_site(self, name):
        try:
            return self.j("POST", "/sites", json={"name": name}, what="create site")
        except DeployError:
            return self.j("POST", "/sites", json={}, what="create site")

    def site(self, sid):
        return self.j("GET", f"/sites/{sid}", what="read site")

    def deploy(self, sid, base, rels, progress=None):
        digests, by_sha = {}, {}
        for r in rels:
            data = sp.read_bytes(os.path.join(base, *r.split("/")))
            h = sp.sha1(data)
            digests["/" + r] = h
            by_sha.setdefault(h, r)
        d = self.j("POST", f"/sites/{sid}/deploys", json={"files": digests}, what="start deploy")
        did, need = d["id"], d.get("required") or []
        for i, h in enumerate(need, 1):
            r = by_sha[h]
            if progress and (i == 1 or i % 10 == 0):
                progress(f"Uploading {i}/{len(need)}: {r}")
            self.j("PUT", f"/deploys/{did}/files/{quote(r)}", ok=(200, 201),
                   data=sp.read_bytes(os.path.join(base, *r.split("/"))),
                   headers={"Content-Type": "application/octet-stream"}, what=f"upload {r}", timeout=300)
        for _ in range(100):
            d = self.j("GET", f"/deploys/{did}", what="check deploy")
            if d.get("state") in ("ready", "error"):
                break
            time.sleep(3)
        if d.get("state") == "error":
            fail(f"Netlify could not publish: {d.get('error_message')}")
        return d, len(need)

    def restore(self, sid, did):
        return self.j("POST", f"/sites/{sid}/deploys/{did}/restore", what="restore earlier version")


# ================================================================= Vercel
class Vercel:
    def __init__(self):
        acc = vault.get("vercel")
        if not acc:
            fail("No Vercel token saved yet. Add one in Websites → Accounts (vercel.com → Account Settings → Tokens).")
        self.api = CFG.get("vercel_api", "https://api.vercel.com").rstrip("/")
        self.team = (acc.get("team") or "").strip()
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {acc['token']}", "User-Agent": "Kakarot-assistant"})

    def _p(self, path):
        if self.team:
            path += ("&" if "?" in path else "?") + "teamId=" + quote(self.team)
        return self.api + path

    def j(self, method, path, ok=(200, 201), what="", **kw):
        try:
            r = self.s.request(method, self._p(path), timeout=kw.pop("timeout", 120), **kw)
        except requests.RequestException as e:
            fail(f"Can't reach Vercel ({e.__class__.__name__}).")
        if r.status_code not in ok:
            hint = {401: " The Vercel token is wrong or expired.", 403: " The token has no access (team?)."}.get(r.status_code, "")
            fail(f"Vercel: {what} failed (HTTP {r.status_code}): {r.text[:250]}.{hint}")
        return r.json() if r.content else {}

    def me(self):
        return self.j("GET", "/v2/user", what="sign in")

    def deploy(self, name, base, rels, framework=None, progress=None):
        entries = []
        for i, r in enumerate(rels, 1):
            data = sp.read_bytes(os.path.join(base, *r.split("/")))
            h = sp.sha1(data)
            if progress and (i == 1 or i % 15 == 0):
                progress(f"Uploading {i}/{len(rels)}: {r}")
            self.j("POST", "/v2/files", data=data, what=f"upload {r}", timeout=300,
                   headers={"x-vercel-digest": h, "Content-Type": "application/octet-stream"})
            entries.append({"file": r, "sha": h, "size": len(data)})
        body = {"name": name, "files": entries, "target": "production",
                "projectSettings": {"framework": framework}}
        d = self.j("POST", "/v13/deployments?skipAutoDetectionConfirmation=1", json=body, what="start deploy")
        for _ in range(200):
            d = self.j("GET", f"/v13/deployments/{d['id']}", what="check deploy")
            if d.get("readyState") in ("READY", "ERROR", "CANCELED"):
                break
            time.sleep(3)
        if d.get("readyState") != "READY":
            fail(f"Vercel could not publish ({d.get('readyState')}): {(d.get('errorMessage') or '')[:300]}")
        alias = next((a for a in d.get("alias") or [] if a.endswith(".vercel.app")), None) or (d.get("alias") or [None])[0]
        return d, "https://" + (alias or d["url"])


# ================================================================= building (React / Vite / ...)
def build(pdir, spec, progress=None):
    """npm install + npm run build. Returns the output folder."""
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        fail("This site must be built with Node.js first, but Node.js isn't installed. Install the LTS version "
             "from nodejs.org, then ask me again.")
    env = dict(os.environ, CI="", BROWSER="none")
    stamp = os.path.join(pdir, "node_modules", ".kakarot-installed")
    pkg_time = max(os.path.getmtime(os.path.join(pdir, f)) for f in ("package.json", "package-lock.json")
                   if os.path.exists(os.path.join(pdir, f)))
    if not os.path.exists(stamp) or os.path.getmtime(stamp) < pkg_time:
        if progress:
            progress("Installing packages (npm install)")
        cmd = [npm, "ci"] if os.path.exists(os.path.join(pdir, "package-lock.json")) else [npm, "install"]
        r = subprocess.run(cmd, cwd=pdir, capture_output=True, text=True, timeout=1800, env=env,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0 and cmd[1] == "ci":
            r = subprocess.run([npm, "install"], cwd=pdir, capture_output=True, text=True, timeout=1800, env=env,
                               encoding="utf-8", errors="replace")
        if r.returncode != 0:
            fail("npm install failed:\n" + (r.stderr or r.stdout)[-2500:])
        with open(stamp, "w") as f:
            f.write(sp.now())
    if progress:
        progress("Building the site (npm run build)")
    r = subprocess.run([npm, "run", "build"], cwd=pdir, capture_output=True, text=True, timeout=1800, env=env,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        fail("The build failed:\n" + (r.stdout + "\n" + r.stderr)[-3000:])
    out = sp.web_root(pdir, spec)
    if not os.path.isdir(out):
        for cand in ("dist", "build", "out", "public"):
            if os.path.isfile(os.path.join(pdir, cand, "index.html")):
                spec["build_dir"] = cand
                return os.path.join(pdir, cand)
        fail(f"The build finished but the output folder {spec.get('build_dir')} wasn't made.")
    return out


# ================================================================= which files get published
def collect(pdir, spec, target, manifest_files=None, progress=None):
    """Returns (base folder, [relative files], [warnings])."""
    kind = spec.get("type", "static")
    warns = []
    manifest_files = manifest_files or {}
    if target == "github" or (target == "vercel" and kind in ("node-build", "flask", "node-server")):
        base = pdir
        rels = sp.files(pdir, use_gitignore=True)
        rels = [r for r in rels if not r.startswith(("instance/", "_screens/")) and not r.endswith(".zip")
                or r in manifest_files]
    elif kind == "node-build":
        base = build(pdir, spec, progress)
        rels = sp.files(base)
    elif kind in ("php", "flask", "node-server", "other") or not spec.get("root"):
        base = pdir
        rels = [r for r in sp.files(pdir, use_gitignore=kind != "php") if not r.startswith("_screens/")]
        if kind != "php":
            rels = [r for r in rels if not r.startswith("instance/") and r not in ("run-site.bat",)]
    else:
        base = sp.web_root(pdir, spec)
        rels = sp.files(base)
    public = target in ("github", "netlify", "vercel", "github-pages")
    if public:     # names AND contents (passwords, keys) - unless the file was already there
        secret = [r for r in rels if r not in manifest_files and sp.is_secret(r, base)]
    else:          # private hosting: only clearly private files that didn't come from that server
        secret = [r for r in rels if r not in manifest_files and sp.is_secret(r)]
    if secret:
        rels = [r for r in rels if r not in secret]
        warns.append("Not sent - they contain passwords/keys or look private: " + ", ".join(secret[:10]))
    big = [r for r in rels if os.path.getsize(os.path.join(base, *r.split("/"))) > 95_000_000]
    if big:
        rels = [r for r in rels if r not in big]
        warns.append("Skipped (bigger than 95 MB): " + ", ".join(big[:5]))
    return base, rels, warns


def root_relative_links(base, rels):
    """Pages that use links starting with '/' (they break on GitHub Pages project sites like user.github.io/repo/)."""
    hits = []
    for r in rels:
        if r.endswith((".html", ".htm")):
            t = sp.read_text(os.path.join(base, *r.split("/")), 300000)
            if re.search(r"""(?:href|src)=["']/(?!/)""", t):
                hits.append(r)
    return hits


# ================================================================= publishing
class Result(dict):
    pass


def _record(pdir, rec):
    rec.setdefault("id", "d" + sp.stamp())
    rec.setdefault("at", sp.now())
    d = sp.meta_dir(pdir, "deploys")
    with open(os.path.join(d, rec["id"] + ".json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    return rec


def deploys(pdir, limit=50):
    d = os.path.join(pdir, sp.META, "deploys")
    out = []
    if os.path.isdir(d):
        for fn in sorted((f for f in os.listdir(d) if f.endswith(".json")), reverse=True)[:limit]:
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                pass
    return out


def get_deploy(pdir, did):
    p = os.path.join(pdir, sp.META, "deploys", did + ".json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _gh_defaults(spec, opts, gh):
    o = spec.get("origin") or {}
    saved = (spec.get("publish") or {}).get("github") or {}
    pr = parse_repo(opts.get("repo") or "") or (o.get("owner") and (o["owner"], o["repo"], o.get("branch"))) or \
        (saved.get("repo") and parse_repo(saved["repo"]))
    if pr:
        owner, repo, br = pr
    else:
        owner, repo, br = gh.login(), re.sub(r"[^\w.-]+", "-", spec.get("name") or "website").strip("-"), None
    return owner, repo, opts.get("branch") or br or saved.get("branch")


def plan(pdir, spec, target, opts):
    """What would be published (for the approval card) - fast, no uploads."""
    opts = opts or {}
    out = {"target": target, "files": [], "warnings": [], "dest": ""}
    kind = spec.get("type", "static")
    if target in ("netlify", "github-pages") and kind in ("flask", "php", "node-server"):
        fail(f"A {kind} site needs a server that runs code; {target} only hosts static sites. Use FTP/cPanel "
             f"hosting for PHP, or GitHub to keep the code.")
    if target == "vercel" and kind in ("php", "flask"):
        fail(f"Vercel can't run this {kind} site as it is. Use FTP/cPanel hosting (PHP) or GitHub.")
    if target.startswith("ftp:") and kind in ("flask", "node-server"):
        fail("Normal cPanel/FTP hosting can't run this app by uploading files. On Namecheap use cPanel → 'Setup "
             "Python App' / 'Setup Node.js App' and upload the zip, or publish the code to GitHub.")
    mkey = manifest_key(spec, target, opts)
    man = (sp.load_manifest(pdir, mkey) or {}).get("files", {}) if mkey else {}
    if kind == "node-build" and target != "github" and not (target == "vercel"):
        base = sp.web_root(pdir, spec)
        rels = sp.files(base) if os.path.isdir(base) else []
        out["warnings"].append("The site will be built (npm run build) first.")
        changed = rels
    else:
        base, rels, warns = collect(pdir, spec, "github" if target == "github-pages" and not spec.get("root") else target, man)
        out["warnings"] += warns
        if target in ("github", "github-pages"):
            changed = [r for r in rels if (man.get(r) or {}).get("local") != sp.git_sha(sp.read_bytes(os.path.join(base, *r.split("/"))))]
        else:
            hashes = sp.local_hashes(base, rels)
            changed = [r for r in rels if (man.get(r) or {}).get("sha1") != hashes.get(r)]
    out["files"] = changed
    out["total"] = len(rels)
    if target == "github-pages" or (target == "github" and opts.get("pages")):
        hits = root_relative_links(base if os.path.isdir(base) else pdir, rels)
        if hits:
            out["warnings"].append("Some pages use links starting with '/', which break on GitHub Pages "
                                   f"(user.github.io/repo/): {', '.join(hits[:4])}. Ask me to fix the links first, "
                                   "or use a custom domain.")
    if target.startswith("ftp:"):
        acc = vault.get(target)
        if not acc:
            fail(f"No FTP account '{target}'. Add it in Websites → Accounts.")
        out["dest"] = f"{acc['host']} → {opts.get('folder') or acc.get('folder') or '/'}" + \
                      (f" ({acc['site_url']})" if acc.get("site_url") else "")
        if not man:
            out["warnings"].append("First publish to this hosting: files with the same name on the server will be "
                                   "replaced (Kakarot downloads a backup of each one first).")
    elif target in ("github", "github-pages"):
        gh = GitHub()
        owner, repo, br = _gh_defaults(spec, opts, gh)
        out["dest"] = f"github.com/{owner}/{repo}" + (f" (branch {br})" if br else "") + \
                      (" + GitHub Pages" if target == "github-pages" or opts.get("pages") else "")
        if not man and target == "github":
            info = gh.repo(owner, repo)
            if info:
                out["warnings"].append(f"{owner}/{repo} already exists - files with the same name will be replaced "
                                       f"by Kakarot's version (other files stay).")
    elif target == "netlify":
        sid = ((spec.get("publish") or {}).get("netlify") or {}).get("site_id")
        out["dest"] = "Netlify " + (f"site {((spec.get('publish') or {}).get('netlify') or {}).get('url', sid)}" if sid
                                    else "(a new site will be created)")
    elif target == "vercel":
        proj = ((spec.get("publish") or {}).get("vercel") or {}).get("project") or _vname(spec)
        out["dest"] = f"Vercel project {proj}"
    elif target == "folder":
        o = spec.get("origin") or {}
        if o.get("type") != "folder":
            fail("This site didn't come from a folder on this PC, so there's no folder to save it back to.")
        out["dest"] = o["path"]
    else:
        fail(f"Unknown place to publish: {target}. Use github, github-pages, netlify, vercel, folder or an FTP "
             f"account like ftp:mysite.")
    return out


def manifest_key(spec, target, opts=None):
    opts = opts or {}
    if target.startswith("ftp:"):
        acc = vault.get(target) or {}
        return f"{target}-{opts.get('folder') or acc.get('folder') or ''}"
    if target in ("github", "github-pages"):
        o = spec.get("origin") or {}
        saved = (spec.get("publish") or {}).get("github") or {}
        pr = parse_repo(opts.get("repo") or "") or (o.get("owner") and (o["owner"], o["repo"], o.get("branch"))) or \
            (saved.get("repo") and parse_repo(saved["repo"]))
        if not pr:
            return None
        br = opts.get("branch") or pr[2] or saved.get("branch") or "main"
        return f"github-{pr[0]}-{pr[1]}-{'gh-pages' if target == 'github-pages' and _pages_needs_branch(spec) else br}"
    if target == "folder":
        return "folder"
    return None


def _pages_needs_branch(spec):
    return spec.get("type") == "node-build" or (spec.get("root") or "") not in ("", "docs")


def _vname(spec):
    return re.sub(r"[^a-z0-9-]+", "-", (spec.get("name") or "website").lower()).strip("-")[:90] or "website"


def publish(pdir, spec, target, opts=None, progress=None):
    opts = opts or {}
    p = progress or (lambda m: None)
    if target in ("github", "github-pages"):
        for attempt in range(2):
            try:
                return _publish_github(pdir, spec, target, opts, p)
            except _Moved:
                p("GitHub changed while publishing - trying again")
        fail("Someone keeps changing the GitHub repository at the same time - try again in a minute.")
    if target.startswith("ftp:"):
        return _publish_ftp(pdir, spec, target, opts, p)
    if target == "netlify":
        return _publish_netlify(pdir, spec, opts, p)
    if target == "vercel":
        return _publish_vercel(pdir, spec, opts, p)
    if target == "folder":
        return _publish_folder(pdir, spec, opts, p)
    fail(f"Unknown target {target}")


def _save_target(pdir, spec, key, data):
    spec.setdefault("publish", {})[key] = {**(spec.get("publish", {}).get(key) or {}), **data, "last": sp.now()}
    sp.save(pdir, spec)


# ---------------------------------------------------------------- GitHub
def _publish_github(pdir, spec, target, opts, p):
    gh = GitHub()
    gh.login()
    owner, repo, branch = _gh_defaults(spec, opts, gh)
    info = gh.repo(owner, repo)
    created = False
    if not info:
        p(f"Creating the repository {owner}/{repo}")
        private = opts.get("visibility", "public" if (target == "github-pages" or opts.get("pages")) else "private") == "private"
        info = gh.create_repo(owner, repo, private, spec.get("summary") or spec.get("title") or "")
        created = True
    branch = branch or info.get("default_branch") or "main"
    pages = target == "github-pages" or opts.get("pages")
    rec = {"target": target, "dest": f"{owner}/{repo}@{branch}", "repo": f"{owner}/{repo}"}
    if pages and _pages_needs_branch(spec):
        # build output -> its own gh-pages branch; the source branch is published too (normal push)
        src = _gh_push(gh, pdir, spec, owner, repo, branch, opts, p, source=True)
        base, rels, warns = collect(pdir, spec, "netlify", progress=p)
        out = _gh_push(gh, pdir, spec, owner, repo, "gh-pages", opts, p, base=base, rels=rels, replace=True)
        rec.update(src, pages_branch=out)
        rec["warnings"] = (src.get("warnings") or []) + warns
        pages_src = ("gh-pages", "/")
    else:
        rec.update(_gh_push(gh, pdir, spec, owner, repo, branch, opts, p, source=True))
        pages_src = (branch, "/docs" if spec.get("root") == "docs" else "/")
    rec["url"] = info.get("html_url") or f"https://github.com/{owner}/{repo}"
    rec["created_repo"] = created
    if pages:
        p("Turning on GitHub Pages")
        pg = gh.enable_pages(owner, repo, *pages_src)
        site = pg.get("html_url") or f"https://{owner.lower()}.github.io/{repo}/"
        rec["live_url"] = site
        p("Waiting for GitHub Pages to publish")
        status, err = None, ""
        for _ in range(60):
            status, err = gh.pages_status(owner, repo)
            if status in ("built", "errored"):
                break
            time.sleep(5)
        rec["pages_status"] = status or "unknown"
        if status == "errored":
            rec.setdefault("warnings", []).append(f"GitHub Pages reported an error: {err}")
    _save_target(pdir, spec, "github", {"repo": f"{owner}/{repo}", "branch": branch, "pages": bool(pages),
                                         "live_url": rec.get("live_url") or ((spec.get("publish") or {}).get("github") or {}).get("live_url")})
    if pages and rec.get("live_url"):
        spec["live_url"] = spec.get("live_url") or rec["live_url"]
        sp.save(pdir, spec)
    return _record(pdir, rec)


def _gh_push(gh, pdir, spec, owner, repo, branch, opts, p, source=True, base=None, rels=None, replace=False):
    """Push changed files to one branch. replace=True makes the branch contain exactly these files (gh-pages)."""
    mkey = f"github-{owner}-{repo}-{branch}"
    man = sp.load_manifest(pdir, mkey) or {"files": {}}
    mf = man["files"]
    if base is None:
        base, rels, warns = collect(pdir, spec, "github", mf, p)
    else:
        warns = []
    if replace and ".nojekyll" not in rels:
        nj = os.path.join(base, ".nojekyll")
        if not os.path.exists(nj):
            open(nj, "w").close()
        rels = rels + [".nojekyll"]
    p(f"Comparing with GitHub ({owner}/{repo}, {branch})")
    head, tsha, remote = gh.files_at(owner, repo, branch)
    new_branch = False
    if head is None:
        info = gh.repo(owner, repo)
        if branch == (info or {}).get("default_branch"):
            gh.seed_empty(owner, repo, branch)
            head, tsha, remote = gh.files_at(owner, repo, branch)
        else:
            new_branch = True
    uploads, skipped_remote_newer, conflicts = [], [], []
    local_sha = {}
    for r in rels:
        data = sp.read_bytes(os.path.join(base, *r.split("/")))
        s = sp.git_sha(data)
        local_sha[r] = s
        rsha = (remote.get(r) or (None, None))[0]
        if rsha == s:
            continue
        m = mf.get(r)
        if m and rsha and m.get("base") and m["base"] != rsha:
            if m.get("local") == s:
                skipped_remote_newer.append(r)      # only GitHub changed it: keep GitHub's version
                continue
            conflicts.append(r)                      # both changed it
            continue
        uploads.append(r)
    deletes = []
    if replace:
        deletes = [r for r in remote if r not in local_sha]
    else:
        for r, m in mf.items():
            if r not in local_sha and not os.path.exists(os.path.join(base, *r.split("/"))) and r in remote \
                    and remote[r][0] == m.get("base"):
                deletes.append(r)
    if conflicts and not opts.get("force"):
        fail("These files were changed on GitHub since Kakarot downloaded them, AND changed here: "
             + ", ".join(conflicts[:10]) + ". Ask me to 'get the latest version from GitHub' first (I'll keep both "
             "copies so nothing is lost), then publish again.")
    uploads += conflicts if opts.get("force") else []
    out = {"uploaded": uploads, "deleted": deletes, "kept_github_version": skipped_remote_newer, "warnings": warns,
           "new_shas": {r: local_sha[r] for r in uploads},
           "prev": {r: (remote.get(r) or (None,))[0] for r in uploads + deletes}, "parent": head, "branch": branch}
    if not uploads and not deletes:
        out["commit"] = head
        out["note"] = "GitHub already had this exact version - nothing to send."
    else:
        p(f"Uploading {len(uploads)} file(s) to GitHub")

        def up(r):
            sha = gh.blob(owner, repo, sp.read_bytes(os.path.join(base, *r.split("/"))))
            if sha != local_sha[r]:
                fail(f"GitHub stored {r} differently than expected - stopped to be safe.")
            return r

        with cf.ThreadPoolExecutor(4) as ex:
            for i, _ in enumerate(ex.map(up, uploads), 1):
                if i % 20 == 0:
                    p(f"Uploaded {i}/{len(uploads)}")
        entries = [{"path": r, "mode": (remote.get(r) or (None, "100644"))[1] or "100644", "type": "blob",
                    "sha": local_sha[r]} for r in uploads]
        entries += [{"path": r, "mode": remote[r][1], "type": "blob", "sha": None} for r in deletes]
        msg = opts.get("message") or (f"Update by Kakarot: {len(uploads)} file(s) changed" +
                                      (f", {len(deletes)} removed" if deletes else ""))
        out["commit"] = gh.write_commit(owner, repo, branch, entries, msg, head,
                                        base_tree=None if (replace or not tsha) else tsha, new_branch=new_branch)
    newremote = {r: v[0] for r, v in remote.items()}
    for r in uploads:
        newremote[r] = local_sha[r]
    for r in deletes:
        newremote.pop(r, None)
    files = {r: {"base": newremote[r], "local": local_sha[r]} for r in local_sha if r in newremote}
    for r in skipped_remote_newer + (conflicts if not opts.get("force") else []):
        files[r] = mf.get(r, {})
    sp.save_manifest(pdir, mkey, {"commit": out["commit"], "branch": branch, "files": files, "at": sp.now()})
    return out


def github_refresh(pdir, spec, progress=None):
    """Bring GitHub's newer changes into the local copy (files changed on both sides are kept side by side)."""
    o = spec.get("origin") or {}
    saved = (spec.get("publish") or {}).get("github") or {}
    pr = (o.get("type") == "github" and (o["owner"], o["repo"], o.get("branch"))) or \
        (saved.get("repo") and parse_repo(saved["repo"]) + ())
    if not pr:
        fail("This site isn't connected to a GitHub repository.")
    owner, repo, branch = pr[0], pr[1], pr[2] or saved.get("branch") or "main"
    gh = GitHub(anonymous=not vault.has("github"))
    mkey = f"github-{owner}-{repo}-{branch}"
    man = sp.load_manifest(pdir, mkey) or {"files": {}}
    head, _, remote = gh.files_at(owner, repo, branch)
    if head and head == man.get("commit"):
        return {"changed": [], "conflicts": [], "note": "Already up to date with GitHub."}
    ch = sp.Change(pdir, f"Get latest from GitHub {owner}/{repo}", kind="refresh")
    mf, changed, conflicts = man["files"], [], []
    cdir = sp.meta_dir(pdir, "conflicts", sp.stamp())
    for r, (rsha, mode) in remote.items():
        m = mf.get(r) or {}
        lp = os.path.join(pdir, *r.split("/"))
        lsha = sp.git_sha(sp.read_bytes(lp)) if os.path.exists(lp) else None
        if lsha == rsha:
            mf[r] = {"base": rsha, "local": lsha}
            continue
        if m.get("base") == rsha:
            continue                                        # only changed here - keep ours
        if lsha is None or lsha == m.get("local"):          # unchanged here (or new on GitHub) -> take GitHub's
            if progress:
                progress(f"Downloading {r}")
            ch.write_bytes(r, gh.read_blob(owner, repo, rsha))
            mf[r] = {"base": rsha, "local": rsha}
            changed.append(r)
        else:
            data = gh.read_blob(owner, repo, rsha)
            dst = os.path.join(cdir, *r.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(data)
            conflicts.append(r)
            mf[r] = {"base": rsha, "local": m.get("local")}   # seen: the next publish sends YOUR version
    for r in list(mf):
        if r not in remote:
            lp = os.path.join(pdir, *r.split("/"))
            if os.path.exists(lp) and sp.git_sha(sp.read_bytes(lp)) == mf[r].get("local"):
                ch.delete(r)
                changed.append(r + " (removed on GitHub)")
            mf.pop(r)
    meta = ch.commit()
    sp.save_manifest(pdir, mkey, {"commit": head, "branch": branch, "files": mf, "at": sp.now()})
    return {"changed": changed, "conflicts": conflicts, "conflict_dir": cdir if conflicts else "",
            "change_id": meta["id"] if meta else None}


# ---------------------------------------------------------------- FTP
def _publish_ftp(pdir, spec, target, opts, p):
    acc = vault.get(target)
    if not acc:
        fail(f"No FTP account '{target}'.")
    folder = opts.get("folder") or acc.get("folder") or ""
    mkey = f"{target}-{folder}"
    man = sp.load_manifest(pdir, mkey) or {"files": {}}
    mf = man["files"]
    base, rels, warns = collect(pdir, spec, target, mf, p)
    hashes = sp.local_hashes(base, rels)
    changed = [r for r in rels if (mf.get(r) or {}).get("sha1") != hashes[r]]
    removed = [r for r in mf if r not in hashes and not os.path.exists(os.path.join(base, *r.split("/")))] \
        if opts.get("remove_deleted") else []
    rec = {"target": target, "dest": f"{acc['host']}:{folder}", "warnings": warns, "uploaded": [], "created": [],
           "replaced": [], "deleted": [], "live_url": opts.get("live_url") or acc.get("site_url") or spec.get("live_url")}
    if not changed and not removed:
        rec["note"] = "The hosting already has this exact version - nothing to send."
        return _record(pdir, rec)
    rid = "d" + sp.stamp()
    rec["id"] = rid
    bdir = sp.meta_dir(pdir, "deploys", rid + "-backup")
    rec["backup_dir"] = bdir
    p(f"Connecting to {acc['host']}")
    with FTP(target) as f:
        root = f.base(folder)
        conflicts = []
        for i, r in enumerate(changed, 1):
            rp = posixpath.join(root, r)
            st = f.stat(rp)
            if st is None:
                rec["created"].append(r)
                continue
            p(f"Backing up the server's {r} ({i}/{len(changed)})")
            local_b = os.path.join(bdir, *r.split("/"))
            f.download(rp, local_b)
            rsha = sp.sha1(sp.read_bytes(local_b))
            if rsha == hashes[r]:
                continue                                # the server already has it
            m = mf.get(r)
            if m and m.get("sha1") and rsha != m["sha1"]:
                conflicts.append(r)
            rec["replaced"].append(r)
        if conflicts and not opts.get("force"):
            fail("These files were changed ON THE SERVER since Kakarot last downloaded/published them: "
                 + ", ".join(conflicts[:10]) + ". To keep those server changes, ask me to 'get the latest version "
                 "from the hosting' first; to overwrite them anyway, publish again with force.")
        todo = rec["created"] + rec["replaced"]
        for i, r in enumerate(todo, 1):
            p(f"Uploading {r} ({i}/{len(todo)})")
            f.upload(os.path.join(base, *r.split("/")), posixpath.join(root, r))
            st = f.stat(posixpath.join(root, r)) or (None, "")
            mf[r] = {"sha1": hashes[r], "size": st[0], "modify": st[1]}
            rec["uploaded"].append(r)
            rec.setdefault("hashes", {})[r] = hashes[r]
        for r in changed:
            if r not in todo:
                mf[r] = {**(mf.get(r) or {}), "sha1": hashes[r]}
        for r in removed:
            rp = posixpath.join(root, r)
            if f.stat(rp):
                f.download(rp, os.path.join(bdir, *r.split("/")))
                if f.delete(rp):
                    rec["deleted"].append(r)
            mf.pop(r, None)
    sp.save_manifest(pdir, mkey, {"files": mf, "at": sp.now()})
    _save_target(pdir, spec, target, {"folder": folder, "live_url": rec["live_url"]})
    return _record(pdir, rec)


def ftp_refresh(pdir, spec, key, folder=None, progress=None):
    """Download files changed on the server since Kakarot last synced (both-sides changes kept side by side)."""
    acc = vault.get(key) or {}
    folder = folder if folder is not None else ((spec.get("origin") or {}).get("folder") or acc.get("folder") or "")
    mkey = f"{key}-{folder}"
    man = sp.load_manifest(pdir, mkey) or {"files": {}}
    mf = man["files"]
    ch = sp.Change(pdir, f"Get latest from the hosting ({acc.get('host')})", kind="refresh")
    changed, conflicts = [], []
    cdir = sp.meta_dir(pdir, "conflicts", sp.stamp())
    with FTP(key) as f:
        root = f.base(folder)
        for r, size, mod in f.walk(root, skip=_ftp_skip, limit=int(CFG.get("site_import_max_files", 8000))):
            m = mf.get(r) or {}
            if m and m.get("size") == size and m.get("modify") and m["modify"][:14] == str(mod)[:14]:
                continue
            lp = os.path.join(pdir, *r.split("/"))
            local_sha = sp.sha1(sp.read_bytes(lp)) if os.path.exists(lp) else None
            data = f.read(posixpath.join(root, r))
            if data is None:
                continue
            rsha = sp.sha1(data)
            if rsha == local_sha:
                mf[r] = {"sha1": rsha, "size": size, "modify": mod}
                continue
            if local_sha is None or local_sha == m.get("sha1"):
                if progress:
                    progress(f"Downloading {r}")
                ch.write_bytes(r, data)
                mf[r] = {"sha1": rsha, "size": size, "modify": mod}
                changed.append(r)
            else:
                dst = os.path.join(cdir, *r.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as fh:
                    fh.write(data)
                conflicts.append(r)
                mf[r] = {"sha1": rsha, "size": size, "modify": mod}   # seen: the next publish sends YOUR version
    meta = ch.commit()
    sp.save_manifest(pdir, mkey, {"files": mf, "at": sp.now()})
    return {"changed": changed, "conflicts": conflicts, "conflict_dir": cdir if conflicts else "",
            "change_id": meta["id"] if meta else None}


def _ftp_skip(rel, is_dir):
    name = rel.split("/")[-1]
    if is_dir:
        return name in (".git", "node_modules", ".cache", "cache", "logs", ".trash", ".well-known", "cgi-bin",
                        ".cpanel", ".kakarot") or rel in ("wp-content/cache", "wp-content/upgrade")
    return name in ("error_log", ".DS_Store") or name.endswith((".log", ".kakarot-tmp"))


# ---------------------------------------------------------------- Netlify
def _publish_netlify(pdir, spec, opts, p):
    n = Netlify()
    saved = (spec.get("publish") or {}).get("netlify") or {}
    sid = opts.get("site") or saved.get("site_id")
    prev = None
    if sid:
        site = n.site(sid)
        prev = (site.get("published_deploy") or {}).get("id")
    else:
        p("Creating a Netlify site")
        site = n.create_site(_vname(spec))
        sid = site["id"]
    base, rels, warns = collect(pdir, spec, "netlify", progress=p)
    p(f"Sending {len(rels)} file(s) to Netlify (only new/changed ones are uploaded)")
    d, sent = n.deploy(sid, base, rels, p)
    url = site.get("ssl_url") or site.get("url") or d.get("ssl_url")
    rec = {"target": "netlify", "dest": f"Netlify {site.get('name')}", "site_id": sid, "deploy_id": d["id"],
           "prev_deploy_id": prev, "uploaded": [], "summary": f"Sent {sent} new/changed file(s) of {len(rels)}.", "live_url": url,
           "warnings": warns}
    _save_target(pdir, spec, "netlify", {"site_id": sid, "url": url, "name": site.get("name")})
    spec["live_url"] = spec.get("live_url") or url
    sp.save(pdir, spec)
    return _record(pdir, rec)


# ---------------------------------------------------------------- Vercel
def _publish_vercel(pdir, spec, opts, p):
    v = Vercel()
    saved = (spec.get("publish") or {}).get("vercel") or {}
    name = opts.get("project") or saved.get("project") or _vname(spec)
    base, rels, warns = collect(pdir, spec, "vercel", progress=p)
    fw = {"vite": "vite", "react-scripts": "create-react-app", "next": "nextjs", "astro": "astro", "gatsby": "gatsby",
          "nuxt": "nuxtjs", "@sveltejs/kit": "sveltekit", "@vue/cli-service": "vue", "@angular/core": "angular",
          "parcel": "parcel"}.get(spec.get("framework") or "") if spec.get("type") == "node-build" else None
    p(f"Sending {len(rels)} file(s) to Vercel")
    d, url = v.deploy(name, base, rels, fw, p)
    rec = {"target": "vercel", "dest": f"Vercel {name}", "deploy_id": d["id"], "prev_deploy_id": saved.get("deploy_id"),
           "uploaded": [], "summary": f"Sent {len(rels)} file(s).", "live_url": url, "warnings": warns}
    _save_target(pdir, spec, "vercel", {"project": name, "url": url, "deploy_id": d["id"]})
    spec["live_url"] = spec.get("live_url") or url
    sp.save(pdir, spec)
    return _record(pdir, rec)


# ---------------------------------------------------------------- back to the original folder
def _publish_folder(pdir, spec, opts, p):
    o = spec.get("origin") or {}
    dest = o.get("path")
    if o.get("type") != "folder" or not dest or not os.path.isdir(dest):
        fail("The original folder isn't there any more.")
    man = sp.load_manifest(pdir, "folder") or {"files": {}}
    mf = man["files"]
    rels = sp.files(pdir)
    hashes = sp.local_hashes(pdir, rels)
    changed = [r for r in rels if (mf.get(r) or {}).get("sha1") != hashes[r]]
    rec = {"target": "folder", "dest": dest, "uploaded": [], "created": [], "replaced": [], "conflicts": []}
    if not changed:
        rec["note"] = "The folder already has this version."
        return _record(pdir, rec)
    rid = "d" + sp.stamp()
    rec["id"] = rid
    bdir = sp.meta_dir(pdir, "deploys", rid + "-backup")
    rec["backup_dir"] = bdir
    for r in changed:
        dp = os.path.join(dest, *r.split("/"))
        if os.path.exists(dp):
            cur = sp.sha1(sp.read_bytes(dp))
            if cur == hashes[r]:
                mf[r] = {"sha1": hashes[r]}
                continue
            if mf.get(r) and cur != mf[r].get("sha1") and not opts.get("force"):
                rec["conflicts"].append(r)
                continue
            b = os.path.join(bdir, *r.split("/"))
            os.makedirs(os.path.dirname(b), exist_ok=True)
            shutil.copy2(dp, b)
            rec["replaced"].append(r)
        else:
            rec["created"].append(r)
        os.makedirs(os.path.dirname(dp), exist_ok=True)
        shutil.copy2(os.path.join(pdir, *r.split("/")), dp)
        mf[r] = {"sha1": hashes[r]}
        rec["uploaded"].append(r)
    if rec["conflicts"]:
        rec.setdefault("warnings", []).append("Not saved because they were changed in the folder meanwhile: "
                                              + ", ".join(rec["conflicts"][:10]))
    sp.save_manifest(pdir, "folder", {"files": mf, "at": sp.now()})
    return _record(pdir, rec)


# ================================================================= undo a publish
def undo(pdir, spec, did, progress=None, force=False):
    rec = get_deploy(pdir, did)
    opts_force = force
    p = progress or (lambda m: None)
    t = rec["target"]
    out = {"target": t, "undo_of": did, "dest": rec.get("dest")}
    if t.startswith("ftp:") or t == "folder":
        bdir = rec.get("backup_dir")
        if t == "folder":
            dest = (spec.get("origin") or {}).get("path")
            for r in rec.get("replaced", []):
                shutil.copy2(os.path.join(bdir, *r.split("/")), os.path.join(dest, *r.split("/")))
            for r in rec.get("created", []):
                fp = os.path.join(dest, *r.split("/"))
                if os.path.exists(fp):
                    shutil.move(fp, os.path.join(sp.meta_dir(pdir, "trash"), r.replace("/", "__")))
        else:
            acc = vault.get(t) or {}
            folder = rec["dest"].split(":", 1)[1] if ":" in rec.get("dest", "") else acc.get("folder", "")
            mkey = f"{t}-{folder}"
            man = sp.load_manifest(pdir, mkey) or {"files": {}}
            skipped, restored, removed = [], [], []
            with FTP(t) as f:
                root = f.base(folder)

                def changed_since(r):
                    """Was the file edited on the server after Kakarot published it? Then don't touch it."""
                    want = (rec.get("hashes") or {}).get(r)
                    if not want or opts_force:
                        return False
                    cur = f.read(posixpath.join(root, r))
                    return cur is not None and sp.sha1(cur) != want

                for r in rec.get("replaced", []) + rec.get("deleted", []):
                    if r in rec.get("replaced", []) and changed_since(r):
                        skipped.append(r)
                        continue
                    p(f"Restoring {r}")
                    lb = os.path.join(bdir, *r.split("/"))
                    f.upload(lb, posixpath.join(root, r))
                    st = f.stat(posixpath.join(root, r)) or (None, "")
                    man["files"][r] = {"sha1": sp.sha1(sp.read_bytes(lb)), "size": st[0], "modify": st[1]}
                    restored.append(r)
                for r in rec.get("created", []):
                    if changed_since(r):
                        skipped.append(r)
                        continue
                    p(f"Removing {r}")
                    f.delete(posixpath.join(root, r))
                    man["files"].pop(r, None)
                    removed.append(r)
            sp.save_manifest(pdir, mkey, {**man, "at": sp.now()})
            out.update(restored=restored, removed=removed, skipped=skipped)
            if skipped:
                out["warnings"] = ["Not undone because they were changed on the server after publishing: " +
                                   ", ".join(skipped[:10])]
        if t == "folder":
            out["restored"] = rec.get("replaced", [])
            out["removed"] = rec.get("created", [])
    elif t in ("github", "github-pages"):
        gh = GitHub()
        owner, repo = rec["repo"].split("/")
        branch = rec.get("branch") or rec["dest"].split("@")[-1]
        head, tsha, remote = gh.files_at(owner, repo, branch)
        entries, skipped = [], []
        for r, prev in (rec.get("prev") or {}).items():
            cur = (remote.get(r) or (None,))[0]
            mine = (rec.get("new_shas") or {}).get(r)
            if mine and cur != mine and not force:
                skipped.append(r)            # changed on GitHub after this publish - leave it
                continue
            if prev:
                entries.append({"path": r, "mode": (remote.get(r) or (None, "100644"))[1] or "100644", "type": "blob",
                                "sha": prev})
            elif r in remote:
                entries.append({"path": r, "mode": remote[r][1], "type": "blob", "sha": None})
        if not entries:
            fail("Nothing to undo on GitHub" + (f" (changed there since: {', '.join(skipped[:8])})." if skipped else "."))
        c = gh.write_commit(owner, repo, branch, entries, f"Undo Kakarot publish {did}", head, base_tree=tsha)
        out.update(commit=c, restored=list((rec.get("prev") or {}).keys()), skipped=skipped)
        mkey = f"github-{owner}-{repo}-{branch}"
        man = sp.load_manifest(pdir, mkey) or {"files": {}}
        for e in entries:
            if e["sha"]:
                man["files"][e["path"]] = {"base": e["sha"], "local": (man["files"].get(e["path"]) or {}).get("local")}
            else:
                man["files"].pop(e["path"], None)
        man["commit"] = c
        sp.save_manifest(pdir, mkey, man)
    elif t == "netlify":
        if not rec.get("prev_deploy_id"):
            fail("There is no earlier Netlify version to go back to.")
        Netlify().restore(rec["site_id"], rec["prev_deploy_id"])
        out["restored"] = [f"Netlify version {rec['prev_deploy_id']}"]
    else:
        fail("Vercel publishes can't be undone from here. Undo the change in Kakarot and publish again "
             "(or use 'Instant Rollback' on vercel.com).")
    out["live_url"] = rec.get("live_url")
    return _record(pdir, out)


# ================================================================= after publishing: is it really live?
def verify(url, base, changed, kind, progress=None):
    """Open the live site: are the new files there, and do the pages work?"""
    if not url:
        return {"checked": False, "note": "No website address known, so the live site wasn't checked."}
    p = progress or (lambda m: None)
    url = url if url.endswith("/") else url + "/"
    out = {"checked": True, "url": url, "fresh": [], "stale": [], "errors": []}
    static = [r for r in changed if r.endswith((".html", ".htm", ".css", ".js", ".json", ".txt", ".svg", ".xml"))
              and os.path.exists(os.path.join(base, *r.split("/")))][:6]
    if kind == "php":
        pages = ["", *[r for r in changed if r.endswith(".php") and "/" not in r][:4]]
        for r in pages:
            try:
                x = requests.get(urljoin(url, quote(r)), headers=UA, timeout=30)
                if x.status_code >= 500 or re.search(r"(Parse|Fatal) error|Uncaught \w+Error", x.text[:20000]):
                    out["errors"].append(f"/{r} shows a PHP error (HTTP {x.status_code})")
            except requests.RequestException as e:
                out["errors"].append(f"/{r} can't be opened ({e.__class__.__name__})")
    for r in static:
        want = sp.sha1(sp.read_bytes(os.path.join(base, *r.split("/"))))
        target = urljoin(url, quote(r[:-10] if r.endswith("index.html") else r))
        ok = False
        for attempt in range(4):
            try:
                x = requests.get(target, params={"kakarot": int(time.time())}, headers=UA, timeout=30)
                if x.status_code == 200 and sp.sha1(x.content) == want:
                    ok = True
                    break
            except requests.RequestException:
                pass
            time.sleep(8 if attempt < 3 else 0)
        (out["fresh"] if ok else out["stale"]).append(r)
    try:
        p("Opening the live site in the browser")
        import siteaudit
        rep = siteaudit.quick(url, 5)
        out["score"] = rep["score"]
        out["errors"] += [i["title"] for i in rep["issues"] if i["sev"] == "high" and i["area"] == "Errors"][:8]
    except Exception as e:
        out["errors"].append(f"Live check didn't run: {e}")
    return out
