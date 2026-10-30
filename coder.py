"""Coding: Kakarot writes, runs, tests and fixes real programs.
The code itself is written by a dedicated coding brain (qwen2.5-coder). Programs live in the code folder
(Documents\\KakarotWorkspace\\code\\<project>) and run there freely; each project gets its own Python
environment (.venv) so packages never break Kakarot itself."""
import os
import re
import signal
import subprocess
import sys

import requests

from config import CFG
from tools import I, ROOTS, S, _cut, tool

CODE_DIR = os.path.normpath(os.path.expandvars(CFG.get("code_folder", "")) or os.path.join(ROOTS[0], "code"))
os.makedirs(CODE_DIR, exist_ok=True)
SKIP = {".venv", "venv", "node_modules", "__pycache__", ".git", "dist", "build", ".pytest_cache"}
WIN = os.name == "nt"

CODER_SYSTEM = """You are an expert senior software engineer. You write complete, correct, production-quality code:
clear structure, good names, error handling, comments only where they help, no placeholders or TODOs, no
'...rest of code'. Follow the existing style of the project. When asked for a file, reply with ONLY the full file
content in one code block."""


def code_model():
    return CFG.get("code_model", "qwen2.5-coder:14b")


def ask_coder(prompt, system=CODER_SYSTEM):
    """Ask the coding brain directly (no tools). Falls back to the main brain if it isn't downloaded."""
    url = CFG["ollama_url"].rstrip("/") + "/api/chat"
    note = ""
    for model in (code_model(), CFG["model"]):
        body = {"model": model, "stream": False, "keep_alive": CFG.get("code_keep_alive", "10m"),
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "options": {"num_ctx": int(CFG.get("code_context_tokens", 16384)), "temperature": 0.2}}
        if model == CFG["model"]:
            body["think"] = False
        r = requests.post(url, json=body, timeout=1800)
        if r.status_code == 404 or "not found" in r.text[:300].lower():
            note = f"(coding brain {code_model()} not downloaded - run get-code-brain.bat; used the main brain) "
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Ollama error {r.status_code}: {r.text[:300]}")
        return (r.json().get("message") or {}).get("content", ""), note
    raise RuntimeError("No brain available to write code.")


def extract_code(text):
    blocks = re.findall(r"```[\w+.-]*\n(.*?)```", text, flags=re.S)
    if blocks:
        return max(blocks, key=len).rstrip() + "\n"
    return text.strip() + "\n"


def code_path(path):
    """Resolve a path inside the code folder (relative paths start there). Never outside."""
    p = os.path.expandvars(str(path).strip().strip('"'))
    if not os.path.isabs(p):
        p = os.path.join(CODE_DIR, p)
    rp = os.path.realpath(p)
    base = os.path.realpath(CODE_DIR)
    if os.path.normcase(rp) != os.path.normcase(base) and not os.path.normcase(rp).startswith(os.path.normcase(base) + os.sep):
        raise PermissionError(f"Code must stay inside the code folder {CODE_DIR}")
    return rp


def _tree(folder, limit=200):
    out = []
    for dp, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for f in sorted(files):
            fp = os.path.join(dp, f)
            out.append(f"{os.path.relpath(fp, folder)}  ({os.path.getsize(fp):,} bytes)")
            if len(out) >= limit:
                return out + ["...(more files)"]
    return out


def _project_of(p):
    rel = os.path.relpath(p, CODE_DIR)
    return os.path.join(CODE_DIR, rel.split(os.sep)[0])


def _read(p, limit=60000):
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read(limit)


@tool("code_new_project", "Start a new coding project (a folder in the code workspace). Use for any program, script, "
                          "website, bot, automation or app the user wants built.",
      {"name": S("Short folder name, e.g. 'bus_fare_app'."), "description": S("What the program should do.")})
def code_new_project(name, description):
    name = re.sub(r"[^\w.-]+", "_", name.strip()).strip("_") or "project"
    p = code_path(name)
    os.makedirs(p, exist_ok=True)
    readme = os.path.join(p, "README.md")
    if not os.path.exists(readme):
        with open(readme, "w", encoding="utf-8") as f:
            f.write(f"# {name}\n\n{description}\n")
    return f"Project folder ready: {p}\nNext: write_code for each file, then run_code to test it."


