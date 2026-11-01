"""Run programs/commands on the PC - ONLY after the user clicks Approve in the chat page."""
import os
import subprocess
import time

import db
from config import CFG
from tools import CTX, ROOTS, S, _cut, safe_path, tool


def _ask(kind, detail, reason):
    """Create an approval request and wait for the user's answer. Returns True/False."""
    if CTX.get("background"):
        raise PermissionError("Scheduled background runs can't run commands (nobody is there to approve).")
    import approve
    aid = db.x("INSERT INTO approvals(kind, detail, reason, status, created) VALUES(?,?,?,?,?)",
               (kind, detail, reason, "pending", db.now()))
    title = {"command": "Run a command", "open": "Open a file", "screen": "Use your PC screen",
             "screen_action": "A click on your PC"}.get(kind, kind) + ": " + str(detail)[:100]
    return approve.wait(aid, title)  # waits until you answer (approval_timeout_minutes: 0 = forever)


@tool("run_command", "Run a Windows command or script on the user's PC (cmd.exe), e.g. 'ipconfig', 'python script.py', "
                     "'start notepad'. The user must click Approve first, so explain why in 'reason'. "
                     "Runs inside the main workspace folder; output is returned.",
      {"command": S("The exact command line."), "reason": S("One short sentence: why this command is needed.")})
def run_command(command, reason):
    ok = _ask("command", command, reason)
    if ok is None:
        return "The user did not answer in time, so the command was NOT run."
    if not ok:
        return "The user DENIED this command. It was not run. Do not try it again; ask or choose another way."
    try:
        r = subprocess.run(command, shell=True, cwd=ROOTS[0], capture_output=True, timeout=int(CFG.get("command_timeout", 120)))
    except subprocess.TimeoutExpired:
        return "The command took too long and was stopped."
    dec = lambda b: (b or b"").decode("utf-8", errors="replace") if b"\x00" not in (b or b"") else (b or b"").decode("utf-16", errors="replace")
    out = (dec(r.stdout) + ("\n[errors]\n" + dec(r.stderr) if r.stderr else "")).strip()
    return f"Exit code {r.returncode}\n" + _cut(out or "(no output)", 6000)


@tool("open_file", "Open a file or folder on the PC with its normal program (Excel, Word, PDF viewer, Explorer...). "
                   "Needs the user's approval.",
      {"path": S("File or folder path."), "reason": S("Why it should be opened.")})
def open_file(path, reason):
    p = safe_path(path)
    if not os.path.exists(p):
        raise FileNotFoundError(p)
    ok = _ask("open", p, reason)
    if not ok:
        return "Not opened (the user denied it or did not answer)."
    os.startfile(p)  # Windows only
    return f"Opened {p}"
