"""One malformed event or store file costs at most its own session, never the whole store.

Fuzz findings MEDIUM-1, MEDIUM-5, LOW-1, LOW-2 (FUZZ/r1_poison_store.py, h4_poison.py, h5_corrupt_store.py): a
`prompt_id` that is a JSON list made every read of the store fail and emptied the pack of every other session;
wrong types in a meta.json or config.json raised tracebacks; a non-string transcript_path or a UTF-8 BOM dropped the
event; prompts and replies were stored uncapped.
"""
from __future__ import annotations

import codecs
import io
import json
import random

import pytest
from hooks_helpers import make_project

from devmem import hooks
from devmem import sessions as S
from devmem.cli import main as cli_main
from devmem.pack import build_pack


def _send(project, event, sid, **kw):
    p = {"session_id": sid, "cwd": str(project), "hook_event_name": event,
         "transcript_path": str(project / "none.jsonl"), **kw}
    return hooks.run([event], json.dumps(p).encode("utf-8"))


def _good_session(project, sid="good-0001"):
    (project / "parser.py").write_text("def parse_ledger(x):\n    return x\n", encoding="utf-8")
    _send(project, "SessionStart", sid, source="startup")
    _send(project, "UserPromptSubmit", sid, prompt="fix parse_ledger rounding in parser.py", prompt_id="a")
    _send(project, "Stop", sid, last_assistant_message="fixed parse_ledger rounding", prompt_id="a")
    _send(project, "SessionEnd", sid, reason="exit")


@pytest.fixture()
def project(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    return project


def test_a_list_prompt_id_does_not_poison_the_store(project, capsys):
    _good_session(project)
    for event, kw in [("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": "hi", "prompt_id": ["x"]}),
                      ("PostToolUse", {"tool_name": "Read", "tool_input": {"file_path": "parser.py"}, "tool_response": {},
                                       "tool_use_id": {"a": 1}, "prompt_id": [1, 2]}),
                      ("Stop", {"last_assistant_message": "m", "prompt_id": {"k": "v"}}), ("SessionEnd", {"reason": "x"})]:
        assert _send(project, event, "bad-0001", **kw)[0] == 0
    sids = {s["session_id"] for s in S.load_sessions(project / ".devmemory")}
    assert {"good-0001", "bad-0001"} <= sids
    text, info = build_pack(project, project / ".devmemory", "parse_ledger rounding")
    assert any(str(x.get("session", "")).startswith("good-000") for x in info["sessions"]), text
    for cmd in (["sessions"], ["status"], ["show", "good-0001"]):
        assert cli_main([*cmd, "-p", str(project)]) == 0, cmd
    capsys.readouterr()


def test_a_poisoned_live_log_from_an_older_version_is_skipped_and_reported(project, capsys):
    _good_session(project)
    log = project / ".devmemory" / "live" / "old-bad1.jsonl"
    log.write_text(json.dumps({"event": "SessionStart", "ts": "2026-11-01T10:00:00.000Z", "session_id": "old-bad1"})
                   + "\n" + json.dumps({"event": "UserPromptSubmit", "ts": "2026-11-01T10:00:01.000Z",
                                        "session_id": "old-bad1", "prompt": "x", "prompt_id": ["x"]}) + "\n"
                   + json.dumps({"event": "Stop", "ts": "2026-11-01T10:00:02.000Z", "session_id": "old-bad1",
                                 "prompt_id": ["x"], "calls": "nope", "git": [1]}) + "\n", encoding="utf-8")
    recs = S.load_sessions(project / ".devmemory")
    assert "good-0001" in {r["session_id"] for r in recs}
    text, info = build_pack(project, project / ".devmemory", "parse_ledger rounding")
    assert info["sessions"], text
    assert cli_main(["status", "-p", str(project)]) == 0
    capsys.readouterr()


WRONG = [None, 5, 3.5, True, "text", [], [1, "a", None], {}, {"a": [1]}]
FIELDS = ["tool_calls", "files_edited", "files_read", "files_deleted", "file_hashes_at_capture", "started_at",
          "result_text", "prompt", "turns", "usage", "git", "assistant_text", "errors", "session_id", "raw_record"]


