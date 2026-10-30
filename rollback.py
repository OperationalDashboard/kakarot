"""Run by start.bat when Kakarot fails to start right after updating itself: puts the previous version back."""
import json
import os
import shutil

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
PENDING = os.path.join(DATA, "pending_update.json")


def main():
    if not os.path.exists(PENDING):
        return
    with open(PENDING, encoding="utf-8") as f:
        p = json.load(f)
    backup = p["backup"]
    for dp, _, files in os.walk(backup):
        for name in files:
            if name == "_new_files.json":
                continue
            src = os.path.join(dp, name)
            dst = os.path.join(BASE, os.path.relpath(src, backup))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    try:
        with open(os.path.join(backup, "_new_files.json"), encoding="utf-8") as f:
            for rel in json.load(f):
                fp = os.path.join(BASE, *rel.split("/"))
                if os.path.exists(fp):
                    os.remove(fp)
    except FileNotFoundError:
        pass
    before = p.get("before") or {}
    before["skip"] = list(set((before.get("skip") or []) + [p["after"]["sha"]]))
    with open(os.path.join(DATA, "version.json"), "w", encoding="utf-8") as f:
        json.dump(before, f, indent=2)
    with open(os.path.join(DATA, "rollback_note.txt"), "w", encoding="utf-8") as f:
        f.write(f"The new version {p['after']['sha'][:7]} ({p['after'].get('message', '')}) did not start, so I went "
                f"back to the previous version {before.get('sha', '?')[:7]}. I won't install that version again.")
    os.remove(PENDING)
    print("  Restored the previous version.")


if __name__ == "__main__":
    main()
