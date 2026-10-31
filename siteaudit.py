"""Deep website check - like a professional audit, for a live site (https://...) or a site folder on this PC.

Opens every page in a real (hidden) Chrome and checks:
  Errors        - pages that fail (404/500), JavaScript errors, console errors, files that don't load
  Links         - broken links inside the site and to other sites
  Mobile        - phone layout (content wider than the screen, tiny text, small buttons, no viewport tag)
  SEO           - titles, descriptions, headings, duplicate titles, robots.txt, sitemap
  Accessibility - missing alt text, form fields without labels, buttons without names, low contrast text
  Speed         - load time, page weight, huge images, too many files, scripts that block the page
  Security      - HTTPS, mixed content, security headers, certificate expiry, exposed private files
  Code          - Python / PHP / JavaScript / JSON syntax in the source files
  Design        - the picture brain looks at desktop and phone screenshots
Each problem gets a severity, the file most likely responsible, and whether Kakarot can fix it itself.
"""
import concurrent.futures as cf
import datetime
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import time
from urllib.parse import urljoin, urlparse, urldefrag

import requests

import design
import siteproject as sp
from config import BASE, CFG
from tools import UA

AREAS = ["Errors", "Links", "Mobile", "SEO", "Accessibility", "Speed", "Security", "Code", "Design"]
WEIGHT = {"high": 18, "medium": 7, "low": 2}
ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}
# problems that are different for each title (each broken link is its own problem); others are merged per file
BY_TITLE = {"custom", "design", "code", "js_error", "console", "broken_link", "failed_load", "dup_title", "big_file",
            "oversized_img", "broken_img", "exposed", "page_status", "load_fail", "cert", "cert_bad", "slow", "heavy"}

PAGE_JS = r"""() => {
  const r = {};
  const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)];
  const short = e => e.outerHTML.replace(/\s+/g, ' ').slice(0, 140);
  r.title = document.title || '';
  r.lang = document.documentElement.getAttribute('lang') || '';
  const md = q('meta[name="description"]'); r.desc = md ? (md.getAttribute('content') || '') : null;
  r.viewport = !!q('meta[name="viewport"]');
  r.h1 = qa('h1').map(h => (h.innerText || '').trim()).slice(0, 5);
  r.canonical = !!q('link[rel="canonical"]');
  r.og = !!q('meta[property="og:title"]');
  r.icon = !!q('link[rel~="icon"]');
  r.charset = !!q('meta[charset]') || !!q('meta[http-equiv="Content-Type"]');
  const imgs = [...document.images];
  r.imgNoAlt = imgs.filter(i => !i.hasAttribute('alt')).map(i => i.currentSrc || i.src).slice(0, 12);
  r.brokenImg = imgs.filter(i => i.complete && i.naturalWidth === 0 && (i.currentSrc || i.src) && !(i.currentSrc || i.src).startsWith('data:')).map(i => i.currentSrc || i.src).slice(0, 12);
  r.bigImg = imgs.filter(i => i.naturalWidth > 1400 && i.naturalWidth > 2.5 * Math.max(i.clientWidth, 1)).map(i => ({src: i.currentSrc || i.src, nat: i.naturalWidth, shown: i.clientWidth})).slice(0, 8);
  r.noLazy = imgs.filter(i => i.getBoundingClientRect().top > window.innerHeight * 2 && i.loading !== 'lazy').length;
  r.unlabeled = qa('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]),select,textarea')
    .filter(e => !(e.labels && e.labels.length) && !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby') && !e.title && !(e.placeholder && e.type !== 'text' && false))
    .map(short).slice(0, 8);
  r.noName = qa('a[href],button').filter(e => {
    if ((e.innerText || '').trim() || e.getAttribute('aria-label') || e.getAttribute('aria-labelledby') || e.title) return false;
    if (e.querySelector('img[alt]:not([alt=""]),svg title,[aria-label]')) return false;
    const st = getComputedStyle(e); return st.display !== 'none' && st.visibility !== 'hidden';
  }).map(short).slice(0, 8);
  const ids = {}; qa('[id]').forEach(e => ids[e.id] = (ids[e.id] || 0) + 1);
  r.dupIds = Object.keys(ids).filter(k => ids[k] > 1).slice(0, 10);
  const parse = c => { const m = (c || '').match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(',').map(x => parseFloat(x)); return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1}; };
  const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
  const bgOf = e => { let n = e; while (n && n.nodeType === 1) { const st = getComputedStyle(n); if (st.backgroundImage && st.backgroundImage !== 'none') return null; const c = parse(st.backgroundColor); if (c && c.a > 0.9) return c; n = n.parentElement; } return {r: 255, g: 255, b: 255, a: 1}; };
  const low = []; let checked = 0;
  for (const e of qa('p,li,a,span,td,th,label,button,h1,h2,h3,h4,h5,h6,small,strong,em,div')) {
    if (checked > 500 || low.length >= 8) break;
    const own = [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 2); if (!own) continue;
    const rc = e.getBoundingClientRect(); if (!rc.width || !rc.height) continue;
    const st = getComputedStyle(e); if (st.visibility === 'hidden' || parseFloat(st.opacity) < 0.3) continue;
    checked++;
    const fg = parse(st.color), bg = bgOf(e); if (!fg || !bg || fg.a < 0.5) continue;
    const L1 = lum(fg), L2 = lum(bg), ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
    const size = parseFloat(st.fontSize), bold = parseInt(st.fontWeight) >= 700;
    const need = (size >= 24 || (size >= 18.5 && bold)) ? 3 : 4.5;
    if (ratio < need) low.push({text: e.innerText.trim().slice(0, 50), ratio: Math.round(ratio * 100) / 100, color: st.color, bg: `rgb(${bg.r}, ${bg.g}, ${bg.b})`});
  }
  r.lowContrast = low;
  r.links = qa('a[href]').map(a => a.href).filter(h => /^https?:/i.test(h));
  r.forms = document.forms.length;
  r.textLen = ((document.body && document.body.innerText) || '').trim().length;
  const nav = performance.getEntriesByType('navigation')[0];
  r.timing = nav ? {ttfb: Math.round(nav.responseStart), dcl: Math.round(nav.domContentLoadedEventEnd), load: Math.round(nav.loadEventEnd)} : null;
  const res = performance.getEntriesByType('resource');
  r.requests = res.length + 1;
  r.bytes = res.reduce((s, e) => s + (e.transferSize || e.encodedBodySize || 0), 0) + (nav ? (nav.transferSize || nav.encodedBodySize || 0) : 0);
  r.bigRes = res.filter(e => (e.encodedBodySize || 0) > 350000).map(e => ({url: e.name, kb: Math.round(e.encodedBodySize / 1024), type: e.initiatorType})).slice(0, 8);
  r.blocking = qa('head script[src]:not([async]):not([defer]):not([type=module])').map(s => s.src).slice(0, 6);
  r.inlineHandlers = qa('[onclick],[onload],[onerror]').length;
  return r;
}"""

