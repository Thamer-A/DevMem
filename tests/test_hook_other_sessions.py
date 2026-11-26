"""Two or more Claude Code sessions in one project (devmem.hooks._other_sessions). The reviewer's c10 concurrent: a file
written by session B during session A's turn was attributed to both sessions at their Stops. Now a path the other
session's own file tools recorded is not attributed to this one, any other path dated while another session was active
is counted (`changed_while_other_session_active`), and outside that time the usual rule applies."""
from __future__ import annotations

import json
import os
import shutil
import time

import pytest
from hooks_helpers import make_project

from devmem import consent, hooks, store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
A = "a1a1a1a1-0000-4000-8000-000000000001"
B = "b2b2b2b2-0000-4000-8000-000000000002"
C = "c3c3c3c3-0000-4000-8000-000000000003"
SLACK = 0.3


@pytest.fixture(autouse=True)
def _short_slack(monkeypatch):
    monkeypatch.setattr(hooks, "OBSERVED_FUTURE_SLACK_S", SLACK)


def _hook(project, sid, event, **payload) -> None:
    store._FILTERS.clear()
    consent.forget_decisions()
    code, _ = hooks.run([event], json.dumps({"session_id": sid, "cwd": str(project), "hook_event_name": event,
                                              "transcript_path": str(project / "none.jsonl"),
                                              **payload}).encode("utf-8"))
    assert code == 0


def _write(project, rel, text="x\n") -> None:
    time.sleep(0.05)
    (project / rel).write_text(text, encoding="utf-8")
    time.sleep(0.05)


def _bash(project, sid, rel) -> None:
    """`rel` written by a Bash call of session `sid` (no file named in the event, as for any shell command)."""
    _write(project, rel)
    _hook(project, sid, "PostToolUse", tool_name="Bash", tool_input={"command": "python gen.py"},
          tool_response={"stdout": "", "stderr": ""}, tool_use_id=f"toolu_{sid[:2]}{rel}")


def _write_tool(project, sid, rel) -> None:
    _write(project, rel)
    _hook(project, sid, "PostToolUse", tool_name="Write", tool_input={"file_path": str(project / rel), "content": "x"},
          tool_response={"type": "create", "filePath": str(project / rel)}, tool_use_id=f"toolu_{sid[:2]}w{rel}")


def _turn(project, sid, n=0) -> dict:
    f = next(f for f in (project / ".devmemory" / "sessions").glob("*.meta.json") if sid[:8] in f.name)
    return json.loads(f.read_text(encoding="utf-8"))["turns"][n]


def _changed(project, sid, n=0) -> list[str]:
    return sorted((_turn(project, sid, n).get("files_changed_observed") or {}).get("files") or [])


def _un(project, sid, n=0) -> dict:
    return {k: v for k, v in (_turn(project, sid, n).get("git_unattributed") or {}).items() if k != "source"}


def _no_errors(project):
    log = project / ".devmemory" / "hook_errors.log"
    assert not log.exists(), log.read_text(encoding="utf-8")[-2000:]


def test_two_interleaved_sessions_attribute_nothing_of_each_other(tmp_path):
    project = make_project(tmp_path)
    for s in (A, B):
        _hook(project, s, "SessionStart", source="startup")
    _hook(project, A, "UserPromptSubmit", prompt="task A")
    _hook(project, B, "UserPromptSubmit", prompt="task B")
    _bash(project, A, "a.py")
    _write_tool(project, B, "b.py")
    _bash(project, B, "c.py")
    _hook(project, A, "Stop", last_assistant_message="A done")
    _hook(project, B, "Stop", last_assistant_message="B done")
    _no_errors(project)
    assert _changed(project, A) == [] and _un(project, A) == {
        "changed_while_other_session_active": 2, "changed_by_other_session_tool": 1}, _turn(project, A)
    # B's Write is B's (its own tool call); a.py and c.py were written while both turns were open
    assert _turn(project, B)["files_edited"] == ["b.py"]
    assert _changed(project, B) == [] and _un(project, B) == {"changed_while_other_session_active": 2}, _turn(project, B)
    # later, A's next turn runs alone: the usual rule
    time.sleep(SLACK + 0.2)
    _hook(project, A, "UserPromptSubmit", prompt="task A2")
    _bash(project, A, "d.py")
    _hook(project, A, "Stop", last_assistant_message="A2 done")
    assert _changed(project, A, 1) == ["d.py"] and not _un(project, A, 1), _turn(project, A, 1)


def test_a_session_whose_turn_ended_before_this_one_began_does_not_count(tmp_path):
    project = make_project(tmp_path)
    for s in (A, B):
        _hook(project, s, "SessionStart", source="startup")
    _hook(project, B, "UserPromptSubmit", prompt="task B")
    _bash(project, B, "b.py")
    _hook(project, B, "Stop", last_assistant_message="B done")
    time.sleep(SLACK + 0.2)
    _hook(project, A, "UserPromptSubmit", prompt="task A")
    _bash(project, A, "a.py")
    _hook(project, A, "Stop", last_assistant_message="A done")
    # b.py: written after A's previous check, before A's prompt: outside A's turns
    assert _changed(project, A) == ["a.py"] and _un(project, A) == {"changed_between_turns": 1}, _turn(project, A)
    assert _changed(project, B) == ["b.py"] and not _un(project, B), _turn(project, B)


