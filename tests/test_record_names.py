"""LOW-4 / L7: every session gets its own record file.

Records were named `<start second>_<first 8 characters of the session id>`: two sessions that started in the same
second with ids sharing 8 leading characters, or (on a case-insensitive file system) ids differing only in case,
were written into one record (reviewer's r8_base_collision.py). The name now ends with a hash of the whole id, and
records with the older name are still found by `sessions`, `show` and `forget`.
"""
from __future__ import annotations

import json

import pytest
from cli_helpers import run_cli
from hooks_helpers import make_project

from devmem import hooks
from devmem import sessions as S
from devmem.sessions import load_sessions


def _send(project, sid, ev, **kw):
    p = {"session_id": sid, "cwd": str(project), "hook_event_name": ev, "transcript_path": "x"}
    p.update(kw)
    assert hooks.run([ev], json.dumps(p).encode())[0] == 0


@pytest.mark.parametrize("pair", [
    ("abcdef01-1111", "abcdef01-2222"),
    # ids that differ only in case still share their live/ event log on a case-insensitive file system (Windows,
    # macOS); Claude Code's ids are lowercase UUIDs. Their record names differ (test_names_are_unique_and_plain).
    pytest.param(("CaseTest-aaaa", "casetest-aaaa"), marks=pytest.mark.skipif(
        __import__("sys").platform in ("win32", "darwin"), reason="case-insensitive file system: live/ is shared"))])
def test_sessions_started_in_the_same_second_keep_separate_records(tmp_path, monkeypatch, pair):
    project = make_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    fixed = "2026-11-14T10:00:00Z"
    monkeypatch.setattr(S, "now_iso", lambda ms=False: fixed)
    for sid in pair:
        _send(project, sid, "SessionStart", source="startup")
    for sid in pair:
        _send(project, sid, "UserPromptSubmit", prompt=f"prompt of {sid}", prompt_id="p")
        _send(project, sid, "Stop", last_assistant_message=f"answer for {sid}", prompt_id="p")
        _send(project, sid, "SessionEnd", reason="exit")
    metas = sorted((project / ".devmemory" / "sessions").glob("*.meta.json"))
    assert len(metas) == 2, [m.name for m in metas]
    got = {r["session_id"]: [t["prompt"] for t in r["turns"]] for r in load_sessions(project / ".devmemory")}
    assert got == {sid: [f"prompt of {sid}"] for sid in pair}


def test_names_are_unique_and_plain():
    a, b = S.session_base("s", "2026-11-14T10:00:00Z", "CaseTest-aaaa"), S.session_base("s", "2026-11-14T10:00:00Z",
                                                                                      "casetest-aaaa")
    assert a.name.lower() != b.name.lower() and S.safe_id(a.name) and S.safe_id(b.name)
    assert S.is_session_name(a.name, "CaseTest-aaaa") and not S.is_session_name(a.name, "casetest-aaaa")
    assert S.is_session_name("20261101T100000_dddd4444", "dddd4444-0000")         # the older form


def test_an_older_record_name_is_still_found(tmp_path):
    project = make_project(tmp_path)
    sessions = project / ".devmemory" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    sid = "dddd4444-0000-0000-0000-000000000004"
    rec = {"schema": 2, "session_id": sid, "started_at": "2026-11-01T10:00:00Z", "provider": "claude-code",
           "turns": [{"prompt": "an old session", "tool_calls": []}], "raw_record": "20261101T100000_dddd4444.jsonl"}
    (sessions / "20261101T100000_dddd4444.meta.json").write_text(json.dumps(rec), encoding="utf-8")
    (sessions / "20261101T100000_dddd4444.jsonl").write_text("", encoding="utf-8")
    out = run_cli(["show", "dddd4444"], cwd=project).stdout.decode("utf-8", "replace")
    assert "an old session" in out
    r = run_cli(["forget", "dddd4444", "--yes"], cwd=project)
    assert r.returncode == 0, r.stderr
    assert not list(sessions.glob("20261101T100000_dddd4444*"))