MOBILE_JS = r"""() => {
  const w = window.innerWidth, sw = document.documentElement.scrollWidth;
  const label = e => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (typeof e.className === 'string' && e.className.trim() ? '.' + e.className.trim().split(/\s+/).slice(0, 2).join('.') : '');
  let wide = [];
  if (sw > w + 2) {
    wide = [...document.querySelectorAll('body *')].filter(e => {
      const r = e.getBoundingClientRect(); if (r.right <= w + 2 || !r.width) return false;
      const st = getComputedStyle(e); if (st.position === 'fixed' || st.position === 'absolute') return false;
      const p = e.parentElement && e.parentElement.getBoundingClientRect(); return !p || p.right <= w + 2;
    }).slice(0, 5).map(e => label(e) + ' (' + Math.round(e.getBoundingClientRect().width) + 'px wide)');
  }
  const small = [...document.querySelectorAll('a[href],button,input:not([type=hidden]),select')].filter(e => {
    const r = e.getBoundingClientRect(); if (!r.width || !r.height) return false;
    const st = getComputedStyle(e); if (st.visibility === 'hidden' || st.display === 'inline' && e.closest('p,li')) return false;
    return r.height < 24 || r.width < 24;
  }).slice(0, 40).map(label);
  const tiny = [...document.querySelectorAll('p,li,a,span,td,label')].filter(e => (e.innerText || '').trim().length > 3 && parseFloat(getComputedStyle(e).fontSize) < 12).length;
  return {overflow: sw - w, wide, smallTargets: small.length, smallExamples: [...new Set(small)].slice(0, 5), tinyText: tiny};
}"""

LOOK_PROMPT = """You are a strict senior web designer reviewing a website. Screenshot 1 is the desktop view, screenshot 2 the
phone view of the home page. List up to 6 concrete VISUAL problems (broken/overlapping layout, unreadable or
low-contrast text, missing images, huge empty areas, unstyled elements, things cut off on the phone, cluttered or
outdated design). Rate the design 1-10. Reply ONLY JSON: {"score": number, "problems": ["..."]}"""


class Report:
    def __init__(self, target, pdir=None, spec=None):
        self.target, self.pdir, self.spec = target, pdir, spec or {}
        self.issues = {}
        self.pages = []
        self.facts = {}
        self.shots = []

    def add(self, area, sev, code, title, detail="", page=None, file=None, fix="ai", hint=""):
        key = (area, code, file or "", title if code in BY_TITLE else "")
        it = self.issues.get(key)
        if not it:
            it = self.issues[key] = {"area": area, "sev": sev, "code": code, "title": title, "detail": detail,
                                     "pages": [], "file": file, "fix": fix, "hint": hint, "count": 0}
        it["count"] += 1
        if page and page not in it["pages"] and len(it["pages"]) < 12:
            it["pages"].append(page)
        if detail and detail not in it["detail"] and len(it["detail"]) < 1500:
            it["detail"] = (it["detail"] + "\n" + detail).strip() if it["detail"] else detail
        return it

    def finish(self):
        issues = sorted(self.issues.values(), key=lambda i: (list(WEIGHT).index(i["sev"]), AREAS.index(i["area"])))
        for n, i in enumerate(issues, 1):
            i["id"] = n
        scores = {}
        for a in AREAS:
            pen = sum(WEIGHT[i["sev"]] * min(1 + (i["count"] - 1) * 0.15, 2) for i in issues if i["area"] == a)
            scores[a] = max(0, round(100 - pen))
        if not self.facts.get("vision") and not self.facts.get("taste"):
            scores.pop("Design", None)
        if not self.facts.get("code_checked"):
            scores.pop("Code", None)
        w = {"Errors": 3, "Links": 2, "Mobile": 2, "Security": 2, "Speed": 1.5, "SEO": 1.5, "Accessibility": 1.5,
             "Code": 2, "Design": 1}
        total = round(sum(scores[a] * w[a] for a in scores) / sum(w[a] for a in scores)) if scores else 0
        if any(i["sev"] == "high" and i["area"] == "Errors" for i in issues):
            total = min(total, 69)
        counts = {s: sum(1 for i in issues if i["sev"] == s) for s in WEIGHT}
        return {"target": self.target, "at": sp.now(), "score": total, "scores": scores, "counts": counts,
                "pages": self.pages, "issues": issues, "facts": self.facts, "shots": self.shots}


