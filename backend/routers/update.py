"""Self-update: check, download, stage and run the installer.

Handlers only: shared helpers, config and the app itself live in
``backend.main`` (imported here as ``core`` so monkeypatched module values -
paths, the MQTT manager - resolve at call time, not import time).
"""
from fastapi import APIRouter
import asyncio
import os

from backend import main as core

router = APIRouter()


@router.get("/api/update")
async def check_update():
    """Ask GitHub Releases whether a newer build is available."""
    return await core.run_blocking(core.updater.check_for_update)


@router.post("/api/update/download")
async def download_update():
    """Download the installer and stop there - nothing is launched.

    The dashboard offers this as a separate step so the file can be fetched
    first (and kept, or run by hand) instead of download-and-launch in one go.
    """
    report = await core.run_blocking(core.updater.check_for_update)
    if not report.get("update_available") or not report.get("asset_url"):
        raise core.HTTPException(status_code=409, detail=report.get("error") or "No update available")

    try:
        installer = await core.run_blocking(
            core.updater.download_installer, core.UPDATE_DIR, report["asset_url"],
            int(report.get("asset_size") or 0)
        )
    except Exception as exc:
        raise core.HTTPException(status_code=502, detail=f"Download failed: {exc}")

    core.logger.info("Staged installer %s for %s at %s", report.get("latest"), report.get("asset_size"), installer)
    return {
        "downloaded": True,
        "version": report.get("latest"),
        "installer": installer,
        "size": os.path.getsize(installer),
    }


@router.get("/api/update/installer")
async def download_installer_copy():
    """Serve the staged installer as a normal browser download.

    Same bytes, saved wherever the browser puts it - handy for keeping a copy
    or running it from somewhere other than the app's data directory.
    """
    installer = core.updater.staged_installer(core.UPDATE_DIR)
    if not installer:
        raise core.HTTPException(status_code=404, detail="No installer has been downloaded yet")
    return core.FileResponse(
        installer,
        media_type="application/vnd.microsoft.portable-executable",
        filename=os.path.basename(installer),
    )


@router.post("/api/update/run")
async def run_staged_installer():
    """Launch an already-downloaded installer, then quit to free the files.

    The second half of the manual flow: download first, run when you are ready.
    """
    report = await core.run_blocking(core.updater.check_for_update)
    installer = core.updater.staged_installer(core.UPDATE_DIR)
    if not installer:
        raise core.HTTPException(
            status_code=409, detail="Nothing downloaded yet - press Download first.")

    # Refuse to run a file that does not match the published asset size.
    expected = int(report.get("asset_size") or 0)
    actual = os.path.getsize(installer)
    if expected and actual != expected:
        raise core.HTTPException(
            status_code=409,
            detail=(f"The downloaded installer is incomplete ({actual} of {expected} bytes). "
                    "Download it again."),
        )

    try:
        await core.run_blocking(core.updater.launch_installer, installer)
    except Exception as exc:
        raise core.HTTPException(status_code=500, detail=f"Could not start installer: {exc}")

    core.logger.info("Running staged installer %s (%s)", report.get("latest"), installer)
    # Give the response a moment to reach the browser, then quit.
    asyncio.get_event_loop().call_later(1.0, core.run_shutdown_hooks)
    return {"started": True, "version": report.get("latest"), "installer": installer}


@router.post("/api/update/install")
async def install_update():
    """Download the latest installer and launch it, then quit to free the files.

    The installer runs silently and upgrades in place. We shut the app down so
    Inno Setup can replace the running files; the installer relaunches it when
    done (``/RESTARTAPPLICATIONS``). This is the one-shot flow; the dashboard
    also exposes download and run as separate steps.
    """
    report = await core.run_blocking(core.updater.check_for_update)
    if not report.get("update_available") or not report.get("asset_url"):
        raise core.HTTPException(status_code=409, detail=report.get("error") or "No update available")

    try:
        installer = await core.run_blocking(
            core.updater.download_installer, core.UPDATE_DIR, report["asset_url"],
            int(report.get("asset_size") or 0)
        )
    except Exception as exc:
        raise core.HTTPException(status_code=502, detail=f"Download failed: {exc}")

    try:
        await core.run_blocking(core.updater.launch_installer, installer)
    except Exception as exc:
        raise core.HTTPException(status_code=500, detail=f"Could not start installer: {exc}")

    core.logger.info("Updating to %s via %s", report.get("latest"), installer)
    # Give the response a moment to reach the browser, then quit.
    asyncio.get_event_loop().call_later(1.0, core.run_shutdown_hooks)
    return {"started": True, "version": report.get("latest"), "installer": installer}
