"""Small hardening: the MCP call log's entry size, the redact patterns init approves are listed, and the viewer's
standard error replies."""
from __future__ import annotations

import json
import socket

from cli_helpers import make_project
from ui_helpers import running_server

from devmem import cli
from devmem.mcp_server import CALL_LOG_ENTRY_CAP, Server


def test_one_call_log_entry_is_bounded(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    srv = Server(project, store)
    srv.call("list_sessions", {})                       # creates (and claims) the store
    (store / "config.json").write_text('{"mcp_call_log": true}', encoding="utf-8")
    srv.call("list_sessions", {"extra": "wallet " * 20000})
    lines = (store / "mcp_calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines and all(len(ln) <= CALL_LOG_ENTRY_CAP + 200 for ln in lines), [len(ln) for ln in lines]
    assert json.loads(lines[-1])["args_chars"] > CALL_LOG_ENTRY_CAP


def test_init_lists_the_redact_patterns_it_approves(tmp_path, monkeypatch, capsys):
    project = make_project(tmp_path)
    (project / ".devmemory").mkdir()
    (project / ".devmemory" / "redact").write_text("# mine\nACME-[0-9]{6}\n", encoding="utf-8")
    monkeypatch.chdir(project)
    assert cli.main(["init", "--no-scan", "--adopt-store"]) == 0
    out = capsys.readouterr().out
    assert "approved the 1 pattern(s)" in out and "ACME-[0-9]{6}" in out


def _raw(port: int, data: bytes, wait: float = 5.0) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=wait) as s:
        s.sendall(data)
        out = b""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
        except OSError:
            pass
    return out


def test_standard_error_replies_carry_the_security_headers(tmp_path):
    project = make_project(tmp_path)
    with running_server(project, None) as (port, _st):
        for req in (b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: chunked\r\n\r\n",
                    b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 999999999\r\n\r\n",
                    b"BREW / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
                    b"GARBAGE\r\n\r\n"):
            head = _raw(port, req).split(b"\r\n\r\n", 1)[0].lower()
            assert head.startswith(b"http/1."), (req, head)
            assert b"content-security-policy:" in head and b"cache-control: no-store" in head, (req, head)
