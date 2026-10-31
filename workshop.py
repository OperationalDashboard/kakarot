"""Website workshop: open any site -> check it -> fix/change it -> publish it, as background Missions.

Every job shows its progress in the Missions/Websites tabs and ends with a report in the Inbox.
  open     : copy a site into Kakarot (folder, GitHub, FTP hosting, live link), then check it
  check    : deep check (errors, links, phone, SEO, accessibility, speed, security, code, design)
  fix      : check -> fix -> check again (up to N rounds); changes that make things worse are undone automatically
  change   : "add a prices page", "make the header green"... -> edit -> test -> report (auto-undo if it breaks)
  publish  : GitHub / GitHub Pages / FTP-cPanel / Netlify / Vercel / original folder (after the user approves)
  refresh  : bring in changes made on GitHub or on the hosting since Kakarot downloaded the site
"""
import json
import logging
import os
import re
import time
import traceback

import db
import deploy
import siteaudit
import siteedit
import siteimport
import siteproject as sp
import sites
import vault
from config import BASE, CFG
from tools import B, S, _cut, tool

log = logging.getLogger("kakarot.workshop")
MODES = ("open", "check", "fix", "change", "publish", "refresh")


# ================================================================= helpers
def _find(name):
    """Website by folder name, title, or live address."""
    if not name:
        raise ValueError("Which website? " + _names())
    cand = sp.slug(name)
    if sp.load(cand):
        return cand
    low = name.lower().strip().rstrip("/")
    for d in _all_names():
        s = sp.load(d) or {}
        live = (s.get("live_url") or "").lower().rstrip("/")
        if low in (s.get("title", "").lower(), d.lower()) or (live and (low == live or low in live.split("//")[-1])):
            return d
    raise ValueError(f"I can't find a website called '{name}'. " + _names())


def _all_names():
    import coder
    out = []
    for d in sorted(os.listdir(coder.CODE_DIR)):
        if os.path.isfile(os.path.join(coder.CODE_DIR, d, sp.SPEC_FILE)):
            out.append(d)
    return out


def _names():
    n = _all_names()
    return ("Your websites: " + ", ".join(n) + ".") if n else "No websites yet - open one with open_website."


def _job(mid):
    import sitebuilder
    return sitebuilder.Job(mid)


def _progress(job):
    import missions

    def p(msg):
        missions._save(job.mid, result="⏳ " + str(msg)[:300])
    return p


def _busy(name):
    rows = db.q("SELECT id, data FROM missions WHERE status IN ('queued','running') AND kind LIKE 'website%'")
    for r in rows:
        try:
            if json.loads(r["data"] or "{}").get("name") == name:
                return r["id"]
        except ValueError:
            pass
    return None


def _start(goal, data):
    import missions
    return missions.start(goal, kind="website", data=data)


def _fresh_report(pdir):
    """Latest check report if nothing changed since it was made (saves a full re-check)."""
    rep = siteaudit.latest(pdir)
    if not rep:
        return None
    hist = sp.history(pdir, 1)
    if hist and hist[0]["at"] > rep["at"]:
        return None
    return rep


def run_check(pdir, spec, p, live=False, look=True, pages=None):
    """Check the site (on this PC, or the live address). Returns the report (saved)."""
    if live:
        if not spec.get("live_url"):
            raise ValueError("I don't know this site's live address yet.")
        p(f"Checking the live site {spec['live_url']}")
        rep = siteaudit.audit(spec["live_url"], pdir, spec, live=True, max_pages=pages, progress=p, look=look,
                              shots_dir=os.path.join(pdir, sp.META, "screens"))
        rep["where"] = "live"
    else:
        p("Starting the site on this PC")
        with sites.local_server(pdir, spec) as (base, why):
            if base:
                rep = siteaudit.audit(base, pdir, spec, live=False, max_pages=pages, progress=p, look=look,
                                      shots_dir=os.path.join(pdir, sp.META, "screens"))
                rep["where"] = "local"
            else:
                rep = None
        if rep is None:
            if spec.get("live_url"):
                p("It can't run on this PC - checking the live site instead")
                rep = siteaudit.audit(spec["live_url"], pdir, spec, live=True, max_pages=pages, progress=p, look=look,
                                      shots_dir=os.path.join(pdir, sp.META, "screens"))
                rep["where"] = "live"
                rep["facts"]["not_run"] = why + " (so I checked the live site, plus the code files)"
            else:
                rep = siteaudit.code_only(pdir, spec, why, p)
                rep["where"] = "code"
    siteaudit.save(rep, pdir)
    spec = sp.load(spec["name"]) or spec
    spec["last_check"] = {"at": rep["at"], "score": rep["score"], "counts": rep["counts"], "where": rep.get("where")}
    sp.save(pdir, spec)
    return rep


def _issues_line(rep, n=6):
    return "\n".join(f"{siteaudit.ICON[i['sev']]} {i['title']}" + (f" [{i['file']}]" if i.get("file") else "")
                     for i in rep["issues"][:n])


