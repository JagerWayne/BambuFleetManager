from backend.telemetry import normalize_gcode_state, parse_print_report

SAMPLE = {
    "gcode_state": "RUNNING",
    "mc_percent": 42,
    "mc_remaining_time": 37,
    "nozzle_temper": 219.6,
    "nozzle_target_temper": 220,
    "bed_temper": 59.4,
    "bed_target_temper": 60,
    "subtask_name": "benchy.3mf",
}


def test_normalize_gcode_state_aliases():
    assert normalize_gcode_state("RUNNING") == "running"
    assert normalize_gcode_state("pause") == "paused"
    assert normalize_gcode_state("PREPARE") == "preparing"
    assert normalize_gcode_state("unknown_state") == "unknown_state"
    assert normalize_gcode_state(None) == "unknown"


def test_parse_print_report_extracts_core_fields():
    report = parse_print_report(SAMPLE)
    assert report["status"] == "running"
    assert report["progress"] == 42
    assert report["remaining_sec"] == 37 * 60
    assert report["nozzle_temp"] == 220  # rounded
    assert report["nozzle_target"] == 220
    assert report["bed_temp"] == 59
    assert report["bed_target"] == 60
    assert report["job"] == "benchy.3mf"


def test_parse_print_report_clamps_progress():
    report = parse_print_report({"gcode_state": "IDLE", "mc_percent": 140})
    assert report["progress"] == 100


def test_parse_print_report_ignores_missing_fields():
    assert parse_print_report({"gcode_state": "IDLE"}) == {"status": "idle"}
    assert parse_print_report("not-a-dict") == {}
