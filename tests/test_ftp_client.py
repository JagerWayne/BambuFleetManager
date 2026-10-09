"""Tests for SD-card browsing, deletion and path safety."""

import pytest

from backend import ftp_client
from backend.ftp_client import ImplicitFTP_TLS, RemoteEntry, normalize_remote_path


class FakeFTPS:
    """In-memory SD card implementing the subset of ftplib we use."""

    last = None
    tree = {}

    def __init__(self, *args, **kwargs):
        self.calls = []
        FakeFTPS.last = self

    # session -------------------------------------------------------
    def connect(self, host="", port=0, timeout=-999):
        self.calls.append(("connect", host, port, timeout))

    def login(self, user=None, passwd=None):
        if passwd != "12345678":
            raise RuntimeError("530 Login incorrect")
        self.calls.append(("login", user, passwd))

    def prot_p(self):
        self.calls.append(("prot_p",))

    def quit(self):
        self.calls.append(("quit",))

    def close(self):
        self.calls.append(("close",))

    # listing -------------------------------------------------------
    def mlsd(self, path="/", facts=None):
        self.calls.append(("mlsd", path))
        if path not in FakeFTPS.tree:
            raise RuntimeError("550 No such directory")
        out = []
        for name in FakeFTPS.tree[path]:
            is_dir = "/" + name in self.tree
            facts_out = {
                "type": "dir" if is_dir else "file",
                "size": "1024" if not is_dir else "0",
                "modify": "20240102030405",
            }
            out.append((name, facts_out))
        return out

    def nlst(self, path="/"):
        self.calls.append(("nlst", path))
        return [f"{path.rstrip('/')}/{name}" for name in FakeFTPS.tree.get(path, [])]

    def size(self, path):
        raise RuntimeError("not a plain file")

    # mutation ------------------------------------------------------
    def delete(self, path):
        self.calls.append(("delete", path))
        parent, _, name = path.rpartition("/")
        FakeFTPS.tree.setdefault(parent or "/", []).remove(name)

    def rmd(self, path):
        self.calls.append(("rmd", path))
        parent, _, name = path.rpartition("/")
        FakeFTPS.tree.setdefault(parent or "/", []).remove(name)
        FakeFTPS.tree.pop(path, None)

    def rename(self, src, dst):
        self.calls.append(("rename", src, dst))
        parent, _, name = src.rpartition("/")
        parent = parent or "/"
        if name not in FakeFTPS.tree.get(parent, []):
            raise RuntimeError("550 No such file")
        FakeFTPS.tree[parent].remove(name)
        new_parent, _, new_name = dst.rpartition("/")
        FakeFTPS.tree.setdefault(new_parent or "/", []).append(new_name)

    def storbinary(self, cmd, fp):
        self.calls.append(("storbinary", cmd))
        parent, _, name = cmd[5:].rpartition("/")
        FakeFTPS.tree.setdefault(parent or "/", []).append(name)


@pytest.fixture(autouse=True)
def fake_ftp(monkeypatch):
    FakeFTPS.tree = {"/": ["apps", "cache", "benchy.3mf"], "/apps": ["notes.txt"]}
    FakeFTPS.last = None
    monkeypatch.setattr(ftp_client, "ImplicitFTP_TLS", FakeFTPS)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "/"),
        ("", "/"),
        ("/", "/"),
        ("/apps", "/apps"),
        ("apps/", "/apps"),
        ("/../etc/passwd", "/etc/passwd"),
        ("../../secret", "/secret"),
        ("\\windows\\system32", "/windows/system32"),
    ],
)
def test_normalize_remote_path(raw, expected):
    assert normalize_remote_path(raw) == expected


def test_listing_sorts_directories_first():
    entries = ftp_client.list_directory("10.0.0.5", "12345678", "/")
    assert [e.name for e in entries] == ["apps", "benchy.3mf", "cache"]
    assert entries[0].is_dir is True
    assert entries[-1].is_printable is False
    printable = [e for e in entries if e.is_printable]
    assert printable[0].path == "/benchy.3mf"
    assert printable[0].modified.startswith("2024-01-02T03:04:05")


def test_listing_uses_an_encrypted_session():
    FakeFTPS.last = None
    ftp_client.list_directory("10.0.0.5", "12345678", "/")
    calls = FakeFTPS.last.calls
    assert calls[0] == ("connect", "10.0.0.5", 990, ftp_client.FTPS_TIMEOUT)
    assert calls[1] == ("login", "bblp", "12345678")
    assert ("prot_p",) in calls
    assert calls[-1] == ("quit",)


