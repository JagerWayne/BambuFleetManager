"""Bambu Fleet Manager - FastAPI application, REST endpoints and WebSocket hub."""

import asyncio
import json
import logging
import os
import posixpath
import re
import secrets
import time
import xml.etree.ElementTree as ET
import zipfile
from contextlib import asynccontextmanager
from typing import Callable, Dict, List, Optional

import aiofiles
from fastapi import (
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
from fastapi.responses import (
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
from backend.filaments import FILAMENT_CATEGORIES, FILAMENTS
from backend.paths import DATA_DIR, RESOURCE_DIR, ensure_data_dirs
from backend import updater
from backend.ftp_client import (
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
from backend.models import (
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
    LightCommand,
    JogCommand,
    LightModeCommand,
    PrintDispatchCommand,
    PrintOptionCommand,
    PrintRemoteCommand,
    PrinterConfig,
    PrinterPublic,
    RebootCommand,
    RenameEntryCommand,
    ServerSettings,
    SendStagedCommand,
    RemotePath,
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

#: Latest full ``print`` report per printer, so a freshly opened dashboard (or a
#: reconnecting WebSocket) can render real state instead of zeroed placeholders.
latest_reports: Dict[str, dict] = {}

#: Tracked homing state per printer. ``home_flag`` in the report is unreliable on
#: X1 firmware (it reads "not homed" even right after G28), so homing is tracked
#: from commands we issue, and cleared when a job starts. Persisted to disk so it
#: survives a restart.
homed_state: Dict[str, bool] = {}

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
        homed_state.update({k: bool(v) for k, v in (data.get("homed") or {}).items()})
    except Exception as exc:
        logger.warning("Could not read %s: %s", STATE_PATH, exc)


def save_state() -> None:
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"homed": homed_state}, f, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as exc:
        logger.warning("Could not write %s: %s", STATE_PATH, exc)


def set_homed(printer_id: str, value: Optional[bool]) -> None:
    homed_state[printer_id] = value
    save_state()


#: Acknowledgements the printer sent, keyed by printer then command, so the API
#: can explain *why* something was refused instead of guessing.
last_acks: Dict[str, Dict[str, dict]] = {}


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
        last_acks.setdefault(printer_id, {})[command] = {
            "command": command,
            "result": report.get("result"),
            "reason": report.get("reason"),
            "err_code": report.get("err_code"),
        }


def track_homing(printer_id: str, report: dict) -> None:
    """Update the in-memory homing state (persisted only on explicit events)."""
    state = str(report.get("gcode_state", "")).upper()
    if state in ("RUNNING", "PREPARE", "SLICING"):
        homed_state[printer_id] = False
        return
    flag = report.get("home_flag")
    # Only trust the flag when it clearly says "all axes homed".
    if isinstance(flag, int) and (flag & 0b111) == 0:
        homed_state[printer_id] = True


async def telemetry_handler(printer_id: str, data: dict) -> None:
    latest_reports[printer_id] = data
    track_homing(printer_id, data)
    track_ack(printer_id, data)
    await ws_hub.broadcast({
        "event": "telemetry",
        "printer_id": printer_id,
        "data": data,
        "homed": homed_state.get(printer_id),
    })


def read_printers() -> Dict[str, PrinterConfig]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: PrinterConfig(**v) for k, v in data.items()}
    except Exception as exc:
        logger.error("Could not parse %s: %s", CONFIG_PATH, exc)
        return {}


def save_printers(printers: Dict[str, PrinterConfig]) -> None:
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({k: v.model_dump() for k, v in printers.items()}, f, indent=2)
    os.replace(tmp_path, CONFIG_PATH)


def read_settings() -> Dict[str, object]:
    if not os.path.exists(SETTINGS_PATH):
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            stored = json.load(f)
    except Exception as exc:
        logger.error("Could not parse %s: %s", SETTINGS_PATH, exc)
        return dict(DEFAULT_SETTINGS)
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: stored[k] for k in DEFAULT_SETTINGS if k in stored})
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
    return homed_state.get(printer_id)


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

    report = latest_reports.get(printer_id) or {}
    state = str(report.get("gcode_state", "")).upper()
    active = state not in ("", "IDLE", "FINISH", "FAILED")

    if risk in ("motion", "home") and active:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Printer is {state.lower()} - the head and bed must be still before this "
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


@app.post("/api/login")
async def do_login(cmd: Dict[str, str] = Body(default={})):
    token = auth_token()
    supplied = (cmd or {}).get("token", "")
    if not token or not secrets.compare_digest(supplied, token):
        raise HTTPException(status_code=401, detail="invalid token")
    response = JSONResponse({"status": "ok"})
    response.set_cookie(AUTH_COOKIE, supplied, httponly=True, samesite="lax")
    return response


@app.post("/api/logout")
async def do_logout():
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(AUTH_COOKIE)
    return response


