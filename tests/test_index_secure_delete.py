"""Deleted documents leave no bytes behind in any file under index/ (main db, -wal, -shm), in any encoding."""
from __future__ import annotations

import sqlite3
import time

from devmem.index import Index
from devmem.text import tokenize

TERM = "zorblaxquux"
PHRASE = f"please remember the {TERM} frobnicating widgets policy"


def raw_hits(store, needles) -> list[str]:
    hits = []
    for f in (store / "index").rglob("*"):
        if f.is_file():
            data = f.read_bytes()
            for n in needles:
                if n.encode("utf-8") in data or n.encode("utf-16-le") in data:
                    hits.append(f"{f.name}:{n}")
    return hits


def fill(idx):
    idx.upsert_doc("turn", "s1#0", {"prompt": PHRASE}, {"p": PHRASE})
    for i in range(40):
        idx.upsert_doc("turn", f"o{i}#0", {"prompt": f"other unrelated filler number {i} about caching"}, {"p": f"filler {i}"})


def test_deleted_text_is_gone_from_every_index_file(tmp_path):
    store = tmp_path / ".devmemory"
    stems = {t for t in tokenize(PHRASE) if "zorbl" in t}
    assert stems
    idx = Index(store)
    fill(idx)
    idx.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    assert raw_hits(store, [TERM, PHRASE]), "the test should see the text before the delete"
    idx.delete_doc("turn", "s1#0")
    idx.close()
    assert raw_hits(store, [TERM, PHRASE, *stems]) == []
    con = sqlite3.connect(store / "index" / "index.db")
    assert con.execute("SELECT COUNT(*) FROM postings WHERE term IN (%s)" % ",".join("?" * len(stems)), list(stems)).fetchone()[0] == 0
    con.close()


def test_open_connection_scrubs_the_wal_too(tmp_path):
    store = tmp_path / ".devmemory"
    idx = Index(store)
    fill(idx)
    idx.delete_doc("turn", "s1#0")
    idx.scrub()                      # while the connection is still open: the -wal file must not hold the text
    assert raw_hits(store, [TERM]) == []
    idx.close()


def test_index_written_before_secure_delete_is_vacuumed_once(tmp_path):
    store = tmp_path / ".devmemory"
    idx = Index(store)
    idx.conn.execute("PRAGMA secure_delete=OFF")   # as an old version wrote it
    idx.conn.execute("DELETE FROM meta WHERE key='secure_delete'")
    fill(idx)
    idx.conn.execute("PRAGMA secure_delete=OFF")
    idx.delete_doc("turn", "s1#0")
    assert idx.scrub() is True   # the explicit forget-path scrub does the one-time VACUUM
    idx.close()
    assert raw_hits(store, [TERM]) == []
    with Index(store) as again:
        assert again.meta_get("secure_delete") == "1"


def test_routine_delete_never_waits_for_a_reader_but_forget_scrub_is_bounded(tmp_path):
    store = tmp_path / ".devmemory"
    idx = Index(store)
    fill(idx)
    reader = sqlite3.connect(store / "index" / "index.db", isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM docs").fetchone()
    t0 = time.perf_counter()
    idx.delete_doc("turn", "s1#0")
    idx.close()                                  # routine path: passive checkpoint, no wait
    assert time.perf_counter() - t0 < 1.5
    idx = Index(store)
    t0 = time.perf_counter()
    assert idx.scrub(timeout=0.5) is False       # bounded, and says it could not finish
    assert time.perf_counter() - t0 < 3
    idx.close()
    reader.execute("COMMIT")
    reader.close()
    with Index(store) as again:
        assert again.scrub() is True
    assert raw_hits(store, [TERM]) == []


def test_forget_says_so_when_the_index_could_not_be_scrubbed(tmp_path, monkeypatch, capsys):
    from devmem import cli
    store = tmp_path / ".devmemory"
    Index(store).close()
    monkeypatch.setattr(Index, "scrub", lambda self, timeout=3.0: False)
    cli._scrub_index(store)
    err = capsys.readouterr().err
    assert "may still be inside the search index files" in err


def test_embed_forget_keys_does_not_vacuum(tmp_path):
    from devmem import embed
    store = tmp_path / ".devmemory"
    (store / "index").mkdir(parents=True)
    con = embed._connect(store)
    con.execute("INSERT INTO vec VALUES ('m','turn','k#0','h','{}',x'00')")
    con.close()
    seen = []
    real = embed.sqlite3.connect

    class Spy:
        def __init__(self, c): self.c = c
        def execute(self, sql, *a):
            seen.append(sql)
            return self.c.execute(sql, *a)
        def close(self): self.c.close()
    monkey = embed.sqlite3
    monkey.connect = lambda *a, **k: Spy(real(*a, **k))
    try:
        embed.forget_keys(store, [("turn", "k")])
    finally:
        monkey.connect = real
    assert not any("VACUUM" in q.upper() for q in seen)
