"""Bambu Fleet Manager - FastAPI application, REST endpoints and WebSocket hub."""

import asyncio
import json
import logging
import os
import re
import secrets
import time
import xml.etree.ElementTree as ET
import zipfile
from contextlib import asynccontextmanager
from typing import Callable, Dict, List, Optional

import aiofiles
from pydantic import ValidationError
from fastapi import (  # noqa: F401  (re-exported: routers use core.<name>)
    Body,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (  # noqa: F401  (re-exported: routers use core.<name>)
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from backend import camera as camera_mod
from backend import commands as cmd_mod
from backend import state
from backend import telemetry
from backend import updater  # noqa: F401  (routers use core.updater)
from backend.filaments import FILAMENT_CATEGORIES, FILAMENTS  # noqa: F401  (re-exported: routers use core.<name>)
from backend.paths import DATA_DIR, RESOURCE_DIR, ensure_data_dirs
from backend.ftp_client import (  # noqa: F401  (re-exported: routers use core.<name>)
    delete_path,
    rename_path,
    detect_plate_indices,
    read_remote_zip_entries,
    download_stream,
    list_directory,
    normalize_remote_path,
    remote_file_exists,
    upload_3mf_file,
)

from backend.models import (  # noqa: F401  (re-exported: routers use core.<Model>)
    AmsActionCommand,
    CalibrationCommand,
    CameraStateCommand,
    DeleteEntryCommand,
    DirectoryListing,
    DispatchResponse,
    ExtrudeCommand,
    FanCommand,
    FilamentSettingCommand,
    FileNode,
    GenericCommand,
    HomeCommand,
    JogCommand,
    LightCommand,
    LightModeCommand,
    PrintDispatchCommand,
    PrintOptionCommand,
    PrintRemoteCommand,
    PrinterConfig,
    PrinterPublic,
    RebootCommand,
    RemotePath,
    RenameEntryCommand,
    SendStagedCommand,
    ServerSettings,
    SkipObjectCommand,
    SpeedCommand,
    SpeedProfileCommand,
    StagedFile,
    TemperatureCommand,
    UploadEntry,
)
from backend.mqtt_manager import FleetMqttManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bfm")

BASE_DIR = DATA_DIR  # kept for backwards-compatible imports
CONFIG_PATH = os.path.join(DATA_DIR, "config", "printers.json")
SETTINGS_PATH = os.path.join(DATA_DIR, "config", "settings.json")
STATE_PATH = os.path.join(DATA_DIR, "config", "state.json")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
ensure_data_dirs()

ALLOWED_SUFFIXES = (".3mf", ".gcode")
DEFAULT_SETTINGS = {"host": "0.0.0.0", "port": 8000, "allow_motion": False, "auth_token": ""}
#: Uploads larger than this are refused (streamed to disk, never held in memory).
MAX_UPLOAD_BYTES = 300 * 1024 * 1024
#: Staged files older than this are pruned on startup.
STAGED_MAX_AGE = 14 * 24 * 3600
#: Cookie that carries the dashboard auth token.
AUTH_COOKIE = "bfm_token"
#: Content-Security-Policy. Script/style stay 'unsafe-inline' because the theme
#: bootstrap in index.html is an inline <script> and app.js writes inline styles;
#: everything that could pull remote code (connect/img/font/object/base/frame)
#: is locked to this origin.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self' ws: wss:; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)
#: Pause between the automatic home and a calibration routine (override in tests).
CALIBRATION_HOME_DELAY = 2.0
#: How long to wait for a print to actually start before reporting failure.
PRINT_START_VERIFY_DELAY = 4.0
#: How long to wait for a just-started job to reach RUNNING before injecting a
#: pre-armed object skip (override in tests).
PRINT_SKIP_INJECT_TIMEOUT = 15.0
#: Poll interval for that wait.
PRINT_SKIP_POLL_INTERVAL = 0.5


class ConnectionHub:
    """Fan-out of telemetry payloads to every connected dashboard socket."""

    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict) -> None:
        dead: List[WebSocket] = []
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection)


