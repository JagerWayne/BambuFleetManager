"""Tests for the update checker and the /api/update endpoints."""

import os

import pytest
import requests

from backend import updater


class FakeResponse:
    def __init__(self, status=200, payload=None, chunks=b"data", headers=None):
        self.status_code = status
        self._payload = payload
        self._chunks = chunks
        self.headers = headers if headers is not None else {}

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


def test_download_installer_accepts_a_matching_content_length(monkeypatch, tmp_path):
    monkeypatch.setattr(
        updater.requests, "get",
        lambda *a, **k: FakeResponse(200, chunks=b"setup", headers={"Content-Length": "5"}))
    path = updater.download_installer(str(tmp_path / "updates"), "http://x/y.exe")
    assert os.path.getsize(path) == 5


def test_download_installer_rejects_a_truncated_download(monkeypatch, tmp_path):
    """A short transfer must never be handed back as if it were the installer."""
    monkeypatch.setattr(
        updater.requests, "get",
        lambda *a, **k: FakeResponse(200, chunks=b"setup", headers={"Content-Length": "9999"}))
    dest = tmp_path / "updates"
    with pytest.raises(IOError):
        updater.download_installer(str(dest), "http://x/y.exe")
    # nothing left behind that could be run by mistake
    assert not (dest / updater.ASSET_NAME).exists()
    assert not (dest / (updater.ASSET_NAME + ".part")).exists()


def test_download_installer_falls_back_to_the_expected_size(monkeypatch, tmp_path):
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(200, chunks=b"setup"))
    dest = tmp_path / "updates"
    with pytest.raises(IOError):
        updater.download_installer(str(dest), "http://x/y.exe", 12345)
    assert not (dest / updater.ASSET_NAME).exists()


def test_download_installer_cleans_up_after_a_network_failure(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise requests.ConnectionError("dropped")

    monkeypatch.setattr(updater.requests, "get", boom)
    dest = tmp_path / "updates"
    with pytest.raises(requests.ConnectionError):
        updater.download_installer(str(dest), "http://x/y.exe")
    assert not (dest / updater.ASSET_NAME).exists()
    assert not (dest / (updater.ASSET_NAME + ".part")).exists()


def test_staged_installer(tmp_path):
    dest = str(tmp_path / "updates")
    assert updater.staged_installer(dest) is None
    os.makedirs(dest, exist_ok=True)
    target = os.path.join(dest, updater.ASSET_NAME)
    with open(target, "wb") as fh:
        fh.write(b"MZ")
    assert updater.staged_installer(dest) == target


def test_download_timeouts_are_not_the_metadata_timeout():
    """The ~45 MB asset needs a real read timeout, not the 15s API one."""
    assert updater._API_TIMEOUT == 15
    assert updater._DOWNLOAD_TIMEOUT[1] > updater._API_TIMEOUT
