"""Work on the hook path whose size the store or the project controls is bounded per event: abandoned sessions
finished per SessionStart, the delivery history kept in a session's state (read and written on every prompt), and the
project files re-hashed for a picked session's freshness."""
from __future__ import annotations

import json
import os
import time

import pytest
from hooks_helpers import make_project

from devmem import hooks
from devmem import pack as P


@pytest.fixture
def project(tmp_path, monkeypatch):
    p = make_project(tmp_path, git=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(p))
    return p


def _hook(event: str, project, sid: str, **payload) -> str:
    code, out = hooks.run([event], json.dumps({"session_id": sid, "cwd": str(project), "hook_event_name": event,
                                               **payload}).encode("utf-8"))
    assert code == 0
    return out


def test_abandoned_sessions_are_finished_a_few_per_session_start(project):
    """Each costs a read and a rewrite of its whole log: all of them in one SessionStart made it as slow as the
    store is large."""
    store = project / ".devmemory"
    n = hooks.ABANDONED_PER_START * 2 + 3
    old = time.time() - hooks.ABANDONED_AFTER_S - 3600
    for i in range(n):
        sid = f"abandoned-{i:03d}"
        _hook("SessionStart", project, sid, source="startup")
        _hook("UserPromptSubmit", project, sid, prompt=f"request number {i}")
        _hook("Stop", project, sid, last_assistant_message="done")
    for i in range(n):          # aged only now: each SessionStart above would have finished the earlier ones
        os.utime(store / "live" / f"abandoned-{i:03d}.jsonl", (old, old))

    def left() -> int:
        return len(list((store / "live").glob("abandoned-*.jsonl")))
    _hook("SessionStart", project, "new-1", source="startup")
    assert left() == n - hooks.ABANDONED_PER_START
    _hook("SessionStart", project, "new-2", source="startup")
    _hook("SessionStart", project, "new-3", source="startup")
    assert left() == 0


def test_the_delivery_history_in_a_sessions_state_is_capped(project, monkeypatch):
    monkeypatch.setattr(hooks, "DELIVERED_KEPT", 5)
    cfg = project / ".devmemory" / "config.json"
    cfg.write_text(json.dumps(dict(json.loads(cfg.read_text(encoding="utf-8")), deliver=True)), encoding="utf-8")
    sid = "many-prompts"
    _hook("SessionStart", project, sid, source="startup")
    for i in range(12):
        _hook("UserPromptSubmit", project, sid, prompt=f"greet the user number {i}")
    st = hooks.read_state(project / ".devmemory", sid)
    assert len(st["delivered"]) == 5 and st["delivered"][-1]["turn"] == 11


def test_freshness_rehashes_read_files_within_its_read_budget(tmp_path, monkeypatch):
    project = tmp_path / "p"
    project.mkdir()
    for i in range(20):
        (project / f"data{i}.txt").write_text("x" * 3000, encoding="utf-8")
    hashed: list = []
    real = P.hash_files
    monkeypatch.setattr(P, "hash_files", lambda pr, rels: hashed.extend(rels) or real(pr, rels))
    monkeypatch.setattr(P, "FRESH_READ_BYTES", 10_000)
    rec = {"tool_calls": [], "files_edited": [],
           "file_hashes_at_capture": {f"data{i}.txt": "0" * 16 for i in range(20)}}
    out = P.freshness(rec, project)
    assert len(hashed) == 3                        # 3 x 3001 bytes fit in 10,000 (before: all 20, at any size)
    assert out["status"] == "context-changed" and len(out["read_files_changed"]) == 3
