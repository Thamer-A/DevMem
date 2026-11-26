"""H1: a begin file is data. Its start commit must be a commit id, every field is checked, and `end` without an id
never takes a begin file that git tracks or that is dated in the future.

A begin file's `head` reached `git diff` unchecked: `"head": "--output=<path>"` made git write a file anywhere,
with content the project controls (reviewer's verify3/argv/repro_end_output.py). Reachable by `devmem end` on a
project that ships `.devmemory/pending/<id>.json` (also after the user's own `begin`, when the shipped file has a
far-future mtime), and by MCP end_session without pending_id.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest
from cli_helpers import run_cli

from devmem import generic as G
from devmem.mcp_server import Server


def _git(p, *a):
    subprocess.run(["git", "-C", str(p), "-c", "user.email=a@b.invalid", "-c", "user.name=a", *a], check=True,
                   capture_output=True)


@pytest.fixture()
def shipped(tmp_path):
    """A repository that ships a begin file whose start commit is a git option writing `target`."""
    p = tmp_path / "proj"
    p.mkdir()
    (p / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(p, "init", "-q")
    _git(p, "add", "-A")
    _git(p, "commit", "-qm", "one")
    target = tmp_path / "outside" / "evil.txt"
    target.parent.mkdir()
    pending = p / ".devmemory" / "pending"
    pending.mkdir(parents=True)
    evil = {"id": "20990101T000000_deadbeef", "started_at": "2099-01-01T00:00:00Z", "kind": "git", "v": 2,
            "head": f"--output={target}", "dirty": {}}
    f = pending / "20990101T000000_deadbeef.json"
    f.write_text(json.dumps(evil), encoding="utf-8")
    _git(p, "add", "-f", str(f))
    _git(p, "commit", "-qm", "ship a begin")
    future = time.time() + 10 * 365 * 86400
    os.utime(f, (future, future))
    (p / "a.py").write_text("x = 2\n", encoding="utf-8")
    return p, target


def test_cli_end_never_passes_a_shipped_head_to_git(shipped):
    p, target = shipped
    r = run_cli(["end", "--prompt", "change x"], cwd=p)
    assert not target.exists()
    assert r.returncode == 2 and b"nothing recorded" in r.stderr, r.stderr     # a store this user did not adopt
    assert run_cli(["init", "--no-scan", "--adopt-store"], cwd=p).returncode == 0
    r = run_cli(["end", "--prompt", "change x"], cwd=p)                       # adopted: the tracked begin is not used
    assert not target.exists()
    assert r.returncode == 2 and b"no usable begin" in r.stderr, r.stderr


def test_cli_end_after_the_users_own_begin_takes_that_begin(shipped):
    p, target = shipped
    assert run_cli(["init", "--no-scan", "--adopt-store"], cwd=p).returncode == 0
    assert run_cli(["begin"], cwd=p).returncode == 0
    (p / "a.py").write_text("x = 3\n", encoding="utf-8")
    r = run_cli(["end", "--prompt", "change x"], cwd=p)
    assert r.returncode == 0, r.stderr
    assert not target.exists()


def test_mcp_end_session_without_an_id_refuses_the_shipped_begin(shipped):
    p, target = shipped
    srv = Server(p, p / ".devmemory")
    out = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "end_session", "arguments": {"prompt": "change x"}}})
    assert out is not None and out["result"]["isError"] is True
    assert not target.exists()


@pytest.mark.parametrize("bad", [
    {"head": "--output=x"}, {"head": "HEAD"}, {"head": "a" * 39}, {"head": 1},
    {"dirty": {"../x.py": None}}, {"dirty": {"a.py": {"sha": "zz", "text": "x"}}}, {"dirty": []},
    {"started_at": "--x"}, {"id": "../../x"}, {"kind": "other"},
])
def test_validate_begin_refuses_bad_fields(bad):
    good = {"id": "20261114T100000_abcdef01", "started_at": "2026-11-14T10:00:00Z", "kind": "git", "v": 2,
            "head": "a" * 40, "dirty": {"a.py": {"sha": "b" * 64, "text": "x"}}}
    assert G.validate_begin(good) is None
    assert G.validate_begin({**good, **bad})


def test_end_state_ignores_a_head_that_is_not_a_commit_id(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(G.S, "_git", lambda project, *a, **k: calls.append(a) or "")
    monkeypatch.setattr(G.S, "git_status_sized", lambda *a, **k: ({}, False))
    monkeypatch.setattr(G.S, "git_head", lambda *a, **k: "c" * 40)
    G.end_state(tmp_path, {"kind": "git", "head": "--output=/tmp/x", "dirty": {}})
    assert not any(any(str(x).startswith("--output") for x in c) for c in calls)