def test_entry_payload_is_serialisable():
    entry = RemoteEntry("a.3mf", "/a.3mf", False, 10, None)
    assert entry.to_dict() == {
        "name": "a.3mf",
        "path": "/a.3mf",
        "is_dir": False,
        "is_printable": True,
        "size": 10,
        "modified": None,
    }


def test_gcode_extension_is_printable():
    assert RemoteEntry("x.gcode", "/x.g", False, 1, None).is_printable is True
    assert RemoteEntry("x.txt", "/x", False, 1, None).is_printable is False


def test_delete_file_removes_it_from_listing():
    assert ftp_client.delete_path("10.0.0.5", "12345678", "/benchy.3mf") is True
    names = [e.name for e in ftp_client.list_directory("10.0.0.5", "12345678", "/")]
    assert "benchy.3mf" not in names


def test_delete_directory():
    ftp_client.delete_path("10.0.0.5", "12345678", "/apps", is_dir=True)
    names = [e.name for e in ftp_client.list_directory("10.0.0.5", "12345678", "/")]
    assert "apps" not in names


def test_delete_root_is_refused():
    with pytest.raises(ValueError):
        ftp_client.delete_path("10.0.0.5", "12345678", "/", is_dir=True)


def test_rename_keeps_the_file_in_its_folder():
    new_path = ftp_client.rename_path("10.0.0.5", "12345678", "/apps/notes.txt", "memo.txt")
    assert new_path == "/apps/memo.txt"
    assert [e.name for e in ftp_client.list_directory("10.0.0.5", "12345678", "/apps")] == ["memo.txt"]


def test_rename_returns_the_new_path_and_reports_the_call():
    FakeFTPS.last = None
    path = ftp_client.rename_path("10.0.0.5", "12345678", "/benchy.3mf", "desk-organizer.3mf")
    assert path == "/desk-organizer.3mf"
    assert ("rename", "/benchy.3mf", "/desk-organizer.3mf") in FakeFTPS.last.calls


def test_rename_refuses_a_name_that_escapes_the_folder():
    with pytest.raises(ValueError):
        ftp_client.rename_path("10.0.0.5", "12345678", "/benchy.3mf", "../escaped.3mf")
    # the file is still where it was
    assert "benchy.3mf" in FakeFTPS.tree["/"]


def test_rename_root_is_refused():
    with pytest.raises(ValueError):
        ftp_client.rename_path("10.0.0.5", "12345678", "/", "nope.3mf")


def test_upload_into_subdirectory(tmp_path):
    job = tmp_path / "part.3mf"
    job.write_bytes(b"data")
    assert ftp_client.upload_3mf_file("10.0.0.5", "12345678", str(job), "/cache") is True
    entries = ftp_client.list_directory("10.0.0.5", "12345678", "/cache")
    assert [e.name for e in entries] == ["part.3mf"]


def test_implicit_client_defaults():
    import inspect
    from ftplib import FTP_TLS

    assert issubclass(ImplicitFTP_TLS, FTP_TLS)
    assert inspect.signature(ImplicitFTP_TLS.connect).parameters["port"].default == 990


def test_connect_wraps_tls_before_reading_the_greeting(monkeypatch):
    """Regression: ftplib.FTP.connect reads the banner on a cleartext socket.

    On an implicit-TLS port that read can only ever time out, so connect()
    must open the socket, wrap it, and *then* read the welcome.
    """
    events = []

    class FakeSock:
        family = 2

        def __init__(self):
            self.wrapped = False

        def makefile(self, mode, encoding=None):
            return None

    raw_sock = FakeSock()

    class WrappedSock(FakeSock):
        def makefile(self, mode, encoding=None):
            events.append("makefile")
            return object()

    def fake_create_connection(address, timeout=None, source_address=None):
        events.append(("create_connection", address, timeout))
        return raw_sock

    class FakeContext:
        def wrap_socket(self, sock, server_hostname=None):
            events.append(("wrap_socket", server_hostname))
            sock.wrapped = True
            return WrappedSock()

    client = ImplicitFTP_TLS(context=FakeContext())
    monkeypatch.setattr(ftp_client.socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(client, "getresp", lambda: events.append("getresp") or "220 ready")

    assert client.connect(host="10.0.0.5", port=990, timeout=12) == "220 ready"
    assert raw_sock.wrapped is True
    assert events == [
        ("create_connection", ("10.0.0.5", 990), 12),
        ("wrap_socket", "10.0.0.5"),
        "makefile",
        "getresp",
    ]
