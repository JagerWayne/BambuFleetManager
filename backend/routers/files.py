"""Staged uploads and SD-card browsing, transfer and deletion.

Handlers only: shared helpers, config and the app itself live in
``backend.main`` (imported here as ``core`` so monkeypatched module values -
paths, the MQTT manager - resolve at call time, not import time).
"""
from fastapi import APIRouter
import os
import posixpath
from typing import List, Optional

from backend import main as core

router = APIRouter()


@router.get("/api/uploads", response_model=List[core.UploadEntry])
async def list_uploads():
    """Files currently staged on the server (so the queue survives a refresh)."""
    entries: List[core.UploadEntry] = []
    for name in sorted(os.listdir(core.UPLOAD_DIR)):
        path = os.path.join(core.UPLOAD_DIR, name)
        if not os.path.isfile(path) or name.startswith("."):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        entries.append(core.UploadEntry(filename=name, size=stat.st_size, modified=stat.st_mtime))
    return entries


@router.delete("/api/uploads/{filename}")
async def delete_upload(filename: str):
    safe = os.path.basename(filename)
    path = os.path.join(core.UPLOAD_DIR, safe)
    if not os.path.isfile(path):
        raise core.HTTPException(status_code=404, detail="Staged file not found")
    os.remove(path)
    return {"status": "deleted", "filename": safe}


@router.post("/api/stage-upload", response_model=core.StagedFile)
async def stage_upload(file: core.UploadFile = core.File(...)):
    filename = os.path.basename(file.filename or "")
    if not filename.lower().endswith(core.ALLOWED_SUFFIXES):
        raise core.HTTPException(status_code=400, detail="Only .3mf / .gcode.3mf projects can be staged")

    dest_path = os.path.join(core.UPLOAD_DIR, filename)
    size = await core._save_upload(file, dest_path)
    core.logger.info("Staged %s (%d bytes)", filename, size)
    return core.StagedFile(filename=filename, size=size)


@router.get("/api/printers/{printer_id}/files/plan")
async def print_plan(printer_id: str, path: str = core.Query(..., max_length=512)):
    """Everything the print dialog needs: plates, time/weight and filaments."""
    printer = core.get_printer(printer_id)
    target = core.normalize_remote_path(path)
    try:
        entries = await core.run_blocking(
            core.read_remote_zip_entries, printer.ip, printer.access_code, target,
            ["Metadata/slice_info.config"],
        )
    except Exception as exc:
        raise core.HTTPException(status_code=502, detail=f"Could not read the project: {exc}")
    raw = entries.get("Metadata/slice_info.config")
    plates = core.parse_slice_info(raw.decode("utf-8", "replace")) if raw else []
    if not plates:
        plates = [{"index": i, "time_sec": 0, "weight_g": 0, "filaments": []}
                  for i in await core.run_blocking(core.detect_plate_indices, printer.ip, printer.access_code, target)]
    return {
        "path": target,
        "name": posixpath.basename(target),
        **core.available_trays(printer_id),
        "plates": plates,
    }


@router.get("/api/printers/{printer_id}/files/plate")
async def print_plate(
    printer_id: str,
    path: str = core.Query(..., max_length=512),
    plate: int = core.Query(1, ge=1),
    size: str = core.Query("large"),
):
    """Serve the plate thumbnail baked into the project (Metadata/plate_N.png)."""
    printer = core.get_printer(printer_id)
    target = core.normalize_remote_path(path)
    suffix = "small" if size == "small" else ""
    candidates = [
        f"Metadata/plate_{plate}{'_small' if suffix else ''}.png",
        f"Metadata/plate_{plate}.png",
        f"Metadata/top_{plate}.png",
    ]
    try:
        entries = await core.run_blocking(
            core.read_remote_zip_entries, printer.ip, printer.access_code, target, candidates
        )
    except Exception as exc:
        raise core.HTTPException(status_code=502, detail=f"Could not read the project: {exc}")
    for name in candidates:
        data = entries.get(name)
        if data:
            return core.Response(content=data, media_type="image/png",
                                 headers={"Cache-Control": "no-store"})
    raise core.HTTPException(status_code=404, detail="No thumbnail for that plate")


