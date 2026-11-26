"""A search reads postings and metadata from one snapshot, and a pack skips a turn row without a record name.

With 50 parallel sessions, 29 of about 300 prompts got no pack: a turn document replaced by another process between
the postings query and the metadata query came back with meta `{}`, and pack.py's `r["meta"]["m"]` raised KeyError.
"""
from __future__ import annotations

from devmem.index import Index
from devmem.store import ensure_store


def test_a_document_removed_during_a_search_keeps_its_metadata(tmp_path):
    store = ensure_store(tmp_path / ".devmemory")
    with Index(store) as idx:
        idx.upsert_doc("turn", "s1#0", {"prompt": "refactor the engine loop"}, meta={"m": "s1", "p": "refactor"})
    with Index(store) as reader, Index(store) as writer:
        real = reader._stats

        def stats_then_concurrent_delete(kids):
            out = real(kids)
            assert writer.delete_doc("turn", "s1#0")      # another process, between the two queries
            return out
        reader._stats = stats_then_concurrent_delete      # type: ignore[method-assign]
        rows = reader.search("turn", "engine loop")
    assert rows and rows[0]["meta"] == {"m": "s1", "p": "refactor"}


def test_a_turn_row_without_a_record_name_is_skipped(tmp_path, monkeypatch):
    from devmem import pack as P
    project = tmp_path / "p"
    project.mkdir()
    (project / "engine.py").write_text("def engine_loop():\n    return 0\n", encoding="utf-8")
    store = ensure_store(project / ".devmemory")
    real = Index.search_terms

    def with_a_bare_row(self, kind, terms, limit=20):
        rows = real(self, kind, terms, limit)
        if kind == "turn":
            rows.append({"key": "x#0", "score": 9.0, "matched": list(terms), "meta": {}})
        return rows
    monkeypatch.setattr(Index, "search_terms", with_a_bare_row)
    text, info = P.build_pack(project, store, "fix the engine_loop in engine.py")
    assert "engine.py" in text
