"""Which turn a change git shows belongs to, when turns do not end with one Stop each (devmem.hooks: _close_open_turn,
_absorb_prompt, on_stop). The reviewer's c10 cases: an interrupted turn (Esc: Claude Code sends no Stop, the docs say
"Does not run if the stoppage occurred due to a user interrupt") had its changes counted as made "while no turn was
running"; a Stop that a Stop hook continued (`stop_hook_active`) counted the continued turn's changes as having an
unknown start; a UserPromptSubmit that did not get the session lock left the turn without a start."""
from __future__ import annotations

import json
import os
import shutil
import time

import pytest
from hooks_helpers import make_project

from devmem import consent, hooks, sessions, store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
SID = "7e7e7e7e-0000-4000-8000-000000000001"
SLACK = 0.3          # OBSERVED_FUTURE_SLACK_S in these tests, so that a gap after a turn's last event is short to wait


@pytest.fixture(autouse=True)
def _short_slack(monkeypatch):
    monkeypatch.setattr(hooks, "OBSERVED_FUTURE_SLACK_S", SLACK)


def _hook(project, event, sid=SID, **payload) -> None:
    store._FILTERS.clear()
    consent.forget_decisions()
    code, _ = hooks.run([payload.pop("argv_event", event)],
                        json.dumps({"session_id": sid, "cwd": str(project), "hook_event_name": event,
                                    "transcript_path": str(project / "none.jsonl"), **payload}).encode("utf-8"))
    assert code == 0


def _bash(project, sid=SID, n=[0]) -> None:
    """A Bash tool event (the file it wrote is written by the test just before)."""
    n[0] += 1
    _hook(project, "PostToolUse", sid=sid, tool_name="Bash", tool_input={"command": "python gen.py"},
          tool_response={"stdout": "", "stderr": "", "interrupted": False}, tool_use_id=f"toolu_b{n[0]}")


def _write(project, rel, text="x\n") -> None:
    time.sleep(0.05)            # the file system's clock ticks coarser than time.time() (Windows: ~16 ms)
    f = project / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    time.sleep(0.05)


def _rec(project, sid=SID) -> dict:
    tag = sid[:8]
    f = next(f for f in (project / ".devmemory" / "sessions").glob("*.meta.json") if tag in f.name)
    return json.loads(f.read_text(encoding="utf-8"))


def _turn(project, n, sid=SID) -> dict:
    return _rec(project, sid)["turns"][n]


def _changed(project, n, sid=SID) -> list[str]:
    return sorted((_turn(project, n, sid).get("files_changed_observed") or {}).get("files") or [])


def _un(project, n, sid=SID) -> dict:
    return {k: v for k, v in (_turn(project, n, sid).get("git_unattributed") or {}).items() if k != "source"}


def _start(project, sid=SID):
    _hook(project, "SessionStart", sid=sid, source="startup")


def _no_errors(project):
    log = project / ".devmemory" / "hook_errors.log"
    assert not log.exists(), log.read_text(encoding="utf-8")[-2000:]


# ---------------------------------------------------------------- interrupted turns (no Stop)

def test_an_interrupted_turn_keeps_what_its_tool_calls_wrote(tmp_path):
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _bash(project)
    # Esc: no Stop
    _hook(project, "UserPromptSubmit", prompt="second after Esc")
    _write(project, "t2.py")
    _hook(project, "Stop", last_assistant_message="done")
    _no_errors(project)
    assert _changed(project, 0) == ["t1.py"] and not _un(project, 0), _turn(project, 0)
    assert _turn(project, 0)["turn_end"] == sessions.TURN_END_WHY["next_prompt"]
    assert _changed(project, 1) == ["t2.py"] and not _un(project, 1), _turn(project, 1)


def test_an_interrupted_turn_without_tool_events_keeps_what_came_within_the_slack(tmp_path):
    """Esc while the first tool call still ran: no PostToolUse came. Its file, written right after the prompt, is
    dated within the slack after the turn's last event (the prompt)."""
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _hook(project, "UserPromptSubmit", prompt="second after Esc")
    _hook(project, "Stop", last_assistant_message="done")
    assert _changed(project, 0) == ["t1.py"], _turn(project, 0)


