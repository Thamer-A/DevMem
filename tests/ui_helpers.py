"""Helpers for the app-server tests: a tiny project, a running server on a free port, raw HTTP requests."""
from __future__ import annotations

import http.client
import json
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from devmem.scan import scan
from devmem import ui_server
from devmem.ui_server import TOKEN_HEADER, make_server


def make_project(root: Path) -> tuple[Path, Path]:
    project = root / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    (project / "README.md").write_text("# demo\n", encoding="utf-8")
    store = project / ".devmemory"
    scan(project, store)
    return project, store


@contextmanager
def running_server(project: Path | None, store: Path | None):
    # make_server records the project in the per-user "recent projects" file; keep tests out of the real one
    scratch = Path(tempfile.mkdtemp(prefix="devmem-recents-")) / "recents.json"
    real = ui_server.recents_file
    ui_server.recents_file = lambda *a, **k: scratch
    srv, st = make_server(project, store, port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1], st
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)
        ui_server.recents_file = real
        shutil.rmtree(scratch.parent, ignore_errors=True)
    # an exception that reached the server's handle_error (printed as one short line) is a bug the test should see
    assert srv.handler_errors == [], srv.handler_errors


def request(port: int, method: str, path: str, headers: dict | None = None, host: str | None = None):
    """Raw request so tests control Host/Origin exactly. Returns (status, response headers, parsed body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        h = {"Host": host or f"127.0.0.1:{port}"}
        h.update(headers or {})
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            conn.putheader(k, v)
        if method == "POST":
            conn.putheader("Content-Length", "0")
        conn.endheaders()
        r = conn.getresponse()
        raw = r.read()
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = raw
        return r.status, dict(r.getheaders()), body
    finally:
        conn.close()


def with_token(st, extra: dict | None = None) -> dict:
    return {TOKEN_HEADER: st.token, **(extra or {})}
