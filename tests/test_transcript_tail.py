"""The tail of a session's transcript is read at SessionEnd (or when an abandoned session is finalised), and text
that is still left out says so in the record.

Since the Stop hook reads one bounded part of the transcript per event (97edb1d: about 200,000 characters), a last
turn holding more than that lost the rest: SessionEnd did not read the transcript, and nothing in the record said
anything was missing (8 MB transcript, one Stop: 50 of about 1,950 texts recorded, silently). Now SessionEnd reads
one more event's budget, and what is still unread, a line longer than the read cap that was skipped, or a line cut to
the text budget, is listed in `transcript_gaps` with a human-readable note (records without it load unchanged).
Usage is counted once per message id even when a message's lines fall on both sides of a read's end.
"""
from __future__ import annotations

import json
import os
import time

import pytest
from hooks_helpers import make_project

from devmem import cli, hooks
from devmem import sessions as S
from devmem.mcp_server import GAPS_KEY, read_session_text
from devmem.pack import build_pack

BODY = ("plain words " * 400)[:3990]
SECRET = "AKIA" + "Q" * 16


def _line(mid: str, text: str, ts: str = "2026-11-14T00:00:01Z", out_tokens: int = 1) -> str:
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "id": mid, "model": "x", "usage": {"input_tokens": 10, "output_tokens": out_tokens},
        "content": [{"type": "text", "text": text}]}}) + "\n"


def _transcript(path, n: int, secret_from: int | None = None) -> list[str]:
    texts = []
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            t = f"#{i} " + (f"key {SECRET} " if secret_from is not None and i >= secret_from else "") + BODY
            f.write(_line(f"m{i}", t))
            texts.append(t)
    return texts


def _hook(event: str, project, sid: str = "sess-tail", **payload) -> None:
    code, _ = hooks.run([event], json.dumps({"session_id": sid, "cwd": str(project), "hook_event_name": event,
                                             **payload}).encode("utf-8"))
    assert code == 0


def _record(project, sid: str = "sess-tail") -> dict:
    return next(s for s in S.load_sessions(project / ".devmemory") if s.get("session_id") == sid)


def _no_secret(store) -> None:
    for p in store.rglob("*"):
        if p.is_file():
            assert SECRET.encode() not in p.read_bytes(), p


@pytest.fixture
def project(tmp_path, monkeypatch):
    p = make_project(tmp_path, git=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(p))
    return p


def _session(project, tp, stops: int = 1, end: bool = True, sid: str = "sess-tail") -> None:
    _hook("SessionStart", project, sid, source="startup", transcript_path=str(tp))
    _hook("UserPromptSubmit", project, sid, prompt="write the long report", transcript_path=str(tp))
    for _ in range(stops):
        _hook("Stop", project, sid, transcript_path=str(tp), last_assistant_message="done")
    if end:
        _hook("SessionEnd", project, sid, reason="exit", transcript_path=str(tp))


def test_session_end_reads_what_the_last_stop_left(project, tmp_path):
    tp = tmp_path / "t.jsonl"
    texts = _transcript(tp, 80, secret_from=60)      # 80 x 4,000 characters: more than one Stop takes in
    _session(project, tp)
    rec = _record(project)
    assert [a["text"].split(" ", 1)[0] for a in rec["assistant_text"]] == [t.split(" ", 1)[0] for t in texts]
    assert "transcript_gaps" not in rec and not any("transcript_gaps" in t for t in rec["turns"])
    assert rec["usage"]["output_tokens"] == 80
    _no_secret(project / ".devmemory")


