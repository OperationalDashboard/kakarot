"""Kakarot's design brain.

Distilled from three open-source design skills (see design_skills/CREDITS.md for the full originals and licences):
- Taste Skill by Leonxlnx (MIT) - art direction from a study of 247 real websites, house rules, real photos.
- Impeccable by Paul Bakaus (Apache 2.0) - design vocabulary, the "craft floor", AI-look tells to refuse.
- Emil Kowalski's design-engineering skills (MIT) - motion, easing, timing and the small details.

What lives here:
- RULES / RULES_SHORT: the rules the coding brain follows when it builds or fixes a site.
- TASTE_JS + check(): measures a page in a real browser and flags the "made by AI" look.
- find_photos(): real, credited photographs from open collections (Openverse) instead of placeholders.
- figma_*(): reads a Figma design (with your Figma token) so a site can be built from it.
"""
import io
import json
import logging
import os
import re
import urllib.parse

import requests

from config import CFG

log = logging.getLogger("kakarot.design")

# ---------------------------------------------------------------- the rules
RULES = """DESIGN RULES (follow all of them):
Idea first
- Build the page from things that belong ONLY to this client: its name and what it means, its street/city, its real
  numbers (prices, hours, seats, years), its materials, rituals and printed things (menu, ticket, label). The
  competitor test: if a rival business could use the same headline or section, rewrite it.
- Give the page ONE live thing the visitor can do with real content (pick a dish and see the price, set an amount and
  read the real monthly cost, see if the shop is open right now). Works on a phone, has a no-script fallback.
- Write the full content: the whole menu with prices, every room/service with details. Never invent reviews, client
  logos, press quotes or statistics.
Type
- One typeface (two at most). Two or three sizes do the work: one very large headline, small body, little between.
- Headlines: 2 lines or fewer at every width (about 32 characters), tracking -0.02em to -0.04em, line-height ~1.0.
- Body text 16-20px, line-height 1.5, 45-75 characters per line, in the full text colour (not grey). Left-aligned.
- First screen: one <h1> that says plainly what this is, one supporting sentence right under it, one main button.
Colour
- One background, one ink, one accent. The accent only on the main action and maybe one other thing.
- Separate surfaces with a small step in background tone, not lines or shadows.
- No violet/indigo "AI palette", no cream + terracotta default, no colours that fight the subject.
Layout
- One grid, equal outer margins (16-32px), sections 120-240px apart on desktop. Neighbouring sections must not
  share the same layout. No half-empty screens; no section huddled on one side with nothing opposite.
- Big things big: one dominant picture beats six small ones; hero media can fill half the first screen.
- Navigation: name/logo, 3-5 plain links, one action button. Footer is a designed close, not a sitemap.
Pictures
- Use real photographs (the client's own, or real credited photos of its world). At least 4 real pictures on a page.
  Same crop ratios and treatment; square corners unless the style says otherwise. Never a fake product photo.
Never use (they make a site look AI-made)
- Small uppercase labels/pills/badges above headings ("eyebrows"); 01/02/03 numbering; tiny letter-spaced caps.
- Rows of identical cards with an icon on top; nested cards; big-number "stats" strips with invented numbers.
- Gradients (backgrounds, buttons, text), glass/blur panels, glows, decorative blobs, shadows on things at rest.
- Emoji or unicode symbols as icons (use real SVG icons in one stroke style), monospace text outside code.
- A styled accent word inside a headline (one word in another colour/italic/serif).
Motion (subtle, fast, purposeful)
- Only animate transform and opacity. UI transitions 150-250ms, never over 300ms for controls.
- Ease-out for things entering/responding: cubic-bezier(0.23, 1, 0.32, 1). Never ease-in for UI. Never
  `transition: all` - name the properties.
- Buttons: transform: scale(0.97) on :active (160ms). Never animate from scale(0) - start at scale(0.95) + opacity 0.
- Sections enter once as they scroll in: heading, then text, then picture, 60-90ms apart, rising 16-24px.
  Content is visible if the script fails. Always add @media (prefers-reduced-motion: reduce) that removes movement.
Finish
- Hover, focus-visible, active, disabled, loading, empty and error states. Theme ::selection and focus rings from
  the palette. Real <a>/<button> elements, alt text, labels, contrast 4.5:1 for text."""

