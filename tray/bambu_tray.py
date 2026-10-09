"""Windows system-tray launcher for Bambu Fleet Manager.

Owns the uvicorn server in-process and exposes a tray menu to open the
dashboard, start/stop/restart the server, toggle "start with Windows" and exit.
Run it with ``pythonw`` (no console) or as the frozen ``BambuFleetManager.exe``.
"""

import json
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler

import uvicorn

from backend import main as backend_main
from backend.paths import DATA_DIR, RESOURCE_DIR, ensure_data_dirs

APP_NAME = "BambuFleetManager"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000

ensure_data_dirs()
LOG_PATH = os.path.join(DATA_DIR, "tray.log")


def _setup_logging() -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    return logging.getLogger("bfm.tray")


log = _setup_logging()


def read_settings() -> dict:
    path = os.path.join(DATA_DIR, "config", "settings.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def app_version() -> str:
    try:
        with open(os.path.join(RESOURCE_DIR, "VERSION"), "r", encoding="utf-8") as fh:
            return fh.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


class ServerController:
    """Runs uvicorn on a background thread so the tray stays responsive."""

    def __init__(self) -> None:
        self.server = None
        self.thread = None
        self.host = DEFAULT_HOST
        self.port = DEFAULT_PORT
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return True
            settings = read_settings()
            self.host = str(settings.get("host") or DEFAULT_HOST)
            self.port = int(settings.get("port") or DEFAULT_PORT)
            config = uvicorn.Config(
                backend_main.app, host=self.host, port=self.port,
                log_level="info", log_config=None, access_log=False,
            )
            self.server = uvicorn.Server(config)
            self.thread = threading.Thread(target=self.server.run, name="bfm-uvicorn", daemon=True)
            self.thread.start()
        ready = self._wait_ready()
        log.info("Server %s on %s:%d", "started" if ready else "failed to start", self.host, self.port)
        return ready

    def _wait_ready(self, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.running:
                return False
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                try:
                    probe.connect(("127.0.0.1", self.port))
                    return True
                except OSError:
                    time.sleep(0.25)
        return self.running

    def stop(self, timeout: float = 8.0) -> None:
        with self._lock:
            server, thread = self.server, self.thread
        if server is not None:
            server.should_exit = True
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        with self._lock:
            self.server = None
            self.thread = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def dashboard_url(self) -> str:
        return f"http://localhost:{self.port}/"


# --------------------------------------------------------------------- icon


def load_icon_image():
    from PIL import Image, ImageDraw

    path = os.path.join(RESOURCE_DIR, "assets", "bambu.ico")
    if os.path.exists(path):
        try:
            return Image.open(path)
        except OSError:
            pass
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle([0, 0, 63, 63], radius=14, fill=(0, 174, 66, 255))
    return img


# --------------------------------------------------------------- autostart


def _autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    script = os.path.abspath(__file__)
    return f'"{pythonw}" "{script}"'


def is_autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except (ImportError, FileNotFoundError, OSError):
        return False


def set_autostart(enabled: bool) -> None:
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
    except (ImportError, OSError) as exc:
        log.warning("Could not update autostart: %s", exc)


def fetch_diagnostics(port: int) -> str:
    """GET /api/diagnostics from the local server (access codes already redacted server-side)."""
    import urllib.request

    headers = {}
    token = str(read_settings().get("auth_token") or "")
    if token:
        headers["X-Auth-Token"] = token
    url = f"http://127.0.0.1:{port}/api/diagnostics"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
        return r.read().decode("utf-8")


def _copy_diagnostics(controller: ServerController) -> str:
    """Write the support bundle to the data dir and try to put it on the clipboard."""
    payload = fetch_diagnostics(controller.port)
    path = os.path.join(DATA_DIR, "diagnostics.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(payload)
    try:
        import subprocess

        subprocess.run("clip", input=payload, text=True, shell=True, check=True,
                       timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return f"Copied to clipboard and saved to {path}"
    except Exception as exc:
        return f"Saved to {path} (clipboard unavailable: {exc})"


# ------------------------------------------------------------------- health


HEALTH_INTERVAL = 30.0


def _warning_icon_image():
    """The normal icon with a red badge over the bottom-right corner."""
    from PIL import ImageDraw

    base = load_icon_image().convert("RGBA").resize((64, 64))
    draw = ImageDraw.Draw(base)
    draw.ellipse([42, 42, 62, 62], fill=(220, 38, 38, 255), outline=(255, 255, 255, 255), width=2)
    return base


class HealthMonitor:
    """Pings /health on a timer and flips the tray icon when the server stops
    answering - the only failure signal a headless, windowed app has."""

    def __init__(self, controller: ServerController, icon) -> None:
        self.controller = controller
        self.icon = icon
        self.up = True
        self._wake = threading.Event()
        self.thread = threading.Thread(target=self._run, name="bfm-health", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._wake.set()

    def _probe(self) -> bool:
        import urllib.request

        url = f"http://127.0.0.1:{self.controller.port}/health"
        headers = {}
        token = str(read_settings().get("auth_token") or "")
        if token:
            headers["X-Auth-Token"] = token
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5) as r:
                return r.status == 200
        except OSError:
            return False

    def _run(self) -> None:
        while not self._wake.wait(HEALTH_INTERVAL):
            if not self.controller.running:
                continue  # stopped from the menu on purpose; the menu already says so
            healthy = self._probe()
            if healthy != self.up:
                self.up = healthy
                self._apply_state()
                log.warning("Server %s", "recovered per /health" if healthy else "is not answering /health")

    def _apply_state(self) -> None:
        try:
            if self.up:
                self.icon.icon = load_icon_image()
                self.icon.title = f"Bambu Fleet Manager v{app_version()}"
            else:
                self.icon.icon = _warning_icon_image()
                self.icon.title = f"Bambu Fleet Manager v{app_version()} - server not responding"
            self.icon.update_menu()
        except Exception as exc:  # pragma: no cover - icon may be gone at shutdown
            log.debug("Tray health update failed: %s", exc)


# ------------------------------------------------------------------- runner


def _selftest() -> int:
    """Start the server headlessly, hit /VERSION, and record the result.

    Used by the build/smoke test because the frozen app is windowed and has no
    console to print to.
    """
    import urllib.request

    controller = ServerController()
    ok, version = False, ""
    try:
        ok = controller.start()
        if ok:
            with urllib.request.urlopen(controller.dashboard_url() + "VERSION", timeout=5) as r:
                version = r.read().decode().strip()
            ok = bool(version)
    except Exception as exc:  # pragma: no cover - diagnostics only
        log.error("selftest failed: %s", exc)
    finally:
        controller.stop()
    try:
        with open(os.path.join(DATA_DIR, "selftest.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"{'OK' if ok else 'FAIL'} {version}\n")
    except OSError:
        pass
    return 0 if ok else 1


def _diagnostics_note(controller: ServerController) -> str:
    """Collect diagnostics and return a short human-readable outcome."""
    try:
        note = _copy_diagnostics(controller)
        log.info("%s", note)
        return note
    except Exception as exc:
        log.warning("Could not collect diagnostics: %s", exc)
        return "diagnostics failed"


def _menu_handlers(controller: ServerController, icon, refresh) -> dict:
    """The callback behind every tray menu item."""

    def on_open(icon_, _item):
        webbrowser.open(controller.dashboard_url())

    def on_start(icon_, _item):
        controller.start()
        refresh()

    def on_stop(icon_, _item):
        controller.stop()
        refresh()

    def on_restart(icon_, _item):
        icon.title = "Bambu Fleet Manager - restarting..."
        controller.restart()
        icon.title = "Bambu Fleet Manager"
        refresh()

    def on_toggle_autostart(icon_, _item):
        set_autostart(not is_autostart_enabled())
        refresh()

    def on_open_data(icon_, _item):
        try:
            os.startfile(DATA_DIR)  # type: ignore[attr-defined]
        except OSError:
            pass

    def on_copy_diagnostics(icon_, _item):
        icon_.title = "Bambu Fleet Manager - collecting diagnostics..."
        icon_.title = f"Bambu Fleet Manager - {_diagnostics_note(controller)[:60]}"
        refresh()

    def on_exit(icon_, _item):
        controller.stop()
        icon.stop()

    return {
        "open": on_open,
        "start": on_start,
        "stop": on_stop,
        "restart": on_restart,
        "autostart": on_toggle_autostart,
        "data": on_open_data,
        "diagnostics": on_copy_diagnostics,
        "exit": on_exit,
    }


def _build_menu(pystray, controller: ServerController, icon, refresh) -> object:
    """Assemble the tray menu, wiring each item to the controller."""
    h = _menu_handlers(controller, icon, refresh)

    def status_text(_item) -> str:
        if controller.running:
            return f"Running - http://localhost:{controller.port}"
        return "Server stopped"

    return pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open dashboard", h["open"], default=True),
        pystray.MenuItem("Start server", h["start"], enabled=lambda _i: not controller.running),
        pystray.MenuItem("Stop server", h["stop"], enabled=lambda _i: controller.running),
        pystray.MenuItem("Restart server", h["restart"], enabled=lambda _i: controller.running),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start with Windows", h["autostart"], checked=lambda _i: is_autostart_enabled()),
        pystray.MenuItem("Open data folder", h["data"]),
        pystray.MenuItem("Copy diagnostics", h["diagnostics"], enabled=lambda _i: controller.running),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", h["exit"]),
    )


def _make_shutdown_hook(controller: ServerController, icon):
    """A self-update asks the app to quit; signal shutdown without joining the
    (current) server thread from inside it."""

    def on_update_shutdown() -> None:
        if controller.server is not None:
            controller.server.should_exit = True
        icon.stop()

    return on_update_shutdown


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()

    import pystray

    controller = ServerController()
    icon = pystray.Icon(APP_NAME, load_icon_image(), "Bambu Fleet Manager")

    def refresh() -> None:
        try:
            icon.update_menu()
        except Exception:  # pragma: no cover - menu may be gone during shutdown
            pass

    icon.menu = _build_menu(pystray, controller, icon, refresh)
    icon.title = f"Bambu Fleet Manager v{app_version()}"
    backend_main.register_shutdown_hook(_make_shutdown_hook(controller, icon))

    controller.start()
    log.info("Tray started (v%s)", app_version())
    monitor = HealthMonitor(controller, icon)
    monitor.start()
    try:
        icon.run()
    finally:
        monitor.stop()
        controller.stop()
        log.info("Tray exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
