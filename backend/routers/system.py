"""Server settings, filaments, the command registry and fleet status.

Handlers only: shared helpers, config and the app itself live in
``backend.main`` (imported here as ``core`` so monkeypatched module values -
paths, the MQTT manager - resolve at call time, not import time).
"""
from fastapi import APIRouter
import secrets
from typing import Dict

from backend import main as core

router = APIRouter()


@router.post("/api/login")
async def do_login(cmd: Dict[str, str] = core.Body(default={})):
    token = core.auth_token()
    supplied = (cmd or {}).get("token", "")
    if not token or not secrets.compare_digest(supplied, token):
        raise core.HTTPException(status_code=401, detail="invalid token")
    response = core.JSONResponse({"status": "ok"})
    response.set_cookie(core.AUTH_COOKIE, supplied, httponly=True, samesite="lax")
    return response


@router.get("/api/settings")
async def get_settings(request: core.Request):
    """Server listen address (applied on next start) plus the port in use now."""
    settings = core.read_settings()
    return {
        "host": settings.get("host"),
        "port": settings.get("port"),
        "allow_motion": bool(settings.get("allow_motion")),
        "auth_required": bool(settings.get("auth_token")),
        "current_port": request.url.port,
        "current_host": request.url.hostname,
    }


@router.get("/api/filaments")
async def list_filaments():
    """Bambu material catalogue for the AMS tray editor.

    Ids are read from genuine Bambu spool RFID tags, so writing one makes the
    printer show the correct material instead of "?".
    """
    return {"categories": core.FILAMENT_CATEGORIES, "filaments": core.FILAMENTS}


@router.post("/api/settings")
async def update_settings(cmd: core.ServerSettings):
    current = core.read_settings()
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
    core.save_settings(settings)
    core.logger.info("Settings saved: host=%s port=%s allow_motion=%s auth=%s",
                     cmd.host, cmd.port, cmd.allow_motion, bool(token))
    return {"status": "saved", "restart_required": True, "auth_required": bool(token)}


@router.get("/api/commands")
async def list_commands():
    """Every command name the fleet API accepts, grouped by MQTT channel."""
    return {
        "commands": core.cmd_mod.available_commands(),
        "groups": core.cmd_mod.command_groups(),
        "speed_profiles": core.cmd_mod.SPEED_PROFILES,
        "risk": {name: core.cmd_mod.risk_class(name) for name in core.cmd_mod.available_commands()},
    }


@router.get("/api/status")
async def fleet_status():
    manager = core.require_manager()
    return {"mqtt": manager.status(), "printers": len(core.read_printers())}