ws_hub = ConnectionHub()
mqtt_manager: Optional[FleetMqttManager] = None

# Per-printer mutable state now lives in backend/state.py, which owns the dicts
# and guards them with a re-entrant lock (MQTT callbacks run on paho threads,
# the API on the asyncio loop). Re-exported here so existing call sites and
# tests keep working against the very same objects.
latest_reports = state.latest_reports
homed_state = state.homed_state
last_acks = state.last_acks

#: Callbacks run right before the process exits, so a self-update can quit the
#: server (and, when running under the tray, the tray) before the installer
#: replaces the files on disk.
_shutdown_hooks: List[Callable[[], None]] = []


def register_shutdown_hook(hook: Callable[[], None]) -> None:
    _shutdown_hooks.append(hook)


def run_shutdown_hooks() -> None:
    for hook in list(_shutdown_hooks):
        try:
            hook()
        except Exception as exc:  # a broken hook must not block shutdown
            logger.warning("Shutdown hook failed: %s", exc)


def load_state() -> None:
    if not os.path.exists(STATE_PATH):
        return
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        with state.locked():
            homed_state.update({k: bool(v) for k, v in (data.get("homed") or {}).items()})
    except Exception as exc:
        logger.warning("Could not read %s: %s", STATE_PATH, exc)


def save_state() -> None:
    try:
        with state.locked():
            homed_snapshot = dict(homed_state)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"homed": homed_snapshot}, f, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as exc:
        logger.warning("Could not write %s: %s", STATE_PATH, exc)


def set_homed(printer_id: str, value: Optional[bool]) -> None:
    state.record_homed(printer_id, value)
    save_state()


def format_hms(entry: dict) -> str:
    """Render an HMS entry the way the printer displays it: HMS_xxxx_xxxx_xxxx_xxxx."""
    attr = int(entry.get("attr", 0) or 0)
    code = int(entry.get("code", 0) or 0)
    return "HMS_{:04X}_{:04X}_{:04X}_{:04X}".format(
        (attr >> 16) & 0xFFFF, attr & 0xFFFF, (code >> 16) & 0xFFFF, code & 0xFFFF
    )


def require_manager() -> FleetMqttManager:
    if mqtt_manager is None:
        raise HTTPException(status_code=503, detail="MQTT engine is still starting up")
    return mqtt_manager


def track_ack(printer_id: str, report: dict) -> None:
    command = report.get("command")
    if isinstance(command, str) and ("result" in report or "reason" in report):
        state.record_ack(printer_id, command, {
            "command": command,
            "result": report.get("result"),
            "reason": report.get("reason"),
            "err_code": report.get("err_code"),
        })


def track_homing(printer_id: str, report: dict) -> None:
    """Update the in-memory homing state (persisted only on explicit events)."""
    gcode_state = str(report.get("gcode_state", "")).upper()
    if gcode_state in ("RUNNING", "PREPARE", "SLICING"):
        state.record_homed(printer_id, False)
        return
    flag = report.get("home_flag")
    # Only trust the flag when it clearly says "all axes homed".
    if isinstance(flag, int) and (flag & 0b111) == 0:
        state.record_homed(printer_id, True)


async def telemetry_handler(printer_id: str, data: dict) -> None:
    """Store one telemetry tick and fan it out.

    Report, homing and ack are written under one lock acquisition so a
    dashboard can never receive a payload whose ``homed`` flag belongs to a
    different tick than its ``data``.

    The typed parse is advisory: it logs firmware drift (a field renamed or
    retyped) instead of letting the dashboard quietly go stale.
    """
    telemetry.parse_report(data)
    with state.locked():
        state.record_report(printer_id, data)
        track_homing(printer_id, data)
        track_ack(printer_id, data)
        homed = state.get_homed(printer_id)
    await ws_hub.broadcast({
        "event": "telemetry",
        "printer_id": printer_id,
        "data": data,
        "homed": homed,
    })


