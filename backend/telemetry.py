"""Telemetry payload parsing helpers and their regression tests target.

`TelemetryReport` types the fields the dashboard and the safety gates actually
depend on. It is *advisory*: a payload that fails validation is logged and still
delivered raw, because dropping a tick is far worse than showing an untyped
field. Unknown keys are kept (``extra="allow"``) - Bambu adds fields constantly.
"""

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)

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


class TelemetryReport(BaseModel):
    """The stable subset of a ``print`` report this project relies on.

    Bambu adds and reshapes fields between firmware versions, so extras are kept
    verbatim and every field is optional: a missing key must never invalidate a
    tick. The point is to turn "the UI silently stopped updating" into a log line
    naming the field that changed.
    """

    model_config = ConfigDict(extra="allow")

    gcode_state: Optional[str] = None
    subtask_name: Optional[str] = None
    subtask_id: Optional[str] = None
    print_type: Optional[str] = None
    stage: Optional[str] = None

    layer_num: Optional[int] = None
    total_layer_num: Optional[int] = None
    mc_percent: Optional[float] = None
    mc_remaining_stage: Optional[str] = None
    mc_remaining_time: Optional[float] = None

    nozzle_temper: Optional[float] = None
    nozzle_target_temper: Optional[float] = None
    bed_temper: Optional[float] = None
    bed_target_temper: Optional[float] = None
    chamber_temper: Optional[float] = None

    home_flag: Optional[int] = None
    print_error: Optional[int] = None
    hms: List[Dict[str, Any]] = []
    s_obj: List[Any] = []
    command: Optional[str] = None


#: Fields the dashboard and the motion/thermal safety gates read. If a payload
#: carries none of them, the shape has almost certainly changed.
CORE_FIELDS = (
    "gcode_state",
    "subtask_name",
    "layer_num",
    "mc_percent",
    "nozzle_temper",
    "bed_temper",
    "hms",
    "print_error",
)


def parse_report(payload: Dict[str, Any]) -> Optional[TelemetryReport]:
    """Validate one telemetry tick, logging firmware drift.

    Never raises and never discards the payload: on failure the raw dict has
    still been stored and broadcast, this only makes the anomaly visible.
    """
    if not isinstance(payload, dict):
        logger.warning("Telemetry payload was %s, expected a dict", type(payload).__name__)
        return None
    try:
        return TelemetryReport(**payload)
    except ValidationError as exc:
        logger.warning("Telemetry field types changed (%s); delivering the tick untyped", exc)
        return None
    except (TypeError, ValueError) as exc:
        logger.warning("Could not type telemetry payload (%s); delivering it untyped", exc)
        return None


def missing_core_fields(payload: Dict[str, Any]) -> List[str]:
    """Which of the fields this project depends on are absent."""
    if not isinstance(payload, dict):
        return list(CORE_FIELDS)
    return [name for name in CORE_FIELDS if name not in payload]