RULES_SHORT = """DESIGN RULES: one typeface (max two); a huge clear <h1> (max 2 lines) + one sentence + one button on the first
screen; body 16-20px in full ink colour, left-aligned; one background, one ink, one accent colour; equal margins,
120-240px between sections, neighbouring sections laid out differently; real photos, big; content specific to THIS
business (its name, place, real prices/hours). NEVER: eyebrow labels/pills above headings, 01/02 numbering, tiny
letter-spaced caps, identical icon cards, gradients, glass/blur, glows, shadows at rest, emoji icons, monospace
outside code, violet/indigo palette, an accent-coloured word in a headline, invented reviews/stats. Motion: only
transform+opacity, 150-250ms, ease-out cubic-bezier(0.23,1,0.32,1), never `transition: all`, never scale(0),
buttons scale(0.97) on :active, staggered section entrances, prefers-reduced-motion fallback."""

DESIGN_WORDS = re.compile(r"design|look|style|beaut|modern|taste|polish|pretty|ugly|animation|animate|motion|"
                          r"redesign|professional|premium|award|ai.?look|নকশা|ডিজাইন", re.I)


def wants_design(text):
    return bool(DESIGN_WORDS.search(text or ""))


# ---------------------------------------------------------------- measuring the "AI look" in a real browser
TASTE_JS = r"""() => {
  const qa = s => [...document.querySelectorAll(s)];
  const vis = e => { const r = e.getBoundingClientRect(); if (!r.width || !r.height) return false;
    const st = getComputedStyle(e); return st.visibility !== 'hidden' && st.display !== 'none' && parseFloat(st.opacity) > 0.05; };
  const txt = e => (e.innerText || '').trim();
  const lab = e => (e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (typeof e.className === 'string' && e.className.trim() ? '.' + e.className.trim().split(/\s+/)[0] : '')) + (txt(e) ? ' "' + txt(e).slice(0, 40) + '"' : '');
  const out = {}, vw = innerWidth, vh = innerHeight, area = vw * vh;
  const all = qa('body *').filter(vis).slice(0, 4000);
  // eyebrows: small label right above a heading
  out.eyebrows = qa('h1,h2,h3').filter(vis).map(h => {
    let p = h.previousElementSibling; if (!p || !vis(p) || /^H[1-6]$/.test(p.tagName) || p.querySelector('img,svg,picture')) return null;
    const t = txt(p), st = getComputedStyle(p);
    if (!t || t.length > 40 || t.split(/\s+/).length > 6) return null;
    const small = parseFloat(st.fontSize) <= 15, caps = st.textTransform === 'uppercase' || (t === t.toUpperCase() && /[A-Z]/.test(t));
    const pill = parseFloat(st.borderRadius) >= 10 && st.backgroundColor !== 'rgba(0, 0, 0, 0)';
    const sp = parseFloat(st.letterSpacing) || 0;
    return (small && (caps || pill || sp > 0.5)) ? lab(p) : null; }).filter(Boolean).slice(0, 6);
  // gradients
  const grads = [], gtext = [];
  for (const e of all) { const st = getComputedStyle(e), bi = st.backgroundImage || '';
    if (!/gradient\(/.test(bi)) continue;
    const r = e.getBoundingClientRect();
    if ((st.webkitBackgroundClip || st.backgroundClip) === 'text' || st.webkitTextFillColor === 'rgba(0, 0, 0, 0)') { gtext.push(lab(e)); continue; }
    if (/url\(/.test(bi)) continue;                       // gradient scrim over a photo is fine
    if (e.querySelector('img,video,picture') && r.width * r.height > area * 0.2) continue;
    if (r.width * r.height > area * 0.01 || e.matches('a,button,[role=button]')) grads.push(lab(e)); }
  out.gradients = grads.slice(0, 6); out.gradientText = gtext.slice(0, 4);
  // glass
  out.glass = all.filter(e => /blur\(/.test(getComputedStyle(e).backdropFilter || getComputedStyle(e).webkitBackdropFilter || '')).map(lab).slice(0, 4);
  // shadows on things at rest (menus/dialogs/sticky bars excluded)
  out.shadows = all.filter(e => { const st = getComputedStyle(e); if (!st.boxShadow || st.boxShadow === 'none') return false;
    if (/^(fixed|sticky)$/.test(st.position) || e.closest('dialog,[role=dialog],[role=menu],nav,header')) return false;
    const r = e.getBoundingClientRect(); return r.width > 60 && r.height > 30; }).map(lab);
  out.shadowCount = out.shadows.length; out.shadows = out.shadows.slice(0, 5);
  // emoji used as icons
  const emo = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B50}\u{2705}\u{2714}\u{2728}]/u;
  out.emoji = qa('h1,h2,h3,h4,button,a,li,span,div,i,p').filter(e => vis(e) && [...e.childNodes].some(n => n.nodeType === 3 && emo.test(n.textContent))).map(lab).slice(0, 6);
  // monospace outside code
  out.mono = all.filter(e => { if (e.closest('pre,code,kbd,samp,textarea,input')) return false;
    if (![...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 1)) return false;
    return /mono|courier|consolas|menlo|monaco/i.test(getComputedStyle(e).fontFamily); }).map(lab).slice(0, 5);
  // index numbering 01 / 02 / 03
  out.indexNums = all.filter(e => e.children.length === 0 && /^\(?0\d\)?[.\s/—-]*$/.test(txt(e))).map(lab).slice(0, 6);
  // tiny tracked caps
  out.tinyCaps = all.filter(e => { const st = getComputedStyle(e);
    return st.textTransform === 'uppercase' && parseFloat(st.fontSize) <= 13 && (parseFloat(st.letterSpacing) || 0) >= 0.6 &&
      [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 2); }).map(lab);
  out.tinyCapsCount = out.tinyCaps.length; out.tinyCaps = out.tinyCaps.slice(0, 5);
  // violet / indigo accent on actions
  const hue = c => { const m = (c || '').match(/rgba?\(([^)]+)\)/); if (!m) return null; let [r, g, b, a] = m[1].split(',').map(parseFloat);
    if (a !== undefined && a < 0.5) return null; r /= 255; g /= 255; b /= 255; const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    if (!d) return null; const l = (mx + mn) / 2, s = d / (1 - Math.abs(2 * l - 1)); let h;
    if (mx === r) h = ((g - b) / d) % 6; else if (mx === g) h = (b - r) / d + 2; else h = (r - g) / d + 4; h = (h * 60 + 360) % 360;
    return {h, s, l}; };
  out.violet = qa('a,button,[role=button],.btn,.button').filter(vis).filter(e => { const st = getComputedStyle(e);
    return [st.backgroundColor, st.color].some(c => { const x = hue(c); return x && x.h >= 238 && x.h <= 290 && x.s > 0.35 && x.l > 0.2 && x.l < 0.8; }); }).map(lab).slice(0, 4);
  // rows of identical icon-topped cards
  const cardRows = [];
  for (const p of all) { const kids = [...p.children].filter(vis); if (kids.length < 3 || kids.length > 8) continue;
    const sig = k => k.tagName + '|' + (typeof k.className === 'string' ? k.className.split(/\s+/)[0] : '');
    if (!kids.every(k => sig(k) === sig(kids[0]))) continue;
    const iconTop = kids.filter(k => { const f = k.firstElementChild; if (!f) return false;
      const fr = f.getBoundingClientRect(); const isIcon = f.matches('svg,i,img,span,div') && fr.width > 0 && fr.width <= 96 && fr.height <= 96 &&
        (f.matches('svg,i') || f.querySelector('svg') || emo.test(txt(f)) || (f.matches('img') && fr.width <= 72));
      return isIcon && k.querySelector('h2,h3,h4,strong') && k.querySelector('p'); }).length;
    if (iconTop >= 3 && iconTop === kids.length) cardRows.push(lab(p)); }
  out.iconCards = cardRows.slice(0, 4);
  // headings longer than two lines
  out.longHeads = qa('h1,h2').filter(vis).filter(h => { const st = getComputedStyle(h); let lh = parseFloat(st.lineHeight);
    if (!lh) lh = parseFloat(st.fontSize) * 1.2; return h.getBoundingClientRect().height / lh > 2.4 && parseFloat(st.fontSize) >= 28; }).map(lab).slice(0, 5);
  // accent word styled inside a headline
  out.accentWord = qa('h1,h2').filter(vis).filter(h => { const hs = getComputedStyle(h);
    return [...h.querySelectorAll('span,em,i,strong,mark,b')].some(s => { const ss = getComputedStyle(s); const t = txt(s);
      return t && t.length < txt(h).length * 0.7 && (ss.color !== hs.color || ss.fontFamily !== hs.fontFamily || /gradient/.test(ss.backgroundImage)); }); }).map(lab).slice(0, 4);
  // fonts actually used for text
  const fams = {}; for (const e of all) { if (![...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 2)) continue;
    if (e.closest('pre,code,kbd,samp')) continue; const f = getComputedStyle(e).fontFamily.split(',')[0].replace(/["']/g, '').trim().toLowerCase();
    fams[f] = (fams[f] || 0) + 1; }
  out.fonts = Object.keys(fams).filter(f => fams[f] >= 2 && !/icon|awesome|material/.test(f));
  // small body text on desktop
  out.smallBody = qa('p').filter(vis).filter(p => txt(p).length > 80 && parseFloat(getComputedStyle(p).fontSize) < 15.5).length;
  // placeholder / stock-random pictures and the real-picture count
  const imgs = qa('img').filter(vis);
  out.placeholders = [...new Set(imgs.map(i => i.currentSrc || i.src).filter(s => /picsum\.photos|placehold|placeholder\.com|dummyimage|source\.unsplash\.com\/random|loremflickr|fakeimg/i.test(s)))].slice(0, 6);
  let pics = imgs.filter(i => { const r = i.getBoundingClientRect(); return r.width * r.height > 40000 && !/\.svg(\?|$)/i.test(i.currentSrc || i.src); }).length;
  pics += qa('video').filter(vis).length;
  pics += all.filter(e => /url\(/.test(getComputedStyle(e).backgroundImage) && e.getBoundingClientRect().width * e.getBoundingClientRect().height > 60000).length;
  out.pictures = pics;
  // motion rules in the page's own stylesheets
  let css = '';
  for (const sh of [...document.styleSheets]) { try { for (const r of sh.cssRules) css += r.cssText + '\n'; } catch (e) {} }
  out.transitionAll = (css.match(/transition:\s*all\b|transition-property:\s*all\b/g) || []).length +
    all.filter(e => { const st = getComputedStyle(e); return /(^|,\s*)all\b/.test(st.transitionProperty) &&
      st.transitionDuration.split(',').some(t => parseFloat(t) > 0); }).length;
  out.scale0 = /scale\(0(\.0+)?\)|scale3d\(0,\s*0/.test(css);
  out.easeIn = (css.match(/transition[^;]*\bease-in\b(?!-out)/g) || []).length;
  out.animates = /@keyframes|transition/.test(css) || qa('[data-aos],[class*=animate],[class*=reveal],[class*=fade]').length > 0;
  out.reducedMotion = /prefers-reduced-motion/.test(css);
  out.activeState = /:active/.test(css);
  out.slowUi = (css.match(/(?:a|button|\.btn[\w-]*)[^{]*\{[^}]*transition[^;}]*\b(0?\.[4-9]\d*s|[1-9]\d*(?:\.\d+)?s|[4-9]\d\dms)/g) || []).length;
  out.focusVisible = /:focus-visible|:focus\b/.test(css);
  return out;
}"""


