"""Implicit FTPS (TLS) client: job upload plus SD-card browsing and deletion.

Printers speak implicit FTPS on port 990 with a self-signed certificate, so
certificate verification is disabled (the channel is still encrypted).
"""

import logging
import os
import posixpath
import re
import socket
import ssl
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from ftplib import FTP_TLS
from typing import Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

FTPS_PORT = 990
FTPS_TIMEOUT = 30
FTPS_CHUNK = 64 * 1024

UPLOAD_ALLOWED_SUFFIXES = (".3mf", ".gcode")


def build_tls_context() -> ssl.SSLContext:
    """Printers ship self-signed certificates, so verification is disabled."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def normalize_remote_path(path: Optional[str]) -> str:
    """Return a safe, absolute SD-card path (no ``..`` escapes, always leading /)."""
    raw = (path or "/").replace("\\", "/").strip()
    parts = [p for p in raw.split("/") if p not in ("", ".", "..")]
    return "/" + posixpath.join(*parts) if parts else "/"


class RemoteEntry:
    """One row of an SD-card directory listing."""

    __slots__ = ("name", "path", "is_dir", "size", "modified")

    def __init__(self, name: str, path: str, is_dir: bool, size: int, modified: Optional[str]):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.size = size
        self.modified = modified

    @property
    def is_printable(self) -> bool:
        """Project files print directly; extracted project folders do too.

        Some firmware revisions store a project as a folder containing
        ``Metadata/`` and ``3D/`` rather than as a single archive, so a
        directory whose name ends in ``.3mf`` is equally printable.
        """
        return self.name.lower().endswith(UPLOAD_ALLOWED_SUFFIXES)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "is_dir": self.is_dir,
            "is_printable": self.is_printable,
            "size": self.size,
            "modified": self.modified,
        }


class _SessionReuseContext:
    """Context proxy that resumes the control connection's TLS session.

    vsFTPd (the server on Bambu firmware) is configured with
    ``ssl_session_reuse``, so it rejects a data connection that starts a fresh
    TLS handshake with ``522 SSL connection failed: session reuse required``.
    Resuming the control session on the data channel is what it expects.
    """

    def __init__(self, context: ssl.SSLContext, owner: "ImplicitFTP_TLS"):
        self._context = context
        self._owner = owner

    def wrap_socket(self, sock, **kwargs):
        session = getattr(self._owner, "tls_session", None)
        if session is not None and "session" not in kwargs:
            kwargs["session"] = session
        return self._context.wrap_socket(sock, **kwargs)

    def __getattr__(self, name):  # protocol/minimum_version/etc still reachable
        return getattr(self._context, name)


class ImplicitFTP_TLS(FTP_TLS):
    """FTP_TLS subclass enforcing the TLS handshake immediately on connect."""

    def __init__(self, *args, **kwargs):
        context = kwargs.get("context")
        # Passing no host keeps FTP.__init__ from opening a session here; the
        # connection is opened explicitly by connect() below.
        super().__init__(*args, **kwargs)
        self._pasv_port = None
        self.tls_session = None
        if context is not None:
            self.context = _SessionReuseContext(context, self)

    def connect(self, host="", port=FTPS_PORT, timeout=-999):
        """Open the socket, wrap it in TLS immediately, *then* read the greeting.

        ``ftplib.FTP.connect`` reads the welcome banner on the cleartext socket,
        which can never succeed on an implicit-TLS port - the banner only exists
        after the handshake. The socket setup is therefore inlined here instead
        of delegating to ``super().connect()``.
        """
        if host:
            self.host = host
        if port:
            self.port = port
        if timeout is not None and timeout != -999:
            self.timeout = timeout
        if self.timeout is not None and not self.timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")

        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
        self.tls_session = getattr(self.sock, "session", None)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


@contextmanager
def ftps_session(ip: str, access_code: str, timeout: int = FTPS_TIMEOUT) -> Iterator[ImplicitFTP_TLS]:
    """Open an authenticated, fully encrypted FTPS session and always close it."""
    ftps = ImplicitFTP_TLS(context=build_tls_context())
    try:
        ftps.connect(host=ip, port=FTPS_PORT, timeout=timeout)
        ftps.login(user="bblp", passwd=access_code)
        ftps.prot_p()  # encrypt the data channel too
        try:
            yield ftps
        finally:
            try:
                ftps.quit()
            except Exception:
                ftps.close()
    except Exception:
        try:
            ftps.close()
        except Exception:
            pass
        raise


def _list_with_mlsd(ftps: ImplicitFTP_TLS, path: str) -> List[RemoteEntry]:
    entries: List[RemoteEntry] = []
    for name, facts in ftps.mlsd(path or "/", facts=["type", "size", "modify"]):
        if name in (".", ".."):
            continue
        is_dir = facts.get("type") in ("dir", "cdir", "pdir")
        try:
            size = 0 if is_dir else int(facts.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        entries.append(
            RemoteEntry(
                name=name,
                path=posixpath.join(path or "/", name),
                is_dir=is_dir,
                size=size,
                modified=_format_mtime(facts.get("modify") or ""),
            )
        )
    return entries


def _list_with_nlst(ftps: ImplicitFTP_TLS, path: str) -> List[RemoteEntry]:
    """Last-resort fallback: names only, no metadata."""
    entries: List[RemoteEntry] = []
    for name in ftps.nlst(path or "/"):
        name = posixpath.basename(name.rstrip("/"))
        if not name or name in (".", ".."):
            continue
        entries.append(
            RemoteEntry(
                name=name,
                path=posixpath.join(path or "/", name),
                is_dir=False,
                size=0,
                modified=None,
            )
        )
    return entries


#: ``ls -l`` prefix: perms, links, owner, group, size, then a date and a name.
_LS_LINE = re.compile(
    r"^(?P<perms>[-dlbcps])(?P<attrs>[rwxSsTt-]{9})\s+"
    r"(?P<links>\d+)\s+(?P<owner>\S+)\s+(?P<group>\S+)\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<time>[\d:]{4,5})\s+"
    r"(?P<name>.+)$"
)


def _ls_date_to_iso(month: str, day: str, stamp: str) -> Optional[str]:
    """Convert an ``ls -l`` date column to ISO-8601.

    ``ls`` prints ``HH:MM`` for files younger than ~6 months and ``YYYY`` for
    older ones, so the year is inferred for recent entries.
    """
    if ":" in stamp:
        year = datetime.now(timezone.utc).year
        raw = f"{month} {day} {year} {stamp}"
        for fmt in ("%b %d %Y %H:%M", "%b %d %H:%M"):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                continue
        return None
    raw = f"{month} {day} {stamp}"
    try:
        return datetime.strptime(raw, "%b %d %Y").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _parse_ls_line(line: str) -> Optional[RemoteEntry]:
    match = _LS_LINE.match(line.rstrip("\r\n"))
    if not match:
        return None
    name = match.group("name").strip()
    # Symlink targets ("name -> target") are not useful in the SD browser.
    name = name.split(" -> ", 1)[0]
    return RemoteEntry(
        name=name,
        path=None,  # filled in by the caller
        is_dir=match.group("perms") == "d",
        size=int(match.group("size")),
        modified=_ls_date_to_iso(
            match.group("month"), match.group("day"), match.group("time")
        ),
    )


def _list_with_list(ftps: ImplicitFTP_TLS, path: str) -> List[RemoteEntry]:
    """Parse an ``ls -l`` listing - what Bambu's vsFTPd actually speaks."""
    lines: List[str] = []
    ftps.retrlines(f"LIST {path or '/'}", lines.append)
    entries: List[RemoteEntry] = []
    for line in lines:
        entry = _parse_ls_line(line)
        if entry is None:
            continue
        if entry.name in (".", ".."):
            continue
        entry.path = posixpath.join(path or "/", entry.name)
        entries.append(entry)
    return entries


