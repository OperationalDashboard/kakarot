"""Website control: a real browser Kakarot keeps open between steps, so it can click, type and fill forms.
Playwright objects must stay on one thread, so a single worker thread owns the browser."""
import datetime
import os
import queue
import threading

from config import CFG
from tools import B, I, ROOTS, S, _cut, tool

_jobs = queue.Queue()
_worker = None
_state = {"pw": None, "browser": None, "page": None}

MARK_JS = r"""
() => {
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link], [role=option],' +
              '[role=combobox], [role=menuitem], [role=tab], [role=checkbox], [onclick], [contenteditable=true]';
  const out = []; let n = 0;
  document.querySelectorAll('[data-kk]').forEach(e => e.removeAttribute('data-kk'));
  for (const e of document.querySelectorAll(sel)) {
    const r = e.getBoundingClientRect(), st = getComputedStyle(e);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    n += 1; e.setAttribute('data-kk', n);
    const tag = e.tagName.toLowerCase(), role = e.getAttribute('role') || '';
    let kind = role || (tag === 'a' ? 'link' : tag === 'input' ? 'input:' + (e.type || 'text') : tag);
    let label = (e.innerText || e.value || e.getAttribute('aria-label') || e.getAttribute('placeholder') ||
                 e.getAttribute('title') || e.getAttribute('name') || '').trim().replace(/\s+/g, ' ').slice(0, 50);
    if (tag === 'select') label += ' [options: ' + [...e.options].map(o => o.text.trim()).slice(0, 15).join(' | ') + ']';
    if (e.type === 'checkbox' || e.type === 'radio') label += e.checked ? ' (checked)' : ' (unchecked)';
    out.push('[' + n + '] ' + kind + ' "' + label + '"');
    if (n >= 80) break;
  }
  return out.join('\n');
}
"""


def _loop():
    while True:
        fn, box, done = _jobs.get()
        try:
            box["ok"] = fn()
        except Exception as e:  # report errors back to the caller
            box["err"] = e
        done.set()


def _run(fn, timeout=120):
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_loop, daemon=True, name="kakarot-browser")
        _worker.start()
    box, done = {}, threading.Event()
    _jobs.put((fn, box, done))
    if not done.wait(timeout):
        raise TimeoutError("The browser took too long.")
    if "err" in box:
        raise box["err"]
    return box["ok"]


def _page():
    if _state["page"] is None or _state["page"].is_closed():
        if _state["browser"] is None:
            from playwright.sync_api import sync_playwright
            _state["pw"] = sync_playwright().start()
            _state["browser"] = _state["pw"].chromium.launch(headless=not CFG.get("browser_visible", False))
        ctx = _state["browser"].new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
        _state["page"] = ctx.new_page()
    return _state["page"]


def _settle(p):
    try:
        p.wait_for_load_state("domcontentloaded", timeout=15000)
        p.wait_for_timeout(1200)
    except Exception:
        pass


def _snapshot(p, note=""):
    text = p.inner_text("body")
    items = p.evaluate(MARK_JS)
    return (f"{note}Page: {p.title()} ({p.url})\n\nCLICKABLE / TYPEABLE ELEMENTS (use their numbers):\n{items or '(none)'}"
            f"\n\nPAGE TEXT:\n{_cut(text, 1800)}")


def _el(p, n):
    loc = p.locator(f'[data-kk="{int(n)}"]')
    if not loc.count():
        raise ValueError(f"No element [{n}] - call web_read to get fresh numbers.")
    return loc.first


@tool("web_open", "Open a website in Kakarot's own browser (stays open between steps) and list its clickable elements "
                  "with numbers. Use this when you need to CLICK, TYPE, FILL FORMS or pick options on a site.",
      {"url": S("Full URL.")})
def web_open(url):
    def go():
        p = _page()
        p.goto(url, wait_until="domcontentloaded", timeout=60000)
        _settle(p)
        return _snapshot(p)
    return _run(go)


@tool("web_read", "Re-read the current page in Kakarot's browser: text plus numbered clickable elements.")
def web_read():
    return _run(lambda: _snapshot(_page()))


@tool("web_click", "Click element [number] on the current page (numbers come from web_open/web_read).",
      {"number": I("Element number.")})
def web_click(number):
    def go():
        p = _page()
        _el(p, number).click(timeout=15000)
        _settle(p)
        return _snapshot(p, f"Clicked [{number}].\n")
    return _run(go)


@tool("web_type", "Type text into input/box [number]. Set press_enter true to submit (e.g. a search box).",
      {"number": I("Element number."), "text": S("Text to type."), "press_enter": B("Press Enter after typing.")},
      ["number", "text"])
def web_type(number, text, press_enter=False):
    def go():
        p = _page()
        el = _el(p, number)
        el.fill(str(text), timeout=15000)
        if press_enter:
            el.press("Enter")
        _settle(p)
        return _snapshot(p, f"Typed into [{number}].\n")
    return _run(go)


@tool("web_select", "Choose an option in dropdown [number] by its visible text.",
      {"number": I("Element number of the <select>."), "option": S("Visible option text.")})
def web_select(number, option):
    def go():
        p = _page()
        _el(p, number).select_option(label=str(option), timeout=15000)
        _settle(p)
        return _snapshot(p, f"Selected '{option}' in [{number}].\n")
    return _run(go)


@tool("web_scroll", "Scroll the current page down (or up) to reveal more content.",
      {"direction": S("'down' or 'up'.")}, [])
def web_scroll(direction="down"):
    def go():
        p = _page()
        p.mouse.wheel(0, 900 if direction != "up" else -900)
        p.wait_for_timeout(800)
        return _snapshot(p)
    return _run(go)


@tool("web_back", "Go back to the previous page in Kakarot's browser.")
def web_back():
    def go():
        p = _page()
        p.go_back()
        _settle(p)
        return _snapshot(p)
    return _run(go)


@tool("web_screenshot", "Save a screenshot of the current page in Kakarot's browser into the workspace.")
def web_screenshot():
    def go():
        p = _page()
        folder = os.path.join(ROOTS[0], "screenshots")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".png")
        p.screenshot(path=path, full_page=True)
        return f"Screenshot saved: {path}"
    return _run(go)


@tool("web_close", "Close Kakarot's browser when finished with a website.")
def web_close():
    def go():
        if _state["browser"]:
            _state["browser"].close()
        if _state["pw"]:
            _state["pw"].stop()
        _state.update(pw=None, browser=None, page=None)
        return "Browser closed."
    return _run(go)
