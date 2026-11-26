"""build_pack under a deadline (hooks, MCP): no scan of its own, a damaged map.json is treated as missing, the
session index is brought up to date in batches only while time is left, and its stored state is read tolerantly."""
from __future__ import annotations

import json
import time

import pytest

from pack_helpers import setup, turn, write_session

from devmem import pack as P
from devmem.index import Index

TASK = "change the reroll cost in the upgrade shop"


def _no_scan(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("build_pack scanned under a deadline")
    monkeypatch.setattr(P, "scan", refuse)


@pytest.mark.parametrize("text", ["", "{", "[]", "null", '"x"', "5", '{"files": []}', '{"files": {"a.py": 5}}',
                                  '{"files": {"src/shop.py": {"deps": 5}}}',
                                  '{"files": {"src/shop.py": {"symbols": [1, 2]}}}',
                                  '{"files": {"src/shop.py": {"module": ["x"]}}}',
                                  '{"files": {"src/shop.py": {"remotes_used": {"a": 5}}}}',
                                  '{"files": {}, "conventions": 5}',
                                  '{"files": {}, "conventions": {"config_modules": {"a.py": 5}}}',
                                  '{"files": {}, "used_by": []}', '{"files": {}, "used_by": {"a.py": 3}}',
                                  "[" * 100_000])
def test_a_damaged_map_is_treated_as_missing(tmp_path, monkeypatch, text):
    project, store, _recs = setup(tmp_path)
    (store / "index" / "map.json").write_text(text, encoding="utf-8")
    _no_scan(monkeypatch)
    out, info = P.build_pack(project, store, TASK, deadline=time.monotonic() + 30)   # from the index's files
    assert "src/shop.py" in info["files"] and "src/shop.py" in out
    monkeypatch.undo()
    out, info = P.build_pack(project, store, TASK)          # the CLI: scans again
    assert "src/shop.py" in info["files"]
    assert P._map_ok(json.loads((store / "index" / "map.json").read_text(encoding="utf-8")))


def test_no_map_under_a_deadline_uses_the_index_and_never_scans(tmp_path, monkeypatch):
    project, store, _recs = setup(tmp_path)
    (store / "index" / "map.json").unlink()
    _no_scan(monkeypatch)
    _out, info = P.build_pack(project, store, TASK, deadline=time.monotonic() + 30)
    assert "src/shop.py" in info["files"]
    assert not (store / "index" / "map.json").exists()


@pytest.mark.parametrize("raw", ["[1, 2]", '"x"', "5", "{", '{"a": 5}', "[" * 100_000])
def test_damaged_session_fingerprints_are_read_as_empty(tmp_path, raw):
    project, store, recs = setup(tmp_path)
    with Index(store) as idx:
        P._sync_sessions(store, idx)
        idx.meta_set("pack_session_fps", raw)
        idx.meta_set("pack_sessions_fp", "")
        out = P._sync_sessions(store, idx)
        assert out["changed"] == 3 and idx.count("session") == 3
    _text, info = P.build_pack(project, store, TASK)
    assert any(s["session"] == recs["shop"]["session_id"] for s in info["sessions"])


def test_past_the_deadline_sessions_are_indexed_a_record_at_a_time_newest_first(tmp_path, monkeypatch):
    project, store, _recs = setup(tmp_path, with_sessions=False)
    monkeypatch.setattr(P, "SESSION_BATCH", 4)
    for i in range(10):
        write_session(store, f"{i:08d}-0000-0000-0000-000000000000", f"2026-11-01T10:{i:02d}:00Z",
                      [turn(f"session number {i} about the shop", "ok")])
    with Index(store) as idx:
        out = P._sync_sessions(store, idx, deadline=time.monotonic() - 1)    # already past: one record only
        assert out["changed"] == 10 and out["indexed"] == 1 and idx.count("session") == 1
        assert any("00000009" in k for k in idx.keys("session"))     # the newest first
        assert idx.meta_get("pack_sessions_fp") is None
        P._sync_sessions(store, idx, deadline=time.monotonic() - 1)
        assert idx.count("session") == 2
        P._sync_sessions(store, idx)
        assert idx.count("session") == 10
        assert P._sync_sessions(store, idx)["skipped"] is True


def test_build_pack_past_its_deadline_skips_session_sync_and_embeddings(tmp_path, monkeypatch):
    project, store, _recs = setup(tmp_path)
    calls = []
    monkeypatch.setattr(P, "_sync_sessions", lambda *a, **k: calls.append("sync") or {})
    from devmem import embed
    monkeypatch.setattr(embed, "rank_for_pack", lambda *a, **k: calls.append("emb"))
    _out, info = P.build_pack(project, store, TASK, deadline=time.monotonic() - 1)
    assert calls == [] and "src/shop.py" in info["files"]
    P.build_pack(project, store, TASK, deadline=time.monotonic() + 30)
    assert calls == ["sync", "emb"]
