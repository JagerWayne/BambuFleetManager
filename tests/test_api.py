"""End-to-end smoke tests for the REST surface and the WebSocket hub."""

import asyncio
import json
import os
import pathlib
import threading
import time
import zipfile

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


def test_dashboard_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "BAMBU" in res.text
    assert "/static/css/tailwind.min.css" in res.text


def test_static_assets_are_served(client):
    assert client.get("/static/css/tailwind.min.css").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200


def test_version_endpoint(client):
    assert client.get("/VERSION").status_code == 200


def test_printer_crud_lifecycle(client):
    printer = {
        "id": "node_01",
        "name": "Bay 1",
        "ip": "192.168.1.151",
        "sn": "00m00a1",
        "access_code": "12345678",
        "bed_type": "cool_plate",
        "ams_count": 2,
    }
    assert client.post("/api/printers", json=printer).status_code == 200

    listed = client.get("/api/printers").json()
    assert len(listed) == 1
    assert listed[0]["sn"] == "00M00A1"  # normalized to upper case
    assert listed[0]["bed_type"] == "cool_plate"

    assert client.delete("/api/printers/node_01").status_code == 200
    assert client.get("/api/printers").json() == []
    assert client.delete("/api/printers/node_01").status_code == 404


def test_stage_upload_rejects_bad_extension(client):
    res = client.post("/api/stage-upload", files={"file": ("payload.exe", b"MZ")})
    assert res.status_code == 400


def test_stage_upload_accepts_3mf(client):
    payload = b"PK\x03\x04data"
    res = client.post("/api/stage-upload", files={"file": ("benchy.3mf", payload)})
    assert res.status_code == 200
    body = res.json()
    assert body["filename"] == "benchy.3mf"
    assert body["size"] == len(payload)


def test_dispatch_rejects_unknown_printer(client):
    res = client.post("/api/dispatch-print", json={"printer_id": "ghost", "filename": "a.3mf"})
    assert res.status_code == 404


def test_dispatch_rejects_missing_file(client):
    client.post(
        "/api/printers",
        json={"id": "n1", "name": "n", "ip": "1.1.1.1", "sn": "SN1", "access_code": "12345678"},
    )
    res = client.post("/api/dispatch-print", json={"printer_id": "n1", "filename": "missing.3mf"})
    assert res.status_code == 400


def test_dispatch_publishes_project_file_command(client, monkeypatch):
    client.post(
        "/api/printers",
        json={"id": "n1", "name": "n", "ip": "1.1.1.1", "sn": "SN1", "access_code": "12345678"},
    )
    client.post("/api/stage-upload", files={"file": ("benchy.3mf", b"PK\x03\x04")})

    # Skip the real FTPS hop and pretend the archive holds plate 1.
    monkeypatch.setattr(main_module, "upload_3mf_file", lambda ip, code, path: True)
    monkeypatch.setattr(main_module, "local_plate_indices", lambda path: [1])
    main_module.latest_reports["n1"] = {"gcode_state": "PREPARE"}
    res = client.post("/api/dispatch-print", json={"printer_id": "n1", "filename": "benchy.3mf"})
    assert res.status_code == 200
    assert res.json()["status"] == "started"

    publishes = [s for s in client.sent if s[0] == "publish"]
    assert publishes, "expected an MQTT publish"
    payload = publishes[-1][2]["print"]
    assert payload["command"] == "project_file"
    assert payload["subtask_name"] == "benchy.3mf"
    assert payload["url"] == "file:///sdcard/benchy.3mf"
    assert payload["param"] == "Metadata/plate_1.gcode"


def test_dispatch_surfaces_ftps_failure(client, monkeypatch):
    client.post(
        "/api/printers",
        json={"id": "n1", "name": "n", "ip": "1.1.1.1", "sn": "SN1", "access_code": "12345678"},
    )
    client.post("/api/stage-upload", files={"file": ("benchy.3mf", b"PK\x03\x04")})

    monkeypatch.setattr(main_module, "upload_3mf_file", lambda ip, code, path: False)
    monkeypatch.setattr(main_module, "local_plate_indices", lambda path: [1])
    res = client.post("/api/dispatch-print", json={"printer_id": "n1", "filename": "benchy.3mf"})
    assert res.status_code == 502
    assert not [s for s in client.sent if s[0] == "publish"]


def test_control_endpoints_build_expected_payloads(client):
    client.post(
        "/api/printers",
        json={"id": "n1", "name": "n", "ip": "1.1.1.1", "sn": "SN1", "access_code": "12345678"},
    )
    for action, command in (("pause", "pause"), ("resume", "resume"), ("stop", "stop")):
        assert client.post(f"/api/printers/n1/{action}").status_code == 200
        assert client.sent[-1][2]["print"]["command"] == command

    assert client.post("/api/printers/n1/speed", json={"speed_level": 3}).status_code == 200
    speed = client.sent[-1][2]["print"]
    assert speed["command"] == "print_speed"
    assert speed["param"] == "3"

    assert client.post("/api/printers/n1/speed", json={"speed_level": 9}).status_code == 422

    assert client.post("/api/printers/n1/light", json={"state": True}).status_code == 200
    system = client.sent[-1][2]["system"]
    assert system["command"] == "ledctrl"
    assert system["led_mode"] == "on"


def test_websocket_telemetry_broadcast(client):
    sent_messages = []

    class FakeSocket:
        async def send_json(self, message):
            sent_messages.append(message)

    class BrokenSocket:
        async def send_json(self, message):
            raise RuntimeError("socket closed")

        def __eq__(self, other):
            return other is self

        def __hash__(self):
            return id(self)

    hub = main_module.ws_hub
    hub.active_connections.extend([FakeSocket(), BrokenSocket()])
    try:
        asyncio.run(main_module.telemetry_handler("node_01", {"gcode_state": "RUNNING", "mc_percent": 10}))
    finally:
        hub.active_connections.clear()

    assert len(sent_messages) == 1
    assert sent_messages[0]["event"] == "telemetry"
    assert sent_messages[0]["printer_id"] == "node_01"
    assert sent_messages[0]["data"]["mc_percent"] == 10
    assert hub.active_connections == []


def test_websocket_endpoint_accepts_connections(client):
    with client.websocket_connect("/ws"):
        assert len(main_module.ws_hub.active_connections) == 1
    assert main_module.ws_hub.active_connections == []


def last_publish(client):
    return client.sent[-1][2]


# ------------------------------------------------------------ command surface


def test_commands_registry_endpoint(client):
    res = client.get("/api/commands").json()
    assert "pause" in res["commands"]
    assert "camera" in res["commands"]
    assert res["speed_profiles"]["4"] == "ludicrous"
    assert set(res["groups"]) >= {"print", "system", "camera"}


def test_generic_command_endpoint(node, client):
    res = client.post(f"/api/printers/{node}/command", json={"command": "pause", "params": {}})
    assert res.status_code == 200
    assert res.json()["command"] == "pause"
    assert last_publish(client)["print"]["command"] == "pause"


def test_generic_command_rejects_unknown_name(node, client):
    res = client.post(f"/api/printers/{node}/command", json={"command": "rm_rf", "params": {}})
    assert res.status_code == 422
    assert not client.sent


def test_generic_command_requires_known_printer(client):
    res = client.post("/api/printers/ghost/command", json={"command": "pause"})
    assert res.status_code == 404


def test_speed_profile_endpoint(node, client):
    assert client.post(f"/api/printers/{node}/speed-profile", json={"level": 4}).status_code == 200
    assert last_publish(client)["print"]["param"] == "4"
    assert client.post(f"/api/printers/{node}/speed-profile", json={"level": 9}).status_code == 422


def test_light_mode_endpoint(node, client):
    res = client.post(f"/api/printers/{node}/light-mode", json={"node": "nozzle_light", "mode": "on"})
    assert res.status_code == 200
    assert last_publish(client)["system"]["led_node"] == "nozzle_light"
    bad = client.post(f"/api/printers/{node}/light-mode", json={"mode": "strobe"})
    assert bad.status_code == 422


