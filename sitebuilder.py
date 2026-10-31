"""Website builder: "I want a website for my bus company with online booking" -> a complete, tested website.

Pipeline (runs in the background as a Mission, progress in the Missions tab):
  1. Plan    - the coding brain designs the site: pages, features, design system, files (kakarot-site.json)
  2. Write   - every file is written in order, each one seeing the files it depends on (so names match)
  3. Test    - the site is opened in a real (hidden) browser: broken links, missing files, JavaScript errors,
               server crashes (Flask) and empty pages are found
  4. Fix     - each problem goes back to the coding brain with the exact error, then it's tested again
  5. Look    - the picture brain looks at screenshots (desktop + phone) and the design gets one polish pass
  6. Deliver - README, run-site.bat (backend apps), a .zip for hosting (e.g. Namecheap cPanel), and a live link
"""
import functools
import http.server
import json
import logging
import os
import re
import shutil
import socketserver
import threading
import time
import zipfile
from urllib.parse import urljoin, urlparse

import requests

import coder
import db
import sites
from config import CFG
from tools import S, _cut, tool

log = logging.getLogger("kakarot.sitebuilder")
SPEC_FILE = "kakarot-site.json"
TEXT_EXT = {".html", ".css", ".js", ".py", ".txt", ".md", ".json", ".svg", ".xml", ".sql", ".bat", ".toml", ".cfg",
            ".ini", ".webmanifest", ".htaccess"}

PLAN_PROMPT = """You are a senior full-stack web architect and designer. Plan a COMPLETE, professional, modern
website for this request from the user:

\"\"\"{request}\"\"\"
{assets}
Choose the type:
- "static": HTML + CSS + JavaScript only. For company/shop/restaurant/portfolio/landing sites, galleries, menus,
  calculators, dashboards with sample data, quizzes, games. Can be hosted anywhere (e.g. Namecheap cPanel).
- "flask": Python Flask + SQLite backend. ONLY when it really needs a server: user accounts/login, an admin panel,
  saving bookings/orders/messages to a database, file uploads, reports from stored data.

Reply with ONLY this JSON:
{{"name": "short_folder_name", "title": "Site title", "type": "static or flask",
  "summary": "2-3 sentences: what the site is and who it is for",
  "language": "language(s) of the site text, e.g. English, Bangla, or both",
  "design": {{"style": "visual direction (e.g. clean corporate, bold modern, warm elegant)",
             "colors": {{"primary": "#hex", "secondary": "#hex", "accent": "#hex", "background": "#hex",
                        "text": "#hex"}},
             "fonts": "Google Fonts names for headings and body",
             "notes": "layout ideas, imagery, icons"}},
  "pages": [{{"title": "Home", "file": "index.html (or route like / for flask)", "sections": ["hero ...", "..."]}}],
  "features": ["every feature, concretely"],
  "data": "content/sample data to include, and (flask) the database tables with their columns",
  "files": [{{"path": "relative/path.ext", "purpose": "exactly what this file contains",
              "depends_on": ["paths of files it must match"]}}]
}}
Rules for "files":
- Order them so each file comes AFTER the files it depends on. Typical static order: css/style.css (complete design
  system: variables, typography, buttons, cards, grid, header/nav, footer, forms, utilities, responsive rules),
  js/main.js, then each page .html. Typical flask order: requirements.txt, models.py/db.py, app.py,
  templates/base.html, the other templates, static/css/style.css, static/js/main.js.
- 5 to 16 files. Keep each file under about 300 lines; split large CSS/JS/templates into more files.
- Do NOT list images; use inline SVG icons/illustrations, CSS gradients, and photos from
  https://picsum.photos/seed/<word>/<width>/<height> (or the user's own images if given).
- Static sites: every link and asset path is RELATIVE (css/style.css, about.html) - never starting with '/'.
- Flask apps: app.py reads the port from env PORT (default 5000) and runs on host 0.0.0.0, stores SQLite in the
  'instance' folder, creates tables and realistic sample data on first run, uses url_for() for EVERY link, form
  action, redirect and static file, and wraps app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1,
  x_prefix=1) (from werkzeug.middleware.proxy_fix). JavaScript fetch() calls use relative URLs. If there is a
  login/admin, create a default admin account (username admin, password admin123) and say so in the summary.
"""

