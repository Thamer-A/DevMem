"""A `git status` with more changed paths than devmem keeps (sessions.MAX_STATUS_ENTRIES) or more output than it reads
(MAX_STATUS_BYTES) is not dropped silently: the session's git data says so, and `devmem status` names it."""
from __future__ import annotations

import json
import shutil

import pytest
from hooks_helpers import make_project

from devmem import cli, consent, hooks, sessions, store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
SID = "5ca1ab1e-0000-4000-8000-000000000001"


def _hook(project, event: str, **payload) -> None:
    store._FILTERS.clear()
    consent.forget_decisions()
    code, _out = hooks.run([event], json.dumps({"session_id": SID, "cwd": str(project), "hook_event_name": event,
                                                **payload}).encode("utf-8"))
    assert code == 0


@pytest.mark.parametrize("limit", ["entries", "bytes"])
def test_a_status_too_large_is_recorded_and_named(tmp_path, monkeypatch, capsys, limit):
    project = make_project(tmp_path)
    for i in range(6):
        (project / f"new{i}.txt").write_text("x\n", encoding="utf-8")
    if limit == "entries":
        monkeypatch.setattr(sessions, "MAX_STATUS_ENTRIES", 2)
    else:
        monkeypatch.setattr(sessions, "MAX_STATUS_BYTES", 16)
    assert sessions.git_status_sized(project) == (None, True)
    assert sessions.git_snapshot(project)["status_too_large"] is True
    _hook(project, "SessionStart", source="startup")
    _hook(project, "UserPromptSubmit", prompt="explain greet")
    _hook(project, "Stop", last_assistant_message="done")    # (a SessionEnd mid-turn leaves the record to later)
    _hook(project, "SessionEnd", reason="other")
    recs = [json.loads(p.read_text(encoding="utf-8")) for p in (project / ".devmemory" / "sessions").glob("*.meta.json")]
    assert recs and recs[0]["git"].get("status_too_large") is True, recs[0].get("git") if recs else recs
    monkeypatch.chdir(project)
    assert cli.main(["status"]) == 0
    assert "recorded without git's changed files" in capsys.readouterr().out


def test_an_ordinary_status_is_not_marked(tmp_path):
    project = make_project(tmp_path)
    (project / "new.txt").write_text("x\n", encoding="utf-8")
    st, too_large = sessions.git_status_sized(project)
    assert st and not too_large
    assert "status_too_large" not in sessions.git_snapshot(project)