def _finish(job, title, body, ok=True):
    import missions
    import scheduler
    missions._save(job.mid, status="done" if ok else "failed", result=body)
    db.add_inbox(title, body)
    try:
        scheduler.notify(title[:60], body[:200])
    except Exception:
        pass


# ================================================================= the missions
def run(mid):
    job = _job(mid)
    import missions
    missions._save(mid, status="running")
    mode = job.data.get("mode")
    p = _progress(job)
    try:
        {"open": _m_open, "check": _m_check, "fix": _m_fix, "change": _m_change, "publish": _m_publish,
         "refresh": _m_refresh}[mode](job, p)
    except (deploy.DeployError, ValueError, PermissionError, FileNotFoundError) as e:
        log.info("website job %s stopped: %s", mid, vault.scrub(e))
        _finish(job, "⚠️ Website job stopped", f"{job.goal}\n\n{vault.scrub(e)}", ok=False)
    except Exception as e:
        log.error("website job %s failed: %s", mid, vault.scrub(traceback.format_exc()))
        _finish(job, "⚠️ Website job failed", f"{job.goal}\n\nSomething went wrong: {vault.scrub(e)}\n"
                f"(Details are in data/kakarot.log. Try again, or tell me what you were doing.)", ok=False)


def _m_open(job, p):
    d = job.data
    job.set_plan(["Open the website", "Check it", "Report"] if d.get("check", True) else ["Open the website", "Report"])
    pdir, spec, msg = siteimport.open_site(d["source"], d.get("name", ""), d.get("account", ""), p)
    d["name"] = spec["name"]
    db.x("UPDATE missions SET data=? WHERE id=?", (json.dumps(d, ensure_ascii=False), job.mid))
    notes = "\n".join("ℹ️ " + n for n in spec.get("notes", []))
    job.done("Open the website", msg + ("\n" + notes if notes else ""))
    rep = None
    if d.get("check", True):
        rep = run_check(pdir, spec, p)
        job.done("Check it", siteaudit.text(rep, 12))
    spec = sp.load(spec["name"])
    body = (f"📂 Opened: {spec['title']} ({siteimport.describe(spec)})\n{msg}\n"
            f"▶ Preview: {sites.preview_path(spec['name'])}\n" +
            (f"🌍 Live: {spec['live_url']}\n" if spec.get("live_url") else "") + (notes + "\n" if notes else "") +
            (f"\n{siteaudit.text(rep, 25)}\n\nSay \"fix my website {spec['name']}\" and I'll fix what I can, "
             f"or tell me what to change." if rep else ""))
    job.done("Report", "Done")
    _finish(job, f"📂 Website opened: {spec['title']}", body)


def _m_check(job, p):
    d = job.data
    if d.get("url") and not d.get("name"):
        job.set_plan(["Check the live site", "Report"])
        url = d["url"]
        rep = siteaudit.audit(url, live=True, progress=p,
                              shots_dir=os.path.join(BASE, "data", "reports", "screens"))
        siteaudit.save(rep, None, re.sub(r"\W+", "_", url.split("//")[-1])[:40])
        job.done("Check the live site", f"Score {rep['score']}/100")
        body = (f"🔍 Website check: {url}\n\n{siteaudit.text(rep, 40)}\n\nTo fix these, open the site in Kakarot "
                f"(\"open my website {url}\") - with your hosting's FTP account saved I can fix and re-publish it.")
        job.done("Report", "Done")
        return _finish(job, f"🔍 Check: {url.split('//')[-1][:40]} - {rep['score']}/100", body)
    name = d["name"]
    pdir, spec = sp.project_dir(name), sp.load(name)
    live = d.get("where") == "live"
    job.set_plan(["Check the site" + (" (live)" if live else ""), "Report"])
    rep = run_check(pdir, spec, p, live=live)
    job.done(job.plan[0], f"Score {rep['score']}/100")
    body = f"🔍 {spec['title']} - check ({rep.get('where')})\n\n{siteaudit.text(rep, 40)}"
    fixable = sum(1 for i in rep["issues"] if siteaudit.can_fix(i))
    tips = sum(1 for i in rep["issues"] if i["fix"] == "suggest")
    if fixable:
        body += f"\n\nI can try to fix {fixable} of these myself: say \"fix my website {name}\"."
    if tips:
        body += (f"\n🎨 {tips} design tip(s) (things that make a site look AI-made). Your current look is kept unless "
                 f"you ask: say \"improve the design of {name}\" to apply them.")
    job.done("Report", "Done")
    _finish(job, f"🔍 {spec['title'][:40]}: {rep['score']}/100", body)


