"""Open an existing website in Kakarot, from:
  - a folder on this PC                     ("C:\\Users\\me\\Desktop\\my-site")
  - a GitHub repository                     ("github.com/owner/repo" or "owner/repo", private ones need a token)
  - your hosting over FTP/cPanel            (an FTP account saved in Websites → Accounts, e.g. Namecheap)
  - a live web address                      ("lalsobujdata.site") - if an FTP account belongs to that address, the
                                             real files are downloaded from the hosting; otherwise Kakarot saves a
                                             copy of what visitors see (good for checks; not the original code)
Kakarot always works on its OWN copy in the code folder. The original is only changed when you publish."""
import os
import re
import shutil
import tempfile
import time
import zipfile
from collections import deque
from urllib.parse import unquote, urljoin, urlparse

import requests

import coder
import deploy
import siteproject as sp
import vault
from config import CFG
from tools import ROOTS, UA

BLOCKED_DIRS = ("\\windows", "\\program files", "\\programdata", "\\appdata", "/etc", "/proc", "/sys", "\\.ssh",
                "/.ssh", "\\$recycle.bin")


def classify(source, account=""):
    s = (source or "").strip().strip('"')
    if account:
        if not account.startswith("ftp:"):
            account = "ftp:" + vault.slug(account)
        is_addr = re.match(r"^https?://", s, re.I) or re.match(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", s)
        return "ftp", {"account": account, "folder": None if (not s or is_addr) else s,
                       "url": (s if s.lower().startswith("http") else "https://" + s) if s and is_addr else None}
    if s.lower().startswith("ftp:") and vault.has(s.lower()):
        return "ftp", {"account": s.lower(), "folder": None}
    if s.lower().startswith(("ftp://", "ftps://")):
        host = urlparse(s).hostname or ""
        acc = next((a["key"] for a in vault.listing() if a["kind"] == "ftp" and a["host"].lower() == host.lower()), None)
        if not acc:
            raise ValueError(f"Add an FTP account for {host} first (Websites → Accounts), then open it again.")
        return "ftp", {"account": acc, "folder": urlparse(s).path or None}
    gh = deploy.parse_repo(s) if ("github.com" in s.lower() or (re.match(r"^[\w.-]+/[\w.-]+$", s) and not os.path.exists(os.path.expandvars(s)))) else None
    if gh:
        return "github", {"owner": gh[0], "repo": gh[1], "branch": gh[2]}
    if re.match(r"^https?://", s, re.I) or (re.match(r"^[\w-]+(\.[\w-]+)+(:\d+)?(/\S*)?$", s) and not os.path.exists(s)):
        url = s if re.match(r"^https?://", s, re.I) else "https://" + s
        acc = vault.ftp_for_host(urlparse(url).hostname)
        if acc:
            return "ftp", {"account": acc, "folder": None, "url": url}
        return "url", {"url": url}
    return "folder", {"path": s}


def _check_folder(path):
    p = os.path.realpath(os.path.expandvars(os.path.expanduser(path)))
    if not os.path.isdir(p):
        raise FileNotFoundError(f"There is no folder {p}")
    low = p.lower()
    if any(b in low for b in BLOCKED_DIRS) or len(p) <= 3:
        raise PermissionError(f"For safety Kakarot doesn't open system folders like {p}.")
    if os.path.normcase(p).startswith(os.path.normcase(os.path.realpath(coder.CODE_DIR))):
        raise ValueError("That folder is already inside Kakarot's code folder - use it directly by its name.")
    return p


def _limits():
    return int(CFG.get("site_import_max_mb", 400)) * 1024 * 1024, int(CFG.get("site_import_max_files", 8000))


def _finish(pdir, name, origin, title, live_url=None, notes=None):
    info = sp.detect(pdir)
    spec = {"name": name, "title": title or name, "type": info["type"], "root": info["root"],
            "framework": info["framework"], "build_dir": info["build_dir"], "imported": True, "origin": origin,
            "live_url": live_url or "", "summary": "", "notes": info["notes"] + (notes or []),
            "opened": sp.now(), "files": []}
    # a short readable title from the home page
    try:
        idx = sp.url_to_file(pdir, spec, "/")
        if idx:
            m = re.search(r"<title[^>]*>(.*?)</title>", sp.read_text(os.path.join(pdir, *idx.split("/")), 50000),
                          re.S | re.I)
            if m and m.group(1).strip() and "<?" not in m.group(1):
                spec["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:80]
    except Exception:
        pass
    sp.save(pdir, spec)
    return spec


def from_folder(path, name="", progress=None):
    src = _check_folder(path)
    max_bytes, max_files = _limits()
    name = sp.free_name(name or os.path.basename(src))
    pdir = sp.project_dir(name)
    total, count = 0, 0
    rels = sp.files(src, limit=max_files + 1)
    if len(rels) > max_files:
        raise ValueError(f"That folder has more than {max_files} files - is it really one website?")
    for r in rels:
        sz = os.path.getsize(os.path.join(src, *r.split("/")))
        total += sz
        if total > max_bytes:
            shutil.rmtree(pdir, ignore_errors=True)
            raise ValueError(f"That folder is bigger than {max_bytes >> 20} MB (setting site_import_max_mb).")
        dst = os.path.join(pdir, *r.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(src, *r.split("/")), dst)
        count += 1
        if progress and count % 200 == 0:
            progress(f"Copied {count}/{len(rels)} files")
    spec = _finish(pdir, name, {"type": "folder", "path": src}, os.path.basename(src))
    sp.save_manifest(pdir, "folder", {"files": {r: {"sha1": h} for r, h in sp.local_hashes(pdir, rels).items()},
                                      "at": sp.now()})
    return pdir, spec, f"Copied {count} files from {src}."


def from_github(owner, repo, branch=None, name="", progress=None):
    gh = deploy.GitHub(anonymous=not vault.has("github"))
    info = gh.repo(owner, repo)
    if not info:
        raise ValueError(f"github.com/{owner}/{repo} wasn't found" +
                         ("" if vault.has("github") else " (if it's private, add your GitHub token in Websites → Accounts)"))
    branch = branch or info.get("default_branch") or "main"
    head, _, remote = gh.files_at(owner, repo, branch)
    if not head:
        raise ValueError(f"The repository {owner}/{repo} is empty (or has no branch {branch}).")
    if progress:
        progress(f"Downloading {owner}/{repo} ({branch}, {len(remote)} files)")
    name = sp.free_name(name or repo)
    pdir = sp.project_dir(name)
    max_bytes, _ = _limits()
    with tempfile.TemporaryDirectory() as tmp:
        zp = os.path.join(tmp, "repo.zip")
        gh.zipball(owner, repo, head, zp)
        if os.path.getsize(zp) > max_bytes:
            raise ValueError(f"The repository is bigger than {max_bytes >> 20} MB.")
        with zipfile.ZipFile(zp) as z:
            for zi in z.infolist():
                parts = zi.filename.split("/", 1)
                if len(parts) < 2 or not parts[1] or zi.is_dir():
                    continue
                r = parts[1]
                if any(seg in sp.SKIP_DIRS or seg == ".." for seg in r.split("/")[:-1]):
                    continue
                dst = sp.abspath(pdir, r)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with z.open(zi) as fi, open(dst, "wb") as fo:
                    shutil.copyfileobj(fi, fo)
    files = {}
    for r, (rsha, _) in remote.items():
        lp = os.path.join(pdir, *r.split("/"))
        if os.path.exists(lp):
            files[r] = {"base": rsha, "local": sp.git_sha(sp.read_bytes(lp))}
    sp.save_manifest(pdir, f"github-{owner}-{repo}-{branch}", {"commit": head, "branch": branch, "files": files,
                                                                 "at": sp.now()})
    live = info.get("homepage") or ""
    if not live and info.get("has_pages"):
        live = f"https://{owner.lower()}.github.io/{repo}/" if repo.lower() != f"{owner.lower()}.github.io" else f"https://{repo}/"
    spec = _finish(pdir, name, {"type": "github", "owner": owner, "repo": repo, "branch": branch,
                                "private": info.get("private", False), "commit": head}, repo, live)
    spec.setdefault("publish", {})["github"] = {"repo": f"{owner}/{repo}", "branch": branch,
                                                 "pages": bool(info.get("has_pages")), "live_url": live}
    if info.get("description"):
        spec["summary"] = info["description"]
    sp.save(pdir, spec)
    return pdir, spec, f"Downloaded {len(files)} files from github.com/{owner}/{repo} ({branch})."


def from_ftp(account, folder=None, name="", url=None, progress=None):
    acc = vault.get(account)
    if not acc:
        raise ValueError(f"No FTP account '{account}'. Add it in Websites → Accounts.")
    folder = folder if folder is not None else (acc.get("folder") or "")
    max_bytes, max_files = _limits()
    host = urlparse(url or acc.get("site_url") or "").hostname or acc["host"]
    name = sp.free_name(name or (host.removeprefix("www.").split(".")[0] if host else account[4:]))
    pdir = sp.project_dir(name)
    os.makedirs(pdir, exist_ok=True)
    files, total, skipped = {}, 0, []
    with deploy.FTP(account) as f:
        root = f.base(folder)
        if progress:
            progress(f"Listing files on {acc['host']}{root}")
        listing = list(f.walk(root, skip=deploy._ftp_skip, limit=max_files + 1))
        if len(listing) > max_files:
            raise ValueError(f"The hosting folder has more than {max_files} files. Open a smaller folder "
                             f"(e.g. the theme folder) or raise site_import_max_files.")
        for i, (r, size, mod) in enumerate(listing, 1):
            if size > 25_000_000:
                skipped.append(f"{r} ({size >> 20} MB)")
                continue
            total += size
            if total > max_bytes:
                raise ValueError(f"The site is bigger than {max_bytes >> 20} MB (setting site_import_max_mb).")
            if progress and (i == 1 or i % 25 == 0):
                progress(f"Downloading {i}/{len(listing)}: {r}")
            lp = sp.abspath(pdir, r)
            f.download(f"{root}/{r}" if root != "/" else f"/{r}", lp)
            files[r] = {"sha1": sp.sha1(sp.read_bytes(lp)), "size": size, "modify": mod}
    sp.save_manifest(pdir, f"{account}-{folder}", {"files": files, "at": sp.now()})
    notes = [f"Not downloaded (too big): {', '.join(skipped[:8])}"] if skipped else []
    live = url or acc.get("site_url") or ""
    spec = _finish(pdir, name, {"type": "ftp", "account": account, "folder": folder, "host": acc["host"]},
                   host, live, notes)
    spec.setdefault("publish", {})[account] = {"folder": folder, "live_url": live}
    sp.save(pdir, spec)
    return pdir, spec, f"Downloaded {len(files)} files from {acc['host']} ({folder or 'home folder'})."


def from_url(url, name="", progress=None, max_pages=60):
    """Save a copy of a live site as visitors see it (HTML/CSS/JS/images), keeping the same paths."""
    max_bytes, max_files = _limits()
    s = requests.Session()
    s.headers.update(UA)
    try:
        first = s.get(url, timeout=30)
    except requests.RequestException as e:
        raise ValueError(f"{url} can't be opened ({e.__class__.__name__}).")
    base = first.url
    host = urlparse(base).netloc
    name = sp.free_name(name or host.split(":")[0].removeprefix("www.").split(".")[0])
    pdir = sp.project_dir(name)
    os.makedirs(pdir, exist_ok=True)
    pages, assets, saved, total = deque([base]), deque(), {}, 0
    seen = set()

    def local_path(u):
        path = unquote(urlparse(u).path) or "/"
        if path.endswith("/"):
            path += "index.html"
        elif "." not in path.split("/")[-1]:
            path += "/index.html"
        r = path.lstrip("/")
        return r if not re.search(r"[<>:\"|?*]", r) else re.sub(r"[<>:\"|?*]", "_", r)

    def same(u):
        return urlparse(u).netloc == host

    def fetch(u, is_page):
        nonlocal total
        if u in seen or len(saved) >= max_files:
            return None
        seen.add(u)
        try:
            r = s.get(u, timeout=30)
        except requests.RequestException:
            return None
        if r.status_code != 200 or not same(r.url):
            return None
        total += len(r.content)
        if total > max_bytes:
            return None
        rel = local_path(r.url)
        ctype = r.headers.get("content-type", "")
        if is_page and "html" not in ctype:
            is_page = False
        lp = sp.abspath(pdir, rel)
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        data = r.content
        if "html" in ctype or "css" in ctype:
            txt = r.text
            txt = re.sub(r"(https?:)?//" + re.escape(host) + r"(?=/)", "", txt)   # same-site links -> /path
            data = txt.encode(r.encoding or "utf-8", errors="replace")
        with open(lp, "wb") as fh:
            fh.write(data)
        saved[rel] = r.url
        return r, is_page

    count = 0
    while (pages or assets) and len(saved) < max_files:
        if pages and count < max_pages:
            u = pages.popleft()
            res = fetch(u, True)
            if not res:
                continue
            r, is_page = res
            count += 1
            if progress and count % 5 == 1:
                progress(f"Saving page {count}: {urlparse(r.url).path or '/'}")
            if not is_page:
                continue
            html = r.text
            for m in re.finditer(r"""(?:href|src|data-src|poster)\s*=\s*["']([^"'#]+)""", html, re.I):
                v = urljoin(r.url, m.group(1).strip())
                if not same(v) or v.startswith(("mailto:", "tel:", "javascript:")):
                    continue
                if re.search(r"\.(css|js|mjs|png|jpe?g|gif|webp|svg|ico|woff2?|ttf|otf|eot|mp4|webm|json|xml|txt|pdf)(\?|$)", v, re.I):
                    assets.append(v)
                elif not re.search(r"logout|wp-admin|wp-login|\?", v, re.I):
                    pages.append(v.split("#")[0])
            for m in re.finditer(r"srcset\s*=\s*[\"']([^\"']+)", html, re.I):
                for part in m.group(1).split(","):
                    v = urljoin(r.url, part.strip().split(" ")[0])
                    if same(v):
                        assets.append(v)
        elif assets:
            u = assets.popleft()
            res = fetch(u, False)
            if res and u.split("?")[0].endswith(".css"):
                for m in re.finditer(r"url\(\s*['\"]?([^'\")]+)", res[0].text):
                    v = urljoin(res[0].url, m.group(1))
                    if same(v) and not v.startswith("data:"):
                        assets.append(v)
        else:
            break
    notes = ["This is a COPY of what visitors see, not the original source code. Changes made here can only be "
             "published to new hosting. To fix the real site, add your hosting's FTP account (Websites → Accounts) "
             "and open the site again."]
    spec = _finish(pdir, name, {"type": "url", "url": base}, host, base, notes)
    spec["type"] = "static" if spec["type"] in ("other", "php") else spec["type"]
    spec["mirror"] = True
    sp.save(pdir, spec)
    return pdir, spec, f"Saved {count} page(s) and {len(saved) - count} file(s) from {base}."


def open_site(source, name="", account="", progress=None):
    kind, info = classify(source, account)
    t0 = time.time()
    if kind == "folder":
        out = from_folder(info["path"], name, progress)
    elif kind == "github":
        out = from_github(info["owner"], info["repo"], info.get("branch"), name, progress)
    elif kind == "ftp":
        out = from_ftp(info["account"], info.get("folder"), name, info.get("url"), progress)
    else:
        out = from_url(info["url"], name, progress)
    pdir, spec, msg = out
    return pdir, spec, f"{msg} ({time.time() - t0:.0f}s) Type: {describe(spec)}."


def describe(spec):
    t = spec.get("type")
    d = {"static": "static website (HTML/CSS/JS)", "flask": "Python Flask web app", "php": "PHP website",
         "node-build": f"{spec.get('framework') or 'JavaScript'} app (needs a build)",
         "node-server": f"Node.js server app ({spec.get('framework')})", "other": "project"}.get(t, t)
    if spec.get("framework") == "wordpress":
        d = "WordPress site"
    if spec.get("mirror"):
        d += ", saved copy of the live pages"
    return d