def read_printers() -> Dict[str, PrinterConfig]:
    """Load the fleet, validating each node independently.

    A single malformed entry must not take the whole fleet offline: the bad
    node is logged and skipped, every valid one still comes up.
    """
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.error("Could not parse %s: %s", CONFIG_PATH, exc)
        return {}
    if not isinstance(data, dict):
        logger.error("%s must contain a JSON object; ignoring it", CONFIG_PATH)
        return {}
    printers: Dict[str, PrinterConfig] = {}
    for pid, raw in data.items():
        try:
            printers[pid] = PrinterConfig(**raw)
        except ValidationError as exc:
            logger.error("Ignoring invalid printer %r in %s: %s", pid, CONFIG_PATH, exc)
        except (TypeError, AttributeError) as exc:
            logger.error("Ignoring malformed printer %r in %s: %s", pid, CONFIG_PATH, exc)
    return printers


def save_printers(printers: Dict[str, PrinterConfig]) -> None:
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({k: v.model_dump() for k, v in printers.items()}, f, indent=2)
    os.replace(tmp_path, CONFIG_PATH)


def read_settings() -> Dict[str, object]:
    """Load server settings, falling back to defaults when the file is bad.

    Known keys are validated through ``ServerSettings`` so a hand-edited port
    or flag type can never boot the server into a broken state.
    """
    if not os.path.exists(SETTINGS_PATH):
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            stored = json.load(f)
    except Exception as exc:
        logger.error("Could not parse %s: %s", SETTINGS_PATH, exc)
        return dict(DEFAULT_SETTINGS)
    if not isinstance(stored, dict):
        logger.error("%s must contain a JSON object; ignoring it", SETTINGS_PATH)
        return dict(DEFAULT_SETTINGS)
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: stored[k] for k in DEFAULT_SETTINGS if k in stored})
    try:
        known = ServerSettings(**{k: merged[k] for k in DEFAULT_SETTINGS})
    except ValidationError as exc:
        logger.error("Ignoring invalid %s, using defaults: %s", SETTINGS_PATH, exc)
        return dict(DEFAULT_SETTINGS)
    # clear_auth is write-only; never let it leak into the saved file
    merged.update(known.model_dump(exclude={"clear_auth"}))
    return merged


def save_settings(settings: Dict[str, object]) -> None:
    tmp_path = SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp_path, SETTINGS_PATH)


def get_printer(printer_id: str) -> PrinterConfig:
    printer = read_printers().get(printer_id)
    if not printer:
        raise HTTPException(status_code=404, detail="Printer node not found")
    return printer


def is_homed(printer_id: str) -> Optional[bool]:
    """Tracked homing state: True/False once known, None if never established."""
    return state.get_homed(printer_id)


def guard_command(
    printer_id: str, command: str, confirm: bool = False, assume_homed: bool = False
) -> None:
    """Block machine-moving commands unless the printer is ready for them."""
    risk = cmd_mod.risk_class(command)
    if risk == "safe":
        return

    # A global "motion unlocked" setting skips the per-action confirmation, but
    # never the homing or idle-printer requirements.
    if read_settings().get("allow_motion"):
        confirm = True

    report = state.get_report(printer_id) or {}
    gcode_state = str(report.get("gcode_state", "")).upper()
    active = gcode_state not in ("", "IDLE", "FINISH", "FAILED")

    if risk in ("motion", "home") and active:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Printer is {gcode_state.lower()} - the head and bed must be still before this "
                "command. Pause or stop the job first."
            ),
        )

    if risk == "home":
        if not confirm:
            raise HTTPException(
                status_code=409,
                detail="Homing moves all axes. Re-send with confirm_motion=true once the plate is clear.",
            )
        logger.warning("HOME command sent to %s", printer_id)
        return

    if risk == "motion":
        if not confirm:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{command}' moves the printer. Re-send with confirm_motion=true "
                    "only once you have cleared the build plate."
                ),
            )
        if not assume_homed and is_homed(printer_id) is not True:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Refusing to move the printer: it is not known to be homed. "
                    "Press Home first (Jog tab)."
                ),
            )
        logger.warning("MOTION command %s sent to %s (confirmed)", command, printer_id)

    if risk == "thermal" and not confirm:
        raise HTTPException(
            status_code=409,
            detail=f"'{command}' heats the hotend and moves filament. Re-send with confirm_thermal=true."
        )


