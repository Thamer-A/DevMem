"""Every hook event finishes in bounded time whatever the shape of its payload (security review R4 and R3).

A MultiEdit had no cap on its number of edits, and each edit is redacted: 1000 edits of 4.9 KB took 18.7 s, over the
15 s PostToolUse timeout. Now at most sessions.MAX_EDITS edits (and EDITS_TEXT_BUDGET characters) are kept per event
and the event says how many were left out. The sweep runs payloads of about 2 MB in many shapes through the hook
in-process and asserts a generous absolute bound per event.
"""
from __future__ import annotations

import json
import time

import pytest
from hooks_helpers import make_project

from devmem import hooks
from devmem import sessions as S

BOUND_S = 5.0
MB2 = 2 * 1024 * 1024


def _nested(depth: int) -> dict:
    d: dict = {"leaf": "x" * 100}
    for _ in range(depth):
        d = {"a": d, "path": "p/" * 10}
    return d


def _nested_long(depth: int) -> dict:
    d: dict = {"leaf": "x" * 100}
    for i in range(depth):
        d = {"k" * 4000 + str(i): d, "v" * 4000 + str(i): "password: k9Lm2QzX\n" * 200}
    return d


RESPONSES = {
    "response_long_keys": {"data": {f"{i:06d}" + "password: k9Lm2QzX\n" * 280: "v" for i in range(400)}},
    "response_deep_long_keys": _nested_long(25),
    "response_lines": {"structuredPatch": [{"lines": ["+password: k9Lm2QzX"] * (MB2 // 20)}]},
}


@pytest.mark.parametrize("shape", sorted(RESPONSES))
def test_each_response_shape_is_handled_in_bounded_time(tmp_path, monkeypatch, shape):
    project = make_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    payload = {"session_id": "sweep-resp", "cwd": str(project), "hook_event_name": "PostToolUse",
               "tool_name": "mcp__x__y", "tool_use_id": "t1", "tool_input": {"q": 1}, "tool_response": RESPONSES[shape]}
    data = json.dumps(payload).encode("utf-8")
    t = time.perf_counter()
    rc, _ = hooks.run(["PostToolUse"], data)
    dt = time.perf_counter() - t
    assert rc == 0 and dt < BOUND_S, f"{shape}: {dt:.2f} s for {len(data)} bytes"
    store = project / ".devmemory"
    assert not (store / "hook_errors.log").exists()
    assert "k9Lm2Qz" not in (store / "live" / "sweep-resp.jsonl").read_text(encoding="utf-8")


def _shapes(project) -> dict[str, tuple[str, dict]]:
    secretish = "curl -ux:pw " * (MB2 // 12)
    return {
        "multiedit_many_big": ("MultiEdit", {"file_path": str(project / "a.py"),
                                             "edits": [{"old_string": "x" * 2000, "new_string": secretish[:2000]}]
                                             * 1000}),
        "multiedit_many_tiny": ("MultiEdit", {"file_path": str(project / "a.py"),
                                              "edits": [{"old_string": "a", "new_string": "b"}] * 50000}),
        "multiedit_huge_strings": ("MultiEdit", {"file_path": str(project / "a.py"),
                                                 "edits": [{"old_string": secretish[:MB2 // 4],
                                                            "new_string": secretish[:MB2 // 4]}] * 4}),
        "read_long_path": ("Read", {"file_path": "a/" * (MB2 // 2)}),
        "grep_long_path": ("Grep", {"pattern": "x", "path": "a\\" * (MB2 // 2)}),
        "mcp_many_long_paths": ("mcp__fs__read_multiple_files", {"paths": ["a/" * 2048] * 500}),
        "mcp_many_short_paths": ("mcp__fs__read_multiple_files", {"paths": [f"f{i}.py" for i in range(100000)]}),
        "deep_nesting": ("mcp__x__y", _nested(400)),
        "many_keys": ("mcp__x__y", {f"k{i}": f"v{i}" for i in range(60000)}),
        # long keys (each redacted as text before 1bef56b capped them) and deep nesting under long keys
        "many_long_keys": ("mcp__x__y", {f"{i:06d}" + "password: k9Lm2QzX\n" * 280: "v" for i in range(400)}),
        "deep_long_keys": ("mcp__x__y", _nested_long(25)),
        "many_strings": ("mcp__x__y", {"a": [f"s{i}" for i in range(250000)]}),
        "write_huge": ("Write", {"file_path": str(project / "b.py"), "content": secretish[:MB2]}),
        "bash_huge": ("Bash", {"command": "echo " + "a/" * (MB2 // 2)}),
        "edit_huge": ("Edit", {"file_path": str(project / "a.py"), "old_string": secretish[:MB2 // 2],
                               "new_string": secretish[:MB2 // 2]}),
    }


@pytest.mark.parametrize("shape", list(_shapes(__import__("pathlib").Path("p")).keys()))
def test_each_payload_shape_is_handled_in_bounded_time(tmp_path, monkeypatch, shape):
    project = make_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    tool, inp = _shapes(project)[shape]
    payload = {"session_id": "sweep-session", "cwd": str(project), "hook_event_name": "PostToolUse",
               "tool_name": tool, "tool_use_id": "t1", "tool_input": inp,
               "tool_response": {"stdout": "o" * MB2, "stderr": ""}}
    data = json.dumps(payload).encode("utf-8")
    t = time.perf_counter()
    rc, _ = hooks.run(["PostToolUse"], data)
    dt = time.perf_counter() - t
    assert rc == 0
    assert dt < BOUND_S, f"{shape}: {dt:.2f} s for {len(data)} bytes"
    store = project / ".devmemory"
    assert not (store / "hook_errors.log").exists(), (store / "hook_errors.log").read_text(encoding="utf-8")
    ev = S.read_events(store / "live" / "sweep-session.jsonl")[-1]
    if tool == "MultiEdit":
        calls = ev["calls"]
        assert len(calls) <= S.MAX_EDITS
        assert sum(len(c.get("old") or "") + len(c.get("new") or "") for c in calls) <= S.EDITS_TEXT_BUDGET + 2 * 4000
        if calls[0].get("input_not_checked"):        # over the check's bounds: recorded as that, no edit kept
            assert len(calls) == 1 and "old" not in calls[0]
        elif len(inp["edits"]) > len(calls):
            assert calls[-1]["truncated"] and calls[-1]["edits_omitted"] == len(inp["edits"]) - len(calls)


def test_a_stop_with_a_huge_message_is_bounded(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    payload = {"session_id": "sweep-stop", "cwd": str(project), "hook_event_name": "Stop",
               "last_assistant_message": "curl -ux:pw " * (MB2 // 12), "transcript_path": str(project / "none")}
    t = time.perf_counter()
    assert hooks.run(["Stop"], json.dumps(payload).encode("utf-8"))[0] == 0
    assert time.perf_counter() - t < BOUND_S