def test_after_the_last_event_of_an_interrupted_turn_nothing_is_attributed(tmp_path):
    """A file dated after the interrupted turn's last recorded event (and the slack): the tool call still running at
    Esc, or an edit made after it. Counted under a reason that says exactly that."""
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    time.sleep(SLACK + 0.3)
    _write(project, "t1.py")
    _hook(project, "UserPromptSubmit", prompt="second after Esc")
    _hook(project, "Stop", last_assistant_message="done")
    assert _changed(project, 0) == [] and _un(project, 0) == {"changed_after_last_activity": 1}, _turn(project, 0)
    assert _changed(project, 1) == [] and not _un(project, 1)


def test_your_edit_in_the_idle_gap_after_an_interrupt_is_not_attributed(tmp_path):
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _bash(project)
    time.sleep(SLACK + 0.3)                    # Esc; the user edits a file by hand, then sends the next prompt
    _write(project, "notes_by_hand.md")
    _hook(project, "UserPromptSubmit", prompt="second")
    _write(project, "t2.py")
    _hook(project, "Stop", last_assistant_message="done")
    assert _changed(project, 0) == ["t1.py"] and _un(project, 0) == {"changed_after_last_activity": 1}
    assert _changed(project, 1) == ["t2.py"] and not _un(project, 1)


def test_three_prompts_without_a_stop_between(tmp_path):
    project = make_project(tmp_path)
    _start(project)
    for i in range(3):
        _hook(project, "UserPromptSubmit", prompt=f"prompt {i}")
        _write(project, f"t{i}.py")
        _bash(project)
    _hook(project, "Stop", last_assistant_message="done")
    _no_errors(project)
    assert [_changed(project, i) for i in range(3)] == [["t0.py"], ["t1.py"], ["t2.py"]], _rec(project)["turns"]
    assert [_turn(project, i).get("turn_end") for i in range(3)] == [sessions.TURN_END_WHY["next_prompt"]] * 2 + [None]


def test_a_turn_ended_by_an_api_error_is_checked_at_its_stop_failure(tmp_path):
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _hook(project, "StopFailure", error="rate_limit", error_details="429")
    _hook(project, "UserPromptSubmit", prompt="again")
    _write(project, "t2.py")
    _hook(project, "Stop", last_assistant_message="done")
    assert _changed(project, 0) == ["t1.py"] and _changed(project, 1) == ["t2.py"], _rec(project)["turns"]
    assert "turn_end" not in _turn(project, 0)


OTHER = "7e7e7e7e-0000-4000-8000-000000000002"


def test_a_session_that_ends_mid_turn_is_finished_by_the_next_session_start(tmp_path):
    """SessionEnd with a turn open records only that the session ended (one append: Claude Code gives SessionEnd hooks
    1.5 s); the next SessionStart in the project checks that turn, ending it at its own last event, and finishes the
    record. A hand edit after the session ended is counted, not attributed."""
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _bash(project)
    live = project / ".devmemory" / "live"
    log_size = (live / f"{SID}.jsonl").stat().st_size
    _hook(project, "SessionEnd", reason="other")
    assert (live / f"{SID}.jsonl").exists() and (live / f"{SID}.open").exists()      # nothing else done yet
    assert '"TurnEnd"' not in (live / f"{SID}.jsonl").read_text(encoding="utf-8")[log_size:]
    time.sleep(SLACK + 0.3)
    _write(project, "after_exit.md")
    _hook(project, "SessionStart", sid=OTHER, source="startup")
    _no_errors(project)
    assert not (live / f"{SID}.jsonl").exists()                                     # finished: moved to sessions/
    t = _turn(project, 0)
    assert _changed(project, 0) == ["t1.py"] and t["turn_end"] == sessions.TURN_END_WHY["session_end"], t
    assert _un(project, 0) == {"changed_after_last_activity": 1}
    rec = _rec(project)
    assert rec["ended_reason"] == "other" and rec["ended_at"], rec


