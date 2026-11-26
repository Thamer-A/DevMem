"""Sessions and turns in the index; turn-level retrieval; the asking session and what it was already given."""
from __future__ import annotations

from devmem.index import Index
from devmem.pack import build_pack, sync_session_index

from pack_helpers import setup, turn, write_live, write_session


def test_session_and_turn_keys(tmp_path):
    project, store, recs = setup(tmp_path)
    build_pack(project, store, "fix the shop")
    with Index(store) as idx:
        assert idx.keys("session") == {r["_meta"] for r in recs.values()}
        two = recs["two"]["_meta"]
        assert {f"{two}#0", f"{two}#1"} <= idx.keys("turn")
        assert len(idx.keys("turn")) == 4


def test_forgotten_session_leaves_the_index(tmp_path):
    project, store, recs = setup(tmp_path)
    sync_session_index(store)
    meta = store / "sessions" / recs["blades"]["_meta"]
    meta.unlink()
    meta.with_suffix("").with_suffix(".jsonl").unlink(missing_ok=True)
    sync_session_index(store)
    with Index(store) as idx:
        assert recs["blades"]["_meta"] not in idx.keys("session")
        assert not [k for k in idx.keys("turn") if k.startswith(recs["blades"]["_meta"])]
        assert idx.search("turn", "orbit radius studs") == []
    text, info = build_pack(project, store, "the orbit radius feels wrong")
    assert "bbbb2222" not in text


def test_sync_is_incremental(tmp_path):
    project, store, recs = setup(tmp_path)
    sync_session_index(store)
    from devmem.pack import _sync_sessions
    with Index(store) as idx:
        assert _sync_sessions(store, idx)["skipped"] is True
        write_session(store, "dddd4444-0000-0000-0000-000000000004", "2026-11-03T10:00:00Z",
                      [turn("Add a graveyard arena with torches.")])
        res = _sync_sessions(store, idx)
        assert res["changed"] == 1 and res["removed"] == 0


def test_later_turn_is_retrieved_and_cited(tmp_path):
    """The request that matters is in turn 2 of the session; the pack says so and quotes it."""
    project, store, recs = setup(tmp_path)
    text, info = build_pack(project, store, "the zombie death sound is too loud")
    s = [x for x in info["sessions"] if x["session"] == recs["two"]["session_id"]]
    assert s and s[0]["turn"] == 1
    assert "turn 2 of 2" in text and "volume 0.4 exactly" in text
    assert "session cccc3333" in text


def test_asking_session_is_not_cited(tmp_path):
    project, store, recs = setup(tmp_path)
    sid = recs["shop"]["session_id"]
    _, info = build_pack(project, store, "make rerolling the shop offers cost more")
    assert any(x["session"] == sid for x in info["sessions"])
    text, info = build_pack(project, store, "make rerolling the shop offers cost more", session_id=sid)
    assert all(x["session"] != sid for x in info["sessions"])
    assert "aaaa1111" not in text


def test_live_session_is_indexed(tmp_path):
    project, store, _ = setup(tmp_path)
    write_live(store, "eeee5555-0000-0000-0000-000000000005", ["Add a graveyard arena with torches and fog."])
    text, info = build_pack(project, store, "the graveyard fog is too thick")
    assert "session eeee5555" in text
    # ... but not to itself
    text, _ = build_pack(project, store, "the graveyard fog is too thick",
                         session_id="eeee5555-0000-0000-0000-000000000005")
    assert "session eeee5555" not in text


def test_already_delivered_items_are_not_resent(tmp_path):
    project, store, recs = setup(tmp_path)
    task = "make rerolling the shop offers cost more"
    text1, info1 = build_pack(project, store, task, session_id="zzzz")
    assert info1["files"] and info1["sessions"]
    delivered = [{"turn": 0, "files": info1["files"], "sessions": [s["session"] for s in info1["sessions"]],
                  "keys": info1["keys"]}]
    text2, info2 = build_pack(project, store, task, session_id="zzzz", delivered=delivered)
    assert text2 == "" and info2["empty_reason"].startswith("everything relevant")
    # old delivery records without "keys" still work (files and sessions lists)
    text3, _ = build_pack(project, store, task, session_id="zzzz",
                          delivered=[{"files": info1["files"], "sessions": [s["session"] for s in info1["sessions"]]}])
    assert "aaaa1111" not in text3


def test_conventions_once_per_session(tmp_path):
    project, store, _ = setup(tmp_path)
    text1, info1 = build_pack(project, store, "change the wave size constant in the config", session_id="s1")
    assert "conventions" in info1["keys"] and "## Conventions" in text1
    text2, _ = build_pack(project, store, "change the blade count constant in the config", session_id="s1",
                          delivered=[{"keys": ["conventions"]}])
    assert "## Conventions" not in text2


def test_requirement_from_another_turn_is_carried(tmp_path):
    project, store, _ = setup(tmp_path)
    write_session(store, "ffff6666-0000-0000-0000-000000000006", "2026-11-04T10:00:00Z", [
        turn("Add a boss zombie that spawns every 5 waves.", "Added."),
        turn("The boss zombie must have exactly 500 health and drop 40 coins.", "Done.")])
    text, info = build_pack(project, store, "the boss zombie health is wrong")
    assert "must have exactly 500 health" in text
