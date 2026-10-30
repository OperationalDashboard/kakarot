"""Kakarot's own abilities: it can write NEW tools for itself.
A new tool is written by the coding brain, checked, and saved as 'pending'. It only becomes active after the user
clicks Approve (Status tab or chat). Active tools live in plugins/*.py and load automatically at start."""
import ast
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys

import db
from config import BASE
from tools import S, TOOLS, tool

log = logging.getLogger("kakarot.plugins")
DIR = os.path.join(BASE, "plugins")
PENDING = os.path.join(DIR, "pending")
DISABLED = os.path.join(DIR, "disabled")
for _d in (DIR, PENDING, DISABLED):
    os.makedirs(_d, exist_ok=True)
_loaded = set()

TEMPLATE = '''from tools import tool, S, I, B   # S = text, I = whole number, B = true/false


@tool("{name}", "<one clear sentence: what it does and when to use it>",
      {{"param_one": S("what this input is"), "param_two": I("...")}})
def {name}(param_one, param_two=0):
    # do the work here: you may use the standard library and: requests, bs4 (BeautifulSoup), openpyxl, docx,
    # pypdf, numpy, PIL. Use tools.safe_path(path) for any file path. Never delete files.
    return "a short text result for the assistant"
'''

BUILD_PROMPT = """Write a new TOOL (a Python plugin file) for a local personal-assistant program.

Tool name: {name}
What it must do: {what}
{why}
The file must follow EXACTLY this pattern (imports, the @tool decorator with name, description and a parameter
dict, then one function with the same name that returns a string):
```python
{template}
```
Rules: robust error messages (raise Exception with a clear text), timeouts on network calls, no input(), no infinite
loops, no deleting or overwriting user files, no secrets. It runs on Windows. Reply with ONLY the full file in one
python code block."""


def _valid_name(name):
    name = re.sub(r"[^a-z0-9_]", "_", name.strip().lower()).strip("_")
    if not re.match(r"^[a-z][a-z0-9_]{2,40}$", name or ""):
        raise ValueError("Tool names must be 3-40 letters/numbers/underscores, starting with a letter.")
    return name


