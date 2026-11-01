"""Use the PC's screen like a person: see what's on it, click, type, press keys, scroll, open apps and switch windows.

How it stays safe:
- Nothing happens until you approve a card in the chat ("Kakarot wants to use your screen for: ..."). The approval
  lasts `screen_grant_minutes` (default 15) for that job only.
- Take over any time: tap Stop in the chat page, or just move the mouse yourself. Kakarot notices you moved it and
  stops. Moving the mouse into a screen corner also stops it.
- Clicks on buttons like Send, Delete, Pay, Buy, Submit, Uninstall ask for a separate OK first.
- It never types into password fields, and scheduled/background jobs can't use the screen.

How it sees: a screenshot is read by the picture brain, and Windows' own accessibility list gives every button,
field and menu of the active window with its exact position (numbered, like web pages). Things that aren't in that
list (games, pictures, canvas apps) can be clicked by describing them; the picture brain finds them.
"""
import ctypes
import io
import json
import logging
import os
import re
import sys
import threading
import time

import db
from config import CFG
from tools import CTX, I, ROOTS, S, _cut, tool

log = logging.getLogger("kakarot.desktop")
WIN = sys.platform == "win32"

RISKY = re.compile(r"\b(send|delete|remove|erase|pay|payment|buy|purchase|order now|checkout|place order|submit|"
                   r"confirm|transfer|withdraw|uninstall|format|reset|sign out|log ?out|shut ?down|restart|empty "
                   r"recycle|publish|post|পাঠান|পাঠাও|মুছুন|মুছে|কিনুন|পেমেন্ট|নিশ্চিত)\b", re.I)
INTERACTIVE = {"ButtonControl", "EditControl", "MenuItemControl", "ListItemControl", "TabItemControl",
               "HyperlinkControl", "CheckBoxControl", "RadioButtonControl", "ComboBoxControl", "TreeItemControl",
               "SplitButtonControl", "DataItemControl", "DocumentControl", "SliderControl", "SpinnerControl",
               "MenuBarControl", "ToolBarControl"}
BLOCKED_KEYS = {"ctrl+alt+delete", "ctrl+alt+del"}


# ================================================================ the Windows side (mouse, keyboard, screen)
class WindowsBackend:
    def __init__(self):
        try:  # real pixels on high-DPI screens, so screenshot and click positions match
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        import uiautomation as auto  # noqa: F401  (pip install uiautomation)
        self.auto = auto

    def screenshot(self):
        from PIL import ImageGrab
        return ImageGrab.grab()

    def cursor(self):
        import ctypes.wintypes as wt
        pt = wt.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def screen_size(self):
        u = ctypes.windll.user32
        return u.GetSystemMetrics(0), u.GetSystemMetrics(1)

    def _com(self):
        """Windows' accessibility API must be set up in every thread that uses it (web requests run in threads)."""
        return self.auto.UIAutomationInitializerInThread()

    def window(self):
        c = self.auto.GetForegroundControl()
        top = c.GetTopLevelControl() if c else None
        return top

    def window_title(self):
        with self._com():
            w = self.window()
            return (w.Name if w else "") or ""

    def window_center(self):
        with self._com():
            w = self.window()
            if not w:
                return None, None
            r = w.BoundingRectangle
            return (r.left + r.right) // 2, (r.top + r.bottom) // 2

    def elements(self, limit=80, seconds=3.0):
        with self._com():
            return self._elements(limit, seconds)

    def _elements(self, limit, seconds):
        w = self.window()
        if not w:
            return []
        out, t0 = [], time.time()
        sw, sh = self.screen_size()
        for ctrl, depth in self.auto.WalkControl(w, maxDepth=14):
            if time.time() - t0 > seconds or len(out) >= limit:
                break
            try:
                ct = ctrl.ControlTypeName
                if ct not in INTERACTIVE:
                    continue
                r = ctrl.BoundingRectangle
                if r.width() <= 2 or r.height() <= 2 or r.right < 0 or r.bottom < 0 or r.left > sw or r.top > sh:
                    continue
                if getattr(ctrl, "IsOffscreen", False):
                    continue
                name = (ctrl.Name or "").strip()
                value, pwd = "", False
                if ct == "EditControl":
                    pwd = bool(getattr(ctrl, "IsPassword", False))
                    if not pwd:
                        try:
                            value = ctrl.GetValuePattern().Value or ""
                        except Exception:
                            value = ""
                if not name and not value and ct not in ("EditControl", "DocumentControl"):
                    continue
                out.append({"type": ct.replace("Control", ""), "name": name[:80], "value": value[:80],
                            "password": pwd, "x": (r.left + r.right) // 2, "y": (r.top + r.bottom) // 2,
                            "rect": [r.left, r.top, r.right, r.bottom],
                            "enabled": bool(getattr(ctrl, "IsEnabled", True))})
            except Exception:
                continue
        return out

    def windows(self):
        out = []
        with self._com():
            for c in self.auto.GetRootControl().GetChildren():
                try:
                    if c.Name and c.ControlTypeName == "WindowControl" and not c.IsOffscreen:
                        out.append(c.Name)
                except Exception:
                    continue
        return out

    def focus(self, title):
        with self._com():
            for c in self.auto.GetRootControl().GetChildren():
                if c.Name and title.lower() in c.Name.lower():
                    try:
                        c.SetActive()
                        c.SetFocus()
                    except Exception:
                        pass
                    return c.Name
        return None

    def click(self, x, y, button="left", double=False):
        a = self.auto
        if button == "right":
            a.RightClick(x, y)
        elif double:
            a.DoubleClick(x, y)
        else:
            a.Click(x, y)

    def scroll(self, direction, amount, x=None, y=None):
        if x is not None:
            self.auto.MoveTo(x, y)
        (self.auto.WheelDown if direction == "down" else self.auto.WheelUp)(wheelTimes=max(1, int(amount)))

    def type_text(self, text):
        _send_unicode(text)

    def keys(self, combo):
        _send_combo(combo)