async def dispatch(
    printer_id: str,
    command: str,
    params: Optional[dict] = None,
    confirm: bool = False,
    assume_homed: bool = False,
) -> Dict[str, object]:
    """Validate, build and publish one registry command, returning what was sent."""
    try:
        payloads = cmd_mod.build_payloads(command, params)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unknown command '{command}'")
    guard_command(printer_id, command, confirm, assume_homed)
    # The publisher waits briefly for the link, so it must not block the loop.
    delivered = await run_blocking(
        require_manager().send_payloads, printer_id, payloads
    )
    if not delivered:
        raise HTTPException(
            status_code=503,
            detail=(
                "Printer node is offline over MQTTS - the command was not delivered. "
                "Check the node's power and network, then retry."
            ),
        )
    logger.info("-> %s : %s (%s, %d payload(s))",
                printer_id, command, cmd_mod.risk_class(command), len(payloads))
    return {
        "status": "sent",
        "command": command,
        "risk": cmd_mod.risk_class(command),
        "payload": payloads[0],
        "payload_count": len(payloads),
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    global mqtt_manager
    load_state()
    pruned = prune_uploads()
    if pruned:
        logger.info("Pruned %d stale staged file(s)", pruned)
    loop = asyncio.get_running_loop()
    mqtt_manager = FleetMqttManager(loop, telemetry_handler)
    printers = read_printers()
    for p in printers.values():
        mqtt_manager.register_printer(p.id, p.ip, p.sn, p.access_code)
    logger.info("Bambu Fleet Manager started with %d printer node(s)", len(printers))
    try:
        yield
    finally:
        # never leave ffmpeg transcodes behind, even on a hard shutdown
        stopped = camera_mod.stop_all_mjpeg_streams()
        if stopped:
            logger.info("Stopped %d live camera stream(s)", stopped)
        if mqtt_manager:
            mqtt_manager.shutdown()


app = FastAPI(title="Bambu Fleet Manager Local Server", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(RESOURCE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(RESOURCE_DIR, "templates"))


@app.middleware("http")
async def no_stale_assets(request: Request, call_next):
    """Force the browser to revalidate the dashboard and its assets.

    Without this a stale cached app.js keeps driving a freshly restarted
    server, which looks exactly like "the buttons do nothing".
    """
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# ------------------------------------------------------------------- auth


#: Paths reachable without a token (the login page and static assets).
OPEN_PATHS = {"/", "/login", "/VERSION", "/favicon.ico", "/api/login"}


def auth_token() -> str:
    return str(read_settings().get("auth_token") or "")


def request_token(request: Request) -> str:
    return request.cookies.get(AUTH_COOKIE) or request.headers.get("X-Auth-Token") or ""


LOGIN_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bambu Fleet Manager - sign in</title><link rel="stylesheet" href="/static/css/tailwind.min.css">
<script>(function(){try{var t=localStorage.getItem('bfm-theme')||'dark';
document.documentElement.setAttribute('data-theme',t);}catch(e){}})();</script>
</head><body class="min-h-screen t-body font-sans grid place-items-center p-6">
<form id="f" class="card w-full max-w-sm p-6 space-y-3">
  <h1 class="text-base font-bold t-strong">Bambu Fleet Manager</h1>
  <p class="font-mono text-[11px] t-mut">This server requires an access token.</p>
  <input id="token" type="password" class="field" placeholder="Access token" autofocus>
  <p id="err" class="font-mono text-[11px] t-danger hidden">Wrong token.</p>
  <button class="btn btn-primary w-full" type="submit">Sign in</button>
</form>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const r = await fetch('/api/login', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ token: document.getElementById('token').value })
  });
  if (r.ok) { location.href = '/'; } else { document.getElementById('err').classList.remove('hidden'); }
});
</script></body></html>"""


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Attach hardening headers to every response.

    Cheap defense in depth for a dashboard that is, by default, unauthenticated
    on the LAN: no remote script/style/image sources, no framing, no sniffing.
    """
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Guard the API and dashboard when a token is configured."""
    token = auth_token()
    if not token:
        return await call_next(request)

    path = request.url.path
    if path in OPEN_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if secrets.compare_digest(request_token(request), token):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    return RedirectResponse("/login")


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page():
    return HTMLResponse(LOGIN_PAGE)


async def run_blocking(fn, *args):
    """Run a blocking FTP call off the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