# ---------------------------------------------------------------- helpers
def _same_site(u, base):
    a, b = urlparse(u), urlparse(base)
    return a.scheme in ("http", "https") and a.netloc.lower().removeprefix("www.") == b.netloc.lower().removeprefix("www.")


def _norm(u):
    """One address per page: no #part, and /index.html == / (so a page isn't checked twice)."""
    u = urldefrag(u)[0]
    return re.sub(r"/index\.(html?|php)$", "/", u)


def _skip_crawl(u):
    return bool(re.search(r"\.(pdf|zip|rar|7z|jpe?g|png|gif|webp|svg|mp4|mp3|docx?|xlsx?|pptx?|csv|exe|apk)(\?|$)", u, re.I)
                or re.search(r"logout|log-out|signout|delete|remove|/wp-admin|/cart/add|add-to-cart", u, re.I))


def _show(u, base):
    """How an address appears in a problem: just the path for the site's own pages (the test server's port
    changes every run), the full address for other sites."""
    if _same_site(u, base):
        p = urlparse(u)
        bpath = urlparse(base).path
        path = p.path
        if bpath not in ("", "/") and path.startswith(bpath):
            path = "/" + path[len(bpath):]
        return path + (("?" + p.query) if p.query else "")
    return u


def _owner(pdir, spec, url, base):
    """Source file for a URL of the site being tested (None for live-only checks / unknown)."""
    if not pdir or not _same_site(url, base):
        return None
    path = urlparse(url).path
    bpath = urlparse(base).path
    if bpath and bpath != "/" and path.startswith(bpath):
        path = "/" + path[len(bpath):]
    f = sp.url_to_file(pdir, spec, path)
    if f and spec.get("type") == "node-build":
        # built files are made by 'npm run build': point at the source file instead (or let the brain find it)
        bd = (spec.get("build_dir") or "dist").strip("/") + "/"
        if f.startswith(bd):
            tail = f[len(bd):]
            for pre in ("", "public/", "src/", "static/"):
                if os.path.isfile(os.path.join(pdir, *(pre + tail).split("/"))):
                    return pre + tail
            return None
    return f


def _script_owner(pdir, spec, text, base, fallback):
    for u in re.findall(r"https?://[^\s'\"()<>]+?\.(?:js|mjs)\b", text or ""):
        f = _owner(pdir, spec, u, base)
        if f:
            return f
    return fallback


def _check_url(u, timeout=15):
    """Status of a link: (status or None, error text)."""
    try:
        r = requests.head(u, headers=UA, timeout=timeout, allow_redirects=True)
        if r.status_code in (403, 405, 400, 501) or r.status_code >= 500:
            r = requests.get(u, headers=UA, timeout=timeout, allow_redirects=True, stream=True)
            r.close()
        return r.status_code, ""
    except requests.exceptions.SSLError:
        return None, "security certificate error"
    except requests.exceptions.ConnectionError:
        return None, "the site can't be reached"
    except requests.exceptions.Timeout:
        return None, "no answer (timeout)"
    except requests.RequestException as e:
        return None, str(e)[:120]


