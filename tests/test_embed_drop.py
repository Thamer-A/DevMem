"""Stale cached vectors are dropped in one transaction and a bounded number per call; a locked index never makes
every cached turn vector look stale. Before: one autocommitted delete per row, 5000 stale vectors made `pack`
take 20 s; and Index.keys() returned an empty set on a lock, which rank_for_pack read as "every turn is gone"."""
from __future__ import annotations

import json
import time

from devmem import embed
from devmem.index import Index
from devmem.store import ensure_store


def _filled(tmp_path, kind: str, n: int):
    store = ensure_store(tmp_path / ".devmemory")
    cache = embed.VectorCache(store, embed.FakeEmbedder())
    v = embed._pack([0.0] * 64)
    cache.conn.execute("BEGIN")
    cache.conn.executemany("INSERT INTO vec(model, kind, key, h, meta, v) VALUES (?,?,?,?,?,?)",
                           [(cache.emb.name, kind, f"k{i}", "h", "{}", v) for i in range(n)])
    cache.conn.execute("COMMIT")
    return store, cache


def test_thousands_of_stale_vectors_are_dropped_quickly_and_in_bounded_steps(tmp_path):
    _store, cache = _filled(tmp_path, "file", 5000)
    try:
        t = time.perf_counter()
        assert cache.drop("file", [f"k{i}" for i in range(5000)]) == embed.DROP_MAX
        took = time.perf_counter() - t
        assert took < 2.0, f"{took:.2f}s (one transaction; 20 s before for 5000 rows)"
        assert len(cache.all("file")) == 5000 - embed.DROP_MAX
        while cache.drop("file", list(cache.all("file"))):
            pass
        assert cache.all("file") == {}
    finally:
        cache.close()


def test_a_locked_index_does_not_drop_cached_turn_vectors(tmp_path, monkeypatch):
    project = tmp_path
    store, cache = _filled(tmp_path, "turn", 10)
    cache.close()
    (store / "config.json").write_text(json.dumps({"embeddings": {"backend": "fake"}}), encoding="utf-8")
    embed._EMBEDDERS.clear()
    monkeypatch.delenv(embed.ENV, raising=False)
    with Index(store) as idx:
        monkeypatch.setattr(idx, "keys", lambda kind, strict=False: None if strict else set())
        embed.rank_for_pack(project, store, "anything at all", idx, {}, with_sessions=True)
    cache = embed.VectorCache(store, embed.FakeEmbedder())
    try:
        assert len(cache.all("turn")) == 10
    finally:
        cache.close()