# --------------------------------------------------------------------- pages


def _app_version() -> str:
    version_file = os.path.join(RESOURCE_DIR, "VERSION")
    if os.path.exists(version_file):
        with open(version_file, "r", encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    return "0.0.0"


@app.get("/VERSION", include_in_schema=False, response_class=PlainTextResponse)
async def read_version():
    return PlainTextResponse(_app_version())


_STARTED_AT = time.time()


@app.get("/health")
async def health():
    """Liveness probe for the tray app, scripts and reverse proxies."""
    clients = mqtt_manager.status() if mqtt_manager else {}
    return {
        "status": "ok",
        "version": _app_version(),
        "uptime_seconds": round(time.time() - _STARTED_AT),
        "printers": len(read_printers()),
        "mqtt_connected": sum(1 for s in clients.values() if s.get("connected")),
    }


def _redact(text: str, secrets_to_hide: List[str]) -> str:
    """Strip printer access codes and RTSP credentials out of any text."""
    for secret in secrets_to_hide:
        if secret:
            text = text.replace(secret, "***")
    # rtsp://user:password@host -> rtsp://user:***@host
    return re.sub(r"(rtsps?://[^:/?#@\s]+:)[^@\s]+@", r"\1***@", text)


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})


# --------------------------------------------------------------- settings


UPDATE_DIR = os.path.join(DATA_DIR, "updates")


# -------------------------------------------------------------------- fleet


# ------------------------------------------------------------------ uploads


async def _save_upload(file: UploadFile, dest_path: str) -> int:
    """Stream an upload to disk, enforcing MAX_UPLOAD_BYTES. Never buffers it all."""
    total = 0
    try:
        async with aiofiles.open(dest_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit",
                    )
                await out.write(chunk)
    except HTTPException:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise
    return total


