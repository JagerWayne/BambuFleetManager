"""Camera control, snapshots and the MJPEG/RTSP streams.

Handlers only: shared helpers, config and the app itself live in
``backend.main`` (imported here as ``core`` so monkeypatched module values -
paths, the MQTT manager - resolve at call time, not import time).
"""
from fastapi import APIRouter
import asyncio
from typing import Optional

from backend import main as core

router = APIRouter()


@router.post("/api/printers/{printer_id}/camera")
async def camera_state(printer_id: str, cmd: core.CameraStateCommand):
    core.get_printer(printer_id)
    action = "enable" if cmd.state else "disable"
    return await core.dispatch(printer_id, "camera", {"action": action})


@router.get("/api/printers/{printer_id}/camera/snapshot")
async def camera_snapshot(printer_id: str, cache_bust: Optional[int] = None):
    printer = core.get_printer(printer_id)
    # ffmpeg pulls a frame straight from the RTSPS stream; the printer's own
    # HTTP camera service is blocked on current firmware, so it is the fallback.
    frame = await core.run_blocking(core.camera_mod.ffmpeg_snapshot, printer.ip, printer.access_code)
    if not frame:
        frame = await core.run_blocking(core.camera_mod.fetch_snapshot, printer.ip)
    if not frame:
        raise core.HTTPException(
            status_code=502, detail="Camera snapshot unavailable (is the camera enabled?)"
        )
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}
    if cache_bust:
        headers["X-Cache-Bust"] = str(cache_bust)
    return core.Response(content=frame, media_type="image/jpeg", headers=headers)


@router.get("/api/printers/{printer_id}/camera/mjpeg")
async def camera_mjpeg(
    printer_id: str,
    request: core.Request,
    width: int = core.Query(960, ge=160, le=1920),
    fps: int = core.Query(10, ge=1, le=30),
):
    """Live view as MJPEG - plays in any browser through a plain <img> tag.

    ffmpeg transcodes the printer's RTSPS H.264, so the browser needs no codec
    support of its own.

    One stream per printer: starting a new one stops the previous stream for the
    same printer, so a page reload or a second viewer takes over instead of
    stacking up another ffmpeg. The reader runs off the event loop and the
    process is torn down in ``finally``, which runs even when the client
    disconnects - and killing ffmpeg is what unblocks a stalled read.
    """
    printer = core.get_printer(printer_id)
    if not core.camera_mod.ffmpeg_exe():
        raise core.HTTPException(
            status_code=503,
            detail="ffmpeg is not installed. Run: pip install imageio-ffmpeg (or install ffmpeg).",
        )

    try:
        stream = await core.run_blocking(
            core.camera_mod.start_mjpeg_stream, printer_id, printer.ip, printer.access_code, width, fps
        )
    except Exception as exc:
        core.logger.error("Could not start the MJPEG stream for %s: %s", printer_id, exc)
        raise core.HTTPException(status_code=502, detail=f"Camera stream unavailable: {exc}")

    loop = asyncio.get_running_loop()

    async def chunks():
        try:
            while True:
                if await request.is_disconnected():
                    core.logger.info("Viewer left the MJPEG stream for %s", printer_id)
                    break
                chunk = await loop.run_in_executor(None, stream.read, 4096)
                if not chunk:
                    break
                yield chunk
        except Exception as exc:  # client left, camera busy, ffmpeg died
            core.logger.info("MJPEG stream for %s ended: %s", printer_id, exc)
        finally:
            # stop off the loop: terminate() may wait for the process to exit
            await loop.run_in_executor(None, core.camera_mod.stop_mjpeg_stream, printer_id, stream)

    return core.StreamingResponse(
        chunks(),
        media_type=f"multipart/x-mixed-replace; boundary={core.camera_mod.MJPEG_BOUNDARY}",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/printers/{printer_id}/camera/stream")
async def camera_stream(printer_id: str):
    printer = core.get_printer(printer_id)
    try:
        content_type, chunks = await core.run_blocking(core.camera_mod.open_stream, printer.ip)
    except Exception as exc:
        raise core.HTTPException(status_code=502, detail=f"Camera stream unavailable: {exc}")
    return core.StreamingResponse(
        chunks,
        media_type=content_type,
        headers={"Cache-Control": "no-store", "Connection": "close"},
    )


@router.get("/api/printers/{printer_id}/camera/url")
async def camera_url(printer_id: str):
    """The full RTSPS URL (contains the access code) - kept out of /api/printers."""
    printer = core.get_printer(printer_id)
    return {"url": core.camera_mod.rtsp_url(printer.ip, printer.access_code), "port": core.camera_mod.RTSP_PORT}


@router.get("/api/printers/{printer_id}/camera/sdp")
async def camera_sdp(printer_id: str):
    """H.264 parameters (SPS/PPS, codec) so the browser can start a decoder."""
    printer = core.get_printer(printer_id)
    try:
        info = await core.run_blocking(core.camera_mod.rtsp_sdp, printer.ip, printer.access_code)
    except Exception as exc:
        raise core.HTTPException(status_code=502, detail=f"Camera RTSP handshake failed: {exc}")
    return info


@router.get("/api/printers/{printer_id}/camera/h264")
async def camera_h264(printer_id: str):
    """Stream the printer's H.264 camera as an Annex-B byte stream.

    The browser decodes this with WebCodecs; see ``static/js/app.js``.
    """
    printer = core.get_printer(printer_id)

    def chunks():
        frames = core.camera_mod.rtsp_h264_stream(printer.ip, printer.access_code)
        try:
            for nal in frames:
                yield nal
        except Exception as exc:  # camera unplugged, session dropped, client left
            core.logger.info("Camera stream from %s ended: %s", printer_id, exc)
        finally:
            frames.close()

    return core.StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
