"""Automatic checks that run on their own:
- Daily Lal Sobuj brief: every morning, all routes for today, compared with the previous brief.
- Website watchdog: checks your websites every few minutes and alerts you when one goes down or comes back."""
import datetime
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlparse

import requests

import db
from config import CFG
from tools import ROOTS, S, UA, tool

log = logging.getLogger("kakarot.monitor")
SEATS_RE = re.compile(r"(\d+)\s+seats? left", re.I)
_brief_lock = threading.Lock()


# ================================================================ daily Lal Sobuj brief
def brief_cfg():
    c = CFG.get("daily_brief") or {}
    return {"enabled": c.get("enabled", True), "time": c.get("time", "07:00"), "low_seats": int(c.get("low_seats", 5))}


def make_brief():
    """Runs the full route check for today and writes a short comparison with the last brief."""
    import bus_report
    if not _brief_lock.acquire(blocking=False):
        return "The morning brief is already running - it takes about 3 minutes."
    try:
        day, rows = bus_report.collect("today")
        full = bus_report.report(save_folder=os.path.join(ROOTS[0], "reports"), collected=(day, rows))
        now = {f"{o} -> {d}": {"n": n, "detail": det} for o, d, n, det, _ in rows}
        prev_row = db.q("SELECT day, data FROM brief_runs WHERE day < ? ORDER BY day DESC LIMIT 1", (str(day),))
        db.x("INSERT OR REPLACE INTO brief_runs(day, data, created) VALUES(?,?,?)",
             (str(day), json.dumps(now, ensure_ascii=False), db.now()))
        cfg = brief_cfg()

        ok = {k: v for k, v in now.items() if v["n"] is not None}
        with_trips = {k: v for k, v in ok.items() if v["n"]}
        total = sum(v["n"] for v in with_trips.values())
        lines = [f"🚌 Lal Sobuj brief - {day:%A %d %b %Y}",
                 f"{len(with_trips)} of {len(ok)} routes have trips today, {total} trips in total."]
        failed = len(now) - len(ok)
        if failed:
            lines.append(f"{failed} route(s) could not be checked.")

        if prev_row:
            prev = json.loads(prev_row[0]["data"])
            pday = prev_row[0]["day"]
            p_total = sum((v["n"] or 0) for v in prev.values())
            diff = total - p_total
            lines.append(f"Compared with {pday}: {'+' if diff >= 0 else ''}{diff} trips ({p_total} then).")
            started = [k for k, v in with_trips.items() if not (prev.get(k) or {}).get("n")]
            stopped = [k for k, v in prev.items() if v.get("n") and ok.get(k, {}).get("n") == 0]
            changed = [f"{k}: {prev[k]['n']} → {v['n']}" for k, v in with_trips.items()
                       if (prev.get(k) or {}).get("n") and prev[k]["n"] != v["n"]]
            if started:
                lines.append("\nNEW today (no trips last time): " + ", ".join(started))
            if stopped:
                lines.append("\nNO TRIPS today (had trips last time): " + ", ".join(stopped))
            if changed:
                lines.append("\nTrip count changed: " + "; ".join(changed))
            if not (started or stopped or changed):
                lines.append("No route changes since last time.")
        else:
            lines.append("(First brief - tomorrow's will show what changed.)")

        low = []
        for k, v in with_trips.items():
            for trip in (v["detail"] or "").split("; "):
                m = SEATS_RE.search(trip)
                if m and int(m.group(1)) <= cfg["low_seats"]:
                    low.append(f"{k}: {trip.split(' | ')[0]} {trip.split(' | ')[1] if ' | ' in trip else ''} "
                               f"- {m.group(1)} seats left")
        if low:
            lines.append(f"\nALMOST FULL (≤{cfg['low_seats']} seats):\n" + "\n".join(f"- {x}" for x in low[:30]))
        busiest = sorted(with_trips.items(), key=lambda kv: -kv[1]["n"])[:5]
        if busiest:
            lines.append("\nBusiest routes: " + ", ".join(f"{k} ({v['n']})" for k, v in busiest))
        csv_line = [ln for ln in full.splitlines() if ln.startswith("Full table saved")]
        if csv_line:
            lines.append("\n" + csv_line[0])
        return "\n".join(lines)
    finally:
        _brief_lock.release()