def prune_uploads() -> int:
    """Delete staged files older than STAGED_MAX_AGE. Returns how many went."""
    removed = 0
    cutoff = time.time() - STAGED_MAX_AGE
    for name in os.listdir(UPLOAD_DIR):
        path = os.path.join(UPLOAD_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


# ------------------------------------------------------------------ dispatch


def parse_slice_info(xml_text: str) -> List[dict]:
    """Parse ``Metadata/slice_info.config`` into per-plate metadata.

    Besides time/weight/filaments this keeps each plate's ``<object>`` list.
    Its ``identify_id`` is the id the ``print.skip_objects`` command expects
    (the printer ignores the ``plate_N.json`` bbox ids), and the elements are
    in the same slicer order as that plate's ``bbox_objects``.
    """
    plates = []
    try:
        root = ET.fromstring(xml_text)
    except Exception as exc:
        logger.debug("Could not parse slice_info.config: %s", exc)
        return []
    for plate in root.findall("plate"):
        meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
        filaments = []
        for f in plate.findall("filament"):
            filaments.append({
                "id": int(f.get("id") or 0),
                "type": f.get("type") or "",
                "color": (f.get("color") or "").lstrip("#").upper(),
                "used_g": float(f.get("used_g") or 0),
                "tray_info_idx": f.get("tray_info_idx") or "",
            })
        objects = []
        for o in plate.findall("object"):
            try:
                identify_id = int(o.get("identify_id") or 0)
            except (TypeError, ValueError):
                continue
            objects.append({
                "id": identify_id,
                "name": o.get("name") or "",
                "skipped": (o.get("skipped") or "").strip().lower() in ("true", "1"),
            })
        try:
            index = int(meta.get("index") or 0)
        except ValueError:
            index = 0
        plates.append({
            "index": index,
            "time_sec": int(float(meta.get("prediction") or 0)),
            "weight_g": float(meta.get("weight") or 0),
            "filaments": filaments,
            "objects": objects,
        })
    return sorted(plates, key=lambda p: p["index"])


#: Printable area of an X1-family build plate, in millimetres. Used as the
#: top-down diagram's bed when a sliced project does not say otherwise.
BED_SIZE_MM = 256


def parse_plate_geometry(json_text: str) -> dict:
    """Parse ``Metadata/plate_N.json`` into a top-down bed diagram.

    The printer's own skip screen draws each object at the plate position the
    slicer recorded, so the dashboard can show the same thing. ``bbox`` is
    ``[x0, y0, x1, y1]`` in millimetres and the objects keep the slicer order
    (which is also the order the printer reports its live object list in).
    """
    bed = [BED_SIZE_MM, BED_SIZE_MM]
    empty = {"bed": bed, "bbox_all": [], "objects": []}
    try:
        data = json.loads(json_text)
    except Exception as exc:
        logger.debug("Could not parse plate json: %s", exc)
        return empty
    if not isinstance(data, dict):
        return empty

    bbox_all = data.get("bbox_all")
    if not (isinstance(bbox_all, (list, tuple)) and len(bbox_all) == 4):
        bbox_all = []

    objects = []
    for obj in (data.get("bbox_objects") or []):
        if not isinstance(obj, dict):
            continue
        bbox = obj.get("bbox")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            continue
        try:
            objects.append({
                "id": int(obj.get("id") or 0),
                "name": str(obj.get("name") or ""),
                "bbox": [float(v) for v in bbox],
            })
        except (TypeError, ValueError):
            continue
    return {"bed": bed, "bbox_all": bbox_all, "objects": objects}


def merge_plate_object_ids(geometry: dict, slice_objects: List[dict]) -> List[dict]:
    """Correlate plate geometry objects with slice_info identify_ids.

    ``geometry["objects"]`` keeps the slicer order and the ``plate_N.json``
    bbox id; ``slice_objects`` is the matching ``slice_info.config`` object list
    (also slicer order, carrying the ``identify_id`` the skip command needs).
    The two are correlated by index; a short/missing slice list falls back to
    the bbox id so nothing regresses. Geometry ``bbox`` is preserved and the
    slice name is preferred only when geometry has none.
    """
    merged = []
    for i, obj in enumerate(geometry.get("objects") or []):
        plate_id = obj.get("id")
        identify_id = plate_id
        name = obj.get("name") or ""
        if i < len(slice_objects):
            so = slice_objects[i]
            if so.get("id"):
                identify_id = so["id"]
            if not name:
                name = so.get("name") or name
        merged.append({
            "id": identify_id,
            "plate_id": plate_id,
            "name": name,
            "bbox": obj.get("bbox"),
        })
    return merged


def available_trays(printer_id: str) -> dict:
    """Flatten the printer's AMS trays + external spool for the mapping dialog."""
    report = state.get_report(printer_id) or {}
    trays: List[dict] = []
    ams = report.get("ams") or {}
    for unit in (ams.get("ams") or []):
        try:
            ams_id = int(unit.get("id", 0))
        except (TypeError, ValueError):
            ams_id = 0
        for tray in (unit.get("tray") or []):
            try:
                tray_id = int(tray.get("id", 0))
            except (TypeError, ValueError):
                tray_id = 0
            trays.append({
                "index": ams_id * 4 + tray_id,
                "ams_id": ams_id,
                "tray_id": tray_id,
                "type": tray.get("tray_type") or "",
                "color": (tray.get("tray_color") or "").lstrip("#").upper()[:6],
                "remain": tray.get("remain", -1),
            })
    external = report.get("vt_tray") or {}
    return {
        "trays": trays,
        "external": {
            "index": 255,
            "type": external.get("tray_type") or "",
            "color": (external.get("tray_color") or "").lstrip("#").upper()[:6],
        } if external else None,
    }


def local_plate_indices(path: str) -> List[int]:
    """Plate numbers inside a local sliced project (a zip of Metadata/plate_N.gcode)."""
    try:
        with zipfile.ZipFile(path) as archive:
            found = set()
            for entry in archive.namelist():
                match = re.match(r"Metadata/plate_(\d+)\.gcode$", entry)
                if match:
                    found.add(int(match.group(1)))
        return sorted(found)
    except Exception as exc:
        logger.debug("Local plate detection for %s failed: %s", path, exc)
        return []


def pick_plate(plates: List[int], requested: int, filename: str) -> int:
    """Explicit request -> `_plate_N` in the name -> the only plate present."""
    if requested and requested in plates:
        return requested
    match = re.search(r"_plate_(\d+)", filename)
    if match and int(match.group(1)) in plates:
        return int(match.group(1))
    return plates[0]


async def ensure_not_failed(printer_id: str) -> None:
    """Clear a sticky FAILED state, or explain precisely why we cannot.

    ``gcode_state`` can stay ``FAILED`` long after the printer has been cleared on
    screen (``print_error`` back to 0, no HMS). That is a *stale* state and must
    not block a new job, so only an **active** error stops us.
    """
    def active_error() -> bool:
        report = state.get_report(printer_id) or {}
        print_error = report.get("print_error") or 0
        hms = [h for h in (report.get("hms") or []) if isinstance(h, dict)]
        return bool(print_error) or bool(hms)

    gcode_state = str((state.get_report(printer_id) or {}).get("gcode_state", "")).upper()
    if gcode_state != "FAILED":
        return

    was_active = active_error()
    await dispatch(printer_id, "clear_error", {}, confirm=True)
    await asyncio.sleep(2)
    gcode_state = str((state.get_report(printer_id) or {}).get("gcode_state", "")).upper()
    if gcode_state != "FAILED":
        return
    if not was_active and not active_error():
        logger.warning(
            "Printer %s reports FAILED with no active error (stale state) - proceeding", printer_id
        )
        return

    report = state.get_report(printer_id) or {}
    code = report.get("print_error") or "unknown"
    hms = report.get("hms") or []
    codes = ", ".join(format_hms(h) for h in hms[:3] if isinstance(h, dict))
    hint = f" Active HMS: {codes}." if codes else ""
    raise HTTPException(
        status_code=409,
        detail=(
            "The printer is in a FAILED state, so it will not start a new job. "
            f"Last error code: {code}.{hint} "
            "Dismiss the error on the printer's touchscreen (and check the build plate is clear), "
            "then try again."
        ),
    )


async def verify_started(printer_id: str, name: str) -> None:
    """After sending project_file, confirm the printer actually began the job."""
    await asyncio.sleep(PRINT_START_VERIFY_DELAY)
    gcode_state = str((state.get_report(printer_id) or {}).get("gcode_state", "")).upper()
    if gcode_state in ("PREPARE", "RUNNING", "SLICING"):
        return
    ack = state.get_ack(printer_id, "project_file") or {}
    reason = ack.get("reason") or ack.get("result") or "no reply"
    raise HTTPException(
        status_code=502,
        detail=(
            f"The printer did not start '{name}' (state stayed {gcode_state.lower() or 'unknown'}). "
            f"project_file was answered: {reason}."
        ),
    )


async def inject_pre_skip(printer_id: str, ids: Optional[List[int]]) -> List[int]:
    """Publish a pre-armed object skip as part of starting the print.

    This is the deterministic path for a selection made *before* the print
    starts: it rides along with the print request, so it cannot be lost to a
    browser WebSocket, telemetry timing, or the ``idle -> prepare`` transition.
    ``verify_started`` also accepts PREPARE/SLICING, but a skip only means
    something once the machine is actually running the job, so wait (bounded) for
    RUNNING first. The firmware queues object skips, so if the machine never gets
    there we still publish once - and either way this must never turn a
    successful print start into an error response.
    """
    wanted = [int(i) for i in (ids or [])]
    if not wanted:
        return []

    deadline = time.monotonic() + PRINT_SKIP_INJECT_TIMEOUT
    running = False
    while True:
        st = str((state.get_report(printer_id) or {}).get("gcode_state", "")).upper()
        if st == "RUNNING":
            running = True
            break
        # Nothing to wait for once the job has already ended again.
        if st in ("FAILED", "FINISH"):
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(PRINT_SKIP_POLL_INTERVAL)
    if not running:
        logger.warning(
            "%s did not report RUNNING within %.0fs - publishing the queued skip for %s anyway",
            printer_id, PRINT_SKIP_INJECT_TIMEOUT, wanted,
        )

    try:
        await dispatch(printer_id, "skip_objects", {"object_ids": wanted})
    except Exception as exc:  # the print already started; never fail it here
        logger.warning("Could not publish the pre-armed skip for %s: %s", printer_id, exc)
        return []
    logger.info(
        "Applied pre-armed skip for %s (%s): object ids %s",
        printer_id,
        (state.get_report(printer_id) or {}).get("subtask_name", "?"),
        wanted,
    )
    return wanted


# ------------------------------------------------------------------ controls


# --------------------------------------------------------------- SD browsing


# ------------------------------------------------------------------- camera


# ----------------------------------------------------------------- websocket


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = auth_token()
    if token:
        supplied = (
            websocket.cookies.get(AUTH_COOKIE)
            or websocket.headers.get("x-auth-token")
            or websocket.query_params.get("token", "")
        )
        if not secrets.compare_digest(supplied, token):
            await websocket.close(code=1008)  # policy violation
            return
    await ws_hub.connect(websocket)
    try:
        await websocket.send_json(
            {
                "event": "hello",
                "printers": state.reports_with_homed(),
            }
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_hub.disconnect(websocket)
    except Exception as exc:
        logger.debug("WebSocket closed: %s", exc)
        ws_hub.disconnect(websocket)

# --------------------------------------------------------------------- routes
#
# The REST surface lives in backend/routers/*; everything shared (config, state,
# dispatch, the safety gates, the WebSocket hub) stays here. Routers reach into
# this module as ``core``, so the values tests monkeypatch here (config paths,
# the MQTT manager) are the ones the handlers actually see.


def _install_routers() -> None:
    """Attach the API routers.

    Imported lazily: the routers import ``backend.main`` for shared helpers, so
    this has to run after everything above is defined.
    """
    from backend.routers import camera as camera_routes
    from backend.routers import files as file_routes
    from backend.routers import printers as printer_routes
    from backend.routers import system as system_routes
    from backend.routers import update as update_routes

    for module in (system_routes, update_routes, printer_routes, file_routes, camera_routes):
        app.include_router(module.router)


_install_routers()