def check(rep, d, page, file, fix):
    """Turn TASTE_JS measurements into report issues (area Design). fix = 'ai' or 'suggest'."""
    def add(sev, code, title, detail="", instead=""):
        rep.add("Design", sev, "taste_" + code, title,
                (detail + ("\nInstead: " + instead if instead else "")).strip(), page, file, fix=fix, hint=instead)
    ex = lambda k: "\n".join(d.get(k) or [])
    if d.get("eyebrows"):
        add("low", "eyebrow", "Small labels above headings (the 'eyebrow' AI look)", ex("eyebrows"),
            "Delete the label; put that information in the heading or the first sentence.")
    if d.get("gradients"):
        add("medium", "gradient", "Gradient backgrounds or buttons", ex("gradients"),
            "Flat colour from the palette, or a real photograph.")
    if d.get("gradientText"):
        add("medium", "gradient_text", "Gradient-coloured text", ex("gradientText"),
            "Solid text colour; get emphasis from size or weight.")
    if d.get("glass"):
        add("low", "glass", "Frosted-glass (blur) panels", ex("glass"),
            "A solid surface one small step lighter or darker than the background.")
    if (d.get("shadowCount") or 0) >= 4:
        add("low", "shadows", f"{d['shadowCount']} boxes with drop shadows at rest", ex("shadows"),
            "Remove the shadows; separate surfaces with a small step in background tone. Shadows only on menus/dialogs.")
    if d.get("emoji"):
        add("medium", "emoji", "Emoji used as icons", ex("emoji"),
            "Real SVG icons in one consistent stroke style, or no icon.")
    if d.get("mono"):
        add("low", "mono", "Monospace text used as decoration", ex("mono"), "Use the page's text typeface.")
    if len(d.get("indexNums") or []) >= 2:
        add("low", "numbering", "01 / 02 / 03 section numbering", ex("indexNums"),
            "Show order by position or a real <ol>; remove the numbers.")
    if (d.get("tinyCapsCount") or 0) >= 3:
        add("low", "tiny_caps", f"{d['tinyCapsCount']} tiny letter-spaced UPPERCASE labels", ex("tinyCaps"),
            "Sentence case at a readable size (14px+).")
    if d.get("violet"):
        add("low", "violet", "Violet/indigo buttons (the default 'AI palette')", ex("violet"),
            "Pick an accent from the subject itself (its sign, material, place).")
    if d.get("iconCards"):
        add("medium", "icon_cards", "Row of identical cards with an icon on top (template look)", ex("iconCards"),
            "A different composition: a list with real pictures, one large feature with details, or a table.")
    if d.get("longHeads"):
        add("medium", "long_heads", "Headlines wrap to 3+ lines", ex("longHeads"),
            "Cut the headline to about 32 characters / 5 words so it fits in two lines.")
    if d.get("accentWord"):
        add("low", "accent_word", "One word in a headline styled differently", ex("accentWord"),
            "One colour and one face for the whole headline.")
    if len(d.get("fonts") or []) > 2:
        add("medium", "fonts", f"{len(d['fonts'])} different typefaces", ", ".join(d["fonts"][:6]),
            "One typeface (two at most) for the whole site.")
    if (d.get("smallBody") or 0) >= 2:
        add("low", "small_body", f"{d['smallBody']} paragraphs set smaller than 16px", "",
            "Body text 16-20px, line-height 1.5.")
    if d.get("placeholders"):
        add("medium", "placeholders", "Random placeholder photos (picsum/placeholder)", ex("placeholders"),
            "Real photos of the business or real credited photos of its world.")
    if d.get("pictures", 0) < 2 and not d.get("placeholders"):
        add("low", "few_pictures", f"Only {d.get('pictures', 0)} real picture(s) on the page", "",
            "Use 4+ real photographs, one of them large on the first screen.")
    if d.get("transitionAll"):
        add("low", "transition_all", "Animations use `transition: all`", "",
            "Name the properties: transition: transform 200ms cubic-bezier(0.23,1,0.32,1), opacity 200ms ...")
    if d.get("scale0"):
        add("low", "scale0", "Something animates from scale(0)", "", "Start from scale(0.95) with opacity 0.")
    if d.get("easeIn"):
        add("low", "ease_in", "ease-in used for interface animation (feels sluggish)", "",
            "ease-out: cubic-bezier(0.23, 1, 0.32, 1).")
    if d.get("slowUi"):
        add("low", "slow_ui", "Buttons/links animate slower than 300ms", "", "150-250ms for controls.")
    if d.get("animates") and not d.get("reducedMotion"):
        add("low", "reduced_motion", "No reduced-motion version of the animations", "",
            "Add @media (prefers-reduced-motion: reduce) that removes movement but keeps content.")
    if d.get("animates") and not d.get("activeState"):
        add("low", "no_active", "Buttons don't react when pressed", "",
            "button:active, .btn:active { transform: scale(0.97) } with transition: transform 160ms ease-out.")
    if not d.get("focusVisible"):
        add("low", "focus", "No visible keyboard focus style", "",
            ":focus-visible outline in the accent colour with an offset.")


