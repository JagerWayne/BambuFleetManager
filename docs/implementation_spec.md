<!-- HISTORICAL DOCUMENT
Original implementation spec, kept for reference only.
It predates the shipped product and has drifted - README.md and backend/main.py are the source of truth.
-->

# Architecture & Implementation Specification: Bambu Fleet Manager

## 1. Project Overview & Objective

**Bambu Fleet Manager** is a lightweight, self-hosted, on-premises management and monitoring solution designed to orchestrate any fleet of networked Bambu Lab 3D printers entirely over the Local Area Network (LAN). The system operates fully decoupled from cloud services, communicating directly with each printer via **MQTTS (Port 8883)** for telemetry and command control, and **FTPS (Implicit TLS, Port 990)** for sliced print job transfer (`.gcode.3mf`).

### Target Architecture

```
â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
â”‚                        Local Host Server                               â”‚
â”‚                                                                        â”‚
â”‚  â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”             â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”  â”‚
â”‚  â”‚ Single-Page Web UI    â”‚â—„â”€â”€WebSocketsâ”‚ FastAPI / Python 3.10+     â”‚  â”‚
â”‚  â”‚ (HTML5/Tailwind/JS)   â”‚â”€â”€â”€REST APIâ”€â–ºâ”‚ Async Engine               â”‚  â”‚
â”‚  â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜             â””â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”˜  â”‚
â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                                                â”‚ MQTTS (8883) â”‚ FTPS (990)
                         â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”´â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”´â”€â”€â”€â”€â”€â”€â”
                         â–¼                                            â–¼
           â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”                â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
           â”‚ Bambu Printer Node 01     â”‚                â”‚ Bambu Printer Node 02     â”‚
           â”‚ IP: 192.168.1.151         â”‚                â”‚ IP: 192.168.1.152         â”‚
           â”‚ Serial: 00M00A...         â”‚                â”‚ Serial: 00M00B...         â”‚
           â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜                â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
```

---

## 2. Git Version Control & Semantic Versioning Strategy