@router.get("/api/printers/{printer_id}/files/objects")
async def print_objects(
    printer_id: str,
    path: str = core.Query(..., max_length=512),
    plate: Optional[int] = core.Query(None, ge=1),
):
    """Per-object plate geometry for the top-down "skip object" bed diagram.

    Reads ``Metadata/plate_<n>.json`` for the bed layout and
    ``Metadata/slice_info.config`` for the ids the skip command needs.

    The returned object ``id`` is the ``slice_info.config`` ``<object
    identify_id>`` (what ``print.skip_objects`` expects) - *not* the
    ``plate_<n>.json`` ``bbox_objects[].id``. The two lists are emitted in the
    same slicer order, so they are correlated by index; ``plate_id`` echoes the
    original bbox id and geometry keeps its ``bbox``. Objects without a
    matching slice entry keep their bbox id, so nothing regresses.
    """
    printer = core.get_printer(printer_id)
    target = core.normalize_remote_path(path)

    if plate is None:
        try:
            entries = await core.run_blocking(
                core.read_remote_zip_entries, printer.ip, printer.access_code, target,
                ["Metadata/slice_info.config"],
            )
        except Exception as exc:
            raise core.HTTPException(status_code=502, detail=f"Could not read the project: {exc}")
        raw = entries.get("Metadata/slice_info.config")
        plates = core.parse_slice_info(raw.decode("utf-8", "replace")) if raw else []
        if plates:
            plate = plates[0]["index"] or 1
        else:
            detected = await core.run_blocking(
                core.detect_plate_indices, printer.ip, printer.access_code, target
            )
            plate = detected[0] if detected else 1

    entry = f"Metadata/plate_{plate}.json"
    slice_entry = "Metadata/slice_info.config"
    try:
        entries = await core.run_blocking(
            core.read_remote_zip_entries, printer.ip, printer.access_code, target,
            [entry, slice_entry],
        )
    except Exception as exc:
        raise core.HTTPException(status_code=502, detail=f"Could not read the project: {exc}")
    raw = entries.get(entry)
    if not raw:
        raise core.HTTPException(
            status_code=404,
            detail=f"No {entry} in the project - it is not a sliced 3MF with a plate map.",
        )
    geometry = core.parse_plate_geometry(raw.decode("utf-8", "replace"))

    # Correlate the bbox objects with slice_info identify_ids by slicer index.
    slice_objects = []
    slice_raw = entries.get(slice_entry)
    if slice_raw:
        for pl in core.parse_slice_info(slice_raw.decode("utf-8", "replace")):
            if pl.get("index") == plate:
                slice_objects = pl.get("objects") or []
                break
    geometry["objects"] = core.merge_plate_object_ids(geometry, slice_objects)
    return {"path": target, "plate": plate, **geometry}


@router.get("/api/printers/{printer_id}/files", response_model=core.DirectoryListing)
async def browse_files(printer_id: str, path: str = core.Query("/", max_length=512)):
    printer = core.get_printer(printer_id)
    target = core.normalize_remote_path(path)
    try:
        entries = await core.run_blocking(core.list_directory, printer.ip, printer.access_code, target)
    except Exception as exc:
        core.logger.error("Listing %s%s on %s failed: %s", target, "", printer_id, exc)
        raise core.HTTPException(status_code=502, detail=f"FTPS listing failed: {exc}")

    parent = None if target == "/" else posixpath.dirname(target) or "/"
    return core.DirectoryListing(
        path=target,
        parent=parent,
        entries=[
            core.FileNode(**(e.to_dict() if hasattr(e, "to_dict") else e)) for e in entries
        ],
    )


@router.delete("/api/printers/{printer_id}/files")
async def delete_remote_file(printer_id: str, cmd: core.DeleteEntryCommand):
    printer = core.get_printer(printer_id)
    try:
        await core.run_blocking(core.delete_path, printer.ip, printer.access_code, cmd.path, cmd.is_dir)
    except ValueError as exc:
        raise core.HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise core.HTTPException(status_code=502, detail=f"Delete failed: {exc}")
    return {"status": "deleted", "path": cmd.path}