def _m_fix(job, p):
    d = job.data
    name = d["name"]
    pdir, spec = sp.project_dir(name), sp.load(name)
    rounds = max(1, min(int(d.get("rounds") or CFG.get("site_fix_rounds", 4)), 4))
    focus = d.get("focus", "")
    job.set_plan(["Check the site"] + [f"Fix round {r}" for r in range(1, rounds + 1)] + ["Report"])
    rep0 = _fresh_report(pdir) or run_check(pdir, spec, p)
    job.done("Check the site", f"Score {rep0['score']}/100\n{_issues_line(rep0)}")
    rep, changes, undone = rep0, [], []
    for r in range(1, rounds + 1):
        if job.cancelled():
            return
        todo = [i for i in rep["issues"] if siteaudit.can_fix(i, focus)]
        if focus and focus.lower() not in ("all", "everything"):
            f = focus.lower()
            todo = [i for i in todo if f in (i["area"] + " " + i["title"] + " " + (i.get("file") or "")).lower()] or todo
        if not todo:
            job.done(f"Fix round {r}", "Nothing left that I can fix automatically.")
            break
        ch = sp.Change(pdir, f"Fix round {r}: {len(todo)} problem(s)" + (f" ({focus})" if focus else ""), kind="fix")
        results = siteedit.fix_issues(pdir, spec, ch, todo, focus, log=p)
        if not ch.touched:
            job.done(f"Fix round {r}", "No file could be changed safely:\n" + "\n".join(results))
            break
        rep_new = run_check(pdir, spec, p, look=False, live=False) if rep0.get("where") != "live" else \
            _retest_after_local_edit(pdir, spec, p)
        broke = siteaudit.serious_keys(rep_new) - siteaudit.serious_keys(rep0)
        if broke:
            p("My changes caused new problems - undoing them")
            ch.rollback()
            undone.append(f"Round {r}: undone because it caused: " + "; ".join(k[2] for k in list(broke)[:3]))
            job.done(f"Fix round {r}", "Undone (made things worse): " + "; ".join(k[2] for k in list(broke)[:4]), ok=False)
            break
        meta = ch.commit({"score_before": rep["score"], "score_after": rep_new["score"]})
        changes.append(meta)
        job.done(f"Fix round {r}", "\n".join(results) + f"\nScore {rep['score']} → {rep_new['score']}")
        if rep_new["score"] <= rep["score"] and r > 1:
            rep = rep_new
            break
        rep = rep_new
    left_manual = [i for i in rep["issues"] if i["fix"] == "manual"]
    left_other = [i for i in rep["issues"] if siteaudit.can_fix(i, focus)]
    body = [f"🛠 {spec['title']}: score {rep0['score']} → {rep['score']}/100"]
    if changes:
        body.append("Changed: " + "; ".join(f"{c['label']}: " + ", ".join(f['path'] for f in c["files"][:8])
                                            for c in changes))
    body += undone
    if left_other:
        body.append(f"\nStill open ({len(left_other)}):\n" + _issues_line({"issues": left_other}, 10))
    if left_manual:
        body.append("\nThese need you (outside the code):\n" + "\n".join(
            f"{siteaudit.ICON[i['sev']]} {i['title']}" + (f" - {i['hint']}" if i.get("hint") else "") for i in left_manual[:8]))
    body.append(f"\n▶ Preview: {sites.preview_path(name)}\nEvery change can be undone (Websites tab → History, or "
                f"\"undo the last change on {name}\").")
    if d.get("publish_to") and changes:
        body.append("\n" + request_publish(name, d["publish_to"], d.get("publish_opts") or {},
                                           reason=f"Publish the fixes (score {rep0['score']} → {rep['score']})"))
    elif changes and _default_target(spec):
        body.append(f"\nWhen you're happy, say \"publish {name}\" to put it online.")
    job.done("Report", "Done")
    _finish(job, f"🛠 Fixed: {spec['title'][:40]} ({rep0['score']} → {rep['score']})", "\n".join(body))


def _retest_after_local_edit(pdir, spec, p):
    """Live-only sites (can't run here): after editing, check the code files again."""
    rep = siteaudit.code_only(pdir, spec, "Checked the code only (the live site changes when you publish)", p)
    siteaudit.save(rep, pdir)
    return rep