async def run_blocking(fn, *args):
    """Run a blocking FTP call off the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


# --------------------------------------------------------------------- pages


@app.get("/VERSION", include_in_schema=False, response_class=PlainTextResponse)
async def read_version():
    version_file = os.path.join(RESOURCE_DIR, "VERSION")
    if os.path.exists(version_file):
        with open(version_file, "r", encoding="utf-8") as f:
            return PlainTextResponse(f.read().strip())
    return PlainTextResponse("0.0.0")


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})


# --------------------------------------------------------------- settings


@app.get("/api/settings")
async def get_settings(request: Request):
    """Server listen address (applied on next start) plus the port in use now."""
    settings = read_settings()
    return {
        "host": settings.get("host"),
        "port": settings.get("port"),
        "allow_motion": bool(settings.get("allow_motion")),
        "auth_required": bool(settings.get("auth_token")),
        "current_port": request.url.port,
        "current_host": request.url.hostname,
    }


@app.get("/api/filaments")
async def list_filaments():
    """Bambu material catalogue for the AMS tray editor.

    Ids are read from genuine Bambu spool RFID tags, so writing one makes the
    printer show the correct material instead of "?".
    """
    return {"categories": FILAMENT_CATEGORIES, "filaments": FILAMENTS}


UPDATE_DIR = os.path.join(DATA_DIR, "updates")


@app.get("/api/update")
async def check_update():
    """Ask GitHub Releases whether a newer build is available."""
    return await run_blocking(updater.check_for_update)


@app.post("/api/update/download")
async def download_update():
    """Download the installer and stop there - nothing is launched.

    The dashboard offers this as a separate step so the file can be fetched
    first (and kept, or run by hand) instead of download-and-launch in one go.
    """
    report = await run_blocking(updater.check_for_update)
    if not report.get("update_available") or not report.get("asset_url"):
        raise HTTPException(status_code=409, detail=report.get("error") or "No update available")

    try:
        installer = await run_blocking(
            updater.download_installer, UPDATE_DIR, report["asset_url"],
            int(report.get("asset_size") or 0)
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Download failed: {exc}")

    logger.info("Staged installer %s for %s at %s", report.get("latest"), report.get("asset_size"), installer)
    return {
        "downloaded": True,
        "version": report.get("latest"),
        "installer": installer,
        "size": os.path.getsize(installer),
    }


@app.get("/api/update/installer")
async def download_installer_copy():
    """Serve the staged installer as a normal browser download.

    Same bytes, saved wherever the browser puts it - handy for keeping a copy
    or running it from somewhere other than the app's data directory.
    """
    installer = updater.staged_installer(UPDATE_DIR)
    if not installer:
        raise HTTPException(status_code=404, detail="No installer has been downloaded yet")
    return FileResponse(
        installer,
        media_type="application/vnd.microsoft.portable-executable",
        filename=os.path.basename(installer),
    )


@app.post("/api/update/run")
async def run_staged_installer():
    """Launch an already-downloaded installer, then quit to free the files.

    The second half of the manual flow: download first, run when you are ready.
    """
    report = await run_blocking(updater.check_for_update)
    installer = updater.staged_installer(UPDATE_DIR)
    if not installer:
        raise HTTPException(
            status_code=409, detail="Nothing downloaded yet - press Download first.")

    # Refuse to run a file that does not match the published asset size.
    expected = int(report.get("asset_size") or 0)
    actual = os.path.getsize(installer)
    if expected and actual != expected:
        raise HTTPException(
            status_code=409,
            detail=(f"The downloaded installer is incomplete ({actual} of {expected} bytes). "
                    "Download it again."),
        )

    try:
        await run_blocking(updater.launch_installer, installer)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start installer: {exc}")

    logger.info("Running staged installer %s (%s)", report.get("latest"), installer)
    # Give the response a moment to reach the browser, then quit.
    asyncio.get_event_loop().call_later(1.0, run_shutdown_hooks)
    return {"started": True, "version": report.get("latest"), "installer": installer}


@app.post("/api/update/install")
async def install_update():
    """Download the latest installer and launch it, then quit to free the files.

    The installer runs silently and upgrades in place. We shut the app down so
    Inno Setup can replace the running files; the installer relaunches it when
    done (``/RESTARTAPPLICATIONS``). This is the one-shot flow; the dashboard
    also exposes download and run as separate steps.
    """
    report = await run_blocking(updater.check_for_update)
    if not report.get("update_available") or not report.get("asset_url"):
        raise HTTPException(status_code=409, detail=report.get("error") or "No update available")

    try:
        installer = await run_blocking(
            updater.download_installer, UPDATE_DIR, report["asset_url"],
            int(report.get("asset_size") or 0)
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Download failed: {exc}")

    try:
        await run_blocking(updater.launch_installer, installer)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start installer: {exc}")

    logger.info("Updating to %s via %s", report.get("latest"), installer)
    # Give the response a moment to reach the browser, then quit.
    asyncio.get_event_loop().call_later(1.0, run_shutdown_hooks)
    return {"started": True, "version": report.get("latest"), "installer": installer}


@app.post("/api/settings")
async def update_settings(cmd: ServerSettings):
    current = read_settings()
    token = str(current.get("auth_token") or "")
    if cmd.clear_auth:
        token = ""
    elif cmd.auth_token:
        token = cmd.auth_token
    settings = {
        "host": cmd.host,
        "port": cmd.port,
        "allow_motion": cmd.allow_motion,
        "auth_token": token,
    }
    save_settings(settings)
    logger.info("Settings saved: host=%s port=%s allow_motion=%s auth=%s",
                cmd.host, cmd.port, cmd.allow_motion, bool(token))
    return {"status": "saved", "restart_required": True, "auth_required": bool(token)}


# -------------------------------------------------------------------- fleet


@app.get("/api/commands")
async def list_commands():
    """Every command name the fleet API accepts, grouped by MQTT channel."""
    return {
        "commands": cmd_mod.available_commands(),
        "groups": cmd_mod.command_groups(),
        "speed_profiles": cmd_mod.SPEED_PROFILES,
        "risk": {name: cmd_mod.risk_class(name) for name in cmd_mod.available_commands()},
    }


@app.get("/api/printers", response_model=List[PrinterPublic])
async def list_printers():
    """Registered nodes. The access code is never included in the response."""
    return [
        PrinterPublic(
            id=p.id, name=p.name, ip=p.ip, sn=p.sn, bed_type=p.bed_type,
            ams_count=p.ams_count, has_access_code=bool(p.access_code),
        )
        for p in read_printers().values()
    ]


@app.post("/api/printers")
async def add_or_update_printer(printer: PrinterConfig):
    printers = read_printers()
    # An empty access code means "keep the one already stored" (the API never
    # hands the code back, so the edit dialog submits it blank).
    existing = printers.get(printer.id)
    if existing and not printer.access_code:
        printer.access_code = existing.access_code
    if not printer.access_code:
        raise HTTPException(status_code=400, detail="access code is required")
    printers[printer.id] = printer
    save_printers(printers)
    require_manager().register_printer(printer.id, printer.ip, printer.sn, printer.access_code)
    return {"status": "ok", "printer": PrinterPublic(
        id=printer.id, name=printer.name, ip=printer.ip, sn=printer.sn,
        bed_type=printer.bed_type, ams_count=printer.ams_count, has_access_code=True,
    )}


@app.delete("/api/printers/{printer_id}")
async def remove_printer(printer_id: str):
    printers = read_printers()
    if printer_id not in printers:
        raise HTTPException(status_code=404, detail="Printer node not found")
    del printers[printer_id]
    save_printers(printers)
    latest_reports.pop(printer_id, None)
    require_manager().unregister_printer(printer_id)
    return {"status": "deleted"}


@app.get("/api/status")
async def fleet_status():
    manager = require_manager()
    return {"mqtt": manager.status(), "printers": len(read_printers())}


@app.get("/api/printers/{printer_id}/telemetry")
async def printer_telemetry(printer_id: str):
    get_printer(printer_id)
    return {
        "printer_id": printer_id,
        "homed": is_homed(printer_id),
        "data": latest_reports.get(printer_id, {}),
    }


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


@app.get("/api/uploads", response_model=List[UploadEntry])
async def list_uploads():
    """Files currently staged on the server (so the queue survives a refresh)."""
    entries: List[UploadEntry] = []
    for name in sorted(os.listdir(UPLOAD_DIR)):
        path = os.path.join(UPLOAD_DIR, name)
        if not os.path.isfile(path) or name.startswith("."):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        entries.append(UploadEntry(filename=name, size=stat.st_size, modified=stat.st_mtime))
    return entries


@app.delete("/api/uploads/{filename}")
async def delete_upload(filename: str):
    safe = os.path.basename(filename)
    path = os.path.join(UPLOAD_DIR, safe)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Staged file not found")
    os.remove(path)
    return {"status": "deleted", "filename": safe}


@app.post("/api/stage-upload", response_model=StagedFile)
async def stage_upload(file: UploadFile = File(...)):
    filename = os.path.basename(file.filename or "")
    if not filename.lower().endswith(ALLOWED_SUFFIXES):
        raise HTTPException(status_code=400, detail="Only .3mf / .gcode.3mf projects can be staged")

    dest_path = os.path.join(UPLOAD_DIR, filename)
    size = await _save_upload(file, dest_path)
    logger.info("Staged %s (%d bytes)", filename, size)
    return StagedFile(filename=filename, size=size)


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
    report = latest_reports.get(printer_id) or {}
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
        report = latest_reports.get(printer_id) or {}
        print_error = report.get("print_error") or 0
        hms = [h for h in (report.get("hms") or []) if isinstance(h, dict)]
        return bool(print_error) or bool(hms)

    state = str((latest_reports.get(printer_id) or {}).get("gcode_state", "")).upper()
    if state != "FAILED":
        return

    was_active = active_error()
    await dispatch(printer_id, "clear_error", {}, confirm=True)
    await asyncio.sleep(2)
    state = str((latest_reports.get(printer_id) or {}).get("gcode_state", "")).upper()
    if state != "FAILED":
        return
    if not was_active and not active_error():
        logger.warning(
            "Printer %s reports FAILED with no active error (stale state) - proceeding", printer_id
        )
        return

    report = latest_reports.get(printer_id) or {}
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
    state = str((latest_reports.get(printer_id) or {}).get("gcode_state", "")).upper()
    if state in ("PREPARE", "RUNNING", "SLICING"):
        return
    ack = (last_acks.get(printer_id) or {}).get("project_file") or {}
    reason = ack.get("reason") or ack.get("result") or "no reply"
    raise HTTPException(
        status_code=502,
        detail=(
            f"The printer did not start '{name}' (state stayed {state.lower() or 'unknown'}). "
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
        st = str((latest_reports.get(printer_id) or {}).get("gcode_state", "")).upper()
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
    return wanted


@app.post("/api/dispatch-print", response_model=DispatchResponse)
async def dispatch_print(cmd: PrintDispatchCommand):
    printers = read_printers()
    if cmd.printer_id not in printers:
        raise HTTPException(status_code=404, detail="Target printer node not found")

    printer = printers[cmd.printer_id]
    local_file = os.path.join(UPLOAD_DIR, cmd.filename)
    if not os.path.exists(local_file):
        raise HTTPException(status_code=400, detail="Target 3MF file not found in upload cache")

    # Work out the plate before touching the printer (see print_remote).
    plates = local_plate_indices(local_file)
    if not plates:
        raise HTTPException(
            status_code=400,
            detail=f"No printable plate (Metadata/plate_N.gcode) found inside {cmd.filename}.",
        )
    plate = pick_plate(plates, cmd.plate_index, cmd.filename)

    # Step 1: push the sliced file over implicit FTPS.
    upload_success = await run_blocking(upload_3mf_file, printer.ip, printer.access_code, local_file)
    if not upload_success:
        raise HTTPException(status_code=502, detail="FTPS transfer failed")

    # Step 2: refuse politely if the printer is stuck, then start the job.
    await ensure_not_failed(cmd.printer_id)
    await dispatch(cmd.printer_id, "print_option", {"bed_type": printer.bed_type})
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
    if not await run_blocking(require_manager().send_to_printer, cmd.printer_id, project_file):
        raise HTTPException(
            status_code=503,
            detail="Printer node is offline over MQTTS - the print was not started.",
        )
    await verify_started(cmd.printer_id, cmd.filename)
    skipped = await inject_pre_skip(cmd.printer_id, cmd.skip_object_ids)
    return DispatchResponse(
        status="started", printer=printer.name, job=cmd.filename, skipped=skipped
    )


@app.get("/api/printers/{printer_id}/files/plan")
async def print_plan(printer_id: str, path: str = Query(..., max_length=512)):
    """Everything the print dialog needs: plates, time/weight and filaments."""
    printer = get_printer(printer_id)
    target = normalize_remote_path(path)
    try:
        entries = await run_blocking(
            read_remote_zip_entries, printer.ip, printer.access_code, target,
            ["Metadata/slice_info.config"],
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not read the project: {exc}")
    raw = entries.get("Metadata/slice_info.config")
    plates = parse_slice_info(raw.decode("utf-8", "replace")) if raw else []
    if not plates:
        plates = [{"index": i, "time_sec": 0, "weight_g": 0, "filaments": []}
                  for i in await run_blocking(detect_plate_indices, printer.ip, printer.access_code, target)]
    return {
        "path": target,
        "name": posixpath.basename(target),
        **available_trays(printer_id),
        "plates": plates,
    }


@app.get("/api/printers/{printer_id}/files/plate")
async def print_plate(
    printer_id: str,
    path: str = Query(..., max_length=512),
    plate: int = Query(1, ge=1),
    size: str = Query("large"),
):
    """Serve the plate thumbnail baked into the project (Metadata/plate_N.png)."""
    printer = get_printer(printer_id)
    target = normalize_remote_path(path)
    suffix = "small" if size == "small" else ""
    candidates = [
        f"Metadata/plate_{plate}{'_small' if suffix else ''}.png",
        f"Metadata/plate_{plate}.png",
        f"Metadata/top_{plate}.png",
    ]
    try:
        entries = await run_blocking(
            read_remote_zip_entries, printer.ip, printer.access_code, target, candidates
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not read the project: {exc}")
    for name in candidates:
        data = entries.get(name)
        if data:
            return Response(content=data, media_type="image/png",
                            headers={"Cache-Control": "no-store"})
    raise HTTPException(status_code=404, detail="No thumbnail for that plate")


@app.get("/api/printers/{printer_id}/files/objects")
async def print_objects(
    printer_id: str,
    path: str = Query(..., max_length=512),
    plate: Optional[int] = Query(None, ge=1),
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
    printer = get_printer(printer_id)
    target = normalize_remote_path(path)

    if plate is None:
        try:
            entries = await run_blocking(
                read_remote_zip_entries, printer.ip, printer.access_code, target,
                ["Metadata/slice_info.config"],
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not read the project: {exc}")
        raw = entries.get("Metadata/slice_info.config")
        plates = parse_slice_info(raw.decode("utf-8", "replace")) if raw else []
        if plates:
            plate = plates[0]["index"] or 1
        else:
            detected = await run_blocking(
                detect_plate_indices, printer.ip, printer.access_code, target
            )
            plate = detected[0] if detected else 1

    entry = f"Metadata/plate_{plate}.json"
    slice_entry = "Metadata/slice_info.config"
    try:
        entries = await run_blocking(
            read_remote_zip_entries, printer.ip, printer.access_code, target,
            [entry, slice_entry],
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not read the project: {exc}")
    raw = entries.get(entry)
    if not raw:
        raise HTTPException(
            status_code=404,
            detail=f"No {entry} in the project - it is not a sliced 3MF with a plate map.",
        )
    geometry = parse_plate_geometry(raw.decode("utf-8", "replace"))

    # Correlate the bbox objects with slice_info identify_ids by slicer index.
    slice_objects = []
    slice_raw = entries.get(slice_entry)
    if slice_raw:
        for pl in parse_slice_info(slice_raw.decode("utf-8", "replace")):
            if pl.get("index") == plate:
                slice_objects = pl.get("objects") or []
                break
    geometry["objects"] = merge_plate_object_ids(geometry, slice_objects)
    return {"path": target, "plate": plate, **geometry}


# ------------------------------------------------------------------ controls


@app.post("/api/printers/{printer_id}/pause")
async def pause_printer(printer_id: str):
    return await dispatch(printer_id, "pause")


@app.post("/api/printers/{printer_id}/resume")
async def resume_printer(printer_id: str):
    return await dispatch(printer_id, "resume")


@app.post("/api/printers/{printer_id}/stop")
async def stop_printer(printer_id: str):
    return await dispatch(printer_id, "stop")


@app.post("/api/printers/{printer_id}/speed")
async def change_speed(printer_id: str, cmd: SpeedCommand):
    return await dispatch(printer_id, "speed", {"level": cmd.speed_level})


@app.post("/api/printers/{printer_id}/speed-profile")
async def change_speed_profile(printer_id: str, cmd: SpeedProfileCommand):
    return await dispatch(printer_id, "speed", {"level": cmd.level})


@app.post("/api/printers/{printer_id}/light")
async def toggle_light(printer_id: str, cmd: LightCommand):
    return await dispatch(printer_id, "light", {"mode": "on" if cmd.state else "off"})


@app.post("/api/printers/{printer_id}/light-mode")
async def light_mode(printer_id: str, cmd: LightModeCommand):
    return await dispatch(printer_id, "light", cmd.model_dump())


@app.post("/api/printers/{printer_id}/calibrate")
async def calibrate(printer_id: str, cmd: CalibrationCommand):
    """Home first, then run the routine - so it is never started from an unknown pose."""
    get_printer(printer_id)
    params: Dict[str, object] = {}
    if cmd.kind == "flow_calibration":
        params = {"mode": cmd.mode, "state": "enable" if cmd.mode else "disable"}

    # 1) home all axes (this is itself a motion command, gated by confirm_motion)
    await dispatch(printer_id, "home", {"axis": ""}, confirm=cmd.confirm_motion)
    set_homed(printer_id, True)
    await asyncio.sleep(CALIBRATION_HOME_DELAY)  # let the printer finish homing

    # 2) run the routine; homing is already done, so the check is satisfied
    return await dispatch(printer_id, cmd.kind, params, confirm=cmd.confirm_motion, assume_homed=True)


@app.post("/api/printers/{printer_id}/ams")
async def ams_action(printer_id: str, cmd: AmsActionCommand):
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
    return await dispatch(
        printer_id, mapping[cmd.action], params, confirm=cmd.confirm_thermal
    )


@app.post("/api/printers/{printer_id}/skip-objects")
async def skip_objects(printer_id: str, cmd: SkipObjectCommand):
    if not cmd.object_ids:
        raise HTTPException(status_code=400, detail="No object ids supplied")
    return await dispatch(printer_id, "skip_objects", {"object_ids": cmd.object_ids})


@app.post("/api/printers/{printer_id}/print-option")
async def print_option(printer_id: str, cmd: PrintOptionCommand):
    params = {k: v for k, v in cmd.model_dump().items() if v is not None}
    return await dispatch(printer_id, "print_option", params)


@app.post("/api/printers/{printer_id}/retry")
async def retry_after_error(printer_id: str):
    return await dispatch(printer_id, "print_error", {"err_code": 0, "sub_err_code": 0})


@app.post("/api/printers/{printer_id}/clear-error")
async def clear_error(printer_id: str):
    """Best-effort clear of a failed print, including any active HMS faults.

    Returns what was tried and whether the printer left the FAILED state. Some
    faults (notably storage/HMS ones) can only be dismissed on the touchscreen.
    """
    get_printer(printer_id)
    report = latest_reports.get(printer_id) or {}
    attempts: List[dict] = []

    # 1) the documented clean_print_error, with the real error code (as OrcaSlicer
    #    sends it: print_error + subtask_id)
    attempts.append(await dispatch(
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
        for err in (format_hms(entry), f"{attr:08X}{code:08X}"):
            try:
                attempts.append(await dispatch(
                    printer_id, "hms_ignore",
                    {"action": "idle_ignore", "err": err, "type": 1},
                    confirm=True,
                ))
                break
            except HTTPException as exc:
                attempts.append({"command": "hms_ignore", "err": err, "error": exc.detail})

    await asyncio.sleep(2)
    state = str((latest_reports.get(printer_id) or {}).get("gcode_state", "")).upper()
    cleared = state != "FAILED"
    if not cleared:
        logger.warning("Failed state on %s survived every clear attempt", printer_id)
    return {"status": "attempted", "state": state.lower(), "cleared": cleared, "attempts": attempts}


@app.post("/api/printers/{printer_id}/filament")
async def set_filament(printer_id: str, cmd: FilamentSettingCommand):
    """Write a filament record (type, colour, remaining) into an AMS tray.

    Metadata only - nothing heats or moves - so no thermal confirmation is needed.
    """
    get_printer(printer_id)
    colour = cmd.color.lstrip("#").upper()
    if len(colour) == 6:
        colour += "FF"
    return await dispatch(printer_id, "ams_filament_setting", {
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


@app.post("/api/printers/{printer_id}/temperature")
async def set_temperature(printer_id: str, cmd: TemperatureCommand):
    """Manual nozzle / bed / chamber setpoints, capped and gated."""
    get_printer(printer_id)
    targets = cmd.targets
    if not targets:
        raise HTTPException(status_code=400, detail="No temperature values supplied")
    if not cmd.confirm_thermal:
        raise HTTPException(
            status_code=409,
            detail="Setting temperatures heats the machine. Re-send with confirm_thermal=true.",
        )

    report = latest_reports.get(printer_id) or {}
    state = str(report.get("gcode_state", "")).upper()
    if state not in ("", "IDLE", "FINISH", "FAILED") and not cmd.allow_while_printing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Printer is {state.lower()} - changing setpoints now would fight the running job. "
                "Pause or stop it first, or re-send with allow_while_printing=true."
            ),
        )

    sent = {}
    for target, value in targets.items():
        low, high = cmd_mod.TEMP_LIMITS[target]
        if not low <= value <= high:
            raise HTTPException(
                status_code=400,
                detail=f"{target} temperature must be between {low} and {high} C (got {value})",
            )
        sent[target] = await dispatch(
            printer_id, f"set_{target}_temp", {"temp": value}, confirm=True
        )
    logger.warning("TEMPERATURE change on %s: %s", printer_id, sent)
    return {"status": "sent", "targets": targets, "printer": printer_id}


@app.post("/api/printers/{printer_id}/fan")
async def set_fan(printer_id: str, cmd: FanCommand):
    """Set a cooling fan's speed (percentage).

    Moves no axes and heats nothing, so it is a `safe` command with no
    confirmation gate - but it is still worth knowing which fan index the
    firmware uses, hence each control sits next to that fan's reported speed.
    """
    get_printer(printer_id)
    try:
        return await dispatch(printer_id, "set_fan", {"fan": cmd.fan, "speed": cmd.speed})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/printers/{printer_id}/home")
async def home_axes(printer_id: str, cmd: HomeCommand):
    """Home all axes, or just one. Allowed while unhomed - that is the point."""
    get_printer(printer_id)
    result = await dispatch(printer_id, "home", {"axis": cmd.axes}, confirm=cmd.confirm_motion)
    if not cmd.axes:
        set_homed(printer_id, True)  # we just homed everything
    return result


@app.post("/api/printers/{printer_id}/jog")
async def jog_axis(printer_id: str, cmd: JogCommand):
    """Move one axis by a small relative amount. Requires homing + confirmation."""
    get_printer(printer_id)
    return await dispatch(
        printer_id,
        "jog",
        {"axis": cmd.axis.upper(), "distance": cmd.distance, "feedrate": cmd.feedrate},
        confirm=cmd.confirm_motion,
    )


@app.post("/api/printers/{printer_id}/extrude")
async def extrude(printer_id: str, cmd: ExtrudeCommand):
    """Extrude or retract filament - refuses on a cold nozzle to avoid grinding."""
    get_printer(printer_id)
    report = latest_reports.get(printer_id) or {}
    state = str(report.get("gcode_state", "")).upper()
    if state not in ("", "IDLE", "FINISH", "FAILED"):
        raise HTTPException(
            status_code=409,
            detail=f"Printer is {state.lower()} - stop the job before moving the extruder.",
        )
    nozzle = report.get("nozzle_temper")
    target = report.get("nozzle_target_temper") or 0
    hot = isinstance(nozzle, (int, float)) and (nozzle >= 170 or target >= 170)
    if cmd.amount > 0 and not hot:
        raise HTTPException(
            status_code=409,
            detail=(
                "Nozzle is below 170 C - extruding now would grind the filament. "
                "Heat the nozzle first (Temperatures tab)."
            ),
        )
    return await dispatch(
        printer_id,
        "extrude",
        {"amount": cmd.amount, "feedrate": cmd.feedrate},
        confirm=cmd.confirm_thermal,
    )


@app.post("/api/printers/{printer_id}/reboot")
async def reboot(printer_id: str, cmd: RebootCommand):
    return await dispatch(printer_id, "restart_module", {"module": cmd.module})


@app.post("/api/printers/{printer_id}/refresh")
async def refresh_state(printer_id: str):
    get_printer(printer_id)
    return await dispatch(printer_id, "push_state")


@app.post("/api/printers/{printer_id}/command")
async def generic_command(printer_id: str, cmd: GenericCommand):
    get_printer(printer_id)
    params = dict(cmd.params)
    confirm = bool(params.pop("confirm", False))
    return await dispatch(printer_id, cmd.command, params, confirm=confirm)


# --------------------------------------------------------------- SD browsing


@app.get("/api/printers/{printer_id}/files", response_model=DirectoryListing)
async def browse_files(printer_id: str, path: str = Query("/", max_length=512)):
    printer = get_printer(printer_id)
    target = normalize_remote_path(path)
    try:
        entries = await run_blocking(list_directory, printer.ip, printer.access_code, target)
    except Exception as exc:
        logger.error("Listing %s%s on %s failed: %s", target, "", printer_id, exc)
        raise HTTPException(status_code=502, detail=f"FTPS listing failed: {exc}")

    parent = None if target == "/" else posixpath.dirname(target) or "/"
    return DirectoryListing(
        path=target,
        parent=parent,
        entries=[
            FileNode(**(e.to_dict() if hasattr(e, "to_dict") else e)) for e in entries
        ],
    )


@app.delete("/api/printers/{printer_id}/files")
async def delete_remote_file(printer_id: str, cmd: DeleteEntryCommand):
    printer = get_printer(printer_id)
    try:
        await run_blocking(delete_path, printer.ip, printer.access_code, cmd.path, cmd.is_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Delete failed: {exc}")
    return {"status": "deleted", "path": cmd.path}


@app.post("/api/printers/{printer_id}/files/rename")
async def rename_remote_file(printer_id: str, cmd: RenameEntryCommand):
    """Rename a .3mf project on the SD card, in place.

    Folders and non-project files cannot be renamed, the extension is pinned to
    ``.3mf`` (a renamed project that lost its suffix would stop being printable
    and would fall out of the Print files filter), and the name may not contain a
    path, so a rename can never move a file into another folder.
    """
    printer = get_printer(printer_id)
    source = normalize_remote_path(cmd.path)
    name = cmd.new_name.strip()

    if not source.lower().endswith(".3mf"):
        raise HTTPException(status_code=400, detail="Only .3mf files can be renamed.")
    if not name.lower().endswith(".3mf"):
        raise HTTPException(status_code=400, detail="The new name must end in .3mf.")
    if name in (".", "..") or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="The new name must not contain a path.")

    parent = posixpath.dirname(source) or "/"
    target = posixpath.join(parent, name)
    if target == source:
        return {"status": "unchanged", "path": source, "name": name}

    try:
        new_path = await run_blocking(
            rename_path, printer.ip, printer.access_code, source, name
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Rename failed: {exc}")
    return {"status": "renamed", "path": new_path, "name": name}


@app.post("/api/printers/{printer_id}/files/upload-staged")
async def upload_staged_file(printer_id: str, cmd: SendStagedCommand):
    """Send a file already staged on this server to the printer's SD card.

    This is the mobile-friendly replacement for dragging a staged file onto the
    card: send it, then run the normal print preview on the uploaded path.
    """
    printer = get_printer(printer_id)
    name = cmd.filename.strip()
    if not name.lower().endswith(ALLOWED_SUFFIXES):
        raise HTTPException(status_code=400, detail="Only .3mf / .gcode.3mf projects can be sent")
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="filename must not contain a path")

    local_path = os.path.join(UPLOAD_DIR, name)
    if not os.path.exists(local_path):
        raise HTTPException(status_code=404, detail=f"{name} is not staged on this server")

    target_dir = normalize_remote_path(cmd.dir_path)
    try:
        ok = await run_blocking(
            upload_3mf_file, printer.ip, printer.access_code, local_path, target_dir
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upload failed: {exc}")
    if not ok:
        raise HTTPException(status_code=502, detail="FTPS transfer failed")

    remote = posixpath.join(target_dir, name) if target_dir != "/" else f"/{name}"
    logger.info("Staged file %s sent to %s at %s", name, printer_id, remote)
    return {"status": "sent", "path": remote, "name": name, "dir": target_dir}


@app.get("/api/printers/{printer_id}/files/download")
async def download_remote_file(printer_id: str, path: str = Query(..., max_length=512)):
    printer = get_printer(printer_id)
    target = normalize_remote_path(path)
    filename = posixpath.basename(target) or "download.bin"

    # A streaming response cannot change its status once started, so confirm the
    # file is really there before committing to a 200.
    if not await run_blocking(remote_file_exists, printer.ip, printer.access_code, target):
        raise HTTPException(status_code=404, detail=f"{target} not found on the SD card")

    logger.info("Streaming %s from %s to a client", target, printer_id)

    def iterator():
        try:
            for chunk in download_stream(printer.ip, printer.access_code, target):
                yield chunk
        except Exception as exc:  # client disconnected mid-transfer
            logger.info("Download of %s aborted: %s", target, exc)

    return StreamingResponse(
        iterator(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/printers/{printer_id}/print-remote", response_model=DispatchResponse)
async def print_remote(printer_id: str, cmd: PrintRemoteCommand):
    """Start a project that already lives on the printer's SD card."""
    printer = get_printer(printer_id)
    if cmd.path == "/":
        raise HTTPException(status_code=400, detail="A project file must be selected")
    if not cmd.path.lower().endswith(ALLOWED_SUFFIXES):
        raise HTTPException(status_code=400, detail="Only .3mf / .gcode.3mf projects can be printed")

    name = posixpath.basename(cmd.path)

    # A sliced project can hold several plates; the printer must be told exactly
    # which one to run or it reports the file as unreadable. Work it out from the
    # archive (falling back to the `_plate_N` in the file name).
    plates = await run_blocking(
        detect_plate_indices, printer.ip, printer.access_code, cmd.path
    )
    if not plates:
        raise HTTPException(
            status_code=400,
            detail=f"No printable plate (Metadata/plate_N.gcode) found inside {name}.",
        )
    plate = pick_plate(plates, cmd.plate_index, name)
    logger.info("Printing %s plate %s (file has plates %s)", name, plate, plates)

    # A printer left in FAILED will refuse any new job.
    await ensure_not_failed(printer_id)
    await dispatch(printer_id, "print_option", {"bed_type": printer.bed_type})

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
    if not await run_blocking(require_manager().send_to_printer, printer_id, project_file):
        raise HTTPException(
            status_code=503,
            detail="Printer node is offline over MQTTS - the print was not started.",
        )

    # Give the printer a moment and report back whether it actually accepted it -
    # a 200 here only means the MQTT publish succeeded.
    await verify_started(printer_id, name)
    skipped = await inject_pre_skip(printer_id, cmd.skip_object_ids)
    return DispatchResponse(status="started", printer=printer.name, job=name, skipped=skipped)


