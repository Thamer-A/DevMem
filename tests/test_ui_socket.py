"""The viewer's listening socket and connections: nobody else can bind its port, a restart on the same port works at
once, and a slow, oversized or never-finished request cannot hold a server thread."""
from __future__ import annotations

import socket
import threading
import time

import pytest
from ui_helpers import make_project, request, running_server, with_token

from devmem import ui_server


def _bind_with_reuse(port: int) -> socket.socket | None:
    """A socket bound to 127.0.0.1:port with SO_REUSEADDR (what a hijacker would use), or None when the bind fails."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        s.close()
        return None
    return s


def test_another_socket_cannot_bind_the_viewers_port(tmp_path):
    project, store = make_project(tmp_path)
    with running_server(project, store) as (port, _st):
        other = _bind_with_reuse(port)
        if other is not None:
            other.close()
        assert other is None, "a second socket could bind the viewer's port (SO_REUSEADDR on Windows)"


def test_the_viewer_can_start_again_at_once_on_the_same_port(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_server, "recents_file", lambda *a, **k: tmp_path / "recents.json")
    project, store = make_project(tmp_path)
    srv, st = ui_server.make_server(project, store, port=0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    for _ in range(5):        # connections the server closes first leave TIME_WAIT entries on its port
        assert request(port, "GET", "/api/state", with_token(st))[0] == 200
    srv.shutdown()
    srv.server_close()
    t.join(5)
    srv2, _st2 = ui_server.make_server(project, store, port=port)
    srv2.server_close()


def test_a_request_body_over_the_cap_is_refused(tmp_path):
    project, store = make_project(tmp_path)
    with running_server(project, store) as (port, st):
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            s.sendall((f"POST /api/state HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{ui_server.TOKEN_HEADER}: {st.token}\r\n"
                       f"Content-Length: {ui_server.MAX_BODY + 1}\r\n\r\n").encode())
            head = s.recv(4096).decode("latin-1")
        finally:
            s.close()
        assert head.startswith("HTTP/1.0 413") or head.startswith("HTTP/1.1 413"), head
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            s.sendall((f"POST /api/state HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{ui_server.TOKEN_HEADER}: {st.token}\r\n"
                       "Transfer-Encoding: chunked\r\n\r\n").encode())
            head = s.recv(4096).decode("latin-1")
        finally:
            s.close()
        assert " 411 " in head.split("\r\n")[0] or " 413 " in head.split("\r\n")[0], head


@pytest.mark.parametrize("partial", [b"", b"GET /api/sta", b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Slow: "])
def test_a_request_that_never_finishes_is_dropped(tmp_path, monkeypatch, partial):
    monkeypatch.setattr(ui_server, "REQUEST_READ_SECONDS", 0.5)
    project, store = make_project(tmp_path)
    with running_server(project, store) as (port, _st):
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            s.sendall(partial)
            started = time.monotonic()
            try:
                data = s.recv(4096)
            except (ConnectionResetError, ConnectionAbortedError):
                data = b""
            elapsed = time.monotonic() - started
        finally:
            s.close()
        assert data == b"" or data.startswith(b"HTTP/1.0 408"), data
        assert elapsed < 8, elapsed
