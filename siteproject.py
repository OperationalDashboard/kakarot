"""Shared pieces for every website Kakarot works on (built by Kakarot or opened from somewhere else):
project info (kakarot-site.json), which files belong to the site, what kind of site it is, and a full
change history with diffs so EVERY change can be undone."""
import datetime
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import time

SPEC_FILE = "kakarot-site.json"
META = ".kakarot"
SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".git", ".pytest_cache"} | {META, "_screens", ".idea", ".vscode", ".next", ".cache",
                                                      ".parcel-cache", ".svelte-kit", ".nuxt", ".output"}
BUILD_DIRS = {"dist", "build", "out"}   # skipped only in JavaScript projects (they're made by 'npm run build')
SKIP_FILES = {SPEC_FILE, "server.log", ".DS_Store", "Thumbs.db", "desktop.ini"}
TEXT_EXT = {".html", ".htm", ".css", ".scss", ".sass", ".less", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue",
            ".svelte", ".astro", ".py", ".php", ".phtml", ".twig", ".inc", ".txt", ".md", ".json", ".svg", ".xml",
            ".sql", ".bat", ".sh", ".toml", ".cfg", ".ini", ".yml", ".yaml", ".webmanifest", ".htaccess", ".env",
            ".tpl", ".jinja", ".j2", ".csv", ".map", ".gitignore", ".rb", ".go"}
# never sent to public places (GitHub/Netlify/Vercel) unless they were already there
SECRET_PATTERNS = [".env", ".env.*", "*.pem", "*.key", "id_rsa*", "*.p12", "*.pfx", "*.sqlite", "*.sqlite3", "*.db",
                   "wp-config.php", "config.inc.php", "*.kdbx"]


def project_dir(name):
    import coder
    return coder.code_path(name)


def load(name):
    try:
        with open(os.path.join(project_dir(name), SPEC_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(pdir, spec):
    p = os.path.join(pdir, SPEC_FILE)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def meta_dir(pdir, *parts):
    p = os.path.join(pdir, META, *parts)
    os.makedirs(p, exist_ok=True)
    return p


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"


def slug(name):
    return (re.sub(r"[^a-z0-9_-]+", "_", (name or "").lower()).strip("_") or "website")[:40]


def free_name(base):
    base = slug(base)
    name, i = base, 2
    while os.path.exists(project_dir(name)) and os.listdir(project_dir(name)):
        name = f"{base}_{i}"
        i += 1
    return name


def rel(p, base):
    return os.path.relpath(p, base).replace(os.sep, "/")


def abspath(pdir, relpath):
    """Path of a site file; refuses anything that would leave the project folder."""
    p = os.path.realpath(os.path.join(pdir, *str(relpath).replace("\\", "/").lstrip("/").split("/")))
    base = os.path.realpath(pdir)
    if os.path.normcase(p) != os.path.normcase(base) and not os.path.normcase(p).startswith(os.path.normcase(base) + os.sep):
        raise PermissionError(f"{relpath} is outside the website folder")
    return p


def read_text(p, limit=None):
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read() if limit is None else f.read(limit)


def read_bytes(p):
    with open(p, "rb") as f:
        return f.read()


def is_text(path):
    ext = os.path.splitext(path)[1].lower() or os.path.basename(path).lower()
    if ext in TEXT_EXT or os.path.basename(path).lower() in (".htaccess", ".gitignore", "dockerfile", "procfile"):
        return True
    try:
        with open(path, "rb") as f:
            chunk = f.read(4096)
        return b"\0" not in chunk and bool(chunk)
    except OSError:
        return False


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def git_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# ---------------------------------------------------------------- which files belong to the site
def gitignore(pdir):
    pats = []
    p = os.path.join(pdir, ".gitignore")
    if os.path.exists(p):
        for line in read_text(p).splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "!")):
                pats.append(line)
    return pats


def ignored(relpath, pats, isdir=False):
    parts = relpath.split("/")
    for pat in pats:
        anchored = pat.startswith("/")
        pat_ = pat.strip("/")
        is_dir = pat.endswith("/")
        cands = ["/".join(parts[:i]) for i in range(1, len(parts) + (1 if isdir or not is_dir else 0))]
        for c in cands:
            name = c.split("/")[-1]
            if fnmatch.fnmatch(c, pat_) or (not anchored and "/" not in pat_ and fnmatch.fnmatch(name, pat_)):
                return True
    return False


