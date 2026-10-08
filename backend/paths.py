"""Resolve where the app reads bundled assets and writes its data.

When run from a source checkout both directories are the repository root, which
keeps the existing ``config/`` and ``uploads/`` layout. When frozen by
PyInstaller the read-only resources (templates, static, VERSION) live inside the
bundle while the mutable data (config, uploads, logs) must live somewhere the
user can write to - ``%LOCALAPPDATA%\\BambuFleetManager`` by default, or the
directory named by the ``BFM_DATA_DIR`` environment variable.
"""

import os
import sys


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _repo_root() -> str:
    # backend/paths.py -> backend -> repo root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


#: Root of read-only resources that ship with the app.
def _resource_dir() -> str:
    if _is_frozen():
        return getattr(sys, "_MEIPASS", None) or _repo_root()
    return _repo_root()


RESOURCE_DIR: str = _resource_dir()


def _default_data_dir() -> str:
    if _is_frozen():
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "BambuFleetManager")
    return _repo_root()


#: Root of the writable data directory (config/, uploads/, logs).
DATA_DIR: str = os.environ.get("BFM_DATA_DIR") or _default_data_dir()


def ensure_data_dirs() -> str:
    """Create the writable data directories and seed example configs."""
    config_dir = os.path.join(DATA_DIR, "config")
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "uploads"), exist_ok=True)

    # Seed editable config files from the bundled *.example templates.
    for name in ("printers.json", "settings.json"):
        dest = os.path.join(config_dir, name)
        if os.path.exists(dest):
            continue
        example = os.path.join(RESOURCE_DIR, "config", name + ".example")
        if os.path.exists(example):
            with open(example, "r", encoding="utf-8") as src:
                content = src.read()
            with open(dest, "w", encoding="utf-8") as out:
                out.write(content)
    return DATA_DIR
