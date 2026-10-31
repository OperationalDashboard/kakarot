"""The coding brain changes websites safely - even big projects with large files.

- Finds the right files (file list + text search + the coding brain's judgement), never edits blind
- Small files are rewritten whole; big files get exact SEARCH/REPLACE edits (nothing else in the file is touched)
- Every edited file is syntax-checked (Python, PHP, JavaScript, JSON, HTML, CSS); a broken edit is retried once
  with the error, otherwise the file is put back exactly as it was
- Some fixes need no AI at all and are done exactly: compressing huge images, viewport/lang tags,
  security headers for Apache/cPanel hosting (.htaccess)
- Everything goes through siteproject.Change -> full diff + undo"""
import difflib
import io
import json
import os
import re
import shutil
import subprocess

import coder
import siteaudit
import siteproject as sp

WHOLE_LIMIT = 7000          # files up to this size are rewritten whole, bigger ones get SEARCH/REPLACE edits
WINDOW_LIMIT = 60000        # files bigger than this are shown to the brain in relevant windows only

PICK_PROMPT = """You maintain this {kind}. The task:
\"\"\"{task}\"\"\"

PROJECT FILES (path, size):
{tree}

FILES THAT CONTAIN WORDS FROM THE TASK:
{hits}
{extra}
Choose which existing files must be EDITED (max {max_edit}) and which NEW files must be CREATED (max 4) to do the
task completely and correctly. Prefer editing existing files. Reply ONLY JSON:
{{"edit": [{{"path": "...", "what": "exactly what to change in this file"}}],
  "create": [{{"path": "...", "purpose": "what it contains", "like": "an existing file to copy the layout/style from"}}],
  "summary": "one short sentence for the user"}}"""

BLOCKS_PROMPT = """You are changing one file of a {kind}.

TASK: {task}
FOR THIS FILE: {what}
{context}
FILE `{path}` ({lines} lines){window_note}:
```
{code}
```

Make the change using SEARCH/REPLACE blocks, exactly in this format:
<<<<<<< SEARCH
exact existing lines copied character for character from the file above
=======
the new lines
>>>>>>> REPLACE

Rules:
- SEARCH text must match the file EXACTLY (spaces, quotes, indentation) and be unique - include 2-3 extra lines
  around the change if needed.
- Use several small blocks instead of one huge block. Don't repeat unchanged parts of the file.
- To add code, SEARCH for the line where it goes and REPLACE it with that line plus the new code.
- Keep the existing style, names and language of the site. Real content only, no placeholders or TODOs.
Reply with ONLY the blocks."""

WHOLE_PROMPT = """You are changing one file of a {kind}.

TASK: {task}
FOR THIS FILE: {what}
{context}
FILE `{path}` NOW:
```
{code}
```
Reply with the COMPLETE updated file `{path}` in one code block. Keep everything that already works; change only
what the task needs. Real content only, no placeholders, no "..." or "rest of the code"."""

CREATE_PROMPT = """You are adding a new file to a {kind}.

TASK: {task}
NEW FILE: `{path}` - {purpose}
{context}
Match the existing site exactly: same layout, header/navigation, footer, CSS classes, scripts, language and tone.
Use paths that work from where this file lives. Real content only, no placeholders.
Reply with ONLY the complete file `{path}` in one code block."""

RETRY_PROMPT = """Some SEARCH blocks did not match the file exactly, so they were NOT applied:
{failed}

Here are the real lines of the file closest to what you searched for:
{closest}

Send corrected SEARCH/REPLACE blocks for ONLY the failed changes (copy SEARCH text exactly from the real lines)."""

SYNTAX_PROMPT = """After your edit, `{path}` has an error:
{error}

The file now:
```
{code}
```
Fix it with SEARCH/REPLACE blocks (same format as before). Reply with ONLY the blocks."""

BLOCK_RE = re.compile(r"<{5,9} ?SEARCH[^\n]*\n(.*?)\n?={5,9}[^\n]*\n(.*?)\n?>{5,9} ?REPLACE", re.S)


