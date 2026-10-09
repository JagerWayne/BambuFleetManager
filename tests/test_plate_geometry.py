"""Unit tests for the sliced-plate geometry parser behind the skip bed."""

import pathlib
import zipfile

from backend import main as main_module

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "Cube_PLA_13m23s.gcode.3mf"


def _plate_json() -> str:
    with zipfile.ZipFile(FIXTURE) as archive:
        return archive.read("Metadata/plate_1.json").decode("utf-8")


def _slice_info() -> str:
    with zipfile.ZipFile(FIXTURE) as archive:
        return archive.read("Metadata/slice_info.config").decode("utf-8")


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


def test_fixture_skip_ids_come_from_slice_info():
    """The skip command needs slice_info identify_ids, not plate_json bbox ids."""
    geometry = main_module.parse_plate_geometry(_plate_json())
    plates = main_module.parse_slice_info(_slice_info())
    assert len(plates) == 1
    merged = main_module.merge_plate_object_ids(geometry, plates[0]["objects"])

    assert [o["id"] for o in merged] == [60, 112, 134, 156, 178]
    # The plate_N.json bbox ids must NOT be what ends up in id ...
    assert [o["id"] for o in merged] != [92, 191, 192, 193, 196]
    # ... but they are echoed as plate_id for reference.
    assert [o["plate_id"] for o in merged] == [92, 191, 192, 193, 196]
    assert all(o["bbox"] for o in merged)


def test_merge_plate_object_ids_falls_back_without_slice_objects():
    geometry = {"objects": [
        {"id": 92, "name": "", "bbox": [0, 0, 1, 1]},
        {"id": 191, "name": "geom", "bbox": [0, 0, 1, 1]},
    ]}
    merged = main_module.merge_plate_object_ids(geometry, [{"id": 60, "name": "slice"}])

    assert [o["id"] for o in merged] == [60, 191]
    assert merged[0]["name"] == "slice"   # blank geometry name -> slice name
    assert merged[1]["name"] == "geom"    # geometry name is preferred
