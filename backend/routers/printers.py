"""Printer CRUD, telemetry and every dispatched command.

Handlers only: shared helpers, config and the app itself live in
``backend.main`` (imported here as ``core`` so monkeypatched module values -
paths, the MQTT manager - resolve at call time, not import time).
"""
from fastapi import APIRouter
import asyncio
import os
import platform
import posixpath
import sys
import time
from typing import Dict, List

from backend import main as core

router = APIRouter()


@router.post("/api/logout")
async def do_logout():
    response = core.JSONResponse({"status": "ok"})
    response.delete_cookie(core.AUTH_COOKIE)
    return response


@router.get("/api/diagnostics")
async def diagnostics(lines: int = core.Query(120, ge=1, le=2000)):
    """One-shot support bundle: version, runtime, per-printer link state and
    the tail of the server log. Access codes and RTSP credentials are redacted,
    so the payload is safe to paste into an issue.
    """
    printers = core.read_printers()
    hide = [p.access_code for p in printers.values()]
    clients = core.mqtt_manager.status() if core.mqtt_manager else {}

    log_tail: List[str] = []
    log_path = os.path.join(core.DATA_DIR, "tray.log")
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                log_tail = [core._redact(line.rstrip("\n"), hide) for line in fh.readlines()[-lines:]]
        except OSError as exc:
            log_tail = [f"<could not read log: {exc}>"]

    return {
        "version": core._app_version(),
        "uptime_seconds": round(time.time() - core._STARTED_AT),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "data_dir": core.DATA_DIR,
        "mqtt": {pid: st for pid, st in clients.items()},
        # note: no access_code - the security invariant is that codes never
        # leave the server, not even in a diagnostics bundle
        "printers": [
            {
                "id": p.id,
                "name": p.name,
                "ip": p.ip,
                "sn": p.sn,
                "bed_type": p.bed_type,
                "ams_count": p.ams_count,
            }
            for p in printers.values()
        ],
        "log_path": log_path,
        "log_tail": log_tail,
    }


@router.get("/api/printers", response_model=List[core.PrinterPublic])
async def list_printers():
    """Registered nodes. The access code is never included in the response."""
    return [
        core.PrinterPublic(
            id=p.id, name=p.name, ip=p.ip, sn=p.sn, bed_type=p.bed_type,
            ams_count=p.ams_count, has_access_code=bool(p.access_code),
        )
        for p in core.read_printers().values()
    ]


@router.post("/api/printers")
async def add_or_update_printer(printer: core.PrinterConfig):
    printers = core.read_printers()
    # An empty access code means "keep the one already stored" (the API never
    # hands the code back, so the edit dialog submits it blank).
    existing = printers.get(printer.id)
    if existing and not printer.access_code:
        printer.access_code = existing.access_code
    if not printer.access_code:
        raise core.HTTPException(status_code=400, detail="access code is required")
    printers[printer.id] = printer
    core.save_printers(printers)
    core.require_manager().register_printer(printer.id, printer.ip, printer.sn, printer.access_code)
    return {"status": "ok", "printer": core.PrinterPublic(
        id=printer.id, name=printer.name, ip=printer.ip, sn=printer.sn,
        bed_type=printer.bed_type, ams_count=printer.ams_count, has_access_code=True,
    )}


@router.delete("/api/printers/{printer_id}")
async def remove_printer(printer_id: str):
    printers = core.read_printers()
    if printer_id not in printers:
        raise core.HTTPException(status_code=404, detail="Printer node not found")
    del printers[printer_id]
    core.save_printers(printers)
    core.state.drop_report(printer_id)
    core.require_manager().unregister_printer(printer_id)
    return {"status": "deleted"}


@router.get("/api/printers/{printer_id}/telemetry")
async def printer_telemetry(printer_id: str):
    core.get_printer(printer_id)
    return {
        "printer_id": printer_id,
        "homed": core.is_homed(printer_id),
        "data": core.state.get_report(printer_id) or {},
    }