@router.post("/api/printers/{printer_id}/files/rename")
async def rename_remote_file(printer_id: str, cmd: core.RenameEntryCommand):
    """Rename a .3mf project on the SD card, in place.

    Folders and non-project files cannot be renamed, the extension is pinned to
    ``.3mf`` (a renamed project that lost its suffix would stop being printable
    and would fall out of the Print files filter), and the name may not contain a
    path, so a rename can never move a file into another folder.
    """
    printer = core.get_printer(printer_id)
    source = core.normalize_remote_path(cmd.path)
    name = cmd.new_name.strip()

    if not source.lower().endswith(".3mf"):
        raise core.HTTPException(status_code=400, detail="Only .3mf files can be renamed.")
    if not name.lower().endswith(".3mf"):
        raise core.HTTPException(status_code=400, detail="The new name must end in .3mf.")
    if name in (".", "..") or "/" in name or "\\" in name:
        raise core.HTTPException(status_code=400, detail="The new name must not contain a path.")

    parent = posixpath.dirname(source) or "/"
    target = posixpath.join(parent, name)
    if target == source:
        return {"status": "unchanged", "path": source, "name": name}

    try:
        new_path = await core.run_blocking(
            core.rename_path, printer.ip, printer.access_code, source, name
        )
    except ValueError as exc:
        raise core.HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise core.HTTPException(status_code=502, detail=f"Rename failed: {exc}")
    return {"status": "renamed", "path": new_path, "name": name}


@router.post("/api/printers/{printer_id}/files/upload-staged")
async def upload_staged_file(printer_id: str, cmd: core.SendStagedCommand):
    """Send a file already staged on this server to the printer's SD card.

    This is the mobile-friendly replacement for dragging a staged file onto the
    card: send it, then run the normal print preview on the uploaded path.
    """
    printer = core.get_printer(printer_id)
    name = cmd.filename.strip()
    if not name.lower().endswith(core.ALLOWED_SUFFIXES):
        raise core.HTTPException(status_code=400, detail="Only .3mf / .gcode.3mf projects can be sent")
    if "/" in name or "\\" in name or ".." in name:
        raise core.HTTPException(status_code=400, detail="filename must not contain a path")

    local_path = os.path.join(core.UPLOAD_DIR, name)
    if not os.path.exists(local_path):
        raise core.HTTPException(status_code=404, detail=f"{name} is not staged on this server")

    target_dir = core.normalize_remote_path(cmd.dir_path)
    try:
        ok = await core.run_blocking(
            core.upload_3mf_file, printer.ip, printer.access_code, local_path, target_dir
        )
    except Exception as exc:
        raise core.HTTPException(status_code=502, detail=f"Upload failed: {exc}")
    if not ok:
        raise core.HTTPException(status_code=502, detail="FTPS transfer failed")

    remote = posixpath.join(target_dir, name) if target_dir != "/" else f"/{name}"
    core.logger.info("Staged file %s sent to %s at %s", name, printer_id, remote)
    return {"status": "sent", "path": remote, "name": name, "dir": target_dir}


@router.get("/api/printers/{printer_id}/files/download")
async def download_remote_file(printer_id: str, path: str = core.Query(..., max_length=512)):
    printer = core.get_printer(printer_id)
    target = core.normalize_remote_path(path)
    filename = posixpath.basename(target) or "download.bin"

    # A streaming response cannot change its status once started, so confirm the
    # file is really there before committing to a 200.
    if not await core.run_blocking(core.remote_file_exists, printer.ip, printer.access_code, target):
        raise core.HTTPException(status_code=404, detail=f"{target} not found on the SD card")

    core.logger.info("Streaming %s from %s to a client", target, printer_id)

    def iterator():
        try:
            for chunk in core.download_stream(printer.ip, printer.access_code, target):
                yield chunk
        except Exception as exc:  # client disconnected mid-transfer
            core.logger.info("Download of %s aborted: %s", target, exc)

    return core.StreamingResponse(
        iterator(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/printers/{printer_id}/files/upload-dir")
async def upload_into_dir(printer_id: str, dir_path: core.RemotePath, file: core.UploadFile = core.File(...)):
    """Upload a staged file into an arbitrary SD-card folder."""
    printer = core.get_printer(printer_id)
    filename = os.path.basename(file.filename or "")
    if not filename.lower().endswith(core.ALLOWED_SUFFIXES):
        raise core.HTTPException(status_code=400, detail="Only .3mf / .gcode.3mf projects can be uploaded")

    local_path = os.path.join(core.UPLOAD_DIR, filename)
    if not os.path.exists(local_path):
        await core._save_upload(file, local_path)

    ok = await core.run_blocking(
        core.upload_3mf_file, printer.ip, printer.access_code, local_path, dir_path.path
    )
    if not ok:
        raise core.HTTPException(status_code=502, detail="FTPS transfer failed")
    return {"status": "uploaded", "path": posixpath.join(dir_path.path, filename)}
