# Bambu Fleet Manager — How the Camera Works

A complete, implementation-level description of the live camera feature: how the
printer's video is discovered, authenticated, depacketised, transcoded and finally
rendered in the browser — plus everything that was tried, what failed on real
hardware, and how to diagnose it.

Verified against a **Bambu Lab X1C** (serial `00M09A…`, firmware reporting
`ver 20000`) on a flat LAN.

---

## 1. TL;DR

```
Printer ──RTSPS/TLS :322──► FastAPI ──ffmpeg──► MJPEG ──HTTP──► <img> in the browser
        (H.264, Digest auth)         (transcode)     multipart/x-mixed-replace
```

* The camera is a **LIVE555 RTSP server over TLS on port 322**, *not* the HTTP
  service on port 6000.
* Auth is **HTTP Digest** (`realm="LIVE555 Streaming Media"`) with `bblp` + the
  printer's **access code**.
* The stream is **H.264**, `packetization-mode=1`, control URL `…/track1`.
* Browsers cannot play RTSP, so the server runs **ffmpeg** to transcode it to
  **MJPEG**, which plays in a plain `<img>` in every browser.
* A pure-Python RTSP client + RTP depacketiser is included and used for the raw
  **H.264** endpoint and for capability probing.

---

## 2. Discovery — finding the camera

The camera's coordinates are **not hard-coded**. Every Bambu `push_status`
telemetry report contains an `ipcam` object. On the X1C it looks like:

```json
"ipcam": {
  "ipcam_dev": "1",
  "ipcam_record": "enable",
  "resolution": "1080p",
  "rtsp_url": "rtsps://192.168.0.80:322/streaming/live/1",
  "brtc_service": "enable",
  "tutk_server": "disable"
}
```

The server reads `ipcam.rtsp_url` to learn the **scheme** (`rtsps`), the **host**
and the **path** (`/streaming/live/1`). The port it uses is the constant `322`
(`backend/camera.py: RTSP_PORT`). `ipcam_resolution` and `ipcam_record` are shown
in the UI's camera info, and the same `rtsp_url` is displayed to the user so they
can open it in VLC/ffmpeg directly.

> **Why not port 6000?** The X1C firmware *resets* plain HTTP requests to port
> 6000 and answers MQTT camera commands with `{"camera":{"command":"enable",
> "reason":"unsupport common","result":"FAILURE"}}`. That endpoint is a
> proprietary dialect, not HTTP. It is only kept as a last-resort fallback.

---

## 3. Authentication — RTSP Digest

### 3.1 The scheme problem

The server **301-redirects** anything that is not the `rtsps://` scheme:

```
OPTIONS rtsp://192.168.0.80:322/streaming/live/1   → 301  Location: rtsps://192.168.0.80/1
```

So the **request line must literally use `rtsps://`** even though the transport is
already TLS:

```
OPTIONS rtsps://192.168.0.80:322/streaming/live/1 RTSP/1.0   → 200 OK
```

### 3.2 Digest

`OPTIONS` needs no auth, but `DESCRIBE`/`SETUP`/`PLAY` do. The server replies
`401` with a **Digest** challenge:

```
WWW-Authenticate: Digest realm="LIVE555 Streaming Media", nonce="064a08a6baf9adbe0e5676e6949f8496"
```

Basic auth is *ignored* here. The client must compute an RFC 2069/2617 response:

```
HA1      = MD5(user":"realm":"password)          = MD5("bblp:LIVE555 Streaming Media:<access_code>")
HA2      = MD5(method":"uri)                     = MD5("DESCRIBE:rtsps://192.168.0.80:322/streaming/live/1")
response = MD5(HA1":"nonce":"HA2)                # no qop
```

If the challenge includes `qop=auth` (some builds), the client also sends
`nc`, `cnonce` and hashes `HA1:nonce:nc:cnonce:qop:HA2`.

The nonce is **single-use per request**, so the client implements
*request → 401 → remember challenge → retry with digest* (up to 3 attempts) and
remembers the challenge for subsequent requests. See
`backend/camera.py: _parse_challenge / _digest_response / RtspCamera.request`.

### 3.3 Session id + keepalive

`SETUP` returns a session that **must be echoed on `PLAY` and every later
request**, or the server answers `454 Session Not Found`:

```
SETUP …/streaming/live/1/track1
  → Transport: RTP/AVP/TCP;unicast;destination=…;source=…;interleaved=0-1
    Session: 10751B47;timeout=10
PLAY  rtsps://…/streaming/live/1      (Session: 10751B47)
```

The session times out after **10 s**, so the client sends a `GET_PARAMETER`
keepalive every 4 s while idle (`RTSP_KEEPALIVE`). Without it the stream dies
mid-view.

### 3.4 SDP

`DESCRIBE` answers with the SDP. The parts the client needs:

```
a=control:*                       ← session level, ignore (would corrupt the URL)
m=video 0 RTP/AVP 96
a=rtpmap:96 H264/90000
a=fmtp:96 packetization-mode=1;profile-level-id=64C029;sprop-parameter-sets=Z2TAKawbGqBu,aO4xshs=
a=control:track1                  ← media level, the real control URL
```

* `sprop-parameter-sets` = base64 **SPS,PPS** — needed to configure a decoder.
* `profile-level-id 64C029` ⇒ WebCodecs codec string **`avc1.640029`**.
* The control URL is `content-base + "track1"` ⇒
  `rtsps://<ip>:322/streaming/live/1/track1`.

`RtspCamera.sdp()` returns `{codec, sps, pps, track_url, content_base, raw}`.

---

## 4. Transport — RTP over the interleaved TCP channel

The client asks for `Transport: RTP/AVP/TCP;unicast;interleaved=0-1`, so RTP is
multiplexed on the control socket rather than opening UDP ports. Each packet is:

```
'$' | channel (1 byte) | length (2 bytes, big endian) | RTP packet
```

RTP header (12 bytes) + optional CSRC list (`CC * 4`) + optional extension
(`X` bit, `1 + length_words * 4`), then the payload:

```python
# backend/camera.py: RtspCamera._rtp_payload
csrc = rtp[0] & 0x0F
offset = 12 + csrc * 4
if rtp[0] & 0x10:                       # extension present
    words = struct.unpack("!H", rtp[offset+2:offset+4])[0]
    offset += 4 + words * 4
payload = rtp[offset:]
```

---

## 5. Depacketisation — RTP → H.264 (RFC 6184)

Because `packetization-mode=1`, an access unit can arrive as:

| NAL type | Meaning | Handling |
|---|---|---|
| `1…23` | single NAL unit | emit as-is |
| `24` | **STAP-A** aggregate | walk 2-byte sizes, emit each NAL |
| `28` | **FU-A** fragment | reassemble using the FU header |

For **FU-A** the original NAL header is *not* transmitted; it is rebuilt from
the FU indicator's NRI bits and the FU header's type bits:

```
FU indicator: F | NRI(2) | type=28
FU header:    S | E | R | type(5)
reconstructed header = (FU_indicator & 0xE0) | (FU_header & 0x1F)
```

The client emits an **Annex-B** byte stream (`00 00 00 01` + NAL). See
`RtspCamera.h264()`.

> Two real bugs were found here and are covered by tests:
> 1. Using `FU_header & 0xE0` instead of `FU_indicator & 0xE0` produced a corrupt
>    NAL header (`0x9C` instead of `0x65`), so nothing decoded.
> 2. `ftplib.FTP.connect` in `ftp_client` (a different subsystem) read the
>    cleartext banner before the TLS handshake — unrelated but fixed the same way.

---

## 6. Two consumers, one stream

### 6.1 ffmpeg → MJPEG (the one the browser uses)

The dashboard shows the camera through a **plain `<img>`** fed by an MJPEG
`multipart/x-mixed-replace` response. ffmpeg does the transcoding:

```bash
ffmpeg -hide_banner -loglevel error \
       -rtsp_transport tcp -fflags nobuffer -flags low_delay \
       -i "rtsps://bblp:<access_code>@<ip>:322/streaming/live/1" \
       -an -vf scale=1280:-2 -r 12 -q:v 7 \
       -f mpjpeg -
```

Key details:

* ffmpeg is **bundled** via the `imageio-ffmpeg` wheel (`ffmpeg_exe()`), so there
  is no system dependency; a system `ffmpeg` on `PATH` is used as a fallback.
* ffmpeg's `mpjpeg` muxer always uses the boundary token **`ffmpeg`**, so the
  response is served as `Content-Type: multipart/x-mixed-replace; boundary=ffmpeg`.
* ffmpeg happens to accept `rtsps://…` with the credentials **in the URL** and
  does the Digest dance itself; TLS verification is not enforced by default for
  this stream.
* One ffmpeg **per open card** (`n` printers ⇒ `n` processes). The UI exposes
  restart ⟳ and stop ■.

### 6.2 Pure-Python H.264 (used for probing + the `/camera/h264` endpoint)

`RtspCamera` performs the whole handshake above in ~200 lines of stdlib-only
Python and yields Annex-B NALs. This is what ran the reverse-engineering and is
served at `GET /api/printers/{id}/camera/h264` for tooling.

> **Historical note.** The first implementation decoded this Annex-B stream in
> the browser with **WebCodecs**. It worked, but it required repackaging the
> NALs into AVC (length-prefixed) access units and only functioned in
> Chromium-based browsers. The ffmpeg→MJPEG path replaced it because it plays
> anywhere, including phones.

---