def test_a_session_that_ended_during_this_turn_still_counts(tmp_path):
    """B wrote during A's turn and ended (SessionEnd moves its log) before A's Stop: its turn times stay in live/."""
    project = make_project(tmp_path)
    for s in (A, B):
        _hook(project, s, "SessionStart", source="startup")
    _hook(project, A, "UserPromptSubmit", prompt="task A")
    _hook(project, B, "UserPromptSubmit", prompt="task B")
    _bash(project, B, "b.py")
    _hook(project, B, "Stop", last_assistant_message="B done")
    _hook(project, B, "SessionEnd", reason="other")
    _hook(project, A, "Stop", last_assistant_message="A done")
    assert _changed(project, A) == [] and _un(project, A) == {"changed_while_other_session_active": 1}


def test_work_another_session_left_running_after_its_turn_counts_while_it_records(tmp_path):
    """B's turn ended, but tool calls of work it left running (a background agent) are still being recorded: B counts
    as active until its last recorded event."""
    project = make_project(tmp_path)
    for s in (A, B):
        _hook(project, s, "SessionStart", source="startup")
    _hook(project, B, "UserPromptSubmit", prompt="task B")
    _hook(project, B, "Stop", last_assistant_message="B done, an agent still runs")
    time.sleep(SLACK + 0.2)
    _hook(project, A, "UserPromptSubmit", prompt="task A")
    _write(project, "bg.py")                                   # written by B's background agent, then its event
    time.sleep(SLACK + 0.2)
    _hook(project, B, "PostToolUse", tool_name="Bash", tool_input={"command": "python gen.py"},
          tool_response={"stdout": ""}, tool_use_id="toolu_bg", agent_id="agent_bg", agent_type="general-purpose")
    _hook(project, A, "Stop", last_assistant_message="A done")
    assert _changed(project, A) == [] and _un(project, A) == {"changed_while_other_session_active": 1}, _turn(project, A)


def test_three_sessions(tmp_path):
    project = make_project(tmp_path)
    for s in (A, B, C):
        _hook(project, s, "SessionStart", source="startup")
    _hook(project, C, "UserPromptSubmit", prompt="task C")      # C: done long before A starts
    _hook(project, C, "Stop", last_assistant_message="C done")
    time.sleep(SLACK + 0.2)
    _hook(project, A, "UserPromptSubmit", prompt="task A")
    _bash(project, A, "a1.py")                                  # only A active
    _hook(project, B, "UserPromptSubmit", prompt="task B")
    _write_tool(project, B, "b.py")
    _bash(project, A, "a2.py")                                  # A and B active
    _hook(project, A, "Stop", last_assistant_message="A done")
    assert _changed(project, A) == ["a1.py"], _turn(project, A)
    assert _un(project, A) == {"changed_while_other_session_active": 1, "changed_by_other_session_tool": 1}


def _fake_open(project, sid, start, log_age=None):
    """Another session's live files as a session with a turn open since `start` leaves them (one that crashed)."""
    live = project / ".devmemory" / "live"
    live.mkdir(exist_ok=True)
    (live / f"{sid}.open").write_text(json.dumps({"at": start}), encoding="utf-8")
    log = live / f"{sid}.jsonl"
    log.write_text("\n", encoding="utf-8")
    if log_age is not None:
        t = time.time() - log_age
        for f in (log, live / f"{sid}.open"):
            os.utime(f, (t, t))


def test_a_stale_open_turn_stops_counting_at_its_last_event(tmp_path):
    """A session that crashed mid-turn leaves live/<sid>.open behind: it counts as active until its last recorded event
    (its log's time) once that is more than OTHER_TURN_STALE_S old, not forever. (Passes on 57d5610 too, which read no
    other session at all.)"""
    project = make_project(tmp_path)
    _hook(project, A, "SessionStart", source="startup")
    _fake_open(project, B, time.time() - 3 * 3600, log_age=2 * 3600)
    _hook(project, A, "UserPromptSubmit", prompt="task A")
    _bash(project, A, "a.py")
    _hook(project, A, "Stop", last_assistant_message="A done")
    assert _changed(project, A) == ["a.py"] and not _un(project, A), _turn(project, A)


def test_an_open_turn_with_recent_events_counts_as_active(tmp_path):
    project = make_project(tmp_path)
    _hook(project, A, "SessionStart", source="startup")
    _fake_open(project, B, time.time() - 3 * 3600, log_age=60)
    _hook(project, A, "UserPromptSubmit", prompt="task A")
    _bash(project, A, "a.py")
    _hook(project, A, "Stop", last_assistant_message="A done")
    assert _changed(project, A) == [] and _un(project, A) == {"changed_while_other_session_active": 1}


