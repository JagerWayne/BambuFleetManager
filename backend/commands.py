"""Central registry of every MQTT command this server can issue to a printer.

Keeping the protocol payloads in one module means the transport layer
(:mod:`backend.mqtt_manager`) stays a dumb pipe and the REST layer stays a
thin, validated shim over the builders below.

Firmware note: Bambu firmware revisions differ slightly in which optional
sub-fields they accept. Every builder here sends the payload shape used by
the current 1.x LAN protocol; a few entries (``flow_calibration``,
``vibration_calibration``, ``restart_module``) are best-effort and depend on
the firmware on the printer.
"""

from typing import Any, Callable, Dict, List, Optional

from backend.filaments import FILAMENTS

CommandBuilder = Callable[[Dict[str, Any]], Dict[str, Any]]

# --------------------------------------------------------------------------
# print channel
# --------------------------------------------------------------------------


def _print_cmd(command: str, **extra: Any) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def builder(params: Dict[str, Any]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"command": command}
        payload.update({k: v for k, v in extra.items() if v is not None})
        payload.update(params)
        return {"print": payload}

    return builder


def build_print_speed(params: Dict[str, Any]) -> Dict[str, Any]:
    """Speed profile select.

    The firmware takes the **numeric** level in ``param`` and only reflects the
    change for those; sending the profile *name* is answered with ``SUCCESS``
    but silently ignored (verified on an X1C). Level 0 is a no-op.
    """
    level = int(params.get("level", 2))
    if level not in SPEED_PROFILES:
        level = 2
    return {"print": {"command": "print_speed", "param": str(level)}}


def build_print_option(params: Dict[str, Any]) -> Dict[str, Any]:
    """Filament / nozzle profile applied when a print starts."""
    option: Dict[str, Any] = {}
    if params.get("filament_id"):
        option["filament_id"] = params["filament_id"]
    if params.get("filament_type"):
        option["filament_type"] = params["filament_type"]
    if params.get("nozzle_diameter"):
        option["nozzle_diameter"] = str(params["nozzle_diameter"])
    if params.get("nozzle_temp") is not None:
        option["nozzle_temp"] = int(params["nozzle_temp"])
    if params.get("bed_type"):
        option["bed_type"] = params["bed_type"]
    return {"print": {"command": "print_option", "print_option": option}}


def build_skip_objects(params: Dict[str, Any]) -> Dict[str, Any]:
    objects = params.get("object_ids") or []
    return {"print": {"command": "skip_objects", "obj_list": [int(o) for o in objects]}}


def build_clear_error(params: Dict[str, Any]) -> Dict[str, Any]:
    """Dismiss the printer's last print error.

    OrcaSlicer sends ``clean_print_error`` with the job's ``subtask_id`` and the
    ``print_error`` code; the firmware answers SUCCESS. ``gcode_state`` can stay
    FAILED when an HMS fault is also active - that needs :func:`build_hms_ignore`.
    """
    payload: Dict[str, Any] = {"command": "clean_print_error"}
    if params.get("subtask_id") is not None:
        payload["subtask_id"] = str(params["subtask_id"])
    if params.get("print_error") is not None:
        payload["print_error"] = int(params["print_error"])
    return {"print": payload}


def build_hms_ignore(params: Dict[str, Any]) -> Dict[str, Any]:
    """Acknowledge an HMS (health management) fault.

    Mirrors OrcaSlicer's ``command_hms_*``: ``idle_ignore`` for a printer that is
    not printing, ``ignore``/``resume``/``stop`` for a job-related fault.
    """
    action = str(params.get("action", "idle_ignore"))
    if action not in ("idle_ignore", "ignore", "resume", "stop"):
        action = "idle_ignore"
    payload: Dict[str, Any] = {"command": action, "err": str(params.get("err", ""))}
    if action == "idle_ignore":
        payload["type"] = int(params.get("type", 1))
    else:
        payload["param"] = "reserve"
        payload["job_id"] = str(params.get("job_id", "0"))
    return {"print": payload}


