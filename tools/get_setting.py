"""Print one value from config/settings.json, for run.bat to consume.

Called as:  python tools/get_setting.py port
Always prints a usable default so the launcher never breaks on a bad file.
"""

import json
import os
import sys

DEFAULTS = {"host": "0.0.0.0", "port": 8000}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.join(ROOT, "config", "settings.json")


def main() -> int:
    key = sys.argv[1] if len(sys.argv) > 1 else "port"
    value = DEFAULTS.get(key, "")
    try:
        with open(SETTINGS, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        if key in stored and stored[key] not in (None, ""):
            value = stored[key]
    except Exception:
        pass
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