def kind_text(spec):
    import siteimport
    return siteimport.describe(spec)


# ---------------------------------------------------------------- SEARCH/REPLACE
def parse_blocks(text):
    return [(s, r) for s, r in BLOCK_RE.findall(text or "")]


def _fuzzy_find(lines, s_lines):
    """Find s_lines in lines ignoring indentation/trailing spaces. Returns (start, end) or None."""
    want = [l.strip() for l in s_lines]
    while want and not want[0]:
        want.pop(0)
        s_lines = s_lines[1:]
    while want and not want[-1]:
        want.pop()
        s_lines = s_lines[:-1]
    if not want:
        return None
    n = len(want)
    hits = []
    for i in range(len(lines) - n + 1):
        if lines[i].strip() == want[0] and [l.strip() for l in lines[i:i + n]] == want:
            hits.append(i)
            if len(hits) > 1:
                break
    if len(hits) == 1:
        return hits[0], hits[0] + n, s_lines
    return None


def apply_blocks(text, blocks):
    """Returns (new_text, applied_count, failed[(search, reason)])."""
    applied, failed = 0, []
    for search, repl in blocks:
        if not search.strip():
            text = text.rstrip("\n") + "\n" + repl + "\n"
            applied += 1
            continue
        cnt = text.count(search)
        if cnt == 1:
            text = text.replace(search, repl, 1)
            applied += 1
            continue
        if cnt > 1:
            failed.append((search, f"matches {cnt} places - add more lines to make it unique"))
            continue
        lines = text.split("\n")
        hit = _fuzzy_find(lines, search.split("\n"))
        if not hit:
            failed.append((search, "not found"))
            continue
        a, b, s_lines = hit
        # put the replacement at the file's real indentation (line by line, keeping the brain's relative indents)
        ind = lambda l: l[:len(l) - len(l.lstrip())]
        s_ind = [ind(l) for l in s_lines] or [""]
        o_ind = [ind(l) for l in lines[a:b]] or [""]
        new = []
        for j, l in enumerate(repl.split("\n")):
            if not l.strip():
                new.append(l)
                continue
            k = min(j, len(o_ind) - 1, len(s_ind) - 1)
            rel = len(ind(l)) - len(s_ind[k])
            basei = o_ind[k]
            new.append((basei + " " * rel if rel >= 0 else basei[:max(0, len(basei) + rel)]) + l.lstrip())
        lines[a:b] = new
        text = "\n".join(lines)
        applied += 1
    return text, applied, failed


def _closest(text, search, n=12):
    lines = text.split("\n")
    s_lines = [l for l in search.split("\n") if l.strip()]
    if not s_lines:
        return ""
    key = s_lines[0].strip()
    best = difflib.get_close_matches(key, [l.strip() for l in lines], n=1, cutoff=0.4)
    if not best:
        return "(nothing similar)"
    i = next(i for i, l in enumerate(lines) if l.strip() == best[0])
    return "\n".join(lines[max(0, i - 2): i + max(len(s_lines), 4) + 2])


