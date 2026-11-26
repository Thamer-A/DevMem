"""`forget <id>` leaves nothing of the session anywhere in the store, whatever shape its files are in (security review
D-1, D-2).

Before: forget found a session only through load_sessions, so a session whose .meta.json was torn, or whose raw
.jsonl was left without a record, could not be forgotten at all ("no session with id ..."), and the opt-in MCP call
log kept a get_context_pack task or a read_session id after `forget <id>`. Now store files are also matched by
name (the id tag in sessions/, the id in live/), sessions that do not load are found from their files, and any
forget clears the call log (it cannot tell which session a call was about). The check is a raw byte search of every
file under the store, index databases and their -wal/-shm files included.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import cli, hooks
from devmem import sessions as S
from devmem.pack import build_pack, sync_session_index


def _hits(store: Path, word: str) -> list[str]:
    return sorted(p.relative_to(store).as_posix() for p in store.rglob("*")
                  if p.is_file() and word.encode() in p.read_bytes())


def _session(project: Path, sid: str, word: str, end: bool = True) -> None:
    base = {"session_id": sid, "cwd": str(project), "transcript_path": str(project / "none")}
    steps = [("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": f"fix the {word} greeting",
                                                                            "prompt_id": "p1"}),
             ("PostToolUse", {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": f"echo {word}"},
                              "tool_response": {"stdout": word}}),
             ("Stop", {"last_assistant_message": f"fixed {word}"})] + ([("SessionEnd", {"reason": "exit"})] if end else [])
    for ev, kw in steps:
        assert hooks.run([ev], json.dumps({**base, "hook_event_name": ev, **kw}).encode("utf-8"))[0] == 0


def _forget(project: Path, prefix: str) -> None:
    assert cli.main(["forget", "--project", str(project), prefix]) == 0


@pytest.fixture
def project(tmp_path, monkeypatch):
    p = make_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(p))
    return p


def _with_index(project: Path, word: str) -> None:
    store = project / ".devmemory"
    sync_session_index(store)
    build_pack(project, store, f"fix the {word} greeting")


def test_a_torn_record_is_forgotten(project):
    store, word, sid = project / ".devmemory", "zebratorn", "c4c4c4c4-0000-0000-0000-000000000004"
    _session(project, sid, word)
    _with_index(project, word)
    meta = next((store / "sessions").glob("*.meta.json"))
    meta.write_bytes(meta.read_bytes()[: meta.stat().st_size // 2])
    _forget(project, "c4c4c4c4")
    assert _hits(store, word) == []


def test_an_orphan_raw_record_and_a_lone_state_are_forgotten(project):
    store, word = project / ".devmemory", "zebraorphan"
    sid, sid2 = "c5c5c5c5-0000-0000-0000-000000000005", "c6c6c6c6-0000-0000-0000-000000000006"
    _session(project, sid, word)
    next((store / "sessions").glob("*.meta.json")).unlink()
    (store / "live" / f"{sid}.state.json").unlink()
    _forget(project, "c5c5")
    assert _hits(store, word) == []
    _session(project, sid2, word, end=False)
    (store / "live" / f"{sid2}.jsonl").unlink()
    for p in (store / "sessions").glob("*"):
        p.unlink()
    assert (store / "live" / f"{sid2}.state.json").exists()
    _forget(project, "c6c6")
    assert not list((store / "live").glob(f"{sid2}*"))


def test_forget_clears_the_mcp_call_log(project):
    store, word, sid = project / ".devmemory", "zebramcp", "aaaa1111-2222-3333-4444-555566667777"
    _session(project, sid, word)
    _session(project, "bbbb1111-2222-3333-4444-555566667777", "keepme")
    _with_index(project, word)
    (store / "mcp_calls.jsonl").write_text(
        json.dumps({"tool": "get_context_pack", "args": {"task": f"speed up {word}"}}) + "\n"
        + json.dumps({"tool": "read_session", "args": {"id": "aaaa1111"}}) + "\n", encoding="utf-8")
    _forget(project, "aaaa1111")
    assert _hits(store, word) == [] and _hits(store, "aaaa1111") == []
    assert _hits(store, "keepme")                       # the other session is still there


def test_forget_before_takes_unloadable_sessions_by_file_time(project):
    store, word, sid = project / ".devmemory", "zebrabefore", "c7c7c7c7-0000-0000-0000-000000000007"
    _session(project, sid, word)
    meta = next((store / "sessions").glob("*.meta.json"))
    meta.write_text("{", encoding="utf-8")
    res = S.forget_before(store, "2999-01-01")
    assert sid in res["sessions"]
    assert _hits(store, word) == []


def test_a_session_of_the_same_first_8_characters_is_kept(project):
    store = project / ".devmemory"
    _session(project, "dddd0000-aaaa-0000-0000-000000000001", "zebrafirst")
    _session(project, "dddd0000-bbbb-0000-0000-000000000002", "zebrasecond")
    _forget(project, "dddd0000-aaaa")
    assert _hits(store, "zebrafirst") == [] and _hits(store, "zebrasecond")
