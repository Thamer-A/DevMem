"""A file whose extraction was slow is remembered by its content hash and indexed as plain text from then on, and the
work is committed file by file, so a slow file never makes the next prompt redo it."""
from __future__ import annotations

import json
import time

from devmem import index as I
from devmem.index import Index
from devmem.store import ensure_store


def _project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.js").write_text("function alpha() {}\n", encoding="utf-8")
    (project / "b.js").write_text("function beta() {}\n", encoding="utf-8")
    store = ensure_store(project / ".devmemory")
    return project, store


def test_a_slow_file_is_remembered_and_then_indexed_as_text(tmp_path, monkeypatch):
    project, store = _project(tmp_path)
    real = I.extract_file

    def slow_for_a(rel, text, plain=False):
        if rel == "a.js" and not plain:
            time.sleep(I.SLOW_EXTRACT_S + 0.1)
        return real(rel, text, plain=plain)
    monkeypatch.setattr(I, "extract_file", slow_for_a)
    with Index(store) as idx:
        out = idx.sync(project)
        assert out.get("slow_files") == 1
        slow = json.loads(idx.meta_get("slow_extract"))
        assert idx.files()["a.js"]["hash"] in slow
        idx.conn.execute("DELETE FROM files")                    # force both files to be read again
        t = time.perf_counter()
        idx.sync(project)
        assert time.perf_counter() - t < I.SLOW_EXTRACT_S          # a.js was not extracted again
        files = idx.files()
        assert files["a.js"].get("extract_skipped") is True and files["a.js"]["symbols"] == []
        assert files["b.js"]["symbols"] == ["beta"]
        assert idx.search("file", "alpha")                         # still found by its text


def test_past_the_deadline_each_file_is_committed(tmp_path):
    project, store = _project(tmp_path)
    with Index(store) as idx:
        out = idx.sync(project, deadline=time.perf_counter() - 1)  # already out of time: one file, then stop
        assert out.get("incomplete") and out["pending"] == 1
        assert len(idx.files()) == 1
    with Index(store) as idx:                                       # the next prompt continues where it stopped
        out = idx.sync(project)
        assert out["added"] == 1 and len(idx.files()) == 2


def test_slow_tokenising_counts_as_slow_too(tmp_path, monkeypatch):
    """The memo timed extraction only: a text that was quick to extract but slow to tokenise (500 KB of U+FDFA took
    6.5 s to tokenise, NFKC after the cut) was never remembered."""
    project, store = _project(tmp_path)
    real = I.doc_terms

    def slow_for_alpha(fields, weights=None):
        if "alpha" in (fields.get("body") or ""):
            time.sleep(I.SLOW_EXTRACT_S + 0.1)
        return real(fields, weights)
    monkeypatch.setattr(I, "doc_terms", slow_for_alpha)
    with Index(store) as idx:
        out = idx.sync(project)
        assert out.get("slow_files") == 1
        assert idx.files()["a.js"]["hash"] in json.loads(idx.meta_get("slow_extract"))


def test_a_damaged_slow_memo_never_stops_a_scan(tmp_path):
    """A value of the wrong type in the memo raised TypeError, and every scan failed until the index was rebuilt."""
    import pytest
    project, store = _project(tmp_path)
    with Index(store) as idx:
        for bad in ("5", "null", '{"a": 1}', '"text"', "[1, null, {}]", "[" * 100_000, json.dumps(["x" * 70] * 10_000)):
            assert idx.meta_set("slow_extract", bad)
            idx.conn.execute("DELETE FROM files")
            try:
                out = idx.sync(project)
            except Exception as e:  # noqa: BLE001
                pytest.fail(f"memo {bad[:20]!r}: {e!r}")
            assert out["added"] == 2 and "error" not in out