def build_print_error(params: Dict[str, Any]) -> Dict[str, Any]:
    """Auto-recovery / acknowledge a print error state."""
    return {
        "print": {
            "command": "print_error",
            "err_code": int(params.get("err_code", 0)),
            "sub_err_code": int(params.get("sub_err_code", 0)),
        }
    }


def build_set_filament(params: Dict[str, Any]) -> Dict[str, Any]:
    """Push a filament profile to a tray (target 0 = external spool)."""
    payload: Dict[str, Any] = {"command": "set_filament"}
    if params.get("filament_id"):
        payload["filament_id"] = params["filament_id"]
    if params.get("target") is not None:
        payload["target"] = int(params["target"])
    if params.get("nozzle_temp") is not None:
        payload["nozzle_temp"] = int(params["nozzle_temp"])
    if params.get("bed_type"):
        payload["bed_type"] = params["bed_type"]
    payload["bed_level_detect"] = bool(params.get("bed_level_detect", True))
    return {"print": payload}


def build_ams_change_filament(params: Dict[str, Any]) -> Dict[str, Any]:
    """Feed (target 1) or unload (target 2) filament on an AMS tray."""
    return {
        "print": {
            "command": "ams_change_filament",
            "target": int(params.get("action", 1)),
            "curr_temp": int(params.get("nozzle_temp", 220)),
            "tar_temp": int(params.get("nozzle_temp", 220)),
        }
    }


def build_ams_user_setting(params: Dict[str, Any]) -> Dict[str, Any]:
    """Select an AMS tray for the next print / auto-select source."""
    return {
        "print": {
            "command": "ams_user_setting",
            "ams_id": int(params.get("ams_id", 0)),
            "select_tray": bool(params.get("select_tray", True)),
            "tray_id": int(params.get("tray_id", 0)),
        }
    }


def build_ams_filament_setting(params: Dict[str, Any]) -> Dict[str, Any]:
    """Write a filament record into an AMS tray.

    Field names/shape mirror OrcaSlicer's ``command_ams_filament_settings``: the
    values sit **directly** on the ``print`` object (not nested in ``tray_info``),
    the colour is ``RRGGBBAA`` without a leading ``#``, and ``tray_info_idx`` is a
    Bambu filament id. Getting any of that wrong makes the printer show "?".
    """
    tray_type = str(params.get("tray_type", "PLA"))
    ams_id = int(params.get("ams_id", 0))
    slot_id = int(params.get("tray_id", 0))
    # the external spool is addressed as ams_id 254/255 with tray_id 254
    tray_id = 254 if ams_id in (254, 255) else slot_id

    colour = str(params.get("color", "#00AE42")).lstrip("#").upper()
    if len(colour) == 6:
        colour += "FF"
    elif len(colour) != 8:
        colour = "00AE42FF"

    filament_id = str(
        params.get("setting_id")
        or FILAMENT_IDS.get(tray_type)
        or FILAMENT_BY_TYPE.get(tray_type, "")
    )

    return {
        "print": {
            "command": "ams_filament_setting",
            "ams_id": ams_id,
            "slot_id": slot_id,
            "tray_id": tray_id,
            "tray_info_idx": filament_id,
            "setting_id": filament_id,
            "tray_color": colour,
            "nozzle_temp_min": int(params.get("nozzle_temp_min", 190)),
            "nozzle_temp_max": int(params.get("nozzle_temp_max", 240)),
            "tray_type": tray_type,
        }
    }


#: Bambu system filament id keyed by catalogue key, e.g. ``{"PLA Basic": "GFA00"}``.
#: Values come from genuine Bambu spool RFID tags (see :mod:`backend.filaments`).
FILAMENT_IDS: Dict[str, str] = {str(f["key"]): str(f["id"]) for f in FILAMENTS}

