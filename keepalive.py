"""Keeps Kakarot available: it doesn't stop when the PC wants to sleep, comes back after a restart, and keeps the
brain ready.

- Keeps the PC awake while Kakarot is working (and, when plugged in, all the time so your phone can always reach
  it). Only the PC stays awake - the screen still turns off as usual.  "keep_pc_awake": "plugged_in" (default),
  "always", "while_working" or "never".
- Starts with Windows (minimized) - run remove-from-startup.bat to stop that.  "auto_start": true.
- Starts Ollama (the brain's engine) again if it stopped.
- Loads the brain shortly after start so the first reply is quick.
"""
import ctypes
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

import requests

from config import BASE, CFG

log = logging.getLogger("kakarot.keepalive")
WIN = sys.platform == "win32"
_busy = 0
_guard = threading.Lock()
STATE = {"awake": False, "ollama_restarts": 0}


def busy(on):
    global _busy
    with _guard:
        _busy = max(0, _busy + (1 if on else -1))


def is_busy():
    if _busy:
        return True
    try:
        import db
        return bool(db.q("SELECT 1 FROM missions WHERE status IN ('running','queued') LIMIT 1"))
    except Exception:
        return False


# ---------------------------------------------------------------- don't let the PC sleep
class _Power(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def plugged_in():
    if not WIN:
        return True
    p = _Power()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(p)):
        return True
    return p.ACLineStatus != 0  # 1 = plugged in, 255 = unknown (desktop PCs)


def want_awake():
    mode = str(CFG.get("keep_pc_awake", "plugged_in")).lower()
    if mode == "never":
        return False
    if mode == "always" or is_busy():
        return True
    if mode == "plugged_in":
        return plugged_in()
    return False


def _awake_loop():
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    while True:
        try:
            want = want_awake()
            if WIN:  # this setting belongs to this thread, so it is always set from here
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if want else 0))
            if want != STATE["awake"]:
                log.info("keeping the PC awake: %s", "yes" if want else "no")
            STATE["awake"] = want
        except Exception as e:
            log.debug("awake check failed: %s", e)
        time.sleep(20)


# ---------------------------------------------------------------- start with Windows
def startup_link():
    return os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup\Kakarot.lnk")


def ensure_autostart():
    """Add the 'start with Windows' shortcut if it's missing (unless the user turned it off)."""
    if not WIN or not CFG.get("auto_start", True) or os.environ.get("KAKAROT_LAUNCHER") != "1":
        return
    if os.path.exists(os.path.join(BASE, "data", "no-autostart")) or os.path.exists(startup_link()):
        return
    target = os.path.join(BASE, "start.bat")
    ps = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + "
          "'\\Kakarot.lnk'); $s.TargetPath = '{t}'; $s.WorkingDirectory = '{d}'; $s.WindowStyle = 7; "
          "$s.Description = 'Start Kakarot assistant'; $s.Save()").format(t=target.replace("'", "''"),
                                                                           d=BASE.replace("'", "''"))
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], timeout=60,
                       capture_output=True, creationflags=0x08000000)
        log.info("Kakarot will now start by itself with Windows (remove-from-startup.bat turns that off)")
    except Exception as e:
        log.warning("could not add Kakarot to startup: %s", e)


# ---------------------------------------------------------------- the brain's engine
def _ollama_up():
    try:
        return requests.get(CFG["ollama_url"].rstrip("/") + "/api/tags", timeout=5).status_code == 200
    except Exception:
        return False


def _ollama_exe():
    found = shutil.which("ollama")
    if found:
        return found
    p = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
    return p if os.path.exists(p) else None


def _start_ollama():
    exe = _ollama_exe()
    if not exe:
        return False
    flags = (0x08000000 | 0x00000008) if WIN else 0  # no window, detached
    subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    STATE["ollama_restarts"] += 1
    log.warning("Ollama wasn't running - started it again")
    return True


def _ollama_loop():
    local = any(h in CFG.get("ollama_url", "") for h in ("localhost", "127.0.0.1"))
    down = 0
    time.sleep(8)
    while True:
        up = _ollama_up()
        down = 0 if up else down + 1
        if local and down == 2:
            try:
                _start_ollama()
            except Exception as e:
                log.warning("could not start Ollama: %s", e)
        if up and down == 0:
            _maybe_warm()
        time.sleep(30 if up else 15)


def _maybe_warm():
    """Once after start (the chat page also warms it when you open it)."""
    try:
        import agent
        if not agent.busy() and not agent._warm["at"]:
            agent.warm()
    except Exception:
        pass


def start():
    for target, name in ((_awake_loop, "kakarot-awake"), (_ollama_loop, "kakarot-ollama")):
        threading.Thread(target=target, daemon=True, name=name).start()
    threading.Thread(target=ensure_autostart, daemon=True).start()