FILE_PROMPT = """You are building this website. Write the file `{path}`.

WEBSITE PLAN:
{spec}

ALL FILES OF THE SITE (path - purpose):
{file_list}

THIS FILE: {path}
Purpose: {purpose}

{context}
Requirements:
- Complete and production quality: no placeholders, no "TODO", no "...", no lorem ipsum. Write real, convincing
  content for this specific business/purpose{lang}.
- Match the other files EXACTLY: use the same CSS class names, element ids, JS function names, routes, template
  names, block names, form field names and database columns as shown above.
- Design: modern and polished, follow the plan's colors and fonts, generous spacing, clear hierarchy, hover/focus
  states, subtle transitions. Responsive and mobile-first (must look right at 360px wide; nav collapses to a menu
  button on phones). Accessible: semantic HTML, alt text, labels, good contrast.
- Static sites use relative paths only. Flask templates use url_for() for every link and asset.
Reply with ONLY the complete file in one code block."""

FIX_PROMPT = """The website below was tested and this file has problems. Fix ALL of them.

WEBSITE PLAN (short):
{spec}

FILE `{path}` NOW:
```
{code}
```

PROBLEMS FOUND BY THE TESTS:
{problems}
{context}
Reply with the COMPLETE corrected file `{path}` in one code block (keep everything that works)."""

MISSING_PROMPT = """The website below links to `{path}` but that file doesn't exist. Create it so it fits the site
perfectly (same layout, header, footer, styles and tone as the other pages).

WEBSITE PLAN:
{spec}

AN EXISTING PAGE TO MATCH:
```
{example}
```
Reply with ONLY the complete file `{path}` in one code block."""

LOOK_PROMPT = """You are a strict web designer. These are screenshots of a website ({which}).
List up to 6 concrete VISUAL problems you can see (broken or overlapping layout, unreadable text, poor contrast,
huge empty areas, unstyled elements, images missing, things cut off on the phone view). Then rate it 1-10.
Reply ONLY JSON: {{"score": number, "problems": ["..."]}}"""

POLISH_PROMPT = """Improve this stylesheet `{path}` of the website below to fix these visual problems seen in
screenshots, and make the design more polished overall (spacing, typography, contrast, consistent cards/buttons,
mobile layout). Keep every existing class name (pages depend on them).

WEBSITE PLAN (short):
{spec}

VISUAL PROBLEMS:
{problems}

CURRENT FILE:
```
{code}
```
Reply with the COMPLETE improved file in one code block."""

# ---------------------------------------------------------------- helpers
def _json(text):
    m = re.search(r"\{.*\}", text or "", flags=re.S)
    if not m:
        raise ValueError("no JSON in the answer")
    return json.loads(m.group(0))


def _slug(name):
    return (re.sub(r"[^a-z0-9_-]+", "_", (name or "").lower()).strip("_") or "website")[:40]


def _read(p, limit=None):
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read() if limit is None else f.read(limit)