def _format_mtime(raw: str) -> Optional[str]:
    """Accept MLSD ``YYYYMMDDHHMMSS`` timestamps."""
    text = (raw or "").strip()
    try:
        parsed = datetime.strptime(text, "%Y%m%d%H%M%S")
    except (ValueError, TypeError):
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat()


def list_directory(ip: str, access_code: str, path: str = "/") -> List[RemoteEntry]:
    """List one SD-card directory. Directories sort first, then by name.

    Bambu firmware runs vsFTPd 3.0.5, which rejects ``MLSD`` (``501``), so the
    chain is MLSD -> ``ls -l`` LIST -> NLST.
    """
    target = normalize_remote_path(path)
    with ftps_session(ip, access_code) as ftps:
        for lister in (_list_with_mlsd, _list_with_list, _list_with_nlst):
            try:
                entries = lister(ftps, target)
                if entries:
                    break
            except Exception as exc:
                logger.debug("%s failed on %s%s: %s", lister.__name__, ip, target, exc)
                entries = []
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


def delete_path(ip: str, access_code: str, path: str, is_dir: bool = False) -> bool:
    """Delete a file or (empty) directory from the SD card."""
    target = normalize_remote_path(path)
    if target == "/":
        raise ValueError("refusing to delete the SD card root")
    with ftps_session(ip, access_code) as ftps:
        if is_dir:
            ftps.rmd(target)
        else:
            ftps.delete(target)
    logger.info("Deleted %s from %s", target, ip)
    return True