#: Fallback id for a bare Bambu ``tray_type`` (``PLA``, ``PETG``, ``PLA-CF`` ...):
#: the first catalogue entry that uses it.
FILAMENT_BY_TYPE: Dict[str, str] = {}
for _filament in FILAMENTS:
    FILAMENT_BY_TYPE.setdefault(str(_filament["type"]), str(_filament["id"]))


def build_flow_calibration(params: Dict[str, Any]) -> Dict[str, Any]:
    """Flow (extrusion) calibration: mode 0 disable, 1 normal, 2 first layer."""
    return {
        "print": {
            "command": "flow_calibration",
            "mode": int(params.get("mode", 1)),
            "sub_option": params.get("state", "enable"),
        }
    }


# --------------------------------------------------------------------------
# system channel
# --------------------------------------------------------------------------


def build_ledctrl(params: Dict[str, Any]) -> Dict[str, Any]:
    """One ``ledctrl`` payload.

    Firmware rejects a payload without the timing block (``json wrong
    format``), and the defaults below are the ones OrcaSlicer uses.
    """
    mode = params.get("mode", "on")
    return {
        "system": {
            "command": "ledctrl",
            "led_node": params.get("node", "chamber_light"),
            "led_mode": mode,
            "led_on_time": int(params.get("on_time", 500)),
            "led_off_time": int(params.get("off_time", 500)),
            "loop_times": int(params.get("times", 1)),
            "interval_time": int(params.get("interval", 1000)),
        }
    }


#: The X1C exposes ``chamber_light`` and ``work_light``. Some models also have
#: a second chamber channel; sending an unknown node only yields a failure ack,
#: so the caller picks the node explicitly (``node="all"`` fans out).
LIGHT_NODES = ("chamber_light", "work_light")


def build_light(params: Dict[str, Any]) -> Dict[str, Any]:
    """Lighting payload for the requested node (default chamber light)."""
    return build_ledctrl(params)


def build_restart_module(params: Dict[str, Any]) -> Dict[str, Any]:
    """Reboot the printer (esp32) or just reload its settings."""
    return {"system": {"command": "restart_module", "module": params.get("module", "esp32")}}


# --------------------------------------------------------------------------
# info / camera channels
# --------------------------------------------------------------------------


def build_info(params: Dict[str, Any]) -> Dict[str, Any]:
    return {"info": {"command": params.get("command", "get_version")}}


def build_camera(params: Dict[str, Any]) -> Dict[str, Any]:
    """Enable/disable the IP camera. Firmware accepts either verb; both are sent."""
    action = params.get("action", "enable")
    verb = "stop" if action == "disable" else "push"
    return {"camera": {"command": verb}}


def build_push_state(params: Dict[str, Any]) -> Dict[str, Any]:
    """Ask the printer to dump its full state/telemetry over the report topic."""
    return {"pushing": {"command": "start"}}


# --------------------------------------------------------------------------
# manual temperature setpoints (OrcaSlicer-compatible payloads)
# --------------------------------------------------------------------------


def build_set_nozzle_temp(params: Dict[str, Any]) -> Dict[str, Any]:
    """Nozzle setpoint via the same G-code line OrcaSlicer sends."""
    temp = int(params.get("temp", 0))
    return {
        "print": {
            "command": "gcode_line",
            "param": f"M104 S{temp}\n",
        }
    }


def build_set_bed_temp(params: Dict[str, Any]) -> Dict[str, Any]:
    """Bed setpoint.

    The X1 firmware answers ``set_bed_temp`` with ``FAIL / ERROR STATE``; the same
    G-code OrcaSlicer falls back to (``M140``) is accepted and moves
    ``bed_target_temper``. Verified on an X1C.
    """
    temp = int(params.get("temp", 0))
    return {"print": {"command": "gcode_line", "param": f"M140 S{temp}\n"}}


def build_set_chamber_temp(params: Dict[str, Any]) -> Dict[str, Any]:
    """Active-chamber heat soak setpoint."""
    temp = int(params.get("temp", 0))
    return {"print": {"command": "set_ctt", "ctt_val": temp}}


