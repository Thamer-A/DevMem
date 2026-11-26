"""Stress tests: cut-down ports of the security review's fuzz harnesses (sec/fuzz/h1, h2, h3, h8). Too slow for every
push, so they carry the `stress` marker, which the default run and CI skip (pyproject.toml addopts). Run them with

    python -m pytest tests -m stress -q -p no:cacheprovider

They use temp folders only and need nothing beyond the standard library and pytest.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cli_helpers import PKG_ROOT, cli_env
from hooks_helpers import TURN1, load_payload, make_project
from test_connect import do_connect

from devmem import connect as C
from devmem import hooks
from devmem.sessions import load_sessions, read_events

pytestmark = pytest.mark.stress


def _hook(project: Path, event: str, payload, timeout: int = 120) -> subprocess.CompletedProcess:
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    env = cli_env(CLAUDE_PROJECT_DIR=str(project), PYTHONPATH=str(PKG_ROOT))
    return subprocess.run([sys.executable, "-m", "devmem", "hook", event], input=data, capture_output=True, env=env,
                          timeout=timeout, cwd=str(project))


def _payload(project: Path, sid: str, event: str, **kw) -> dict:
    p = {"session_id": sid, "cwd": str(project), "hook_event_name": event, "transcript_path": str(project / "none")}
    p.update(kw)
    return p


# ---- h3: concurrency ---------------------------------------------------------------------------------------------

def test_50_parallel_tool_events_of_one_session_are_all_recorded(tmp_path):
    project = make_project(tmp_path)
    sid = "stress-one-session"
    assert _hook(project, "SessionStart", _payload(project, sid, "SessionStart", source="startup")).returncode == 0
    assert _hook(project, "UserPromptSubmit", _payload(project, sid, "UserPromptSubmit", prompt="p",
                                                       prompt_id="p1")).returncode == 0

    def tool(i):
        return _hook(project, "PostToolUse", _payload(project, sid, "PostToolUse", tool_name="Bash", prompt_id="p1",
                                                      tool_use_id=f"t{i}", tool_input={"command": f"echo {i}"},
                                                      tool_response={"stdout": str(i)}))
    with cf.ThreadPoolExecutor(16) as ex:
        results = list(ex.map(tool, range(50)))
    assert all(r.returncode == 0 and not r.stderr for r in results)
    ids = [e.get("tool_use_id") for e in read_events(project / ".devmemory" / "live" / f"{sid}.jsonl")
           if e.get("event") == "PostToolUse"]
    assert sorted(ids) == sorted(f"t{i}" for i in range(50))


def test_50_sessions_in_parallel_each_get_their_own_record(tmp_path):
    project = make_project(tmp_path)

    def life(i):
        sid = f"stress-{i:03d}-session"
        steps = [("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": f"prompt {i}", "prompt_id": "p"}),
                 ("PostToolUse", {"tool_name": "Bash", "tool_use_id": f"t{i}", "prompt_id": "p",
                                  "tool_input": {"command": "ls"}, "tool_response": {"stdout": "x"}}),
                 ("Stop", {"last_assistant_message": f"answer {i}", "prompt_id": "p"}), ("SessionEnd", {"reason": "exit"})]
        return [_hook(project, ev, _payload(project, sid, ev, **kw)).returncode for ev, kw in steps]
    with cf.ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(life, range(50)))
    assert all(c == 0 for row in codes for c in row)
    recs = {r["session_id"]: r for r in load_sessions(project / ".devmemory")}
    assert len(recs) == 50
    for i in range(50):
        r = recs[f"stress-{i:03d}-session"]
        assert [t["prompt"] for t in r["turns"]] == [f"prompt {i}"]
        assert [c.get("command") for t in r["turns"] for c in t["tool_calls"] if c.get("tool") == "Bash"] == ["ls"]


# ---- h8: connect / disconnect round trip -------------------------------------------------------------------------

def _random_text(rnd: random.Random) -> str:
    alphabet = ["# Notes", "line", "", "  indented", "<!-- devmemory -->", "`code`", "é中", "\t", "x" * 200]
    eol = rnd.choice(["\n", "\r\n"])
    body = eol.join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 30)))
    return body + (eol if rnd.random() < 0.7 else "")


def _random_json(rnd: random.Random) -> str:
    obj = {"mcpServers": {f"s{i}": {"command": "x", "args": [str(rnd.random())]} for i in range(rnd.randint(0, 4))}}
    if rnd.random() < 0.3:
        obj["other"] = [1, {"a": None}, "é"]
    return json.dumps(obj, indent=rnd.choice([None, 2, 4]))


def test_connect_then_disconnect_restores_a_few_hundred_files_byte_for_byte(tmp_path):
    rnd = random.Random(20261114)
    for n in range(200):
        project = tmp_path / f"p{n}"
        project.mkdir()
        files = {}
        if rnd.random() < 0.8:
            files["AGENTS.md"] = (_random_text(rnd) if rnd.random() < 0.9 else "﻿" + _random_text(rnd)).encode()
        if rnd.random() < 0.6:
            (project / ".cursor").mkdir()
            files[".cursor/mcp.json"] = _random_json(rnd).encode()
        for rel, data in files.items():
            (project / rel).write_bytes(data)
        try:
            do_connect(project, "cursor")
        except C.ConnectError:
            for rel, data in files.items():                      # a refused connect changes nothing
                assert (project / rel).read_bytes() == data, (n, rel)
            continue
        C.disconnect(project, "cursor")
        for rel, data in files.items():
            assert (project / rel).read_bytes() == data, (n, rel)
        for rel in ("AGENTS.md", ".cursor/mcp.json"):
            if rel not in files:
                assert not (project / rel).exists(), (n, rel)


# ---- h1: every field of every recorded payload set to every wrong type -------------------------------------------

WRONG = [None, 0, -1, 1.5, True, "", "x" * 5000, [], [1, "a"], {}, {"a": {"b": []}}]


def test_every_field_with_every_wrong_type_never_breaks_a_hook(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    t0 = time.monotonic()
    cases = 0
    for name in TURN1:
        base = load_payload(name, project)
        event = name.split("_", 1)[1]
        for field in list(base):
            for value in WRONG:
                payload = dict(base, **{field: value})
                code, out = hooks.run([event], json.dumps(payload).encode("utf-8"))
                assert code == 0 and isinstance(out, str), (name, field, value)
                cases += 1
            payload = {k: v for k, v in base.items() if k != field}       # the field missing
            assert hooks.run([event], json.dumps(payload).encode("utf-8"))[0] == 0
    assert cases > 300 and time.monotonic() - t0 < 600
    for p in (project / ".devmemory" / "sessions").glob("*.meta.json"):  # what was written still parses
        json.loads(p.read_text(encoding="utf-8"))


# ---- h2: raw stdin shapes through the real command -------------------------------------------------------------

def _stdin_shapes(project: Path) -> dict[str, bytes]:
    good = json.dumps(_payload(project, "stress-stdin", "UserPromptSubmit", prompt="hello", prompt_id="p")).encode()
    return {
        "empty": b"", "space": b"   \n", "truncated": good[:len(good) // 2], "not-json": b"hello world",
        "invalid-utf8": b"\xff\xfe\xfa" + good, "bom": b"\xef\xbb\xbf" + good, "crlf": good + b"\r\n",
        "nul": good[:10] + b"\x00" + good[10:], "array": b"[1, 2, 3]", "number": b"42", "deep": b"[" * 100_000,
        "huge-prompt": json.dumps(_payload(project, "stress-stdin", "UserPromptSubmit", prompt="a " * 2_000_000,
                                           prompt_id="p")).encode(),
        "bad-session-id": json.dumps(_payload(project, "../../etc/x", "UserPromptSubmit", prompt="p")).encode(),
        "unicode-separators": json.dumps(_payload(project, "stress-stdin", "UserPromptSubmit",
                                                  prompt="a b c\u0085d", prompt_id="p2")).encode(),
    }


def test_raw_stdin_shapes_exit_0_quietly_and_quickly(tmp_path):
    project = make_project(tmp_path)
    before = {p.name for p in tmp_path.rglob("*")}
    for name, data in _stdin_shapes(project).items():
        t = time.monotonic()
        r = _hook(project, "UserPromptSubmit", data)
        dt = time.monotonic() - t
        assert r.returncode == 0 and r.stderr == b"", (name, r.stderr[-500:])
        assert dt < 10, (name, dt)
    created = {p.name for p in tmp_path.rglob("*")} - before
    assert not [n for n in created if n in ("etc", "x")], created            # a hostile id never named a path
    assert os.path.isdir(project / ".devmemory")
