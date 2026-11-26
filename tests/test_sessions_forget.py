"""`forget` removes every file of a session, including one still in progress, and nothing of it stays in the
store or the index afterwards."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from devmem import generic, hooks
from devmem import sessions as S

from hooks_helpers import SESSION_ID, load_payload, make_project

WORD = "zebraquokka"  # distinctive: must not be found anywhere in the store after forget


def _hits(store: Path) -> list[str]:
    """Every file under the store whose bytes contain WORD (index databases included)."""
    out = []
    for p in store.rglob("*"):
        if p.is_file() and WORD.encode() in p.read_bytes():
            out.append(p.relative_to(store).as_posix())
    return out


def _in_progress_session(project: Path, monkeypatch) -> None:
    """Two prompts, a Stop after the first (which writes sessions/<base>.meta.json and .jsonl), no SessionEnd."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    p1 = dict(load_payload("002_UserPromptSubmit", project), prompt=f"rotate the {WORD} plan")
    stop = dict(load_payload("022_Stop", project), last_assistant_message=f"done with {WORD}")
    p2 = dict(p1, prompt=f"second prompt mentions {WORD} again", prompt_id="p2")
    for ev, pl in (("UserPromptSubmit", p1), ("Stop", stop), ("UserPromptSubmit", p2)):
        assert hooks.run([ev], json.dumps(pl).encode("utf-8"))[0] == 0


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


def test_forget_in_progress_session_removes_record_raw_copy_and_live_files(project, monkeypatch):
    from devmem.pack import sync_session_index
    store = project / ".devmemory"
    _in_progress_session(project, monkeypatch)
    sync_session_index(store)
    assert any(h.startswith("sessions/") for h in _hits(store)) and any(h.startswith("live/") for h in _hits(store))
    (store / "live" / f".{SESSION_ID}.state.json.999.tmp").write_text(WORD, encoding="utf-8")  # interrupted write
    res = S.forget_session(store, SESSION_ID[:8])
    assert res["sessions"] == [SESSION_ID]
    assert not list((store / "sessions").glob("*")) and not list((store / "live").glob("*"))
    sync_session_index(store)
    assert _hits(store) == [], _hits(store)


def test_forget_all_removes_the_mcp_call_log_and_pending_begins(project, monkeypatch):
    store = project / ".devmemory"
    _in_progress_session(project, monkeypatch)
    (store / "mcp_calls.jsonl").write_text(json.dumps({"tool": "end_session", "args": {"prompt": WORD}}) + "\n",
                                           encoding="utf-8")
    generic.begin(project, store)
    S.forget_all(store)
    assert not (store / "mcp_calls.jsonl").exists() and not list((store / "pending").glob("*"))
    from devmem.pack import sync_session_index
    sync_session_index(store)
    assert _hits(store) == [], _hits(store)


def test_forget_generic_session_removes_its_leftover_begin(project):
    store = project / ".devmemory"
    p = generic.begin(project, store)
    (project / "greet.py").write_text(f"# {WORD}\n", encoding="utf-8")
    meta = generic.end(project, f"note {WORD}", store=store, pending_id=p.stem)
    p.write_text("{}", encoding="utf-8")  # as if `end` crashed after writing the record
    sid = json.loads(meta.read_text(encoding="utf-8"))["session_id"]
    removed = S.forget_session(store, sid[:8])["removed"]
    assert str(p) in removed and not meta.exists()