SECRET_TEXT = re.compile(
    r"""(?ix)
    -----BEGIN\ (?:RSA\ |EC\ |OPENSSH\ |DSA\ )?PRIVATE\ KEY-----
    | \b(?:AKIA|ASIA)[0-9A-Z]{16}\b                                   # AWS key
    | \b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}  # GitHub token
    | \bsk_live_[A-Za-z0-9]{16,}|\bxox[bpoa]-[A-Za-z0-9-]{20,}          # Stripe / Slack
    | AIza[0-9A-Za-z_-]{35}                                             # Google API key
    | define\(\s*['"]\w*(?:PASS|PASSWORD|SECRET|KEY|TOKEN)\w*['"]\s*,\s*['"][^'"\s]{4,}['"]
    | (?:mysqli_connect|mysql_connect|new\s+mysqli|new\s+PDO)\s*\([^)]*?,\s*['"][^'"\s]+['"]\s*,\s*['"][^'"\s]{3,}['"]
    | [$@]?\b\w*(?:pass(?:word|wd)?|pwd|secret|api_?key|auth_?token|private_?key)\w*['"]?\s*(?:=>|=|:)\s*['"](?![$]|required|nullable|string\b)[^'"\s<>{}|]{6,}['"]
    """)


def is_secret(relpath, base=None):
    """True for files that must never go to public places: .env/keys/databases, or text containing a password,
    API key or private key (e.g. a PHP config.php with the database password)."""
    name = relpath.split("/")[-1].lower()
    if any(fnmatch.fnmatch(name, p) for p in SECRET_PATTERNS):
        return True
    if base is None:
        return False
    fp = os.path.join(base, *relpath.split("/"))
    try:
        if os.path.getsize(fp) > 400_000 or not is_text(fp) or name.endswith((".min.js", ".map", ".svg")):
            return False
        return bool(SECRET_TEXT.search(read_text(fp)))
    except OSError:
        return False


def files(pdir, sub="", use_gitignore=False, limit=20000):
    """All site files (relative, with /), skipping tools' folders, Kakarot's own data and temp files."""
    base = os.path.join(pdir, *sub.split("/")) if sub else pdir
    pats = gitignore(pdir) if use_gitignore else []
    node = os.path.exists(os.path.join(base, "package.json"))
    out = []
    for dp, dirs, fs in os.walk(base):
        top = dp == base
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (node and top and d in BUILD_DIRS)
                         and not (pats and ignored(rel(os.path.join(dp, d), pdir), pats, True)))
        for f in sorted(fs):
            if f in SKIP_FILES or f.endswith((".kakarot-tmp", ".pyc")):
                continue
            r = rel(os.path.join(dp, f), base)
            if pats and ignored(rel(os.path.join(dp, f), pdir), pats):
                continue
            out.append(r)
            if len(out) >= limit:
                return out
    return out


def tree_text(pdir, limit=250):
    fs = files(pdir)
    lines = []
    for r in fs[:limit]:
        try:
            size = os.path.getsize(os.path.join(pdir, *r.split("/")))
        except OSError:
            size = 0
        lines.append(f"{r}  ({size:,} bytes)")
    if len(fs) > limit:
        lines.append(f"...and {len(fs) - limit} more files")
    return "\n".join(lines)