## 7. HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/printers/{id}/camera/sdp` | `{codec, sps, pps, track_url, content_base, raw}` |
| `GET` | `/api/printers/{id}/camera/mjpeg` | Live MJPEG (`?width=960&fps=10`), browser `<img>` |
| `GET` | `/api/printers/{id}/camera/h264` | Raw Annex-B H.264 stream |
| `GET` | `/api/printers/{id}/camera/snapshot` | One JPEG frame (ffmpeg first, HTTP 6000 fallback) |

* `/camera/mjpeg` returns **`503`** with an install hint if ffmpeg cannot be
  found.
* `/camera/snapshot` returns **`502`** if no JPEG could be obtained.
* Streaming responses set `Cache-Control: no-store` so the image is never cached.
* Blocking RTSP/ffmpeg work runs in a thread (`run_blocking`), never on the event
  loop. Each ffmpeg process is terminated in the generator's `finally` block when
  the client disconnects.

---

## 8. Frontend

* Each printer card has a **persistent camera column** on the left (full width on
  phones, fixed 330 px on desktop).
* Live view is an `<img src="/api/printers/{id}/camera/mjpeg?…&t=<ts>">`.
  Visibility is set **immediately** on Start — `load` is not awaited, because
  browsers do not reliably fire `load` for `multipart/x-mixed-replace`.
* Buttons: **restart**, **stop**, **snapshot**, **fullscreen** (Fullscreen API;
  `:fullscreen` CSS makes the video fill the screen with `object-fit: contain`).
* The RTSPS URL (with the access code) is shown so it can be pasted into VLC:

  ```
  rtsps://bblp:<access_code>@<printer-ip>:322/streaming/live/1
  ```

---

## 9. Security

* **Encrypted, not authenticated.** The printer uses a self-signed certificate,
  so both the Python client (`ssl.CERT_NONE`, `check_hostname=False`) and ffmpeg
  skip verification. The stream is TLS-encrypted but the peer is not identified.
* The **access code is a password**. It is stored in `config/printers.json`
  (git-ignored) and is embedded in the RTSPS URL shown in the UI — do not expose
  the dashboard beyond the trusted LAN without a TLS-terminating reverse proxy
  and authentication.
* Each viewer costs one ffmpeg process; stop streams you are not watching.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Connection **reset** on port 6000 | Wrong service: 6000 is not HTTP on current firmware | Use RTSPS 322 |
| `{"reason":"unsupport common"}` to camera commands | Firmware ignores MQTT camera control | Not needed; stream via RTSP |
| `301 Moved Permanently` | Request line used `rtsp://` | Use `rtsps://` |
| `401 Unauthorized` after sending Basic | Server wants **Digest** | Implement digest (see §3.2) |
| `454 Session Not Found` | `PLAY` without the `Session:` header | Echo the id from `SETUP` |
| Stream dies after ~10 s | Session timeout | `GET_PARAMETER` keepalive every 4 s |
| `501 Option not understood` (FTPS) | vsFTPd syntax quirk | N/A for camera |
| Blank live view but snapshot works | The image element was hidden until `load`, which never fires for MJPEG | Reveal it immediately |
| ffmpeg missing | `imageio-ffmpeg` not installed | `pip install imageio-ffmpeg` |
| Corrupt decode / no frames | FU-A header rebuilt from the wrong bits | `(FU_indicator & 0xE0) \| (FU_header & 0x1F)` |

### Quick manual test

```bash
ffmpeg -rtsp_transport tcp -i "rtsps://bblp:<access_code>@<ip>:322/streaming/live/1" \
       -frames:v 1 -f image2 -update 1 snap.jpg
```

If this writes a JPEG, the printer, credentials and network are all fine and any
remaining problem is in the server or the browser.

---

## 11. Source map

| File | Responsibility |
|---|---|
| `backend/camera.py` | RTSP client, digest auth, RTP→H.264, ffmpeg bridge, snapshot |
| `backend/main.py` | `/camera/sdp`, `/camera/mjpeg`, `/camera/h264`, `/camera/snapshot` |
| `static/js/app.js` | camera column, start/stop/restart/snapshot/fullscreen |
| `tests/test_camera.py` | snapshot/stream helpers, JPEG magic checks |
| `tests/test_camera_rtsp.py` | digest, SDP parsing, STAP-A/FU-A depacketising, ffmpeg bridge |
| `tests/js/dom_files_test.js` etc. | UI behaviour around the card |

### Reference

The MQTT/RTP command semantics cross-checked against **OrcaSlicer**
(`src/slic3r/GUI/MediaPlayCtrl.cpp` — `bambu:///rtsps___<user>:<pass>@<ip>/streaming/live/1?proto=rtsps`,
`bblp` username, access-code password):

<https://github.com/OrcaSlicer/OrcaSlicer>
