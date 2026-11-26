"""V-X4: the viewer never stat-s a network path from a request. V-X5: at most MAX_CONNECTIONS connections at once."""
from __future__ import annotations

import os
import pathlib
import socket
import time

from ui_helpers import make_project, request, running_server, with_token

from devmem import ui_server


def test_open_and_reveal_refuse_a_unc_path_without_touching_it(tmp_path, monkeypatch):
    project, store = make_project(tmp_path)
    touched = []
    real_stat, real_exists = os.stat, pathlib.Path.exists

    def stat(p, *a, **k):
        if os.fspath(p).replace("/", "\\").startswith("\\\\"):
            touched.append(os.fspath(p))
        return real_stat(p, *a, **k)
    with running_server(project, store) as (port, st):
        monkeypatch.setattr(os, "stat", stat)
        for path in ("/api/open?project=%5C%5Cunc-probe.invalid%5Cshare", "/api/reveal?dry=1&path=%5C%5Cunc-probe.invalid%5Cs%5Cx",
                     "/api/open?project=//unc-probe.invalid/share"):
            code, _h, _b = request(port, "POST", path, with_token(st))
            assert code == 400, path
        monkeypatch.setattr(os, "stat", real_stat)
    assert touched == [] and real_exists


def test_connections_beyond_the_cap_are_closed_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_server, "MAX_CONNECTIONS", 3)
    monkeypatch.setattr(ui_server.ViewerServer, "max_connections", 3)
    project, store = make_project(tmp_path)
    with running_server(project, store) as (port, st):
        held = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(3)]
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            extra.sendall(b"GET / HTTP/1.0\r\n\r\n")
            data = extra.recv(100)
        except (ConnectionResetError, ConnectionAbortedError):
            data = b""
        assert data == b""
        for s in held:
            s.close()
        extra.close()
        time.sleep(0.5)
        assert request(port, "GET", "/api/state", with_token(st))[0] == 200     # free again