@pytest.mark.parametrize("cap", ["sessions", "listing"])
def test_more_live_sessions_than_one_check_reads_attributes_nothing(tmp_path, monkeypatch, cap):
    if cap == "sessions":
        monkeypatch.setattr(hooks, "OTHER_SESSIONS_READ", 3)
    else:
        monkeypatch.setattr(hooks, "OTHER_LIVE_ENTRIES", 6)
    project = make_project(tmp_path)
    _hook(project, A, "SessionStart", source="startup")
    for i in range(4):
        _fake_open(project, f"f{i}f{i}f{i}f{i}-0000-4000-8000-00000000000{i}", time.time() - 100)
    read = []
    real = hooks._json_file
    monkeypatch.setattr(hooks, "_json_file", lambda p: read.append(p) or real(p))
    _hook(project, A, "UserPromptSubmit", prompt="task A")
    _bash(project, A, "a.py")
    _hook(project, A, "Stop", last_assistant_message="A done")
    assert _changed(project, A) == [] and _un(project, A) == {"changed_other_sessions_not_read": 1}, _turn(project, A)
    assert not [p for p in read if p.name.startswith("f")]       # none of them read past the cap


def test_fifty_live_sessions_cost_a_bounded_check(tmp_path, monkeypatch):
    """50 other sessions with recent live files: more than OTHER_SESSIONS_READ, so none is read and nothing is
    attributed; the check lists live/ once."""
    project = make_project(tmp_path)
    _hook(project, A, "SessionStart", source="startup")
    for i in range(50):
        _fake_open(project, f"e{i:07d}-0000-4000-8000-000000000000", time.time() - 100)
    _hook(project, A, "UserPromptSubmit", prompt="task A")
    _bash(project, A, "a.py")
    t = time.perf_counter()
    _hook(project, A, "Stop", last_assistant_message="A done")
    assert time.perf_counter() - t < 30
    assert _un(project, A) == {"changed_other_sessions_not_read": 1}


@pytest.mark.parametrize("junk", ["", "{", "[]", '{"at": "x"}', '{"at": 1e400}', '{"spans": [[1, 2, 3]]}',
                                  '{"spans": "x"}', "\x00" * 100, "A" * 5000])
def test_damaged_live_files_of_another_session_never_raise(tmp_path, junk):
    project = make_project(tmp_path)
    _hook(project, A, "SessionStart", source="startup")
    live = project / ".devmemory" / "live"
    for suffix in (".open", ".turn", ".prompt", ".edits"):
        (live / f"{B}{suffix}").write_text(junk, encoding="utf-8")
    _hook(project, A, "UserPromptSubmit", prompt="task A")
    _bash(project, A, "a.py")
    _hook(project, A, "Stop", last_assistant_message="A done")
    _no_errors(project)
    # an unreadable start is no start: what the other session's files say is taken only when it is a sane time
    assert _changed(project, A) == ["a.py"] or _un(project, A) == {"changed_while_other_session_active": 1}


def test_what_other_sessions_read_of_a_session_stays_small(tmp_path, monkeypatch):
    """live/<sid>.edits is cut back to the last EDITS_KEPT records at a Stop once it holds more than twice as many;
    live/<sid>.turn keeps the last TURN_SPANS_KEPT turns."""
    monkeypatch.setattr(hooks, "EDITS_KEPT", 4)
    project = make_project(tmp_path)
    _hook(project, A, "SessionStart", source="startup")
    live = project / ".devmemory" / "live"
    for k in range(hooks.TURN_SPANS_KEPT + 3):
        _hook(project, A, "UserPromptSubmit", prompt=f"task {k}")
        for i in range(3):
            _write_tool(project, A, f"w{k}_{i}.py")
        _hook(project, A, "Stop", last_assistant_message="done")
        assert (live / f"{A}.edits").stat().st_size <= 2 * 4 * hooks.EDIT_RECORD
    assert len(json.loads((live / f"{A}.turn").read_text(encoding="utf-8"))["spans"]) == hooks.TURN_SPANS_KEPT
    assert not (live / f"{A}.open").exists()


def test_stale_open_turns_of_crashed_sessions_do_not_fill_the_cap(tmp_path, monkeypatch):
    """The reviewer's l30 (N=500): more than OTHER_SESSIONS_READ sessions that crashed mid-turn left live/<id>.open
    behind, and every change in the project was counted `changed_other_sessions_not_read` until they were finalised.
    The sessions read are chosen by what can overlap this turn before the cap: a stale open turn ended at its last
    event, before this turn began."""
    monkeypatch.setattr(hooks, "OTHER_SESSIONS_READ", 3)
    project = make_project(tmp_path)
    _hook(project, A, "SessionStart", source="startup")
    for i in range(5):
        _fake_open(project, f"d{i}d{i}d{i}d{i}-0000-4000-8000-00000000000{i}", time.time() - 3 * 3600, log_age=2 * 3600)
    _hook(project, A, "UserPromptSubmit", prompt="task A")
    _bash(project, A, "a.py")
    _hook(project, A, "Stop", last_assistant_message="A done")
    assert _changed(project, A) == ["a.py"] and not _un(project, A), _turn(project, A)
