"""A passing SQLite error that says nothing about the file (an I/O error or a sharing violation while another process
or a virus scanner has it open) never deletes the index. Before, every error other than "locked"/"busy" rebuilt it from
empty, so a pack built at that moment (right after a killed refresh, say) had no files at all.

The stress test kills the refresh child at random points and checks after each kill that a file indexed before is
still found by the pack built right then."""
from __future__ import annotations

import json
import random
import sqlite3
import time
from pathlib import Path

import pytest

from devmem import index as I
from devmem.index import Index
from devmem.scan import scan
from devmem.store import ensure_store


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "damage.py").write_text("def apply_armor(damage, armor):\n    return max(0, damage - armor)\n",
                                       encoding="utf-8")
    store = ensure_store(project / ".devmemory")
    scan(project, store)
    return project, store


@pytest.mark.parametrize("message", ["disk I/O error", "unable to open database file",
                                     "attempt to write a readonly database", "database or disk is full"])
def test_a_passing_error_in_a_search_or_a_sync_keeps_the_index(tmp_path, monkeypatch, message):
    project, store = _project(tmp_path)
    real = Index._search_rows
    calls = []

    def flaky(self, *a, **k):
        if not calls:
            calls.append(1)
            raise sqlite3.OperationalError(message)
        return real(self, *a, **k)
    monkeypatch.setattr(Index, "_search_rows", flaky)
    with Index(store) as idx:
        assert idx.search("file", "armor") == []         # given up for now
        assert not idx.rebuilt and message in (idx.last_error or "")
        assert [r["key"] for r in idx.search("file", "armor")] == ["damage.py"]
    real_sync = Index._sync
    monkeypatch.setattr(Index, "_sync", lambda self, *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError(message)))
    with Index(store) as idx:
        assert idx.sync(project)["error"] == "locked" and not idx.rebuilt
    monkeypatch.setattr(Index, "_sync", real_sync)
    with Index(store) as idx:
        assert idx.files().keys() == {"damage.py"}


def test_an_index_that_cannot_be_opened_for_now_is_not_deleted(tmp_path, monkeypatch):
    project, store = _project(tmp_path)
    real = Index._init_schema
    calls = []

    def flaky(self):
        if not calls:
            calls.append(1)
            raise sqlite3.OperationalError("disk I/O error")
        return real(self)
    monkeypatch.setattr(Index, "_init_schema", flaky)
    with Index(store) as idx:
        assert not idx.rebuilt
    real_connect = Index._connect

    def no_connect(self, target):
        if target != ":memory:" and not calls[1:]:
            calls.append(2)
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(self, target)
    monkeypatch.setattr(Index, "_connect", no_connect)
    with Index(store) as idx:
        assert idx.degraded                              # an empty in-memory index for now; the file stays
    with Index(store) as idx:
        assert not idx.degraded and idx.files().keys() == {"damage.py"}


def test_damage_is_still_rebuilt(tmp_path):
    project, store = _project(tmp_path)
    db = store / "index" / "index.db"
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    db.write_bytes(b"not a database" * 300)
    with Index(store) as idx:
        assert idx.rebuilt and not idx.degraded


@pytest.mark.stress
def test_files_indexed_before_stay_retrievable_whatever_point_the_refresh_is_killed_at(tmp_path, monkeypatch):
    from devmem import refresh as R
    from devmem.pack import build_pack
    project, store = _project(tmp_path)
    for i in range(150):
        (project / f"m{i}.py").write_text(f"# BLOCKMARK\ndef handler_{i}():\n    return {i}\n", encoding="utf-8")
    monkeypatch.setenv(I.TEST_SWITCH_ENV, "1")
    monkeypatch.setenv(I.TEST_BLOCK_ENV, "BLOCKMARK|0.02")
    rnd = random.Random(7)
    for k in range(25):
        monkeypatch.setenv(R.TEST_BUDGET_ENV, f"{rnd.uniform(0.35, 2.5):.2f}")
        for i in rnd.sample(range(150), 10):             # keep giving it work, so the kill lands anywhere
            (project / f"m{i}.py").write_text(f"# BLOCKMARK {k}\ndef handler_{i}_{k}():\n    return {i}\n",
                                              encoding="utf-8")
        R.refresh(project, store, 8)
        text, info = build_pack(project, store, "change how apply_armor handles damage",
                                deadline=time.monotonic() + 4)
        assert "damage.py" in info["files"], (k, info.get("empty_reason"))
        m = json.loads((store / "index" / "map.json").read_text(encoding="utf-8"))
        assert "damage.py" in m["files"]
