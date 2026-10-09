"""Shared fixtures: the isolated TestClient plus a single registered printer.

Lifted verbatim from tests/test_api.py so any test module can request
``client`` / ``node`` without importing from it. The fixture monkeypatches
every machine-local path in backend/main.py, records what the fake MQTT
manager published in ``client.sent``, and clears cross-test state.
"""

import pytest
from fastapi.testclient import TestClient

from backend import main as main_module


@pytest.fixture()
def node(client):
    """A single registered printer node."""
    client.post(
        "/api/printers",
        json={"id": "n1", "name": "Bay 1", "ip": "10.0.0.5", "sn": "SN1", "access_code": "12345678"},
    )
    client.sent.clear()
    return "n1"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    config_file = tmp_path / "printers.json"
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(main_module, "CONFIG_PATH", str(config_file))
    monkeypatch.setattr(main_module, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(main_module, "mqtt_manager", None)
    monkeypatch.setattr(main_module, "CALIBRATION_HOME_DELAY", 0)
    monkeypatch.setattr(main_module, "PRINT_START_VERIFY_DELAY", 0)
    monkeypatch.setattr(main_module, "STATE_PATH", str(tmp_path / "state.json"))
    main_module.homed_state.clear()
    # isolate from the machine-local settings.json (allow_motion etc.)
    monkeypatch.setattr(main_module, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    main_module.latest_reports.clear()
    main_module.homed_state.clear()

    sent: list = []

    class FakeManager:
        clients = {}

        def register_printer(self, *a, **kw):
            sent.append(("register", a))

        def unregister_printer(self, *a, **kw):
            sent.append(("unregister", a))

        def send_payloads(self, p_id, payloads):
            for payload in payloads:
                sent.append(("publish", p_id, payload))
            return True

        def send_to_printer(self, p_id, payload):
            return self.send_payloads(p_id, [payload])

        def status(self):
            return {}

        def shutdown(self):
            pass

    monkeypatch.setattr(main_module, "require_manager", lambda: FakeManager())
    with TestClient(main_module.app) as test_client:
        test_client.sent = sent
        yield test_client
