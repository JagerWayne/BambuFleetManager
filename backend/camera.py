"""Printer camera access.

Two transports, because X1-series firmware does not speak plain HTTP for the
camera:

* ``rtsps://<ip>:322/streaming/live/1`` with ``bblp`` / access-code auth — this
  is what the printer advertises in ``ipcam.rtsp_url`` and what OrcaSlicer uses.
  Browsers cannot play RTSP, so :func:`rtsp_jpeg_frames` implements a minimal
  RTSP client and re-emits the JPEG frames as a byte stream that the server
  republishes as MJPEG.
* ``http://<ip>:6000/cache/snapshot.jpg`` — attempted as a fallback; current
  firmware resets these connections, so it is best-effort only.
"""

import base64
import hashlib
import logging
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import urllib.parse
from typing import Iterator, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

CAMERA_PORT = 6000
SNAPSHOT_PATH = "/cache/snapshot.jpg"
STREAM_PATH = "/video/1"
CAMERA_TIMEOUT = 8
CHUNK = 8192

# RTSP / RTP
RTSP_PORT = 322
RTSP_PATH = "/streaming/live/1"
RTSP_USER = "bblp"
RTSP_TIMEOUT = 10
RTSP_KEEPALIVE = 4
RTP_HEADER_LEN = 12
SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
START_CODE = b"\x00\x00\x00\x01"

# ffmpeg bridge
FFMPEG_TIMEOUT = 25
#: ffmpeg's `mpjpeg` muxer always uses this boundary token.
MJPEG_BOUNDARY = "ffmpeg"


