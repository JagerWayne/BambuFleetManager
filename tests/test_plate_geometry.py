"""Unit tests for the sliced-plate geometry parser behind the skip bed."""

import pathlib
import zipfile

from backend import main as main_module

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "Cube_PLA_13m23s.gcode.3mf"


def _plate_json() -> str:
    with zipfile.ZipFile(FIXTURE) as archive:
        return archive.read("Metadata/plate_1.json").decode("utf-8")


def test_parse_plate_geometry_from_fixture():
    geometry = main_module.parse_plate_geometry(_plate_json())

    assert geometry["bed"] == [256, 256]
    assert geometry["bbox_all"] == [45.0, 45.0, 200.0, 200.0]
    assert len(geometry["objects"]) == 5

    first = geometry["objects"][0]
    assert first["id"] == 92
    assert first["name"] == "Cube"
    assert first["bbox"] == [190.0, 190.0, 200.0, 200.0]
    assert all(o["name"] == "Cube" for o in geometry["objects"])


def test_parse_plate_geometry_rejects_non_json():
    geometry = main_module.parse_plate_geometry("not json at all")
    assert geometry["bed"] == [256, 256]
    assert geometry["bbox_all"] == []
    assert geometry["objects"] == []


def test_parse_plate_geometry_skips_bad_objects():
    geometry = main_module.parse_plate_geometry(
        '{"bbox_all": [0, 0, 1, 1], "bbox_objects": ['
        '{"id": 1, "name": "ok", "bbox": [0, 0, 10, 10]},'
        '{"id": 2, "name": "no bbox"},'
        '{"name": "bad bbox", "bbox": [1, 2, 3]}]}'
    )
    assert [o["id"] for o in geometry["objects"]] == [1]