@pytest.mark.parametrize("late", ["main", "agent"])
def test_a_session_that_ended_mid_turn_and_got_a_late_event_is_still_finished(tmp_path, late):
    """The reviewer's c31: after a SessionEnd with a turn open, one more event reached the log (a late tool call of the
    main agent, or of a background agent). The next SessionStart took the log's last event for "not ended" and left it
    to the abandoned path 12 h later, which never checked the turn (t1.py neither attributed nor counted). It is now
    finished like any other ended session: the turn is checked."""
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _bash(project)
    _hook(project, "SessionEnd", reason="other")
    extra = {"agent_id": "agent-1", "agent_type": "general-purpose"} if late == "agent" else {}
    _hook(project, "PostToolUse", tool_name="Read", tool_input={"file_path": str(project / "greet.py")},
          tool_response={"type": "text"}, tool_use_id="late", **extra)
    _hook(project, "SessionStart", sid=OTHER, source="startup")
    _no_errors(project)
    assert not (project / ".devmemory" / "live" / f"{SID}.jsonl").exists()       # finished: moved to sessions/
    t = _turn(project, 0)
    assert _changed(project, 0) == ["t1.py"] and t["turn_end"] == sessions.TURN_END_WHY["session_end"], t
    assert not (project / ".devmemory" / "live" / f"{SID}.end").exists()
    assert _rec(project)["ended_reason"] == "other"


def test_a_session_start_opens_no_live_log_it_does_not_finish(tmp_path, monkeypatch):
    """The reviewer's t12/t13: every SessionStart opened every live log younger than 12 h to read its last event (about
    35 ms each for a just-written file on Windows: 19 s for 500 logs). Now one lstat per log tells an ended session
    (live/<sid>.end); only an ended session's small state file is read, and its log only when it can be finished (at
    most 2 * ABANDONED_PER_START of them per SessionStart)."""
    project = make_project(tmp_path)
    live = project / ".devmemory" / "live"
    live.mkdir()
    for i in range(60):        # other sessions, running (recent logs), and ended ones whose finishing was left over
        sid = f"aaaaaaaa-0000-0000-0000-{i:012d}"
        (live / f"{sid}.jsonl").write_text('{"event": "UserPromptSubmit", "prompt": "x"}\n', encoding="utf-8")
        (live / f"{sid}.state.json").write_text('{"session_id": "x"}', encoding="utf-8")    # no record name
        if i % 3 == 0:
            (live / f"{sid}.end").write_text("", encoding="utf-8")
    opened: set[str] = set()
    real_open = store._open_regular

    def counting_open(path, *a, **k):          # every read of a store file goes through here (logs, states)
        name = os.path.basename(str(path))
        if name.startswith("aaaaaaaa"):
            opened.add(name)
        return real_open(path, *a, **k)
    monkeypatch.setattr(store, "_open_regular", counting_open)
    _start(project)
    assert opened and all(n.endswith(".state.json") for n in opened), opened     # no log: none can be finished
    assert all(int(n[24:36]) % 3 == 0 for n in opened), opened                    # only sessions marked as ended


@pytest.mark.parametrize("k", [20, 200])
@pytest.mark.parametrize("damage", ["marked_no_state", "marked_state_without_base", "stale_damaged_state",
                                    "mark_cannot_be_removed"])
def test_marked_sessions_that_cannot_be_finished_do_not_starve_a_real_one(tmp_path, monkeypatch, damage, k):
    """The verifier's m3_ss starve / m3_starve2: logs that nothing can finish (no state file, a state without a record
    name, a damaged state 12 h old, a mark that cannot be removed) sorted first and took the tries of every SessionStart,
    so a real ended session behind them was never finished. Tries now go only to logs that can be finished: an
    unfinishable one costs one lstat or one small state read and is skipped. The real session is finished at the first
    SessionStart."""
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _bash(project)
    _hook(project, "SessionEnd", reason="other")
    live = project / ".devmemory" / "live"
    old = time.time() - hooks.ABANDONED_AFTER_S - 3600
    if damage == "mark_cannot_be_removed":
        monkeypatch.setattr(hooks, "_unmark_ended", lambda p, marked=True: None)
    for i in range(k):                        # names that sort before SID
        stuck = f"00000000-0000-0000-0000-{i:012d}"
        (live / f"{stuck}.jsonl").write_text('{"event": "SessionEnd"}\n', encoding="utf-8")
        if damage != "stale_damaged_state":
            (live / f"{stuck}.end").write_text("", encoding="utf-8")
        if damage != "marked_no_state":
            (live / f"{stuck}.state.json").write_text('{"session_id": "x", "base": "../x"}' if i % 2 else "{damaged",
                                                      encoding="utf-8")
        if damage == "stale_damaged_state":
            os.utime(live / f"{stuck}.jsonl", (old, old))
    _hook(project, "SessionStart", sid=OTHER, source="startup")
    assert not (live / f"{SID}.jsonl").exists(), "the real ended session was not finished"
    assert _changed(project, 0) == ["t1.py"]


