"""devmem.index.Index: incremental sync counts, search ranking, documents, robustness."""
import os
import sqlite3
import time
from pathlib import Path

import pytest

import devmem.store as store_mod
from devmem.index import Index


def write(root: Path, rel: str, text: str, age: float = 100.0) -> Path:
    """Write a file and backdate its mtime (so it is outside the 'recently modified' re-hash window)."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    t = time.time() - age
    os.utime(p, (t, t))
    return p


@pytest.fixture(autouse=True)
def no_git(monkeypatch):
    # these tests exercise the index, not git; listing falls back to the directory walk
    monkeypatch.setattr(store_mod, "_git_list", lambda project: None)


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    write(root, "app/inventory.py", "def add_item(bag, item):\n    bag.append(item)\n")
    write(root, "app/shop.py", "from app.inventory import add_item\n\ndef buy(bag):\n    add_item(bag, 'sword')\n")
    write(root, "README.md", "# Demo\nA shop that sells swords.\n")
    return root


def test_sync_counts_add_change_remove_unchanged(proj, tmp_path):
    idx = Index(tmp_path / "store")
    c = idx.sync(proj)
    assert (c["added"], c["changed"], c["removed"], c["unchanged"]) == (3, 0, 0, 0)
    assert c["seconds"] >= 0 and c["files"] == 3

    c = idx.sync(proj)
    assert (c["added"], c["changed"], c["removed"], c["unchanged"]) == (0, 0, 0, 3)
    assert c["hashed"] == 0, "unchanged files must be skipped by stat, not read"

    write(proj, "app/shop.py", "def buy(bag):\n    return None\n", age=50)    # changed
    write(proj, "app/ledger.py", "def balance():\n    return 0\n")              # added
    (proj / "README.md").unlink()                                              # removed
    c = idx.sync(proj)
    assert (c["added"], c["changed"], c["removed"], c["unchanged"]) == (1, 1, 1, 1)
    assert set(idx.files()) == {"app/inventory.py", "app/shop.py", "app/ledger.py"}
    assert idx.files()["app/shop.py"]["deps"] == []          # re-extracted from the new content
    assert idx.search("file", "demo swords") == []           # the removed file's postings are gone
    idx.close()


def test_touch_without_content_change_is_confirmed_by_hash(proj, tmp_path):
    idx = Index(tmp_path / "store")
    idx.sync(proj)
    p = proj / "app" / "inventory.py"
    t = time.time() - 30
    os.utime(p, (t, t))
    c = idx.sync(proj)
    assert c["hashed"] == 1 and c["changed"] == 0 and c["unchanged"] == 3
    c = idx.sync(proj)
    assert c["hashed"] == 0
    idx.close()


def test_crlf_only_change_is_not_a_change(proj, tmp_path):
    idx = Index(tmp_path / "store")
    idx.sync(proj)
    p = proj / "app" / "inventory.py"
    data = p.read_bytes()
    lf = data.replace(b"\r\n", b"\n")
    p.write_bytes(lf if data != lf else lf.replace(b"\n", b"\r\n"))   # flip line endings either way
    assert p.read_bytes() != data
    t = time.time() - 20
    os.utime(p, (t, t))
    c = idx.sync(proj)
    assert c["changed"] == 0
    idx.close()


def test_file_entries_have_map_shape(proj, tmp_path):
    with Index(tmp_path / "store") as idx:
        idx.sync(proj)
        info = idx.files()["app/shop.py"]
    for key in ("lang", "module", "symbols", "deps", "hash", "lines", "bytes"):
        assert key in info
    assert info["lang"] == "python" and info["module"] == "shop" and info["symbols"] == ["buy"]
    assert "app.inventory" in info["deps"] and "app.inventory.add_item" in info["deps"]


def test_search_ranks_path_and_symbols_above_body(tmp_path):
    root = tmp_path / "p"
    write(root, "src/inventory.py", "def stack(a):\n    return a\n")
    write(root, "src/notes.py", "x = 1  # the inventory is mentioned once in a long body " + "word " * 40 + "\n")
    write(root, "src/other.py", "def unrelated():\n    pass\n")
    with Index(tmp_path / "store") as idx:
        idx.sync(root)
        res = idx.search("file", "inventory")
        assert [r["key"] for r in res][:2] == ["src/inventory.py", "src/notes.py"]
        assert res[0]["matched"] == ["inventory"] and res[0]["score"] > res[1]["score"]
        assert res[0]["meta"]["lang"] == "python"
        sym = idx.search("file", "stack")
        assert sym[0]["key"] == "src/inventory.py"
        assert idx.search("file", "the and of") == []          # stop words only
        assert idx.search("file", "zzzunknown") == []


def test_search_is_deterministic_on_ties(tmp_path):
    root = tmp_path / "p"
    for name in ("b.py", "a.py", "c.py"):
        write(root, name, "value = 1\n")
    with Index(tmp_path / "store") as idx:
        idx.sync(root)
        assert [r["key"] for r in idx.search("file", "value")] == ["a.py", "b.py", "c.py"]


def test_session_and_turn_docs(tmp_path):
    with Index(tmp_path / "store") as idx:
        assert idx.upsert_doc("session", "s1", {"prompt": "add a shop reroll button", "body": "edited ShopUi"},
                              meta={"started_at": "2026-11-13"})
        assert idx.upsert_doc("session", "s2", {"prompt": "fix the leaderboard sorting"})
        assert idx.upsert_doc("turn", "s1#0", {"prompt": "أضف زر إعادة السحب في المتجر"})
        r = idx.search("session", "reroll shop")
        assert r[0]["key"] == "s1" and r[0]["meta"] == {"started_at": "2026-11-13"}
        assert set(r[0]["matched"]) == {"reroll", "shop"}
        assert idx.search("turn", "المتجر")[0]["key"] == "s1#0"   # Arabic query, Arabic doc
        assert idx.search("file", "shop") == []                   # kinds are separate
        # replace, then delete
        idx.upsert_doc("session", "s1", {"prompt": "rename the coins label"})
        assert idx.search("session", "reroll") == []
        assert idx.keys("session") == {"s1", "s2"} and idx.count("session") == 2
        assert idx.delete_doc("session", "s2")
        assert idx.keys("session") == {"s1"}
        assert idx.idf("session", "zzz") > idx.idf("session", "coin")
        assert idx.idf(["session", "turn"], "coin") > 0
        with pytest.raises(ValueError):
            idx.search("nonsense", "x")


def test_corrupted_db_is_rebuilt(proj, tmp_path):
    store = tmp_path / "store"
    with Index(store) as idx:
        idx.sync(proj)
    db = store / "index" / "index.db"
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    db.write_bytes(b"this is not a sqlite database" * 100)
    with Index(store) as idx:
        assert idx.rebuilt and not idx.degraded
        c = idx.sync(proj)
        assert c["added"] == 3
        assert idx.search("file", "inventory")[0]["key"] == "app/inventory.py"


def test_truncated_db_is_rebuilt(proj, tmp_path):
    store = tmp_path / "store"
    with Index(store) as idx:
        idx.sync(proj)
        idx.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db = store / "index" / "index.db"
    data = db.read_bytes()
    db.write_bytes(data[: len(data) // 2] + b"\x00garbage" * 64)
    with Index(store) as idx:
        c = idx.sync(proj)      # must not raise
        assert c["files"] == 3
        assert idx.search("file", "inventory")[0]["key"] == "app/inventory.py"


def test_version_mismatch_rebuilds(proj, tmp_path):
    store = tmp_path / "store"
    with Index(store) as idx:
        idx.sync(proj)
        idx.meta_set("version", "old-format")
    with Index(store) as idx:
        assert idx.rebuilt
        assert idx.sync(proj)["added"] == 3


def test_locked_db_does_not_crash(proj, tmp_path):
    store = tmp_path / "store"
    with Index(store) as idx:
        idx.sync(proj)
    write(proj, "app/new.py", "def fresh():\n    pass\n")
    blocker = sqlite3.connect(str(store / "index" / "index.db"), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")        # another process holds the write lock
    try:
        idx = Index(store, timeout=0.2)
        c = idx.sync(proj)
        assert c.get("error") == "locked"
        assert idx.upsert_doc("session", "s", {"prompt": "x y"}) is False
        assert idx.search("file", "inventory")[0]["key"] == "app/inventory.py"   # reads still work (WAL)
        idx.close()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    with Index(store) as idx:
        assert idx.sync(proj)["added"] == 1


def test_two_connections_interleave(proj, tmp_path):
    store = tmp_path / "store"
    a, b = Index(store), Index(store)
    a.sync(proj)
    assert b.upsert_doc("session", "s1", {"prompt": "hello inventory"})
    write(proj, "app/x.py", "def x():\n    pass\n")
    assert a.sync(proj)["added"] == 1
    assert b.sync(proj)["unchanged"] == 4
    assert a.search("session", "inventory")[0]["key"] == "s1"
    a.close()
    b.close()


def test_store_inside_project_is_not_indexed(proj):
    store = proj / "memory-store"
    with Index(store) as idx:
        idx.sync(proj)
        idx.sync(proj)
        assert not [r for r in idx.files() if r.startswith("memory-store")]


def test_binary_file_with_text_extension_is_skipped(proj, tmp_path):
    write(proj, "data.json", "")
    (proj / "data.json").write_bytes(b"\x00\x01\x02binary")
    with Index(tmp_path / "store") as idx:
        idx.sync(proj)
        assert "data.json" not in idx.files()


def test_size_cap(proj, tmp_path):
    write(proj, "big.txt", "x" * (store_mod.MAX_TEXT_BYTES + 10))
    with Index(tmp_path / "store") as idx:
        idx.sync(proj)
        assert "big.txt" not in idx.files()