def _m_change(job, p):
    d = job.data
    name, task = d["name"], d["change"]
    pdir, spec = sp.project_dir(name), sp.load(name)
    job.set_plan(["Check the site first", "Make the change", "Test the result", "Report"])
    base = _fresh_report(pdir)
    if not base:
        base = run_check(pdir, spec, p, look=False, pages=8)
    job.done("Check the site first", f"Score {base['score']}/100")
    ch = sp.Change(pdir, task[:120], kind="change")
    summary, results = siteedit.change(pdir, spec, ch, task, log=p)
    if not ch.touched:
        job.done("Make the change", "Nothing could be changed safely:\n" + "\n".join(results), ok=False)
        return _finish(job, f"✏️ Couldn't change {spec['title'][:40]}",
                       f"I couldn't make this change safely: {task}\n\n" + "\n".join(results) +
                       "\n\nTry describing it a bit more exactly (which page, which part).", ok=False)
    job.done("Make the change", (summary + "\n" if summary else "") + "\n".join(results))
    after = run_check(pdir, spec, p, look=bool(re.search(r"colou?r|design|look|style|layout|font|theme", task, re.I)),
                      pages=8) if base.get("where") != "code" else _retest_after_local_edit(pdir, spec, p)
    broke = siteaudit.serious_keys(after) - siteaudit.serious_keys(base)
    if broke:
        p("The change broke something - trying to repair it")
        bad = [i for i in after["issues"] if siteaudit.serious_keys({"issues": [i]}) & broke]
        siteedit.fix_issues(pdir, spec, ch, bad, log=p, max_files=4)
        after = run_check(pdir, spec, p, look=False, pages=8) if base.get("where") != "code" else \
            _retest_after_local_edit(pdir, spec, p)
        broke = siteaudit.serious_keys(after) - siteaudit.serious_keys(base)
    if broke:
        ch.rollback()
        job.done("Test the result", "The change broke: " + "; ".join(k[2] for k in list(broke)[:4]) +
                 " - so I undid it.", ok=False)
        return _finish(job, f"✏️ Change undone: {spec['title'][:40]}",
                       f"I made the change \"{task}\" but it caused new problems:\n" +
                       "\n".join(f"🔴 {k[2]}" for k in list(broke)[:6]) +
                       "\n\nSo I put everything back. Tell me more about what you want, and I'll try another way.",
                       ok=False)
    meta = ch.commit({"score_before": base["score"], "score_after": after["score"], "summary": summary})
    job.done("Test the result", f"Works. Score {base['score']} → {after['score']}")
    body = (f"✏️ Done: {task}\n{summary}\n\nChanged: {', '.join(f['path'] + (' (new)' if f['state'] == 'created' else '') for f in meta['files'])}\n"
            f"Tests: score {base['score']} → {after['score']}/100, nothing broke.\n▶ Preview: {sites.preview_path(name)}"
            f"\nUndo any time: \"undo the last change on {name}\".")
    if d.get("publish_to"):
        body += "\n\n" + request_publish(name, d["publish_to"], d.get("publish_opts") or {}, reason=f"Publish: {task[:80]}")
    elif _default_target(spec):
        body += f"\nSay \"publish {name}\" to put it online."
    job.done("Report", "Done")
    _finish(job, f"✏️ Changed: {spec['title'][:40]}", body)


def _m_publish(job, p):
    d = job.data
    name, target, opts = d["name"], d["target"], d.get("opts") or {}
    pdir, spec = sp.project_dir(name), sp.load(name)
    if d.get("undo"):
        job.set_plan(["Undo the publish", "Check the live site", "Report"])
        rec = deploy.undo(pdir, spec, d["undo"], p)
        job.done("Undo the publish", f"Restored: {', '.join(rec.get('restored', [])[:15]) or '-'}"
                 + (f"\nRemoved: {', '.join(rec.get('removed', [])[:15])}" if rec.get("removed") else ""))
        v = deploy.verify(rec.get("live_url"), sp.web_root(pdir, spec), [], spec.get("type"), p) if rec.get("live_url") else {}
        job.done("Check the live site", _verify_text(v, True))
        job.done("Report", "Done")
        warn = ("\n⚠️ " + "\n⚠️ ".join(rec["warnings"])) if rec.get("warnings") else ""
        return _finish(job, f"↩️ Publish undone: {spec['title'][:40]}",
                       f"The earlier version of {spec['title']} is back on {rec.get('dest')}.{warn}\n{_verify_text(v, True)}")
    job.set_plan(["Prepare", "Publish", "Check the live site", "Report"])
    pl = deploy.plan(pdir, spec, target, opts)
    job.done("Prepare", f"To: {pl['dest']}\n{len(pl['files'])} changed file(s) of {pl.get('total', '?')}"
             + ("\n⚠️ " + "\n⚠️ ".join(pl["warnings"]) if pl["warnings"] else ""))
    rec = deploy.publish(pdir, spec, target, opts, p)
    spec = sp.load(name)
    sent = rec.get("uploaded") or []
    job.done("Publish", rec.get("note") or rec.get("summary") or (f"Sent {len(sent)} file(s)" + (": " + ", ".join(map(str, sent[:12])) if sent else "")
                                             + (f"\nRemoved: {', '.join(rec['deleted'][:10])}" if rec.get("deleted") else "")))
    url = rec.get("live_url") or spec.get("live_url")
    root = sp.web_root(pdir, spec)
    if target == "github" and not rec.get("live_url"):
        v = {"checked": False, "note": f"The code is on GitHub: {rec.get('url')}"}
    else:
        if rec.get("pages_branch"):
            changed = rec["pages_branch"].get("uploaded") or []
        elif target in ("netlify", "vercel"):
            changed = ["index.html"] if os.path.isfile(os.path.join(root, "index.html")) else []
        elif spec.get("type") == "php":
            changed = [c for c in sent if isinstance(c, str)]
            root = pdir
        else:
            changed = [sp.rel(os.path.join(pdir, *c.split("/")), root) for c in sent if isinstance(c, str)
                       and os.path.realpath(os.path.join(pdir, *c.split("/"))).startswith(os.path.realpath(root))]
        v = deploy.verify(url, root if os.path.isdir(root) else pdir, changed, spec.get("type"), p)
    rec["verify"] = v
    deploy._record(pdir, rec)
    job.done("Check the live site", _verify_text(v), ok=not v.get("errors") and not v.get("stale"))
    body = (f"🚀 Published {spec['title']} → {rec.get('dest')}\n" +
            (f"🌍 Live: {url}\n" if url else "") + (f"📦 Code: {rec['url']}\n" if rec.get("url") else "") +
            (rec.get("note", "") + "\n" if rec.get("note") else "") +
            (rec["summary"] + "\n" if rec.get("summary") else f"Sent {len(sent)} file(s).\n" if sent else "") +
            ("⚠️ " + "\n⚠️ ".join(rec["warnings"]) + "\n" if rec.get("warnings") else "") +
            f"\n{_verify_text(v)}\n\nUndo this publish any time: Websites tab → History, or \"undo the last publish of {name}\".")
    job.done("Report", "Done")
    _finish(job, f"🚀 Published: {spec['title'][:40]}", body)