This project adheres to [Semantic Versioning (SemVer 2.0.0)](https://semver.org/): `MAJOR.MINOR.PATCH`.

* **MAJOR**: Breaking schema changes or protocol redesigns.
* **MINOR**: New hardware features, additional fleet controls, or backward-compatible API additions.
* **PATCH**: Bug fixes, security patches, and minor UI polishes.

### 2.1 Branching Strategy (Git Flow / Trunk-Based with Release Tags)

* `main`: Production-ready releases. Direct commits are restricted. Every merge requires passing tests and creates a Git tag (`vX.Y.Z`).
* `develop`: Integration branch for active feature merges.
* `feature/<feature-name>`: Scoped branches branched from `develop` and merged via PR.
* `hotfix/<fix-name>`: Critical fixes branched directly from `main` and merged to both `main` and `develop`.

### 2.2 Conventional Commits Standard

All commits must follow the [Conventional Commits](https://www.conventionalcommits.org/) convention:
* `feat(backend): add automatic multi-spool AMS fallback mapping`
* `fix(mqtt): handle TLS reconnection backoff on printer reboot`
* `docs(readme): add local setup and port forwarding guidelines`
* `refactor(ui): streamline drag-and-drop printer card hitboxes`
* `chore(release): bump version to v1.0.0`

### 2.3 Repository `.gitignore`

```gitignore
# Byte-compiled / optimized / DLL files
__pycache__/
*.py[cod]
*$py.class

# C extensions
*.so

# Distribution / packaging
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
share/python-wheels/
*.egg-info/
.installed.cfg
*.egg

# Virtual Environment
.venv/
env/
venv/
ENV/

# Local cache & uploaded staging binaries
uploads/*
!uploads/.gitkeep
config/printers.json

# Logs
*.log
npm-debug.log*
yarn-debug.log*

# Editor configurations
.vscode/
.idea/
*.swp
*.swo

# OS artifacts
.DS_Store
Thumbs.db
```

### 2.4 GitHub Actions Workflow (`.github/workflows/ci.yml`)

```yaml
name: Bambu Fleet Manager CI

on:
  push:
    branches: [ "main", "develop" ]
  pull_request:
    branches: [ "main" ]

jobs:
  lint-and-test:
    runs-on: ubuntu-latest
    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: "pip"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install flake8 pytest pytest-asyncio
          pip install -r requirements.txt

      - name: Lint with flake8
        run: |
          # stop the build if there are Python syntax errors or undefined names
          flake8 backend --count --select=E9,F63,F7,F82 --show-source --statistics
          # exit-zero treats all errors as warnings
          flake8 backend --count --exit-zero --max-complexity=10 --max-line-length=120 --statistics

      - name: Execute Backend Tests
        run: |
          pytest tests/ -v
```

---

## 3. Directory Layout

```
bambu-fleet-manager/
â”œâ”€â”€ .github/
â”‚   â””â”€â”€ workflows/
â”‚       â””â”€â”€ ci.yml              # CI automation workflow
â”œâ”€â”€ .gitignore                  # Git ignore rules
â”œâ”€â”€ .venv/                      # Python virtual environment (ignored in git)
â”œâ”€â”€ config/
â”‚   â””â”€â”€ printers.json.example   # Checked-in template config
â”œâ”€â”€ static/
â”‚   â”œâ”€â”€ css/
â”‚   â”‚   â””â”€â”€ tailwind.min.css    # Bundled CSS for offline setups
â”‚   â””â”€â”€ js/
â”‚       â””â”€â”€ app.js              # Frontend UI orchestration & WebSocket listener
â”œâ”€â”€ templates/
â”‚   â””â”€â”€ index.html              # Single-page dashboard interface
â”œâ”€â”€ tests/
â”‚   â”œâ”€â”€ __init__.py
â”‚   â”œâ”€â”€ test_models.py          # Data contract verification tests
â”‚   â””â”€â”€ test_mqtt_parser.py     # Telemetry parsing tests
â”œâ”€â”€ uploads/
â”‚   â””â”€â”€ .gitkeep                # Keeps uploads folder tracked
â”œâ”€â”€ backend/
â”‚   â”œâ”€â”€ __init__.py
â”‚   â”œâ”€â”€ main.py                 # FastAPI application, REST endpoints, WS hub
â”‚   â”œâ”€â”€ mqtt_manager.py         # Multi-client MQTTS connection and message pump
â”‚   â”œâ”€â”€ ftp_client.py           # Implicit TLS FTPS file transfer module
â”‚   â””â”€â”€ models.py               # Pydantic schemas and models
â”œâ”€â”€ requirements.txt            # Locked Python dependencies
â”œâ”€â”€ VERSION                     # Plaintext semantic version string (e.g., 1.0.0)
â””â”€â”€ README.md                   # Project documentation
```

---

## 4. Environment & Dependency Setup

### 4.1 Virtual Environment Initialization

```bash
git clone https://github.com/your-org/bambu-fleet-manager.git
cd bambu-fleet-manager

python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install --upgrade pip
```

### 4.2 `requirements.txt`

```text
fastapi>=0.110.0
uvicorn[standard]>=0.29.0
paho-mqtt>=1.6.1,<2.0.0
pydantic>=2.6.0
python-multipart>=0.0.9
aiofiles>=23.2.1
requests>=2.31.0
websockets>=12.0
```

Install packages inside `.venv`:

```bash
pip install -r requirements.txt
```

### 4.3 Version Tracking File (`VERSION`)

```text
1.0.0
```

---

## 5. Hardware Protocol Specifications

### 5.1 MQTTS Specification
* **Port**: `8883`
* **Transport**: TCP over SSL/TLS (`TLSv1.2`)
* **Certificate Validation**: Disabled (`ssl.CERT_NONE`, `check_hostname=False` to accommodate self-signed device certificates).
* **Credentials**:
  * Username: `bblp`
  * Password: The printer's 8-character **Access Code** (located on the machine display under Network settings).
* **Topics**:
  * Subscribe: `device/{serial_number}/report` (Machine state, telemetry, and AMS reports)
  * Publish: `device/{serial_number}/request` (Direct command and task execution)

### 5.2 FTPS Specification
* **Port**: `990`
* **Protocol**: Implicit FTPS (`FTP_TLS`)
* **Security Settings**: Immediate TLS handshake on initial socket connection, data channel protected via `prot_p()`.
* **Credentials**:
  * Username: `bblp`
  * Password: The printer's **Access Code**
* **Root Target**: `/` (SD card root).

---

## 6. Backend Source Code

### 6.1 `backend/models.py`

```python
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class PrinterConfig(BaseModel):
    id: str
    name: str
    ip: str
    sn: str
    access_code: str
    bed_type: str = "textured_pei"
    ams_count: int = 1

class PrintDispatchCommand(BaseModel):
    printer_id: str
    filename: str
    plate_index: int = 1
    bed_levelling: bool = True
    flow_cali: bool = True
    vibration_cali: bool = True
    timelapse: bool = True
    use_ams: bool = True

class SpeedCommand(BaseModel):
    speed_level: int = Field(..., ge=1, le=4)  # 1: Silent, 2: Standard, 3: Sport, 4: Ludicrous

class LightCommand(BaseModel):
    state: bool  # True: ON, False: OFF
```

### 6.2 `backend/ftp_client.py`

```python
import ssl
from ftplib import FTP_TLS
import os

class ImplicitFTP_TLS(FTP_TLS):
    """Subclass of FTP_TLS enforcing implicit TLS on port 990."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pasv_port = None

    def connect(self, host='', port=990, timeout=-999):
        self.host = host
        self.port = port
        self.sock = socket_create = super().connect(host=host, port=port, timeout=timeout)
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
        self.file = self.sock.makefile('r', encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

def upload_3mf_file(ip: str, access_code: str, file_path: str) -> bool:
    """Uploads sliced .3mf or .gcode.3mf directly to the printer SD card."""
    filename = os.path.basename(file_path)
    
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    ftps = ImplicitFTP_TLS(context=ctx)
    try:
        ftps.connect(host=ip, port=990, timeout=12)
        ftps.login(user="bblp", passwd=access_code)
        ftps.prot_p()  # Enforce encrypted data connection
        
        with open(file_path, "rb") as fp:
            ftps.storbinary(f"STOR /{filename}", fp)
            
        ftps.quit()
        return True
    except Exception as e:
        print(f"[FTPS ERROR] Failed to transfer {filename} to {ip}: {e}")
        try:
            ftps.close()
        except Exception:
            pass
        return False
```

### 6.3 `backend/mqtt_manager.py`

```python
import ssl
import json
import asyncio
from typing import Dict, Callable, Any
import paho.mqtt.client as mqtt

class BambuMqttClient:
    def __init__(self, printer_id: str, ip: str, sn: str, access_code: str, loop: asyncio.AbstractEventLoop, on_telemetry: Callable[[str, Dict[str, Any]], None]):
        self.printer_id = printer_id
        self.ip = ip
        self.sn = sn
        self.access_code = access_code
        self.loop = loop
        self.on_telemetry = on_telemetry
        self.client = mqtt.Client(client_id=f"bfm_{sn}")
        self.connected = False

    def start(self):
        self.client.username_pw_set("bblp", self.access_code)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.client.tls_set_context(ctx)
        self.client.tls_insecure_set(True)

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        try:
            self.client.connect_async(self.ip, 8883, keepalive=30)
            self.client.loop_start()
        except Exception as e:
            print(f"[MQTT] Connection setup failed for {self.sn} ({self.ip}): {e}")

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            client.subscribe(f"device/{self.sn}/report")
            # Request initial status dump
            self.send_command({"pushing": {"sequence_id": "0", "command": "start"}})
        else:
            print(f"[MQTT] Connection rejected for {self.sn}, return code: {rc}")

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            if "print" in payload:
                asyncio.run_coroutine_threadsafe(
                    self.on_telemetry(self.printer_id, payload["print"]),
                    self.loop
                )
        except Exception:
            pass

    def send_command(self, cmd_dict: Dict[str, Any]):
        topic = f"device/{self.sn}/request"
        self.client.publish(topic, json.dumps(cmd_dict))

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()


class FleetMqttManager:
    def __init__(self, loop: asyncio.AbstractEventLoop, on_telemetry: Callable[[str, Dict[str, Any]], None]):
        self.loop = loop
        self.on_telemetry = on_telemetry
        self.clients: Dict[str, BambuMqttClient] = {}

    def register_printer(self, p_id: str, ip: str, sn: str, access_code: str):
        if p_id in self.clients:
            self.clients[p_id].stop()
        client = BambuMqttClient(p_id, ip, sn, access_code, self.loop, self.on_telemetry)
        client.start()
        self.clients[p_id] = client

    def unregister_printer(self, p_id: str):
        if p_id in self.clients:
            self.clients[p_id].stop()
            del self.clients[p_id]

    def send_to_printer(self, p_id: str, payload: Dict[str, Any]):
        if p_id in self.clients:
            self.clients[p_id].send_command(payload)
```

### 6.4 `backend/main.py`

```python
import os
import json
import aiofiles
import asyncio
from typing import Dict, List
from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from backend.models import PrinterConfig, PrintDispatchCommand, SpeedCommand, LightCommand
from backend.mqtt_manager import FleetMqttManager
from backend.ftp_client import upload_3mf_file

CONFIG_PATH = os.path.join("config", "printers.json")
UPLOAD_DIR = "uploads"
os.makedirs("config", exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="Bambu Fleet Manager Local Server")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

class ConnectionHub:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)

ws_hub = ConnectionHub()
mqtt_manager: FleetMqttManager = None

async def telemetry_handler(printer_id: str, data: dict):
    msg = {
        "event": "telemetry",
        "printer_id": printer_id,
        "data": data
    }
    await ws_hub.broadcast(msg)

def read_printers() -> Dict[str, PrinterConfig]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        data = json.load(f)
        return {k: PrinterConfig(**v) for k, v in data.items()}

def save_printers(printers: Dict[str, PrinterConfig]):
    with open(CONFIG_PATH, "w") as f:
        json.dump({k: v.dict() for k, v in printers.items()}, f, indent=2)

@app.on_event("startup")
async def startup_event():
    global mqtt_manager
    loop = asyncio.get_running_loop()
    mqtt_manager = FleetMqttManager(loop, telemetry_handler)
    printers = read_printers()
    for p_id, p in printers.items():
        mqtt_manager.register_printer(p.id, p.ip, p.sn, p.access_code)

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/printers", response_model=List[PrinterConfig])
async def list_printers():
    return list(read_printers().values())

@app.post("/api/printers")
async def add_or_update_printer(printer: PrinterConfig):
    printers = read_printers()
    printers[printer.id] = printer
    save_printers(printers)
    mqtt_manager.register_printer(printer.id, printer.ip, printer.sn, printer.access_code)
    return {"status": "ok", "printer": printer}

@app.delete("/api/printers/{printer_id}")
async def remove_printer(printer_id: str):
    printers = read_printers()
    if printer_id in printers:
        del printers[printer_id]
        save_printers(printers)
        mqtt_manager.unregister_printer(printer_id)
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Printer node not found")

@app.post("/api/stage-upload")
async def stage_upload(file: UploadFile = File(...)):
    dest_path = os.path.join(UPLOAD_DIR, file.filename)
    async with aiofiles.open(dest_path, 'wb') as out_file:
        content = await file.read()
        await out_file.write(content)
    return {"filename": file.filename, "size": len(content)}

@app.post("/api/dispatch-print")
async def dispatch_print(cmd: PrintDispatchCommand):
    printers = read_printers()
    if cmd.printer_id not in printers:
        raise HTTPException(status_code=404, detail="Target printer node not found")
    
    printer = printers[cmd.printer_id]
    local_file = os.path.join(UPLOAD_DIR, cmd.filename)
    if not os.path.exists(local_file):
        raise HTTPException(status_code=400, detail="Target 3MF file not found in upload cache")

    # Step 1: Upload via FTPS
    loop = asyncio.get_running_loop()
    upload_success = await loop.run_in_executor(
        None, upload_3mf_file, printer.ip, printer.access_code, local_file
    )
    if not upload_success:
        raise HTTPException(status_code=502, detail="FTPS transfer failed")

    # Step 2: Issue MQTT project_file command
    mqtt_payload = {
        "print": {
            "sequence_id": "1",
            "command": "project_file",
            "param": f"Metadata/plate_{cmd.plate_index}.gcode",
            "subtask_name": cmd.filename,
            "url": f"file:///sdcard/{cmd.filename}",
            "bed_type": printer.bed_type,
            "bed_levelling": cmd.bed_levelling,
            "flow_cali": cmd.flow_cali,
            "vibration_cali": cmd.vibration_cali,
            "timelapse": cmd.timelapse,
            "use_ams": cmd.use_ams
        }
    }
    mqtt_manager.send_to_printer(printer.id, mqtt_payload)
    return {"status": "started", "printer": printer.name, "job": cmd.filename}

@app.post("/api/printers/{printer_id}/pause")
async def pause_printer(printer_id: str):
    mqtt_manager.send_to_printer(printer_id, {"print": {"sequence_id": "1", "command": "pause"}})
    return {"status": "sent"}

@app.post("/api/printers/{printer_id}/resume")
async def resume_printer(printer_id: str):
    mqtt_manager.send_to_printer(printer_id, {"print": {"sequence_id": "1", "command": "resume"}})
    return {"status": "sent"}

@app.post("/api/printers/{printer_id}/stop")
async def stop_printer(printer_id: str):
    mqtt_manager.send_to_printer(printer_id, {"print": {"sequence_id": "1", "command": "stop"}})
    return {"status": "sent"}

@app.post("/api/printers/{printer_id}/speed")
async def change_speed(printer_id: str, cmd: SpeedCommand):
    mqtt_manager.send_to_printer(printer_id, {
        "print": {"sequence_id": "1", "command": "print_speed", "param": str(cmd.speed_level)}
    })
    return {"status": "sent"}

@app.post("/api/printers/{printer_id}/light")
async def toggle_light(printer_id: str, cmd: LightCommand):
    mqtt_manager.send_to_printer(printer_id, {
        "system": {
            "sequence_id": "1",
            "command": "ledctrl",
            "led_node": "chamber_light",
            "led_mode": "on" if cmd.state else "off"
        }
    })
    return {"status": "sent"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_hub.disconnect(websocket)
```

---

## 7. Frontend Architecture & HTML UI

### 7.1 `templates/index.html`

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Bambu Fleet Manager</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <script>
    tailwind.config = {
      theme: {
        extend: {
          colors: {
            bambu: {
              DEFAULT: '#00AE42',
              hover: '#009237',
              dark: '#005822',
              subtle: 'rgba(0, 174, 66, 0.12)'
            },
            carbon: {
              950: '#090b0e',
              900: '#11151a',
              850: '#161c24',
              800: '#1f2733',
              750: '#273242',
              700: '#323f52'
            }
          },
          fontFamily: {
            sans: ['Inter', 'system-ui', 'sans-serif'],
            mono: ['JetBrains Mono', 'monospace']
          }
        }
      }
    }
  </script>
  <style>
    .drag-target-hover {
      outline: 2px dashed #00AE42 !important;
      outline-offset: -2px;
      background: rgba(0, 174, 66, 0.08) !important;
      transform: translateY(-2px);
    }
  </style>
</head>
<body class="bg-carbon-950 text-slate-200 font-sans min-h-screen flex flex-col selection:bg-bambu selection:text-black">

  <!-- Header -->
  <header class="bg-carbon-900 border-b border-carbon-800 sticky top-0 z-40 px-4 sm:px-6 py-3">
    <div class="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-4">
      <div class="flex items-center space-x-3">
        <div class="w-10 h-10 rounded-xl bg-bambu/20 border border-bambu/50 flex items-center justify-center text-bambu font-black text-xl">
          <svg class="w-6 h-6 fill-current" viewBox="0 0 24 24"><path d="M19 8l-7-4-7 4v8l7 4 7-4V8zm-7 2.15L16.7 8 12 5.3 7.3 8 12 10.15zm-5 1.73l4 2.29v4.54l-4-2.29v-4.54zm6 6.83v-4.54l4-2.29v4.54l-4 2.29z"/></svg>
        </div>
        <div>
          <h1 class="font-bold text-lg text-white tracking-tight">BAMBU <span class="text-bambu font-extrabold">FLEET MANAGER</span></h1>
          <p class="text-xs text-slate-400 font-mono">Local Network Server Â· MQTTS 8883 Â· FTPS 990</p>
        </div>
      </div>

      <!-- Stats Bar -->
      <div class="flex items-center gap-3 bg-carbon-850 px-3.5 py-1.5 rounded-xl border border-carbon-800 text-xs font-mono">
        <div><span class="text-[10px] text-slate-400 block">TOTAL NODES</span><span id="stat-total" class="font-bold text-white text-sm">0</span></div>
        <div class="h-6 w-px bg-carbon-700"></div>
        <div><span class="text-[10px] text-slate-400 block">PRINTING</span><span id="stat-printing" class="font-bold text-bambu text-sm">0</span></div>
        <div class="h-6 w-px bg-carbon-700"></div>
        <div><span class="text-[10px] text-slate-400 block">IDLE</span><span id="stat-idle" class="font-bold text-sky-400 text-sm">0</span></div>
      </div>

      <!-- Actions -->
      <div class="flex items-center space-x-2">
        <button onclick="openPrinterModal()" class="px-3.5 py-1.5 bg-bambu hover:bg-bambu-hover text-black text-xs font-bold rounded-lg transition active:scale-95 flex items-center gap-1.5">
          <svg class="w-4 h-4 fill-current" viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>
          Add Printer Node
        </button>
      </div>
    </div>
  </header>

  <!-- Main Content -->
  <main class="max-w-7xl mx-auto w-full p-4 sm:p-6 space-y-6 flex-1">
    
    <!-- Sliced Job Staging -->
    <section class="bg-carbon-900 border border-carbon-800 rounded-2xl p-4 sm:p-5 shadow-xl">
      <div class="flex items-center justify-between mb-3">
        <div>
          <h2 class="text-xs font-bold uppercase tracking-wider text-white">Sliced Jobs Staging Queue</h2>
          <p class="text-xs text-slate-400">Drag files here to stage, then drop a staged card onto any idle printer.</p>
        </div>
      </div>
      <div class="grid grid-cols-1 lg:grid-cols-4 gap-4">
        <div id="drop-zone" 
             ondragover="handleDragOver(event)" 
             ondragleave="handleDragLeave(event)" 
             ondrop="handleDrop(event)"
             onclick="document.getElementById('file-input').click()"
             class="border-2 border-dashed border-carbon-700 hover:border-bambu bg-carbon-850/60 rounded-xl p-4 flex flex-col items-center justify-center text-center cursor-pointer min-h-[110px]">
          <input type="file" id="file-input" class="hidden" accept=".3mf,.gcode" multiple onchange="handleFileSelect(event)">
          <svg class="w-6 h-6 text-slate-400 mb-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"/></svg>
          <span class="text-xs font-bold text-slate-200">Drop .3mf or .gcode.3mf</span>
        </div>
        <div id="staged-container" class="lg:col-span-3 grid grid-cols-1 sm:grid-cols-3 gap-3 overflow-y-auto max-h-[120px]"></div>
      </div>
    </section>

    <!-- Fleet Grid -->
    <div id="fleet-grid" class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5"></div>
  </main>

  <!-- Modal: Add Printer -->
  <div id="modal-printer" class="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4">
    <div class="bg-carbon-900 border border-carbon-700 rounded-2xl max-w-md w-full p-6 shadow-2xl">
      <h3 class="text-base font-bold text-white mb-3">Add Printer Node</h3>
      <form id="printer-form" onsubmit="savePrinter(event)" class="space-y-3 text-xs">
        <input type="hidden" id="form-id">
        <div>
          <label class="block text-slate-300 font-medium mb-1">Friendly Name</label>
          <input type="text" id="form-name" required placeholder="e.g. Printer-Bay-1" class="w-full bg-carbon-850 border border-carbon-700 rounded-lg p-2 text-white font-mono">
        </div>
        <div>
          <label class="block text-slate-300 font-medium mb-1">IP Address</label>
          <input type="text" id="form-ip" required placeholder="192.168.1.150" class="w-full bg-carbon-850 border border-carbon-700 rounded-lg p-2 text-white font-mono">
        </div>
        <div>
          <label class="block text-slate-300 font-medium mb-1">Serial Number (SN)</label>
          <input type="text" id="form-sn" required placeholder="00M00A..." class="w-full bg-carbon-850 border border-carbon-700 rounded-lg p-2 text-white font-mono uppercase">
        </div>
        <div>
          <label class="block text-slate-300 font-medium mb-1">Access Code</label>
          <input type="text" id="form-code" required placeholder="8-digit code" class="w-full bg-carbon-850 border border-carbon-700 rounded-lg p-2 text-white font-mono">
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick="closePrinterModal()" class="px-4 py-2 bg-carbon-800 rounded-lg text-slate-300">Cancel</button>
          <button type="submit" class="px-5 py-2 bg-bambu text-black font-bold rounded-lg">Save</button>
        </div>
      </form>
    </div>
  </div>

  <script src="/static/js/app.js"></script>
</body>
</html>
```

### 7.2 `static/js/app.js`

```javascript
let fleet = [];
let stagedFiles = [];
let activeDragFile = null;
let socket = null;

// WebSocket Telemetry Connection
function initWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  socket = new WebSocket(`${protocol}//${window.location.host}/ws`);

  socket.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.event === 'telemetry') {
      updatePrinterTelemetry(payload.printer_id, payload.data);
    }
  };

  socket.onclose = () => {
    setTimeout(initWebSocket, 3000);
  };
}

