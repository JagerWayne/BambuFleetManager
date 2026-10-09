# Bambu Fleet Manager

A lightweight, **self-hosted, fully on-premises** dashboard for a fleet of networked
Bambu Lab 3D printers. It talks to every printer directly over the LAN — no cloud
account, no vendor relay, no internet connection required.

| Channel | Protocol | Port | Purpose |
|---|---|---|---|
| Telemetry & control | MQTTS | `8883` | Live status, job control, speed, lighting, AMS, calibration, jog |
| Job transfer | Implicit FTPS | `990` | Browse the SD card, upload and start prints, fetch previews |
| Camera | RTSPS | `322` | Live view (transcoded to MJPEG) and snapshots |

```
Browser ──REST/WS──► FastAPI ──MQTTS 8883──► printers
                        │──────FTPS  990──► SD cards / uploads
                        └──────RTSPS 322──► cameras
```

## Features

- **Focused dashboard.** One printer is shown at a time, chosen from the selector bar at
  the top (which lists every node with its live status and is remembered per browser). The
  selected printer gets the full page: an always-visible camera, temperatures, job progress
  and a tab strip for controls, jog, temperatures, files, AMS, system and staging.
- **Light & dark themes**, remembered per browser; the choice follows the OS until changed.
- **Responsive / touch friendly.** One page per printer on both phones and desktops; on a
  phone the actions become a fixed bottom navigation bar in the thumb zone.
- **Full job control:** pause / resume / stop / recover, speed profiles (Silent → Ludicrous),
  chamber + work lighting.
- **Skip object,** with the printer's own object list: see which object is printing, which is
  next, and which are already done, then skip the current or next one by name.
- **Print preview dialog.** Pick the plate, preview the render, read time & filament weight,
  and map each filament to an AMS tray or the external spool (auto-mapped by colour).
- **AMS management.** Per-unit trays with real colour swatches, remaining bar, humidity,
  the loaded tray highlighted, and **edit** to set a tray's material / colour / remaining.
- **SD-card browser.** Browse folders, upload into the current folder, download, delete, and
  print a stored project. Sort by name/date/size in either direction, filter to print files
  (3MF) or video files (MP4), search the folder, and rename a `.3mf` in place. Folders are
  open-only, the file being printed is highlighted, and rename/delete are blocked mid-job.
- **Staging queue.** Drop a sliced project in the *Staging* tab, then drag it onto the card
  to start it on the selected printer.
- **Manual temperatures, homing, jog (X/Y/Z), extruder** — all safety-gated.
- **Fan control.** Sliders for the part-cooling, auxiliary and chamber fans (0-100%), each shown
  next to the speed the printer reports back, plus Off/50/100 presets.
- **Live telemetry** over WebSockets with auto-reconnect.
- **Optional access token** to protect the dashboard and API.
- **Windows tray app.** A single `BambuFleetManager.exe` bundle that runs the server in
  the background with a tray menu: open the dashboard, start / stop / restart, toggle
  *Start with Windows*, open the data folder and quit.
- **In-app updates.** *Settings → Updates* checks GitHub Releases, downloads the installer,
  and runs it when you are ready (or saves a copy to run yourself).

## Requirements

- Python 3.10+ (verified on 3.11 and 3.14).
- Printers in **LAN-only mode** with MQTT/FTP binding enabled
  (*Settings → Network* on the printer display).
- ffmpeg is bundled via `imageio-ffmpeg` — nothing to install.

## Install (Windows)

Grab `BambuFleetManagerSetup.exe` from the [latest release](../../releases/latest) and run
it. It installs **per-user** (no admin) to `%LOCALAPPDATA%\Programs\BambuFleetManager`,
adds Start Menu/desktop shortcuts and launches the tray app. No Python needed.

The tray menu lets you open the dashboard, start/stop/restart the server, toggle
*Start with Windows*, open the data folder, or quit. Configuration and uploads live in
`%LOCALAPPDATA%\BambuFleetManager` (override with `BFM_DATA_DIR`).

## Setup

```bash
git clone <your-repo-url>
cd BambuFleetManager

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Optionally seed the fleet from the template:

```bash
cp config/printers.json.example config/printers.json   # Windows: copy
```

## Run

### Windows

```bat
run.bat                          :: serve on the configured host/port
run.bat --reload                 :: auto-reload on source changes
run.bat --host 0.0.0.0 --port 8000
```

`run.bat` creates `.venv` if needed, installs dependencies, then launches the server.
Host/port default to `config/settings.json` (editable from the dashboard's ⚙ menu);
command-line flags override them.

### Windows tray app

```bat
python -m tray.bambu_tray       :: background server + tray icon
python -m tray.bambu_tray --selftest   :: headless start/HTTP/shutdown check
```

The tray app runs uvicorn in-process and exposes a menu to open the dashboard,
start/stop/restart, toggle *Start with Windows* (an `HKCU\...\Run` entry) and quit.
Installing the release build from *Settings → Updates* restarts it automatically.

### Linux / macOS

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Open `http://<host-ip>:8000` from any browser on the same subnet.

### Registering a printer

| Field | Where to find it |
|---|---|
| IP address | Printer → *Settings → Network* |
| Serial number | Printer → *Settings → Device info* |
| Access code | Printer → *Settings → Network → Access Code* |