@app.post("/api/printers/{printer_id}/files/upload-dir")
async def upload_into_dir(printer_id: str, dir_path: RemotePath, file: UploadFile = File(...)):
    """Upload a staged file into an arbitrary SD-card folder."""
    printer = get_printer(printer_id)
    filename = os.path.basename(file.filename or "")
    if not filename.lower().endswith(ALLOWED_SUFFIXES):
        raise HTTPException(status_code=400, detail="Only .3mf / .gcode.3mf projects can be uploaded")

    local_path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(local_path):
        await _save_upload(file, local_path)

    ok = await run_blocking(
        upload_3mf_file, printer.ip, printer.access_code, local_path, dir_path.path
    )
    if not ok:
        raise HTTPException(status_code=502, detail="FTPS transfer failed")
    return {"status": "uploaded", "path": posixpath.join(dir_path.path, filename)}


# ------------------------------------------------------------------- camera


@app.post("/api/printers/{printer_id}/camera")
async def camera_state(printer_id: str, cmd: CameraStateCommand):
    get_printer(printer_id)
    action = "enable" if cmd.state else "disable"
    return await dispatch(printer_id, "camera", {"action": action})


@app.get("/api/printers/{printer_id}/camera/snapshot")
async def camera_snapshot(printer_id: str, cache_bust: Optional[int] = None):
    printer = get_printer(printer_id)
    # ffmpeg pulls a frame straight from the RTSPS stream; the printer's own
    # HTTP camera service is blocked on current firmware, so it is the fallback.
    frame = await run_blocking(camera_mod.ffmpeg_snapshot, printer.ip, printer.access_code)
    if not frame:
        frame = await run_blocking(camera_mod.fetch_snapshot, printer.ip)
    if not frame:
        raise HTTPException(
            status_code=502, detail="Camera snapshot unavailable (is the camera enabled?)"
        )
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}
    if cache_bust:
        headers["X-Cache-Bust"] = str(cache_bust)
    return Response(content=frame, media_type="image/jpeg", headers=headers)