def run_brief_job():
    import scheduler
    try:
        text = make_brief()
    except Exception as e:
        log.exception("brief failed")
        text = f"The morning brief failed: {e}"
    db.add_inbox("🚌 Lal Sobuj morning brief", text)
    scheduler.notify("Lal Sobuj morning brief", text.split("\n", 2)[1] if "\n" in text else text)
    return text


@tool("lalsobuj_morning_brief",
      "Make the Lal Sobuj daily brief NOW: checks all routes for today and says what changed since the last brief "
      "(new/stopped routes, trip counts, almost-full trips). Takes about 3 minutes.")
def lalsobuj_morning_brief():
    return "[[DIRECT]]" + make_brief()


# ================================================================ website watchdog
def sites():
    return db.q("SELECT * FROM watch_sites ORDER BY added")


def seed_sites():
    """First start only: copy watch_sites from config.json into the database (edit them in the Status tab later)."""
    flag = os.path.join(os.path.dirname(db.DB_PATH), "sites_seeded")
    if os.path.exists(flag):
        return
    open(flag, "w").close()
    for s in CFG.get("watch_sites", []):
        s = {"url": s} if isinstance(s, str) else s
        db.x("INSERT OR IGNORE INTO watch_sites(url, must_contain, added) VALUES(?,?,?)",
             (s["url"], s.get("must_contain", ""), db.now()))


def _cert_days(url):
    host = urlparse(url).hostname
    if urlparse(url).scheme != "https" or not host:
        return None
    ctx = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as s:
            exp = ssl.cert_time_to_seconds(s.getpeercert()["notAfter"])
    return int((exp - time.time()) // 86400)


def check_site(site):
    url, must = site["url"], (site.get("must_contain") or "").strip()
    t0 = time.time()
    code, err = None, ""
    try:
        r = requests.get(url, headers=UA, timeout=25, allow_redirects=True)
        code = r.status_code
        if code >= 400:
            err = f"HTTP {code}"
        elif must and must.lower() not in r.text.lower():
            err = f"page loaded but '{must}' is missing"
    except requests.exceptions.SSLError:
        err = "security certificate (SSL) error"
    except requests.exceptions.Timeout:
        err = "no answer within 25 seconds"
    except requests.exceptions.ConnectionError:
        err = "cannot connect (site or internet down)"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    ms = int((time.time() - t0) * 1000)
    return {"ok": not err, "code": code, "ms": ms, "error": err}


def _internet_ok():
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            socket.create_connection((host, 53), timeout=5).close()
            return True
        except OSError:
            continue
    return False


def watch_all():
    import scheduler
    for site in sites():
        url = site["url"]
        res = check_site(site)
        st = (db.q("SELECT * FROM site_state WHERE url=?", (url,)) or [None])[0]
        now = db.now()
        if not res["ok"] and not _internet_ok():  # the PC is offline, not the website: note it, no alarm
            log.info("%s unreachable but this PC seems offline", url)
            if st:
                db.x("UPDATE site_state SET checked=?, error=? WHERE url=?",
                     (now, "not checked: this PC has no internet", url))
            continue
        db.x("INSERT INTO site_checks(url, ok, ms, code, checked) VALUES(?,?,?,?,?)",
             (url, int(res["ok"]), res["ms"], res["code"], now))
        if not st:
            db.x("INSERT INTO site_state(url, ok, since, checked, ms, code, error, fails, alerted) "
                 "VALUES(?,?,?,?,?,?,?,?,0)", (url, int(res["ok"]), now, now, res["ms"], res["code"], res["error"],
                                               0 if res["ok"] else 1))
            st = db.q("SELECT * FROM site_state WHERE url=?", (url,))[0]
        else:
            fails = 0 if res["ok"] else (st["fails"] or 0) + 1
            since = st["since"] if bool(st["ok"]) == res["ok"] else now
            db.x("UPDATE site_state SET ok=?, since=?, checked=?, ms=?, code=?, error=?, fails=? WHERE url=?",
                 (int(res["ok"]), since, now, res["ms"], res["code"], res["error"], fails, url))
            st = db.q("SELECT * FROM site_state WHERE url=?", (url,))[0]
        # alert after 2 failed checks in a row (avoids false alarms), and once when it's back
        if not res["ok"] and st["fails"] >= 2 and not st["alerted"]:
            msg = f"{url} is DOWN: {res['error']}. Checked twice, {st['since']} onwards."
            db.add_inbox("🔴 Website down", msg)
            scheduler.notify("Website down", msg)
            db.x("UPDATE site_state SET alerted=1 WHERE url=?", (url,))
        elif res["ok"] and st["alerted"]:
            msg = f"{url} is back UP (answered in {res['ms']} ms)."
            db.add_inbox("🟢 Website back up", msg)
            scheduler.notify("Website back up", msg)
            db.x("UPDATE site_state SET alerted=0 WHERE url=?", (url,))
        elif res["ok"] and res["ms"] > int(CFG.get("watch_slow_ms", 8000)):
            log.info("%s slow: %s ms", url, res["ms"])
        # certificate expiry, once a day
        if (st.get("cert_checked") or "")[:10] != now[:10]:
            try:
                days = _cert_days(url)
            except Exception:
                days = None
            db.x("UPDATE site_state SET cert_days=?, cert_checked=? WHERE url=?", (days, now, url))
            if days is not None and days <= 14:
                db.add_inbox("🔐 Certificate expiring", f"The HTTPS certificate of {url} expires in {days} day(s). "
                             "Renew it (Namecheap → SSL Certificates) so visitors don't get a warning.")
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=30)).isoformat(sep=" ", timespec="seconds")
    db.x("DELETE FROM site_checks WHERE checked < ?", (cutoff,))