@router.post("/api/dispatch-print", response_model=core.DispatchResponse)
async def dispatch_print(cmd: core.PrintDispatchCommand):
    printers = core.read_printers()
    if cmd.printer_id not in printers:
        raise core.HTTPException(status_code=404, detail="Target printer node not found")

    printer = printers[cmd.printer_id]
    local_file = os.path.join(core.UPLOAD_DIR, cmd.filename)
    if not os.path.exists(local_file):
        raise core.HTTPException(status_code=400, detail="Target 3MF file not found in upload cache")

    # Work out the plate before touching the printer (see print_remote).
    plates = core.local_plate_indices(local_file)
    if not plates:
        raise core.HTTPException(
            status_code=400,
            detail=f"No printable plate (Metadata/plate_N.gcode) found inside {cmd.filename}.",
        )
    plate = core.pick_plate(plates, cmd.plate_index, cmd.filename)

    # Step 1: push the sliced file over implicit FTPS.
    upload_success = await core.run_blocking(core.upload_3mf_file, printer.ip, printer.access_code, local_file)
    if not upload_success:
        raise core.HTTPException(status_code=502, detail="FTPS transfer failed")

    # Step 2: refuse politely if the printer is stuck, then start the job.
    await core.ensure_not_failed(cmd.printer_id)
    await core.dispatch(cmd.printer_id, "print_option", {"bed_type": printer.bed_type})
    project_file = {
        "print": {
            "sequence_id": "1",
            "command": "project_file",
            "param": f"Metadata/plate_{plate}.gcode",
            "subtask_name": cmd.filename,
            "url": f"file:///sdcard/{cmd.filename}",
            "bed_type": printer.bed_type,
            "bed_levelling": cmd.bed_levelling,
            "flow_cali": cmd.flow_cali,
            "vibration_cali": cmd.vibration_cali,
            "timelapse": cmd.timelapse,
            "use_ams": cmd.use_ams,
        }
    }
    if not await core.run_blocking(core.require_manager().send_to_printer, cmd.printer_id, project_file):
        raise core.HTTPException(
            status_code=503,
            detail="Printer node is offline over MQTTS - the print was not started.",
        )
    await core.verify_started(cmd.printer_id, cmd.filename)
    skipped = await core.inject_pre_skip(cmd.printer_id, cmd.skip_object_ids)
    return core.DispatchResponse(
        status="started", printer=printer.name, job=cmd.filename, skipped=skipped
    )


@router.post("/api/printers/{printer_id}/pause")
async def pause_printer(printer_id: str):
    return await core.dispatch(printer_id, "pause")


@router.post("/api/printers/{printer_id}/resume")
async def resume_printer(printer_id: str):
    return await core.dispatch(printer_id, "resume")


@router.post("/api/printers/{printer_id}/stop")
async def stop_printer(printer_id: str):
    return await core.dispatch(printer_id, "stop")


@router.post("/api/printers/{printer_id}/speed")
async def change_speed(printer_id: str, cmd: core.SpeedCommand):
    return await core.dispatch(printer_id, "speed", {"level": cmd.speed_level})


@router.post("/api/printers/{printer_id}/speed-profile")
async def change_speed_profile(printer_id: str, cmd: core.SpeedProfileCommand):
    return await core.dispatch(printer_id, "speed", {"level": cmd.level})


@router.post("/api/printers/{printer_id}/light")
async def toggle_light(printer_id: str, cmd: core.LightCommand):
    return await core.dispatch(printer_id, "light", {"mode": "on" if cmd.state else "off"})


@router.post("/api/printers/{printer_id}/light-mode")
async def light_mode(printer_id: str, cmd: core.LightModeCommand):
    return await core.dispatch(printer_id, "light", cmd.model_dump())


@router.post("/api/printers/{printer_id}/calibrate")
async def calibrate(printer_id: str, cmd: core.CalibrationCommand):
    """Home first, then run the routine - so it is never started from an unknown pose."""
    core.get_printer(printer_id)
    params: Dict[str, object] = {}
    if cmd.kind == "flow_calibration":
        params = {"mode": cmd.mode, "state": "enable" if cmd.mode else "disable"}

    # 1) home all axes (this is itself a motion command, gated by confirm_motion)
    await core.dispatch(printer_id, "home", {"axis": ""}, confirm=cmd.confirm_motion)
    core.set_homed(printer_id, True)
    await asyncio.sleep(core.CALIBRATION_HOME_DELAY)  # let the printer finish homing

    # 2) run the routine; homing is already done, so the check is satisfied
    return await core.dispatch(printer_id, cmd.kind, params, confirm=cmd.confirm_motion, assume_homed=True)


