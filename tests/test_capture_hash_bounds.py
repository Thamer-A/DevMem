"""derive_aggregates (`devmem import`/`run`, the hooks' Stop) hashes the files a session touched within a named bound
on files (CAPTURE_HASH_FILES) and on bytes read (CAPTURE_HASH_BYTES); the rest get no hash and the record counts them.
An ordinary record is what store.hash_files made before, field for field."""
from __future__ import annotations

from devmem import sessions, store


def _rec(names):
    return {"turns": [{"files_read": list(names), "files_edited": []}]}


def _counted(monkeypatch):
    seen = {"opens": 0, "bytes": 0}
    real = store.file_hash

    def counted(p):
        seen["opens"] += 1
        try:
            seen["bytes"] += p.stat().st_size
        except OSError:
            pass
        return real(p)
    monkeypatch.setattr(store, "file_hash", counted)
    return seen


def test_an_ordinary_record_is_unchanged(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text(f"x = {i}\r\n", encoding="utf-8")
    names = [f"f{i}.py" for i in range(5)] + ["missing.py"]
    rec = sessions.derive_aggregates(_rec(names), tmp_path)
    assert rec["file_hashes_at_capture"] == store.hash_files(tmp_path, names)
    assert "file_hashes_left_out" not in rec


def test_files_past_the_count_bound_are_counted_not_hashed(tmp_path, monkeypatch):
    for i in range(10):
        (tmp_path / f"f{i}.py").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(sessions, "CAPTURE_HASH_FILES", 4)
    seen = _counted(monkeypatch)
    rec = sessions.derive_aggregates(_rec(f"f{i}.py" for i in range(10)), tmp_path)
    assert seen["opens"] == 4 and len(rec["file_hashes_at_capture"]) == 4
    assert rec["file_hashes_left_out"] == 6


def test_bytes_past_the_byte_bound_are_not_read(tmp_path, monkeypatch):
    for i in range(6):
        (tmp_path / f"big{i}.bin").write_bytes(b"z" * 100_000)
    monkeypatch.setattr(sessions, "CAPTURE_HASH_BYTES", 250_000)
    seen = _counted(monkeypatch)
    rec = sessions.derive_aggregates(_rec(f"big{i}.bin" for i in range(6)), tmp_path)
    assert seen["bytes"] <= 250_000 and seen["opens"] == 2
    assert rec["file_hashes_left_out"] == 4 and len(rec["file_hashes_at_capture"]) == 2
