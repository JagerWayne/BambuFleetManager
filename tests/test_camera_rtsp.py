"""RTSP camera tests: digest auth, SDP parsing and RTP depacketisation."""

import hashlib
import pytest
import struct

from backend import camera as cam
from backend.camera import RtspCamera, _digest_response, _parse_challenge, mjpeg_stream


# ------------------------------------------------------------------ digest

def test_parse_challenge():
    parsed = _parse_challenge('Digest realm="LIVE555 Streaming Media", nonce="abc123"')
    assert parsed["scheme"] == "digest"
    assert parsed["realm"] == "LIVE555 Streaming Media"
    assert parsed["nonce"] == "abc123"


def test_digest_response_without_qop_matches_rfc2069():
    challenge = {"scheme": "digest", "realm": "LIVE555 Streaming Media", "nonce": "abc123"}
    method, uri, password = "DESCRIBE", "rtsps://10.0.0.5:322/streaming/live/1", "f0240da4"
    md5 = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
    expected = md5(f"{md5(f'bblp:{challenge['realm']}:{password}')}:abc123:{md5(f'{method}:{uri}')}")
    header = _digest_response(challenge, method, uri, password)
    assert header.startswith("Digest ")
    assert f'response="{expected}"' in header
    assert 'username="bblp"' in header
    assert 'uri="' + uri + '"' in header


def test_digest_response_with_qop_adds_nc_and_cnonce():
    challenge = {"scheme": "digest", "realm": "x", "nonce": "n", "qop": "auth"}
    header = _digest_response(challenge, "SETUP", "rtsps://h/p", "pw")
    for field in ("qop=auth", "nc=", 'cnonce="', 'response="'):
        assert field in header


# --------------------------------------------------------------- sdp parse

SDP = (
    "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=rtsp stream server\r\n"
    "a=control:*\r\nm=video 0 RTP/AVP 96\r\n"
    "a=rtpmap:96 H264/90000\r\n"
    "a=fmtp:96 packetization-mode=1;profile-level-id=64C029;"
    "sprop-parameter-sets=Z2TAKawbGqBu,BASE64PPS=\r\n"
    "a=control:track1\r\n"
)


class FakeSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, _n):
        return self.chunks.pop(0) if self.chunks else b""

    def settimeout(self, _t):
        pass

    def close(self):
        pass


