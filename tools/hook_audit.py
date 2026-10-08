r"""DOM-hook integrity check: compare the dashboard markup against git HEAD.

Reports any id / name / data-* / for attribute that exists in the committed
index.html but is missing from the working copy (or was newly introduced).
Run:  .venv\Scripts\python tools/hook_audit.py
"""
import re
import subprocess
import pathlib
import sys

root = pathlib.Path(__file__).resolve().parent.parent
rel = "templates/index.html"

ATTRS = ("id", "name", "data-", "for", "type", "role")


def hooks(markup: str):
    found = set()
    for tag in re.findall(r"<[a-zA-Z][^>]*>", markup):
        for attr in re.findall(r"([\w:-]+)\s*=\s*\"([^\"]*)\"", tag):
            name, value = attr
            if any(name == a or name.startswith(a) for a in ATTRS):
                # collapse data-* into name=value so repeated hooks stay distinct
                found.add(f"{name}={value}")
    return found


def ids(markup: str):
    return set(re.findall(r"\bid=\"([^\"]+)\"", markup))


def main() -> int:
    current = (root / rel).read_text(encoding="utf-8")
    try:
        original = subprocess.run(
            ["git", "show", f"HEAD:{rel}"], cwd=root, capture_output=True,
            check=True, text=True, encoding="utf-8",
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"could not read HEAD:{rel} ({exc}); comparing working copy only")
        original = current

    base_hooks, cur_hooks = hooks(original), hooks(current)
    base_ids, cur_ids = ids(original), ids(current)

    missing_hooks = sorted(base_hooks - cur_hooks)
    missing_ids = sorted(base_ids - cur_ids)
    added_ids = sorted(cur_ids - base_ids)

    print(f"HEAD {rel}: {len(base_ids)} ids / {len(base_hooks)} hooked attributes")
    print(f"work {rel}: {len(cur_ids)} ids / {len(cur_hooks)} hooked attributes")
    print()
    print(f"ids lost:        {len(missing_ids)} {missing_ids}")
    print(f"hooked attrs lost: {len(missing_hooks)} {missing_hooks}")
    print(f"ids added:       {len(added_ids)} {added_ids}")
    print()

    # every id the JS looks up must still exist in the markup
    appjs = (root / "static" / "js" / "app.js").read_text(encoding="utf-8")
    wanted = set(re.findall(r"\$\('([\w-]+)'\)", appjs))
    wanted |= set(re.findall(r"getElementById\('([\w-]+)'\)", appjs))
    orphan = sorted(w for w in wanted if w not in cur_ids)
    print(f"ids app.js looks up: {len(wanted)}; missing from markup: {len(orphan)} {orphan}")

    ok = not missing_hooks and not orphan
    print("\nRESULT:", "OK - all DOM hooks intact" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())