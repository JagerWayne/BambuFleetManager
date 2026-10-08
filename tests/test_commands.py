"""Payload-shape tests for every command in the registry."""

import pytest

from backend.commands import (
    SPEED_PROFILES,
    available_commands,
    build_command,
    command_groups,
    risk_class,
)


def test_every_command_builds_without_params():
    for name in available_commands():
        payload = build_command(name)
        assert isinstance(payload, dict) and payload
        channel = next(iter(payload))
        assert channel in {"print", "system", "info", "camera", "pushing"}
        assert "command" in payload[channel]


def test_unknown_command_raises():
    with pytest.raises(KeyError):
        build_command("self_destruct")


def test_groups_cover_registry():
    groups = command_groups()
    assert set(groups) == {"print", "system", "info", "camera", "pushing"}
    assert sum(len(v) for v in groups.values()) == len(available_commands())


@pytest.mark.parametrize(
    "level,name",
    [(1, "silent"), (2, "standard"), (3, "sport"), (4, "ludicrous")],
)
def test_speed_profiles(level, name):
    assert SPEED_PROFILES[level] == name
    payload = build_command("speed", {"level": level})["print"]
    # The firmware only honours the numeric level; names are silently ignored.
    assert payload["param"] == str(level)
    assert "param_id" not in payload


def test_speed_profile_rejects_unknown_level():
    # Out-of-range falls back to Standard rather than sending garbage.
    assert build_command("speed", {"level": 0})["print"]["param"] == "2"
    assert build_command("speed", {"level": 9})["print"]["param"] == "2"


def test_print_control_commands():
    for name in ("pause", "resume", "stop"):
        assert build_command(name)["print"]["command"] == name


def test_skip_objects_coerces_ids():
    payload = build_command("skip_objects", {"object_ids": ["2", 3]})["print"]
    assert payload["obj_list"] == [2, 3]


def test_ams_feed_and_unload_targets():
    assert build_command("ams_feed")["print"]["target"] == 1
    assert build_command("ams_unload")["print"]["target"] == 2


def test_ams_tray_select_carries_unit_and_tray():
    payload = build_command("ams_tray_select", {"ams_id": 2, "tray_id": 5})["print"]
    assert payload["ams_id"] == 2
    assert payload["tray_id"] == 5
    assert payload["select_tray"] is True


def test_ams_filament_setting_shape():
    """Flat OrcaSlicer shape: fields on `print`, RGBA colour, system filament id."""
    payload = build_command("ams_filament_setting",
                            {"ams_id": 0, "tray_id": 2, "tray_type": "PETG", "color": "#FF0000"})["print"]
    assert payload["command"] == "ams_filament_setting"
    assert payload["ams_id"] == 0 and payload["slot_id"] == 2 and payload["tray_id"] == 2
    assert payload["tray_info_idx"] == "GFG00"      # PETG system id
    assert payload["setting_id"] == "GFG00"
    assert payload["tray_color"] == "FF0000FF"      # no '#', RGBA
    assert payload["tray_type"] == "PETG"
    assert "tray_info" not in payload


def test_ams_filament_setting_external_spool():
    payload = build_command("ams_filament_setting",
                            {"ams_id": 255, "tray_id": 0, "tray_type": "PLA", "color": "#00AE42"})["print"]
    assert payload["tray_id"] == 254                 # external spool
    assert payload["tray_color"] == "00AE42FF"


def test_flow_calibration_modes():
    enable = build_command("flow_calibration", {"mode": 1, "state": "enable"})["print"]
    assert enable["mode"] == 1 and enable["sub_option"] == "enable"
    disable = build_command("flow_calibration", {"mode": 0, "state": "disable"})["print"]
    assert disable["mode"] == 0 and disable["sub_option"] == "disable"


def test_light_modes():
    on = build_command("light", {"mode": "on"})["system"]
    assert on["led_node"] == "chamber_light" and on["led_mode"] == "on"
    # Firmware rejects a payload without the timing block.
    for field in ("led_on_time", "led_off_time", "loop_times", "interval_time"):
        assert field in on


def test_light_flashing_sets_loop_count():
    flashing = build_command("light", {"mode": "flashing", "times": 5})["system"]
    assert flashing["loop_times"] == 5
    assert flashing["led_mode"] == "flashing"


def test_light_node_is_configurable():
    payload = build_command("light", {"node": "work_light", "mode": "off"})["system"]
    assert payload["led_node"] == "work_light"


def test_camera_enable_and_disable():
    assert build_command("camera", {"action": "enable"})["camera"]["command"] == "push"
    assert build_command("camera", {"action": "disable"})["camera"]["command"] == "stop"


def test_reboot_module():
    assert build_command("restart_module", {"module": "esp32"})["system"]["module"] == "esp32"


def test_print_option_only_includes_supplied_fields():
    payload = build_command("print_option", {"nozzle_temp": 240})["print"]
    assert payload["print_option"] == {"nozzle_temp": 240}


# ------------------------------------------------------------ motion / g-code


def test_home_payloads():
    assert build_command("home")["print"]["param"] == "G28\n"
    assert build_command("home", {"axis": "z"})["print"]["param"] == "G28 Z\n"
    assert build_command("home", {"axis": "Q"})["print"]["param"] == "G28\n"  # unknown axis -> home all


def test_jog_payload_is_relative():
    payload = build_command("jog", {"axis": "x", "distance": 10})["print"]
    assert payload["command"] == "gcode_line"
    assert payload["param"] == "G91\nG1 X10 F3000\nG90\n"


def test_jog_negative_and_feedrate():
    payload = build_command("jog", {"axis": "Z", "distance": -0.1, "feedrate": 600})["print"]
    assert payload["param"] == "G91\nG1 Z-0.1 F600\nG90\n"


def test_jog_rejects_unknown_axis():
    with pytest.raises(ValueError):
        build_command("jog", {"axis": "A", "distance": 1})


def test_extrude_payloads():
    assert build_command("extrude", {"amount": 5})["print"]["param"] == "G91\nG1 E5 F300\nG90\n"
    assert build_command("extrude", {"amount": -5})["print"]["param"] == "G91\nG1 E-5 F300\nG90\n"


def test_motion_risk_classes():
    assert risk_class("jog") == "motion"
    assert risk_class("home") == "home"
    assert risk_class("extrude") == "thermal"
    assert risk_class("light") == "safe"


def test_bed_setpoint_uses_gcode_not_the_rejected_mqtt_command():
    """X1 firmware answers set_bed_temp with FAIL/ERROR STATE; M140 works."""
    payload = build_command("set_bed_temp", {"temp": 60})["print"]
    assert payload["command"] == "gcode_line"
    assert payload["param"] == "M140 S60\n"


def test_nozzle_setpoint_uses_gcode():
    payload = build_command("set_nozzle_temp", {"temp": 220})["print"]
    assert payload["command"] == "gcode_line"
    assert payload["param"] == "M104 S220\n"