@router.post("/api/printers/{printer_id}/ams")
async def ams_action(printer_id: str, cmd: core.AmsActionCommand):
    mapping = {
        "feed": "ams_feed",
        "unload": "ams_unload",
        "resume": "ams_resume",
        "tray_select": "ams_tray_select",
        "tray_info": "ams_tray_info",
        "current_tray": "ams_current_tray",
        "filament_setting": "ams_filament_setting",
        "auto": "ams_auto",
    }
    params = {
        "ams_id": cmd.ams_id,
        "tray_id": cmd.tray_id,
        "nozzle_temp": cmd.nozzle_temp,
        "tray_type": cmd.tray_type,
        "color": cmd.color,
        "remaining": cmd.remaining,
    }
    return await core.dispatch(
        printer_id, mapping[cmd.action], params, confirm=cmd.confirm_thermal
    )


@router.post("/api/printers/{printer_id}/skip-objects")
async def skip_objects(printer_id: str, cmd: core.SkipObjectCommand):
    if not cmd.object_ids:
        raise core.HTTPException(status_code=400, detail="No object ids supplied")
    return await core.dispatch(printer_id, "skip_objects", {"object_ids": cmd.object_ids})


@router.post("/api/printers/{printer_id}/print-option")
async def print_option(printer_id: str, cmd: core.PrintOptionCommand):
    params = {k: v for k, v in cmd.model_dump().items() if v is not None}
    return await core.dispatch(printer_id, "print_option", params)


@router.post("/api/printers/{printer_id}/retry")
async def retry_after_error(printer_id: str):
    return await core.dispatch(printer_id, "print_error", {"err_code": 0, "sub_err_code": 0})


@router.post("/api/printers/{printer_id}/clear-error")
async def clear_error(printer_id: str):
    """Best-effort clear of a failed print, including any active HMS faults.

    Returns what was tried and whether the printer left the FAILED state. Some
    faults (notably storage/HMS ones) can only be dismissed on the touchscreen.
    """
    core.get_printer(printer_id)
    report = core.state.get_report(printer_id) or {}
    attempts: List[dict] = []

    # 1) the documented clean_print_error, with the real error code (as OrcaSlicer
    #    sends it: print_error + subtask_id)
    attempts.append(await core.dispatch(
        printer_id, "clear_error",
        {"print_error": report.get("print_error") or 0,
         "subtask_id": report.get("subtask_id") or "0"},
        confirm=True,
    ))
    await asyncio.sleep(1)

    # 2) HMS acknowledgements (idle_ignore for every active fault)
    for entry in (report.get("hms") or [])[:2]:
        if not isinstance(entry, dict):
            continue
        attr, code = int(entry.get("attr", 0) or 0), int(entry.get("code", 0) or 0)
        for err in (core.format_hms(entry), f"{attr:08X}{code:08X}"):
            try:
                attempts.append(await core.dispatch(
                    printer_id, "hms_ignore",
                    {"action": "idle_ignore", "err": err, "type": 1},
                    confirm=True,
                ))
                break
            except core.HTTPException as exc:
                attempts.append({"command": "hms_ignore", "err": err, "error": exc.detail})

    await asyncio.sleep(2)
    gcode_state = str((core.state.get_report(printer_id) or {}).get("gcode_state", "")).upper()
    cleared = gcode_state != "FAILED"
    if not cleared:
        core.logger.warning("Failed state on %s survived every clear attempt", printer_id)
    return {"status": "attempted", "state": gcode_state.lower(), "cleared": cleared, "attempts": attempts}