# ---------------------------------------------------------------- real photos (Openverse: CC/public-domain, commercial use)
OPENVERSE = "https://api.openverse.org/v1/images/"


def find_photos(query, n=6):
    """Search open collections for real photographs that allow commercial use."""
    try:
        r = requests.get(OPENVERSE, params={"q": query, "license_type": "commercial", "page_size": max(n * 3, 12),
                                            "format": "json"},
                         timeout=20, headers={"User-Agent": "Kakarot-assistant"})
        r.raise_for_status()
        out = []
        for it in r.json().get("results", []):
            if (it.get("width") or 0) < 900:
                continue
            out.append({"url": it.get("url"), "title": it.get("title") or query, "creator": it.get("creator") or "",
                        "license": ("CC " if (it.get("license") or "") not in ("pdm", "cc0") else "") +
                                   (it.get("license") or "").upper() + (" " + it["license_version"]
                                                                        if it.get("license_version") else ""),
                        "license_url": it.get("license_url") or "", "attribution": it.get("attribution") or "",
                        "source": it.get("foreign_landing_url") or "", "width": it.get("width"),
                        "height": it.get("height")})
            if len(out) >= n:
                break
        return out
    except Exception as e:
        log.warning("photo search failed for %r: %s", query, e)
        return []


def download_photo(item, dest_dir, name, max_w=1800):
    """Download, resize and save as JPEG. Returns the relative file name or None."""
    try:
        from PIL import Image
        r = requests.get(item["url"], timeout=40, headers={"User-Agent": "Kakarot-assistant"})
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content))
        im = im.convert("RGB")
        if im.width > max_w:
            im = im.resize((max_w, round(im.height * max_w / im.width)))
        os.makedirs(dest_dir, exist_ok=True)
        fn = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40] + ".jpg"
        im.save(os.path.join(dest_dir, fn), "JPEG", quality=82, optimize=True, progressive=True)
        return fn
    except Exception as e:
        log.warning("photo download failed %s: %s", item.get("url"), e)
        return None


