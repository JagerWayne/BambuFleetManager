"""Thread-safe per-printer state shared across threads.

paho runs its network callbacks on its own threads while FastAPI handlers run on
the asyncio loop, and both touch the same three dicts. Every compound
read-modify-write therefore goes through :func:`locked`, so a telemetry tick can
never observe (or publish) a half-updated snapshot - e.g. a report stored
without its homing flag.

The lock is re-entrant because some helpers (set_homed -> save_state) call each
other while already holding it.

The dicts are module-level on purpose: `backend.main` re-exports them, so tests
and existing call sites can seed or clear state directly.
"""

import threading
from contextlib import contextmanager
from typing import Dict, Iterator, Optional, Tuple

#: Latest full ``print`` report per printer, so a freshly opened dashboard (or
#: a reconnecting WebSocket) can render real state instead of zeroed placeholders.
latest_reports: Dict[str, dict] = {}

#: Tracked homing state per printer. ``home_flag`` in the report is unreliable on
#: X1 firmware (it reads "not homed" even right after G28), so homing is tracked
#: from commands we issue, and cleared when a job starts. Persisted to disk so it
#: survives a restart.
homed_state: Dict[str, bool] = {}

#: Acknowledgements the printer sent, keyed by printer then command, so the API
#: can explain *why* something was refused instead of guessing.
last_acks: Dict[str, Dict[str, dict]] = {}

_lock = threading.RLock()


@contextmanager
def locked() -> Iterator[None]:
    """Hold the shared-state lock. Re-entrant, and always released."""
    _lock.acquire()
    try:
        yield
    finally:
        _lock.release()


def record_report(printer_id: str, data: dict) -> None:
    with locked():
        latest_reports[printer_id] = data


def drop_report(printer_id: str) -> None:
    with locked():
        latest_reports.pop(printer_id, None)


def get_report(printer_id: str) -> Optional[dict]:
    with locked():
        return latest_reports.get(printer_id)


def record_homed(printer_id: str, value: Optional[bool]) -> None:
    with locked():
        homed_state[printer_id] = value


def get_homed(printer_id: str) -> Optional[bool]:
    with locked():
        return homed_state.get(printer_id)


def homed_map() -> Dict[str, bool]:
    with locked():
        return dict(homed_state)


def record_ack(printer_id: str, command: str, entry: dict) -> None:
    with locked():
        last_acks.setdefault(printer_id, {})[command] = entry


def get_ack(printer_id: str, command: str) -> Optional[dict]:
    with locked():
        return (last_acks.get(printer_id) or {}).get(command)


def report_and_homed(printer_id: str) -> Tuple[Optional[dict], Optional[bool]]:
    """The last report and homing flag read as one consistent pair."""
    with locked():
        return latest_reports.get(printer_id), homed_state.get(printer_id)


def reports_with_homed() -> Dict[str, dict]:
    """Every stored report annotated with its homing flag (WebSocket hello)."""
    with locked():
        return {k: {**v, "homed": homed_state.get(k)} for k, v in latest_reports.items()}


def reset() -> None:
    """Clear everything (tests)."""
    with locked():
        latest_reports.clear()
        homed_state.clear()
        last_acks.clear()
