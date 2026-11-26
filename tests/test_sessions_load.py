"""The single session loader: schema-1 records load as one-turn sessions, nothing is rewritten; forget."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from devmem import sessions as S

REPO = Path(__file__).resolve().parent.parent
V1_DIR = REPO / "fixtures" / "blade-swarm" / "memory" / "sessions"


def _copy_v1(store: Path, n: int = 3) -> list[Path]:
    from devmem.store import ensure_store
    ensure_store(store)      # a store devmem made, so this user's (load_sessions loads nothing from another one)
    (store / "sessions").mkdir(parents=True)
    metas = sorted(V1_DIR.glob("*.meta.json"))[:n]
    assert metas, "real schema-1 records expected in fixtures/blade-swarm/memory"
    out = []
    for m in metas:
        shutil.copyfile(m, store / "sessions" / m.name)
        out.append(store / "sessions" / m.name)
    return out


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_v1_records_load_as_one_turn_without_rewrite(tmp_path):
    store = tmp_path / ".devmemory"
    copies = _copy_v1(store)
    before = {p: _digest(p) for p in copies}
    ss = S.load_sessions(store)
    assert len(ss) == len(copies)
    for s, p in zip(ss, copies):
        v1 = json.loads(p.read_text(encoding="utf-8"))
        assert s["schema"] == 2 and s["upgraded_from"] == 1 and s["_meta"] == p.name
        assert len(s["turns"]) == 1
        t = s["turns"][0]
        assert t["prompt"] == v1["prompt"] and t["result_text"] == v1["result_text"]
        assert t["tool_calls"] == v1["tool_calls"] and t["files_edited"] == v1["files_edited"]
        # v1 aggregate fields untouched for v1 readers
        for k in ("prompt", "result_text", "tool_calls", "files_read", "files_edited", "usage", "session_id"):
            assert s[k] == v1[k]
        assert s["files_deleted"] == [] and s["git"] == {}
    assert {p: _digest(p) for p in copies} == before


def test_v2_record_roundtrip_and_aggregates(tmp_path):
    store = tmp_path / "store"
    t0 = S.new_turn(0, "first ask", "2026-11-13T10:00:00Z")
    t0["tool_calls"] = [{"tool": "Write", "file": "a.py", "new": "x = 1\n", "truncated": False},
                        {"tool": "Read", "file": "b.py"}]
    t1 = S.new_turn(1, "second ask", "2026-11-13T10:05:00Z")
    t1["tool_calls"] = [{"tool": "Edit", "file": "b.py", "old": "1", "new": "2", "truncated": False},
                        {"tool": "Delete", "file": "c.py"}]
    t1["result_text"], t1["result_source"] = "done", "assistant-visible"
    rec = {"session_id": "abcdef0123", "started_at": "2026-11-13T10:00:00Z", "provider": "claude-code",
           "capture_method": "hooks", "turns": [t0, t1]}
    S.derive_aggregates(rec)
    meta = S.write_record(store, rec)
    assert meta.name == f"20261113T100000_{S.id_tag(rec['session_id'])}.meta.json"   # <start>_<8 chars>_<id hash>
    [s] = S.load_sessions(store)
    assert s["prompt"] == "first ask" and s["result_text"] == "done"
    assert s["files_edited"] == ["a.py", "b.py"] and s["files_read"] == ["b.py"] and s["files_deleted"] == ["c.py"]
    assert [c["tool"] for c in s["tool_calls"]] == ["Write", "Read", "Edit", "Delete"]


def test_cap_marks_truncation():
    text, trunc = S.cap("x" * 5000)
    assert len(text) == S.TEXT_CAP and trunc
    assert S.cap("short") == ("short", False)


def test_tool_entries_write_multiedit_notebook(tmp_path):
    w = S.tool_entries("Write", {"file_path": str(tmp_path / "big.py"), "content": "y" * 4500}, tmp_path)
    assert w[0]["file"] == "big.py" and len(w[0]["new"]) == 4000 and w[0]["truncated"] is True
    me = S.tool_entries("MultiEdit", {"file_path": str(tmp_path / "m.py"), "edits": [
        {"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "d", "replace_all": True}]}, tmp_path)
    assert [(e["old"], e["new"], e["edit_index"]) for e in me] == [("a", "b", 0), ("c", "d", 1)]
    assert me[1]["replace_all"] is True
    nb = S.tool_entries("NotebookEdit", {"notebook_path": str(tmp_path / "n.ipynb"), "new_source": "print(1)"},
                        tmp_path)
    assert nb[0]["file"] == "n.ipynb" and nb[0]["new"] == "print(1)"
    outside = S.tool_entries("Read", {"file_path": "/etc/hosts"}, tmp_path)
    assert "file" not in outside[0] and outside[0]["path_omitted"] == S.OUTSIDE_PROJECT   # not stored (project_file)


def _make_sessions(store: Path) -> list[str]:
    ids = []
    for i, day in enumerate(["2026-10-01", "2026-10-15", "2026-11-02"]):
        sid = f"{i}{i}aa{i}bbb-1234"
        t = S.new_turn(0, f"ask {i}", f"{day}T09:00:00Z")
        rec = {"session_id": sid, "started_at": f"{day}T09:00:00Z", "turns": [t]}
        S.derive_aggregates(rec)
        S.write_record(store, rec, raw_lines=['{"type": "prompt"}'])
        ids.append(sid)
    live = store / "live"
    live.mkdir()
    (live / f"{ids[2]}.state.json").write_text("{}", encoding="utf-8")
    return ids


def test_forget_session_by_prefix(tmp_path):
    store = tmp_path / "s"
    ids = _make_sessions(store)
    res = S.forget_session(store, ids[1][:6])
    assert res["sessions"] == [ids[1]] and len(res["removed"]) == 2  # meta + raw
    assert [s["session_id"] for s in S.load_sessions(store)] == [ids[0], ids[2]]
    with pytest.raises(ValueError):
        S.forget_session(store, "zzzz")
    with pytest.raises(ValueError):
        S.forget_session(store, "ab")  # too short


def test_forget_ambiguous_prefix(tmp_path):
    store = tmp_path / "s"
    for sid in ("abcd1111", "abcd2222"):
        rec = {"session_id": sid, "started_at": "2026-11-01T00:00:00Z", "turns": [S.new_turn(0, "x", None)]}
        S.write_record(store, S.derive_aggregates(rec))
    with pytest.raises(ValueError, match="matches 2"):
        S.forget_session(store, "abcd")


def test_forget_before_and_all(tmp_path):
    store = tmp_path / "s"
    ids = _make_sessions(store)
    res = S.forget_before(store, "2026-10-20")
    assert res["sessions"] == sorted(ids[:2]) and len(res["removed"]) == 4
    res = S.forget_all(store)
    assert res["sessions"] == [ids[2]]
    assert not list((store / "sessions").glob("*")) and not list((store / "live").glob("*"))


def test_live_log_without_record_is_loaded(tmp_path):
    from devmem.store import ensure_store
    store = ensure_store(tmp_path / "s")
    (store / "live").mkdir(parents=True)
    events = [{"event": "UserPromptSubmit", "ts": "2026-11-13T10:00:00.000Z", "session_id": "live1234x",
               "prompt_id": "p1", "prompt": "fix the bug"},
              {"event": "PostToolUse", "ts": "2026-11-13T10:00:01.000Z", "session_id": "live1234x",
               "prompt_id": "p1", "tool": "Edit", "tool_use_id": "t1",
               "calls": [{"tool": "Edit", "file": "a.py", "old": "1", "new": "2", "truncated": False}]}]
    (store / "live" / "live1234x.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n{torn line", encoding="utf-8")
    [s] = S.load_sessions(store)
    assert s["in_progress"] and s["_meta"] == "live/live1234x.jsonl"
    assert s["prompt"] == "fix the bug" and s["files_edited"] == ["a.py"]
    assert S.load_sessions(store, include_live=False) == []