def ffmpeg_exe() -> Optional[str]:
    """Locate an ffmpeg binary: the bundled one, or one on PATH."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def hidden_creation_flags() -> int:
    """``creationflags`` that keep ffmpeg from flashing a console window.

    The tray app ships as a windowed (GUI subsystem) exe, so it owns no console
    of its own. Without this flag Windows gives every ffmpeg child a brand-new
    visible console window while a camera is streaming. Harmless elsewhere, so
    the flag is zero everywhere except Windows.
    """
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def rtsp_url(ip: str, access_code: str, port: int = RTSP_PORT) -> str:
    """The URL form the printer documents for local streaming."""
    user = urllib.parse.quote(RTSP_USER)
    code = urllib.parse.quote(access_code, safe="")
    return f"rtsps://{user}:{code}@{ip}:{port}{RTSP_PATH}"


def _ffmpeg_input(ip: str, access_code: str, port: int = RTSP_PORT) -> list:
    return [
        "-hide_banner",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-i", rtsp_url(ip, access_code, port),
    ]


def ffmpeg_mjpeg_stream(
    ip: str, access_code: str, width: int = 960, fps: int = 10, port: int = RTSP_PORT
) -> Iterator[bytes]:
    """Transcode the printer's RTSPS H.264 into an MJPEG stream via ffmpeg.

    MJPEG in a ``multipart/x-mixed-replace`` response plays in every browser
    through a plain ``<img>``, which avoids depending on the browser's H.264
    WebCodecs support.
    """
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("ffmpeg is not available (pip install imageio-ffmpeg)")

    args = [exe, *_ffmpeg_input(ip, access_code, port),
            "-an", "-vf", f"scale={width}:-2", "-r", str(fps),
            "-q:v", "7", "-f", "mpjpeg", "-"]

    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=hidden_creation_flags(),
    )
    try:
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(CHUNK)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def ffmpeg_snapshot(ip: str, access_code: str, width: int = 1280, port: int = RTSP_PORT) -> Optional[bytes]:
    """Grab a single JPEG frame with ffmpeg (works where the HTTP cam does not)."""
    exe = ffmpeg_exe()
    if not exe:
        return None
    args = [exe, *_ffmpeg_input(ip, access_code, port),
            "-frames:v", "1", "-vf", f"scale={width}:-2",
            "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT,
            creationflags=hidden_creation_flags(),
        )
        if result.stdout.startswith(SOI):
            return result.stdout
        logger.debug("ffmpeg snapshot produced no JPEG (%d bytes)", len(result.stdout))
    except Exception as exc:
        logger.debug("ffmpeg snapshot failed: %s", exc)
    return None


def snapshot_url(ip: str) -> str:
    return f"http://{ip}:{CAMERA_PORT}{SNAPSHOT_PATH}"


def stream_url(ip: str) -> str:
    return f"http://{ip}:{CAMERA_PORT}{STREAM_PATH}"


def fetch_snapshot(ip: str) -> Optional[bytes]:
    """Return one JPEG frame, or ``None`` when the camera is off/unreachable.

    A disabled camera answers ``200`` with a placeholder body, so the JPEG
    start-of-image marker is verified before handing bytes to the browser.
    """
    try:
        res = requests.get(snapshot_url(ip), timeout=CAMERA_TIMEOUT)
        if res.status_code == 200 and res.content.startswith(SOI):
            return res.content
        logger.debug("Snapshot from %s returned HTTP %s (no JPEG frame)", ip, res.status_code)
    except requests.RequestException as exc:
        logger.debug("Snapshot from %s failed: %s", ip, exc)
    return None


def open_stream(ip: str) -> Tuple[str, Iterator[bytes]]:
    """Open the legacy HTTP MJPEG stream (kept for older firmware)."""
    response = requests.get(stream_url(ip), stream=True, timeout=CAMERA_TIMEOUT)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "multipart/x-mixed-replace")

    def chunks() -> Iterator[bytes]:
        try:
            for chunk in response.iter_content(CHUNK):
                if chunk:
                    yield chunk
        except Exception as exc:  # client hung up / printer rebooted
            logger.debug("Camera stream from %s ended: %s", ip, exc)
        finally:
            close_stream(response)

    return content_type, chunks()


def close_stream(response: requests.Response) -> None:
    try:
        response.close()
    except Exception:
        pass


# --------------------------------------------------------------------- RTSP


def _parse_challenge(header: str) -> dict:
    """Parse a ``WWW-Authenticate`` header into its scheme and parameters."""
    header = (header or "").strip()
    if not header:
        return {}
    scheme, _, rest = header.partition(" ")
    params = {}
    for chunk in rest.split(","):
        key, _, value = chunk.strip().partition("=")
        if key:
            params[key.strip().lower()] = value.strip().strip('"')
    return {"scheme": scheme.lower(), **params}


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()  # noqa: S324 - RTSP digest


def _digest_response(challenge: dict, method: str, uri: str, access_code: str) -> str:
    """Build an RFC 2069/2617 digest ``Authorization`` value."""
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    ha1 = _md5(f"{RTSP_USER}:{realm}:{access_code}")
    ha2 = _md5(f"{method}:{uri}")
    parts = [
        f'username="{RTSP_USER}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
    ]
    qop = challenge.get("qop")
    if qop:
        options = [q.strip() for q in qop.split(",")]
        selected = "auth" if "auth" in options else options[0]
        cnonce = hashlib.sha1(os.urandom(16)).hexdigest()[:16]
        nc = "00000001"
        response = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{selected}:{ha2}")
        parts += [f"qop={selected}", f"nc={nc}", f'cnonce="{cnonce}"', f'response="{response}"']
    else:
        response = _md5(f"{ha1}:{nonce}:{ha2}")
    parts.append(f'response="{response}"')
    return "Digest " + ", ".join(parts)


class RtspError(Exception):
    """The camera refused, or the RTSP conversation failed."""


class RtspCamera:
    """Minimal RTSP-over-TLS client that yields JPEG frames as RTP payloads.

    The X1C runs a LIVE555 RTSP server on port 322 that requires **Digest**
    auth, and it only accepts ``rtsps://`` in the request line. This implements
    OPTIONS/SETUP/PLAY plus RTP/JPEG depacketisation from the interleaved TCP
    channel - no ffmpeg or media framework needed.
    """

    def __init__(self, ip: str, access_code: str, path: str = RTSP_PATH, port: int = RTSP_PORT):
        self.ip = ip
        self.access_code = access_code
        self.path = path
        self.port = port
        self.sock: Optional[ssl.SSLSocket] = None
        self._buffer = b""
        self._seq = 1
        self._challenge: dict = {}
        self._session: str = ""

    # -- plumbing ---------------------------------------------------------
    def _url(self, suffix: str = "") -> str:
        # The server 301-redirects anything that is not the rtsps:// scheme.
        return f"rtsps://{self.ip}:{self.port}{self.path}{suffix}"

    def connect(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((self.ip, self.port), RTSP_TIMEOUT)
        raw.settimeout(RTSP_TIMEOUT)
        self.sock = context.wrap_socket(raw, server_hostname=self.ip)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.keepalive()
                self._transact("TEARDOWN", self._url())
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _auth_header(self, method: str, url: str) -> str:
        if self._challenge.get("scheme", "").startswith("digest"):
            return f"Authorization: {_digest_response(self._challenge, method, url, self.access_code)}\r\n"
        token = base64.b64encode(f"{RTSP_USER}:{self.access_code}".encode()).decode()
        return f"Authorization: Basic {token}\r\n"

    def _transact(self, method: str, url: str, extra_headers: str = "") -> bytes:
        """Send one RTSP request, raising :class:`RtspError` unless it returned 200."""
        code, head, _ = self.request(method, url, extra_headers)
        if code != 200:
            status = head.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
            raise RtspError(status.strip() or f"RTSP status {code}")
        return head

    # -- session ----------------------------------------------------------
    def describe(self, suffix: str = "") -> Tuple[int, bytes, bytes]:
        """DESCRIBE a resource; returns ``(status_code, head, body)``."""
        code, head, body = self.request("DESCRIBE", self._url(suffix), "Accept: application/sdp\r\n")
        return code, head, body

    def sdp(self) -> dict:
        """Parse the H.264 parameters a WebCodecs decoder needs to start."""
        _, head, body = self.describe()
        text = head.decode("latin-1", errors="replace")
        control = ""
        for line in text.split("\r\n"):
            if line.lower().startswith("content-base:"):
                control = line.split(":", 1)[1].strip()
        sdp = body.decode("utf-8", errors="replace")
        sps = pps = ""
        for line in sdp.split("\r\n"):
            if line.startswith("a=control:") and line.split(":", 1)[1].strip() not in ("", "*"):
                control = control.rstrip("/") + "/" + line.split(":", 1)[1].strip().lstrip("/")
            if line.startswith("a=fmtp:"):
                for part in line.split(" ")[1].split(";"):
                    key, _, value = part.partition("=")
                    if key.strip() == "sprop-parameter-sets" and "," in value:
                        sps, _, pps = value.partition(",")
                        break
        return {
            "content_base": control,
            "track_url": self._url("/track1"),
            "sps": sps.strip(),
            "pps": pps.strip(),
            "codec": "avc1.640029",
            "raw": sdp,
        }

    def request(
        self, method: str, url: str, extra_headers: str = ""
    ) -> Tuple[int, bytes, bytes]:
        """Send one RTSP request, transparently answering a 401 challenge."""
        for _ in range(3):
            headers = (
                f"{method} {url} RTSP/1.0\r\n"
                f"CSeq: {self._seq}\r\n"
                f"User-Agent: bambu-fleet-manager\r\n"
            )
            self._seq += 1
            if self._challenge:
                headers += self._auth_header(method, url)
            if self._session:
                headers += f"Session: {self._session}\r\n"
            headers += extra_headers + "\r\n"
            self.sock.sendall(headers.encode())  # type: ignore[union-attr]

            head, body = self._read_response_with_body()
            parts = head.split(b" ")
            code = int(parts[1]) if len(parts) > 1 else 0
            if code == 401:
                self._remember_challenge(head)
                continue
            return code, head, body
        return 401, b"", b""

    def _remember_challenge(self, head: bytes) -> None:
        for line in head.decode("latin-1", errors="replace").split("\r\n"):
            if line.lower().startswith("www-authenticate:"):
                self._challenge = _parse_challenge(line.split(":", 1)[1])

    def _read_response_with_body(self) -> Tuple[bytes, bytes]:
        while b"\r\n\r\n" not in self._buffer:
            chunk = self.sock.recv(4096)  # type: ignore[union-attr]
            if not chunk:
                raise RtspError("connection closed")
            self._buffer += chunk
        head, rest = self._buffer.split(b"\r\n\r\n", 1)
        length = 0
        for line in head.decode("latin-1", errors="replace").split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        while len(rest) < length:
            chunk = self.sock.recv(4096)  # type: ignore[union-attr]
            if not chunk:
                break
            rest += chunk
        body, self._buffer = rest[:length], rest[length:]
        return head, body

    def start(self) -> None:
        """Authenticate, set up the track and start playback."""
        self.connect()
        self._transact("OPTIONS", self._url())
        info = self.sdp()
        head = self._transact(
            "SETUP",
            info["track_url"],
            "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n",
        )
        # The session id is required on PLAY, and expires after ~10s unless kept
        # alive with GET_PARAMETER.
        for line in head.decode("latin-1", errors="replace").split("\r\n"):
            if line.lower().startswith("session:"):
                self._session = line.split(":", 1)[1].strip().split(";")[0]
        self._transact("PLAY", self._url(), "Range: npt=0.000-\r\n")
        self.sock.settimeout(RTSP_KEEPALIVE)  # type: ignore[union-attr]

    # -- depacketising ----------------------------------------------------
    def _read_exactly(self, count: int) -> bytes:
        while len(self._buffer) < count:
            try:
                chunk = self.sock.recv(65536)  # type: ignore[union-attr]
            except socket.timeout:
                self.keepalive()
                continue
            if not chunk:
                raise RtspError("connection closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def keepalive(self) -> None:
        """Refresh the RTSP session; the camera drops it after ~10s idle."""
        if not self._session:
            return
        try:
            self._seq += 1
            request = (
                f"GET_PARAMETER {self._url()} RTSP/1.0\r\n"
                f"CSeq: {self._seq}\r\n"
                f"Session: {self._session}\r\n"
                f"User-Agent: bambu-fleet-manager\r\n\r\n"
            ).encode()
            self.sock.sendall(request)  # type: ignore[union-attr]
        except Exception as exc:
            logger.debug("RTSP keepalive failed: %s", exc)

    def packets(self) -> Iterator[bytes]:
        """Yield raw RTP payloads from the interleaved TCP channel."""
        while True:
            header = self._read_exactly(4)
            if header[0:1] != b"$":
                # Interleaved control data (a late RTSP response) - skip it.
                length = struct.unpack("!H", header[2:4])[0]
                self._read_exactly(length)
                continue
            length = struct.unpack("!H", header[2:4])[0]
            rtp = self._read_exactly(length)
            payload = self._jpeg_payload(rtp)
            if payload:
                yield payload

    def h264(self) -> Iterator[bytes]:
        """Yield an Annex-B byte stream reconstructed from RTP (RFC 6184).

        The X1C streams H.264 with packetization-mode=1, so single NAL units,
        STAP-A aggregates and FU-A fragments all have to be reassembled. The
        browser decodes this with WebCodecs.
        """
        fu_buffer = b""
        fu_header = 0

        for payload in self.packets():
            if not payload:
                continue
            nal_type = payload[0] & 0x1F

            if 1 <= nal_type <= 23:  # single NAL unit
                yield START_CODE + payload
            elif nal_type == 24:  # STAP-A
                offset = 1
                while offset + 2 <= len(payload):
                    size = struct.unpack("!H", payload[offset:offset + 2])[0]
                    offset += 2
                    unit = payload[offset:offset + size]
                    offset += size
                    if unit:
                        yield START_CODE + unit
            elif nal_type == 28:  # FU-A
                if len(payload) < 2:
                    continue
                fu_header = payload[1]
                start_bit = bool(fu_header & 0x80)
                end_bit = bool(fu_header & 0x40)
                fragment = payload[2:]
                if start_bit:
                    # Rebuild the NAL header: NRI from the FU indicator, type from the FU header.
                    fu_buffer = bytes([(payload[0] & 0xE0) | (fu_header & 0x1F)])
                    fu_buffer += fragment
                else:
                    fu_buffer += fragment
                if end_bit and fu_buffer:
                    yield START_CODE + fu_buffer
                    fu_buffer = b""

    def frames(self) -> Iterator[bytes]:
        """Yield complete JPEG frames from an MJPEG stream (older firmware)."""
        pending = b""
        for payload in self.packets():
            pending += payload
            while True:
                start = pending.find(SOI)
                if start < 0:
                    pending = b""
                    break
                end = pending.find(EOI, start + 2)
                if end < 0:
                    pending = pending[start:]
                    break
                frame = pending[start:end + 2]
                pending = pending[end + 2:]
                if len(frame) > 1024:
                    yield frame

    @staticmethod
    def _jpeg_payload(rtp: bytes) -> bytes:
        """Strip the RTP header (with CSRC and extension) from a packet."""
        if len(rtp) < RTP_HEADER_LEN:
            return b""
        csrc_count = rtp[0] & 0x0F
        offset = RTP_HEADER_LEN + csrc_count * 4
        if rtp[0] & 0x10:  # header extension present
            if len(rtp) < offset + 4:
                return b""
            ext_words = struct.unpack("!H", rtp[offset + 2:offset + 4])[0]
            offset += 4 + ext_words * 4
        return rtp[offset:]


def rtsp_jpeg_frames(ip: str, access_code: str) -> Iterator[bytes]:
    """Yield JPEG frames from a printer that streams MJPEG."""
    camera = RtspCamera(ip, access_code)
    camera.start()
    try:
        yield from camera.frames()
    finally:
        camera.close()


def rtsp_h264_stream(ip: str, access_code: str, port: int = RTSP_PORT) -> Iterator[bytes]:
    """Yield an Annex-B H.264 byte stream from the printer's RTSP camera."""
    camera = RtspCamera(ip, access_code, port=port)
    camera.start()
    try:
        yield from camera.h264()
    finally:
        camera.close()


def rtsp_sdp(ip: str, access_code: str, port: int = RTSP_PORT) -> dict:
    """Return the camera's SDP parameters (codec, SPS/PPS)."""
    camera = RtspCamera(ip, access_code, port=port)
    try:
        camera.connect()
        return camera.sdp()
    finally:
        camera.close()


def mjpeg_stream(frames: Iterator[bytes], boundary: str = "frame") -> Iterator[bytes]:
    """Wrap raw JPEG frames as a multipart/x-mixed-replace stream."""
    for frame in frames:
        yield (
            f"--{boundary}\r\n".encode()
            + b"Content-Type: image/jpeg\r\n"
            + f"Content-Length: {len(frame)}\r\n\r\n".encode()
            + frame
            + b"\r\n"
        )
