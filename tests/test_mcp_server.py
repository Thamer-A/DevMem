"""The MCP server: JSON-RPC handling in-process, and once as a real subprocess over stdio."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
from cli_helpers import ROOT, cli_env, make_project, write_session

from devmem import mcp_server
from devmem.mcp_server import Server

SID = "dddd4444-0000-0000-0000-000000000004"


@pytest.fixture()
def srv(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    from devmem.consent import claim_store
    claim_store(store)       # the user's own store (devmem notes a store it creates; these tests make it by hand)
    return Server(project, store), project, store


def rpc(server, method, params=None, id=1):
    msg = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        msg["params"] = params
    return server.handle(msg)


def call(server, name, args=None):
    r = rpc(server, "tools/call", {"name": name, "arguments": args or {}})
    return r["result"] if "result" in r else r


def text(result) -> str:
    return result["content"][0]["text"]


def write_schema2_session(store, project):
    """A hooks-style session: two turns; the second has MultiEdit, Write and a failed command."""
    (project / "src" / "wallet.py").write_text("def label():\n    return 'neg'\n", encoding="utf-8")
    rec = {"schema": 2, "session_id": SID, "provider": "claude-code", "capture_method": "hooks",
           "started_at": "2026-11-02T09:00:00Z", "ended_at": "2026-11-02T09:10:00Z",
           "turns": [
               {"index": 0, "started_at": "2026-11-02T09:00:00Z", "prompt": "add a wallet label",
                "assistant_text": [{"text": "I added the label.", "source": "assistant-visible"}],
                "result_text": "label added", "result_source": "assistant-visible",
                "tool_calls": [{"tool": "Write", "file": "src/wallet.py", "new": "def label():\n    return 'neg'\n"}],
                "files_read": [], "files_edited": ["src/wallet.py"], "files_deleted": [], "errors": []},
               {"index": 1, "started_at": "2026-11-02T09:05:00Z", "prompt": "now rename it to tag",
                "assistant_text": [], "result_text": "renamed", "result_source": "assistant-visible",
                "tool_calls": [{"tool": "MultiEdit", "file": "src/wallet.py", "old": "label", "new": "tag"},
                               {"tool": "Bash", "command": "pytest", "ok": False}],
                "files_read": [], "files_edited": ["src/wallet.py"], "files_deleted": [], "errors": ["boom"]}],
           "prompt": "add a wallet label", "result_text": "renamed", "result_source": "assistant-visible",
           "tool_calls": [{"tool": "Write", "file": "src/wallet.py", "new": "def label():\n    return 'neg'\n"},
                          {"tool": "MultiEdit", "file": "src/wallet.py", "old": "label", "new": "tag"},
                          {"tool": "Bash", "command": "pytest", "ok": False}],
           "files_read": [], "files_edited": ["src/wallet.py"], "files_deleted": [], "file_hashes_at_capture": {},
           "usage": {}, "assistant_text": [], "errors": ["boom"], "raw_record": "20261102T0900_dddd4444.jsonl"}
    (store / "sessions").mkdir(parents=True, exist_ok=True)
    (store / "sessions" / "20261102T0900_dddd4444.meta.json").write_text(json.dumps(rec), encoding="utf-8")
    (store / "sessions" / "20261102T0900_dddd4444.jsonl").write_text("{}\n", encoding="utf-8")


# ---------------------------------------------------------------- protocol

def test_initialize_negotiates_the_client_version_and_reports_the_package_version(srv):
    server, *_ = srv
    from devmem import __version__
    for ver in ("2025-11-25", "2025-06-18", "2024-11-05"):
        r = rpc(server, "initialize", {"protocolVersion": ver, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        assert r["result"]["protocolVersion"] == ver
        assert r["result"]["serverInfo"] == {"name": "devmemory", "version": __version__}
        assert "tools" in r["result"]["capabilities"] and "resultType" not in r["result"]
    r = rpc(server, "initialize", {"protocolVersion": "1999-01-01"})
    assert r["result"]["protocolVersion"] == "2025-11-25"  # the server's own latest legacy version


def test_notifications_get_no_reply_and_bad_input_is_an_error_not_a_crash(srv):
    server, *_ = srv
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert rpc(server, "no/such/method")["error"]["code"] == -32601
    assert server.handle([1, 2])["error"]["code"] == -32600
    assert rpc(server, "ping")["result"] == {}


def test_tools_list_is_stable_and_every_tool_has_an_object_schema(srv):
    server, *_ = srv
    a = rpc(server, "tools/list")["result"]["tools"]
    b = rpc(server, "tools/list")["result"]["tools"]
    assert a == b
    assert [t["name"] for t in a] == ["get_context_pack", "list_sessions", "read_session", "begin_session", "end_session"]
    for t in a:
        assert t["inputSchema"]["type"] == "object" and t["description"]
    end = next(t for t in a if t["name"] == "end_session")
    assert "pending_id" in end["inputSchema"]["properties"]


def test_modern_revision_discover_and_per_request_version(srv):
    server, *_ = srv
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}
    r = rpc(server, "server/discover", {"_meta": meta})["result"]
    assert r["resultType"] == "complete" and "2026-07-28" in r["supportedVersions"]
    assert r["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "devmemory"
    r = rpc(server, "tools/list", {"_meta": meta})["result"]
    assert r["resultType"] == "complete" and len(r["tools"]) == 5
    r = rpc(server, "tools/call", {"name": "list_sessions", "arguments": {}, "_meta": meta})["result"]
    assert r["resultType"] == "complete" and r["isError"] is False
    bad = rpc(server, "tools/list", {"_meta": {**meta, "io.modelcontextprotocol/protocolVersion": "1900-01-01"}})
    assert bad["error"]["code"] == -32022 and bad["error"]["data"]["requested"] == "1900-01-01"
    assert "2026-07-28" in bad["error"]["data"]["supported"]


def test_unknown_tool_is_a_protocol_error_and_a_failing_tool_is_an_error_result(srv):
    server, *_ = srv
    r = rpc(server, "tools/call", {"name": "nope", "arguments": {}})
    assert r["error"]["code"] == -32602 and "nope" in r["error"]["message"]
    r = call(server, "read_session", {"id": "zzzz"})
    assert r["isError"] is True and "no session" in text(r)
    r = call(server, "get_context_pack", {})
    assert r["isError"] is True and "task is required" in text(r)
    r = rpc(server, "tools/call", {"name": "list_sessions", "arguments": "oops"})
    assert r["error"]["code"] == -32602


# ---------------------------------------------------------------- tools

def test_context_pack_and_list_sessions(srv):
    server, project, store = srv
    write_session(store, SID, "keep the negative balance wallet label short")
    r = call(server, "get_context_pack", {"task": "negative balance wallet", "budget": 800})
    assert r["isError"] is False and "context pack" in text(r) and "wallet.py" in text(r)
    assert ".claude/" not in text(r)
    listing = text(call(server, "list_sessions"))
    assert "dddd4444" in listing and "1 turn(s)" in listing


def test_read_session_shows_turns_and_multiedit(srv):
    server, project, store = srv
    write_schema2_session(store, project)
    view = json.loads(text(call(server, "read_session", {"id": "dddd44"})))
    assert [t["prompt (written by the user)"] for t in view["turns"]] == ["add a wallet label", "now rename it to tag"]
    assert view["turns"][0]["assistant_text (assistant-visible)"] == ["I added the label."]
    tools = [c["tool"] for c in view["turns"][1]["tool_calls"]]
    assert tools == ["MultiEdit", "Bash"]
    assert [e["tool"] for e in view["edits"]] == ["Write", "MultiEdit"] and view["edits_total"] == 2
    assert view["in_progress"] is False and view["freshness"]["status"]
    assert view["turns"][1]["errors"] == ["boom"]


def test_read_session_caps_long_text(srv):
    server, project, store = srv
    write_schema2_session(store, project)
    p = next((store / "sessions").glob("*.meta.json"))
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["turns"][0]["prompt"] = "x" * 20000
    p.write_text(json.dumps(rec), encoding="utf-8")
    out = text(call(server, "read_session", {"id": SID}))
    assert len(out) < 12000 and "more characters" in out


def test_begin_returns_an_id_and_two_overlapping_sessions_do_not_collide(srv):
    server, project, store = srv
    t1 = text(call(server, "begin_session"))
    t2 = text(call(server, "begin_session"))
    id1, id2 = (t.split("pending id:")[1].split()[0] for t in (t1, t2))
    assert id1 != id2
    (project / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    r1 = call(server, "end_session", {"prompt": "first job", "pending_id": id1, "client": "cursor"})
    assert r1["isError"] is False and "recorded session" in text(r1)
    r2 = call(server, "end_session", {"prompt": "second job", "pending_id": id2, "summary": "did b"})
    assert r2["isError"] is False
    again = call(server, "end_session", {"prompt": "third", "pending_id": id1})
    assert again["isError"] is True  # already consumed
    prompts = sorted(json.loads(p.read_text(encoding="utf-8"))["prompt"] for p in (store / "sessions").glob("*.meta.json"))
    assert prompts == ["first job", "second job"]


def test_end_session_marks_the_prompt_as_reported_by_the_client_and_packs_say_so(srv):
    server, project, store = srv
    pid = text(call(server, "begin_session")).split("pending id:")[1].split()[0]
    (project / "src" / "wallet.py").write_text("def balance_label(amount):\n    return 'neg'\n", encoding="utf-8")
    call(server, "end_session", {"prompt": "make the negative balance label clearer", "pending_id": pid})
    rec = json.loads(next((store / "sessions").glob("*.meta.json")).read_text(encoding="utf-8"))
    assert rec["turns"][0]["prompt_source"] == "reported-by-client"
    pack = text(call(server, "get_context_pack", {"task": "the negative balance label is still unclear"}))
    assert "reported by the AI client" in pack and "User asked" not in pack
    view = json.loads(text(call(server, "read_session", {"id": rec["session_id"][:8]})))
    assert any("reported by the AI client" in k for k in view["turns"][0])


def test_end_session_without_begin_is_a_tool_error(srv):
    server, *_ = srv
    r = call(server, "end_session", {"prompt": "x"})
    assert r["isError"] is True and "begin" in text(r)


def test_calls_are_not_logged_by_default(srv):
    server, project, store = srv
    call(server, "list_sessions")
    assert not (store / "mcp_calls.jsonl").exists()


def test_calls_are_logged_when_enabled_and_arguments_are_redacted(srv):
    server, project, store = srv
    store.mkdir(exist_ok=True)
    (store / "config.json").write_text('{"mcp_call_log": true}', encoding="utf-8")
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    call(server, "list_sessions")
    call(server, "begin_session")
    r = call(server, "end_session", {"prompt": f"use token {secret} for the deploy", "summary": f"key={secret}"})
    raw = (store / "mcp_calls.jsonl").read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert json.loads(lines[0])["tool"] == "list_sessions" and len(lines) == 3
    assert secret not in raw, "a token passed to end_session must not reach the call log"
    assert "deploy" in raw   # the rest of the argument is kept
    assert r["isError"] is False


# ---------------------------------------------------------------- real stdio subprocess

def test_stdio_subprocess_round_trip(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    write_session(store, SID, "negative balance wallet label")
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_context_pack", "arguments": {"task": "negative balance − wallet"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "list_sessions", "arguments": {}}},
    ]
    stdin = "\n".join(json.dumps(m) for m in msgs) + "\nnot json\n"
    r = subprocess.run([sys.executable, "-m", "devmem", "mcp", "--project", str(project)], input=stdin.encode("utf-8"),
                       capture_output=True, env=cli_env(PYTHONIOENCODING="cp1252"), cwd=tmp_path, timeout=120)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    replies = [json.loads(l) for l in r.stdout.decode("utf-8").splitlines()]
    by_id = {x.get("id"): x for x in replies}
    assert len(replies) == 5  # 4 requests + the parse error; the notification got no reply
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert len(by_id[2]["result"]["tools"]) == 5
    assert "context pack" in by_id[3]["result"]["content"][0]["text"]
    assert "dddd4444" in by_id[4]["result"]["content"][0]["text"]
    assert by_id[None]["error"]["code"] == -32700
    assert mcp_server.MODERN_PROTOCOL == "2026-07-28" and ROOT.is_dir()


def test_stdio_server_survives_malformed_lines(tmp_path):
    """One bad line (invalid UTF-8, deeply nested JSON, a huge line, a request that blows up inside the handler)
    gets an error reply; the server keeps serving and the next ping is answered. No traceback or path leaks."""
    project = make_project(tmp_path)
    ping = lambda i: json.dumps({"jsonrpc": "2.0", "id": i, "method": "ping"}).encode("utf-8")  # noqa: E731
    lines = [
        b"\xff\xfe{not utf-8 \x80\x81}",                        # invalid UTF-8
        ping(1),
        b"[" * 100_000,                                          # RecursionError inside json.loads
        ping(2),
        b'{"jsonrpc": "2.0", "id": 9, "method": "x", "pad": "' + b"a" * (mcp_server.MAX_LINE_BYTES + 10) + b'"}',
        ping(3),
        b'{"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "list_sessions", "arguments": {}},'
        b' "id2": ' + b"[" * 50 + b"]" * 50 + b"}",
        ping(4),
    ]
    r = subprocess.run([sys.executable, "-m", "devmem", "mcp", "--project", str(project), "--require-store"],
                       input=b"\n".join(lines) + b"\n", capture_output=True, env=cli_env(), cwd=tmp_path, timeout=120)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    out = r.stdout.decode("utf-8")
    replies = [json.loads(ln) for ln in out.splitlines()]
    ids = [x.get("id") for x in replies]
    for i in (1, 2, 3, 4):
        assert ids.count(i) == 1, (i, out[:2000])
    errors = [x["error"]["code"] for x in replies if "error" in x]
    assert errors.count(-32700) >= 3, out[:2000]   # bad UTF-8, too deep, too long
    assert "Traceback" not in out and str(tmp_path) not in out


def test_unexpected_handler_exception_is_an_internal_error_without_details(srv, monkeypatch):
    server, project, store = srv

    def boom(*a, **k):
        raise RuntimeError(f"secret path {project}")
    monkeypatch.setattr(server, "handle", boom)
    out = mcp_server.respond(server, b'{"jsonrpc": "2.0", "id": 5, "method": "ping"}')
    assert out is not None and out["id"] == 5 and out["error"]["code"] == -32603
    assert str(project) not in json.dumps(out)


def test_call_log_switch_reads_false_strings_as_off(srv):
    server, project, store = srv
    store.mkdir(exist_ok=True)
    for value, expect in (("false", False), ("off", False), ("true", True), (False, False)):
        (store / "config.json").write_text(json.dumps({"mcp_call_log": value}), encoding="utf-8")
        assert mcp_server.call_log_enabled(store) is expect, value