# ---- raw keyboard input (works for Bangla and any Unicode text)
VK = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B, "enter": 0x0D,
      "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08, "delete": 0x2E,
      "del": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
      "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D, "printscreen": 0x2C, "capslock": 0x14}
VK.update({f"f{i}": 0x6F + i for i in range(1, 13)})


def _send_inputs(items):
    import ctypes.wintypes as wt

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wt.DWORD), ("u", _U)]

    arr = (INPUT * len(items))()
    for i, (vk, scan, flags) in enumerate(items):
        arr[i].type = 1
        arr[i].ki = KEYBDINPUT(vk, scan, flags, 0, None)
    ctypes.windll.user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))


def _send_unicode(text):
    KEYUP, UNICODE = 0x0002, 0x0004
    for ch in text:
        if ch == "\n":
            _send_inputs([(0x0D, 0, 0), (0x0D, 0, KEYUP)])
        else:
            for unit in _utf16(ch):
                _send_inputs([(0, unit, UNICODE), (0, unit, UNICODE | KEYUP)])
        time.sleep(0.008)


def _utf16(ch):
    b = ch.encode("utf-16-le")
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]


def _send_combo(combo):
    KEYUP = 0x0002
    parts = [p.strip().lower() for p in re.split(r"\+", combo) if p.strip()]
    codes = []
    for p in parts:
        if p in VK:
            codes.append(VK[p])
        elif len(p) == 1:
            codes.append(ctypes.windll.user32.VkKeyScanW(ord(p)) & 0xFF)
        else:
            raise ValueError(f"Unknown key '{p}'. Use names like ctrl, alt, shift, win, enter, tab, esc, f5, a-z.")
    _send_inputs([(c, 0, 0) for c in codes] + [(c, 0, KEYUP) for c in reversed(codes)])


# ================================================================ state: approval, stop, take-over
_state = {"until": 0, "goal": "", "stopped": False, "last_cursor": None, "last_shot": None, "elements": [],
          "shot_size": None, "actions": 0}
_lock = threading.Lock()
_backend = None


def backend():
    global _backend
    if _backend is None:
        if not WIN:
            raise RuntimeError("Screen control works on Windows only.")
        try:
            _backend = WindowsBackend()
        except ImportError:
            raise RuntimeError("Screen control needs one extra package. Run update.bat (or: pip install uiautomation) "
                               "and restart Kakarot.")
    return _backend