def validate(name, code):
    """Static checks + a test import in a separate process. Returns None or an error text."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"Syntax error line {e.lineno}: {e.msg}"
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and getattr(d.func, "id", "") == "tool" and d.args and \
                        isinstance(d.args[0], ast.Constant) and d.args[0].value == name:
                    found = True
    if not found:
        return f'The file must define def {name}(...) decorated with @tool("{name}", ...).'
    if name in TOOLS and name not in _loaded:
        return f"'{name}' is already a built-in tool - choose another name."
    tmp = os.path.join(PENDING, f"_check_{name}.py")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(code)
    test = ("import sys,importlib.util; sys.path.insert(0, sys.argv[1]); import tools; "
            "s=importlib.util.spec_from_file_location('chk', sys.argv[2]); m=importlib.util.module_from_spec(s); "
            "s.loader.exec_module(m); assert sys.argv[3] in tools.TOOLS, 'tool not registered'; print('OK')")
    try:
        r = subprocess.run([sys.executable, "-c", test, BASE, tmp, name], capture_output=True, text=True,
                           timeout=120, cwd=BASE)
        if "OK" not in r.stdout:
            return "Test import failed:\n" + (r.stderr or r.stdout)[-1500:]
    except subprocess.TimeoutExpired:
        return "Test import took too long (the file must not run work at import time)."
    finally:
        os.path.exists(tmp) and os.remove(tmp)
    return None


def submit(name, description, code):
    """Save as pending and ask the user. Returns a message."""
    name = _valid_name(name)
    err = validate(name, code)
    if err:
        raise ValueError(err)
    path = os.path.join(PENDING, f"{name}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    db.x("UPDATE approvals SET status='replaced' WHERE kind='tool' AND status='pending' AND detail LIKE ?",
         (f'%"name": "{name}"%',))
    db.x("INSERT INTO approvals(kind, detail, reason, status, created) VALUES(?,?,?,?,?)",
         ("tool", json.dumps({"name": name, "code": code}, ensure_ascii=False), description, "pending", db.now()))
    return name


def build(name, what, why=""):
    """Have the coding brain write a tool, check it (one repair attempt), and submit it for approval."""
    import coder
    name = _valid_name(name)
    prompt = BUILD_PROMPT.format(name=name, what=what, why=f"Why it's needed: {why}" if why else "",
                                 template=TEMPLATE.format(name=name))
    answer, note = coder.ask_coder(prompt)
    code = coder.extract_code(answer)
    err = validate(name, code)
    if err:
        answer, note = coder.ask_coder(prompt + f"\n\nYour previous attempt failed this check:\n{err}\n"
                                                f"Previous attempt:\n```python\n{code}\n```\nFix it.")
        code = coder.extract_code(answer)
        err = validate(name, code)
        if err:
            raise ValueError(f"I couldn't build a working '{name}' tool: {err}")
    submit(name, what, code)
    return name


def _load(path):
    name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(f"kakarot_plugin_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _loaded.add(name)
    return name


def load_all():
    for f in sorted(os.listdir(DIR)):
        if f.endswith(".py"):
            try:
                _load(os.path.join(DIR, f))
            except Exception as e:
                log.error("plugin %s failed to load: %s", f, e)


def activate(name):
    src = os.path.join(PENDING, f"{name}.py")
    if not os.path.exists(src):
        return False
    dst = os.path.join(DIR, f"{name}.py")
    shutil.move(src, dst)
    _load(dst)
    db.add_inbox("🧩 New ability active", f"Kakarot can now use '{name}'.")
    return True


def reject(name):
    src = os.path.join(PENDING, f"{name}.py")
    if os.path.exists(src):
        shutil.move(src, os.path.join(DISABLED, f"{name}.rejected.py"))


def listing():
    active = []
    for f in sorted(os.listdir(DIR)):
        if f.endswith(".py"):
            n = f[:-3]
            desc = TOOLS.get(n, {}).get("schema", {}).get("function", {}).get("description", "")
            active.append({"name": n, "description": desc})
    pending = []
    for a in db.q("SELECT * FROM approvals WHERE kind='tool' AND status='pending' ORDER BY id"):
        d = json.loads(a["detail"])
        pending.append({"id": a["id"], "name": d["name"], "description": a["reason"], "code": d["code"]})
    return {"active": active, "pending": pending}


@tool("build_new_tool",
      "Give yourself a NEW ABILITY: your coding brain writes a new tool (Python) for something you can't do yet, "
      "e.g. 'convert currency with a live rate', 'read my bank SMS export'. It is checked and then waits for the "
      "user's Approve in the Status tab before you can use it.",
      {"name": S("snake_case tool name, e.g. 'currency_convert'."),
       "what_it_does": S("Detailed description: inputs, what it should do, what it returns.")})
def build_new_tool(name, what_it_does):
    n = build(name, what_it_does)
    return (f"I wrote the new ability '{n}' and tested that it loads. It's waiting for your approval: open the "
            f"Status tab → New abilities → Approve. After that I can use it straight away.")


@tool("list_my_tools", "List the extra abilities Kakarot built for itself (active and waiting for approval).")
def list_my_tools():
    ls = listing()
    a = "\n".join(f"- {t['name']}: {t['description']}" for t in ls["active"]) or "- none"
    p = "\n".join(f"- {t['name']}: {t['description']}" for t in ls["pending"]) or "- none"
    return f"Active self-built tools:\n{a}\n\nWaiting for approval:\n{p}"


@tool("remove_my_tool", "Switch off one of the abilities Kakarot built for itself.", {"name": S("Tool name.")})
def remove_my_tool(name):
    src = os.path.join(DIR, f"{name}.py")
    if not os.path.exists(src):
        return f"No self-built tool called '{name}'."
    shutil.move(src, os.path.join(DISABLED, f"{name}.py"))
    TOOLS.pop(name, None)
    _loaded.discard(name)
    return f"Removed '{name}'."