async function loadFleet() {
  const res = await fetch('/api/printers');
  if (res.ok) {
    const data = await res.json();
    fleet = data.map(p => ({
      ...p,
      status: 'idle',
      job: 'None',
      progress: 0,
      remainingSec: 0,
      nozzleTemp: 0,
      nozzleTarget: 0,
      bedTemp: 0,
      bedTarget: 0,
      light: false
    }));
    renderFleet();
  }
}

function updatePrinterTelemetry(printerId, telemetry) {
  const printer = fleet.find(p => p.id === printerId);
  if (!printer) return;

  if (telemetry.gcode_state) printer.status = telemetry.gcode_state.toLowerCase();
  if (telemetry.mc_percent !== undefined) printer.progress = telemetry.mc_percent;
  if (telemetry.mc_remaining_time !== undefined) printer.remainingSec = telemetry.mc_remaining_time * 60;
  if (telemetry.nozzle_temper !== undefined) printer.nozzleTemp = Math.round(telemetry.nozzle_temper);
  if (telemetry.nozzle_target_temper !== undefined) printer.nozzleTarget = Math.round(telemetry.nozzle_target_temper);
  if (telemetry.bed_temper !== undefined) printer.bedTemp = Math.round(telemetry.bed_temper);
  if (telemetry.bed_target_temper !== undefined) printer.bedTarget = Math.round(telemetry.bed_target_temper);
  if (telemetry.subtask_name) printer.job = telemetry.subtask_name;

  renderFleet();
}