# ---------------------------------------------------------------- syntax guard
def check(fp, text, before=""):
    """Returns an error message if the new text is broken (or obviously cut off), else None."""
    ext = os.path.splitext(fp)[1].lower()
    if ext in (".py", ".json", ".php", ".js", ".cjs"):
        tmp = fp + ".kakarot-check" + ext
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        try:
            err = siteaudit.syntax_error(tmp, ext)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if err:
            return err.replace(tmp, fp)
    if ext in (".html", ".htm", ".php"):
        low, blow = text.lower(), before.lower()
        if "</html>" in blow and "</html>" not in low:
            return "The page was cut off: </html> at the end is missing."
        if low.count("<script") > low.count("</script>"):
            return "A <script> tag is not closed."
        if "</body>" in blow and "</body>" not in low:
            return "The </body> tag is missing."
    if ext in (".css", ".scss", ".less"):
        t = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        t = re.sub(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", "", t)
        if t.count("{") != t.count("}"):
            return f"Curly braces don't match ({t.count('{')} '{{' vs {t.count('}')} '}}') - a rule is unfinished."
    return None


# ---------------------------------------------------------------- choosing files
STOP = set("the a an and or to of in on for with this that from my our your page site website make add change fix "
           "please into it is are be all new more use using also like want need should can".split())


def keywords(task):
    words = re.findall(r"[\"']([^\"']{3,40})[\"']", task)
    words += [w for w in re.findall(r"[A-Za-z][\w-]{3,}", task) if w.lower() not in STOP]
    words += re.findall(r"[\u0980-\u09FF]{2,}", task)   # Bangla words
    return list(dict.fromkeys(words))[:14]


def grep(pdir, words, limit=15):
    if not words:
        return []
    pats = [re.compile(re.escape(w), re.I) for w in words]
    out = []
    for r in sp.files(pdir, limit=5000):
        fp = os.path.join(pdir, *r.split("/"))
        if not sp.is_text(fp) or ".min." in r or "/vendor/" in "/" + r or os.path.getsize(fp) > 800_000:
            continue
        txt = sp.read_text(fp)
        score, lines = 0, []
        for i, line in enumerate(txt.split("\n"), 1):
            if any(p.search(line) for p in pats):
                score += 1
                if len(lines) < 3:
                    lines.append(f"  {i}: {line.strip()[:140]}")
        name_hit = sum(1 for p in pats if p.search(r))
        if score or name_hit:
            out.append((score + 5 * name_hit, r, lines))
    out.sort(reverse=True)
    return out[:limit]


def pick_files(pdir, spec, task, hint_files=None, max_edit=6):
    tree = sp.tree_text(pdir, 300)
    hits = grep(pdir, keywords(task))
    hit_text = "\n".join(f"- {r}\n" + "\n".join(lines) for _, r, lines in hits) or "(none)"
    extra = ""
    if hint_files:
        extra = "\nThe tests point at these files: " + ", ".join(hint_files) + "\n"
    raw, _ = coder.ask_coder(PICK_PROMPT.format(kind=kind_text(spec), task=task, tree=tree, hits=hit_text, extra=extra,
                                                max_edit=max_edit))
    try:
        plan = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except Exception:
        plan = {"edit": [{"path": h, "what": task} for h in (hint_files or [r for _, r, _ in hits[:2]])], "create": []}
    existing = set(sp.files(pdir, limit=20000))
    if spec.get("type") == "node-build":
        bd = (spec.get("build_dir") or "dist").strip("/") + "/"
        existing = {e for e in existing if not e.startswith(bd)}
    edits = []
    for e in plan.get("edit") or []:
        p = str(e.get("path", "")).strip().lstrip("/").replace("\\", "/")
        if p in existing and p not in [x["path"] for x in edits]:
            edits.append({"path": p, "what": e.get("what") or task})
    creates = []
    for c in plan.get("create") or []:
        p = str(c.get("path", "")).strip().lstrip("/").replace("\\", "/")
        if p and p not in existing and not p.startswith((".kakarot", "..")):
            creates.append({"path": p, "purpose": c.get("purpose") or task,
                            "like": str(c.get("like") or "").strip().lstrip("/")})
    return edits[:max_edit], creates[:4], plan.get("summary") or ""


def _context(pdir, spec, path, budget=9000):
    """Small, useful context for editing one file: the main stylesheet's class names, linked files' heads."""
    parts, used = [], 0
    fp = os.path.join(pdir, *path.split("/"))
    txt = sp.read_text(fp, 200000) if os.path.exists(fp) else ""
    linked = re.findall(r"""(?:href|src)=["']([^"'#?]+\.(?:css|js))""", txt)
    for l in linked[:4]:
        if l.startswith(("http", "//")):
            continue
        cand = os.path.normpath(os.path.join(os.path.dirname(fp), l)) if not l.startswith("/") else \
            os.path.join(sp.web_root(pdir, spec), *l.lstrip("/").split("/"))
        if os.path.isfile(cand) and used < budget:
            t = sp.read_text(cand, 60000)
            if cand.endswith(".css"):
                classes = sorted(set(re.findall(r"\.([a-zA-Z][\w-]+)", t)))[:150]
                piece = f"CSS classes available in {sp.rel(cand, pdir)}: " + " ".join(classes)
            else:
                funcs = sorted(set(re.findall(r"function\s+(\w+)|(\w+)\s*=\s*\(?[\w,\s]*\)?\s*=>", t)), key=str)[:60]
                piece = f"{sp.rel(cand, pdir)} defines: " + ", ".join(a or b for a, b in funcs)
            parts.append(piece[:2500])
            used += len(piece)
    return ("\nCONTEXT:\n" + "\n".join(parts) + "\n") if parts else ""


def _windows(text, task, line_hints=None, radius=70):
    """For huge files: only the parts that matter (with their real text, so SEARCH blocks still match)."""
    lines = text.split("\n")
    want = set()
    for n in line_hints or []:
        want.update(range(max(0, n - radius), min(len(lines), n + radius)))
    pats = [re.compile(re.escape(w), re.I) for w in keywords(task)]
    for i, l in enumerate(lines):
        if any(p.search(l) for p in pats):
            want.update(range(max(0, i - 25), min(len(lines), i + 25)))
        if len(want) > 900:
            break
    if not want:
        want = set(range(min(len(lines), 300)))
    out, prev = [], -2
    for i in sorted(want):
        if i != prev + 1:
            out.append(f"... (lines {prev + 2}-{i} not shown) ...")
        out.append(lines[i])
        prev = i
    return "\n".join(out)


# ---------------------------------------------------------------- editing one file
def edit_file(pdir, spec, ch, path, task, what="", line_hints=None, log=None):
    """Change one existing file. Returns a short result text; the file is restored if the edit failed."""
    fp = sp.abspath(pdir, path)
    old = sp.read_text(fp)
    kind = kind_text(spec)
    ctx = _context(pdir, spec, path)
    note = lambda m: log(m) if log else None
    if len(old) <= WHOLE_LIMIT:
        new = coder.extract_code(coder.ask_coder(WHOLE_PROMPT.format(kind=kind, task=task, what=what or task,
                                                                     context=ctx, path=path, code=old))[0])
        if not old.strip() or len(new.strip()) >= 0.35 * len(old.strip()) or re.search(r"remov|delet|shorter|clean", task, re.I):
            if new.strip() == old.strip():
                return f"{path}: no change needed"
            err = check(fp, new, old)
            if not err:
                ch.write(path, new)
                return f"{path}: updated"
            note(f"{path}: {err} - retrying")
            text, failed = new, []
        else:
            return f"{path}: rejected an edit that removed most of the file"
    else:
        shown = old if len(old) <= WINDOW_LIMIT else _windows(old, task + " " + what, line_hints)
        prompt = BLOCKS_PROMPT.format(kind=kind, task=task, what=what or task, context=ctx, path=path,
                                      lines=old.count("\n") + 1, code=shown,
                                      window_note="" if shown is old else " - only the relevant parts are shown")
        answer, _ = coder.ask_coder(prompt)
        text, n, failed = apply_blocks(old, parse_blocks(answer))
        if failed:
            note(f"{path}: {len(failed)} edit(s) didn't match - asking again")
            again = RETRY_PROMPT.format(failed="\n\n".join(f"SEARCH (reason: {why}):\n{s[:800]}" for s, why in failed[:6]),
                                        closest="\n\n".join(_closest(text, s) for s, _ in failed[:6]))
            answer2, _ = coder.ask_coder(prompt + "\n\n" + answer + "\n\n" + again)
            text, n2, failed = apply_blocks(text, parse_blocks(answer2))
            n += n2
        if not n:
            if len(old) <= 40000:
                note(f"{path}: switching to a full rewrite")
                new = coder.extract_code(coder.ask_coder(WHOLE_PROMPT.format(kind=kind, task=task, what=what or task,
                                                                             context=ctx, path=path, code=old))[0])
                if len(new.strip()) >= 0.6 * len(old.strip()):
                    text, failed = new, []
                else:
                    return f"{path}: couldn't make a safe edit"
            else:
                return f"{path}: couldn't make the edit (the brain's changes didn't match the file)"
        err = check(fp, text, old)
        if not err:
            ch.write(path, text)
            return f"{path}: updated" + (f" ({len(failed)} small part(s) skipped)" if failed else "")
        note(f"{path}: {err} - retrying")
    # one repair round for a syntax problem
    answer, _ = coder.ask_coder(SYNTAX_PROMPT.format(path=path, error=err, code=text if len(text) < WINDOW_LIMIT else
                                                     _windows(text, err, [int(x) for x in re.findall(r"line (\d+)", err)])))
    fixed, n, _ = apply_blocks(text, parse_blocks(answer))
    if not n:
        maybe = coder.extract_code(answer)
        if len(maybe.strip()) > 0.6 * len(text.strip()):
            fixed = maybe
    err2 = check(fp, fixed, old)
    if not err2:
        ch.write(path, fixed)
        return f"{path}: updated (after fixing an error in my edit)"
    return f"{path}: my edit had an error ({err2[:120]}), so I left the file unchanged"


def create_file(pdir, spec, ch, path, task, purpose, like=""):
    ctx = ""
    if like and os.path.isfile(os.path.join(pdir, *like.split("/"))):
        ctx = f"\nEXISTING FILE TO MATCH (`{like}`):\n```\n{sp.read_text(os.path.join(pdir, *like.split('/')), 12000)}\n```\n"
    else:
        sib = next((r for r in sp.files(pdir) if os.path.splitext(r)[1] == os.path.splitext(path)[1]), None)
        if sib:
            ctx = f"\nEXISTING FILE TO MATCH (`{sib}`):\n```\n{sp.read_text(os.path.join(pdir, *sib.split('/')), 12000)}\n```\n"
    code = coder.extract_code(coder.ask_coder(CREATE_PROMPT.format(kind=kind_text(spec), task=task, path=path,
                                                                   purpose=purpose, context=ctx))[0])
    err = check(sp.abspath(pdir, path), code)
    if err:
        return f"{path}: not created ({err[:100]})"
    ch.write(path, code)
    return f"{path}: created"


# ---------------------------------------------------------------- a change request ("add a prices page")
def change(pdir, spec, ch, task, log=None):
    edits, creates, summary = pick_files(pdir, spec, task)
    if log:
        log("Plan: " + (summary or task) + " | edit " + ", ".join(e["path"] for e in edits) +
            (" | new " + ", ".join(c["path"] for c in creates) if creates else ""))
    results = []
    for c in creates:
        results.append(create_file(pdir, spec, ch, c["path"], task, c["purpose"], c.get("like", "")))
    for e in edits:
        if log:
            log(f"Editing {e['path']}")
        results.append(edit_file(pdir, spec, ch, e["path"], task, e["what"], log=log))
    return summary, results


# ---------------------------------------------------------------- fixing problems from a check report
def _compress_image(fp, max_w=1920):
    from PIL import Image
    before = os.path.getsize(fp)
    with Image.open(fp) as im:
        fmt = (im.format or "").upper()
        if fmt not in ("JPEG", "PNG", "WEBP") or getattr(im, "is_animated", False):
            return None
        im.load()
        if im.width > max_w:
            im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        if fmt == "JPEG":
            im.convert("RGB").save(buf, "JPEG", quality=82, optimize=True, progressive=True)
        elif fmt == "WEBP":
            im.save(buf, "WEBP", quality=80, method=6)
        else:
            if im.mode not in ("RGBA", "LA", "P") and before > 300_000:
                im = im.convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT)
            im.save(buf, "PNG", optimize=True)
    data = buf.getvalue()
    return data if len(data) < before * 0.85 else None