def test_a_session_end_cut_off_between_its_append_and_its_mark_is_left_to_the_abandoned_path(tmp_path, monkeypatch):
    """docs/LIMITS.md: killed in the moment between the SessionEnd's append and its mark, the session is finished as abandoned
    12 h after its last event, with the reason SessionEnd gave, and the open turn's changes are neither attributed nor
    counted (the verifier's m3_upgrade)."""
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _bash(project)
    with monkeypatch.context() as m:
        m.setattr(hooks, "_mark_ended", lambda p: (_ for _ in ()).throw(KeyboardInterrupt()))
        _hook(project, "SessionEnd", reason="prompt_input_exit")
    (project / ".devmemory" / "hook_errors.log").unlink()
    live = project / ".devmemory" / "live"
    _hook(project, "SessionStart", sid=OTHER, source="startup")
    assert (live / f"{SID}.jsonl").exists()                                       # not finished by it
    old = time.time() - hooks.ABANDONED_AFTER_S - 3600
    for f in live.glob(f"{SID}.*"):
        os.utime(f, (old, old))
    _hook(project, "SessionStart", sid=OTHER, source="resume")
    rec = _rec(project)
    assert not (live / f"{SID}.jsonl").exists() and rec["ended_reason"] == "prompt_input_exit", rec
    assert _changed(project, 0) == [] and not _un(project, 0)