function renderFleet() {
  const grid = document.getElementById('fleet-grid');
  document.getElementById('stat-total').innerText = fleet.length;
  document.getElementById('stat-printing').innerText = fleet.filter(p => p.status === 'running').length;
  document.getElementById('stat-idle').innerText = fleet.filter(p => p.status === 'idle').length;

  grid.innerHTML = fleet.map(p => {
    const isPrinting = p.status === 'running';
    return `
      <div id="card-${p.id}" 
           ondragover="handleCardDragOver(event)" 
           ondragleave="handleCardDragLeave(event)" 
           ondrop="handleCardDrop(event, '${p.id}')"
           class="bg-carbon-900 border border-carbon-800 rounded-2xl p-4 shadow-xl flex flex-col justify-between">
        <div>
          <div class="flex items-center justify-between pb-2 border-b border-carbon-800">
            <div>
              <div class="flex items-center gap-2">
                <span class="w-2.5 h-2.5 rounded-full ${isPrinting ? 'bg-bambu animate-pulse' : 'bg-sky-400'}"></span>
                <h3 class="font-bold text-white text-base">${p.name}</h3>
                <span class="text-[10px] font-mono px-2 py-0.5 rounded uppercase ${isPrinting ? 'bg-emerald-950 text-emerald-400' : 'bg-carbon-800 text-slate-400'}">${p.status}</span>
              </div>
              <div class="text-[11px] font-mono text-slate-400">${p.ip} Â· ${p.sn}</div>
            </div>
            <button onclick="toggleLight('${p.id}', ${!p.light})" class="p-1.5 rounded-lg bg-carbon-800 border border-carbon-700 text-slate-400 hover:text-white">
              ðŸ’¡
            </button>
          </div>

          <div class="grid grid-cols-2 gap-2 my-3 text-center font-mono">
            <div class="bg-carbon-850 p-2 rounded-lg">
              <span class="text-[9px] text-slate-400 block">NOZZLE</span>
              <span class="text-xs font-bold ${p.nozzleTemp > 45 ? 'text-emerald-400' : 'text-slate-300'}">${p.nozzleTemp}/${p.nozzleTarget}Â°C</span>
            </div>
            <div class="bg-carbon-850 p-2 rounded-lg">
              <span class="text-[9px] text-slate-400 block">BED</span>
              <span class="text-xs font-bold ${p.bedTemp > 40 ? 'text-amber-400' : 'text-slate-300'}">${p.bedTemp}/${p.bedTarget}Â°C</span>
            </div>
          </div>

          <div class="bg-carbon-850 p-2.5 rounded-xl border border-carbon-800 mb-3">
            <div class="flex justify-between text-xs mb-1 font-mono">
              <span class="truncate max-w-[150px] text-slate-300">${p.job}</span>
              <span class="text-bambu font-bold">${p.progress}%</span>
            </div>
            <div class="w-full bg-carbon-800 rounded-full h-2 overflow-hidden">
              <div class="bg-bambu h-2 rounded-full transition-all" style="width: ${p.progress}%"></div>
            </div>
          </div>
        </div>

        <div class="pt-2 border-t border-carbon-800 flex items-center justify-between">
          <div class="flex gap-1.5">
            ${isPrinting ? `
              <button onclick="controlPrinter('${p.id}', 'pause')" class="px-3 py-1 bg-amber-500/20 text-amber-300 text-xs rounded font-bold">Pause</button>
              <button onclick="controlPrinter('${p.id}', 'stop')" class="px-3 py-1 bg-red-500/20 text-red-300 text-xs rounded font-bold">Stop</button>
            ` : `
              <button onclick="controlPrinter('${p.id}', 'resume')" class="px-3 py-1 bg-bambu/20 text-bambu text-xs rounded font-bold">Resume</button>
            `}
          </div>
          <div class="flex gap-1 text-[10px] font-mono">
            <button onclick="setSpeed('${p.id}', 1)" class="px-1.5 py-0.5 bg-carbon-800 rounded">Silent</button>
            <button onclick="setSpeed('${p.id}', 2)" class="px-1.5 py-0.5 bg-carbon-800 rounded">Std</button>
            <button onclick="setSpeed('${p.id}', 3)" class="px-1.5 py-0.5 bg-carbon-800 rounded">Sport</button>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

// Drag & Drop File Handlers
function handleDragOver(e) { e.preventDefault(); }
function handleDragLeave(e) { e.preventDefault(); }
function handleDrop(e) {
  e.preventDefault();
  const files = e.dataTransfer.files;
  if (files.length > 0) uploadFile(files[0]);
}

function handleFileSelect(e) {
  const files = e.target.files;
  if (files.length > 0) uploadFile(files[0]);
}

async function uploadFile(file) {
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch('/api/stage-upload', { method: 'POST', body: formData });
  if (res.ok) {
    const data = await res.json();
    stagedFiles.push(data.filename);
    renderStaged();
  }
}

function renderStaged() {
  const container = document.getElementById('staged-container');
  container.innerHTML = stagedFiles.map((fn, index) => `
    <div draggable="true" ondragstart="handleJobDragStart(event, '${fn}')" class="bg-carbon-850 p-2.5 rounded-xl border border-carbon-700 cursor-grab active:cursor-grabbing flex items-center justify-between text-xs">
      <span class="truncate font-mono text-white">${fn}</span>
      <span class="text-[10px] text-bambu font-bold">DRAG âž”</span>
    </div>
  `).join('');
}

function handleJobDragStart(e, filename) {
  activeDragFile = filename;
  e.dataTransfer.setData('text/plain', filename);
}

function handleCardDragOver(e) { e.preventDefault(); e.currentTarget.classList.add('drag-target-hover'); }
function handleCardDragLeave(e) { e.currentTarget.classList.remove('drag-target-hover'); }
async function handleCardDrop(e, printerId) {
  e.preventDefault();
  e.currentTarget.classList.remove('drag-target-hover');
  if (!activeDragFile) return;

  const payload = {
    printer_id: printerId,
    filename: activeDragFile,
    plate_index: 1,
    bed_levelling: true,
    flow_cali: true,
    vibration_cali: true,
    timelapse: true,
    use_ams: true
  };

  alert(`Dispatching ${activeDragFile} to printer...`);
  await fetch('/api/dispatch-print', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}

// Hardware API Triggers
async function controlPrinter(id, action) {
  await fetch(`/api/printers/${id}/${action}`, { method: 'POST' });
}

async function setSpeed(id, speedLevel) {
  await fetch(`/api/printers/${id}/speed`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ speed_level: speedLevel })
  });
}

async function toggleLight(id, state) {
  await fetch(`/api/printers/${id}/light`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state: state })
  });
}

function openPrinterModal() { document.getElementById('modal-printer').classList.remove('hidden'); }
function closePrinterModal() { document.getElementById('modal-printer').classList.add('hidden'); }

async function savePrinter(e) {
  e.preventDefault();
  const printer = {
    id: 'node_' + Math.random().toString(36).substring(5),
    name: document.getElementById('form-name').value,
    ip: document.getElementById('form-ip').value,
    sn: document.getElementById('form-sn').value,
    access_code: document.getElementById('form-code').value,
    bed_type: 'textured_pei',
    ams_count: 1
  };
  await fetch('/api/printers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(printer)
  });
  closePrinterModal();
  loadFleet();
}

window.addEventListener('DOMContentLoaded', () => {
  initWebSocket();
  loadFleet();
});
```

---

## 8. Unit Testing Plan (`tests/test_models.py`)

```python
import pytest
from backend.models import PrinterConfig, PrintDispatchCommand, SpeedCommand

def test_printer_config_validation():
    p = PrinterConfig(
        id="node_01",
        name="Print Farm Node A",
        ip="192.168.1.50",
        sn="00M00A123456789",
        access_code="12345678"
    )
    assert p.bed_type == "textured_pei"
    assert p.ams_count == 1

def test_speed_command_boundaries():
    valid = SpeedCommand(speed_level=3)
    assert valid.speed_level == 3

    with pytest.raises(Exception):
        SpeedCommand(speed_level=5)  # Max allowed is 4
```

Execute tests inside the virtual environment:

```bash
pytest tests/
```

---

## 9. Launch & Operational Runbook

1. **Activate Virtual Environment**:
   ```bash
   source .venv/bin/activate
   ```
2. **Start Backend Server**:
   ```bash
   uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
   ```
3. **Open Client**:
   Navigate to `http://localhost:8000` from any browser on the local subnet.