@pytest.mark.parametrize(
    "kind",
    ["bed_leveling", "flow_calibration", "vibration_calibration"],
)
def test_calibration_requires_confirmation(node, client, kind):
    """Machine-moving commands must never fire from a bare request."""
    res = client.post(f"/api/printers/{node}/calibrate", json={"kind": kind, "mode": 2})
    assert res.status_code == 409
    assert "confirm_motion" in res.json()["detail"]
    assert not client.sent


@pytest.mark.parametrize(
    "kind", ["bed_leveling", "flow_calibration", "vibration_calibration"]
)
def test_calibration_homes_first_then_runs_the_routine(node, client, kind):
    main_module.latest_reports[node] = {"gcode_state": "IDLE"}
    res = client.post(
        f"/api/printers/{node}/calibrate",
        json={"kind": kind, "mode": 2, "confirm_motion": True},
    )
    assert res.status_code == 200
    payloads = [entry[2]["print"]["command"] for entry in client.sent]
    # a G28 home payload, then the calibration routine
    assert payloads[0] == "gcode_line"
    assert client.sent[0][2]["print"]["param"] == "G28\n"
    assert payloads[-1] == kind
    assert main_module.is_homed(node) is True


def test_calibration_homes_even_when_homing_state_was_unknown(node, client):
    # no report at all - calibrating still works because it homes first
    res = client.post(
        f"/api/printers/{node}/calibrate",
        json={"kind": "bed_leveling", "confirm_motion": True},
    )
    assert res.status_code == 200
    assert main_module.is_homed(node) is True


def test_calibration_rejects_unknown_kind(node, client):
    assert client.post(f"/api/printers/{node}/calibrate", json={"kind": "warp_core"}).status_code == 422


def test_axis_ramming_commands_are_not_implemented(node, client):
    """Axis self-test and bridge test are deliberately absent from the registry."""
    for kind in ("axis_self_test", "bridge_test", "self_test"):
        assert client.post(
            f"/api/printers/{node}/calibrate",
            json={"kind": kind, "confirm_motion": True},
        ).status_code == 422


@pytest.mark.parametrize(
    "action,expected",
    [
        ("current_tray", "get_current_tray"),
        ("tray_select", "ams_user_setting"),
        ("tray_info", "tray_info"),
    ],
)
def test_ams_informational_actions_are_safe(node, client, action, expected):
    res = client.post(f"/api/printers/{node}/ams", json={"action": action, "tray_id": 3})
    assert res.status_code == 200
    assert last_publish(client)["print"]["command"] == expected


@pytest.mark.parametrize("action", ["feed", "unload", "resume", "auto"])
def test_ams_moving_actions_require_confirmation(node, client, action):
    res = client.post(f"/api/printers/{node}/ams", json={"action": action})
    assert res.status_code == 409
    assert "confirm_thermal" in res.json()["detail"]
    assert not client.sent


def test_ams_feed_unload_use_target_flag(node, client):
    client.post(f"/api/printers/{node}/ams", json={"action": "feed", "confirm_thermal": True})
    assert last_publish(client)["print"]["target"] == 1
    client.post(f"/api/printers/{node}/ams", json={"action": "unload", "confirm_thermal": True})
    assert last_publish(client)["print"]["target"] == 2


def test_ams_external_spool_id_allowed(node, client):
    res = client.post(
        f"/api/printers/{node}/ams",
        json={"action": "feed", "ams_id": 255, "confirm_thermal": True},
    )
    assert res.status_code == 200


def test_skip_objects_endpoint(node, client):
    res = client.post(f"/api/printers/{node}/skip-objects", json={"object_ids": [1, 4]})
    assert res.status_code == 200
    assert last_publish(client)["print"]["obj_list"] == [1, 4]
    assert client.post(f"/api/printers/{node}/skip-objects", json={"object_ids": []}).status_code == 400


def test_print_option_endpoint(node, client):
    res = client.post(f"/api/printers/{node}/print-option", json={"nozzle_temp": 250, "bed_type": "cool_plate"})
    assert res.status_code == 200
    option = last_publish(client)["print"]["print_option"]
    assert option == {"nozzle_temp": 250, "bed_type": "cool_plate"}


def test_retry_and_reboot_endpoints(node, client):
    assert client.post(f"/api/printers/{node}/retry").status_code == 200
    assert last_publish(client)["print"]["command"] == "print_error"
    assert client.post(f"/api/printers/{node}/reboot", json={"module": "esp32"}).status_code == 200
    assert last_publish(client)["system"] == {"command": "restart_module", "module": "esp32"}
    assert client.post(f"/api/printers/{node}/reboot", json={"module": "nuke"}).status_code == 422


def test_refresh_state_endpoint(node, client):
    assert client.post(f"/api/printers/{node}/refresh").status_code == 200
    assert last_publish(client)["pushing"]["command"] == "start"


# --------------------------------------------------------------- SD browsing

ENTRIES = [
    {"name": "apps", "path": "/apps", "is_dir": True, "is_printable": False, "size": 0, "modified": None},
    {"name": "benchy.3mf", "path": "/benchy.3mf", "is_dir": False, "is_printable": True, "size": 42,
     "modified": "2024-01-02T03:04:05"},
]


@pytest.fixture()
def fake_listing(monkeypatch):
    monkeypatch.setattr(main_module, "list_directory", lambda ip, code, path="/": ENTRIES)


def test_file_browser_listing(node, client, fake_listing):
    res = client.get(f"/api/printers/{node}/files")
    assert res.status_code == 200
    body = res.json()
    assert body["path"] == "/"
    assert body["parent"] is None
    assert [e["name"] for e in body["entries"]] == ["apps", "benchy.3mf"]


def test_file_browser_parent_of_subdirectory(node, client, fake_listing):
    body = client.get(f"/api/printers/{node}/files", params={"path": "/apps"}).json()
    assert body["path"] == "/apps"
    assert body["parent"] == "/"


def test_file_browser_normalizes_traversal(node, client, fake_listing):
    body = client.get(f"/api/printers/{node}/files", params={"path": "/../secrets"}).json()
    assert body["path"] == "/secrets"


def test_file_browser_requires_printer(client):
    assert client.get("/api/printers/ghost/files").status_code == 404


def test_file_browser_surfaces_ftps_errors(node, client, monkeypatch):
    def boom(ip, code, path="/"):
        raise RuntimeError("550 connection reset")

    monkeypatch.setattr(main_module, "list_directory", boom)
    res = client.get(f"/api/printers/{node}/files")
    assert res.status_code == 502
    assert "550" in res.json()["detail"]


def test_file_delete(node, client, monkeypatch):
    calls = {}
    monkeypatch.setattr(main_module, "delete_path", lambda ip, code, path, is_dir: calls.update(
        {"path": path, "is_dir": is_dir}
    ))
    res = client.request(
        "DELETE", f"/api/printers/{node}/files", json={"path": "/apps", "is_dir": True}
    )
    assert res.status_code == 200
    assert calls == {"path": "/apps", "is_dir": True}


def test_file_delete_rejects_root(node, client, monkeypatch):
    def refuse(ip, code, path, is_dir):
        raise ValueError("refusing to delete the SD card root")

    monkeypatch.setattr(main_module, "delete_path", refuse)
    res = client.request("DELETE", f"/api/printers/{node}/files", json={"path": "/", "is_dir": True})
    assert res.status_code == 400


def test_file_rename(node, client, monkeypatch):
    calls = {}

    def fake_rename(ip, code, path, new_name):
        calls.update({"path": path, "new_name": new_name})
        return "/apps/memo.3mf"

    monkeypatch.setattr(main_module, "rename_path", fake_rename)
    res = client.post(
        f"/api/printers/{node}/files/rename",
        json={"path": "/apps/notes.3mf", "new_name": "memo.3mf"},
    )
    assert res.status_code == 200
    assert res.json()["path"] == "/apps/memo.3mf"
    assert calls == {"path": "/apps/notes.3mf", "new_name": "memo.3mf"}