@router.post("/api/printers/{printer_id}/filament")
async def set_filament(printer_id: str, cmd: core.FilamentSettingCommand):
    """Write a filament record (type, colour, remaining) into an AMS tray.

    Metadata only - nothing heats or moves - so no thermal confirmation is needed.
    """
    core.get_printer(printer_id)
    colour = cmd.color.lstrip("#").upper()
    if len(colour) == 6:
        colour += "FF"
    return await core.dispatch(printer_id, "ams_filament_setting", {
        "ams_id": cmd.ams_id,
        "tray_id": cmd.tray_id,
        "tray_type": cmd.tray_type,
        "color": "#" + colour,
        "remaining": cmd.remaining,
        "setting_id": cmd.setting_id,
        "sub_brands": cmd.sub_brands,
        "nozzle_temp_min": cmd.temp_min,
        "nozzle_temp_max": cmd.temp_max,
    })


@router.post("/api/printers/{printer_id}/temperature")
async def set_temperature(printer_id: str, cmd: core.TemperatureCommand):
    """Manual nozzle / bed / chamber setpoints, capped and gated."""
    core.get_printer(printer_id)
    targets = cmd.targets
    if not targets:
        raise core.HTTPException(status_code=400, detail="No temperature values supplied")
    if not cmd.confirm_thermal:
        raise core.HTTPException(
            status_code=409,
            detail="Setting temperatures heats the machine. Re-send with confirm_thermal=true.",
        )

    report = core.state.get_report(printer_id) or {}
    gcode_state = str(report.get("gcode_state", "")).upper()
    if gcode_state not in ("", "IDLE", "FINISH", "FAILED") and not cmd.allow_while_printing:
        raise core.HTTPException(
            status_code=409,
            detail=(
                f"Printer is {gcode_state.lower()} - changing setpoints now would fight the running job. "
                "Pause or stop it first, or re-send with allow_while_printing=true."
            ),
        )

    sent = {}
    for target, value in targets.items():
        low, high = core.cmd_mod.TEMP_LIMITS[target]
        if not low <= value <= high:
            raise core.HTTPException(
                status_code=400,
                detail=f"{target} temperature must be between {low} and {high} C (got {value})",
            )
        sent[target] = await core.dispatch(
            printer_id, f"set_{target}_temp", {"temp": value}, confirm=True
        )
    core.logger.warning("TEMPERATURE change on %s: %s", printer_id, sent)
    return {"status": "sent", "targets": targets, "printer": printer_id}


@router.post("/api/printers/{printer_id}/fan")
async def set_fan(printer_id: str, cmd: core.FanCommand):
    """Set a cooling fan's speed (percentage).

    Moves no axes and heats nothing, so it is a `safe` command with no
    confirmation gate - but it is still worth knowing which fan index the
    firmware uses, hence each control sits next to that fan's reported speed.
    """
    core.get_printer(printer_id)
    try:
        return await core.dispatch(printer_id, "set_fan", {"fan": cmd.fan, "speed": cmd.speed})
    except ValueError as exc:
        raise core.HTTPException(status_code=400, detail=str(exc))


@router.post("/api/printers/{printer_id}/home")
async def home_axes(printer_id: str, cmd: core.HomeCommand):
    """Home all axes, or just one. Allowed while unhomed - that is the point."""
    core.get_printer(printer_id)
    result = await core.dispatch(printer_id, "home", {"axis": cmd.axes}, confirm=cmd.confirm_motion)
    if not cmd.axes:
        core.set_homed(printer_id, True)  # we just homed everything
    return result


@router.post("/api/printers/{printer_id}/jog")
async def jog_axis(printer_id: str, cmd: core.JogCommand):
    """Move one axis by a small relative amount. Requires homing + confirmation."""
    core.get_printer(printer_id)
    return await core.dispatch(
        printer_id,
        "jog",
        {"axis": cmd.axis.upper(), "distance": cmd.distance, "feedrate": cmd.feedrate},
        confirm=cmd.confirm_motion,
    )


@router.post("/api/printers/{printer_id}/extrude")
async def extrude(printer_id: str, cmd: core.ExtrudeCommand):
    """Extrude or retract filament - refuses on a cold nozzle to avoid grinding."""
    core.get_printer(printer_id)
    report = core.state.get_report(printer_id) or {}
    gcode_state = str(report.get("gcode_state", "")).upper()
    if gcode_state not in ("", "IDLE", "FINISH", "FAILED"):
        raise core.HTTPException(
            status_code=409,
            detail=f"Printer is {gcode_state.lower()} - stop the job before moving the extruder.",
        )
    nozzle = report.get("nozzle_temper")
    target = report.get("nozzle_target_temper") or 0
    hot = isinstance(nozzle, (int, float)) and (nozzle >= 170 or target >= 170)
    if cmd.amount > 0 and not hot:
        raise core.HTTPException(
            status_code=409,
            detail=(
                "Nozzle is below 170 C - extruding now would grind the filament. "
                "Heat the nozzle first (Temperatures tab)."
            ),
        )
    return await core.dispatch(
        printer_id,
        "extrude",
        {"amount": cmd.amount, "feedrate": cmd.feedrate},
        confirm=cmd.confirm_thermal,
    )


