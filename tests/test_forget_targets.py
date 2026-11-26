"""`devmem forget` deletes only files inside the store, named by devmem itself.

Before: a record's `raw_record` was only checked for a leading `..`, so an absolute path (or `C:x` on Windows)
became the deletion target, and a file outside the store was deleted (reproduced by the reviewer).
"""
from __future__ import annotations

import json
import os

import pytest
from pack_helpers import make_project, turn, write_session

from devmem import sessions as S

SID = "abcd1234-0000-0000-0000-000000000001"


def _victim(tmp_path, name="victim.txt"):
    v = tmp_path / name
    v.write_text("keep me\n", encoding="utf-8")
    return v


@pytest.mark.parametrize("kind", ["absolute", "dotdot", "drive", "nested"])
def test_raw_record_naming_a_file_outside_the_store_is_not_deleted(tmp_path, kind):
    project, store = make_project(tmp_path)
    victim = _victim(tmp_path)
    rec = write_session(store, SID, "2026-11-01T10:00:00Z", [turn("add the shop", "done")])
    meta = store / "sessions" / rec["_meta"]
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["raw_record"] = {"absolute": str(victim), "dotdot": "../../victim.txt", "drive": "C:victim.txt",
                          "nested": "sub/../../../victim.txt"}[kind]
    data["begin_id"] = "../../victim"
    meta.write_text(json.dumps(data), encoding="utf-8")
    out = S.forget_session(store, SID[:8])
    assert victim.exists() and victim.read_text(encoding="utf-8") == "keep me\n"
    assert not meta.exists() and out["sessions"] == [SID]
    assert all(str(store) in r for r in out["removed"])


def test_forget_all_never_deletes_through_a_linked_folder(tmp_path):
    project, store = make_project(tmp_path)
    write_session(store, SID, "2026-11-01T10:00:00Z", [turn("add the shop", "done")])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    keep = elsewhere / "precious.jsonl"
    keep.write_text("{}\n", encoding="utf-8")
    try:
        os.symlink(elsewhere, store / "pending", target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"cannot create a symbolic link here: {e}")
    S.forget_all(store)
    assert keep.exists()
    assert not list((store / "sessions").glob("*.meta.json"))
