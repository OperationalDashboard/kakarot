import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

CFG["allowed_folders"] = [
    os.path.normpath(os.path.expandvars(os.path.expanduser(p)))
    for p in CFG.get("allowed_folders", [])
] or [os.path.join(BASE, "workspace")]

for _p in CFG["allowed_folders"]:
    os.makedirs(_p, exist_ok=True)