def status():
    return {"active": _state["until"] > time.time() and not _state["stopped"], "goal": _state["goal"],
            "until": _state["until"], "actions": _state["actions"], "enabled": CFG.get("screen_control", True)}


def stop(reason="You stopped it."):
    _state.update(until=0, stopped=True, stop_reason=reason)


def _ask(kind, detail, reason):
    import pctools
    return pctools._ask(kind, detail, reason)


def _need_grant(goal):
    if not CFG.get("screen_control", True):
        raise PermissionError("Screen control is switched off in config.json (screen_control).")
    if CTX.get("background"):
        raise PermissionError("Background and scheduled jobs can't use the screen (nobody is there to watch).")
    if _state["until"] > time.time() and not _state["stopped"]:
        return
    mins = int(CFG.get("screen_grant_minutes", 15))
    ok = _ask("screen", json.dumps({"goal": goal or "a task on your PC", "minutes": mins}, ensure_ascii=False),
              f"Kakarot will move the mouse and type on your PC for up to {mins} minutes.")
    if ok is None:
        raise PermissionError("Nobody approved using the screen in time, so I didn't touch it.")
    if not ok:
        raise PermissionError("You denied using the screen. I won't touch it; ask me another way.")
    _state.update(until=time.time() + mins * 60, goal=goal or "", stopped=False, last_cursor=None, actions=0)


def _check_takeover():
    """Stop if the user moved the mouse since our last action (they took over) or parked it in a corner."""
    b = backend()
    x, y = b.cursor()
    w, h = b.screen_size()
    if (x <= 2 and y <= 2) or (x >= w - 3 and y <= 2):
        stop("You moved the mouse into a top corner.")
    last = _state.get("last_cursor")
    if last and abs(x - last[0]) + abs(y - last[1]) > int(CFG.get("screen_takeover_pixels", 60)):
        stop("You moved the mouse, so I let you take over.")
    if _state["stopped"] or _state["until"] <= time.time():
        raise PermissionError(f"Screen control has stopped ({_state.get('stop_reason') or 'time is up'}). "
                              "Ask the user before using the screen again.")


def _after_action():
    _state["actions"] += 1
    try:
        _state["last_cursor"] = backend().cursor()
    except Exception:
        pass
    time.sleep(float(CFG.get("screen_settle_seconds", 0.6)))


def _shots_dir():
    d = os.path.join(ROOTS[0], "screens")
    os.makedirs(d, exist_ok=True)
    return d


def capture():
    """Screenshot -> saved as latest.jpg (for the live view) + kept in memory."""
    img = backend().screenshot().convert("RGB")
    _state["last_shot"] = img
    _state["shot_size"] = img.size
    small = img.copy()
    small.thumbnail((1600, 1600))
    small.save(os.path.join(_shots_dir(), "latest.jpg"), "JPEG", quality=80)
    return img


def latest_jpeg():
    p = os.path.join(_shots_dir(), "latest.jpg")
    return p if os.path.exists(p) else None


def _is_dark(img):
    g = img.convert("L").resize((64, 36))
    return max(g.getdata()) < 12


def _b64(img, side=1400):
    import base64
    im = img.copy()
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode(), im.size


def _element_list(els):
    lines = []
    for i, e in enumerate(els, 1):
        bits = f"[{i}] {e['type']}: {e['name'] or '(no name)'}"
        if e.get("password"):
            bits += " (PASSWORD FIELD - never type here)"
        elif e.get("value"):
            bits += f" = \"{e['value']}\""
        if not e.get("enabled", True):
            bits += " (disabled)"
        lines.append(bits)
    return "\n".join(lines)


LOOK_PROMPT = """This is a screenshot of the user's Windows PC. Window in front: "{title}".
Describe briefly what is on the screen: which app/page, the main content, any dialog, error, pop-up or message.
{question}
Be factual; read important text exactly."""

FIND_PROMPT = """This screenshot is {w}x{h} pixels. Find this on the screen: "{what}".
Reply ONLY with JSON: {{"found": true or false, "x": center x in pixels, "y": center y in pixels, "label": "what you found"}}"""