def _verify_text(v, undo=False):
    if not v:
        return ""
    if not v.get("checked"):
        return v.get("note", "")
    parts = []
    if v.get("fresh"):
        parts.append(f"✅ The live site shows the new version ({len(v['fresh'])} file(s) checked).")
    if v.get("stale"):
        parts.append("⏳ Still old on the live site (cache?): " + ", ".join(v["stale"][:5]) +
                     " - it usually updates within a few minutes.")
    if v.get("errors"):
        parts.append("⚠️ Problems on the live site:\n" + "\n".join("- " + e for e in v["errors"][:6]) +
                     ("" if undo else "\nIf this is new, undo the publish."))
    elif v.get("score") is not None:
        parts.append(f"✅ Live pages open without errors (quick score {v['score']}/100).")
    return "\n".join(parts)


def _m_refresh(job, p):
    name = job.data["name"]
    pdir, spec = sp.project_dir(name), sp.load(name)
    o = spec.get("origin") or {}
    job.set_plan(["Get the latest version", "Report"])
    if o.get("type") == "github" or (spec.get("publish") or {}).get("github"):
        res = deploy.github_refresh(pdir, spec, p)
    elif o.get("type") == "ftp":
        res = deploy.ftp_refresh(pdir, spec, o["account"], o.get("folder"), p)
    else:
        raise ValueError("This site isn't connected to GitHub or a hosting account, so there's nothing to refresh.")
    txt = res.get("note") or (f"Updated {len(res['changed'])} file(s): " + ", ".join(res["changed"][:15]) if res["changed"]
                              else "No newer files.")
    if res.get("conflicts"):
        txt += ("\n⚠️ Changed in both places (I kept YOUR version here; the other version is saved in "
                f"{res['conflict_dir']}): " + ", ".join(res["conflicts"][:10]))
    job.done("Get the latest version", txt)
    job.done("Report", "Done")
    _finish(job, f"🔄 Refreshed: {spec['title'][:40]}", txt)


# ================================================================= publishing needs the user's OK
def resolve_target(text, spec):
    t = (text or "").strip().lower()
    accounts = vault.listing()
    ftps = [a for a in accounts if a["kind"] == "ftp"]
    o = spec.get("origin") or {}
    if not t or t in ("default", "online", "live", "it", "same place", "back"):
        if o.get("type") == "ftp":
            return o["account"]
        if o.get("type") == "github":
            return "github-pages" if ((spec.get("publish") or {}).get("github") or {}).get("pages") else "github"
        if o.get("type") == "folder" and t == "back":
            return "folder"
        last = sorted(((v.get("last", ""), k) for k, v in (spec.get("publish") or {}).items()), reverse=True)
        if last and last[0][0]:
            k = last[0][1]
            return ("github-pages" if (spec["publish"][k] or {}).get("pages") else "github") if k == "github" else k
        raise ValueError("Where should I publish it? Options: " + targets_text())
    if t.startswith("ftp:") and vault.has(t):
        return t
    if "pages" in t or "github.io" in t:
        return "github-pages"
    if "github" in t or t in ("git", "repo", "repository"):
        return "github"
    if "netlify" in t:
        return "netlify"
    if "vercel" in t:
        return "vercel"
    if t in ("folder", "original folder", "back to folder", "my folder", "pc"):
        return "folder"
    for a in ftps:
        hay = " ".join([a["key"], a.get("label", ""), a.get("host", ""), a.get("site_url", "")]).lower()
        if t in hay or vault.slug(t) in hay:
            return a["key"]
    if any(w in t for w in ("hosting", "cpanel", "namecheap", "ftp", "server", "host")):
        if o.get("type") == "ftp":
            return o["account"]
        if len(ftps) == 1:
            return ftps[0]["key"]
        if not ftps:
            raise ValueError("No hosting (FTP) account saved yet. Add it in Websites → Accounts (cPanel → FTP "
                             "Accounts shows the server, user name and port).")
        raise ValueError("Which hosting account? " + ", ".join(a["key"] for a in ftps))
    raise ValueError(f"I don't know where '{text}' is. Options: " + targets_text())