# ---------------------------------------------------------------- what kind of site is it?
def detect(pdir):
    """Returns {type, root, framework, build_dir, notes}.
    type: static | flask | php | node-build (React/Vite/... needs 'npm run build') | node-server | other"""
    info = {"type": "other", "root": "", "framework": "", "build_dir": "", "notes": []}
    fs = files(pdir, limit=6000)
    names = set(fs)
    pkg = None
    if "package.json" in names:
        try:
            pkg = json.loads(read_text(os.path.join(pdir, "package.json")))
        except ValueError:
            pkg = {}
    if pkg is not None:
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        scripts = pkg.get("scripts") or {}
        fw = next((f for f in ("next", "astro", "vite", "react-scripts", "@vue/cli-service", "@sveltejs/kit", "gatsby",
                               "nuxt", "parcel", "@angular/core") if f in deps), "")
        if fw and "build" in scripts:
            out = {"vite": "dist", "astro": "dist", "react-scripts": "build", "@vue/cli-service": "dist",
                   "parcel": "dist", "gatsby": "public", "@angular/core": "dist", "next": "out", "nuxt": ".output/public",
                   "@sveltejs/kit": "build"}.get(fw, "dist")
            info.update(type="node-build", framework=fw, build_dir=out)
            if fw == "next":
                info["notes"].append("Next.js: static hosting (FTP/Netlify/GitHub Pages) needs output: 'export' in "
                                     "next.config; Vercel works directly.")
            return info
        if "start" in scripts and any(d in deps for d in ("express", "fastify", "koa", "hapi", "@hapi/hapi")):
            info.update(type="node-server", framework=next(d for d in ("express", "fastify", "koa", "hapi", "@hapi/hapi")
                                                           if d in deps))
            return info
    py_app = next((p for p in ("app.py", "main.py", "wsgi.py", "run.py") if p in names), None)
    req = read_text(os.path.join(pdir, "requirements.txt")).lower() if "requirements.txt" in names else ""
    if py_app and ("flask" in req or "from flask" in read_text(os.path.join(pdir, py_app), 20000)):
        info.update(type="flask", framework="flask")
        return info
    php = [f for f in fs if f.endswith((".php", ".phtml"))]
    if php:
        info.update(type="php", framework="wordpress" if "wp-config.php" in names or "wp-login.php" in names else "")
        if info["framework"] == "wordpress":
            info["notes"].append("WordPress: pages and posts live in the database; Kakarot edits theme/plugin files "
                                 "and settings files.")
        if "public/index.php" in names and "index.php" not in names:
            info["root"] = "public"
        return info
    htmls = [f for f in fs if f.endswith((".html", ".htm"))]
    if htmls:
        idx = sorted((f for f in htmls if f.split("/")[-1] in ("index.html", "index.htm")), key=lambda f: f.count("/"))
        info["type"] = "static"
        if idx and "/" in idx[0] and idx[0].count("/") <= 2 and not any(f.count("/") == 0 for f in htmls):
            info["root"] = idx[0].rsplit("/", 1)[0]   # e.g. site lives in public/ or docs/
        return info
    return info


def web_root(pdir, spec):
    """Folder that is served to visitors (project folder, or public/, docs/, a build output...)."""
    if spec.get("type") == "node-build":
        return os.path.join(pdir, *(spec.get("build_dir") or "dist").split("/"))
    return os.path.join(pdir, *spec["root"].split("/")) if spec.get("root") else pdir


def url_to_file(pdir, spec, path):
    """Which source file most likely makes this page? '/about' -> about.html, '/x/' -> x/index.html."""
    root = web_root(pdir, spec)
    path = (path or "/").split("?")[0].split("#")[0]
    p = path.lstrip("/")
    cands = []
    if not p or p.endswith("/"):
        cands += [p + "index.html", p + "index.php", p + "index.htm"]
    else:
        cands += [p, p + ".html", p + ".php", p + "/index.html", p + "/index.php"]
    for c in cands:
        fp = os.path.join(root, *c.split("/"))
        if os.path.isfile(fp):
            return rel(fp, pdir)
    return None