@app.get("/api/printers/{printer_id}/camera/mjpeg")
async def camera_mjpeg(
    printer_id: str,
    request: Request,
    width: int = Query(960, ge=160, le=1920),
    fps: int = Query(10, ge=1, le=30),
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
    printer = get_printer(printer_id)
    if not camera_mod.ffmpeg_exe():
        raise HTTPException(
            status_code=503,
            detail="ffmpeg is not installed. Run: pip install imageio-ffmpeg (or install ffmpeg).",
        )

    try:
        stream = await run_blocking(
            camera_mod.start_mjpeg_stream, printer_id, printer.ip, printer.access_code, width, fps
        )
    except Exception as exc:
        logger.error("Could not start the MJPEG stream for %s: %s", printer_id, exc)
        raise HTTPException(status_code=502, detail=f"Camera stream unavailable: {exc}")

    loop = asyncio.get_running_loop()

    async def chunks():
        try:
            while True:
                if await request.is_disconnected():
                    logger.info("Viewer left the MJPEG stream for %s", printer_id)
                    break
                chunk = await loop.run_in_executor(None, stream.read, 4096)
                if not chunk:
                    break
                yield chunk
        except Exception as exc:  # client left, camera busy, ffmpeg died
            logger.info("MJPEG stream for %s ended: %s", printer_id, exc)
        finally:
            # stop off the loop: terminate() may wait for the process to exit
            await loop.run_in_executor(None, camera_mod.stop_mjpeg_stream, printer_id, stream)

    return StreamingResponse(
        chunks(),
        media_type=f"multipart/x-mixed-replace; boundary={camera_mod.MJPEG_BOUNDARY}",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/printers/{printer_id}/camera/stream")
async def camera_stream(printer_id: str):
    printer = get_printer(printer_id)
    try:
        content_type, chunks = await run_blocking(camera_mod.open_stream, printer.ip)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Camera stream unavailable: {exc}")
    return StreamingResponse(
        chunks,
        media_type=content_type,
        headers={"Cache-Control": "no-store", "Connection": "close"},
    )


@app.get("/api/printers/{printer_id}/camera/url")
async def camera_url(printer_id: str):
    """The full RTSPS URL (contains the access code) - kept out of /api/printers."""
    printer = get_printer(printer_id)
    return {"url": camera_mod.rtsp_url(printer.ip, printer.access_code), "port": camera_mod.RTSP_PORT}


@app.get("/api/printers/{printer_id}/camera/sdp")
async def camera_sdp(printer_id: str):
    """H.264 parameters (SPS/PPS, codec) so the browser can start a decoder."""
    printer = get_printer(printer_id)
    try:
        info = await run_blocking(camera_mod.rtsp_sdp, printer.ip, printer.access_code)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Camera RTSP handshake failed: {exc}")
    return info


@app.get("/api/printers/{printer_id}/camera/h264")
async def camera_h264(printer_id: str):
    """Stream the printer's H.264 camera as an Annex-B byte stream.

    The browser decodes this with WebCodecs; see ``static/js/app.js``.
    """
    printer = get_printer(printer_id)

    def chunks():
        frames = camera_mod.rtsp_h264_stream(printer.ip, printer.access_code)
        try:
            for nal in frames:
                yield nal
        except Exception as exc:  # camera unplugged, session dropped, client left
            logger.info("Camera stream from %s ended: %s", printer_id, exc)
        finally:
            frames.close()

    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


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
                "printers": {
                    k: {**v, "homed": homed_state.get(k)} for k, v in latest_reports.items()
                },
            }
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_hub.disconnect(websocket)
    except Exception as exc:
        logger.debug("WebSocket closed: %s", exc)
        ws_hub.disconnect(websocket)