# --------------------------------------------------------------------------
# motion: homing, jogging (OrcaSlicer/G-code)
# --------------------------------------------------------------------------


def _gcode(param: str) -> Dict[str, Any]:
    return {"print": {"command": "gcode_line", "param": param}}


def build_home(params: Dict[str, Any]) -> Dict[str, Any]:
    """Home all axes, or the one named in ``axis`` (X/Y/Z)."""
    axis = str(params.get("axis", "") or "").upper()
    if axis and axis not in ("X", "Y", "Z"):
        axis = ""
    return _gcode(f"G28 {axis}".strip() + "\n")


def build_jog(params: Dict[str, Any]) -> Dict[str, Any]:
    """Relative move of one axis, expressed as G91/G1/G90."""
    axis = str(params.get("axis", "X")).upper()
    if axis not in ("X", "Y", "Z"):
        raise ValueError(f"unknown axis '{params.get('axis')}'")
    distance = float(params.get("distance", 10))
    feed = int(params.get("feedrate", 3000))
    # G91 -> relative positioning, G90 -> absolute again.
    return _gcode(f"G91\nG1 {axis}{distance:g} F{feed}\nG90\n")


def build_extrude(params: Dict[str, Any]) -> Dict[str, Any]:
    """Relative extruder move (positive extrudes, negative retracts)."""
    amount = float(params.get("amount", 5))
    feed = int(params.get("feedrate", 300))
    return _gcode(f"G91\nG1 E{amount:g} F{feed}\nG90\n")


# --------------------------------------------------------------------------
# fans (M106)
# --------------------------------------------------------------------------

#: Fan name -> M106 ``P`` index, for the X1 family this app targets. The
#: indices follow the firmware's fan order (part cooling first, then the aux
#: part fan, then the chamber fan). This is per-model: a P1/A1 series machine
#: numbers them differently, so the UI shows each fan's reported speed next to
#: its control - if moving one changes the wrong readout, this table is wrong
#: for that firmware.
FAN_INDICES: Dict[str, int] = {
    "part": 0,
    "aux": 1,
    "chamber": 3,
}

FANS = tuple(FAN_INDICES)


def build_set_fan(params: Dict[str, Any]) -> Dict[str, Any]:
    """Set one fan's speed with an M106 G-code line.

    ``speed`` is a percentage (0-100) so the UI can share the temperature
    slider's range; M106 itself takes 0-255.
    """
    fan = str(params.get("fan", "part")).lower()
    if fan not in FAN_INDICES:
        raise ValueError(f"unknown fan '{params.get('fan')}'")
    percent = max(0, min(100, int(params.get("speed", 0))))
    value = round(percent * 255 / 100)
    return _gcode(f"M106 P{FAN_INDICES[fan]} S{value}\n")


# --------------------------------------------------------------------------
# lookup tables
# --------------------------------------------------------------------------

SPEED_PROFILES: Dict[int, str] = {
    1: "silent",
    2: "standard",
    3: "sport",
    4: "ludicrous",
}

#: Reported speed magnitude per level (percent), from the printer's telemetry.
SPEED_PERCENT: Dict[int, int] = {1: 50, 2: 100, 3: 124, 4: 166}