def test_sdp_extracts_sps_pps_and_track_url():
    body = SDP.encode()
    head = (b"RTSP/1.0 200 OK\r\nContent-Base: rtsps://10.0.0.5/streaming/live/1/\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode())
    camera = RtspCamera("10.0.0.5", "pw")
    camera.sock = FakeSocket([head + body])
    camera._challenge = {"scheme": "digest", "realm": "r", "nonce": "n"}
    info = camera.sdp()
    assert info["sps"] == "Z2TAKawbGqBu"
    assert info["pps"] == "BASE64PPS="
    # session-level a=control:* must not leak into the media control URL
    assert info["track_url"] == "rtsps://10.0.0.5:322/streaming/live/1/track1"


# ------------------------------------------------------------ depacketising

def rtp(payload, seq=1, ts=0):
    return struct.pack("!BBHII", 0x80, 26, seq, ts, 0x11223344) + payload


def interleaved(payload):
    body = rtp(payload)
    return b"$" + b"\x00" + struct.pack("!H", len(body)) + body


def test_h264_reassembles_single_nals_and_fu_a_fragments():
    sps = bytes([0x67, 0xAA, 0xBB])
    pps = bytes([0x68, 0xCC])
    idr = bytes([0x65, 0xDD, 0xEE, 0xFF, 0x11])
    payload = idr[1:]  # FU-A carries the payload only; the type lives in the FU header
    fu_indicator = bytes([0x7C])
    first = fu_indicator + bytes([0x80 | 0x05]) + payload[:2]
    second = fu_indicator + bytes([0x40 | 0x05]) + payload[2:]

    stream = b"".join(interleaved(p) for p in (sps, pps, first, second))
    camera = RtspCamera("10.0.0.5", "pw")
    camera.sock = FakeSocket([stream])

    got = []
    for nal in camera.h264():
        got.append(nal)
        if len(got) == 3:
            break
        if len(got) > 5:
            break

    assert got[0] == cam.START_CODE + sps
    assert got[1] == cam.START_CODE + pps
    assert got[2] == cam.START_CODE + idr
    assert got[2][4] & 0x1F == 5  # reconstructed NAL header is an IDR


def test_h264_expands_stap_a():
    sps = bytes([0x67, 0x01])
    pps = bytes([0x68, 0x02])
    stap = bytes([0x78]) + struct.pack("!H", len(sps)) + sps + struct.pack("!H", len(pps)) + pps
    camera = RtspCamera("10.0.0.5", "pw")
    camera.sock = FakeSocket([interleaved(stap)])
    got = []
    for nal in camera.h264():
        got.append(nal)
        if len(got) == 2:
            break
        if len(got) > 4:
            break
    assert got == [cam.START_CODE + sps, cam.START_CODE + pps]


def test_rtp_payload_strips_csrc_and_extension():
    # CC=2 and extension bit set
    header = struct.pack("!BBHII", 0x90 | 0x02, 26, 1, 0, 0x99)
    header += b"\x01\x02\x03\x04" * 2            # 2 CSRC words
    header += struct.pack("!HH", 0xBEDE, 1) + b"\x00\x00\x00\x00"  # extension
    payload = bytes([0x65, 0x01, 0x02])
    assert RtspCamera._jpeg_payload(header + payload) == payload


def test_mjpeg_stream_wraps_frames():
    out = b"".join(mjpeg_stream(iter([b"\xff\xd8a\xff\xd9", b"\xff\xd8b\xff\xd9"])))
    assert out.count(b"--frame") == 2
    assert b"Content-Type: image/jpeg" in out


# --------------------------------------------------------------- ffmpeg bridge

def test_rtsp_url_uses_documented_form():
    from backend import camera as c

    assert c.rtsp_url("192.168.1.50", "abcd1234") == \
        "rtsps://bblp:abcd1234@192.168.1.50:322/streaming/live/1"


def test_rtsp_url_encodes_special_characters():
    from backend import camera as c

    assert "p%40ss" in c.rtsp_url("10.0.0.9", "p@ss")


def test_ffmpeg_mjpeg_stream_pipes_and_terminates(monkeypatch):
    from backend import camera as c
    import io

    class FakeProc:
        def __init__(self, payload):
            self.stdout = io.BytesIO(payload)
            self.terminated = False
            self.killed = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.killed = True

    proc = FakeProc(b"--ffmpeg\r\n" + b"x" * 5000)
    monkeypatch.setattr(c, "ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(c.subprocess, "Popen", lambda *a, **k: proc)

    out = b"".join(c.ffmpeg_mjpeg_stream("10.0.0.9", "code", width=640, fps=5))
    assert out.startswith(b"--ffmpeg")
    assert proc.terminated is True


def test_ffmpeg_mjpeg_requires_ffmpeg(monkeypatch):
    from backend import camera as c

    monkeypatch.setattr(c, "ffmpeg_exe", lambda: None)
    with pytest.raises(RuntimeError):
        list(c.ffmpeg_mjpeg_stream("10.0.0.9", "code"))


def test_ffmpeg_snapshot_returns_jpeg(monkeypatch):
    from backend import camera as c

    class Result:
        stdout = b"\xff\xd8jpegdata"

    monkeypatch.setattr(c, "ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(c.subprocess, "run", lambda *a, **k: Result())
    assert c.ffmpeg_snapshot("10.0.0.9", "code") == b"\xff\xd8jpegdata"


def test_ffmpeg_snapshot_returns_none_on_non_jpeg(monkeypatch):
    from backend import camera as c

    class Result:
        stdout = b"not a jpeg"

    monkeypatch.setattr(c, "ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(c.subprocess, "run", lambda *a, **k: Result())
    assert c.ffmpeg_snapshot("10.0.0.9", "code") is None


def test_rtsp_url_accepts_a_custom_port():
    from backend import camera as c

    assert c.rtsp_url("10.0.0.9", "code", 8554) == \
        "rtsps://bblp:code@10.0.0.9:8554/streaming/live/1"
    assert ":322/" in c.rtsp_url("10.0.0.9", "code")
