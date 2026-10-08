"""Camera URL/proxy tests (no hardware involved)."""

import pytest
import requests

from backend import camera as camera_mod


class FakeResponse:
    def __init__(self, chunks, status=200, content_type="multipart/x-mixed-replace", raise_for_status=None):
        self._chunks = chunks
        self.status_code = status
        self.content = b"".join(chunks)
        self.headers = {"Content-Type": content_type}
        self.closed = False
        self._raise = raise_for_status

    def iter_content(self, size):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def close(self):
        self.closed = True


def test_urls():
    assert camera_mod.snapshot_url("192.168.1.151") == "http://192.168.1.151:6000/cache/snapshot.jpg"
    assert camera_mod.stream_url("192.168.1.151") == "http://192.168.1.151:6000/video/1"


def test_fetch_snapshot_returns_jpeg(monkeypatch):
    seen = {}

    def fake_get(url, timeout=None):
        seen["url"] = url
        seen["timeout"] = timeout
        return FakeResponse([b"\xff\xd8jpegbytes"], content_type="image/jpeg")

    monkeypatch.setattr(camera_mod.requests, "get", fake_get)
    frame = camera_mod.fetch_snapshot("10.0.0.5")
    assert frame == b"\xff\xd8jpegbytes"
    assert seen["url"] == "http://10.0.0.5:6000/cache/snapshot.jpg"
    assert seen["timeout"] == camera_mod.CAMERA_TIMEOUT


@pytest.mark.parametrize(
    "result",
    [
        FakeResponse([], status=404),
        FakeResponse([b"<html>camera disabled</html>"], status=200),
    ],
)
def test_fetch_snapshot_returns_none_on_bad_response(monkeypatch, result):
    monkeypatch.setattr(camera_mod.requests, "get", lambda *a, **k: result)
    assert camera_mod.fetch_snapshot("10.0.0.5") is None


def test_fetch_snapshot_returns_none_on_connection_error(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(camera_mod.requests, "get", boom)
    assert camera_mod.fetch_snapshot("10.0.0.5") is None


def test_open_stream_passes_through_content_type_and_chunks(monkeypatch):
    response = FakeResponse([b"part1", b"part2"], content_type="multipart/x-mixed-replace; boundary=x")
    monkeypatch.setattr(camera_mod.requests, "get", lambda *a, **k: response)

    content_type, chunks = camera_mod.open_stream("10.0.0.5")
    assert content_type.startswith("multipart/x-mixed-replace")
    assert b"".join(chunks) == b"part1part2"
    assert response.closed is True


def test_open_stream_propagates_http_errors(monkeypatch):
    response = FakeResponse([], status=502, raise_for_status=requests.HTTPError("502"))
    monkeypatch.setattr(camera_mod.requests, "get", lambda *a, **k: response)
    with pytest.raises(requests.HTTPError):
        camera_mod.open_stream("10.0.0.5")
