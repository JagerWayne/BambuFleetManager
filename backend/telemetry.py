"""Telemetry payload parsing helpers and their regression tests target."""

from typing import Any, Dict

ACTIVE_STATES = ("running", "idle", "pause", "paused", "prepare", "heating", "finish", "failed", "slicing")

STATE_ALIASES: Dict[str, str] = {
    "running": "running",
    "idle": "idle",
    "pause": "paused",
    "paused": "paused",
    "prepare": "preparing",
    "heating": "preparing",
    "finish": "finish",
    "failed": "failed",
    "slicing": "slicing",
}


def normalize_gcode_state(state: Any) -> str:
    """Map a raw ``gcode_state`` value onto the small set the UI understands."""
    if not isinstance(state, str):
        return "unknown"
    return STATE_ALIASES.get(state.strip().lower(), state.strip().lower() or "unknown")


def parse_print_report(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a raw ``print`` report block into the fields used by the dashboard."""
    if not isinstance(payload, dict):
        return {}

    report: Dict[str, Any] = {"status": normalize_gcode_state(payload.get("gcode_state"))}

    percent = payload.get("mc_percent")
    if isinstance(percent, (int, float)):
        report["progress"] = max(0, min(100, int(percent)))

    remaining = payload.get("mc_remaining_time")
    if isinstance(remaining, (int, float)):
        report["remaining_sec"] = max(0, int(remaining * 60))

    for source, target in (
        ("nozzle_temper", "nozzle_temp"),
        ("nozzle_target_temper", "nozzle_target"),
        ("bed_temper", "bed_temp"),
        ("bed_target_temper", "bed_target"),
    ):
        value = payload.get(source)
        if isinstance(value, (int, float)):
            report[target] = round(float(value))

    if payload.get("subtask_name"):
        report["job"] = str(payload["subtask_name"])

    return report