def gather_photos(pdir, wants, folder="images/photos", per_query=2, limit=10):
    """wants = [{"query": "...", "use": "hero"}...]. Downloads real photos into the site and writes credits.
    Returns a list of {"file", "use", "alt", "credit"}."""
    got, credits = [], []
    dest = os.path.join(pdir, *folder.split("/"))
    for w in (wants or [])[:8]:
        q = (w.get("query") if isinstance(w, dict) else str(w)) or ""
        if not q.strip():
            continue
        for k, it in enumerate(find_photos(q, per_query)):
            fn = download_photo(it, dest, f"{q}-{k + 1}")
            if not fn:
                continue
            credit = f"\"{it['title'][:80]}\" by {it['creator'] or 'unknown'} ({it['license']}) {it['source']}".strip()
            got.append({"file": f"{folder}/{fn}", "use": (w.get("use") if isinstance(w, dict) else "") or q,
                        "alt": it["title"][:120], "credit": credit})
            credits.append({"file": f"{folder}/{fn}", **it})
            if len(got) >= limit:
                break
        if len(got) >= limit:
            break
    if credits:
        with open(os.path.join(dest, "CREDITS.json"), "w", encoding="utf-8") as f:
            json.dump(credits, f, ensure_ascii=False, indent=1)
    return got


