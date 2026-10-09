# AGENTS.md

Self-hosted FastAPI dashboard for Bambu Lab printers (MQTTS 8883 / FTPS 990 / RTSPS 322).
README.md is the user-facing doc; this file is the working notes.

## Commands (run from the repo root)

Python 3.10+; the venv lives at `.venv` and is what everything expects.

```bat
.venv\Scripts\python -m pytest tests/ -q          :: 222 tests, ~6s
.venv\Scripts\python -m pytest tests/test_api.py::test_dashboard_renders -q
npm install --no-audit --no-fund
npm run test:ui                                   :: jsdom UI logic tests
npm run build:css                                 :: rebuild static/css/tailwind.min.css
.venv\Scripts\python -m flake8 backend tray --count --select=E9,F63,F7,F82 --show-source --statistics
.venv\Scripts\python -m flake8 backend tray --count --max-complexity=12 --max-line-length=120 --statistics
uv pip compile --python-version 3.11 requirements.txt -o requirements.lock   :: after editing requirements.txt
run.bat --dev --reload                            :: serve (creates .venv, pip installs)
.venv\Scripts\python -m tray.bambu_tray --selftest
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 [-Release]
```

- There is **no** `pyproject.toml` / `setup.cfg` / `.flake8` / `pytest.ini`. Bare `flake8`
  and `pytest` use defaults, which is *not* what CI runs — pass the flags above.
- CI (`.github/workflows/ci.yml`) is two jobs: `lint` (flake8, both passes **blocking**;
  max-complexity is ratcheted at the current worst offender, currently 12) then `test`
  (matrix: Python 3.11 + 3.13 on ubuntu, 3.13 on windows) running `pytest tests/ -v` →
  `npm run test:ui` → assert `static/css/tailwind.min.css` is non-empty.
- `requirements.lock` (uv-compiled, pinned for Python 3.11) is what CI and
  `packaging/build.ps1` install; regenerate it whenever `requirements.txt` changes.
  Runtime is paho-mqtt **2.x** (`CallbackAPIVersion.VERSION2` callbacks in
  `mqtt_manager.py`).
- `README.md`'s API table is partial and drifts; the `@app.*` decorators in
  `backend/main.py` are the source of truth (also served at `/docs`).

## Frontend: no bundler, vendored Tailwind

- `static/js/app.js` (~3300 lines) and `templates/index.html` are served **raw**. There is
  no npm build for the app itself, no framework, no ES modules. Edit them directly.
  Pure helpers (`escapeHtml`, `baseName`, `formatDuration`, `formatBytes`, `formatDate`,
  `speedLabel`, `printableJob`) live in `static/js/lib/util.js`, loaded by a plain
  `<script>` tag **before** app.js; they use `var` (not `const`) so jsdom's `window.eval`
  exposes them.
- `app.js` must stay a **non-strict, top-level script** (no IIFE, no `"use strict"`, no
  imports): `tests/js/dom_*_test.js` does `window.eval(appJs)` and then calls top-level
  functions as globals (`window.initTheme()`, `window.controlHtml()`). Wrapping it breaks the
  UI tests. Note the inverse: `const state` / `const cards` are **not** reachable from a test
  (indirect eval keeps `const` in its own scope), so tests must drive the public functions
  and assert on the DOM.
- **One printer is mounted at a time.** `renderFleet()` resolves `state.selectedId` (persisted
  in `localStorage` as `bfm-printer`), renders only that card, and fills the
  `<select id="printer-select">` in the printer bar. Cards for other ids are `destroy()`ed, so
  a stray second `[data-card-id]` in the DOM means the selection logic regressed.
- The card's controls are **always shown** (`entry.tab` defaults to `'control'` and never goes
  null); there is no collapse toggle. Node removal lives in the System tab behind a
  three-number challenge (`removeChallengeHtml`). That gate is an anti-misclick guard, not a
  security control - the delete endpoint is unauthenticated like the rest when no token is set.
- The file browser's sort/filter live in a module-level `const fileView` (persisted as
  `bfm-files-*`), so jsdom tests must drive them through the DOM (a bubbling `change`/`input`
  event on the controls) rather than reaching for the object. Per-row actions are in a
  `<details class="file-menu">`; `renderFiles`'s `sig` includes the view state, the printer
  status and the printing file, so those changes re-render on the next telemetry tick.
- Component CSS lives in **`@layer components`** in `assets/tailwind.src.css`. This is load
  bearing: unlayered CSS beats every Tailwind layer, so an unlayered `.chip { display: … }`
  silently overrides the `hidden` utility. Keep new component classes inside that layer.
- Tailwind is pre-built offline. `assets/tailwind.src.css` only scans
  `templates/index.html` and `static/js/app.js` via `@source` — that includes the HTML
  strings generated inside `app.js`. **Any new utility class requires
  `npm run build:css`**, and the regenerated `static/css/tailwind.min.css` is a tracked
  build artifact that CI checks for.
- The jsdom tests stub `fetch`, `WebSocket`, `IntersectionObserver`, `matchMedia` and
  `requestAnimationFrame` before eval; telemetry arrives several times/sec so cards are
  created once and mutated through cached refs — don't re-serialise card markup from the
  live feed. The IntersectionObserver pauses a card's camera when it scrolls away, which is
  why fullscreen needs the `isCamFullscreen()` guard (see `dom_camera_test.js`).