def test_file_rename_only_accepts_3mf(node, client, monkeypatch):
    monkeypatch.setattr(main_module, "rename_path", lambda *a: "/x.3mf")
    # renaming a non-project file
    assert client.post(
        f"/api/printers/{node}/files/rename",
        json={"path": "/apps/notes.txt", "new_name": "memo.3mf"},
    ).status_code == 400
    # and a new name that would drop the project suffix
    assert client.post(
        f"/api/printers/{node}/files/rename",
        json={"path": "/apps/notes.3mf", "new_name": "memo.txt"},
    ).status_code == 400


def test_file_rename_rejects_a_path_in_the_name(node, client, monkeypatch):
    monkeypatch.setattr(main_module, "rename_path", lambda *a: "/x.3mf")
    for bad in ("../escaped.3mf", "sub/memo.3mf", "..", "back\\slash.3mf"):
        res = client.post(
            f"/api/printers/{node}/files/rename",
            json={"path": "/apps/notes.3mf", "new_name": bad},
        )
        assert res.status_code in (400, 422), bad


def test_file_rename_unchanged_name_is_a_noop(node, client, monkeypatch):
    def boom(*a):
        raise AssertionError("must not touch the SD card for an unchanged name")

    monkeypatch.setattr(main_module, "rename_path", boom)
    res = client.post(
        f"/api/printers/{node}/files/rename",
        json={"path": "/apps/memo.3mf", "new_name": "memo.3mf"},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "unchanged"


def test_file_rename_surfaces_ftps_errors(node, client, monkeypatch):
    def boom(*a):
        raise OSError("550 rename failed")

    monkeypatch.setattr(main_module, "rename_path", boom)
    res = client.post(
        f"/api/printers/{node}/files/rename",
        json={"path": "/apps/notes.3mf", "new_name": "memo.3mf"},
    )
    assert res.status_code == 502


def test_print_remote_from_sd_card(node, client, monkeypatch):
    # the printer must start the job for the call to succeed
    monkeypatch.setattr(main_module, "detect_plate_indices", lambda ip, code, path: [5])
    main_module.latest_reports[node] = {"gcode_state": "PREPARE"}
    res = client.post(f"/api/printers/{node}/print-remote", json={"path": "/benchy.3mf"})
    assert res.status_code == 200
    assert res.json()["job"] == "benchy.3mf"
    payload = last_publish(client)["print"]
    assert payload["command"] == "project_file"
    # the printer addresses its SD card as /sdcard
    assert payload["url"] == "file:///sdcard/benchy.3mf"
    assert payload["subtask_name"] == "benchy.3mf"
    # plate 5 was auto-selected because that is what the archive contains
    assert payload["param"] == "Metadata/plate_5.gcode"


def test_print_remote_sends_layer_inspect_flag(node, client, monkeypatch):
    monkeypatch.setattr(main_module, "detect_plate_indices", lambda ip, code, path: [1])
    main_module.latest_reports[node] = {"gcode_state": "PREPARE"}
    res = client.post(
        f"/api/printers/{node}/print-remote",
        json={"path": "/benchy.3mf", "layer_inspect": False},
    )
    assert res.status_code == 200
    assert last_publish(client)["print"]["layer_inspect"] is False


def test_print_remote_in_subfolder(node, client, monkeypatch):
    monkeypatch.setattr(main_module, "detect_plate_indices", lambda ip, code, path: [1])
    main_module.latest_reports[node] = {"gcode_state": "RUNNING"}
    res = client.post(f"/api/printers/{node}/print-remote", json={"path": "/cache/part.3mf"})
    assert res.status_code == 200
    assert last_publish(client)["print"]["url"] == "file:///sdcard/cache/part.3mf"


def test_print_remote_reports_printer_refusal(node, client, monkeypatch):
    # printer stays in FINISH and answers with an error -> surface its reason
    plate_stub = lambda ip, code, path: [1]  # noqa: E731
    monkeypatch.setattr(main_module, "detect_plate_indices", plate_stub)
    main_module.latest_reports[node] = {"gcode_state": "FINISH"}

    async def fake_send(fn, *args):
        if fn is plate_stub:
            return [1]
        main_module.last_acks.setdefault(node, {})["project_file"] = {
            "command": "project_file", "result": "FAIL", "reason": "ERROR STATE"
        }
        return True

    monkeypatch.setattr(main_module, "run_blocking", fake_send)
    res = client.post(f"/api/printers/{node}/print-remote", json={"path": "/benchy.3mf"})
    assert res.status_code == 502
    assert "did not start" in res.json()["detail"]
    assert "ERROR STATE" in res.json()["detail"]


def test_print_remote_refuses_when_printer_is_failed(node, client, monkeypatch):
    plate_stub = lambda ip, code, path: [1]  # noqa: E731
    monkeypatch.setattr(main_module, "detect_plate_indices", plate_stub)
    main_module.latest_reports[node] = {"gcode_state": "FAILED", "print_error": 83902467}

    async def fake_send(fn, *args):
        if fn is plate_stub:
            return [1]
        return True

    monkeypatch.setattr(main_module, "run_blocking", fake_send)
    res = client.post(f"/api/printers/{node}/print-remote", json={"path": "/benchy.3mf"})
    assert res.status_code == 409
    assert "FAILED" in res.json()["detail"]
    assert "83902467" in res.json()["detail"]


def test_print_remote_rejects_non_project(node, client):
    res = client.post(f"/api/printers/{node}/print-remote", json={"path": "/notes.txt"})
    assert res.status_code == 400


def test_download_endpoint_streams(node, client, monkeypatch):
    monkeypatch.setattr(main_module, "remote_file_exists", lambda ip, code, path: True)
    monkeypatch.setattr(main_module, "download_stream", lambda ip, code, path: iter([b"abc", b"def"]))
    res = client.get(f"/api/printers/{node}/files/download", params={"path": "/benchy.3mf"})
    assert res.status_code == 200
    assert res.content == b"abcdef"
    assert "benchy.3mf" in res.headers["content-disposition"]


def test_download_missing_file_is_404(node, client, monkeypatch):
    monkeypatch.setattr(main_module, "remote_file_exists", lambda ip, code, path: False)
    res = client.get(f"/api/printers/{node}/files/download", params={"path": "/nope.3mf"})
    assert res.status_code == 404


# -------------------------------------------------------------------- camera


def test_camera_toggle(node, client):
    assert client.post(f"/api/printers/{node}/camera", json={"state": True}).status_code == 200
    assert last_publish(client)["camera"]["command"] == "push"
    client.post(f"/api/printers/{node}/camera", json={"state": False})
    assert last_publish(client)["camera"]["command"] == "stop"


def test_camera_snapshot(node, client, monkeypatch):
    monkeypatch.setattr(main_module.camera_mod, "ffmpeg_snapshot", lambda *a: b"\xff\xd8frame")
    res = client.get(f"/api/printers/{node}/camera/snapshot", params={"cache_bust": 7})
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert res.content == b"\xff\xd8frame"
    assert res.headers["cache-control"].startswith("no-store")


def test_camera_snapshot_unavailable(node, client, monkeypatch):
    monkeypatch.setattr(main_module.camera_mod, "ffmpeg_snapshot", lambda *a: None)
    monkeypatch.setattr(main_module.camera_mod, "fetch_snapshot", lambda ip: None)
    res = client.get(f"/api/printers/{node}/camera/snapshot")
    assert res.status_code == 502


def test_camera_mjpeg_streams_then_releases_the_ffmpeg(node, client, monkeypatch):
    """The stream must be registered up front and released when it ends, or the
    ffmpeg process outlives the viewer."""
    calls = []

    class FakeStream:
        def __init__(self):
            self.pending = [b"--frame\r\n", b"Content-Type: image/jpeg\r\n\r\n", b"jpeg"]

        def read(self, size=4096):
            return self.pending.pop(0) if self.pending else b""

    stream = FakeStream()
    monkeypatch.setattr(main_module.camera_mod, "ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(main_module.camera_mod, "start_mjpeg_stream",
                        lambda *a, **k: (calls.append("start"), stream)[1])
    monkeypatch.setattr(main_module.camera_mod, "stop_mjpeg_stream",
                        lambda *a, **k: calls.append("stop"))

    res = client.get(f"/api/printers/{node}/camera/mjpeg")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("multipart/x-mixed-replace")
    assert b"jpeg" in res.content
    assert calls[0] == "start"
    assert calls[-1] == "stop"


def test_camera_mjpeg_is_one_stream_per_printer(node, client, monkeypatch):
    """Starting a stream passes the printer id, so the registry can replace it."""
    seen = {}

    class FakeStream:
        def read(self, size=4096):
            return b""

    def fake_start(printer_id, ip, code, width, fps):
        seen["printer_id"] = printer_id
        seen["ip"] = ip
        return FakeStream()

    monkeypatch.setattr(main_module.camera_mod, "ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(main_module.camera_mod, "start_mjpeg_stream", fake_start)
    monkeypatch.setattr(main_module.camera_mod, "stop_mjpeg_stream", lambda *a, **k: None)

    assert client.get(f"/api/printers/{node}/camera/mjpeg").status_code == 200
    assert seen["printer_id"] == node
    assert seen["ip"] == "10.0.0.5"


def test_camera_mjpeg_503_without_ffmpeg(node, client, monkeypatch):
    monkeypatch.setattr(main_module.camera_mod, "ffmpeg_exe", lambda: None)
    assert client.get(f"/api/printers/{node}/camera/mjpeg").status_code == 503


def test_camera_stream_proxies(node, client, monkeypatch):
    monkeypatch.setattr(
        main_module.camera_mod,
        "open_stream",
        lambda ip: ("multipart/x-mixed-replace", iter([b"a", b"b"])),
    )
    res = client.get(f"/api/printers/{node}/camera/stream")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("multipart/x-mixed-replace")
    assert res.content == b"ab"


# -------------------------------------------------------------- ws snapshots


def test_telemetry_snapshot_endpoint(client):
    client.post(
        "/api/printers",
        json={"id": "n1", "name": "n", "ip": "1.1.1.1", "sn": "SN1", "access_code": "12345678"},
    )
    main_module.latest_reports["n1"] = {"gcode_state": "RUNNING", "mc_percent": 55}
    body = client.get("/api/printers/n1/telemetry").json()
    assert body["data"]["mc_percent"] == 55


def test_websocket_hello_includes_reports(client):
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
    assert hello["event"] == "hello"


# ------------------------------------------------------ manual temperatures


def test_temperature_requires_confirmation(node, client):
    res = client.post(f"/api/printers/{node}/temperature", json={"nozzle": 200})
    assert res.status_code == 409
    assert "confirm_thermal" in res.json()["detail"]
    assert not client.sent


def test_temperature_sends_each_target(node, client):
    res = client.post(
        f"/api/printers/{node}/temperature",
        json={"nozzle": 220, "bed": 60, "confirm_thermal": True},
    )
    assert res.status_code == 200
    params = [p["print"]["param"] for _, _, p in client.sent if p["print"]["command"] == "gcode_line"]
    # both setpoints go out as G-code: M104 for the nozzle, M140 for the bed
    assert "M104 S220\n" in params
    assert "M140 S60\n" in params


# ------------------------------------------------------------------------- fans


# ----------------------------------------------------------------- staging send


def test_send_staged_file_uploads_it(node, client, monkeypatch, tmp_path):
    staged = tmp_path / "part.3mf"
    staged.write_bytes(b"PK\x03\x04project")
    monkeypatch.setattr(main_module, "UPLOAD_DIR", str(tmp_path))
    calls = {}
    monkeypatch.setattr(
        main_module, "upload_3mf_file",
        lambda ip, code, local, remote_dir: calls.update(
            {"ip": ip, "local": local, "dir": remote_dir}) or True,
    )

    res = client.post(
        f"/api/printers/{node}/files/upload-staged",
        json={"filename": "part.3mf", "dir_path": "/"},
    )
    assert res.status_code == 200
    assert res.json()["path"] == "/part.3mf"
    assert res.json()["name"] == "part.3mf"
    assert calls["local"] == str(staged)
    assert calls["dir"] == "/"


def test_send_staged_file_rejects_a_missing_file(node, client, monkeypatch, tmp_path):
    monkeypatch.setattr(main_module, "UPLOAD_DIR", str(tmp_path))
    res = client.post(
        f"/api/printers/{node}/files/upload-staged",
        json={"filename": "ghost.3mf", "dir_path": "/"},
    )
    assert res.status_code == 404


def test_send_staged_file_rejects_a_bad_name(node, client):
    for bad in ("part.txt", "../escape.3mf", "sub/dir.3mf"):
        res = client.post(
            f"/api/printers/{node}/files/upload-staged",
            json={"filename": bad, "dir_path": "/"},
        )
        assert res.status_code == 400, bad


def test_send_staged_file_surfaces_ftps_errors(node, client, monkeypatch, tmp_path):
    (tmp_path / "part.3mf").write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(main_module, "UPLOAD_DIR", str(tmp_path))

    def boom(*a, **k):
        raise OSError("425 transfer failed")

    monkeypatch.setattr(main_module, "upload_3mf_file", boom)
    res = client.post(
        f"/api/printers/{node}/files/upload-staged",
        json={"filename": "part.3mf", "dir_path": "/"},
    )
    assert res.status_code == 502


def test_fan_sends_an_m106_gcode_line(node, client):
    res = client.post(f"/api/printers/{node}/fan", json={"fan": "aux", "speed": 100})
    assert res.status_code == 200
    params = [p["print"]["param"] for _, _, p in client.sent if p["print"]["command"] == "gcode_line"]
    assert params == ["M106 P1 S255\n"]
    # safe command: no confirmation flags required
    assert res.json()["risk"] == "safe"


def test_fan_accepts_the_three_fans(node, client):
    for fan, index in (("part", 0), ("aux", 1), ("chamber", 3)):
        client.sent.clear()
        assert client.post(
            f"/api/printers/{node}/fan", json={"fan": fan, "speed": 50}
        ).status_code == 200
        param = [p["print"]["param"] for _, _, p in client.sent if p["print"]["command"] == "gcode_line"][0]
        assert param == f"M106 P{index} S128\n", fan


def test_fan_rejects_an_unknown_fan(node, client):
    res = client.post(f"/api/printers/{node}/fan", json={"fan": "turbo", "speed": 50})
    assert res.status_code == 422


def test_fan_rejects_an_out_of_range_speed(node, client):
    assert client.post(
        f"/api/printers/{node}/fan", json={"fan": "part", "speed": 101}
    ).status_code == 422
    assert client.post(
        f"/api/printers/{node}/fan", json={"fan": "part", "speed": -1}
    ).status_code == 422


def test_fan_rejects_an_unknown_printer(client):
    assert client.post(
        "/api/printers/ghost/fan", json={"fan": "part", "speed": 50}
    ).status_code == 404


def test_temperature_rejects_over_cap(node, client):
    # 900 > 300 -> rejected by the request model
    assert client.post(
        f"/api/printers/{node}/temperature", json={"nozzle": 900, "confirm_thermal": True}
    ).status_code == 422


def test_temperature_requires_a_value(node, client):
    assert client.post(
        f"/api/printers/{node}/temperature", json={"confirm_thermal": True}
    ).status_code == 400


def test_temperature_blocked_while_printing(node, client):
    main_module.latest_reports[node] = {"gcode_state": "RUNNING"}
    res = client.post(
        f"/api/printers/{node}/temperature", json={"nozzle": 200, "confirm_thermal": True}
    )
    assert res.status_code == 409
    assert "running" in res.json()["detail"]


def test_temperature_override_while_printing(node, client):
    main_module.latest_reports[node] = {"gcode_state": "RUNNING"}
    res = client.post(
        f"/api/printers/{node}/temperature",
        json={"chamber": 40, "confirm_thermal": True, "allow_while_printing": True},
    )
    assert res.status_code == 200
    assert client.sent[-1][2]["print"]["command"] == "set_ctt"
    assert client.sent[-1][2]["print"]["ctt_val"] == 40


def test_light_sends_timing_block(node, client):
    res = client.post(f"/api/printers/{node}/light-mode", json={"node": "chamber_light", "mode": "on"})
    assert res.status_code == 200
    system = client.sent[-1][2]["system"]
    for field in ("led_on_time", "led_off_time", "loop_times", "interval_time"):
        assert field in system


# ------------------------------------------------------------- server settings


def test_settings_defaults(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    body = client.get("/api/settings").json()
    assert body["host"] == "0.0.0.0"
    assert body["port"] == 8000
    assert "current_port" in body


def test_settings_round_trip(client, tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(main_module, "SETTINGS_PATH", str(path))

    saved = client.post("/api/settings", json={"host": "127.0.0.1", "port": 9001})
    assert saved.status_code == 200
    assert saved.json()["restart_required"] is True
    assert json.loads(path.read_text())["port"] == 9001

    assert client.get("/api/settings").json()["port"] == 9001


def test_settings_rejects_bad_port(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    assert client.post("/api/settings", json={"port": 0}).status_code == 422
    assert client.post("/api/settings", json={"port": 99999}).status_code == 422


# --------------------------------------------------------------- motion APIs


def test_home_requires_confirmation(node, client):
    res = client.post(f"/api/printers/{node}/home", json={"axes": ""})
    assert res.status_code == 409
    assert "confirm_motion" in res.json()["detail"]
    assert not client.sent


def test_home_blocked_while_printing(node, client):
    main_module.latest_reports[node] = {"gcode_state": "RUNNING"}
    res = client.post(f"/api/printers/{node}/home", json={"axes": "z", "confirm_motion": True})
    assert res.status_code == 409
    assert "running" in res.json()["detail"]
    assert not client.sent


def test_home_allowed_while_unhomed(node, client):
    # homing is the way out of the unhomed state, so it must not require homed
    main_module.latest_reports[node] = {"gcode_state": "IDLE", "home_flag": 0b111}
    res = client.post(f"/api/printers/{node}/home", json={"axes": "x", "confirm_motion": True})
    assert res.status_code == 200
    assert last_publish(client)["print"]["param"] == "G28 X\n"


def test_jog_requires_confirmation(node, client):
    assert client.post(f"/api/printers/{node}/jog", json={"axis": "x", "distance": 10}).status_code == 409
    assert not client.sent


def test_jog_requires_homing(node, client):
    main_module.latest_reports[node] = {"gcode_state": "IDLE", "home_flag": 0b011}
    res = client.post(f"/api/printers/{node}/jog",
                      json={"axis": "x", "distance": 10, "confirm_motion": True})
    assert res.status_code == 409
    assert "homed" in res.json()["detail"]


def test_jog_ok_when_homed(node, client):
    main_module.latest_reports[node] = {"gcode_state": "IDLE"}
    main_module.homed_state[node] = True
    res = client.post(f"/api/printers/{node}/jog",
                      json={"axis": "z", "distance": -1, "feedrate": 600, "confirm_motion": True})
    assert res.status_code == 200
    assert last_publish(client)["print"]["param"] == "G91\nG1 Z-1 F600\nG90\n"


def test_jog_rejects_crazy_distance(node, client):
    assert client.post(f"/api/printers/{node}/jog",
                       json={"axis": "x", "distance": 9999, "confirm_motion": True}).status_code == 422
    assert client.post(f"/api/printers/{node}/jog",
                       json={"axis": "a", "distance": 1, "confirm_motion": True}).status_code == 422


def test_extrude_blocked_on_cold_nozzle(node, client):
    main_module.latest_reports[node] = {"gcode_state": "IDLE", "nozzle_temper": 25}
    res = client.post(f"/api/printers/{node}/extrude", json={"amount": 5, "confirm_thermal": True})
    assert res.status_code == 409
    assert "170" in res.json()["detail"]
    assert not client.sent


def test_extrude_retract_allowed_cold(node, client):
    main_module.latest_reports[node] = {"gcode_state": "IDLE", "nozzle_temper": 25}
    res = client.post(f"/api/printers/{node}/extrude", json={"amount": -5, "confirm_thermal": True})
    assert res.status_code == 200
    assert last_publish(client)["print"]["param"] == "G91\nG1 E-5 F300\nG90\n"


def test_extrude_allowed_hot(node, client):
    main_module.latest_reports[node] = {"gcode_state": "IDLE", "nozzle_temper": 250}
    res = client.post(f"/api/printers/{node}/extrude", json={"amount": 5, "confirm_thermal": True})
    assert res.status_code == 200


def test_extrude_blocked_while_printing(node, client):
    main_module.latest_reports[node] = {"gcode_state": "RUNNING", "nozzle_temper": 250}
    res = client.post(f"/api/printers/{node}/extrude", json={"amount": 5, "confirm_thermal": True})
    assert res.status_code == 409


# ---------------------------------------------------------- homed tracking


def test_homed_state_is_tracked_not_guessed(client, node):
    # home_flag is unreliable on X1 firmware, so nothing is assumed up front
    assert main_module.is_homed(node) is None
    main_module.latest_reports[node] = {"gcode_state": "IDLE", "home_flag": 0b111}
    main_module.track_homing(node, main_module.latest_reports[node])
    assert main_module.is_homed(node) is None

    main_module.latest_reports[node] = {"gcode_state": "IDLE", "home_flag": 0}
    main_module.track_homing(node, main_module.latest_reports[node])
    assert main_module.is_homed(node) is True

    main_module.latest_reports[node] = {"gcode_state": "RUNNING"}
    main_module.track_homing(node, main_module.latest_reports[node])
    assert main_module.is_homed(node) is False


def test_home_endpoint_marks_homed(node, client):
    main_module.latest_reports[node] = {"gcode_state": "IDLE"}
    res = client.post(f"/api/printers/{node}/home", json={"axes": "", "confirm_motion": True})
    assert res.status_code == 200
    assert main_module.is_homed(node) is True


def test_home_single_axis_does_not_mark_fully_homed(node, client):
    res = client.post(f"/api/printers/{node}/home", json={"axes": "x", "confirm_motion": True})
    assert res.status_code == 200
    assert main_module.is_homed(node) is None


# --------------------------------------------------- global motion unlock


def test_allow_motion_setting_skips_the_confirmation_tick(node, client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    main_module.latest_reports[node] = {"gcode_state": "IDLE"}
    main_module.homed_state[node] = True

    # locked by default: without the tick the move is refused
    assert client.post(f"/api/printers/{node}/jog",
                       json={"axis": "x", "distance": 1}).status_code == 409

    client.post("/api/settings", json={"host": "0.0.0.0", "port": 8000, "allow_motion": True})

    # unlocked: no confirmation needed, but homing is still enforced
    res = client.post(f"/api/printers/{node}/jog", json={"axis": "x", "distance": 1})
    assert res.status_code == 200

    main_module.homed_state[node] = False
    assert client.post(f"/api/printers/{node}/jog",
                       json={"axis": "x", "distance": 1}).status_code == 409


def test_print_remote_rejects_archive_without_a_plate(node, client, monkeypatch):
    monkeypatch.setattr(main_module, "detect_plate_indices", lambda ip, code, path: [])
    main_module.latest_reports[node] = {"gcode_state": "IDLE"}
    res = client.post(f"/api/printers/{node}/print-remote", json={"path": "/notes.3mf"})
    assert res.status_code == 400
    assert "plate" in res.json()["detail"].lower()


# ----------------------------------------------------- security / auth


def test_printers_never_return_the_access_code(client):
    client.post("/api/printers", json={
        "id": "n1", "name": "n", "ip": "1.1.1.1", "sn": "SN1", "access_code": "supersecret"
    })
    body = client.get("/api/printers").json()
    assert body[0]["has_access_code"] is True
    assert "access_code" not in body[0]
    assert "supersecret" not in json.dumps(body)


def test_editing_without_access_code_keeps_the_stored_one(client):
    client.post("/api/printers", json={
        "id": "n1", "name": "n", "ip": "1.1.1.1", "sn": "SN1", "access_code": "keepme"
    })
    # the dialog cannot show the code, so it submits it blank
    res = client.post("/api/printers", json={
        "id": "n1", "name": "renamed", "ip": "1.1.1.1", "sn": "SN1", "access_code": ""
    })
    assert res.status_code == 200
    stored = main_module.read_printers()["n1"]
    assert stored.access_code == "keepme"
    assert stored.name == "renamed"


def test_camera_url_endpoint_returns_full_rtsp_url(client):
    client.post("/api/printers", json={
        "id": "n1", "name": "n", "ip": "10.0.0.9", "sn": "SN1", "access_code": "abcd1234"
    })
    body = client.get("/api/printers/n1/camera/url").json()
    assert body["url"] == "rtsps://bblp:abcd1234@10.0.0.9:322/streaming/live/1"


def test_settings_never_returns_the_token(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    client.post("/api/settings", json={"host": "0.0.0.0", "port": 8000, "auth_token": "hunter2"})
    client.post("/api/login", json={"token": "hunter2"})
    body = client.get("/api/settings").json()
    assert body["auth_required"] is True
    assert "auth_token" not in body
    assert "hunter2" not in json.dumps(body)


def test_auth_blocks_api_without_the_token(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    client.post("/api/settings", json={"host": "0.0.0.0", "port": 8000, "auth_token": "hunter2"})

    assert client.get("/api/printers").status_code == 401
    assert client.post("/api/login", json={"token": "wrong"}).status_code == 401

    assert client.post("/api/login", json={"token": "hunter2"}).status_code == 200
    # the cookie set by the login is reused for subsequent calls
    assert client.get("/api/printers").status_code == 200
    assert client.get("/VERSION").status_code == 200  # open path


def test_clear_auth_disables_the_token(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    client.post("/api/settings", json={"host": "0.0.0.0", "port": 8000, "auth_token": "hunter2"})
    client.post("/api/login", json={"token": "hunter2"})
    client.post("/api/settings", json={"host": "0.0.0.0", "port": 8000, "clear_auth": True})
    assert client.get("/api/settings").json()["auth_required"] is False


# ------------------------------------------------------------ uploads


def test_uploads_are_listed_and_deletable(client):
    client.post("/api/stage-upload", files={"file": ("a.3mf", b"PK\x03\x04data")})
    body = client.get("/api/uploads").json()
    assert [e["filename"] for e in body] == ["a.3mf"]
    assert body[0]["size"] == 8

    assert client.delete("/api/uploads/a.3mf").status_code == 200
    assert client.get("/api/uploads").json() == []
    assert client.delete("/api/uploads/a.3mf").status_code == 404


def test_upload_size_limit(client, monkeypatch):
    monkeypatch.setattr(main_module, "MAX_UPLOAD_BYTES", 10)
    res = client.post("/api/stage-upload", files={"file": ("big.3mf", b"x" * 50)})
    assert res.status_code == 413
    # nothing half-written is left behind
    assert client.get("/api/uploads").json() == []


def test_prune_uploads_removes_old_files(client, monkeypatch, tmp_path):
    client.post("/api/stage-upload", files={"file": ("old.3mf", b"data")})
    path = os.path.join(main_module.UPLOAD_DIR, "old.3mf")
    old = time.time() - (main_module.STAGED_MAX_AGE + 60)
    os.utime(path, (old, old))
    assert main_module.prune_uploads() == 1
    assert client.get("/api/uploads").json() == []


# -------------------------------------------------------- clear error


def test_clear_error_tries_print_error_and_hms(node, client, monkeypatch):
    main_module.latest_reports[node] = {
        "gcode_state": "FAILED", "print_error": 83902467, "subtask_id": "42",
        "hms": [{"attr": 83887104, "code": 65539}],
    }

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    res = client.post(f"/api/printers/{node}/clear-error")
    assert res.status_code == 200
    body = res.json()
    assert body["cleared"] is False  # state never left FAILED in this stub
    commands = [s[2]["print"]["command"] for s in client.sent if s[0] == "publish"]
    assert "clean_print_error" in commands
    assert "idle_ignore" in commands


def test_print_remote_does_not_block_on_a_stale_failed_state(node, client, monkeypatch):
    # FAILED but no print_error and no HMS -> leftover state, must NOT 409
    plate_stub = lambda ip, code, path: [1]  # noqa: E731
    monkeypatch.setattr(main_module, "detect_plate_indices", plate_stub)
    main_module.latest_reports[node] = {"gcode_state": "FAILED", "print_error": 0, "hms": []}

    async def fake_send(fn, *args):
        if fn is plate_stub:
            return [1]
        return True

    monkeypatch.setattr(main_module, "run_blocking", fake_send)
    res = client.post(f"/api/printers/{node}/print-remote", json={"path": "/benchy.3mf"})
    # it gets past the FAILED gate (the job simply did not start in this stub)
    assert res.status_code != 409 or "FAILED state" not in res.json().get("detail", "")
    assert res.status_code in (200, 502)


# ------------------------------------------------------- print preview


SLICE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<config><plate>
  <metadata key="index" value="4"/>
  <metadata key="prediction" value="5256"/>
  <metadata key="weight" value="56.17"/>
  <object identify_id="60" name="Cube" skipped="false"/>
  <object identify_id="112" name="Cube" skipped="true"/>
  <filament id="1" tray_info_idx="GFA01" type="PLA" color="#000000" used_m="17.69" used_g="56.17"/>
</plate></config>"""


def test_parse_slice_info():
    plates = main_module.parse_slice_info(SLICE_XML)
    assert len(plates) == 1
    plate = plates[0]
    assert plate["index"] == 4
    assert plate["time_sec"] == 5256
    assert plate["weight_g"] == 56.17
    assert plate["filaments"][0]["type"] == "PLA"
    assert plate["filaments"][0]["color"] == "000000"
    assert [o["id"] for o in plate["objects"]] == [60, 112]
    assert plate["objects"][0]["name"] == "Cube"
    assert plate["objects"][1]["skipped"] is True


def test_plan_endpoint(node, client, monkeypatch):
    main_module.latest_reports[node] = {
        "ams": {"ams": [{"id": 0, "tray": [
            {"id": 0, "tray_type": "PLA", "tray_color": "FFFFFFFF", "remain": 90},
            {"id": 1, "tray_type": "PETG", "tray_color": "161616FF", "remain": -1},
        ]}]},
        "vt_tray": {"tray_type": "PLA", "tray_color": "00AE42FF"},
    }
    monkeypatch.setattr(main_module, "read_remote_zip_entries",
                        lambda ip, code, path, names: {names[0]: SLICE_XML.encode()})
    body = client.get(f"/api/printers/{node}/files/plan", params={"path": "/004.gcode.3mf"}).json()
    assert body["name"] == "004.gcode.3mf"
    assert body["plates"][0]["index"] == 4
    assert [t["index"] for t in body["trays"]] == [0, 1]
    assert body["trays"][0]["color"] == "FFFFFF"
    assert body["external"]["index"] == 255
    assert body["external"]["color"] == "00AE42"


FIXTURE_3MF = pathlib.Path(__file__).parent / "fixtures" / "Cube_PLA_13m23s.gcode.3mf"


def test_print_objects_returns_slice_info_ids(node, client, monkeypatch):
    """The objects endpoint hands back slice_info identify_ids, not bbox ids."""
    contents = {}
    with zipfile.ZipFile(FIXTURE_3MF) as archive:
        contents["Metadata/plate_1.json"] = archive.read("Metadata/plate_1.json")
        contents["Metadata/slice_info.config"] = archive.read("Metadata/slice_info.config")
    monkeypatch.setattr(
        main_module, "read_remote_zip_entries",
        lambda ip, code, path, names: {n: contents[n] for n in names if n in contents},
    )
    body = client.get(
        f"/api/printers/{node}/files/objects",
        params={"path": "/Cube_PLA_13m23s.gcode.3mf", "plate": 1},
    ).json()
    assert body["plate"] == 1
    assert [o["id"] for o in body["objects"]] == [60, 112, 134, 156, 178]
    assert [o["plate_id"] for o in body["objects"]] == [92, 191, 192, 193, 196]


def test_plate_endpoint_serves_png(node, client, monkeypatch):
    monkeypatch.setattr(main_module, "read_remote_zip_entries",
                        lambda ip, code, path, names: {names[0]: b"\x89PNGdata"})
    res = client.get(f"/api/printers/{node}/files/plate", params={"path": "/x.3mf", "plate": 4})
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    assert res.content == b"\x89PNGdata"


def test_plate_endpoint_404_when_missing(node, client, monkeypatch):
    monkeypatch.setattr(main_module, "read_remote_zip_entries",
                        lambda ip, code, path, names: {n: None for n in names})
    res = client.get(f"/api/printers/{node}/files/plate", params={"path": "/x.3mf", "plate": 9})
    assert res.status_code == 404


def test_print_remote_includes_ams_mapping(node, client, monkeypatch):
    plate_stub = lambda ip, code, path: [1]  # noqa: E731
    monkeypatch.setattr(main_module, "detect_plate_indices", plate_stub)
    main_module.latest_reports[node] = {"gcode_state": "PREPARE"}

    async def fake_send(fn, *args):
        if fn is plate_stub:
            return [1]
        return fn(*args)

    monkeypatch.setattr(main_module, "run_blocking", fake_send)
    res = client.post(f"/api/printers/{node}/print-remote",
                      json={"path": "/benchy.3mf", "ams_mapping": [2, 255]})
    assert res.status_code == 200
    assert last_publish(client)["print"]["ams_mapping"] == [2, 255]


# ------------------------------------------------- pre-armed object skips


def print_commands(client):
    """Every `print` payload published during the request, in order."""
    return [s[2]["print"] for s in client.sent if s[0] == "publish" and "print" in s[2]]


def test_print_remote_injects_pre_armed_skip(node, client, monkeypatch):
    """A selection armed before the start is published by the backend itself."""
    monkeypatch.setattr(main_module, "detect_plate_indices", lambda ip, code, path: [1])
    main_module.latest_reports[node] = {"gcode_state": "RUNNING"}
    res = client.post(f"/api/printers/{node}/print-remote",
                      json={"path": "/benchy.3mf", "skip_object_ids": [60, 112]})
    assert res.status_code == 200
    assert res.json()["skipped"] == [60, 112]
    cmds = print_commands(client)
    # the job is started first, then the skip is injected on top of it
    assert [c["command"] for c in cmds[-2:]] == ["project_file", "skip_objects"]
    assert cmds[-1]["obj_list"] == [60, 112]


def test_print_remote_without_ids_sends_no_skip(node, client, monkeypatch):
    monkeypatch.setattr(main_module, "detect_plate_indices", lambda ip, code, path: [1])
    main_module.latest_reports[node] = {"gcode_state": "RUNNING"}
    res = client.post(f"/api/printers/{node}/print-remote",
                      json={"path": "/benchy.3mf", "skip_object_ids": []})
    assert res.status_code == 200
    assert res.json()["skipped"] == []
    assert "skip_objects" not in [c["command"] for c in print_commands(client)]

    client.sent.clear()
    res = client.post(f"/api/printers/{node}/print-remote", json={"path": "/benchy.3mf"})
    assert res.status_code == 200
    assert res.json()["skipped"] == []
    assert "skip_objects" not in [c["command"] for c in print_commands(client)]


def test_pre_skip_wait_is_bounded_when_never_running(node, client, monkeypatch):
    """A printer that stays in PREPARE must not hang the print-start request."""
    monkeypatch.setattr(main_module, "detect_plate_indices", lambda ip, code, path: [1])
    monkeypatch.setattr(main_module, "PRINT_SKIP_INJECT_TIMEOUT", 0.2)
    monkeypatch.setattr(main_module, "PRINT_SKIP_POLL_INTERVAL", 0.05)
    main_module.latest_reports[node] = {"gcode_state": "PREPARE"}  # never RUNNING

    started = time.monotonic()
    res = client.post(f"/api/printers/{node}/print-remote",
                      json={"path": "/benchy.3mf", "skip_object_ids": [156]})
    elapsed = time.monotonic() - started
    assert res.status_code == 200          # a started print is never turned into an error
    assert res.json()["skipped"] == [156]  # the firmware queues it, so it is still sent
    assert elapsed < 5
    assert print_commands(client)[-1]["obj_list"] == [156]


def test_pre_skip_waits_for_running_before_publishing(node, client, monkeypatch):
    """The skip is held back while the printer is only preparing the job."""
    monkeypatch.setattr(main_module, "detect_plate_indices", lambda ip, code, path: [1])
    monkeypatch.setattr(main_module, "PRINT_SKIP_INJECT_TIMEOUT", 5.0)
    monkeypatch.setattr(main_module, "PRINT_SKIP_POLL_INTERVAL", 0.05)
    main_module.latest_reports[node] = {"gcode_state": "PREPARE"}

    def go_running():
        main_module.latest_reports[node] = {"gcode_state": "RUNNING"}

    threading.Timer(0.3, go_running).start()
    res = client.post(f"/api/printers/{node}/print-remote",
                      json={"path": "/benchy.3mf", "skip_object_ids": [156]})
    assert res.status_code == 200
    assert res.json()["skipped"] == [156]


def test_dispatch_print_injects_pre_armed_skip(client, monkeypatch):
    client.post(
        "/api/printers",
        json={"id": "n1", "name": "n", "ip": "1.1.1.1", "sn": "SN1", "access_code": "12345678"},
    )
    client.post("/api/stage-upload", files={"file": ("benchy.3mf", b"PK\x03\x04")})
    monkeypatch.setattr(main_module, "upload_3mf_file", lambda ip, code, path: True)
    monkeypatch.setattr(main_module, "local_plate_indices", lambda path: [1])
    main_module.latest_reports["n1"] = {"gcode_state": "RUNNING"}

    res = client.post("/api/dispatch-print", json={
        "printer_id": "n1", "filename": "benchy.3mf", "skip_object_ids": [7, 9],
    })
    assert res.status_code == 200
    assert res.json()["skipped"] == [7, 9]
    assert print_commands(client)[-1]["obj_list"] == [7, 9]


def test_print_remote_rejects_bad_skip_ids(node, client, monkeypatch):
    monkeypatch.setattr(main_module, "detect_plate_indices", lambda ip, code, path: [1])
    main_module.latest_reports[node] = {"gcode_state": "RUNNING"}
    client.sent.clear()
    assert client.post(f"/api/printers/{node}/print-remote",
                       json={"path": "/benchy.3mf", "skip_object_ids": ["a"]}).status_code == 422
    assert client.post(f"/api/printers/{node}/print-remote",
                       json={"path": "/benchy.3mf", "skip_object_ids": [1.5]}).status_code == 422
    assert client.post(f"/api/printers/{node}/print-remote",
                       json={"path": "/benchy.3mf",
                             "skip_object_ids": list(range(65))}).status_code == 422
    assert not [s for s in client.sent if s[0] == "publish"]   # rejected before touching the printer


# ---------------------------------------------------- filament writing


def test_set_filament_writes_tray_metadata(node, client):
    res = client.post(f"/api/printers/{node}/filament", json={
        "ams_id": 0, "tray_id": 2, "tray_type": "PETG", "color": "#ff8800",
        "remaining": 55, "setting_id": "GFG00", "sub_brands": "HF", "temp_min": 240, "temp_max": 260,
    })
    assert res.status_code == 200
    payload = last_publish(client)["print"]
    # OrcaSlicer shape: flat fields directly on `print`
    assert payload["command"] == "ams_filament_setting"
    assert payload["ams_id"] == 0 and payload["slot_id"] == 2 and payload["tray_id"] == 2
    assert payload["tray_type"] == "PETG"
    assert payload["tray_color"] == "FF8800FF"   # RGBA, no '#'
    assert payload["tray_info_idx"] == "GFG00"
    assert payload["setting_id"] == "GFG00"
    assert payload["nozzle_temp_min"] == 240
    assert "tray_info" not in payload


def test_set_filament_needs_no_thermal_confirmation(node, client):
    # metadata only -> must work without confirm_thermal
    res = client.post(f"/api/printers/{node}/filament", json={"tray_type": "PLA", "color": "#00AE42"})
    assert res.status_code == 200
    assert last_publish(client)["print"]["tray_info_idx"] == "GFA00"   # PLA system id


def test_external_spool_maps_to_tray_254(node, client):
    client.post(f"/api/printers/{node}/filament", json={"ams_id": 255, "tray_id": 0, "tray_type": "PLA"})
    assert last_publish(client)["print"]["tray_id"] == 254


def test_filaments_catalog_exposes_rfid_ids(client):
    """The dashboard's material list is served with RFID-verified system ids."""
    res = client.get("/api/filaments")
    assert res.status_code == 200
    data = res.json()
    ids = {f["key"]: f["id"] for f in data["filaments"]}
    assert ids["PLA Basic"] == "GFA00"
    assert ids["PETG Basic"] == "GFG00"
    assert ids["ASA"] == "GFB01"
    assert data["categories"][0] == "PLA"
    for f in data["filaments"]:
        assert f["id"] and f["type"] and f["category"]
        assert f["min"] < f["max"]


# --------------------------------------------------------------- updates


def test_update_endpoint(client, monkeypatch):
    monkeypatch.setattr(main_module.updater, "check_for_update",
                        lambda: {"current": "1.0.0", "latest": "1.1.0", "update_available": True})
    res = client.get("/api/update")
    assert res.status_code == 200
    assert res.json()["latest"] == "1.1.0"


def test_update_install_refuses_when_current(client, monkeypatch):
    monkeypatch.setattr(main_module.updater, "check_for_update",
                        lambda: {"update_available": False, "error": ""})
    res = client.post("/api/update/install")
    assert res.status_code == 409


def test_update_install_downloads_and_launches(client, monkeypatch):
    calls = {}

    def fake_download(dest, url, expect_size=0):
        calls["download"] = (dest, url, expect_size)
        return "C:/tmp/s.exe"

    monkeypatch.setattr(main_module.updater, "check_for_update",
                        lambda: {"update_available": True, "asset_url": "http://x/y.exe",
                                 "latest": "v1.1.0", "asset_size": 4096})
    monkeypatch.setattr(main_module.updater, "download_installer", fake_download)
    monkeypatch.setattr(main_module.updater, "launch_installer",
                        lambda path: calls.setdefault("launch", path))
    res = client.post("/api/update/install")
    assert res.status_code == 200
    assert res.json()["started"] is True
    assert calls["download"][1] == "http://x/y.exe"
    # the published asset size is forwarded so a truncated file is caught
    assert calls["download"][2] == 4096
    assert calls["launch"] == "C:/tmp/s.exe"


def test_update_download_only_stages_the_installer(client, monkeypatch, tmp_path):
    """The manual flow: fetch the file, do not launch anything."""
    staged = tmp_path / "BambuFleetManagerSetup.exe"
    staged.write_bytes(b"x" * 128)
    launched = []

    monkeypatch.setattr(main_module.updater, "check_for_update",
                        lambda: {"update_available": True, "asset_url": "http://x/y.exe",
                                 "latest": "v1.2.0", "asset_size": 128})
    monkeypatch.setattr(main_module.updater, "download_installer",
                        lambda dest, url, expect_size=0: str(staged))
    monkeypatch.setattr(main_module.updater, "launch_installer",
                        lambda path: launched.append(path))

    res = client.post("/api/update/download")
    assert res.status_code == 200
    body = res.json()
    assert body["downloaded"] is True
    assert body["version"] == "v1.2.0"
    assert body["size"] == 128
    # downloading must never start the installer or quit the app
    assert launched == []


def test_update_download_refuses_when_current(client, monkeypatch):
    monkeypatch.setattr(main_module.updater, "check_for_update",
                        lambda: {"update_available": False, "error": ""})
    assert client.post("/api/update/download").status_code == 409


def test_update_run_requires_a_staged_file(client, monkeypatch):
    monkeypatch.setattr(main_module.updater, "check_for_update",
                        lambda: {"latest": "v1.2.0", "asset_size": 128})
    monkeypatch.setattr(main_module.updater, "staged_installer", lambda dest: None)
    res = client.post("/api/update/run")
    assert res.status_code == 409
    assert "Download" in res.json()["detail"]


def test_update_run_refuses_an_incomplete_download(client, monkeypatch, tmp_path):
    partial = tmp_path / "BambuFleetManagerSetup.exe"
    partial.write_bytes(b"x" * 10)
    launched = []
    monkeypatch.setattr(main_module.updater, "check_for_update",
                        lambda: {"latest": "v1.2.0", "asset_size": 4096})
    monkeypatch.setattr(main_module.updater, "staged_installer", lambda dest: str(partial))
    monkeypatch.setattr(main_module.updater, "launch_installer",
                        lambda path: launched.append(path))
    res = client.post("/api/update/run")
    assert res.status_code == 409
    assert launched == []


def test_update_run_launches_the_staged_installer(client, monkeypatch, tmp_path):
    staged = tmp_path / "BambuFleetManagerSetup.exe"
    staged.write_bytes(b"x" * 4096)
    launched = []
    monkeypatch.setattr(main_module.updater, "check_for_update",
                        lambda: {"latest": "v1.2.0", "asset_size": 4096})
    monkeypatch.setattr(main_module.updater, "staged_installer", lambda dest: str(staged))
    monkeypatch.setattr(main_module.updater, "launch_installer",
                        lambda path: launched.append(path))
    res = client.post("/api/update/run")
    assert res.status_code == 200
    assert res.json()["started"] is True
    assert launched == [str(staged)]


def test_update_installer_copy_serves_the_staged_file(client, monkeypatch, tmp_path):
    staged = tmp_path / "BambuFleetManagerSetup.exe"
    staged.write_bytes(b"MZ" + b"\x00" * 32)
    monkeypatch.setattr(main_module.updater, "staged_installer", lambda dest: str(staged))
    res = client.get("/api/update/installer")
    assert res.status_code == 200
    assert res.content == b"MZ" + b"\x00" * 32
    assert "attachment" in res.headers["content-disposition"]
    assert "BambuFleetManagerSetup.exe" in res.headers["content-disposition"]


def test_update_installer_copy_404_when_absent(client, monkeypatch):
    monkeypatch.setattr(main_module.updater, "staged_installer", lambda dest: None)
    assert client.get("/api/update/installer").status_code == 404


def test_shutdown_hooks_run():
    ran = []
    main_module._shutdown_hooks.clear()
    main_module.register_shutdown_hook(lambda: ran.append("hook"))
    main_module.run_shutdown_hooks()
    assert ran == ["hook"]
    main_module._shutdown_hooks.clear()
