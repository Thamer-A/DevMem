"""The live state (`live/<sid>.state.json`) is read through one validating loader (devmem.hooks.valid_state): every field
a handler reads is checked for type and range there, and a field of the wrong type reads as absent. The reviewer's
d3_hookfuzz: `turns` as a list or a string made every UserPromptSubmit raise TypeError."""
from __future__ import annotations

import json
import shutil

import pytest
from hooks_helpers import make_project

from devmem import consent, hooks, store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
SID = "5e5e5e5e-0000-4000-8000-000000000001"
WRONG = [[1, "x"], "x", {"a": 1}, None, 10 ** 400, float("nan"), -5, True, 3.5]


def _hook(project, event, **payload) -> None:
    store._FILTERS.clear()
    consent.forget_decisions()
    code, _ = hooks.run([event], json.dumps({"session_id": SID, "cwd": str(project), "hook_event_name": event,
                                              "transcript_path": str(project / "none.jsonl"),
                                              **payload}).encode("utf-8"))
    assert code == 0


def _set(project, field, value) -> None:
    f = project / ".devmemory" / "live" / f"{SID}.state.json"
    st = json.loads(f.read_text(encoding="utf-8"))
    st[field] = value
    f.write_text(json.dumps(st), encoding="utf-8")


def _errors(project) -> str:
    log = project / ".devmemory" / "hook_errors.log"
    return log.read_text(encoding="utf-8")[-3000:] if log.exists() else ""


@pytest.mark.parametrize("field", sorted(hooks._STATE_FIELDS))
def test_a_state_field_of_any_wrong_type_never_stops_recording(tmp_path, field):
    project = make_project(tmp_path)
    _hook(project, "SessionStart", source="startup")
    prompts = 0
    for i, bad in enumerate(WRONG):
        if hooks._STATE_FIELDS[field](bad):
            continue                                    # a value of the right type for this field
        _set(project, field, bad)
        _hook(project, "UserPromptSubmit", prompt=f"p{i}")
        prompts += 1
        _set(project, field, bad)
        (project / f"f{i}.py").write_text("x\n", encoding="utf-8")
        _hook(project, "PostToolUse", tool_name="Write", tool_use_id=f"w{i}",
              tool_input={"file_path": str(project / f"f{i}.py"), "content": "x"}, tool_response={"type": "create"})
        _set(project, field, bad)
        _hook(project, "Stop", last_assistant_message=f"d{i}", stop_hook_active=bool(i % 2))
        assert not _errors(project), (field, bad, _errors(project))
    for event, extra in (("PostCompact", {"trigger": "auto", "compact_summary": "s"}),
                         ("SessionStart", {"source": "resume"}), ("StopFailure", {"error": "unknown"}),
                         ("SessionEnd", {"reason": "other"})):
        _set(project, field, WRONG[0] if not hooks._STATE_FIELDS[field](WRONG[0]) else "x")
        _hook(project, event, **extra)
        assert not _errors(project), (field, event, _errors(project))
    rec = json.loads(next((project / ".devmemory" / "sessions").glob("*.meta.json")).read_text(encoding="utf-8"))
    assert len(rec["turns"]) == prompts and all(t["files_edited"] for t in rec["turns"]), rec["turns"]


@pytest.mark.parametrize("field", ["delivered", "scanned", "scan_incomplete", "turns"])
def test_the_delivery_path_reads_the_state_through_the_loader(tmp_path, monkeypatch, field):
    """Delivery on (a stand-in refresh and pack): the fields it reads, of a wrong type."""
    from devmem import pack, refresh
    monkeypatch.setattr(refresh, "refresh", lambda *a, **k: {"state": "done"})
    monkeypatch.setattr(pack, "build_pack", lambda *a, **k: ("## pack", {"keys": ["conventions"]}))
    project = make_project(tmp_path)
    (project / ".devmemory" / "config.json").write_text('{"deliver": true}', encoding="utf-8")
    _hook(project, "SessionStart", source="startup")
    for bad in WRONG:
        if hooks._STATE_FIELDS[field](bad):
            continue
        _set(project, field, bad)
        _hook(project, "UserPromptSubmit", prompt="p")
        assert not _errors(project), (field, bad, _errors(project))


def test_valid_state_keeps_what_is_right_and_drops_what_is_not():
    st = {"turns": 3, "prompt_at": 10 ** 400, "delivered": [{"turn": 0}, "x"], "spans": [[1, 2]],
          "turn_open": "yes", "unknown_field": [1]}
    out = hooks.valid_state(st)
    assert out == {"turns": 3, "delivered": [{"turn": 0}], "unknown_field": [1]}
