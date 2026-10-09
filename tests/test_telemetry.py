"""Typed telemetry: drift is logged, never fatal."""

import logging

from backend import telemetry


def test_well_formed_report_parses():
    rep = telemetry.parse_report({
        "gcode_state": "RUNNING",
        "subtask_name": "cube.gcode.3mf",
        "layer_num": 42,
        "total_layer_num": 100,
        "mc_percent": 41.5,
        "nozzle_temper": 220,
        "bed_temper": 55,
        "hms": [],
        "print_error": 0,
    })
    assert rep is not None
    assert rep.gcode_state == "RUNNING"
    assert rep.layer_num == 42
    assert rep.mc_percent == 41.5


def test_unknown_fields_are_kept():
    rep = telemetry.parse_report({"gcode_state": "IDLE", "brand_new_field": {"a": 1}})
    assert rep is not None
    assert rep.brand_new_field == {"a": 1}


def test_retyped_field_is_logged_but_not_fatal(caplog):
    with caplog.at_level(logging.WARNING):
        rep = telemetry.parse_report({"gcode_state": "RUNNING", "layer_num": "forty-two"})
    assert rep is None
    assert caplog.records, "firmware drift should be logged"
    assert "untyped" in caplog.text


def test_non_dict_payload_is_reported(caplog):
    with caplog.at_level(logging.WARNING):
        assert telemetry.parse_report(["not", "a", "dict"]) is None


def test_missing_core_fields_lists_them():
    missing = telemetry.missing_core_fields({"some": "payload"})
    assert "gcode_state" in missing
    assert "layer_num" in missing
    assert telemetry.missing_core_fields({"gcode_state": "IDLE"}) == [
        name for name in telemetry.CORE_FIELDS if name != "gcode_state"
    ]


def test_parse_print_report_still_reduces_for_the_ui():
    out = telemetry.parse_print_report({
        "gcode_state": "RUNNING",
        "mc_percent": 250,
        "mc_remaining_time": 1.5,
        "nozzle_temper": 219.6,
        "subtask_name": "cube.gcode.3mf",
    })
    assert out["status"] == "running"
    assert out["progress"] == 100  # clamped
    assert out["remaining_sec"] == 90
    assert out["nozzle_temp"] == 220
    assert out["job"] == "cube.gcode.3mf"