# ---------------------------------------------------------------- Figma
FIGMA_API = "https://api.figma.com/v1"


def parse_figma_url(url):
    m = re.search(r"figma\.com/(?:file|design|proto|board)/([A-Za-z0-9]+)", url or "")
    if not m:
        raise ValueError("That doesn't look like a Figma link (figma.com/design/...).")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    node = (q.get("node-id") or [""])[0].replace("-", ":")
    return m.group(1), node


def _figma(path, token, **params):
    r = requests.get(FIGMA_API + path, params=params, headers={"X-Figma-Token": token}, timeout=60)
    if r.status_code == 403:
        raise PermissionError("Figma refused the token - check it in Websites → Publishing accounts → Figma.")
    if r.status_code == 404:
        raise ValueError("Figma can't find that file - is the link right and shared with your account?")
    r.raise_for_status()
    return r.json()


def figma_me(token):
    return _figma("/me", token)


def _hex(c):
    return "#{:02x}{:02x}{:02x}".format(*(round(c.get(k, 0) * 255) for k in "rgb"))


def figma_read(url, token, out_dir):
    """Reads a Figma frame: returns (summary_text, [png paths])."""
    key, node = parse_figma_url(url)
    if node:
        data = _figma(f"/files/{key}/nodes", token, ids=node, depth=12)
        root = list(data["nodes"].values())[0]["document"]
        fname = data.get("name", "")
    else:
        data = _figma(f"/files/{key}", token, depth=3)
        fname = data.get("name", "")
        page = data["document"]["children"][0]
        frames = [c for c in page.get("children", []) if c.get("type") in ("FRAME", "COMPONENT", "SECTION")]
        if not frames:
            raise ValueError("No frames found on the first page of that Figma file. Copy the link of one frame.")
        root = max(frames, key=lambda f: (f.get("absoluteBoundingBox") or {}).get("width", 0))
        node = root["id"]
        root = list(_figma(f"/files/{key}/nodes", token, ids=node, depth=12)["nodes"].values())[0]["document"]
    texts, colors, fonts, blocks = [], {}, {}, []

    def walk(n, depth=0):
        t = n.get("type")
        if t == "TEXT":
            s = (n.get("characters") or "").strip()
            st = n.get("style") or {}
            if s:
                texts.append(f"[{round(st.get('fontSize', 0))}px {st.get('fontFamily', '')} {st.get('fontWeight', '')}] {s[:200]}")
            if st.get("fontFamily"):
                fonts[st["fontFamily"]] = fonts.get(st["fontFamily"], 0) + 1
        for f in n.get("fills") or []:
            if f.get("type") == "SOLID" and f.get("visible", True) and f.get("color"):
                h = _hex(f["color"])
                colors[h] = colors.get(h, 0) + 1
        if depth == 1 and t in ("FRAME", "GROUP", "SECTION", "INSTANCE", "COMPONENT"):
            bb = n.get("absoluteBoundingBox") or {}
            blocks.append(f"- {n.get('name', '')} ({round(bb.get('width', 0))}x{round(bb.get('height', 0))}"
                          f"{', ' + n['layoutMode'].lower() + ' auto-layout' if n.get('layoutMode') else ''})")
        for c in n.get("children") or []:
            walk(c, depth + 1)
    walk(root)
    bb = root.get("absoluteBoundingBox") or {}
    pngs = []
    try:
        img = _figma(f"/images/{key}", token, ids=node, format="png", scale=1)
        u = (img.get("images") or {}).get(node)
        if u:
            os.makedirs(out_dir, exist_ok=True)
            p = os.path.join(out_dir, "figma-design.png")
            with open(p, "wb") as f:
                f.write(requests.get(u, timeout=90).content)
            pngs.append(p)
    except Exception as e:
        log.warning("figma render failed: %s", e)
    summary = (f"Figma file \"{fname}\", frame \"{root.get('name', '')}\" "
               f"({round(bb.get('width', 0))}x{round(bb.get('height', 0))}px).\n"
               f"Sections from top: \n" + "\n".join(blocks[:30]) +
               "\nColours used (most first): " + ", ".join(k for k, _ in sorted(colors.items(), key=lambda x: -x[1])[:10]) +
               "\nFonts: " + ", ".join(k for k, _ in sorted(fonts.items(), key=lambda x: -x[1])[:4]) +
               "\nAll text in the design (size, font, weight, words) - use these exact words:\n" + "\n".join(texts[:120]))
    return summary[:9000], pngs
