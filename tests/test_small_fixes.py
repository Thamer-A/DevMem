"""Item 11: a damaged vector blob is skipped (embed._unpack), and a UTF-8 byte order mark in front of the devmemory
begin marker does not hide it (connect._find_block; str.strip() does not remove U+FEFF)."""
from __future__ import annotations

import json

import pytest
from test_connect import do_connect

from devmem import connect as C
from devmem import embed
from devmem.connect import ConnectError

BOM = chr(0xFEFF)


def test_unpack_skips_a_blob_whose_length_is_not_a_multiple_of_4():
    assert embed._unpack(b"\x00\x00\x80\x3f") == [1.0]
    assert embed._unpack(b"\x00\x00\x80") is None
    assert embed._unpack("not bytes") is None


def test_a_damaged_row_does_not_break_reading_the_vectors(tmp_path):
    store = tmp_path / "store"
    (store / "index").mkdir(parents=True)
    emb = embed.FakeEmbedder()
    cache = embed.VectorCache(store, emb)
    cache.conn.execute("INSERT INTO vec VALUES (?,?,?,?,?,?)", (emb.name, "file", "a.py", "h1", "{}", b"\x00\x00\x80\x3f"))
    cache.conn.execute("INSERT INTO vec VALUES (?,?,?,?,?,?)", (emb.name, "file", "b.py", "h2", "{}", b"\x01\x02\x03"))
    try:
        assert set(cache.all("file")) == {"a.py"}
    finally:
        cache.close()


def test_a_bom_before_the_begin_marker_does_not_hide_the_block(tmp_path):
    begin, _p, end = C.MARKERS["md"]
    project = tmp_path / "proj"
    project.mkdir()
    agents = project / "AGENTS.md"
    agents.write_text(f"{BOM}{begin}\nold text\n{end}\nbottom\n", encoding="utf-8")
    raw = agents.read_bytes()
    # FG-2: "old text" was not written by devmem, so it is refused (the block was found, not hidden by the BOM)
    with pytest.raises(ConnectError, match="did not write"):
        do_connect(project, "cursor")
    assert agents.read_bytes() == raw
    do_connect(project, "cursor", force=True)
    text = agents.read_text(encoding="utf-8")
    assert text.count(begin) == 1 and "old text" not in text and text.startswith(BOM + begin), text[:80]
    assert text.endswith(end + "\nbottom\n")
    rec = json.loads((project / ".devmemory" / "connect.json").read_text(encoding="utf-8"))["written"]["AGENTS.md"]
    lines = agents.read_bytes().decode("utf-8").splitlines(keepends=True)     # as written (CRLF on Windows)
    assert rec["block_sha256"] == C.block_sha(lines, C._find_block(agents, lines, "md"))   # the BOM is not hashed in


def test_a_bom_before_devmems_own_block_still_counts_as_devmems(tmp_path):
    begin, _p, _end = C.MARKERS["md"]
    project = tmp_path / "proj"
    project.mkdir()
    agents = project / "AGENTS.md"
    body = "".join(C._block_lines(C.INSTRUCTIONS, "md", "\n"))
    agents.write_text(f"{BOM}{body}bottom\n", encoding="utf-8")     # never connected, block identical to this version's
    do_connect(project, "cursor")
    text = agents.read_text(encoding="utf-8")
    assert text == BOM + body + "bottom\n" and text.count(begin) == 1, text[:80]