def targets_text():
    acc = vault.listing()
    opts = []
    if any(a["kind"] == "github" for a in acc):
        opts += ["github", "github-pages (free hosting)"]
    opts += [a["key"] + f" ({a.get('site_url') or a.get('host')})" for a in acc if a["kind"] == "ftp"]
    opts += [k for k in ("netlify", "vercel") if any(a["kind"] == k for a in acc)]
    missing = [k for k in ("github", "netlify", "vercel") if not any(a["kind"] == k for a in acc)]
    return (", ".join(opts) or "none yet") + (f". (Not set up: {', '.join(missing)}, FTP hosting - add them in "
                                             f"Websites → Accounts.)" if missing else "")


def _default_target(spec):
    try:
        return resolve_target("", spec)
    except ValueError:
        return None


def request_publish(name, target_text, opts=None, reason="", undo=None):
    name = _find(name)
    spec = sp.load(name)
    pdir = sp.project_dir(name)
    opts = opts or {}
    if undo:
        rec = deploy.get_deploy(pdir, undo)
        target = rec["target"]
        pl = {"dest": rec.get("dest"), "files": rec.get("uploaded") or [], "warnings": [], "total": None}
        what = "undo the publish of " + rec.get("at", "")
    else:
        target = resolve_target(target_text, spec)
        pl = deploy.plan(pdir, spec, target, opts)
        if not pl["files"] and target not in ("netlify", "vercel") and not opts.get("pages"):
            return f"Nothing to publish: {pl['dest']} already has the newest version of {name}."
        what = "publish"
    detail = {"name": name, "title": spec.get("title"), "target": target, "opts": opts, "dest": pl["dest"],
              "files": [str(f) for f in pl["files"][:60]], "count": len(pl["files"]), "total": pl.get("total"),
              "warnings": pl["warnings"], "undo": undo, "what": what}
    if not CFG.get("publish_needs_approval", True):
        mid = _start(f"{'Undo publish' if undo else 'Publish'} {name} → {pl['dest']}",
                     {"mode": "publish", "name": name, "target": target, "opts": opts, "undo": undo})
        return f"Publishing {name} to {pl['dest']} (Mission #{mid})."
    old = db.q("SELECT id, detail FROM approvals WHERE kind='publish' AND status='pending'")
    for r in old:
        try:
            od = json.loads(r["detail"])
            if od.get("name") == name and od.get("target") == target and od.get("undo") == undo:
                db.x("UPDATE approvals SET status='replaced' WHERE id=?", (r["id"],))
        except ValueError:
            pass
    db.x("INSERT INTO approvals(kind, detail, reason, status, created) VALUES(?,?,?,?,?)",
         ("publish", json.dumps(detail, ensure_ascii=False), reason or f"Publish {spec.get('title', name)}",
          "pending", db.now()))
    return (f"Ready to {what} {name} → {pl['dest']} ({len(pl['files'])} file(s)). I've asked for your OK: tap "
            f"Approve on the card in the chat page (or in the Websites tab)." +
            ("\nNote: " + " ".join(pl["warnings"]) if pl["warnings"] else ""))


def approved(detail):
    """Called when the user taps Approve on a publish card."""
    d = json.loads(detail) if isinstance(detail, str) else detail
    return _start(f"{'Undo publish' if d.get('undo') else 'Publish'} {d['name']} → {d['dest']}",
                  {"mode": "publish", "name": d["name"], "target": d["target"], "opts": d.get("opts") or {},
                   "undo": d.get("undo")})


# ================================================================= starting jobs (used by the tools and the web page)
def start_open(source, name="", account="", check=True):
    if not (source or account):
        raise ValueError("Tell me where the website is: a link, a folder on this PC, or a GitHub repository.")
    kind, info = siteimport.classify(source, account)
    where = {"folder": "folder", "github": "GitHub", "ftp": "hosting (FTP)", "url": "live site (copy)"}[kind]
    mid = _start(f"Open website from {where}: {source or account}", {"mode": "open", "source": source, "name": name,
                                                                     "account": account, "check": check})
    return mid, kind


def start_job(mode, name, **data):
    name = _find(name)
    b = _busy(name)
    if b:
        raise ValueError(f"I'm already working on {name} (Mission #{b}). It'll be ready soon.")
    labels = {"check": "Check website", "fix": "Fix website", "change": "Change website", "refresh": "Refresh website"}
    goal = f"{labels[mode]} {name}" + (f": {data.get('change') or data.get('focus')}" if data.get("change") or data.get("focus") else "")
    return _start(goal[:200], {"mode": mode, "name": name, **data})


# ================================================================= tools for the assistant
@tool("open_website",
      "OPEN AN EXISTING WEBSITE so Kakarot can check, fix, change and publish it. Source can be: a live address "
      "(lalsobujdata.site - if its hosting FTP account is saved, the REAL files are downloaded), a GitHub repo "
      "(github.com/owner/repo or owner/repo), a folder on this PC, or an FTP account name (ftp:mysite). It is "
      "copied into Kakarot and checked automatically in the background.",
      {"source": S("Live address, GitHub repo, folder path, or FTP account name."),
       "name": S("Optional short name for it in Kakarot."),
       "account": S("Optional FTP account to download from (e.g. ftp:lalsobuj).")}, ["source"])