#: name -> (group, builder). The REST layer maps these onto documented endpoints
#: and ``/api/printers/{id}/command`` accepts the same keys.
COMMANDS: Dict[str, tuple] = {
    # print control
    "pause": ("print", _print_cmd("pause")),
    "resume": ("print", _print_cmd("resume")),
    "stop": ("print", _print_cmd("stop")),
    "speed": ("print", build_print_speed),
    "print_option": ("print", build_print_option),
    "skip_objects": ("print", build_skip_objects),
    "print_error": ("print", build_print_error),
    "clear_error": ("print", build_clear_error),
    "hms_ignore": ("print", build_hms_ignore),
    "set_filament": ("print", build_set_filament),
    # calibration - all of these MOVE the printer, see MOTION_COMMANDS
    "bed_leveling": ("print", _print_cmd("bed_leveling")),
    "vibration_calibration": ("print", _print_cmd("vibration_calibration")),
    "flow_calibration": ("print", build_flow_calibration),
    # motion / g-code
    "home": ("print", build_home),
    "jog": ("print", build_jog),
    "extrude": ("print", build_extrude),
    # fans - moves nothing and heats nothing, so this stays a `safe` command
    "set_fan": ("print", build_set_fan),
    # ams
    "ams_feed": ("print", _print_cmd("ams_change_filament", target=1)),
    "ams_unload": ("print", _print_cmd("ams_change_filament", target=2)),
    "ams_resume": ("print", _print_cmd("ams_resume_filament")),
    "ams_tray_select": ("print", build_ams_user_setting),
    "ams_tray_info": ("print", _print_cmd("tray_info")),
    "ams_current_tray": ("print", _print_cmd("get_current_tray")),
    "ams_filament_setting": ("print", build_ams_filament_setting),
    "ams_auto": ("print", _print_cmd("ams_auto_feed")),
    # system
    "light": ("system", build_light),
    "restart_module": ("system", build_restart_module),
    # manual temperature setpoints
    "set_nozzle_temp": ("print", build_set_nozzle_temp),
    "set_bed_temp": ("print", build_set_bed_temp),
    "set_chamber_temp": ("print", build_set_chamber_temp),
    "system_version": ("system", _print_cmd("get_version")),
    # info
    "push_state": ("pushing", build_push_state),
    "printer_info": ("info", build_info),
    # camera
    "camera": ("camera", build_camera),
}


def command_groups() -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {}
    for name, (group, _) in COMMANDS.items():
        groups.setdefault(group, []).append(name)
    return groups


#: Commands that drive the gantry, bed or extruder. They are refused unless the
#: caller explicitly confirms and the printer reports all axes homed - axis
#: self-tests and similar routines slam the axes at full speed on this class of
#: machine and are deliberately not implemented at all.
MOTION_COMMANDS = {
    "bed_leveling",
    "flow_calibration",
    "vibration_calibration",
    "jog",
}

#: Homing is the pre-requisite for everything else, so it is allowed while
#: unhomed (that is the point) but still needs confirmation and an idle printer.
HOME_COMMANDS = {"home"}

#: Commands that heat the nozzle/bed or move filament through the extruder.
THERMAL_COMMANDS = {
    "ams_feed",
    "ams_unload",
    "ams_resume",
    "ams_auto",
    "set_filament",
    "set_nozzle_temp",
    "set_bed_temp",
    "set_chamber_temp",
    "extrude",
}

#: Hard safety caps for manual setpoints (degrees Celsius).
TEMP_LIMITS = {"nozzle": (0, 300), "bed": (0, 130), "chamber": (0, 60)}

#: Commands that can change what the printer is doing right now.
JOB_COMMANDS = {"pause", "resume", "stop", "skip_objects", "print_error", "clear_error", "hms_ignore", "restart_module"}


def risk_class(name: str) -> str:
    """``safe`` | ``motion`` | ``home`` | ``thermal`` | ``job`` for a command."""
    if name in MOTION_COMMANDS:
        return "motion"
    if name in HOME_COMMANDS:
        return "home"
    if name in THERMAL_COMMANDS:
        return "thermal"
    if name in JOB_COMMANDS:
        return "job"
    return "safe"


def build_command(name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the first MQTT payload for ``name``; raises ``KeyError`` if unknown."""
    return build_payloads(name, params)[0]


def build_payloads(name: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Every MQTT payload a command needs (a single one for most commands).

    Lighting fans out to both chamber channels, so a command can produce more
    than one publish.
    """
    if name not in COMMANDS:
        raise KeyError(name)
    _, builder = COMMANDS[name]
    built = builder(dict(params or {}))
    return built if isinstance(built, list) else [built]


def available_commands() -> List[str]:
    return sorted(COMMANDS)