def site_report():
    out = []
    since = (datetime.datetime.now() - datetime.timedelta(hours=24)).isoformat(sep=" ", timespec="seconds")
    for s in sites():
        st = (db.q("SELECT * FROM site_state WHERE url=?", (s["url"],)) or [{}])[0]
        h = db.q("SELECT COUNT(*) AS n, SUM(ok) AS up, AVG(ms) AS avg FROM site_checks WHERE url=? AND checked>=?",
                 (s["url"], since))[0]
        out.append({"url": s["url"], "must_contain": s["must_contain"], "ok": st.get("ok"),
                    "since": st.get("since"), "checked": st.get("checked"), "ms": st.get("ms"),
                    "error": st.get("error"), "cert_days": st.get("cert_days"),
                    "uptime24": round(100 * (h["up"] or 0) / h["n"], 1) if h["n"] else None,
                    "avg_ms": int(h["avg"]) if h["avg"] else None})
    return out


@tool("website_status", "Show whether the watched websites are up right now, response time, 24h uptime and "
                        "certificate expiry. Checks them fresh first.")
def website_status():
    if not sites():
        return "No websites are being watched. Use watch_website to add one."
    watch_all()
    lines = []
    for s in site_report():
        if s["ok"] is None:
            lines.append(f"- {s['url']}: not checked yet ({s['error'] or 'no result'})")
            continue
        state = "UP" if s["ok"] else f"DOWN ({s['error']})"
        lines.append(f"- {s['url']}: {state}, {s['ms']} ms, since {s['since']}; 24h uptime "
                     f"{s['uptime24'] if s['uptime24'] is not None else '?'}%"
                     + (f"; certificate valid {s['cert_days']} more days" if s["cert_days"] is not None else ""))
    return "\n".join(lines)


@tool("watch_website", "Start watching a website: Kakarot checks it every few minutes and alerts when it goes down.",
      {"url": S("Full URL, e.g. https://example.com"),
       "must_contain": S("Optional text that must be on the page (e.g. 'Login') - if missing it counts as broken.")},
      ["url"])
def watch_website(url, must_contain=""):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    db.x("INSERT OR REPLACE INTO watch_sites(url, must_contain, added) VALUES(?,?,?)", (url, must_contain, db.now()))
    res = check_site({"url": url, "must_contain": must_contain})
    return f"Now watching {url} every {CFG.get('watch_every_minutes', 5)} minutes. Right now it is " + \
        ("UP" if res["ok"] else f"DOWN ({res['error']})") + f", {res['ms']} ms."


@tool("unwatch_website", "Stop watching a website.", {"url": S("The URL being watched.")})
def unwatch_website(url):
    db.x("DELETE FROM watch_sites WHERE url=?", (url,))
    db.x("DELETE FROM site_state WHERE url=?", (url,))
    return f"Stopped watching {url}."