# ================================================================ tools
@tool("screen_look",
      "SEE THE PC SCREEN: takes a screenshot, describes what is on it, and lists the buttons, fields, menus and links of "
      "the window in front with numbers (use those numbers with screen_click / screen_type). Call it first, and again "
      "after every change to check the result. Also answers a question about the screen.",
      {"question": S("Optional: what you want to know (e.g. 'is there an error message?', 'what is the total?')."),
       "goal": S("Short description of the whole task you are doing for the user (shown on the approval card).")},
      [])
def screen_look(question="", goal=""):
    _need_grant(goal or question)
    b = backend()
    img = capture()
    if _is_dark(img):
        return "The screen is black - the PC is probably locked or the display is asleep. Ask the user to unlock it."
    title = b.window_title()
    els = b.elements(limit=int(CFG.get("screen_max_elements", 80)))
    _state["elements"] = els
    desc = ""
    if CFG.get("screen_use_vision", True):
        try:
            import vision
            data, _ = _b64(img)
            desc = vision.ask_vision([data], LOOK_PROMPT.format(title=title, question=(
                f"Then answer: {question}" if question else "")))
        except Exception as e:
            desc = f"(picture brain not available: {e})"
    _state["last_cursor"] = b.cursor()
    wins = ", ".join(w for w in b.windows()[:12] if w != title)
    return _cut(f"Window in front: {title}\nOther open windows: {wins or '-'}\nScreen: {img.size[0]}x{img.size[1]}\n\n"
                f"What I see:\n{desc or '-'}\n\nClickable things in this window (use the numbers):\n"
                f"{_element_list(els) or '(none found - use screen_click with a description, or keys)'}", 7000)


def _target(element=0, description="", x=None, y=None):
    """Resolve where to click. Returns (x, y, label)."""
    if element:
        els = _state.get("elements") or []
        if not 1 <= int(element) <= len(els):
            raise ValueError(f"There is no element [{element}]. Call screen_look again to get fresh numbers.")
        e = els[int(element) - 1]
        return e["x"], e["y"], f"{e['type']} '{e['name']}'", e
    if x is not None and y is not None and (x or y):
        return int(x), int(y), f"point {x},{y}", None
    if description:
        import vision
        img = _state.get("last_shot") or capture()
        data, (w, h) = _b64(img)
        raw = vision.ask_vision([data], FIND_PROMPT.format(w=w, h=h, what=description))
        m = re.search(r"\{.*\}", raw or "", re.S)
        r = json.loads(m.group(0)) if m else {}
        if not r.get("found"):
            raise ValueError(f"I couldn't find '{description}' on the screen. Call screen_look and pick a number.")
        sx, sy = img.size[0] / w, img.size[1] / h
        return int(float(r["x"]) * sx), int(float(r["y"]) * sy), r.get("label") or description, None
    raise ValueError("Say what to click: an element number from screen_look, or a description.")


@tool("screen_click",
      "CLICK on the PC screen: an element number from screen_look (best), or describe what to click (e.g. 'the blue "
      "Save button', 'the Chrome icon on the taskbar'). Buttons like Send/Delete/Pay/Submit ask the user first.",
      {"element": I("Number from screen_look's list (0 = not using a number)."),
       "description": S("If there's no number: describe the thing to click."),
       "button": S("left (default), right, or double.")}, [])
def screen_click(element=0, description="", button="left"):
    _need_grant(description)
    _check_takeover()
    x, y, label, el = _target(element, description)
    name = (el or {}).get("name", "") or description or label
    import approve
    k = approve.kind_of(name)
    if k:
        approve.require(k, f"Click '{name}' in {backend().window_title() or 'the window in front'}", [name],
                        reason="This click may delete or change something on your PC.")
    elif RISKY.search(name or ""):
        ok = _ask("screen_action", json.dumps({"action": f"Click '{name}'", "window": backend().window_title()},
                                              ensure_ascii=False), "This click may send, delete, pay or submit something.")
        if not ok:
            return f"The user did not allow clicking '{name}'. It was NOT clicked."
    backend().click(x, y, button="right" if button == "right" else "left", double=button == "double")
    _after_action()
    return f"Clicked {label}. Call screen_look to see what happened."