def test_what_even_session_end_does_not_reach_is_labelled_everywhere(project, tmp_path, capsys):
    tp = tmp_path / "t.jsonl"
    _transcript(tp, 130, secret_from=55)             # two events' budgets are 100 of these texts
    _session(project, tp)
    store = project / ".devmemory"
    rec = _record(project)
    assert len(rec["assistant_text"]) == 100
    _transcript(tmp_path / "first100.jsonl", 100, secret_from=55)      # the same first 100 lines
    unread = tp.stat().st_size - (tmp_path / "first100.jsonl").stat().st_size
    [gap] = rec["transcript_gaps"]
    assert gap["what"] == "unread" and gap["bytes"] == unread and gap["turn"] == 0
    assert f"{unread:,} bytes" in gap["note"] and "not in this record" in gap["note"]
    assert gap["source"] == S.GAP_SOURCE
    assert rec["turns"][0]["transcript_gaps"][0]["note"] == gap["note"]
    _no_secret(store)
    # every reader shows it, or at least works with it
    view = json.loads(read_session_text(store, project, "sess-tail"))
    assert view["turns"][0][GAPS_KEY] == [gap["note"]]
    assert cli.main(["show", "--project", str(project), "sess-tail"]) == 0
    assert gap["note"] in capsys.readouterr().out
    build_pack(project, store, "write the long report")
    assert cli.main(["forget", "--project", str(project), "sess-tail"]) == 0
    assert not list((store / "sessions").glob("*")) and not any(s.get("session_id") == "sess-tail"
                                                               for s in S.load_sessions(store))


def test_session_end_stays_well_inside_its_timeout_on_a_big_transcript(project, tmp_path):
    tp = tmp_path / "t.jsonl"
    _transcript(tp, 2000)                             # about 8 MB
    _session(project, tp, end=False)
    t = time.perf_counter()
    _hook("SessionEnd", project, reason="exit", transcript_path=str(tp))
    wall = time.perf_counter() - t
    assert wall < 7.5, wall                           # about 0.5 s measured; Claude Code's SessionEnd timeout is 15 s
    rec = _record(project)
    assert len(rec["assistant_text"]) == 100 and rec["transcript_gaps"][0]["what"] == "unread"


def test_a_skipped_line_and_a_cut_line_are_labelled(project, tmp_path):
    tp = tmp_path / "t.jsonl"
    long_line = _line("m1", "y" * (2 * 1024 * 1024 + 10))
    blocks = [{"type": "text", "text": f"block {i} " + "z" * 3990} for i in range(60)]
    cut_line = json.dumps({"type": "assistant", "timestamp": "2026-11-14T00:00:02Z",
                           "message": {"id": "m2", "content": blocks}}) + "\n"
    tp.write_bytes((_line("m0", "before") + long_line + cut_line + _line("m3", "after")).encode("utf-8"))
    _session(project, tp, stops=3)
    rec = _record(project)
    said = [a["text"].split(" ", 1)[0] for a in rec["assistant_text"]]
    kept, used = 0, 0
    while used < S.TRANSCRIPT_TEXT_BUDGET:            # texts are taken while the budget is not yet reached
        used, kept = used + len(blocks[kept]["text"]), kept + 1
    dropped = sum(len(b["text"]) for b in blocks[kept:])
    assert said[0] == "before" and said[-1] == "after" and said.count("block") == kept
    gaps = {g["what"]: g for g in rec["transcript_gaps"]}
    assert set(gaps) == {"skipped-line", "cut"}
    assert gaps["skipped-line"]["bytes"] == len(long_line.encode())
    assert f"{len(long_line.encode()):,} bytes was skipped" in gaps["skipped-line"]["note"]
    assert gaps["cut"]["chars"] == dropped
    assert f"{dropped:,} characters are not in this record" in gaps["cut"]["note"]


def test_a_text_cut_to_the_text_cap_is_marked(project, tmp_path):
    tp = tmp_path / "t.jsonl"
    tp.write_text(_line("m0", "w" * (S.TEXT_CAP + 500)), encoding="utf-8")
    _session(project, tp)
    rec = _record(project)
    [a] = rec["assistant_text"]
    assert a["truncated"] is True and len(a["text"]) <= S.TEXT_CAP
    view = json.loads(read_session_text(project / ".devmemory", project, "sess-tail"))
    assert "cut when recorded" in view["turns"][0]["assistant_text (assistant-visible)"][0]


def _split_message_transcript(path) -> None:
    """49 messages, then message mX whose two text lines fall on both sides of the first read's end, then mY."""
    with open(path, "w", encoding="utf-8") as f:
        for i in range(49):
            f.write(_line(f"m{i}", f"#{i} " + BODY))
        f.write(_line("mX", "#X1 " + BODY))
        f.write(_line("mX", "#X2 " + BODY))
        f.write(_line("mY", "#Y " + BODY))


