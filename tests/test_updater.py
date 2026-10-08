"""Tests for the update checker and the /api/update endpoints."""

import os

import requests

from backend import updater


class FakeResponse:
    def __init__(self, status=200, payload=None, chunks=b"data"):
        self.status_code = status
        self._payload = payload
        self._chunks = chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1):
        yield self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_parse_version_variants():
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("1.2") == (1, 2, 0)
    assert updater.parse_version("1.2.3-beta1") == (1, 2, 3)
    assert updater.parse_version("") == (0, 0, 0)


def test_is_newer():
    assert updater.is_newer("1.1.0", "1.0.0")
    assert not updater.is_newer("1.0.0", "1.0.0")
    assert not updater.is_newer("0.9.9", "1.0.0")


def test_current_version_matches_file():
    with open(os.path.join(updater.RESOURCE_DIR, "VERSION"), encoding="utf-8") as fh:
        assert updater.current_version() == fh.read().strip()


def test_check_for_update_finds_installer(monkeypatch):
    release = {
        "tag_name": "v9.9.9",
        "name": "Release 9.9.9",
        "body": "notes",
        "html_url": "https://example/release",
        "assets": [
            {"name": "readme.txt", "browser_download_url": "u0", "size": 1},
            {"name": updater.ASSET_NAME, "browser_download_url": "u1", "size": 42},
        ],
    }
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(200, release))
    report = updater.check_for_update()
    assert report["update_available"] is True
    assert report["latest"] == "v9.9.9"
    assert report["asset_url"] == "u1"
    assert report["asset_size"] == 42
    assert report["error"] == ""


def test_check_for_update_no_release(monkeypatch):
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(404))
    report = updater.check_for_update()
    assert report["update_available"] is False
    assert "No releases" in report["error"]


def test_check_for_update_network_error(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(updater.requests, "get", boom)
    report = updater.check_for_update()
    assert report["update_available"] is False
    assert "Could not reach GitHub" in report["error"]


def test_download_installer(monkeypatch, tmp_path):
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(200, chunks=b"setup"))
    path = updater.download_installer(str(tmp_path / "updates"), "http://x/y.exe")
    assert os.path.basename(path) == updater.ASSET_NAME
    with open(path, "rb") as fh:
        assert fh.read() == b"setup"