def _write(path, instructions, related_files="", must_exist=False):
    p = code_path(path)
    exists = os.path.exists(p)
    if must_exist and not exists:
        raise FileNotFoundError(f"{p} doesn't exist yet - use write_code to create it.")
    project = _project_of(p)
    ctx = [f"Project folder: {os.path.basename(project)}", "Files in the project:", *(_tree(project, 80) or ["(none yet)"])]
    readme = os.path.join(project, "README.md")
    if os.path.exists(readme) and readme != p:
        ctx += ["", "README.md:", _read(readme, 3000)]
    for rel in [r.strip() for r in (related_files or "").replace(";", ",").split(",") if r.strip()]:
        try:
            rp = code_path(rel if os.path.isabs(rel) or os.sep in rel or "/" in rel else os.path.join(project, rel))
            ctx += ["", f"--- {os.path.relpath(rp, project)} ---", _read(rp, 12000)]
        except Exception as e:
            ctx += ["", f"(could not read {rel}: {e})"]
    rel_name = os.path.relpath(p, project)
    if exists:
        task = (f"Here is the CURRENT content of {rel_name}:\n```\n{_read(p)}\n```\n\nChange it as follows:\n{instructions}\n\n"
                f"Reply with the COMPLETE updated file {rel_name} in one code block (keep everything that should stay).")
    else:
        task = f"Write the file {rel_name}.\n\nRequirements:\n{instructions}\n\nReply with the COMPLETE file in one code block."
    answer, note = ask_coder("\n".join(ctx) + "\n\n" + task)
    code = extract_code(answer)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(code)
    lines = code.count("\n")
    check = ""
    if p.endswith(".py"):
        try:
            compile(code, p, "exec")
            check = "Syntax check: OK."
        except SyntaxError as e:
            check = f"SYNTAX ERROR line {e.lineno}: {e.msg} - fix it with edit_code."
    preview = "\n".join(code.splitlines()[:25])
    return f"{note}{'Updated' if exists else 'Created'} {p} ({lines} lines). {check}\nStart of file:\n{preview}"


@tool("write_code",
      "Have your CODING BRAIN write a complete source file (any language: Python, JavaScript, HTML/CSS, SQL, C#, "
      "PowerShell...). Give detailed instructions: purpose, functions, inputs/outputs, libraries, edge cases. "
      "Path like 'project/app.py'. If the file exists it is rewritten following the instructions.",
      {"path": S("File path inside the code folder, e.g. 'bus_fare_app/main.py'."),
       "instructions": S("Exactly what this file must contain/do."),
       "related_files": S("Optional comma-separated files the coder should read first (same project), "
                          "e.g. 'models.py, utils.py'.")}, ["path", "instructions"])
def write_code(path, instructions, related_files=""):
    return _write(path, instructions, related_files)


@tool("edit_code", "Change an EXISTING code file with your coding brain: fix a bug (paste the error), add a feature, "
                   "refactor. The whole file is updated consistently.",
      {"path": S("Existing file path inside the code folder."),
       "instructions": S("What to change. For bugs include the exact error message and the line."),
       "related_files": S("Optional comma-separated related files to read first.")}, ["path", "instructions"])
def edit_code(path, instructions, related_files=""):
    return _write(path, instructions, related_files, must_exist=True)


@tool("read_code", "Read a code file with line numbers (optionally a range of lines).",
      {"path": S("File path inside the code folder."), "start_line": I("First line (default 1)."),
       "end_line": I("Last line (0 = to the end).")}, ["path"])
def read_code(path, start_line=1, end_line=0):
    lines = _read(code_path(path), 400000).splitlines()
    s = max(int(start_line or 1), 1)
    e = int(end_line or 0) or len(lines)
    body = "\n".join(f"{i:5d}| {l}" for i, l in enumerate(lines[s - 1:e], s))
    return _cut(f"{path} - {len(lines)} lines\n{body}", 14000)


@tool("code_files", "List the files of a coding project (or all projects if no name is given).",
      {"project": S("Project folder name (empty = list all projects).")}, [])
def code_files(project=""):
    if not project:
        ps = sorted(d for d in os.listdir(CODE_DIR) if os.path.isdir(os.path.join(CODE_DIR, d)))
        return f"Code folder: {CODE_DIR}\nProjects: " + (", ".join(ps) or "(none yet)")
    p = code_path(project)
    return f"{p}\n" + ("\n".join(_tree(p)) or "(empty)")


@tool("search_code", "Find text in a project's code files (shows file:line: text).",
      {"project": S("Project folder name."), "text": S("Text or regular expression to find.")})