def test_usage_of_a_message_split_across_two_reads_is_counted_once(tmp_path):
    tp = tmp_path / "t.jsonl"
    _split_message_transcript(tp)
    seen: set = set()
    items, offset = S.read_transcript_since(str(tp), 0, seen_msg=seen)
    assert [it["text"][:4] for it in items if it["kind"] == "text"][-1] == "#X1 "      # the read ends inside mX
    more, _ = S.read_transcript_since(str(tp), offset, seen_msg=seen)
    ids = [it["id"] for it in items + more if it["kind"] == "usage"]
    assert len(ids) == len(set(ids)) == 51


def test_usage_of_a_split_message_is_counted_once_by_the_hooks(project, tmp_path):
    tp = tmp_path / "t.jsonl"
    _split_message_transcript(tp)
    _session(project, tp, stops=2)
    rec = _record(project)
    assert len(rec["assistant_text"]) == 52
    assert rec["usage"]["output_tokens"] == 51 and rec["usage"]["input_tokens"] == 510


def _age(store, sid: str) -> None:
    old = time.time() - hooks.ABANDONED_AFTER_S - 3600
    os.utime(store / "live" / f"{sid}.jsonl", (old, old))


def test_an_abandoned_session_is_finalised_with_its_tail_one_per_session_start(project, tmp_path):
    store = project / ".devmemory"
    for sid in ("sess-a", "sess-b"):
        _transcript(tmp_path / f"{sid}.jsonl", 80, secret_from=70)
        _session(project, tmp_path / f"{sid}.jsonl", end=False, sid=sid)
    for sid in ("sess-a", "sess-b"):
        _age(store, sid)
    _hook("SessionStart", project, "sess-c", source="startup", transcript_path=str(tmp_path / "c.jsonl"))
    left = sorted(p.stem for p in (store / "live").glob("sess-[ab].jsonl"))
    assert len(left) == 1                       # one transcript read per SessionStart; the other waits
    done = ({"sess-a", "sess-b"} - set(left)).pop()
    rec = _record(project, done)
    assert rec["ended_reason"] == "no SessionEnd seen (finalised later)" and len(rec["assistant_text"]) == 80
    assert "transcript_gaps" not in rec
    _hook("SessionStart", project, "sess-d", source="startup", transcript_path=str(tmp_path / "d.jsonl"))
    assert not list((store / "live").glob("sess-[ab].jsonl"))
    assert len(_record(project, left[0])["assistant_text"]) == 80
    _no_secret(store)


def test_an_abandoned_session_whose_state_names_no_transcript_file_is_finalised_as_before(project, tmp_path):
    store = project / ".devmemory"
    tp = tmp_path / "t.jsonl"
    _transcript(tp, 80)
    _session(project, tp, end=False)
    state = store / "live" / "sess-tail.state.json"
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["transcript_path"] == str(tp)
    st["transcript_path"] = str(project / "README.md")          # not a transcript: never read from a state file
    state.write_text(json.dumps(st), encoding="utf-8")
    _age(store, "sess-tail")
    _hook("SessionStart", project, "sess-c", source="startup", transcript_path=str(tmp_path / "c.jsonl"))
    rec = _record(project)
    assert rec["ended_reason"] == "no SessionEnd seen (finalised later)" and len(rec["assistant_text"]) == 50


def test_records_without_the_field_load_unchanged_and_odd_values_are_tolerated(project, tmp_path):
    tp = tmp_path / "t.jsonl"
    _transcript(tp, 3)
    _session(project, tp)
    store = project / ".devmemory"
    meta = next((store / "sessions").glob("*.meta.json"))
    raw = json.loads(meta.read_text(encoding="utf-8"))
    assert "transcript_gaps" not in raw and "transcript_gaps" not in raw["turns"][0]
    loaded = {k: v for k, v in _record(project).items() if k != "_meta"}
    assert loaded == raw
    raw["transcript_gaps"] = [{"what": "unread", "bytes": "many"}, 7, None]
    raw["turns"][0]["transcript_gaps"] = "not a list"
    meta.write_text(json.dumps(raw), encoding="utf-8")
    rec = _record(project)
    assert [g["what"] for g in rec["transcript_gaps"]] == ["unread"] and rec["transcript_gaps"][0]["note"]
    assert rec["turns"][0]["transcript_gaps"] == []
    json.loads(read_session_text(store, project, "sess-tail"))