## Backend layout

- `backend/main.py` is the **composition root and the shared core**: config load/save, the
  auth + security-header middleware, the `ConnectionHub` WebSocket fan-out, `dispatch()` /
  `guard_command()`, state re-exports and page routes (`/`, `/login`, `/VERSION`, `/health`,
  `/api/diagnostics`, `/ws`).
- The REST surface lives in `backend/routers/` (`system`, `update`, `printers`, `files`,
  `camera`) and is attached by `_install_routers()` at the very bottom of `main.py`.
  Routers reach shared helpers as **`core.<name>`** (`from backend import main as core`) so
  config paths, `mqtt_manager` and test monkeypatches on `backend.main` are still observed -
  never import those by value.
- Protocol modules are separate and independently testable: `mqtt_manager.py` (MQTTS +
  per-printer client, `FleetMqttManager`), `ftp_client.py` (implicit FTPS + 3mf/zip
  parsing), `camera.py` (ffmpeg MJPEG/snapshot + pure-Python RTSP), `updater.py`.
- `backend/commands.py` is the **command registry**: `COMMANDS` maps name → builder,
  and `risk_class()` classifies each as `safe | job | thermal | motion | home`.
  `backend/telemetry.py` parses the MQTT `print` report and types it (`TelemetryReport`);
  a retyped/renamed field is logged as drift but the tick is still delivered raw.
- `backend/state.py` owns the shared per-printer dicts (`latest_reports`, `homed_state`,
  `last_acks`) behind a re-entrant lock; `main.py` re-exports them, so seeding/clearing
  them in tests still works.
- `backend/paths.py` splits `RESOURCE_DIR` (read-only: `templates/`, `static/`, `VERSION`;
  `sys._MEIPASS` when frozen) from `DATA_DIR` (writable: `config/`, `uploads/`; repo root
  in dev, `%LOCALAPPDATA%\BambuFleetManager` when frozen, `BFM_DATA_DIR` overrides).

## Safety gating — do not route around it

`dispatch()` calls `guard_command()` before publishing anything.

- `motion` needs `confirm_motion=true` **and** tracked homing (`is_homed() is True`) **and**
  an idle printer (409 otherwise). `home` needs confirmation + idle but is allowed while
  unhomed. `thermal` needs `confirm_thermal=true`.
- The `allow_motion` setting only skips the confirmation ticks — never homing/idle checks.
- Axis self-test and bridge-test are deliberately **not implemented** (full-speed axis
  moves). Keep it that way.
- Homing is tracked from commands we issue, not from `home_flag` (unreliable on X1
  firmware); it is persisted in `config/state.json` and cleared when a job starts.
- If you add a command, add it to `COMMANDS` **and** the risk sets in `commands.py`, and
  exercise the 409 path in `tests/test_api.py`.
- `set_fan` is a **`safe`** command (it moves no axes and heats nothing) sent as an `M106`
  G-code line. Its `FAN_INDICES` name->`P` table is X1-family specific and **not verified on
  every firmware** - the UI shows each fan's reported speed beside its slider so a wrong
  mapping is visible rather than silent. Don't retune that table without a machine to check it.

## Testing quirks

- The `client` / `node` fixtures live in `tests/conftest.py` (lifted verbatim from
  `test_api.py`); any test module can request them.
- The `client` fixture (tmp_path + monkeypatch) isolates the machine-local state:
  `CONFIG_PATH`, `SETTINGS_PATH`, `STATE_PATH`, `UPLOAD_DIR`, `mqtt_manager=None`,
  clears `homed_state` / `latest_reports`, and sets `CALIBRATION_HOME_DELAY` /
  `PRINT_START_VERIFY_DELAY` to 0. `TestClient` runs the real lifespan, so
  `require_manager` **must** stay monkeypatched to the `FakeManager` that records
  `("publish", printer_id, payload)` tuples in `client.sent` — that list is how tests
  assert what was actually sent.
- Tests are sync (`TestClient`); no pytest-asyncio (removed — it was installed but unused).

## Release

- Version lives in `VERSION` (semver). `packaging/build.ps1` reads it, rebuilds the icon
  via `packaging/make_icon.py`, runs PyInstaller, **smoke-tests the frozen exe with
  `--selftest` against a scratch `BFM_DATA_DIR`**, then compiles the Inno Setup installer.
  `-Release` force-tags `v<version>` and publishes the installer to a GitHub Release —
  that's the artifact the in-app updater (`/api/update`) consumes.
- Commits follow Conventional Commits; released from `main`.
- Security invariants: printer access codes are never returned by the API, the RTSPS URL
  (which embeds one) comes from an authenticated endpoint, and TLS verification is
  intentionally disabled for the printers' self-signed certs.
- `docs/CAMERA.md` is the reference for the RTSP/Digest/RTP work — read it before touching
  `backend/camera.py`. Live MJPEG streams are registered per printer
  (`start_mjpeg_stream` / `stop_mjpeg_stream`): a printer serves one RTSP client, so a new
  viewer must replace the old ffmpeg, and the endpoint tears the process down in `finally`
  because killing ffmpeg is what unblocks a stalled pipe read.