def search_code(project, text):
    p = code_path(project)
    try:
        rx = re.compile(text, re.I)
    except re.error:
        rx = re.compile(re.escape(text), re.I)
    hits = []
    for dp, dirs, files in os.walk(p):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for f in files:
            fp = os.path.join(dp, f)
            if os.path.getsize(fp) > 2_000_000:
                continue
            try:
                for n, line in enumerate(_read(fp, 2_000_000).splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{os.path.relpath(fp, p)}:{n}: {line.strip()[:160]}")
            except Exception:
                continue
            if len(hits) > 150:
                break
    return "\n".join(hits[:150]) or "No matches."


# ---------------------------------------------------------------- running code
BLOCKED = re.compile(r"\b(format|shutdown|diskpart|bcdedit|cipher\s+/w|takeown|icacls|net\s+user|net\s+localgroup|"
                     r"schtasks|sc\s+(delete|create|config)|reg\s+(add|delete|import)|vssadmin|wmic|"
                     r"set-executionpolicy|restart-computer|stop-computer)\b", re.I)
ABS_PATH = re.compile(r"(?:(?<![\w])[a-zA-Z]:[\\/][^\s\"'|&<>]*)|\\\\[^\s\"']+|%\w+%|\$env:\w+|~[\\/]")


def check_command(command, cwd):
    if BLOCKED.search(command):
        raise PermissionError("That command can change system settings, so it is blocked for code runs.")
    if re.search(r"\.\.[\\/]", command):
        raise PermissionError("Code runs must stay inside the project (no '..\\' paths).")
    base = os.path.normcase(os.path.realpath(CODE_DIR))
    for m in ABS_PATH.findall(command):
        rp = os.path.normcase(os.path.realpath(os.path.expandvars(m)))
        if not rp.startswith(base):
            raise PermissionError(f"'{m}' is outside the code folder. Code runs can only touch files in {CODE_DIR}.")


def _venv(project_dir):
    """Each project gets its own Python environment, created on first use."""
    vdir = os.path.join(project_dir, ".venv")
    scripts = os.path.join(vdir, "Scripts" if WIN else "bin")
    if not os.path.isdir(scripts):
        base = getattr(sys, "_base_executable", sys.executable)
        subprocess.run([base, "-m", "venv", vdir], capture_output=True, timeout=300)
    return vdir, scripts


def _kill(proc):
    try:
        if WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        proc.kill()


def _dec(b):
    b = b or b""
    return b.decode("utf-16", errors="replace") if b"\x00" in b[:200] else b.decode("utf-8", errors="replace")


@tool("run_code",
      "Run a command inside a coding project folder to test the code: e.g. 'python main.py', 'python -m pytest -q', "
      "'pip install requests', 'node app.js', 'npm install'. Python and pip use the project's own environment. "
      "Returns the output and errors so you can fix them. For servers/long programs set seconds to run briefly.",
      {"project": S("Project folder name."), "command": S("The command line to run in the project folder."),
       "seconds": I("Max seconds to let it run (default 120, max 900). It is stopped after that.")},
      ["project", "command"])
def run_code(project, command, seconds=120):
    p = code_path(project)
    if not os.path.isdir(p):
        raise FileNotFoundError(f"No project folder {p} - create it with code_new_project.")
    check_command(command, p)
    env = dict(os.environ)
    if re.match(r"\s*(python|py|pip|pytest)\b", command, re.I) or os.path.exists(os.path.join(p, "requirements.txt")):
        vdir, scripts = _venv(p)
        env["VIRTUAL_ENV"] = vdir
        env["PATH"] = scripts + os.pathsep + env.get("PATH", "")
        env.pop("PYTHONHOME", None)
        command = re.sub(r"^\s*py\b", "python", command)
    env["PYTHONIOENCODING"] = "utf-8"
    limit = max(5, min(int(seconds or 120), 900))
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WIN else {"start_new_session": True}
    proc = subprocess.Popen(command, shell=True, cwd=p, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    stopped = ""
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        _kill(proc)
        out, err = proc.communicate()
        stopped = f"\n(stopped after {limit} seconds - it was still running)"
    text = _dec(out) + ("\n[errors]\n" + _dec(err) if err else "")
    text = text.strip() or "(no output)"
    if len(text) > 7000:  # keep the start and the end: errors are usually at the end
        text = text[:1500] + f"\n...[{len(text) - 6500:,} characters cut]...\n" + text[-5000:]
    return f"Exit code {proc.returncode}{stopped}\n{text}"