def rename_path(ip: str, access_code: str, path: str, new_name: str) -> str:
    """Rename a file in place and return the new path.

    Only the basename changes - the file stays in its folder. Validation (the
    ``.3mf`` extension, no separators) belongs to the caller, but the folder is
    re-derived here so a crafted name can never move a file elsewhere.
    """
    source = normalize_remote_path(path)
    if source == "/":
        raise ValueError("refusing to rename the SD card root")
    if not new_name or new_name in (".", "..") or "/" in new_name or "\\" in new_name:
        raise ValueError("the new name must not contain a path")
    parent = posixpath.dirname(source) or "/"
    target = normalize_remote_path(posixpath.join(parent, new_name))
    if posixpath.dirname(target) != parent:
        raise ValueError("the new name must not contain a path")
    with ftps_session(ip, access_code) as ftps:
        ftps.rename(source, target)
    logger.info("Renamed %s -> %s on %s", source, target, ip)
    return target


def upload_3mf_file(ip: str, access_code: str, file_path: str, remote_dir: str = "/") -> bool:
    """Upload a sliced .3mf / .gcode.3mf project into the printer SD card."""
    filename = os.path.basename(file_path)
    target = normalize_remote_path(remote_dir)

    try:
        with ftps_session(ip, access_code) as ftps:
            with open(file_path, "rb") as fp:
                ftps.storbinary(f"STOR {posixpath.join(target, filename)}", fp)
        logger.info("FTPS upload of %s to %s succeeded", filename, ip)
        return True
    except Exception as exc:
        logger.error("FTPS transfer of %s to %s failed: %s", filename, ip, exc)
        return False


