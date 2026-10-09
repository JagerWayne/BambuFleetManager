from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class PrinterConfig(BaseModel):
    id: str
    name: str
    ip: str
    sn: str
    access_code: str
    bed_type: str = "textured_pei"
    ams_count: int = 1

    @field_validator("sn")
    @classmethod
    def _normalize_serial(cls, value: str) -> str:
        return value.strip().upper()


class ServerSettings(BaseModel):
    """Where the dashboard server listens. Applied on the next start."""

    host: str = "0.0.0.0"
    port: int = Field(8000, ge=1, le=65535)
    #: When true, machine-moving commands skip the per-action confirmation tick.
    #: Homing and idle-printer requirements still apply.
    allow_motion: bool = False
    #: Shared token guarding the API/dashboard. Empty means "no authentication".
    auth_token: str = ""
    #: Set true to remove the token (disable authentication).
    clear_auth: bool = False

    @field_validator("host")
    @classmethod
    def _non_empty_host(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("host must not be empty")
        return value


class PrinterPublic(BaseModel):
    """A printer as returned by the API - the access code is never included."""

    id: str
    name: str
    ip: str
    sn: str
    bed_type: str = "textured_pei"
    ams_count: int = 1
    has_access_code: bool = True


class UploadEntry(BaseModel):
    filename: str
    size: int
    modified: Optional[float] = None


class PrintDispatchCommand(BaseModel):
    printer_id: str
    filename: str
    plate_index: int = 1
    bed_levelling: bool = True
    flow_cali: bool = True
    vibration_cali: bool = True
    timelapse: bool = True
    use_ams: bool = True

    @field_validator("filename")
    @classmethod
    def _reject_path_traversal(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("filename must not be empty")
        if "/" in name or "\\" in name or ".." in name:
            raise ValueError("filename must be a bare file name")
        if not name.lower().endswith((".3mf", ".gcode")):
            raise ValueError("only .3mf / .gcode.3mf projects can be dispatched")
        return name


class SpeedCommand(BaseModel):
    # 1: Silent, 2: Standard, 3: Sport, 4: Ludicrous
    speed_level: int = Field(..., ge=1, le=4)


class LightCommand(BaseModel):
    # True: ON, False: OFF
    state: bool


class StagedFile(BaseModel):
    filename: str
    size: int


class DispatchResponse(BaseModel):
    status: str
    printer: Optional[str] = None
    job: Optional[str] = None


# ------------------------------------------------------------------ commands


class GenericCommand(BaseModel):
    """Escape hatch onto :mod:`backend.commands`, still allow-listed."""

    command: str
    params: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("command")
    @classmethod
    def _known_command(cls, value: str) -> str:
        from backend.commands import COMMANDS

        if value not in COMMANDS:
            raise ValueError(f"unknown command '{value}'")
        return value


class SpeedProfileCommand(BaseModel):
    level: int = Field(..., ge=1, le=4)
    # Optional second-stage confirm; firmware-dependent.
    confirm: bool = True


class LightModeCommand(BaseModel):
    node: Literal["chamber_light", "work_light", "nozzle_light", "combo_light"] = "chamber_light"
    mode: Literal["on", "off", "flashing"] = "on"
    # Firmware requires these fields or it answers "json wrong format".
    on_time: int = Field(500, ge=0, le=60000)
    off_time: int = Field(500, ge=0, le=60000)
    times: int = Field(0, ge=0, le=100)
    interval: int = Field(0, ge=0, le=60000)


class CalibrationCommand(BaseModel):
    """Machine-moving routines - these need explicit confirmation."""

    kind: Literal[
        "bed_leveling",
        "flow_calibration",
        "vibration_calibration",
    ]
    mode: int = Field(1, ge=0, le=2)
    confirm_motion: bool = False


class AmsActionCommand(BaseModel):
    action: Literal[
        "feed",
        "unload",
        "resume",
        "tray_select",
        "tray_info",
        "current_tray",
        "filament_setting",
        "auto",
    ]
    # 255 addresses the external spool (VT tray); 0-15 are AMS units.
    ams_id: int = Field(0, ge=0, le=255)
    tray_id: int = Field(0, ge=0, le=15)
    nozzle_temp: int = Field(220, ge=0, le=400)
    # filament_setting only
    tray_type: str = "PLA"
    color: str = "#00AE42"
    remaining: int = Field(100, ge=0, le=100)
    # Required for anything that heats the hotend or feeds filament.
    confirm_thermal: bool = False


class SkipObjectCommand(BaseModel):
    object_ids: List[int] = Field(default_factory=list, max_length=64)


class PrintOptionCommand(BaseModel):
    filament_id: Optional[str] = None
    filament_type: Optional[str] = None
    nozzle_diameter: Optional[str] = None
    nozzle_temp: Optional[int] = Field(None, ge=0, le=400)
    bed_type: Optional[str] = None


class FilamentSettingCommand(BaseModel):
    """Write a filament record into an AMS tray (type, colour, temps, remaining)."""

    ams_id: int = Field(0, ge=0, le=255)
    tray_id: int = Field(0, ge=0, le=15)
    tray_type: str = "PLA"
    color: str = "#00AE42"
    remaining: int = Field(100, ge=0, le=100)
    setting_id: str = ""          # blank -> the builder picks the system id for the material
    sub_brands: str = "Basic"
    temp_min: int = Field(190, ge=0, le=400)
    temp_max: int = Field(240, ge=0, le=400)


class TemperatureCommand(BaseModel):
    """Manual setpoints for the nozzle, bed and heated chamber."""

    nozzle: Optional[int] = Field(None, ge=0, le=300)
    bed: Optional[int] = Field(None, ge=0, le=130)
    chamber: Optional[int] = Field(None, ge=0, le=60)
    # Heats the machine: requires the same explicit opt-in as AMS actions.
    confirm_thermal: bool = False
    # Allow changing setpoints while a print is running (it will fight the job).
    allow_while_printing: bool = False

    @property
    def targets(self) -> Dict[str, int]:
        return {k: v for k, v in
                (("nozzle", self.nozzle), ("bed", self.bed), ("chamber", self.chamber))
                if v is not None}


class FanCommand(BaseModel):
    """Set one cooling fan's speed, as a percentage.

    Moves no axes and heats nothing, so there is no confirmation gate.
    """

    fan: Literal["part", "aux", "chamber"] = "part"
    speed: int = Field(ge=0, le=100)


class SendStagedCommand(BaseModel):
    """Send a file staged on this server to the printer's SD card."""

    filename: str = Field(min_length=1, max_length=255)
    dir_path: str = "/"


class JogCommand(BaseModel):
    """Relative move of one axis. Machine-moving - needs confirmation."""

    axis: Literal["X", "Y", "Z", "x", "y", "z"]
    #: Millimetres, signed. Capped so a slipped key can't send a huge move.
    distance: float = Field(..., ge=-100, le=100)
    feedrate: int = Field(3000, ge=60, le=20000)
    confirm_motion: bool = False


class HomeCommand(BaseModel):
    axes: Literal["", "x", "y", "z", "X", "Y", "Z"] = ""
    confirm_motion: bool = False


class ExtrudeCommand(BaseModel):
    """Relative extruder move - needs a hot nozzle and confirmation."""

    amount: float = Field(..., ge=-50, le=50)
    feedrate: int = Field(300, ge=30, le=2000)
    confirm_thermal: bool = False


class RebootCommand(BaseModel):
    module: Literal["esp32", "ota", "settings"] = "esp32"


class CameraStateCommand(BaseModel):
    state: bool


# --------------------------------------------------------------------- files


class RemotePath(BaseModel):
    """Base for anything that acts on a remote SD-card path."""

    path: str = "/"

    @field_validator("path")
    @classmethod
    def _clean(cls, value: str) -> str:
        from backend.ftp_client import normalize_remote_path

        return normalize_remote_path(value)


class DeleteEntryCommand(RemotePath):
    is_dir: bool = False


class RenameEntryCommand(RemotePath):
    """Rename a remote file. Only the basename is sent; the folder is derived
    server-side from ``path``."""

    new_name: str = Field(min_length=1, max_length=128)


class PrintRemoteCommand(RemotePath):
    """Start a print for a project already sitting on the SD card."""

    #: 0 = pick the plate that is actually inside the archive (recommended).
    plate_index: int = Field(0, ge=0)
    bed_levelling: bool = True
    flow_cali: bool = True
    vibration_cali: bool = True
    timelapse: bool = True
    use_ams: bool = True
    #: Filament slot -> tray, as flattened AMS indices (ams_id*4 + tray_id) or 255
    #: for the external spool. Omit to let the printer keep its current mapping.
    ams_mapping: Optional[List[int]] = None


class FileNode(BaseModel):
    name: str
    path: str
    is_dir: bool
    is_printable: bool
    size: int = 0
    modified: Optional[str] = None


class DirectoryListing(BaseModel):
    path: str
    parent: Optional[str]
    entries: List[FileNode]
