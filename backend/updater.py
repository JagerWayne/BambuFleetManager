"""Check GitHub Releases for a newer build and download/launch its installer.

The project is published at :data:`REPO` and every build is attached to a
GitHub Release as a Windows installer asset. This module is deliberately pure
(no FastAPI imports) so it can be unit tested by mocking ``requests``.
"""

import os
import subprocess
import sys
from typing import Any, Dict, Optional

import requests

from backend.paths import RESOURCE_DIR

#: Public GitHub repository that hosts releases.
REPO = "JagerWayne/BambuFleetManager"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
#: Exact asset name the build script uploads.
ASSET_NAME = "BambuFleetManagerSetup.exe"
#: Fallback if the exact asset is missing: any installer-looking .exe.
ASSET_HINTS = ("setup", "installer")

_USER_AGENT = "BambuFleetManager-Updater"
_TIMEOUT = 15


def current_version() -> str:
    """Return the running app version (contents of ``VERSION``)."""
    path = os.path.join(RESOURCE_DIR, "VERSION")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def parse_version(value: str) -> tuple:
    """Turn ``v1.2.3``/``1.2.3-beta`` into a comparable tuple of ints."""
    text = str(value or "").strip().lstrip("vV")
    core = text.split("-", 1)[0].split("+", 1)[0]
    parts = []
    for chunk in core.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def _pick_asset(assets: list) -> Optional[Dict[str, Any]]:
    for asset in assets or []:
        if str(asset.get("name", "")).lower() == ASSET_NAME.lower():
            return asset
    for asset in assets or []:
        name = str(asset.get("name", "")).lower()
        if name.endswith(".exe") and any(hint in name for hint in ASSET_HINTS):
            return asset
    return None


def check_for_update() -> Dict[str, Any]:
    """Query GitHub for the latest release. Never raises; reports ``error``."""
    current = current_version()
    result: Dict[str, Any] = {
        "current": current,
        "latest": current,
        "update_available": False,
        "name": "",
        "notes": "",
        "published_at": "",
        "html_url": f"https://github.com/{REPO}/releases",
        "asset_url": "",
        "asset_name": "",
        "asset_size": 0,
        "error": "",
    }
    try:
        resp = requests.get(
            RELEASES_API,
            headers={"Accept": "application/vnd.github+json", "User-Agent": _USER_AGENT},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 404:
            result["error"] = "No releases published yet."
            return result
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        result["error"] = f"Could not reach GitHub: {exc}"
        return result

    latest = str(data.get("tag_name") or data.get("name") or current)
    asset = _pick_asset(data.get("assets") or [])
    result.update({
        "latest": latest,
        "name": str(data.get("name") or latest),
        "notes": str(data.get("body") or "").strip(),
        "published_at": str(data.get("published_at") or ""),
        "html_url": str(data.get("html_url") or result["html_url"]),
        "update_available": is_newer(latest, current) and asset is not None,
    })
    if asset is not None:
        result["asset_url"] = str(asset.get("browser_download_url") or "")
        result["asset_name"] = str(asset.get("name") or "")
        result["asset_size"] = int(asset.get("size") or 0)
    if is_newer(latest, current) and asset is None:
        result["error"] = "A newer version exists but has no installer asset."
    return result


def download_installer(dest_dir: str, asset_url: str) -> str:
    """Stream the installer asset into ``dest_dir`` and return its path."""
    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, ASSET_NAME)
    with requests.get(asset_url, headers={"User-Agent": _USER_AGENT},
                      stream=True, timeout=_TIMEOUT) as resp:
        resp.raise_for_status()
        with open(target, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    fh.write(chunk)
    return target


def launch_installer(path: str) -> None:
    """Start the installer silently, detached from this process.

    Inno Setup upgrades in place; ``/CLOSEAPPLICATIONS`` closes the running
    tray/server so the files can be replaced and ``/RESTARTAPPLICATIONS``
    (best effort) brings the app back when the install finishes.
    """
    args = [path, "/SILENT", "/NORESTART", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"]
    if sys.platform == "win32":
        subprocess.Popen(
            args,
            close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:  # pragma: no cover - the product is Windows-only
        subprocess.Popen(args, close_fds=True)