def test_a_session_end_cut_off_before_it_finished_is_finished_later_and_says_why_it_ended(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _hook(project, "Stop", last_assistant_message="done")
    with monkeypatch.context() as m:        # killed after the event was appended, before the record was written
        m.setattr(hooks, "_write_record", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
        _hook(project, "SessionEnd", reason="prompt_input_exit")
    (project / ".devmemory" / "hook_errors.log").unlink()
    _hook(project, "SessionStart", sid=OTHER, source="startup")
    _no_errors(project)
    assert not (project / ".devmemory" / "live" / f"{SID}.jsonl").exists()
    assert _rec(project)["ended_reason"] == "prompt_input_exit"


def _after_interrupt(project, kind):
    """One event that is not the interrupted turn's own work, in the position "after Esc, before the next prompt"."""
    if kind == "PostCompact":
        _hook(project, "PostCompact", trigger="manual", compact_summary="summary")
    elif kind == "SessionStart_compact":
        _hook(project, "SessionStart", source="compact")
    elif kind == "subagent_PostToolUse":
        _hook(project, "PostToolUse", tool_name="Bash", tool_input={"command": "make"}, tool_response={"stdout": ""},
              tool_use_id="toolu_bg1", agent_id="agent_bg", agent_type="general-purpose")
    elif kind == "subagent_PostToolUseFailure":
        _hook(project, "PostToolUseFailure", tool_name="Bash", tool_input={"command": "make"}, error="exit 1",
              tool_use_id="toolu_bg2", agent_id="agent_bg", agent_type="general-purpose")
    elif kind == "late_interrupt_failure":
        _hook(project, "PostToolUseFailure", tool_name="Bash", tool_input={"command": "sleep 100"},
              error="interrupted", is_interrupt=True, tool_use_id="toolu_late")
    elif kind == "interrupted_bash_result":
        _hook(project, "PostToolUse", tool_name="Bash", tool_input={"command": "sleep 100"},
              tool_response={"stdout": "", "stderr": "", "interrupted": True}, tool_use_id="toolu_int")
    elif kind == "SubagentStop":
        _hook(project, "SubagentStop", argv_event="Stop", stop_hook_active=False, agent_id="def456",
              agent_type="Explore", last_assistant_message="done")
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["PostCompact", "SessionStart_compact", "subagent_PostToolUse",
                                  "subagent_PostToolUseFailure", "late_interrupt_failure", "interrupted_bash_result",
                                  "SubagentStop"])
def test_an_event_after_an_interrupt_that_is_not_the_turns_work_does_not_move_its_end(tmp_path, kind):
    """The reviewer's c20 int_compact / int_ss_compact / int_bg_agent / int_late_fail: the turn's end was the event
    log's time, which these events moved past a hand edit made after Esc, so the edit was attributed to the turn."""
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _bash(project)
    time.sleep(SLACK + 0.3)                    # Esc
    _write(project, "edit_before.md")          # by hand
    _after_interrupt(project, kind)
    _write(project, "edit_after.md")           # by hand
    _hook(project, "UserPromptSubmit", prompt="second")
    _hook(project, "Stop", last_assistant_message="done")
    _no_errors(project)
    assert _changed(project, 0) == ["t1.py"], _turn(project, 0)
    assert _un(project, 0) == {"changed_after_last_activity": 2}, _turn(project, 0)
    assert _changed(project, 1) == [] and not _un(project, 1)


def test_a_resume_after_an_interrupt_ends_the_turn_at_its_own_last_event(tmp_path):
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _bash(project)
    time.sleep(SLACK + 0.3)
    _write(project, "edit_before.md")
    _hook(project, "SessionStart", source="resume")
    _write(project, "edit_after.md")           # no turn open now
    _hook(project, "UserPromptSubmit", prompt="second")
    _hook(project, "Stop", last_assistant_message="done")
    assert _changed(project, 0) == ["t1.py"] and _un(project, 0) == {"changed_after_last_activity": 1}
    assert _changed(project, 1) == [] and _un(project, 1) == {"changed_between_turns": 1}


def test_a_main_agent_tool_call_reported_after_esc_counts_as_the_turns_work(tmp_path):
    """Ctrl+Enter lets a running tool finish; its PostToolUse (no agent_id, not interrupted) is the turn's tool call
    ending, so the turn ends there (as docs/LIMITS.md says, an edit you make before it is attributed to the turn)."""
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    time.sleep(SLACK + 0.3)
    _write(project, "t1.py")
    _bash(project)
    _hook(project, "UserPromptSubmit", prompt="second")
    _hook(project, "Stop", last_assistant_message="done")
    assert _changed(project, 0) == ["t1.py"] and not _un(project, 0)


def test_a_resumed_session_closes_the_turn_its_earlier_run_left_open(tmp_path):
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _bash(project)
    # the terminal was closed mid-turn: no Stop, no SessionEnd; later `claude --resume`
    _hook(project, "SessionStart", source="resume")
    _hook(project, "UserPromptSubmit", prompt="second")
    _write(project, "t2.py")
    _hook(project, "Stop", last_assistant_message="done")
    assert _changed(project, 0) == ["t1.py"] and _turn(project, 0)["turn_end"] == sessions.TURN_END_WHY["resume"]
    assert _changed(project, 1) == ["t2.py"] and not _un(project, 1)


def test_a_compaction_in_the_middle_of_a_turn_keeps_the_turn_open(tmp_path):
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _hook(project, "PostCompact", trigger="auto", compact_summary="summary")
    _hook(project, "SessionStart", source="compact")
    _write(project, "t2.py")
    _hook(project, "Stop", last_assistant_message="done")
    assert _changed(project, 0) == ["t1.py", "t2.py"] and "turn_end" not in _turn(project, 0)


# ---------------------------------------------------------------- Stops without a prompt between

def test_a_turn_a_stop_hook_continued_is_the_same_turn(tmp_path):
    """Stop, then a Stop hook blocks and Claude goes on: the next Stop comes with `stop_hook_active` true and no prompt
    between (Claude Code's docs: "true when Claude Code is already continuing as a result of a stop hook")."""
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "a.py")
    _hook(project, "Stop", last_assistant_message="d1", stop_hook_active=False)
    _write(project, "b.py")
    _hook(project, "Stop", last_assistant_message="d2", stop_hook_active=True)
    _write(project, "c.py")
    _hook(project, "Stop", last_assistant_message="d3", stop_hook_active=True)
    _no_errors(project)
    assert len(_rec(project)["turns"]) == 1
    assert _changed(project, 0) == ["a.py", "b.py", "c.py"] and not _un(project, 0), _turn(project, 0)


