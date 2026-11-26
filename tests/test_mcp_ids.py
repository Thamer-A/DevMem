"""V8: the MCP server never writes Infinity or NaN and never echoes an id that is not a JSON-RPC id.

`{"id": 1e999, ...}` parses to float('inf'), which json.dumps wrote back as `Infinity` (not JSON); an object or a
list was echoed as the id too.
"""
from __future__ import annotations

import json

import pytest
from cli_helpers import make_project

from devmem import mcp_server
from devmem.mcp_server import Server


@pytest.fixture()
def srv(tmp_path):
    project = make_project(tmp_path)
    return Server(project, project / ".devmemory")


def _reply(srv, line: str) -> dict:
    out = mcp_server.respond(srv, line.encode("utf-8"))
    assert out is not None
    raw = mcp_server._encode(out).decode("utf-8")
    assert "Infinity" not in raw and "NaN" not in raw, raw
    return json.loads(raw)       # strict JSON: would fail on Infinity/NaN


@pytest.mark.parametrize("mid", ["1e999", "-1e999", "{\"a\": 1}", "[1]", "true"])
def test_a_non_scalar_or_non_finite_id_is_an_invalid_request(srv, mid):
    r = _reply(srv, '{"jsonrpc": "2.0", "id": %s, "method": "tools/list"}' % mid)
    assert r["id"] is None and r["error"]["code"] == -32600


def test_valid_ids_are_echoed(srv):
    for mid in ('"abc"', "7", "2.5"):
        r = _reply(srv, '{"jsonrpc": "2.0", "id": %s, "method": "tools/list"}' % mid)
        assert r["id"] == json.loads(mid) and "result" in r


def test_a_non_finite_value_anywhere_in_a_reply_becomes_an_internal_error():
    raw = mcp_server._encode({"jsonrpc": "2.0", "id": 3, "result": {"x": float("nan")}}).decode("utf-8")
    r = json.loads(raw)
    assert r["id"] == 3 and r["error"]["code"] == -32603