@tool("screen_type",
      "TYPE text on the PC (any language, Bangla too): optionally click a field number first. Set enter=true to "
      "press Enter after. Never used for passwords or card numbers.",
      {"text": S("Exactly what to type."), "element": I("Optional field number from screen_look to click first."),
       "enter": {"type": "boolean", "description": "Press Enter after typing."}}, ["text"])
def screen_type(text, element=0, enter=False):
    _need_grant("typing")
    _check_takeover()
    if element:
        x, y, label, el = _target(element)
        if el and el.get("password"):
            return "That is a password field. I never type passwords - ask the user to type it themselves."
        backend().click(x, y)
        time.sleep(0.25)
    if re.search(r"\b(?:\d[ -]?){13,19}\b", text):
        return "That looks like a card number. I don't type payment details - ask the user to type it."
    backend().type_text(text)
    if enter:
        backend().keys("enter")
    _after_action()
    return f"Typed {len(text)} characters" + (" and pressed Enter" if enter else "") + ". Check with screen_look."


@tool("screen_key",
      "PRESS KEYS on the PC: one key or a combination, e.g. 'enter', 'esc', 'tab', 'ctrl+s', 'ctrl+c', 'alt+tab', "
      "'win', 'ctrl+shift+n', 'f5', 'pagedown'.",
      {"keys": S("Key or combination joined with +."), "times": I("How many times (default 1).")}, ["keys"])
def screen_key(keys, times=1):
    _need_grant(keys)
    _check_takeover()
    k = keys.lower().replace(" ", "")
    if k in BLOCKED_KEYS:
        return "That key combination isn't allowed."
    import approve
    if k in ("delete", "del", "shift+delete", "shift+del", "ctrl+x", "ctrl+d"):
        approve.require("delete", f"Press {keys} in {backend().window_title() or 'the window in front'}", [keys],
                        reason="This key deletes or cuts what is selected.")
    elif k in ("ctrl+s", "ctrl+shift+s", "f2", "ctrl+h", "ctrl+z", "ctrl+y"):
        approve.require("edit", f"Press {keys} in {backend().window_title() or 'the window in front'}", [keys],
                        reason="This key saves, renames or changes something.")
    for _ in range(max(1, min(int(times or 1), 30))):
        backend().keys(k)
        time.sleep(0.12)
    _after_action()
    return f"Pressed {keys}" + (f" {times} times" if int(times or 1) > 1 else "") + "."


@tool("screen_scroll", "SCROLL the window in front up or down.",
      {"direction": S("up or down"), "amount": I("How much (1-20, default 5).")}, ["direction"])
def screen_scroll(direction, amount=5):
    _need_grant("scrolling")
    _check_takeover()
    b = backend()
    x, y = b.window_center()
    b.scroll("up" if str(direction).lower().startswith("u") else "down", min(int(amount or 5), 20), x, y)
    _after_action()
    return f"Scrolled {direction}."


@tool("open_app",
      "OPEN AN APP on the PC by name (Excel, Word, Notepad, Calculator, Chrome, File Explorer, Settings, WhatsApp...) "
      "using the Start menu, then wait for it.",
      {"name": S("App name as in the Start menu.")})
def open_app(name):
    _need_grant(f"open {name}")
    _check_takeover()
    b = backend()
    b.keys("win")
    time.sleep(0.9)
    b.type_text(name)
    time.sleep(1.4)
    b.keys("enter")
    time.sleep(float(CFG.get("screen_app_wait_seconds", 3)))
    _after_action()
    return f"Opened '{name}' from the Start menu (window in front now: {b.window_title() or '?'}). Call screen_look."


@tool("switch_window", "BRING A WINDOW TO THE FRONT by (part of) its title, e.g. 'Excel', 'Chrome', 'Notepad'.",
      {"title": S("Part of the window title.")})
def switch_window(title):
    _need_grant(f"switch to {title}")
    _check_takeover()
    got = backend().focus(title)
    _after_action()
    if not got:
        return f"No open window matches '{title}'. Open windows: " + ", ".join(backend().windows()[:15])
    return f"Switched to: {got}. Call screen_look."


@tool("screen_done", "Call when the screen task is finished (or you must stop), so the user gets the mouse back.",
      {"summary": S("One line: what you did.")})
def screen_done(summary=""):
    stop("Task finished.")
    return "Screen control released. " + summary