def test_a_second_stop_without_a_prompt_and_without_the_flag_has_an_unknown_start(tmp_path):
    """Every documented way a new turn starts fires UserPromptSubmit; a Stop after a Stop without either is a turn whose
    prompt this hook did not see (its UserPromptSubmit hook was cancelled at its timeout, say), so idle time may lie
    between: nothing is attributed. (Passes on 57d5610 too: the rule there was the same for this case.)"""
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "a.py")
    _hook(project, "Stop", last_assistant_message="d1")
    _write(project, "b.py")
    _hook(project, "Stop", last_assistant_message="d2")
    assert _changed(project, 0) == ["a.py"] and _un(project, 0) == {"changed_turn_start_unknown": 1}


def test_a_subagent_stop_does_not_end_the_main_turn(tmp_path):
    """SubagentStop carries agent_id, agent_type, agent_transcript_path and stop_hook_active (the docs' example). devmem
    does not install a SubagentStop hook; one delivered to the Stop handler anyway (or a Stop that names an agent) must
    not close the main turn."""
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "a.py")
    _hook(project, "SubagentStop", argv_event="Stop", stop_hook_active=False, agent_id="def456", agent_type="Explore",
          agent_transcript_path=str(project / "sub.jsonl"), last_assistant_message="Analysis complete.")
    _hook(project, "SubagentStop", stop_hook_active=False, agent_id="def457", agent_type="Explore")
    _write(project, "b.py")
    _hook(project, "Stop", last_assistant_message="done")
    _no_errors(project)
    assert _changed(project, 0) == ["a.py", "b.py"] and not _un(project, 0), _turn(project, 0)


def test_a_stop_for_a_session_whose_live_state_was_lost_attributes_nothing(tmp_path):
    """(Passes on 57d5610 too.)"""
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "a.py")
    (project / ".devmemory" / "live" / f"{SID}.state.json").unlink()
    _hook(project, "Stop", last_assistant_message="done")
    _no_errors(project)
    assert _changed(project, 0) == [] and _un(project, 0) == {"changed_turn_start_unknown": 1}


# ---------------------------------------------------------------- hooks that did not get the session lock

class _Busy:
    """The session lock as another hook of the session holds it past LOCK_TIMEOUT_S."""

    def __init__(self, *a, **k):
        self.held = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _busy(monkeypatch, project, event, **payload):
    with monkeypatch.context() as m:
        m.setattr(hooks, "_Lock", _Busy)
        _hook(project, event, **payload)