Usernames (`bblp`) and ports are fixed by the printer firmware.

## Configuration

- `config/printers.json` — the fleet (git-ignored; `printers.json.example` is tracked).
- `config/settings.json` — listen host/port, the motion-unlock flag and the optional token.
- `uploads/` — staged projects (pruned after 14 days).

When run from source these live in the repo; the installed tray app keeps them in
`%LOCALAPPDATA%\BambuFleetManager` (set `BFM_DATA_DIR` to override). The `.example`
templates are copied there on first start.

## Safety

| Class | Commands | Behaviour |
|---|---|---|
| `safe` | lighting, speed, state dump, camera | sent immediately |
| `job` | pause / resume / stop / skip / recover / clear error | affects the running job |
| `thermal` | AMS feed / unload, filament, setpoints | needs `confirm_thermal=true` |
| `motion` | bed levelling, flow/vibration calibration, jog | needs `confirm_motion=true` **and** all axes homed **and** an idle printer |
| `home` | home all / X / Y / Z | needs confirmation; allowed while unhomed |

Calibration homes all axes automatically first. Axis self-test and bridge-test are **not
implemented** — they slam the axes at full speed. An optional **Unlock machine moves**
setting skips the confirmation ticks while still enforcing homing and the idle check.

## API (summary)

| Method | Path | Purpose |
|---|---|---|
| `GET`/`POST`/`DELETE` | `/api/printers[/{id}]` | list / add-update / remove nodes |
| `GET` | `/api/printers/{id}/telemetry` | last telemetry report + homing state |
| `POST` | `/api/printers/{id}/{pause\|resume\|stop\|retry\|refresh}` | job control |
| `POST` | `/api/printers/{id}/speed` | `{"speed_level": 1..4}` |
| `POST` | `/api/printers/{id}/light-mode` | `{node, mode}` |
| `POST` | `/api/printers/{id}/temperature` | `{nozzle, bed, confirm_thermal}` |
| `POST` | `/api/printers/{id}/fan` | `{fan: part\|aux\|chamber, speed: 0-100}` |
| `POST` | `/api/printers/{id}/{home\|jog\|extrude}` | safe motion |
| `POST` | `/api/printers/{id}/ams` | AMS feed / unload / select / info |
| `POST` | `/api/printers/{id}/filament` | write a tray's material/colour/remaining |
| `GET` | `/api/filaments` | Bambu filament catalog (id, type, temps) |
| `GET` | `/api/printers/{id}/files?path=/` | SD listing |
| `GET` | `/api/printers/{id}/files/plan` | plates, time, weight, filaments |
| `GET` | `/api/printers/{id}/files/plate` | plate thumbnail (PNG) |
| `POST` | `/api/printers/{id}/print-remote` | start an SD project (with `ams_mapping`) |
| `GET` | `/api/printers/{id}/camera/mjpeg` | live view (`<img>`) |
| `GET` | `/api/printers/{id}/camera/snapshot` | one JPEG |
| `GET`/`DELETE` | `/api/uploads[/{name}]` | staged files |
| `GET`/`POST` | `/api/settings` | server settings |
| `GET` | `/api/update` | check GitHub for a newer release |
| `POST` | `/api/update/install` | download + silently install the update |
| `WS` | `/ws` | telemetry stream |

Full details: run the server and open `/docs`.

## Camera

Live view is transcoded to MJPEG with ffmpeg so it plays in any browser; snapshots come
from the same RTSPS stream. The complete protocol write-up (RTSP discovery, Digest auth,
RTP depacketising, troubleshooting) is in **[docs/CAMERA.md](docs/CAMERA.md)**.

## Development

```bash
pip install -r requirements-dev.txt
pytest tests/ -v                 # backend
npm install && npm run test:ui   # UI logic (jsdom)
flake8 backend tray --count --exit-zero --max-complexity=10 --max-line-length=120 --statistics
```

Rebuild the vendored CSS after editing the markup or JS:

```bash
npm install && npm run build:css
```

Build the tray app and Windows installer (PyInstaller + Inno Setup):

```bash
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Release   # + GitHub release
```

This produces `dist\BambuFleetManager\` (bundle) and `dist\BambuFleetManagerSetup.exe`
(installer). `-Release` tags `v<version>`, pushes the tag and uploads the installer to a
GitHub Release, which is what the in-app updater consumes.

## Versioning

[Semantic Versioning](https://semver.org/) tracked in `VERSION`. Branches: `main`
(released, tagged) and feature branches merged via PR. Commit messages follow
[Conventional Commits](https://www.conventionalcommits.org/).

## Security

- **Access token (optional).** Set one under ⚙; the dashboard then requires sign-in and
  the API/WebSocket reject unauthenticated calls. Leave blank for a fully trusted LAN.
- Printer **access codes are never returned by the API**; the RTSPS URL (which embeds one)
  comes from an authenticated endpoint.
- TLS certificate verification is disabled for MQTTS/FTPS/RTSPS because the printers
  present self-signed certificates — traffic is encrypted, the peer is not authenticated.
- Put the server behind a TLS-terminating reverse proxy before exposing it beyond the LAN.
