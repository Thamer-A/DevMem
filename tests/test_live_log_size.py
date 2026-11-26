"""A long session's event log (live/<sid>.jsonl) never wipes its record. The log was read through the whole-file cap
of every store read (64 MB): past it the read gave no events, and the Stop hook rewrote the record from [] + [Stop]
(a long session went from many turns to one empty turn). Now the log is read as a stream, at most
sessions.LIVE_READ_CAP bytes per event (lowered here to keep the test small), and a record is never replaced by one
built from part of the log or with fewer turns or tool calls than it held."""
from __future__ import annotations

import json

from hooks_helpers import make_project

from devmem import hooks
from devmem import sessions as S

SID = "64646464-2222-3333-4444-555555555555"


def _send(project, event: str, **fields) -> None:
    payload = {"session_id": SID, "cwd": str(project), "transcript_path": str(project / "none.jsonl"),
               "hook_event_name": event, **fields}
    assert hooks.run([event], json.dumps(payload).encode("utf-8")) == (0, "")


def _turns(project, n: int, start: int = 0) -> None:
    for i in range(start, start + n):
        _send(project, "UserPromptSubmit", prompt=f"REQUESTMARK{i} change greet.py step {i}", prompt_id=f"p{i}")
        _send(project, "PostToolUse", tool_name="Bash", tool_use_id=f"t{i}", tool_input={"command": f"echo {i}"},
              tool_response={"stdout": "ok", "stderr": ""})
        _send(project, "Stop", last_assistant_message=f"ANSWERMARK{i} done")


def _record(store) -> dict:
    metas = sorted((store / "sessions").glob("*.meta.json"))
    assert len(metas) == 1, metas
    return json.loads(metas[0].read_text(encoding="utf-8"))


def _pad(log, size: int) -> int:
    """Append ordinary (already capped) tool events until the log holds `size` bytes; returns how many."""
    line = next(json.loads(x) for x in log.read_text(encoding="utf-8").split(chr(10)) if '"PostToolUse"' in x)
    n = 0
    with log.open("a", encoding="utf-8", newline="") as f:
        while log.stat().st_size < size:
            n += 1
            f.write(json.dumps(dict(line, tool_use_id=f"pad{n}", output="y" * 3900)) + chr(10))
            f.flush()
    return n


def test_read_events_streams_past_the_cap_and_skips_an_overlong_line(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "LIVE_LINE_CAP", 1000)
    log = tmp_path / "x.jsonl"
    good = json.dumps({"event": "Stop", "session_id": "a"})
    log.write_text(chr(10).join([good, json.dumps({"event": "Stop", "big": "z" * 5000}), good]) + chr(10),
                   encoding="utf-8")
    info: dict = {}
    assert S.read_events(log, info) == [json.loads(good)] * 2
    assert info == {"complete": True, "skipped": 1}
    monkeypatch.setattr(S, "LIVE_READ_CAP", len(good) + 1)          # one line per call: the rest is not read
    assert S.read_events(log, info) == [json.loads(good)] and info["complete"] is False


def test_a_log_longer_than_one_read_keeps_the_record(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    _send(project, "SessionStart", source="startup")
    _turns(project, 3)
    assert len(_record(store)["turns"]) == 3
    monkeypatch.setattr(S, "LIVE_READ_CAP", 256 * 1024)
    log = store / "live" / f"{SID}.jsonl"
    padded = _pad(log, 300 * 1024)
    _turns(project, 1, start=3)
    rec = _record(store)
    assert [t["prompt"][:12] for t in rec["turns"]] == [f"REQUESTMARK{i}" for i in range(3)]   # kept, not wiped
    assert "was kept as it is" in (store / "hook_errors.log").read_text(encoding="utf-8")
    raw = log.read_text(encoding="utf-8")
    assert "REQUESTMARK3" in raw and raw.count('"pad') == padded                                 # every event kept
    monkeypatch.setattr(S, "LIVE_READ_CAP", 128 * 1024 * 1024)       # a read that reaches the end: rebuilt in full
    _send(project, "SessionEnd", reason="other")
    rec = _record(store)
    assert len(rec["turns"]) == 4 and sum(len(t["tool_calls"]) for t in rec["turns"]) == 4 + padded
    assert "REQUESTMARK3" in (sorted((store / "sessions").glob("*.jsonl"))[0]).read_text(encoding="utf-8")


def test_a_log_read_back_with_fewer_turns_does_not_replace_the_record(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    _send(project, "SessionStart", source="startup")
    _turns(project, 3)
    log = store / "live" / f"{SID}.jsonl"
    lines = log.read_text(encoding="utf-8").split(chr(10))
    log.write_text(chr(10).join(lines[:2]) + chr(10), encoding="utf-8")   # the log lost its earlier turns
    _send(project, "Stop", last_assistant_message="again")
    assert len(_record(store)["turns"]) == 3
    assert "fewer turns or tool calls" in (store / "hook_errors.log").read_text(encoding="utf-8")