HTACCESS = """# BEGIN Kakarot security
<IfModule mod_headers.c>
  Header always set X-Content-Type-Options "nosniff"
  Header always set X-Frame-Options "SAMEORIGIN"
  Header always set Referrer-Policy "strict-origin-when-cross-origin"
</IfModule>
{redirect}<FilesMatch "^\\.(env|git)">
  Require all denied
</FilesMatch>
RedirectMatch 404 /\\.git
# END Kakarot security
"""
HTTPS_REDIRECT = """<IfModule mod_rewrite.c>
  RewriteEngine On
  RewriteCond %{HTTPS} off
  RewriteCond %{HTTP:X-Forwarded-Proto} !https
  RewriteRule ^ https://%{HTTP_HOST}%{REQUEST_URI} [L,R=301]
</IfModule>
"""


def auto_fixes(pdir, spec, ch, issues, log=None):
    """Exact fixes that don't need the AI. Returns (results, ids of issues handled)."""
    res, done = [], set()
    root = sp.web_root(pdir, spec)
    # huge / oversized images
    for i in issues:
        if i["code"] in ("big_file", "oversized_img") and i.get("file") and re.search(r"\.(jpe?g|png|webp)$", i["file"], re.I):
            fp = os.path.join(pdir, *i["file"].split("/"))
            if os.path.isfile(fp) and i["file"] not in ch.touched:
                try:
                    data = _compress_image(fp)
                except Exception as e:
                    data = None
                    if log:
                        log(f"couldn't compress {i['file']}: {e}")
                if data:
                    before = os.path.getsize(fp)
                    ch.write_bytes(i["file"], data)
                    res.append(f"{i['file']}: image compressed {before // 1024} KB → {len(data) // 1024} KB")
                    done.add(i["id"])
    # viewport / lang on pages
    for code, fixer in (("no_viewport", _add_viewport), ("no_lang", _add_lang)):
        for i in issues:
            if i["code"] != code:
                continue
            targets = [i["file"]] if i.get("file") else []
            for f in targets:
                fp = os.path.join(pdir, *f.split("/"))
                if not os.path.isfile(fp):
                    continue
                new = fixer(sp.read_text(fp))
                if new:
                    ch.write(f, new)
                    res.append(f"{f}: added {'viewport tag (phone layout)' if code == 'no_viewport' else 'page language'}")
                    done.add(i["id"])
    # security headers / https redirect / private files -> .htaccess (Apache hosting = cPanel, Namecheap...)
    sec = [i for i in issues if i["code"] in ("nosniff", "frames", "referrer", "exposed", "no_redirect")]
    apache = spec.get("type") == "php" or (spec.get("origin") or {}).get("type") == "ftp" or \
        any(k.startswith("ftp:") for k in (spec.get("publish") or {}))
    if sec and apache:
        rel_ht = sp.rel(os.path.join(root, ".htaccess"), pdir)
        fp = os.path.join(pdir, *rel_ht.split("/"))
        cur = sp.read_text(fp) if os.path.exists(fp) else ""
        block = HTACCESS.format(redirect=HTTPS_REDIRECT if any(i["code"] == "no_redirect" for i in sec) and
                                "RewriteCond %{HTTPS}" not in cur else "")
        cur = re.sub(r"# BEGIN Kakarot security.*?# END Kakarot security\n?", "", cur, flags=re.S)
        ch.write(rel_ht, block + ("\n" + cur if cur.strip() else ""))
        res.append(f"{rel_ht}: security headers" + (", https redirect" if "RewriteCond %{HTTPS} off" in block else "")
                   + ", blocked private files")
        done.update(i["id"] for i in sec)
    elif sec and (spec.get("publish") or {}).get("netlify"):
        hp = sp.rel(os.path.join(root, "_headers"), pdir)
        ch.write(hp, "/*\n  X-Content-Type-Options: nosniff\n  X-Frame-Options: SAMEORIGIN\n"
                     "  Referrer-Policy: strict-origin-when-cross-origin\n")
        res.append(f"{hp}: security headers for Netlify")
        done.update(i["id"] for i in sec if i["code"] != "exposed")
    return res, done


