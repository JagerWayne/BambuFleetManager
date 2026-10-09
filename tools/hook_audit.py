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

#: Hooks that are deliberately gone, with the reason. Anything not listed here,
#: and not emitted by app.js at runtime, counts as an accidental loss.
ALLOW_REMOVED = {
    "id=collapse-all": "fleet rail removed - one printer is shown at a time, so there is nothing to collapse",
    "for=fleet-filter": "filter moved into the printer bar, now labelled with aria-label",
    "type=file": "the staging file input moved into the Staging tab, which app.js builds",
    "id=update-staged": "the long installer path overflowed the settings modal; the file name and size now go in the result line",
}


def _allowed(attr_hook: str, emitted: set) -> bool:
    if attr_hook in ALLOW_REMOVED:
        return True
    return attr_hook.startswith("id=") and attr_hook[3:] in emitted


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
    appjs = (root / "static" / "js" / "app.js").read_text(encoding="utf-8")
    # Some hooks live in markup app.js builds at runtime (e.g. the staging tab),
    # so an id emitted by the JS is present even when index.html never had it.
    emitted = set(re.findall(r'\bid="([\w-]+)"', appjs))
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

    missing_hooks = sorted(h for h in (base_hooks - cur_hooks) if not _allowed(h, emitted))
    allowed_ids = {h.split("=", 1)[1] for h in ALLOW_REMOVED if h.startswith("id=")}
    missing_ids = sorted(base_ids - cur_ids - emitted - allowed_ids)
    added_ids = sorted(cur_ids - base_ids)
    relocated = sorted(base_ids - cur_ids & emitted)
    declared = sorted(h for h in (base_hooks - cur_hooks) if _allowed(h, emitted))

    print(f"HEAD {rel}: {len(base_ids)} ids / {len(base_hooks)} hooked attributes")
    print(f"work {rel}: {len(cur_ids)} ids / {len(cur_hooks)} hooked attributes")
    print(f"app.js emits: {len(emitted)} ids")
    print()
    print(f"ids lost:          {len(missing_ids)} {missing_ids}")
    print(f"hooked attrs lost: {len(missing_hooks)} {missing_hooks}")
    print(f"ids moved into JS: {len(relocated)} {relocated}")
    print(f"declared removals: {len(declared)} {declared}")
    print(f"ids added:         {len(added_ids)} {added_ids}")
    print()
    for hook in declared:
        reason = ALLOW_REMOVED.get(hook)
        if reason:
            print(f"  removed {hook}: {reason}")

    # every id the JS looks up must still exist, in markup or in JS-emitted markup
    wanted = set(re.findall(r"\$\('([\w-]+)'\)", appjs))
    wanted |= set(re.findall(r"getElementById\('([\w-]+)'\)", appjs))
    orphan = sorted(w for w in wanted if w not in cur_ids and w not in emitted)
    print(f"ids app.js looks up: {len(wanted)}; unresolved: {len(orphan)} {orphan}")

    ok = not missing_hooks and not orphan
    print("\nRESULT:", "OK - all DOM hooks intact" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())