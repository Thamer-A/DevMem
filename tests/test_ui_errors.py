"""G3: what the viewer prints when a connection fails, and an unusable project map.

socketserver's default handle_error printed a full traceback (with the paths of the installed package) to the user's
terminal for every dropped connection. Ordinary connection errors are now quiet; anything else is one short line with
the exception type only (DEVMEM_DEBUG=1 prints the traceback) and is kept in `server.handler_errors`, so a test can
still see a real bug.

An index/map.json that is a FIFO, a link or a folder made /api/overview, /api/files and /api/graph answer HTTP 500
{"error": "FileNotFoundError"} (verify3/linux/G/ui_fifo.py); it now counts as unreadable, like a garbled map.
"""
from __future__ import annotations

import os
import socket
import struct
import time

import pytest
from ui_helpers import make_project, request, running_server, with_token

from devmem import ui_server


def _reset(port: int, data: bytes) -> None:
    """Send `data` and close with a TCP reset (SO_LINGER 0), as a browser tab closed mid-request can."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(data)
    time.sleep(0.2)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    s.close()


def test_a_dropped_connection_prints_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("DEVMEM_DEBUG", raising=False)
    project, store = make_project(tmp_path)
    with running_server(project, store) as (port, st):
        for _ in range(3):
            _reset(port, b"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1")    # the headers never finish
        time.sleep(0.5)
        assert request(port, "GET", "/api/state", headers=with_token(st))[0] == 200    # still serving
    err = capsys.readouterr().err
    assert "Traceback" not in err and "Exception occurred" not in err, err


def _raise_into_handle_error(srv, exc: BaseException) -> None:
    try:
        raise exc
    except BaseException:
        srv.handle_error(None, ("127.0.0.1", 1))


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_server, "recents_file", lambda *a, **k: tmp_path / "recents.json")
    monkeypatch.delenv("DEVMEM_DEBUG", raising=False)
    project, store = make_project(tmp_path)
    srv, _st = ui_server.make_server(project, store, port=0)
    try:
        yield srv
    finally:
        srv.server_close()


@pytest.mark.parametrize("exc", [ConnectionResetError(10054, "reset"), BrokenPipeError(32, "pipe"),
                                 ConnectionAbortedError(10053, "aborted"), TimeoutError("timed out"),
                                 OSError(10057, "not connected")])
def test_connection_errors_are_quiet(server, capsys, exc):
    _raise_into_handle_error(server, exc)
    assert capsys.readouterr().err == ""
    assert server.handler_errors == []


def test_another_error_is_one_short_line_without_its_message(server, capsys):
    secret_path = os.path.join("C:" if os.name == "nt" else "/", "Users", "someone", "private", "notes.txt")
    _raise_into_handle_error(server, ValueError(f"cannot read {secret_path}"))
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "ValueError" in err and "DEVMEM_DEBUG" in err, err
    assert "Traceback" not in err and "private" not in err, err
    assert server.handler_errors == ["ValueError"]


def test_debug_prints_the_traceback(server, capsys, monkeypatch):
    monkeypatch.setenv("DEVMEM_DEBUG", "1")
    _raise_into_handle_error(server, KeyError("k"))
    assert "Traceback" in capsys.readouterr().err
    assert server.handler_errors == ["KeyError"]


# ---- an unusable index/map.json ----

def _unusable_map(store, kind: str) -> None:
    p = store / "index" / "map.json"
    p.unlink()
    if kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("no FIFOs here")
        os.mkfifo(p)
    elif kind == "link":
        target = store.parent / "elsewhere.json"
        target.write_text('{"files": {"evil.py": {}}}', encoding="utf-8")
        try:
            os.symlink(target, p)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a symbolic link here")
    elif kind == "folder":
        p.mkdir()
    else:
        p.write_text("{not json", encoding="utf-8")


@pytest.mark.parametrize("kind", ["fifo", "link", "folder", "garbled"])
def test_an_unusable_map_is_treated_like_a_garbled_one(tmp_path, kind):
    project, store = make_project(tmp_path)
    _unusable_map(store, kind)
    with running_server(project, store) as (port, st):
        for path in ("/api/overview", "/api/files", "/api/graph"):
            status, _h, body = request(port, "GET", path, headers=with_token(st))
            assert status == 200, (path, status, body)
        _s, _h, files = request(port, "GET", "/api/files", headers=with_token(st))
    assert "evil.py" not in str(files)      # a linked map is never read
    if kind != "folder":                    # rebuilt by a scan, which replaces the entry (a folder cannot be)
        assert "src/main.py" in str(files)
        p = store / "index" / "map.json"
        assert p.is_file() and not p.is_symlink()
