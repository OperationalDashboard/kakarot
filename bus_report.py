"""Lal Sobuj trip report: checks every route (rotation) on lalsobujbus.com for a date
and counts the trips, all in one go with a hidden browser (several routes at the same time)."""
import asyncio
import csv
import datetime
import os
import re

SITE = "https://www.lalsobujbus.com"
PARALLEL = 4
TRIP_RE = re.compile(r"Total\s+(\d+)\s+trips?\s+found", re.I)
NONE_RE = re.compile(r"No departures found", re.I)
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s?[AP]M)\b")
SEATS_RE = re.compile(r"(\d+)\s+seats? left", re.I)


def _trips(text):
    """Split the results text into one block per bus and pull out the details."""
    out = []
    for block in re.split(r"\n\s*Lal Sobuj Paribahan\s*\n", "\n" + text)[1:]:
        lines = [l.strip() for l in block.splitlines() if l.strip() and l.strip() != "•"]
        times = TIME_RE.findall(block)
        seats = SEATS_RE.search(block)
        t = {"code": lines[0] if lines else "", "coach": lines[1] if len(lines) > 1 else "",
             "dep": times[0] if times else "", "arr": times[1] if len(times) > 1 else "",
             "seats": seats.group(1) if seats else "", "from": "", "fare": ""}
        if t["dep"] and t["dep"] in lines:
            i = lines.index(t["dep"])
            t["from"] = lines[i + 1] if i + 1 < len(lines) else ""
        fares = re.findall(r"BDT\s*([\d,]+(?:\.\d+)?)", block)
        t["fare"] = fares[-1] if fares else ""
        out.append(t)
    return out


def _trip_line(t):
    bits = [t["code"], t["dep"] and f"{t['dep']}" + (f" from {t['from']}" if t["from"] else "")]
    bits += [t["coach"], t["seats"] and f"{t['seats']} seats left", t["fare"] and f"BDT {t['fare']}"]
    return " | ".join(b for b in bits if b)


def slug(name):
    s = name.lower().replace("'", "").replace(".", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


async def _options(page):
    await page.locator("[role=option]:visible").first.wait_for(timeout=8000)
    return [t.strip() for t in await page.locator("[role=option]:visible").all_inner_texts() if t.strip()]


async def _route_list(browser):
    """Read origins, then each origin's destinations, from the search box on the homepage."""
    page = await browser.new_page()
    try:
        await page.goto(SITE + "/", wait_until="domcontentloaded", timeout=60000)
        boxes = page.locator("[role=combobox]:visible")
        await boxes.first.wait_for(timeout=20000)
        await boxes.nth(0).click()
        origins = await _options(page)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
        routes = []
        for o in origins:
            try:
                await boxes.nth(0).click()
                await page.get_by_role("option", name=o, exact=True).first.click()
                await page.wait_for_timeout(1200)  # destinations load; the site opens that list by itself
                if not await page.locator("[role=option]:visible").count():
                    await boxes.nth(1).click()
                dests = await _options(page)
            except Exception:
                dests = []
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            routes += [(o, d) for d in dests if d != o]
        if not routes and origins:  # could not read the destination lists: try every pair instead
            routes = [(o, d) for o in origins for d in origins if o != d]
        return routes
    finally:
        await page.close()


async def _check(browser, sem, origin, dest, date_label):
    url = f"{SITE}/departures/{slug(origin)}-to-{slug(dest)}"
    async with sem:
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_function(
                "() => /trips? found|No departures found/i.test(document.body.innerText)", timeout=30000)
            if date_label:  # e.g. "31 Oct" -> click that day in the date bar
                await page.get_by_text(date_label, exact=True).first.click()
                await page.wait_for_timeout(1500)
                await page.wait_for_function(
                    "() => /trips? found|No departures found/i.test(document.body.innerText)", timeout=30000)
            text = await page.inner_text("body")
            m = TRIP_RE.search(text)
            n = int(m.group(1)) if m else 0
            if not m and not NONE_RE.search(text):
                return origin, dest, None, "", "could not read page"
            detail = "; ".join(_trip_line(t) for t in _trips(text[m.end():])) if n else ""
            return origin, dest, n, detail, ""
        except Exception as e:
            return origin, dest, None, "", f"error: {type(e).__name__}"
        finally:
            await page.close()


async def _run(date_label):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            routes = await _route_list(browser)
            sem = asyncio.Semaphore(PARALLEL)
            return await asyncio.gather(*[_check(browser, sem, o, d, date_label) for o, d in routes])
        finally:
            await browser.close()


def _date_label(date):
    """'' for today, else the label shown in the site's date bar, like '31 Oct'."""
    if not date or date.strip().lower() in ("today", ""):
        return "", datetime.date.today()
    d = date.strip().lower()
    if d == "tomorrow":
        day = datetime.date.today() + datetime.timedelta(days=1)
    else:
        day = datetime.date.fromisoformat(d)
    if day == datetime.date.today():
        return "", day
    return f"{day.day} {day:%b}", day


def collect(date="today"):
    """All routes for a date: (day, [(origin, dest, trips or None, detail, error), ...])."""
    label, day = _date_label(date)
    rows = asyncio.run(_run(label))
    return day, sorted(rows, key=lambda r: (-(r[2] or 0), r[0], r[1]))


def report(date="today", save_folder=None, collected=None):
    day, rows = collected or collect(date)
    with_trips = [r for r in rows if r[2]]
    without = [r for r in rows if r[2] == 0]
    failed = [r for r in rows if r[2] is None]

    saved = ""
    if save_folder:
        os.makedirs(save_folder, exist_ok=True)
        path = os.path.join(save_folder, f"lalsobuj-trips-{day:%Y-%m-%d}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Origin", "Destination", "Trips", "Trip codes & departure", "Note"])
            w.writerows(rows)
        saved = f"\nFull table saved: {path}"

    lines = [f"Lal Sobuj trip report for {day:%a %d %b %Y} - {len(rows)} routes checked",
             f"\nROUTES WITH TRIPS ({len(with_trips)}, {sum(r[2] for r in with_trips)} trips in total):"]
    def short(det):
        return ", ".join(" ".join(x.split(" | ")[:2]).replace(" from ", " ") for x in det.split("; ") if x)
    lines += [f"- {o} -> {d}: {n} trip{'s' if n > 1 else ''}" + (f" ({short(det)})" if det else "")
              for o, d, n, det, _ in with_trips] or ["- none"]
    lines += [f"\nROUTES WITH NO TRIPS ({len(without)}):",
              ", ".join(f"{o} -> {d}" for o, d, *_ in without) or "none"]
    if failed:
        lines += [f"\nCOULD NOT CHECK ({len(failed)}):", ", ".join(f"{o} -> {d} ({e})" for o, d, _, _, e in failed)]
    return "\n".join(lines) + saved


async def _one(origin, dest, date_label):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            return await _check(browser, asyncio.Semaphore(1), origin, dest, date_label)
        finally:
            await browser.close()


def route(origin, destination, date="today"):
    """Trips for ONE origin -> destination on one date."""
    label, day = _date_label(date)
    o, d, n, detail, err = asyncio.run(_one(origin.strip(), destination.strip(), label))
    head = f"{o} -> {d} on {day:%a %d %b %Y}: "
    if n is None:
        return head + f"could not check ({err}). Check the city names are spelled as on the website."
    if not n:
        return head + "no trips."
    return head + f"{n} trip{'s' if n > 1 else ''}\n" + "\n".join(f"- {x}" for x in detail.split("; "))