def _write(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _short_spec(spec):
    s = {k: spec.get(k) for k in ("title", "type", "summary", "language", "design", "features")}
    s["pages"] = [{"title": p.get("title"), "file": p.get("file")} for p in spec.get("pages", [])]
    return json.dumps(s, ensure_ascii=False, indent=1)


def _file_list(spec):
    return "\n".join(f"- {f['path']} - {f.get('purpose', '')}" for f in spec.get("files", []))


def _context_for(pdir, deps, budget=26000):
    parts, used = [], 0
    for d in deps:
        p = os.path.join(pdir, *d.split("/"))
        if not os.path.exists(p):
            continue
        text = _read(p)
        room = budget - used
        if room < 1500:
            break
        if len(text) > room:
            text = text[:room] + "\n/* ...(rest of file cut) */"
        parts.append(f"--- {d} (already written - match it) ---\n{text}")
        used += len(text)
    return ("FILES THIS ONE MUST MATCH:\n" + "\n\n".join(parts)) if parts else ""


def _ask_file(prompt):
    answer, _ = coder.ask_coder(prompt)
    return coder.extract_code(answer)


# ---------------------------------------------------------------- testing
class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def _serve_static(pdir):
    return sites.serve_folder(pdir)


def _static_link_problems(pdir):
    """Broken relative links/assets in static HTML. Returns {file: [problems]}, missing_files."""
    from bs4 import BeautifulSoup
    probs, missing = {}, {}
    for dp, dirs, files in os.walk(pdir):
        dirs[:] = [d for d in dirs if d not in coder.SKIP]
        for f in files:
            if not f.endswith(".html"):
                continue
            fp = os.path.join(dp, f)
            rel = os.path.relpath(fp, pdir).replace(os.sep, "/")
            soup = BeautifulSoup(_read(fp), "html.parser")
            for tag, attr in (("a", "href"), ("link", "href"), ("script", "src"), ("img", "src"), ("source", "src")):
                for el in soup.find_all(tag):
                    v = (el.get(attr) or "").strip()
                    if not v or re.match(r"^(https?:|//|#|mailto:|tel:|data:|javascript:|sms:|whatsapp:)", v):
                        continue
                    if v.startswith("/"):
                        probs.setdefault(rel, []).append(f"'{v}' starts with '/' - static sites must use relative "
                                                         f"paths (e.g. '{v.lstrip('/')}')")
                        continue
                    target = os.path.normpath(os.path.join(dp, v.split("#")[0].split("?")[0]))
                    if not os.path.exists(target):
                        trel = os.path.relpath(target, pdir).replace(os.sep, "/")
                        if trel.endswith((".html", ".css", ".js")) and not trel.startswith(".."):
                            missing.setdefault(trel, rel)
                        else:
                            probs.setdefault(rel, []).append(f"<{tag} {attr}=\"{v}\"> points to a file that "
                                                             f"doesn't exist")
    return probs, missing


def _browser_check(base, pages, shots_dir=None):
    """Open pages in hidden Chromium. Returns {page: [problems]}, screenshot paths."""
    from playwright.sync_api import sync_playwright
    probs, shots = {}, []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        try:
            for i, (url, owner) in enumerate(pages):
                page = b.new_page(viewport={"width": 1280, "height": 900})
                errs, by_file = [], {}

                def js_owner(text, owner=owner):
                    """Blame the script file named in the error's stack, not the page that loaded it."""
                    for path in re.findall(r"https?://127\.0\.0\.1:\d+/([^\s:)?#]+\.js)", text or ""):
                        rel = re.sub(r"^apps/[^/]+/", "", path)
                        return rel
                    return owner

                def on_error(e):
                    stack = getattr(e, "stack", "") or str(e)
                    by_file.setdefault(js_owner(stack), []).append(f"JavaScript error on {owner}: {e}\n{stack[:600]}")

                def on_console(m):
                    if m.type != "error" or "favicon" in m.text or "picsum" in m.text:
                        return
                    url = (m.location or {}).get("url", "")
                    by_file.setdefault(js_owner(url), []).append(f"Console error on {owner}: {m.text}")
                page.on("pageerror", on_error)
                page.on("console", on_console)
                try:
                    resp = page.goto(url, wait_until="load", timeout=30000)
                    page.wait_for_timeout(800)
                    if resp and resp.status >= 400:
                        errs.append(f"Page answered HTTP {resp.status}")
                    text = page.inner_text("body") if page.query_selector("body") else ""
                    if len(text.strip()) < 40:
                        errs.append("The page shows almost no text (it may be broken or empty).")
                    if shots_dir is not None and i == 0:
                        os.makedirs(shots_dir, exist_ok=True)
                        d = os.path.join(shots_dir, "desktop.png")
                        page.screenshot(path=d, full_page=False)
                        page.set_viewport_size({"width": 390, "height": 844})
                        page.wait_for_timeout(500)
                        m = os.path.join(shots_dir, "phone.png")
                        page.screenshot(path=m, full_page=False)
                        shots = [d, m]
                except Exception as e:
                    errs.append(f"Could not load the page: {e}")
                if errs:
                    by_file.setdefault(owner, []).extend(errs)
                for k, v in by_file.items():
                    probs.setdefault(k, []).extend(x for x in dict.fromkeys(v) if x not in probs.get(k, []))
                page.close()
        finally:
            b.close()
    return probs, shots


def _flask_pages(base, limit=25):
    """Crawl a running Flask app from '/'. Returns [(url, guessed_template_owner)], problems."""
    from bs4 import BeautifulSoup
    seen, queue, out, probs = set(), [base], [], []
    while queue and len(seen) < limit:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            r = requests.get(url, timeout=30)
        except requests.RequestException as e:
            probs.append(f"{urlparse(url).path}: request failed ({e})")
            continue
        if r.status_code >= 500:
            probs.append(f"{urlparse(url).path}: server error HTTP {r.status_code}")
            continue
        if r.status_code == 404:
            probs.append(f"{urlparse(url).path}: page not found (404) but it is linked from the site")
            continue
        out.append((url, "app.py"))
        if "html" not in r.headers.get("content-type", ""):
            continue
        for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
            u = urljoin(url, a["href"].split("#")[0])
            if u.startswith(base) and u not in seen and not re.search(r"logout|delete|remove", u, re.I):
                queue.append(u)
    return out, probs


def _owner_from_trace(pdir, text):
    """Which project file does a Python traceback point at (last frame inside the project)?"""
    hits = re.findall(r'File "([^"]+)", line (\d+)', text or "")
    for path, _ in reversed(hits):
        rp = os.path.realpath(path)
        if rp.startswith(os.path.realpath(pdir)) and ".venv" not in rp:
            return os.path.relpath(rp, pdir).replace(os.sep, "/")
    m = re.search(r"TemplateNotFound: ([\w./-]+)", text or "")
    if m:
        return "app.py"
    m = re.search(r'templates[\\/]([\w./-]+\.html)', text or "")
    return f"templates/{m.group(1)}" if m else "app.py"


def test_site(pdir, spec, shots_dir=None):
    """Returns (problems {file: [..]}, missing {file: linked_from}, screenshots)."""
    if spec.get("type") == "flask":
        port = sites.free_port()
        proc, err = sites.start_app(pdir, port)
        if not proc:
            return {_owner_from_trace(pdir, err): [err[-2500:]]}, {}, []
        try:
            base = f"http://127.0.0.1:{port}/"
            pages, crawl_probs = _flask_pages(base)
            probs = {}
            logtxt = sites.read_log(pdir, 8000)
            if crawl_probs:
                owner = _owner_from_trace(pdir, logtxt)
                probs.setdefault(owner, []).extend(crawl_probs)
                if "Traceback" in logtxt:
                    probs[owner].append("Server log:\n" + logtxt[-2500:])
            bprobs, shots = _browser_check(base, [(u, "templates") for u, _ in pages[:8]] or [(base, "app.py")],
                                           shots_dir)
            for k, v in bprobs.items():
                probs.setdefault("templates/base.html" if k == "templates" else k, []).extend(v)
            return probs, {}, shots
        finally:
            coder._kill(proc)
    probs, missing = _static_link_problems(pdir)
    htmls = sorted(os.path.relpath(os.path.join(dp, f), pdir).replace(os.sep, "/")
                   for dp, dirs, fs in os.walk(pdir) for f in fs if f.endswith(".html") and ".venv" not in dp)
    if "index.html" in htmls:
        htmls.remove("index.html")
        htmls.insert(0, "index.html")
    srv, base = _serve_static(pdir)
    try:
        bprobs, shots = _browser_check(base, [(base + h, h) for h in htmls[:20]], shots_dir)
    finally:
        srv.shutdown()
    for k, v in bprobs.items():
        probs.setdefault(k, []).extend(v)
    if not htmls:
        probs["index.html"] = ["There is no index.html start page."]
        missing["index.html"] = ""
    return probs, missing, shots


# ---------------------------------------------------------------- mission bookkeeping
class Job:
    def __init__(self, mid):
        import missions
        self.m = missions
        self.mid = mid
        row = missions._get(mid)
        self.goal = row["goal"]
        self.plan = row["plan"]
        self.log = row["log"]
        self.data = json.loads(db.q("SELECT data FROM missions WHERE id=?", (mid,))[0]["data"] or "{}")

    def cancelled(self):
        return (self.m._get(self.mid) or {}).get("status") == "cancelled"

    def set_plan(self, steps):
        self.plan = steps
        self.m._save(self.mid, plan=steps)

    def done(self, step, result, ok=True):
        self.log.append({"step": step, "ok": ok, "result": result, "actions": []})
        self.m._save(self.mid, log=self.log)

    def has(self, step):
        return any(e["step"] == step for e in self.log)


def _fix_round(pdir, spec, probs, missing, job, round_no):
    fixed = []
    for path, linked_from in list(missing.items())[:6]:
        example_path = os.path.join(pdir, *linked_from.split("/")) if linked_from else ""
        example = _read(example_path, 9000) if example_path and os.path.exists(example_path) else ""
        code = _ask_file(MISSING_PROMPT.format(path=path, spec=_short_spec(spec), example=example))
        _write(os.path.join(pdir, *path.split("/")), code)
        fixed.append(f"created missing {path}")
    for path, plist in list(probs.items())[:8]:
        if job.cancelled():
            break
        fp = os.path.join(pdir, *path.split("/"))
        if not os.path.isfile(fp):
            continue
        deps = next((f.get("depends_on", []) for f in spec.get("files", []) if f["path"] == path), [])
        ctx = _context_for(pdir, deps, 12000)
        code = _ask_file(FIX_PROMPT.format(spec=_short_spec(spec), path=path, code=_read(fp, 40000),
                                           problems="\n".join(f"- {p}" for p in plist[:12]),
                                           context=("\n" + ctx) if ctx else ""))
        old = _read(fp, 40000)
        if len(code.strip()) < 0.35 * len(old.strip()):  # a "fix" that throws most of the file away is wrong
            fixed.append(f"rejected a bad fix for {path} (it removed too much)")
            continue
        _write(fp, code)
        fixed.append(f"fixed {path} ({len(plist)} problem{'s' if len(plist) > 1 else ''})")
    return fixed


def _look_and_polish(pdir, spec, shots):
    """Picture brain reviews screenshots; one polish pass on the main stylesheet if needed."""
    import vision
    if not shots:
        return "No screenshots to check."
    try:
        imgs = [vision.load_images(s)[0] for s in shots]
        raw = vision.ask_vision(imgs, LOOK_PROMPT.format(which="desktop view, then phone view"))
        review = _json(raw)
    except Exception as e:
        return f"Visual check skipped ({e})."
    score, problems = review.get("score"), [p for p in review.get("problems") or [] if isinstance(p, str)]
    note = f"Picture brain score: {score}/10." + (" Problems: " + "; ".join(problems) if problems else "")
    try:
        bad = float(score) < 8
    except (TypeError, ValueError):
        bad = bool(problems)
    css = next((f["path"] for f in spec.get("files", []) if f["path"].endswith(".css")), None)
    if bad and problems and css and os.path.exists(os.path.join(pdir, *css.split("/"))):
        fp = os.path.join(pdir, *css.split("/"))
        code = _ask_file(POLISH_PROMPT.format(path=css, spec=_short_spec(spec), code=_read(fp, 40000),
                                              problems="\n".join(f"- {p}" for p in problems)))
        if len(code) > 0.6 * os.path.getsize(fp):  # never accept a stylesheet that lost most of its rules
            _write(fp, code)
            note += f" Polished {css}."
    return note


RUN_BAT = """@echo off
title {title}
cd /d "%~dp0"
if not exist .venv\\Scripts\\python.exe (
  echo Setting up (first time only)...
  python -m venv .venv
)
.venv\\Scripts\\python.exe -m pip install -q -r requirements.txt
echo.
echo  {title} is running: http://127.0.0.1:5000   (close this window to stop)
start "" http://127.0.0.1:5000
.venv\\Scripts\\python.exe app.py
pause
"""


def _package(pdir, name, spec, notes):
    files = [os.path.relpath(os.path.join(dp, f), pdir) for dp, dirs, fs in os.walk(pdir)
             for f in fs if not any(s in dp.split(os.sep) for s in coder.SKIP | {"instance", "_screens"})
             and f not in ("server.log",)]
    if spec.get("type") == "flask":
        _write(os.path.join(pdir, "run-site.bat"), RUN_BAT.format(title=spec.get("title", name)).replace("\n", "\r\n"))
    how = ("Double-click run-site.bat (it installs what it needs and opens the site)." if spec.get("type") == "flask"
           else "Open index.html in any browser, or upload all files to your hosting (e.g. Namecheap cPanel → "
                "File Manager → public_html) - or upload the .zip and extract it there.")
    readme = (f"# {spec.get('title', name)}\n\n{spec.get('summary', '')}\n\n## Pages\n" +
              "\n".join(f"- {p.get('title')}: {p.get('file')}" for p in spec.get("pages", [])) +
              "\n\n## Features\n" + "\n".join(f"- {f}" for f in spec.get("features", [])) +
              f"\n\n## How to use\n{how}\n\nBuilt by Kakarot. Ask Kakarot to change anything: "
              f"\"update my website {name}: ...\"\n")
    _write(os.path.join(pdir, "README.md"), readme)
    zpath = os.path.join(coder.CODE_DIR, f"{name}.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in dict.fromkeys(files + ["README.md"] + (["run-site.bat"] if spec.get("type") == "flask" else [])):
            fp = os.path.join(pdir, rel)
            if os.path.isfile(fp) and not rel.startswith(SPEC_FILE):
                z.write(fp, rel)
    return zpath


def _test_and_fix(pdir, spec, job, rounds):
    shots_dir = os.path.join(pdir, "_screens")
    history, probs, missing, shots = [], {}, {}, []
    for r in range(1, rounds + 1):
        if job.cancelled():
            break
        probs, missing, shots = test_site(pdir, spec, shots_dir)
        n = sum(len(v) for v in probs.values()) + len(missing)
        if not n:
            history.append(f"Test {r}: everything works (no broken links, no errors).")
            break
        detail = "; ".join(f"{k}: {v[0][:140]}" for k, v in list(probs.items())[:5])
        history.append(f"Test {r}: {n} problem(s) - {detail}" + (f"; missing files: {', '.join(missing)}"
                                                                   if missing else ""))
        if r == rounds:
            break
        fixed = _fix_round(pdir, spec, probs, missing, job, r)
        history.append("  Fixed: " + (", ".join(fixed) or "nothing could be fixed automatically"))
    remaining = sum(len(v) for v in probs.values()) + len(missing)
    return history, shots, remaining, probs


# ---------------------------------------------------------------- the build
def run(mid):
    job = Job(mid)
    import missions
    import workshop
    mode = job.data.get("mode")
    if mode == "update":                      # older queued jobs -> the new safe change engine
        job.data["mode"] = "change"
        db.x("UPDATE missions SET data=? WHERE id=?", (json.dumps(job.data, ensure_ascii=False), mid))
        mode = "change"
    if mode in workshop.MODES:
        return workshop.run(mid)
    missions._save(mid, status="running")
    request = job.data.get("request") or job.goal
    rounds = int(CFG.get("site_fix_rounds", 4))
    # 1. plan
    pdir = job.data.get("dir")
    if not job.has("Plan the website"):
        if not job.plan:
            job.set_plan(["Plan the website", "Write the files", "Test in a real browser and fix",
                          "Visual check", "Package and publish preview"])
        assets = ""
        if job.data.get("assets"):
            assets = "\nThe user gave these files to use (logo/photos), they will be in images/: " + \
                     ", ".join(os.path.basename(a) for a in job.data["assets"]) + "\n"
        raw, _ = coder.ask_coder(PLAN_PROMPT.format(request=request, assets=assets))
        try:
            spec = _json(raw)
        except Exception:
            raw, _ = coder.ask_coder(PLAN_PROMPT.format(request=request, assets=assets) +
                                     "\nIMPORTANT: reply with valid JSON only.")
            spec = _json(raw)
        spec["type"] = "flask" if str(spec.get("type", "")).lower().startswith("flask") else "static"
        name = _slug(job.data.get("name") or spec.get("name") or spec.get("title"))
        pdir = coder.code_path(name)
        if os.path.exists(pdir) and os.listdir(pdir):
            name = f"{name}_{time.strftime('%m%d%H%M')}"
            pdir = coder.code_path(name)
        os.makedirs(pdir, exist_ok=True)
        spec["name"], spec["request"] = name, request
        spec["origin"] = {"type": "built"}
        spec["files"] = [f for f in spec.get("files", []) if isinstance(f, dict) and f.get("path")][:18]
        for a in job.data.get("assets") or []:
            try:
                os.makedirs(os.path.join(pdir, "images"), exist_ok=True)
                shutil.copy2(a, os.path.join(pdir, "images", os.path.basename(a)))
            except Exception as e:
                log.warning("asset copy failed: %s", e)
        _write(os.path.join(pdir, SPEC_FILE), json.dumps(spec, ensure_ascii=False, indent=2))
        job.data.update(dir=pdir, name=name)
        db.x("UPDATE missions SET data=? WHERE id=?", (json.dumps(job.data, ensure_ascii=False), mid))
        steps = ["Plan the website"] + [f"Write {f['path']}" for f in spec["files"]] + \
                ["Test in a real browser and fix", "Visual check", "Package and publish preview"]
        job.set_plan(steps)
        job.done("Plan the website", f"{spec.get('title')} ({spec['type']} site): {spec.get('summary', '')}\n"
                                     f"Pages: {', '.join(p.get('title', '') for p in spec.get('pages', []))}\n"
                                     f"Files: {len(spec['files'])}")
    with open(os.path.join(pdir, SPEC_FILE), encoding="utf-8") as f:
        spec = json.load(f)
    name = spec["name"]
    lang = f" (in {spec['language']})" if spec.get("language") and "english" != spec["language"].lower() else ""
    # 2. write files
    for f in spec["files"]:
        step = f"Write {f['path']}"
        if job.has(step):
            continue
        if job.cancelled():
            return
        ctx = _context_for(pdir, f.get("depends_on") or [])
        code = _ask_file(FILE_PROMPT.format(path=f["path"], spec=json.dumps(spec, ensure_ascii=False, indent=1)[:9000],
                                            file_list=_file_list(spec), purpose=f.get("purpose", ""),
                                            context=ctx, lang=lang))
        _write(os.path.join(pdir, *f["path"].split("/")), code)
        job.done(step, f"{code.count(chr(10))} lines written.")
    # 3+4. test and fix
    history, shots, remaining, probs = _test_and_fix(pdir, spec, job, rounds)
    if job.cancelled():
        return
    job.done("Test in a real browser and fix", "\n".join(history), ok=remaining == 0)
    # 5. look
    note = _look_and_polish(pdir, spec, shots) if CFG.get("site_visual_check", True) else "Skipped."
    if "Polished" in note:
        _, _, remaining2, probs = _test_and_fix(pdir, spec, job, 2)
        remaining = remaining2
    job.done("Visual check", note)
    # 6. deliver
    _finish(job, pdir, spec, remaining, probs, note)


def _finish(job, pdir, spec, remaining, probs, look_note, headline=None):
    import missions
    import scheduler
    name = spec["name"]
    zpath = _package(pdir, name, spec, look_note)
    link = sites.preview_path(name)
    left = ""
    if remaining:
        left = "\n\n⚠️ Still not perfect (" + str(remaining) + " issue(s) left after the fix rounds):\n" + \
               "\n".join(f"- {k}: {v[0][:200]}" for k, v in list(probs.items())[:5]) + \
               f"\nAsk me: \"fix my website {name}\" to keep working on it."
    result = (f"{headline or '🌐 Your website is ready'}: {spec.get('title', name)}\n"
              f"{spec.get('summary', '')}\n\n"
              f"▶ Open it: {link}  (works on this PC and on your phone through your Kakarot link)\n"
              f"📁 Files: {pdir}\n📦 Zip for hosting: {zpath}\n"
              + ("🖥 Run it on its own: double-click run-site.bat in the folder.\n" if spec.get("type") == "flask"
                 else "") + f"\nPages: {', '.join(p.get('title', '') for p in spec.get('pages', []))}{left}")
    job.done("Package and publish preview", f"Preview: {link}\nZip: {zpath}")
    missions._save(job.mid, status="done", result=result)
    db.add_inbox(f"🌐 Website ready: {spec.get('title', name)}", result)
    scheduler.notify("Website ready", spec.get("title", name))


# ---------------------------------------------------------------- tools for the assistant
def _assets(text):
    out = []
    for a in re.split(r"[,;\n]", text or ""):
        a = a.strip().strip('"')
        if not a:
            continue
        try:
            from tools import safe_path
            p = safe_path(a)
            if os.path.isfile(p):
                out.append(p)
        except Exception:
            continue
    return out


@tool("build_website",
      "BUILD A COMPLETE WEBSITE OR WEB APP for the user (company site, shop, restaurant, portfolio, booking system, "
      "dashboard, admin panel, landing page...). Your coding brain plans it, writes all files, tests it in a real "
      "browser, fixes errors and checks the design, in the background; the user gets a live link. Pass the user's "
      "FULL wishes: business/topic, pages, features, colors/style, language, example sites they like, contact info.",
      {"request": S("Everything the user wants, in detail (copy their words + any details from the chat)."),
       "name": S("Optional short folder name."),
       "assets": S("Optional comma-separated paths of the user's own logo/photos (e.g. attached files).")},
      ["request"])
def build_website(request, name="", assets=""):
    import missions
    files = _assets(assets)
    mid = missions.start(f"Build website: {request[:140]}", kind="website",
                         data={"request": request, "name": _slug(name) if name else "", "assets": files})
    n = 10
    return (f"Started building the website (Mission #{mid}). I'm planning it now, then I write about {n} files, test "
            f"everything in a real browser and fix problems. With the coding brain on this PC that takes roughly "
            f"30-90 minutes. Progress is in the Missions tab, and the live link will arrive in your Inbox.")