def open_website(source, name="", account=""):
    mid, kind = start_open(source, name, account)
    extra = {"url": " (no hosting account saved for it, so I save a copy of the public pages - add the FTP account "
                    "in Websites → Accounts to work on the real files)",
             "ftp": " from your hosting"}.get(kind, "")
    return (f"Opening it{extra} and checking it (Mission #{mid}). The report arrives in your Inbox; you can follow "
            f"progress in the Websites tab.")


@tool("check_website",
      "DEEP CHECK of a website: errors, broken links, phone layout, SEO, accessibility, speed, security, code, "
      "design. Works for a site in Kakarot (by name) or ANY live address. Runs in the background; report in Inbox.",
      {"target": S("Website name in Kakarot, or a live address like https://example.com"),
       "live": B("For a site in Kakarot: check the live published site instead of the copy on this PC.")}, ["target"])
def check_website(target, live=False):
    try:
        name = _find(target)
    except ValueError:
        if re.match(r"^(https?://)?[\w-]+(\.[\w-]+)+", target.strip()):
            url = target.strip() if target.startswith("http") else "https://" + target.strip()
            mid = _start(f"Check website {url}", {"mode": "check", "url": url})
            return f"Checking {url} in the background (Mission #{mid}) - every page, links, phone view, speed, " \
                   f"security. The report arrives in your Inbox in a few minutes."
        raise
    mid = start_job("check", name, where="live" if live else "local")
    return f"Checking {name} in the background (Mission #{mid}). The report arrives in your Inbox."


@tool("fix_website",
      "FIX a website's problems automatically: check it, fix errors/links/SEO/accessibility/phone layout/speed, check "
      "again (several rounds). Changes that make things worse are undone automatically. Optional: publish after.",
      {"name": S("Website name (see list_websites)."),
       "focus": S("Optional: only fix this kind of problem (e.g. 'errors', 'mobile', 'SEO', 'speed', or 'design' to "
                  "also apply the design tips that remove the AI-made look)."),
       "publish_to": S("Optional: where to publish after fixing (github, github-pages, netlify, vercel, an FTP "
                       "account, or 'online' for where it came from). The user still approves.")}, ["name"])
def fix_website(name, focus="", publish_to=""):
    mid = start_job("fix", name, focus=focus, publish_to=publish_to)
    return f"Fixing it in the background (Mission #{mid}): check → fix → re-check. Report in your Inbox; every change can be undone."


@tool("update_website",
      "CHANGE any website in Kakarot (built or opened): add/remove pages or sections, change text, colors, layout, "
      "add features, fix a specific bug. Edits the right files safely, tests the result and undoes it if it breaks.",
      {"name": S("Website name (see list_websites)."), "change": S("Exactly what to change, with all details."),
       "publish_to": S("Optional: publish after the change (the user approves).")}, ["name", "change"])
def update_website(name, change, publish_to=""):
    mid = start_job("change", name, change=change, publish_to=publish_to)
    return f"Working on it in the background (Mission #{mid}): change → test → report in your Inbox."


@tool("publish_website",
      "PUBLISH a website: to GitHub (code), GitHub Pages (free hosting), the user's hosting over FTP/cPanel "
      "(e.g. Namecheap), Netlify, Vercel, or back to its original folder. Only changed files are sent, server files "
      "are backed up first. The user must approve (a card appears). Leave target empty to publish where it came from.",
      {"name": S("Website name."),
       "target": S("github | github-pages | netlify | vercel | folder | an FTP account like ftp:lalsobuj | "
                   "'hosting' | empty = where it came from"),
       "repo": S("For GitHub: owner/repo (optional; default: its own repo or a new one)."),
       "branch": S("For GitHub: branch (optional)."),
       "message": S("Optional note for the GitHub commit."),
       "folder": S("For FTP: remote folder (optional; default from the account, e.g. public_html).")}, ["name"])
def publish_website(name, target="", repo="", branch="", message="", folder=""):
    opts = {k: v for k, v in (("repo", repo), ("branch", branch), ("message", message), ("folder", folder)) if v}
    return request_publish(name, target, opts)


@tool("undo_website", "UNDO on a website: the last change (or a change by id), or the last publish (puts the "
                      "earlier version back online; needs approval).",
      {"name": S("Website name."), "what": S("'change' (default) or 'publish'."),
       "id": S("Optional id from website_history.")}, ["name"])
def undo_website(name, what="change", id=""):
    name = _find(name)
    pdir = sp.project_dir(name)
    if what.lower().startswith("pub"):
        recs = [r for r in deploy.deploys(pdir) if not r.get("undo_of") and r.get("target") and not r.get("note")]
        rec = next((r for r in recs if r["id"] == id), None) if id else (recs[0] if recs else None)
        if not rec:
            return "There's no publish to undo."
        return request_publish(name, rec["target"], undo=rec["id"], reason=f"Undo the publish of {rec['at']}")
    target, meta = sp.undo(pdir, id or None)
    return (f"Undone: \"{target['label']}\" ({', '.join(f['path'] for f in target['files'][:8])}). The site in Kakarot "
            f"is back as it was. (This undo can be undone too.) Publish again to update the live site.")