@router.post("/api/printers/{printer_id}/reboot")
async def reboot(printer_id: str, cmd: core.RebootCommand):
    return await core.dispatch(printer_id, "restart_module", {"module": cmd.module})


@router.post("/api/printers/{printer_id}/refresh")
async def refresh_state(printer_id: str):
    core.get_printer(printer_id)
    return await core.dispatch(printer_id, "push_state")


@router.post("/api/printers/{printer_id}/command")
async def generic_command(printer_id: str, cmd: core.GenericCommand):
    core.get_printer(printer_id)
    params = dict(cmd.params)
    confirm = bool(params.pop("confirm", False))
    return await core.dispatch(printer_id, cmd.command, params, confirm=confirm)


@router.post("/api/printers/{printer_id}/print-remote", response_model=core.DispatchResponse)
async def print_remote(printer_id: str, cmd: core.PrintRemoteCommand):
    """Start a project that already lives on the printer's SD card."""
    printer = core.get_printer(printer_id)
    if cmd.path == "/":
        raise core.HTTPException(status_code=400, detail="A project file must be selected")
    if not cmd.path.lower().endswith(core.ALLOWED_SUFFIXES):
        raise core.HTTPException(status_code=400, detail="Only .3mf / .gcode.3mf projects can be printed")

    name = posixpath.basename(cmd.path)

    # A sliced project can hold several plates; the printer must be told exactly
    # which one to run or it reports the file as unreadable. Work it out from the
    # archive (falling back to the `_plate_N` in the file name).
    plates = await core.run_blocking(
        core.detect_plate_indices, printer.ip, printer.access_code, cmd.path
    )
    if not plates:
        raise core.HTTPException(
            status_code=400,
            detail=f"No printable plate (Metadata/plate_N.gcode) found inside {name}.",
        )
    plate = core.pick_plate(plates, cmd.plate_index, name)
    core.logger.info("Printing %s plate %s (file has plates %s)", name, plate, plates)

    # A printer left in FAILED will refuse any new job.
    await core.ensure_not_failed(printer_id)
    await core.dispatch(printer_id, "print_option", {"bed_type": printer.bed_type})

    project_file = {
        "print": {
            "sequence_id": "1",
            "command": "project_file",
            "param": f"Metadata/plate_{plate}.gcode",
            "subtask_name": name,
            # the printer addresses its SD card as /sdcard
            "url": f"file:///sdcard{cmd.path}",
            "bed_type": printer.bed_type,
            "bed_levelling": cmd.bed_levelling,
            "layer_inspect": cmd.layer_inspect,
            "flow_cali": cmd.flow_cali,
            "vibration_cali": cmd.vibration_cali,
            "timelapse": cmd.timelapse,
            "use_ams": cmd.use_ams,
            "profile_id": "0",
            "project_id": "0",
            "subtask_id": "0",
            "task_id": "0",
            "md5": "",
        }
    }
    if cmd.ams_mapping is not None:
        project_file["print"]["ams_mapping"] = [int(v) for v in cmd.ams_mapping]
    if not await core.run_blocking(core.require_manager().send_to_printer, printer_id, project_file):
        raise core.HTTPException(
            status_code=503,
            detail="Printer node is offline over MQTTS - the print was not started.",
        )

    # Give the printer a moment and report back whether it actually accepted it -
    # a 200 here only means the MQTT publish succeeded.
    await core.verify_started(printer_id, name)
    skipped = await core.inject_pre_skip(printer_id, cmd.skip_object_ids)
    return core.DispatchResponse(status="started", printer=printer.name, job=name, skipped=skipped)