def download_stream(ip: str, access_code: str, path: str, timeout: int = FTPS_TIMEOUT) -> Iterator[bytes]:
    """Yield the bytes of a remote file in chunks (keeps the session streaming)."""
    target = normalize_remote_path(path)
    with ftps_session(ip, access_code, timeout=timeout) as ftps:
        ftps.voidcmd("TYPE I")  # binary; vsFTPd would otherwise use ASCII
        conn = ftps.transfercmd(f"RETR {target}")
        sock = conn[0] if isinstance(conn, tuple) else conn
        try:
            while True:
                chunk = sock.recv(FTPS_CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                sock.close()
            except Exception:
                pass
            try:
                ftps.voidresp()
            except Exception:
                pass


def remote_file_exists(ip: str, access_code: str, path: str) -> bool:
    """Check a file exists without pulling its contents (vsFTPd SIZE is broken)."""
    target = normalize_remote_path(path)
    if target == "/":
        return False
    parent = posixpath.dirname(target) or "/"
    name = posixpath.basename(target)
    try:
        return any(entry.name == name and not entry.is_dir
                   for entry in list_directory(ip, access_code, parent))
    except Exception as exc:
        logger.debug("Existence check for %s failed: %s", target, exc)
        return False


def _remote_size(ip: str, access_code: str, path: str) -> int:
    target = normalize_remote_path(path)
    parent = posixpath.dirname(target) or "/"
    name = posixpath.basename(target)
    for entry in list_directory(ip, access_code, parent):
        if entry.name == name:
            return entry.size
    return 0


def read_remote_tail(
    ip: str, access_code: str, path: str, nbytes: int = 200_000, timeout: int = FTPS_TIMEOUT
) -> bytes:
    """Read the last ``nbytes`` of a remote file (where the zip index lives)."""
    target = normalize_remote_path(path)
    size = _remote_size(ip, access_code, target)
    start = max(0, size - nbytes)

    with ftps_session(ip, access_code, timeout=timeout) as ftps:
        ftps.voidcmd("TYPE I")
        conn = ftps.transfercmd(f"RETR {target}", rest=start) if start else ftps.transfercmd(f"RETR {target}")
        sock = conn[0] if isinstance(conn, tuple) else conn
        chunks = []
        try:
            while True:
                chunk = sock.recv(FTPS_CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            try:
                sock.close()
            except Exception:
                pass
            try:
                ftps.voidresp()
            except Exception:
                pass
    return b"".join(chunks)


def detect_plate_indices(ip: str, access_code: str, path: str) -> List[int]:
    """Return the plate numbers inside a sliced ``.gcode.3mf``.

    A project can hold several plates and the printer must be told exactly which
    one to run (``Metadata/plate_<n>.gcode``); asking for a plate that is not in
    the archive makes the printer report the file as unreadable. Only the tail of
    the file is fetched, which is where the zip central directory lists them.
    """
    try:
        tail = read_remote_tail(ip, access_code, path)
    except Exception as exc:
        logger.debug("Plate detection for %s failed: %s", path, exc)
        return []
    found = {int(m) for m in re.findall(rb"Metadata/plate_(\d+)\.gcode", tail)}
    return sorted(found)


def read_remote_range(
    ip: str, access_code: str, path: str, offset: int, length: int, timeout: int = FTPS_TIMEOUT
) -> bytes:
    """Read ``length`` bytes of a remote file starting at ``offset`` (FTP REST)."""
    target = normalize_remote_path(path)
    if offset < 0 or length <= 0:
        return b""
    with ftps_session(ip, access_code, timeout=timeout) as ftps:
        ftps.voidcmd("TYPE I")
        conn = ftps.transfercmd(f"RETR {target}", rest=offset)
        sock = conn[0] if isinstance(conn, tuple) else conn
        data = b""
        try:
            while len(data) < length:
                chunk = sock.recv(min(FTPS_CHUNK, length - len(data)))
                if not chunk:
                    break
                data += chunk
        finally:
            try:
                sock.close()
            except Exception:
                pass
            try:
                ftps.voidresp()
            except Exception:
                pass
    return data


#: Central-directory record layout of a zip entry (fixed part before the name).
_CD_FIXED = 46


def zip_central_directory(tail: bytes, file_size: int) -> Dict[str, dict]:
    """Map entry name -> {offset, comp, size, method} using the zip index.

    ``tail`` is the last chunk of the file and must contain the central
    directory. Pure function so it can be unit tested without a printer.
    """
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0 or eocd + 22 > len(tail):
        return {}
    cd_size = int.from_bytes(tail[eocd + 12:eocd + 16], "little")
    start = eocd - cd_size
    if start < 0:
        return {}
    pos = start
    entries: Dict[str, dict] = {}
    while pos + _CD_FIXED <= len(tail) and tail[pos:pos + 4] == b"PK\x01\x02":
        method = int.from_bytes(tail[pos + 10:pos + 12], "little")
        comp = int.from_bytes(tail[pos + 20:pos + 24], "little")
        size = int.from_bytes(tail[pos + 24:pos + 28], "little")
        name_len = int.from_bytes(tail[pos + 28:pos + 30], "little")
        extra_len = int.from_bytes(tail[pos + 30:pos + 32], "little")
        comment_len = int.from_bytes(tail[pos + 32:pos + 34], "little")
        offset = int.from_bytes(tail[pos + 42:pos + 46], "little")
        name = tail[pos + _CD_FIXED:pos + _CD_FIXED + name_len].decode("utf-8", "replace")
        entries[name] = {"offset": offset, "comp": comp, "size": size, "method": method}
        pos += _CD_FIXED + name_len + extra_len + comment_len
    return entries


def _decompress(method: int, blob: bytes) -> bytes:
    if method == 0:
        return blob
    if method == 8:
        return zlib.decompress(blob, -15)
    raise ValueError(f"unsupported zip compression method {method}")


def read_remote_zip_entries(
    ip: str, access_code: str, path: str, names: List[str], timeout: int = FTPS_TIMEOUT
) -> Dict[str, Optional[bytes]]:
    """Extract several entries from a remote zip in a single FTPS session.

    The tail (which holds the zip central directory) is fetched once, then every
    entry is read with a REST range on the same connection - far quicker than
    opening a session per entry.
    """
    target = normalize_remote_path(path)
    missing = {name: None for name in names}
    size = _remote_size(ip, access_code, target)
    if not size:
        return missing

    result: Dict[str, Optional[bytes]] = {}
    with ftps_session(ip, access_code, timeout=timeout) as ftps:
        ftps.voidcmd("TYPE I")

        def fetch_range(offset: int, length: int) -> bytes:
            conn = ftps.transfercmd(f"RETR {target}", rest=offset)
            sock = conn[0] if isinstance(conn, tuple) else conn
            data = b""
            try:
                while len(data) < length:
                    chunk = sock.recv(min(FTPS_CHUNK, length - len(data)))
                    if not chunk:
                        break
                    data += chunk
            finally:
                try:
                    sock.close()
                except Exception:
                    pass
                try:
                    ftps.voidresp()
                except Exception:
                    pass
            return data

        tail_len = min(size, 200_000)
        tail = fetch_range(size - tail_len, tail_len)
        directory = zip_central_directory(tail, size)

        for name in names:
            info = directory.get(name)
            if not info:
                result[name] = None
                continue
            header = fetch_range(info["offset"], 30)
            if len(header) < 30 or header[:4] != b"PK\x03\x04":
                result[name] = None
                continue
            name_len = int.from_bytes(header[26:28], "little")
            extra_len = int.from_bytes(header[28:30], "little")
            data_start = info["offset"] + 30 + name_len + extra_len
            blob = fetch_range(data_start, info["comp"])
            try:
                result[name] = _decompress(info["method"], blob)
            except Exception as exc:
                logger.debug("Could not inflate %s in %s: %s", name, path, exc)
                result[name] = None
    return result


def read_remote_zip_entry(ip: str, access_code: str, path: str, entry_name: str) -> Optional[bytes]:
    """Extract one entry from a remote zip, reading only what it needs."""
    return read_remote_zip_entries(ip, access_code, path, [entry_name]).get(entry_name)