# ---------------------------------------------------------------- history (undo for every change)
class Change:
    """Collects every file Kakarot touches during one job; saves the 'before' versions so it can be undone.
        ch = Change(pdir, "Fix 5 problems"); ch.write("css/style.css", text); ...; ch.commit()"""

    def __init__(self, pdir, label, kind="change"):
        self.pdir, self.label, self.kind = pdir, label, kind
        self.id = "c" + stamp()
        self.dir = os.path.join(pdir, META, "history", self.id)
        self.touched = {}          # rel -> "modified" | "created"
        self.notes = []

    def _before(self, relpath):
        if relpath in self.touched:
            return
        fp = abspath(self.pdir, relpath)
        if os.path.exists(fp):
            dst = os.path.join(self.dir, "before", *relpath.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(fp, dst)
            self.touched[relpath] = "modified"
        else:
            self.touched[relpath] = "created"

    def original(self, relpath):
        """Text of the file before this change started (or '' if it is new)."""
        if self.touched.get(relpath) == "modified":
            return read_text(os.path.join(self.dir, "before", *relpath.split("/")))
        if self.touched.get(relpath) == "created":
            return ""
        fp = abspath(self.pdir, relpath)
        return read_text(fp) if os.path.exists(fp) else ""

    def write(self, relpath, text):
        self._before(relpath)
        fp = abspath(self.pdir, relpath)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def write_bytes(self, relpath, data):
        self._before(relpath)
        fp = abspath(self.pdir, relpath)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "wb") as f:
            f.write(data)

    def delete(self, relpath):
        self._before(relpath)
        fp = abspath(self.pdir, relpath)
        if os.path.exists(fp):
            os.remove(fp)
        if self.touched.get(relpath) == "created":   # created and deleted again in the same change
            self.touched.pop(relpath)

    def restore_file(self, relpath):
        """Put one file back the way it was before this change (used when an edit made things worse)."""
        state = self.touched.pop(relpath, None)
        fp = abspath(self.pdir, relpath)
        if state == "modified":
            shutil.copy2(os.path.join(self.dir, "before", *relpath.split("/")), fp)
        elif state == "created" and os.path.exists(fp):
            os.remove(fp)

    def rollback(self):
        for r in list(self.touched):
            self.restore_file(r)
        shutil.rmtree(self.dir, ignore_errors=True)

    def stats(self):
        out = []
        for r, state in sorted(self.touched.items()):
            fp = abspath(self.pdir, r)
            now_exists = os.path.exists(fp)
            if not is_text(fp if now_exists else os.path.join(self.dir, "before", *r.split("/"))):
                out.append({"path": r, "state": state if now_exists else "deleted", "added": 0, "removed": 0,
                            "binary": True})
                continue
            old = self.original(r).splitlines()
            new = read_text(fp).splitlines() if now_exists else []
            add = rem = 0
            for line in difflib.unified_diff(old, new, lineterm="", n=0):
                if line.startswith("+") and not line.startswith("+++"):
                    add += 1
                elif line.startswith("-") and not line.startswith("---"):
                    rem += 1
            if not add and not rem and state == "modified":
                continue
            out.append({"path": r, "state": state if now_exists else "deleted", "added": add, "removed": rem})
        return out

    def diff(self, limit=200000):
        parts = []
        for r in sorted(self.touched):
            fp = abspath(self.pdir, r)
            if os.path.exists(fp) and not is_text(fp):
                parts.append(f"Binary file {r} changed")
                continue
            new = read_text(fp).splitlines() if os.path.exists(fp) else []
            parts.extend(difflib.unified_diff(self.original(r).splitlines(), new, f"a/{r}", f"b/{r}", lineterm=""))
        return "\n".join(parts)[:limit]

    def commit(self, extra=None):
        st = self.stats()
        if not st:
            shutil.rmtree(self.dir, ignore_errors=True)
            return None
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "changes.diff"), "w", encoding="utf-8") as f:
            f.write(self.diff())
        meta = {"id": self.id, "at": now(), "label": self.label, "kind": self.kind, "files": st,
                "touched": self.touched, "notes": self.notes, **(extra or {})}
        with open(os.path.join(self.dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        return meta

    def summary(self):
        st = self.stats()
        return ", ".join(f"{s['path']} ({'new' if s['state'] == 'created' else s['state']}"
                         + ("" if s.get("binary") else f", +{s['added']} -{s['removed']}") + ")" for s in st) or "no files"


def history(pdir, limit=50):
    d = os.path.join(pdir, META, "history")
    out = []
    if os.path.isdir(d):
        for cid in sorted(os.listdir(d), reverse=True)[:limit]:
            try:
                with open(os.path.join(d, cid, "meta.json"), encoding="utf-8") as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                continue
    return out


def change_diff(pdir, cid):
    p = os.path.join(pdir, META, "history", cid, "changes.diff")
    return read_text(p) if os.path.exists(p) else ""


def undo(pdir, cid=None):
    """Undo a change (the newest one if no id). The undo itself is recorded, so it can be undone too."""
    hist = [h for h in history(pdir, 500) if not h.get("undone")]
    target = next((h for h in hist if h["id"] == cid), None) if cid else (hist[0] if hist else None)
    if not target:
        raise ValueError("There is no change to undo." if not cid else f"No change with id {cid}.")
    src = os.path.join(pdir, META, "history", target["id"])
    ch = Change(pdir, f"Undo: {target['label']}", kind="undo")
    for r, state in target.get("touched", {}).items():
        if state == "modified":
            ch.write_bytes(r, read_bytes(os.path.join(src, "before", *r.split("/"))))
        elif state == "created":
            ch.delete(r)
    meta = ch.commit({"undo_of": target["id"]})
    target["undone"] = meta["id"] if meta else True
    with open(os.path.join(src, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(target, f, ensure_ascii=False, indent=1)
    return target, meta


# ---------------------------------------------------------------- sync manifests (what was last downloaded/published)
def manifest_path(pdir, key):
    return os.path.join(meta_dir(pdir, "sync"), re.sub(r"[^\w.-]+", "_", key) + ".json")


def load_manifest(pdir, key):
    try:
        with open(manifest_path(pdir, key), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_manifest(pdir, key, data):
    p = manifest_path(pdir, key)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=0)
    os.replace(p + ".tmp", p)


def local_hashes(base, rels):
    out = {}
    for r in rels:
        try:
            out[r] = sha1(read_bytes(os.path.join(base, *r.split("/"))))
        except OSError:
            pass
    return out