def test_sampled_corrupted_records_never_break_readers_or_other_sessions(project, capsys):
    _good_session(project)
    _good_session(project, "victim-0002")
    store = project / ".devmemory"
    victim = next(p for p in (store / "sessions").glob("*.meta.json") if "victim-0" in p.name)
    original = json.loads(victim.read_text(encoding="utf-8"))
    rnd = random.Random(5)
    cases = [(f, w) for f in FIELDS for w in WRONG]
    rnd.shuffle(cases)
    for field, wrong in cases[:100]:
        rec = json.loads(json.dumps(original))
        if field in ("tool_calls", "files_edited", "assistant_text", "errors", "prompt", "result_text") and rec["turns"]:
            rec["turns"][0][field] = wrong          # inside a turn too
        rec[field] = wrong
        victim.write_text(json.dumps(rec), encoding="utf-8")
        recs = S.load_sessions(store)
        assert "good-0001" in {r.get("session_id") for r in recs}, (field, wrong)
        _text, info = build_pack(project, store, "parse_ledger rounding")
        assert info["sessions"], (field, wrong)
        for cmd in (["sessions"], ["status"]):
            assert cli_main([*cmd, "-p", str(project)]) == 0, (cmd, field, wrong)
    for bad in (b"{bad", b"[]", b"", b"\xff\xfe"):
        victim.write_bytes(bad)
        assert "good-0001" in {r.get("session_id") for r in S.load_sessions(store)}
    capsys.readouterr()


def test_wrong_typed_config_values_fall_back_to_defaults(project):
    store = project / ".devmemory"
    _good_session(project)
    store.joinpath("config.json").write_text(json.dumps({"pack_budget": "lots", "pack_mode": ["x"]}), encoding="utf-8")
    code, out = _send(project, "UserPromptSubmit", "probe-0001", prompt="fix parse_ledger rounding", prompt_id="q")
    assert code == 0 and "parse_ledger" in out


def test_bom_and_non_string_transcript_path_keep_the_event(project):
    sid = "bom-0001"
    payload = {"session_id": sid, "cwd": str(project), "hook_event_name": "SessionStart", "source": "startup",
               "transcript_path": ["not", "a", "path"]}
    assert hooks.run(["SessionStart"], codecs.BOM_UTF8 + json.dumps(payload).encode())[0] == 0
    payload.update(hook_event_name="UserPromptSubmit", prompt="hello there from a BOM payload")
    assert hooks.run(["UserPromptSubmit"], codecs.BOM_UTF8 + json.dumps(payload).encode())[0] == 0
    events = S.read_events(project / ".devmemory" / "live" / f"{sid}.jsonl")
    assert [e["event"] for e in events] == ["SessionStart", "UserPromptSubmit"]


def test_long_prompts_and_replies_are_capped_and_flagged(project):
    sid = "long-0001"
    _send(project, "SessionStart", sid, source="startup")
    _send(project, "UserPromptSubmit", sid, prompt="word " * 100_000, prompt_id="a")
    _send(project, "Stop", sid, last_assistant_message="reply " * 100_000, prompt_id="a")
    events = S.read_events(project / ".devmemory" / "live" / f"{sid}.jsonl")
    up = next(e for e in events if e["event"] == "UserPromptSubmit")
    stop = next(e for e in events if e["event"] == "Stop")
    assert len(up["prompt"]) <= hooks.PROMPT_CAP and up["prompt_truncated"] is True
    assert len(stop["last_assistant_message"]) <= hooks.PROMPT_CAP and stop["last_assistant_message_truncated"]


def test_stdin_read_is_bounded():
    big = io.BytesIO(b"x" * 5000)
    assert len(hooks.read_stdin(big, limit=1000)) == 1001 and big.read() == b""
    assert hooks.read_stdin(io.BytesIO(b"{}"), limit=1000) == b"{}"