def cert_days(host, port=443):
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=10) as s, ctx.wrap_socket(s, server_hostname=host) as t:
        exp = ssl.cert_time_to_seconds(t.getpeercert()["notAfter"])
    return int((exp - time.time()) // 86400)


# ---------------------------------------------------------------- browser part
def _browse(rep, base, max_pages, shots_dir, progress):
    # a site Kakarot built gets the design rules applied when fixing; someone's existing site only gets design
    # suggestions (fixed only when the user asks to improve the look) - its look is theirs.
    taste_fix = "ai" if ((rep.spec or {}).get("origin") or {}).get("type") == "built" else "suggest"
    from playwright.sync_api import sync_playwright
    pdir, spec = rep.pdir, rep.spec
    queue, seen, titles, all_links = [_norm(base)], set(), {}, {}
    secure = base.startswith("https://")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1366, "height": 900}, user_agent=UA["User-Agent"],
                                  ignore_https_errors=False)
        try:
            while queue and len(rep.pages) < max_pages:
                url = _norm(queue.pop(0))
                if url in seen:
                    continue
                seen.add(url)
                if progress:
                    progress(f"Checking page {len(rep.pages) + 1}: {urlparse(url).path or '/'}")
                page = ctx.new_page()
                owner = _owner(pdir, spec, url, base)
                pname = urlparse(url).path or "/"
                failed, events = [], []

                def on_error(e, pname=pname, owner=owner):
                    stack = getattr(e, "stack", "") or str(e)
                    f = _script_owner(pdir, spec, stack, base, owner)
                    rep.add("Errors", "high", "js_error", f"JavaScript error: {str(e)[:160]}", stack[:700], pname, f,
                            hint="A script crashes, so buttons/menus/forms on this page may not work.")

                def on_console(m, pname=pname, owner=owner):
                    if m.type != "error" or re.search(r"favicon|picsum|google-analytics|googletagmanager|"
                                                      r"facebook|doubleclick|Failed to load resource", m.text, re.I):
                        return
                    loc = (m.location or {}).get("url", "")
                    rep.add("Errors", "medium", "console", f"Console error: {m.text[:160]}", m.text[:600], pname,
                            _script_owner(pdir, spec, loc, base, owner))

                def on_response(r, url=url):
                    try:
                        if r.status >= 400 and r.url != url and r.request.resource_type in (
                                "stylesheet", "script", "image", "font", "media", "fetch", "xhr"):
                            failed.append((r.url, r.status, r.request.resource_type))
                    except Exception:
                        pass

                def on_failed(rq):
                    try:
                        err = rq.failure or ""
                        if "ERR_ABORTED" in err:
                            return
                        failed.append((rq.url, err, rq.resource_type))
                    except Exception:
                        pass

                def on_request(rq):
                    if secure and rq.url.startswith("http://") and not re.match(r"http://(127\.0\.0\.1|localhost)", rq.url):
                        events.append(rq.url)

                page.on("pageerror", on_error)
                page.on("console", on_console)
                page.on("response", on_response)
                page.on("requestfailed", on_failed)
                page.on("request", on_request)
                info = {"url": url, "path": pname, "file": owner}
                try:
                    t0 = time.time()
                    resp = page.goto(url, wait_until="load", timeout=45000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:
                        pass
                    info["seconds"] = round(time.time() - t0, 1)
                    status = resp.status if resp else 0
                    info["status"] = status
                    final = page.url
                    if status >= 400:
                        refs = sorted(all_links.get(url, []))
                        if status in (404, 410) and refs:
                            ref_file = _owner(pdir, spec, urljoin(base, refs[0].lstrip("/")), base)
                            it = rep.add("Links", "high", "broken_link", f"Broken link: {_show(url, base)[:90]} ({status})", url,
                                         refs[0], ref_file, hint="Visitors who click it get an error. Create the "
                                                                 "missing page or fix the link.")
                            it["pages"] = refs[:12]
                        else:
                            rep.add("Errors", "high", "page_status", f"Page answers HTTP {status}", f"{url} → {status}",
                                    pname, owner, hint="Visitors see an error page instead of the content.")
                        info["status"] = status
                        rep.pages.append(info)
                        page.close()
                        continue
                    if not _same_site(final, base):
                        info["left_site"] = final
                        rep.pages.append(info)
                        page.close()
                        continue
                    if "text/html" not in ((resp.headers or {}).get("content-type", "text/html") if resp else ""):
                        page.close()
                        continue
                    d = page.evaluate(PAGE_JS)
                    try:  # the "made by AI" look: measured, not guessed (see design.py)
                        design.check(rep, page.evaluate(design.TASTE_JS), pname, owner, taste_fix)
                        rep.facts["taste"] = True
                    except Exception as e:
                        rep.facts["taste_error"] = str(e)[:200]
                    info.update(title=d["title"], requests=d["requests"], kb=round(d["bytes"] / 1024),
                                load_ms=(d.get("timing") or {}).get("load"))
                    # ---- SEO
                    if not d["title"].strip():
                        rep.add("SEO", "high", "no_title", "Page has no <title>", page=pname, file=owner,
                                hint="Google shows the title in search results and the browser tab.")
                    elif len(d["title"]) > 65:
                        rep.add("SEO", "low", "long_title", "Title is longer than 65 characters", d["title"], pname, owner)
                    elif len(d["title"]) < 10:
                        rep.add("SEO", "low", "short_title", "Title is very short", d["title"], pname, owner)
                    titles.setdefault(d["title"].strip(), []).append(pname)
                    if d["desc"] is None or not d["desc"].strip():
                        rep.add("SEO", "medium", "no_desc", "No meta description", page=pname, file=owner,
                                hint="The description is the text Google shows under your title.")
                    elif len(d["desc"]) > 170:
                        rep.add("SEO", "low", "long_desc", "Meta description is longer than 170 characters",
                                page=pname, file=owner)
                    if not d["h1"]:
                        rep.add("SEO", "medium", "no_h1", "No main heading (<h1>)", page=pname, file=owner)
                    elif len(d["h1"]) > 1:
                        rep.add("SEO", "low", "many_h1", f"{len(d['h1'])} main headings (<h1>) - use one per page",
                                ", ".join(d["h1"][:3]), pname, owner)
                    if not d["lang"]:
                        rep.add("SEO", "low", "no_lang", "Page language not set (<html lang=...>)", page=pname,
                                file=owner, fix="auto")
                    if not d["icon"] and len(rep.pages) == 0:
                        rep.add("SEO", "low", "no_icon", "No favicon (the small icon in the browser tab)",
                                page=pname, file=owner)
                    if not d["og"] and len(rep.pages) == 0:
                        rep.add("SEO", "low", "no_og", "No social sharing preview (Open Graph tags)",
                                hint="Links shared on Facebook/WhatsApp look plain.", page=pname, file=owner)
                    if not d["charset"]:
                        rep.add("Code", "low", "no_charset", "No <meta charset> - Bangla/special letters may break",
                                page=pname, file=owner)
                    # ---- accessibility
                    if d["imgNoAlt"]:
                        rep.add("Accessibility", "medium", "img_alt", f"Images without alt text",
                                "\n".join(d["imgNoAlt"][:6]), pname, owner,
                                hint="Alt text is read aloud to blind visitors and helps Google Images.")
                    if d["unlabeled"]:
                        rep.add("Accessibility", "medium", "no_label", "Form fields without a label",
                                "\n".join(d["unlabeled"][:5]), pname, owner)
                    if d["noName"]:
                        rep.add("Accessibility", "medium", "no_name", "Buttons/links with no text or label",
                                "\n".join(d["noName"][:5]), pname, owner,
                                hint="Screen readers announce them as just 'button'/'link'.")
                    if d["lowContrast"]:
                        ex = "; ".join(f"\"{x['text']}\" ({x['ratio']}:1, {x['color']} on {x['bg']})"
                                       for x in d["lowContrast"][:4])
                        rep.add("Accessibility", "medium", "contrast", "Text with low contrast (hard to read)", ex,
                                pname, None, hint="Needs at least 4.5:1 for normal text.")
                    if d["dupIds"]:
                        rep.add("Code", "low", "dup_ids", "Duplicate element ids", ", ".join(d["dupIds"][:8]), pname,
                                owner, hint="JavaScript and labels can pick the wrong element.")
                    # ---- speed
                    lm = (d.get("timing") or {}).get("load") or 0
                    if lm > 6000:
                        rep.add("Speed", "high", "slow", f"Page takes {lm / 1000:.1f}s to load", page=pname, file=owner,
                                fix="manual", hint="Comes from big files below and the hosting's speed.")
                    elif lm > 3000:
                        rep.add("Speed", "medium", "slow", f"Page takes {lm / 1000:.1f}s to load", page=pname, file=owner,
                                fix="manual", hint="Comes from big files below and the hosting's speed.")
                    if d["bytes"] > 5_000_000:
                        rep.add("Speed", "high", "heavy", f"Page is {d['bytes'] / 1e6:.1f} MB", page=pname, file=owner,
                                fix="manual", hint="Mostly images: compressing them fixes this.")
                    elif d["bytes"] > 2_500_000:
                        rep.add("Speed", "medium", "heavy", f"Page is {d['bytes'] / 1e6:.1f} MB", page=pname, file=owner,
                                fix="manual", hint="Mostly images: compressing them fixes this.")
                    for x in d["bigRes"]:
                        f = _owner(pdir, spec, x["url"], base)
                        rep.add("Speed", "medium" if x["kb"] < 1200 else "high", "big_file",
                                f"Large {x['type'] or 'file'}: {x['url'].split('/')[-1][:60]} ({x['kb']} KB)",
                                x["url"], pname, f, fix="auto" if f and x["type"] in ("img", "css") and
                                re.search(r"\.(jpe?g|png|webp)$", f, re.I) else "manual",
                                hint="Compress or resize it.")
                    for x in d["bigImg"]:
                        f = _owner(pdir, spec, x["src"], base)
                        rep.add("Speed", "low", "oversized_img",
                                f"Image is {x['nat']}px wide but shown at {x['shown']}px: {x['src'].split('/')[-1][:50]}",
                                x["src"], pname, f, fix="auto" if f else "manual")
                    if d["requests"] > 90:
                        rep.add("Speed", "low", "many_requests", f"{d['requests']} files loaded for one page",
                                page=pname, file=owner)
                    if len(d["blocking"]) > 2:
                        rep.add("Speed", "low", "blocking", "Scripts in <head> block the page from showing",
                                "\n".join(d["blocking"]), pname, owner, hint="Add 'defer' to those script tags.")
                    if d["noLazy"] > 3:
                        rep.add("Speed", "low", "lazy", f"{d['noLazy']} images far down the page load immediately",
                                page=pname, file=owner, hint="Add loading=\"lazy\" to them.")
                    if d["textLen"] < 40:
                        rep.add("Errors", "medium", "empty", "Page shows almost no text (broken or empty?)",
                                page=pname, file=owner)
                    # ---- broken things loaded by the page
                    for fu, st, kind in failed[:15]:
                        if re.search(r"google-analytics|googletagmanager|doubleclick|facebook\.net|hotjar", fu):
                            continue
                        f = _owner(pdir, spec, fu, base) if _same_site(fu, base) else None
                        rep.add("Errors", "high" if kind in ("stylesheet", "script") else "medium", "failed_load",
                                f"A {kind} doesn't load: {_show(fu.split('?')[0], base)[-80:]} ({st})", fu, pname, owner,
                                hint="The page links to a file that is missing or broken." +
                                     (f" (it should be {f})" if f else ""))
                    for bi in d["brokenImg"]:
                        rep.add("Errors", "medium", "broken_img", f"Broken image: {_show(bi.split('?')[0], base)[-70:]}", bi, pname, owner)
                    if events:
                        rep.add("Security", "medium", "mixed", "Insecure http:// files on an https page",
                                "\n".join(dict.fromkeys(events))[:600], pname, owner,
                                hint="Browsers block or warn about them. Change http:// to https://.")
                    # ---- phone view
                    if not d["viewport"]:
                        rep.add("Mobile", "high", "no_viewport", "No viewport tag - the page is tiny on phones",
                                page=pname, file=owner, fix="auto")
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.wait_for_timeout(450)
                    m = page.evaluate(MOBILE_JS)
                    if m["overflow"] > 8:
                        rep.add("Mobile", "high", "overflow",
                                f"On phones the page is {m['overflow']}px wider than the screen (sideways scrolling)",
                                "Too wide: " + ", ".join(m["wide"]), pname, owner)
                    if m["smallTargets"] > 6:
                        rep.add("Mobile", "low", "small_targets",
                                f"{m['smallTargets']} links/buttons are too small to tap easily",
                                ", ".join(m["smallExamples"]), pname, owner)
                    if m["tinyText"] > 10:
                        rep.add("Mobile", "medium", "tiny_text", f"{m['tinyText']} text blocks smaller than 12px on phones",
                                page=pname, file=owner)
                    if shots_dir and len(rep.pages) == 0:
                        os.makedirs(shots_dir, exist_ok=True)
                        ph = os.path.join(shots_dir, "phone.png")
                        page.screenshot(path=ph)
                        page.set_viewport_size({"width": 1366, "height": 900})
                        page.wait_for_timeout(400)
                        dk = os.path.join(shots_dir, "desktop.png")
                        page.screenshot(path=dk)
                        rep.shots = [dk, ph]
                    for link in d["links"]:
                        link = _norm(link)
                        all_links.setdefault(link, set()).add(pname)
                        if _same_site(link, base) and link not in seen and link not in queue and not _skip_crawl(link) \
                                and (urlparse(base).path in ("", "/") or urlparse(link).path.startswith(urlparse(base).path)):
                            queue.append(link)
                except Exception as e:
                    rep.add("Errors", "high", "load_fail", f"Page could not be opened: {str(e).splitlines()[0][:150]}",
                            str(e)[:500], pname, owner)
                rep.pages.append(info)
                page.close()
        finally:
            browser.close()
    # duplicate titles
    for t, pgs in titles.items():
        if t and len(pgs) > 1:
            it = rep.add("SEO", "medium", "dup_title", f"{len(pgs)} pages share the same title \"{t[:60]}\"", "",
                         pgs[0], None, hint="Each page should have its own title.")
            it["pages"] = pgs[:12]
    return all_links, seen


def _links(rep, all_links, seen, base, external=True, progress=None):
    """Check links that weren't opened as pages: other pages/files of the site, and external sites."""
    internal = [u for u in all_links if _same_site(u, base) and u not in seen]
    ext = [u for u in all_links if not _same_site(u, base)] if external else []
    ext = [u for u in ext if not re.search(r"facebook\.com|instagram\.com|linkedin\.com|twitter\.com|x\.com|"
                                            r"wa\.me|whatsapp|youtube\.com|tiktok\.com|maps\.google|goo\.gl", u)][:70]
    todo = internal[:150] + ext
    if not todo:
        return
    if progress:
        progress(f"Checking {len(todo)} links")
    with cf.ThreadPoolExecutor(8) as ex:
        results = dict(zip(todo, ex.map(_check_url, todo)))
    for u, (st, err) in results.items():
        if st in (401, 403, 429, 999) or (st and st < 400):
            continue
        pages = sorted(all_links[u])
        inside = _same_site(u, base)
        owner = _owner(rep.pdir, rep.spec, urljoin(base, pages[0]), base) if pages else None
        it = rep.add("Links", "high" if inside else "medium", "broken_link",
                f"Broken {'link' if inside else 'link to another site'}: {_show(u, base)[:90]} ({st or err})", u, pages[0] if pages else None,
                owner, fix="ai" if inside else "ai",
                hint="Visitors who click it get an error." + (" Create the missing page or fix the link." if inside else
                                                                " Update or remove the link."))
        it["pages"] = pages[:12]


def _site_checks(rep, base, live):
    """robots/sitemap/HTTPS/headers/certificate/exposed files (only meaningful for a live site)."""
    u = urlparse(base)
    root = f"{u.scheme}://{u.netloc}/"
    try:
        r = requests.get(base, headers=UA, timeout=30, allow_redirects=True)
    except requests.RequestException as e:
        rep.add("Errors", "high", "unreachable", f"The site can't be reached: {e.__class__.__name__}", str(e)[:300])
        return
    rep.facts["final_url"] = r.url
    rep.facts["server"] = r.headers.get("server", "")
    rep.facts["powered_by"] = r.headers.get("x-powered-by", "")
    if not live:
        return
    h = {k.lower(): v for k, v in r.headers.items()}
    if not r.url.startswith("https://"):
        rep.add("Security", "high", "no_https", "The site doesn't use HTTPS (the padlock)",
                hint="Browsers mark it 'Not secure'. Turn on SSL in your hosting (cPanel → SSL/TLS Status → AutoSSL).",
                fix="manual")
    else:
        try:
            http = requests.get("http://" + u.netloc + "/", headers=UA, timeout=20, allow_redirects=False)
            if http.status_code not in (301, 302, 307, 308) or not http.headers.get("location", "").startswith("https"):
                rep.add("Security", "medium", "no_redirect", "http:// does not redirect to https://",
                        hint="Visitors who type the address without https stay on the insecure version.", fix="auto")
        except requests.RequestException:
            pass
        try:
            days = cert_days(u.hostname, u.port or 443)
            rep.facts["cert_days"] = days
            if days < 7:
                rep.add("Security", "high", "cert", f"Security certificate expires in {days} days", fix="manual")
            elif days < 21:
                rep.add("Security", "medium", "cert", f"Security certificate expires in {days} days", fix="manual")
        except Exception as e:
            rep.add("Security", "high", "cert_bad", f"Security certificate problem: {str(e)[:120]}", fix="manual")
        if "strict-transport-security" not in h:
            rep.add("Security", "low", "hsts", "No HSTS header (forces HTTPS in browsers)", fix="manual")
    if "x-content-type-options" not in h:
        rep.add("Security", "low", "nosniff", "Missing X-Content-Type-Options header", fix="auto")
    if "x-frame-options" not in h and "frame-ancestors" not in h.get("content-security-policy", ""):
        rep.add("Security", "medium", "frames", "Missing X-Frame-Options (other sites can embed yours - clickjacking)",
                fix="auto")
    if "referrer-policy" not in h:
        rep.add("Security", "low", "referrer", "Missing Referrer-Policy header", fix="auto")
    if re.search(r"php/[0-5]\.|php/7\.[0-3]", h.get("x-powered-by", ""), re.I):
        rep.add("Security", "high", "old_php", f"Old PHP version: {h['x-powered-by']} (no security updates)",
                hint="Choose a newer PHP in cPanel → Select PHP Version.", fix="manual")
    for path, test in ((".git/HEAD", lambda t: t.startswith("ref:")),
                       (".env", lambda t: re.search(r"^[A-Z_]{3,}=", t, re.M) and "<html" not in t.lower())):
        try:
            x = requests.get(root + path, headers=UA, timeout=15, allow_redirects=False)
            if x.status_code == 200 and test(x.text[:2000]):
                rep.add("Security", "high", "exposed", f"Private file is public: /{path}",
                        hint="Anyone can download it (it can contain passwords/source code). Block or delete it.",
                        fix="auto")
        except requests.RequestException:
            pass
    for path, code, title in (("robots.txt", "robots", "No robots.txt"), ("sitemap.xml", "sitemap", "No sitemap.xml")):
        try:
            x = requests.get(root + path, headers=UA, timeout=15)
            if x.status_code != 200 or "<html" in x.text[:300].lower():
                rep.add("SEO", "low", code, title, hint="Helps Google find all your pages.", fix="ai")
        except requests.RequestException:
            pass


def _code_checks(rep, pdir, progress=None):
    """Syntax of the source files (catches errors before a visitor does)."""
    if progress:
        progress("Checking the code")
    php = shutil.which("php")
    node = shutil.which("node")
    n = 0
    for r in sp.files(pdir, limit=4000):
        fp = os.path.join(pdir, *r.split("/"))
        ext = os.path.splitext(r)[1].lower()
        try:
            if os.path.getsize(fp) > 1_500_000 or ".min." in r or "/vendor/" in "/" + r or "/node_modules/" in r:
                continue
        except OSError:
            continue
        err = syntax_error(fp, ext, php, node)
        if ext in (".py", ".json", ".php", ".js"):
            n += 1
        if err:
            rep.add("Code", "high", "code", f"Code error in {r}", err[:800], file=r,
                    hint="This file can't run, so the page/feature using it is broken.")
    rep.facts["code_checked"] = n
    rep.facts["php_available"] = bool(php)


def syntax_error(fp, ext=None, php=None, node=None):
    ext = ext or os.path.splitext(fp)[1].lower()
    try:
        if ext == ".py":
            compile(sp.read_text(fp), fp, "exec")
        elif ext == ".json":
            if os.path.basename(fp) not in ("tsconfig.json", "jsconfig.json"):
                json.loads(sp.read_text(fp))
        elif ext == ".php" and (php or shutil.which("php")):
            r = subprocess.run([php or shutil.which("php"), "-l", fp], capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return (r.stdout + r.stderr).strip()
        elif ext in (".js", ".cjs") and (node or shutil.which("node")):
            txt = sp.read_text(fp, 400000)
            if re.search(r"^\s*(import|export)\s", txt, re.M) or "<%" in txt or "{{" in txt[:3000]:
                return None
            r = subprocess.run([node or shutil.which("node"), "--check", fp], capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return r.stderr.strip()[-800:]
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"
    except ValueError as e:
        return f"invalid JSON: {e}"
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _look(rep, progress=None):
    if not rep.shots or not CFG.get("site_visual_check", True):
        return
    if progress:
        progress("Picture brain is looking at the design")
    try:
        import vision
        imgs = [vision.load_images(s)[0] for s in rep.shots]
        raw = vision.ask_vision(imgs, LOOK_PROMPT)
        m = re.search(r"\{.*\}", raw or "", re.S)
        rv = json.loads(m.group(0)) if m else {}
    except Exception as e:
        rep.facts["vision_error"] = str(e)[:200]
        return
    rep.facts["vision"] = {"score": rv.get("score"), "problems": rv.get("problems") or []}
    for p in (rv.get("problems") or [])[:6]:
        if isinstance(p, str) and p.strip():
            rep.add("Design", "medium", "design", p.strip()[:200], page="/", file=None)


# ---------------------------------------------------------------- entry points
def audit(base, pdir=None, spec=None, live=False, max_pages=None, shots_dir=None, progress=None, look=True,
          external_links=None):
    """Full check of a running site at `base`. pdir/spec = the source files (for code checks & file names)."""
    rep = Report(base, pdir, spec)
    max_pages = int(max_pages or CFG.get("site_check_pages", 20))
    rep.facts["live"] = live
    _site_checks(rep, base, live)
    if any(i["code"] == "unreachable" for i in rep.issues.values()):
        return rep.finish()
    links, seen = _browse(rep, base, max_pages, shots_dir, progress)
    ext = CFG.get("site_check_external_links", True) if external_links is None else external_links
    _links(rep, links, seen, base, external=ext, progress=progress)
    if pdir:
        _code_checks(rep, pdir, progress)
    if look:
        _look(rep, progress)
    return rep.finish()


def code_only(pdir, spec, reason, progress=None):
    """When the site can't run on this PC (e.g. PHP not installed): check the source files only."""
    rep = Report(f"files of {spec.get('name')}", pdir, spec)
    rep.facts["not_run"] = reason
    _code_checks(rep, pdir, progress)
    return rep.finish()


def quick(base, max_pages=5):
    """Fast after-publish check: do the pages open without errors?"""
    rep = Report(base)
    _browse(rep, base, max_pages, None, None)
    return rep.finish()


def text(rep, limit=40, pdir=None):
    """Readable report for the chat / Inbox."""
    lines = [f"Score: {rep['score']}/100  ({rep['counts']['high']} serious, {rep['counts']['medium']} medium, "
             f"{rep['counts']['low']} small problems) · {len(rep['pages'])} page(s) checked"]
    if rep["scores"]:
        lines.append(" · ".join(f"{a} {s}" for a, s in rep["scores"].items()))
    f = rep.get("facts", {})
    if f.get("not_run"):
        lines.append(f"⚠️ {f['not_run']}")
    if f.get("cert_days") is not None:
        lines.append(f"Certificate valid {f['cert_days']} more days.")
    if not rep["issues"]:
        lines.append("✅ No problems found.")
    for i in rep["issues"][:limit]:
        where = (f" [{i['file']}]" if i.get("file") else "") + (f" on {', '.join(i['pages'][:3])}" if i["pages"] else "")
        tag = {"ai": "", "auto": " (auto-fix)", "manual": " (needs you)"}.get(i["fix"], "")
        lines.append(f"{ICON[i['sev']]} #{i['id']} {i['area']}: {i['title']}{where}{tag}")
    if len(rep["issues"]) > limit:
        lines.append(f"...and {len(rep['issues']) - limit} more (see the full report in the Websites tab).")
    return "\n".join(lines)


def save(rep, pdir=None, key=None):
    """Keep the report: in the project's .kakarot/reports, or data/reports for sites without a folder."""
    d = sp.meta_dir(pdir, "reports") if pdir else os.path.join(BASE, "data", "reports")
    os.makedirs(d, exist_ok=True)
    name = (key or "report") + "-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=1)
    return p


def latest(pdir):
    d = os.path.join(pdir, sp.META, "reports")
    if not os.path.isdir(d):
        return None
    fs = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    if not fs:
        return None
    with open(os.path.join(d, fs[-1]), encoding="utf-8") as fh:
        return json.load(fh)


def can_fix(issue, focus=""):
    """Should the automatic fixer work on this issue? Design suggestions only when the user asked about the look."""
    if issue["fix"] == "manual":
        return False
    if issue["fix"] == "suggest":
        return design.wants_design(focus)
    return True


def serious_keys(rep):
    """Identity of the problems that really break a site (used to spot regressions after a change)."""
    return {(i["code"], i.get("file") or "", re.sub(r"https?://(127\.0\.0\.1|localhost):\d+", "", i["title"])[:80])
            for i in rep["issues"] if i["sev"] == "high" and i["area"] in ("Errors", "Links", "Code", "Mobile")}