def _add_viewport(html):
    if re.search(r"<meta[^>]+name=[\"']viewport", html, re.I):
        return None
    tag = '<meta name="viewport" content="width=device-width, initial-scale=1">'
    m = re.search(r"<meta[^>]+charset[^>]*>", html, re.I) or re.search(r"<head[^>]*>", html, re.I)
    return html[:m.end()] + "\n  " + tag + html[m.end():] if m else None


def _add_lang(html):
    m = re.search(r"<html(?![^>]*\slang=)([^>]*)>", html, re.I)
    if not m:
        return None
    lang = "bn" if len(re.findall(r"[\u0980-\u09FF]", html)) > 200 else "en"
    return html[:m.start()] + f'<html lang="{lang}"{m.group(1)}>' + html[m.end():]


def fix_issues(pdir, spec, ch, issues, focus="", log=None, max_files=8):
    """Fix the problems of a report. Returns list of result lines."""
    say = log or (lambda m: None)
    todo = [i for i in issues if i["fix"] != "manual"]
    if focus:
        f = focus.lower()
        todo = [i for i in todo if f in (i["area"].lower() + " " + i["title"].lower() + " " + (i.get("file") or "").lower())
                or f in ("all", "everything")] or todo
    results, done = auto_fixes(pdir, spec, ch, todo, say)
    rest = [i for i in todo if i["id"] not in done]
    # missing pages that the site links to
    for i in [i for i in rest if i["code"] == "broken_link" and i["sev"] == "high"][:4]:
        from urllib.parse import urlparse
        path = urlparse(i.get("detail") or "").path.lstrip("/") if (i.get("detail") or "").startswith("http") else ""
        m = re.match(r"Broken link: (\S+)", i["title"])
        if not path and m:
            path = urlparse(m.group(1)).path.lstrip("/")
        if not path:
            continue
        if re.search(r"\.(html?|php)$", path) and not os.path.exists(os.path.join(sp.web_root(pdir, spec), *path.split("/"))):
            relp = sp.rel(os.path.join(sp.web_root(pdir, spec), *path.split("/")), pdir)
            say(f"Creating the missing page {relp}")
            results.append(create_file(pdir, spec, ch, relp, f"The site links to {path} but it doesn't exist. Create it.",
                                       "a page of the site that the navigation links to", i.get("file") or ""))
            done.add(i["id"])
    rest = [i for i in rest if i["id"] not in done]
    by_file, unknown = {}, []
    for i in rest:
        (by_file.setdefault(i["file"], []) if i.get("file") else unknown).append(i)
    if unknown:
        # let the brain decide where problems without a known file live (Flask templates, shared headers...)
        task = "Fix these problems found by testing the website:\n" + "\n".join(
            f"- {i['title']}" + (f" (pages: {', '.join(i['pages'][:3])})" if i["pages"] else "") +
            (f"\n  details: {i['detail'][:300]}" if i.get("detail") else "") for i in unknown[:15])
        edits, creates, _ = pick_files(pdir, spec, task, max_edit=4)
        for e in edits:
            by_file.setdefault(e["path"], []).append({"title": e["what"], "detail": "", "pages": [], "_task": task})
    build_dir = (spec.get("build_dir") or "dist").strip("/") + "/" if spec.get("type") == "node-build" else None
    for path, items in list(by_file.items())[:max_files]:
        fp = os.path.join(pdir, *path.split("/"))
        if not os.path.isfile(fp) or not sp.is_text(fp) or (build_dir and path.startswith(build_dir)):
            continue
        lines = []
        hints = []
        for i in items[:14]:
            lines.append(f"- {i['title']}" + (f"\n  details: {i['detail'][:500]}" if i.get("detail") else "") +
                         (f"\n  how: {i['hint']}" if i.get("hint") else ""))
            for a, b in re.findall(r"line (\d+)|:(\d+):\d+", i.get("detail") or ""):
                hints.append(int(a or b))
        task = "Fix these problems found by testing the website:\n" + "\n".join(lines)
        say(f"Fixing {path} ({len(items)} problem{'s' if len(items) > 1 else ''})")
        results.append(edit_file(pdir, spec, ch, path, task, line_hints=hints, log=say))
    return results
