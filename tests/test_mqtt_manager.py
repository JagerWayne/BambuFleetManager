"""Publisher-level tests: sequence_id stamping (the firmware requires it)."""

import threading

from backend.mqtt_manager import BambuMqttClient


def make_client(seq=0):
    client = BambuMqttClient.__new__(BambuMqttClient)  # no __init__ / no socket
    client._seq = seq
    client._lock = threading.Lock()
    return client


def test_stamp_adds_missing_ids():
    stamped = make_client().stamp_sequence_ids({"system": {"command": "ledctrl", "led_mode": "on"}})
    assert stamped["system"]["sequence_id"] == "1"


def test_stamp_does_not_mutate_the_input():
    payload = {"system": {"command": "ledctrl", "led_mode": "on"}}
    make_client().stamp_sequence_ids(payload)
    assert "sequence_id" not in payload["system"]


def test_stamp_keeps_existing_id():
    stamped = make_client(5).stamp_sequence_ids({"print": {"command": "project_file", "sequence_id": "1"}})
    assert stamped["print"]["sequence_id"] == "1"


def test_stamp_handles_multiple_channels():
    stamped = make_client().stamp_sequence_ids({"print": {"command": "pause"}, "camera": {"command": "push"}})
    assert stamped["print"]["sequence_id"] == "1"
    assert stamped["camera"]["sequence_id"] == "2"


def test_stamp_ignores_blocks_without_a_command():
    payload = {"print": {"gcode_state": "RUNNING"}}
    assert make_client().stamp_sequence_ids(payload) == payload
