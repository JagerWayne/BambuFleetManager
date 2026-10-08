import pytest

from backend.models import LightCommand, PrintDispatchCommand, PrinterConfig, SpeedCommand


def test_printer_config_validation():
    p = PrinterConfig(
        id="node_01",
        name="Print Farm Node A",
        ip="192.168.1.50",
        sn="00M00A123456789",
        access_code="12345678",
    )
    assert p.bed_type == "textured_pei"
    assert p.ams_count == 1
    assert p.sn == "00M00A123456789"


def test_printer_config_normalizes_serial_case():
    p = PrinterConfig(id="n", name="n", ip="10.0.0.1", sn=" 00m00a123 ", access_code="12345678")
    assert p.sn == "00M00A123"


def test_speed_command_boundaries():
    valid = SpeedCommand(speed_level=3)
    assert valid.speed_level == 3

    with pytest.raises(Exception):
        SpeedCommand(speed_level=5)  # Max allowed is 4


def test_speed_command_rejects_zero():
    with pytest.raises(Exception):
        SpeedCommand(speed_level=0)


def test_dispatch_command_defaults():
    cmd = PrintDispatchCommand(printer_id="node_01", filename="benchy.3mf")
    assert cmd.plate_index == 1
    assert cmd.bed_levelling is True
    assert cmd.flow_cali is True
    assert cmd.vibration_cali is True
    assert cmd.timelapse is True
    assert cmd.use_ams is True


def test_dispatch_command_rejects_path_traversal():
    with pytest.raises(Exception):
        PrintDispatchCommand(printer_id="node_01", filename="../../etc/passwd.3mf")


def test_dispatch_command_rejects_foreign_extension():
    with pytest.raises(Exception):
        PrintDispatchCommand(printer_id="node_01", filename="payload.exe")


def test_light_command_accepts_both_states():
    assert LightCommand(state=True).state is True
    assert LightCommand(state=False).state is False


def test_server_settings_defaults_and_bounds():
    from backend.models import ServerSettings

    assert ServerSettings().port == 8000
    assert ServerSettings().host == "0.0.0.0"
    assert ServerSettings(host="127.0.0.1", port=9000).port == 9000

    with pytest.raises(Exception):
        ServerSettings(port=0)
    with pytest.raises(Exception):
        ServerSettings(port=70000)
    with pytest.raises(Exception):
        ServerSettings(host="   ")