@tool("website_history", "Show the changes and publishes of a website (with ids for undo_website).",
      {"name": S("Website name.")})
def website_history(name):
    name = _find(name)
    pdir = sp.project_dir(name)
    out = ["Changes:"]
    for h in sp.history(pdir, 15):
        out.append(f"- {h['id']} {h['at']} {h['label']}" + (" (undone)" if h.get("undone") else "") + ": " +
                   ", ".join(f"{f['path']}" for f in h["files"][:6]))
    out.append("Publishes:")
    for r in deploy.deploys(pdir, 10):
        out.append(f"- {r['id']} {r['at']} {'UNDO ' if r.get('undo_of') else ''}{r.get('target')} → {r.get('dest')}"
                   + (f" ({len(r.get('uploaded') or [])} files)" if r.get("uploaded") else "") +
                   (f" {r['note']}" if r.get("note") else ""))
    return "\n".join(out)


@tool("refresh_website", "Get the latest version of a website from GitHub or from the hosting (changes made there "
                         "since Kakarot downloaded it). Files changed in both places are kept side by side.",
      {"name": S("Website name.")})
def refresh_website(name):
    mid = start_job("refresh", name)
    return f"Getting the latest version (Mission #{mid})."


@tool("list_websites", "List all websites in Kakarot (built or opened) with type, where they came from, live address, "
                       "last check score and preview link.")
def list_websites():
    rows = []
    for d in _all_names():
        s = sp.load(d) or {}
        lc = s.get("last_check") or {}
        o = s.get("origin") or {}
        src = {"github": f"GitHub {o.get('owner')}/{o.get('repo')}", "ftp": f"hosting {o.get('account')}",
               "folder": f"folder {o.get('path')}", "url": "copy of live pages"}.get(o.get("type"), "built by Kakarot")
        rows.append(f"- {d}: {s.get('title', d)} · {siteimport.describe(s)} · from {src}" +
                    (f" · live {s['live_url']}" if s.get("live_url") else "") +
                    (f" · score {lc['score']}/100 ({lc['at'][:10]})" if lc else "") + f" · preview {sites.preview_path(d)}")
    return ("Websites:\n" + "\n".join(rows)) if rows else "No websites yet. Build one (build_website) or open one (open_website)."


@tool("publishing_accounts", "Which publishing accounts are set up (GitHub, Netlify, Vercel, FTP hosting). Never "
                             "shows passwords.")
def publishing_accounts():
    acc = vault.listing()
    if not acc:
        return "No publishing accounts yet. The user can add them in the Websites tab → Accounts."
    return "Accounts: " + "; ".join(f"{a['key']}" + (f" ({a.get('host')} → {a.get('folder')}, site {a.get('site_url') or '-'})"
                                                     if a["kind"] == "ftp" else "") for a in acc) + \
        ". Targets: " + targets_text()


# ================================================================= data for the web page
def site_cards():
    out = []
    pend = {}
    for r in db.q("SELECT id, detail, reason FROM approvals WHERE kind='publish' AND status='pending'"):
        try:
            d = json.loads(r["detail"])
            pend.setdefault(d["name"], []).append({"id": r["id"], **d, "reason": r["reason"]})
        except ValueError:
            pass
    for d in _all_names():
        s = sp.load(d) or {}
        pdir = sp.project_dir(d)
        hist = sp.history(pdir, 1)
        out.append({"name": d, "title": s.get("title", d), "type": s.get("type"), "kind": siteimport.describe(s),
                    "summary": s.get("summary", ""), "link": sites.preview_path(d), "live": s.get("live_url") or "",
                    "origin": s.get("origin") or {"type": "built"}, "last_check": s.get("last_check"),
                    "targets": list((s.get("publish") or {}).keys()), "busy": _busy(d),
                    "last_change": hist[0] if hist else None, "pending": pend.get(d, []),
                    "notes": s.get("notes", [])})
    return out


def site_detail(name):
    name = _find(name)
    pdir = sp.project_dir(name)
    s = sp.load(name)
    full = siteaudit.latest(pdir)
    rep = None
    if full:
        rep = {k: full[k] for k in ("at", "score", "scores", "counts", "issues", "facts", "where", "target") if k in full}
        rep["pages"] = len(full.get("pages", []))
    return {"name": name, "spec": {k: s.get(k) for k in ("title", "type", "root", "framework", "live_url", "origin",
                                                          "publish", "notes", "summary", "last_check")},
            "kind": siteimport.describe(s), "link": sites.preview_path(name), "report": rep,
            "history": sp.history(pdir, 30), "deploys": deploy.deploys(pdir, 20),
            "files": len(sp.files(pdir)), "default_target": _default_target(s), "busy": _busy(name)}