def test_a_prompt_whose_hook_did_not_get_the_lock_still_starts_its_turn(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    _start(project)
    _busy(monkeypatch, project, "UserPromptSubmit", prompt="first")
    assert (project / ".devmemory" / "live" / f"{SID}.prompt").is_file()
    _write(project, "a.py")
    _hook(project, "Stop", last_assistant_message="done")
    _no_errors(project)
    assert _changed(project, 0) == ["a.py"] and not _un(project, 0), _turn(project, 0)
    assert not (project / ".devmemory" / "live" / f"{SID}.prompt").exists()


def test_an_interrupted_turn_whose_next_prompt_missed_the_lock_is_counted_not_attributed(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _bash(project)
    _busy(monkeypatch, project, "UserPromptSubmit", prompt="second after Esc")
    _write(project, "t2.py")
    _hook(project, "Stop", last_assistant_message="done")
    _no_errors(project)
    assert _changed(project, 1) == ["t2.py"] and _un(project, 1) == {"changed_turn_end_unknown": 1}, _turn(project, 1)


def test_a_stop_that_did_not_get_the_lock_ends_its_turn_at_its_time(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _bash(project)
    _busy(monkeypatch, project, "Stop", last_assistant_message="d1")
    _hook(project, "UserPromptSubmit", prompt="second")
    _write(project, "t2.py")
    _hook(project, "Stop", last_assistant_message="d2")
    _no_errors(project)
    assert _changed(project, 0) == ["t1.py"] and _turn(project, 0)["turn_end"] == sessions.TURN_END_WHY["stop_unchecked"]
    assert _changed(project, 1) == ["t2.py"] and not _un(project, 1)


# ---------------------------------------------------------------- the turn's start in the live state

def _edit_state(project, **kv):
    f = project / ".devmemory" / "live" / f"{SID}.state.json"
    st = json.loads(f.read_text(encoding="utf-8"))
    st.update(kv)
    f.write_text(json.dumps(st), encoding="utf-8")


@pytest.mark.parametrize("case", ["year_2000", "eight_days", "before_session_start", "an_hour_ahead"])
def test_a_turn_start_out_of_range_is_unknown(tmp_path, case):
    """The reviewer's pa_far_past2: prompt_at = 946684800 with a garbage git_checked_at attributed a file dated 2001."""
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    old = 1_000_000_000                                   # 2001
    _write(project, "a.py")
    os.utime(project / "a.py", (old, old))
    now = time.time()
    pa = {"year_2000": 946_684_800, "eight_days": now - 8 * 86400, "before_session_start": now - 3600,
          "an_hour_ahead": now + 3600}[case]
    _edit_state(project, prompt_at=pa, git_checked_at="x", **({"started_at": sessions.now_iso()}
                                                              if case == "before_session_start" else {}))
    if case in ("eight_days", "before_session_start"):
        os.utime(project / "a.py", (pa + 10, pa + 10))   # dated inside what the false start would claim
    _hook(project, "Stop", last_assistant_message="done")
    _no_errors(project)
    assert _changed(project, 0) == [] and _un(project, 0) == {"changed_turn_start_unknown": 1}, _turn(project, 0)


def test_git_failing_at_the_session_start_is_unknown_not_no_git(tmp_path, monkeypatch):
    """git timed out (or failed) when the session started, in a git work tree: the start was taken as "not a git
    project" and nothing git showed was ever counted. It is unknown now: the first Stop counts what git shows and becomes
    the known check."""
    project = make_project(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(sessions, "git_status_sized", lambda *a, **k: (None, False))
        _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "a.py")
    _hook(project, "Stop", last_assistant_message="d1")
    _hook(project, "UserPromptSubmit", prompt="second")
    _write(project, "b.py")
    _hook(project, "Stop", last_assistant_message="d2")
    _no_errors(project)
    assert _changed(project, 0) == [] and _un(project, 0) == {"deleted": 0, "changed": 1}, _turn(project, 0)
    assert _changed(project, 1) == ["b.py"] and not _un(project, 1), _turn(project, 1)


def test_a_project_without_git_records_no_git_counts(tmp_path):
    project = make_project(tmp_path, git=False)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "a.py")
    _hook(project, "Stop", last_assistant_message="d1")
    _no_errors(project)
    assert "git_unattributed" not in _turn(project, 0) and "files_changed_observed" not in _turn(project, 0)


@pytest.mark.parametrize("ending", ["Stop", "next_prompt"])
def test_a_turn_whose_end_git_could_not_check_is_not_taken_for_between_turns(tmp_path, monkeypatch, ending):
    """git failed (or devmem waited after a git timeout) when the turn ended: its changes are counted at the next check
    as made in a turn whose end was not checked, not as made between turns."""
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _bash(project)
    with monkeypatch.context() as m:
        m.setattr(sessions, "git_status", lambda *a, **k: None)
        if ending == "Stop":
            _hook(project, "Stop", last_assistant_message="d1")
            _hook(project, "UserPromptSubmit", prompt="second")
        else:
            _hook(project, "UserPromptSubmit", prompt="second")
    _write(project, "t2.py")
    _hook(project, "Stop", last_assistant_message="d2")
    _no_errors(project)
    assert _changed(project, 1) == ["t2.py"] and _un(project, 1) == {"changed_turn_end_unknown": 1}, _turn(project, 1)


def test_a_session_resumed_after_it_ended_mid_turn_continues_its_log(tmp_path):
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "UserPromptSubmit", prompt="first")
    _write(project, "t1.py")
    _bash(project)
    _hook(project, "SessionEnd", reason="other")
    _hook(project, "SessionStart", source="resume")            # the same session again, before any other started
    _hook(project, "UserPromptSubmit", prompt="second")
    _write(project, "t2.py")
    _hook(project, "Stop", last_assistant_message="done")
    _no_errors(project)
    assert (project / ".devmemory" / "live" / f"{SID}.jsonl").exists()
    assert _changed(project, 0) == ["t1.py"] and _turn(project, 0)["turn_end"] == sessions.TURN_END_WHY["resume"]
    assert _changed(project, 1) == ["t2.py"] and _rec(project)["ended_at"] is None
