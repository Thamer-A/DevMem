"""The session index under a deadline (the hook's and MCP's pack step) never reads every record: fingerprints come from
one listing and a stat per file, only changed records are loaded (one at a time, newest first) until the deadline, and
the rest is picked up by later packs. Before: load_sessions read every record whenever any one changed (5,000 small
records: 100 s; 50 records of 5 MB: a 4 s-deadline pack took 31 s)."""
from __future__ import annotations

import json
import os
import time

import pytest
from pack_helpers import setup, turn, write_live, write_session

from devmem import pack as P
from devmem.index import Index
from devmem.sessions import load_sessions


def _many(store, n: int, turns: int = 1, pad: int = 50) -> None:
    folder = store / "sessions"
    for i in range(n):
        sid = f"{i:08d}-0000-0000-0000-000000000000"
        rec = {"schema": 2, "session_id": sid, "provider": "claude-code", "capture_method": "hooks",
               "started_at": "2026-11-01T10:00:00Z",
               "turns": [{"index": t, "prompt": f"session {i} turn {t} reroll the shop offers " + "x" * pad,
                          "result_text": "done", "tool_calls": [], "files_edited": [],
                          "started_at": "2026-11-01T10:00:00Z"} for t in range(turns)]}
        (folder / f"20261101-10{i:06d}_{sid[:8]}.meta.json").write_text(json.dumps(rec), encoding="utf-8")


def test_one_record_is_loaded_per_changed_record_not_all(tmp_path, monkeypatch):
    project, store, _recs = setup(tmp_path, with_sessions=False)
    _many(store, 300)
    with Index(store) as idx:
        P._sync_sessions(store, idx)
        assert idx.count("session") == 300
        loads = []
        real = P._load_one
        monkeypatch.setattr(P, "_load_one", lambda s, k: loads.append(k) or real(s, k))
        assert P._sync_sessions(store, idx, deadline=time.monotonic() + 30)["skipped"] is True
        p = sorted((store / "sessions").glob("*.meta.json"))[10]
        rec = json.loads(p.read_text(encoding="utf-8"))
        rec["turns"][0]["prompt"] = "a changed request about the reroll price"
        p.write_text(json.dumps(rec), encoding="utf-8")
        out = P._sync_sessions(store, idx, deadline=time.monotonic() + 30)
        assert loads == [p.name] and out["indexed"] == 1
        assert [r["key"] for r in idx.search("session", "changed")] == [p.name]


def _check_many(tmp_path, n: int) -> None:
    project, store, _recs = setup(tmp_path, with_sessions=False)
    _many(store, n)
    t = time.perf_counter()
    _text, _info = P.build_pack(project, store, "reroll the shop offers", deadline=time.monotonic() + 1.0)
    assert time.perf_counter() - t < 1.0 + 5.0
    with Index(store) as idx:
        first = idx.count("session")
        assert first > 0          # on a fast machine all of them may fit in the second; the rest goes on below
        P._sync_sessions(store, idx, deadline=time.monotonic() + 0.5)        # a later pack goes on
        assert idx.count("session") > first or first == n
        P._sync_sessions(store, idx)                                          # (the CLI: everything, batched)
        assert idx.count("session") == n
        assert P._sync_sessions(store, idx, deadline=time.monotonic() + 1.0)["skipped"] is True


def test_many_records_cost_one_deadline_and_later_packs_finish_the_rest(tmp_path):
    _check_many(tmp_path, 800)


@pytest.mark.stress
def test_five_thousand_records_cost_one_deadline(tmp_path):
    _check_many(tmp_path, 5000)


def test_a_few_huge_records_cost_at_most_one_chunk_past_the_deadline(tmp_path):
    project, store, _recs = setup(tmp_path, with_sessions=False)
    _many(store, 4, turns=200, pad=25_000)          # about 5 MB each
    assert min(p.stat().st_size for p in (store / "sessions").glob("*.meta.json")) > 4_000_000
    with Index(store) as idx:
        t = time.perf_counter()
        out = P._sync_sessions(store, idx, deadline=time.monotonic() - 1)     # already past: one chunk of one record
        assert out["indexed"] == 0 and out["partial"] is True
        assert idx.count("session") == 1 and idx.count("turn") == P.DOC_CHUNK - 1    # its session document first
        assert time.perf_counter() - t < 15.0
        for _ in range(4 * 200 // (P.DOC_CHUNK - 1) + 8):        # later calls go on where it stopped
            if P._sync_sessions(store, idx, deadline=time.monotonic() - 1).get("skipped"):
                break
        assert idx.count("session") == 4 and idx.count("turn") == 800
    t = time.perf_counter()
    P.build_pack(project, store, "reroll the shop offers", deadline=time.monotonic() + 1.0)
    assert time.perf_counter() - t < 1.0 + 15.0


def test_one_record_loads_exactly_as_the_full_loader_loads_it(tmp_path):
    project, store, _recs = setup(tmp_path)          # three records
    write_live(store, "dddd4444-0000-0000-0000-000000000004", ["a live session with no record yet"])
    rec = write_session(store, "eeee5555-0000-0000-0000-000000000005", "2026-11-01T13:00:00Z",
                        [turn("an older record whose live log is newer")])
    log = write_live(store, "eeee5555-0000-0000-0000-000000000005", ["the newer live prompt"])
    later = os.stat(store / "sessions" / rec["_meta"]).st_mtime + 10
    os.utime(log, (later, later))
    rec2 = write_session(store, "ffff6666-0000-0000-0000-000000000006", "2026-11-01T14:00:00Z",
                         [turn("a record newer than its live log")])
    log2 = write_live(store, "ffff6666-0000-0000-0000-000000000006", ["old live prompt"])
    earlier = os.stat(store / "sessions" / rec2["_meta"]).st_mtime - 10
    os.utime(log2, (earlier, earlier))
    full = {r["_meta"]: r for r in load_sessions(store)}
    current, own = P._session_files(store, None)
    assert set(current) == set(full) and own == set()
    for key, r in full.items():
        assert P._load_one(store, key) == r, key
    assert full[rec["_meta"]]["turns"][0]["prompt"] == "the newer live prompt"


def test_the_asking_sessions_files_are_left_alone(tmp_path):
    project, store, recs = setup(tmp_path)
    sid = recs["shop"]["session_id"]
    with Index(store) as idx:
        P._sync_sessions(store, idx)
        n = idx.count("session")
        write_live(store, sid, ["the asking session keeps writing"])
        out = P._sync_sessions(store, idx, skip_sid=sid)
        assert out["removed"] == 0 and idx.count("session") == n


def test_a_record_that_vanishes_or_breaks_after_the_listing_is_dropped_not_retried(tmp_path):
    project, store, recs = setup(tmp_path)
    with Index(store) as idx:
        P._sync_sessions(store, idx)
        p = store / "sessions" / recs["blades"]["_meta"]
        p.write_text("{ not json", encoding="utf-8")
        P._sync_sessions(store, idx)
        assert recs["blades"]["_meta"] not in idx.keys("session")
        assert P._sync_sessions(store, idx)["skipped"] is True


def _append_prompt(log, sid: str, prompt: str) -> None:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "UserPromptSubmit", "ts": "2026-11-02T10:00:00.000Z", "session_id": sid,
                            "prompt": prompt}) + "\n")


def _count_writes(monkeypatch) -> list:
    written: list = []
    real = Index.upsert_docs

    def counting(self, kind, docs, weights=None):
        written.extend((kind, d[0]) for d in docs)
        return real(self, kind, docs, weights)
    monkeypatch.setattr(Index, "upsert_docs", counting)
    return written


def test_many_live_logs_and_one_growing_large_one_cost_their_new_turns(tmp_path, monkeypatch):
    """Another running session's live log is re-read on every prompt of this one. Before, every turn of it was
    tokenised again and every posting deleted and re-inserted (a 7 MB log: 18-31 s per prompt with a 4 s deadline).
    Now only its new turns and its session document are written, and each call stops at its deadline."""
    import random
    import string
    project, store, _recs = setup(tmp_path, with_sessions=False)
    rnd = random.Random(7)
    words = ["".join(rnd.choice(string.ascii_lowercase) for _ in range(rnd.randint(4, 9))) for _ in range(5000)]
    for i in range(750):
        write_live(store, f"{i:08d}-1111-0000-0000-000000000000", [f"small session {i} shop offers"])
    big_sid = "ffffffff-2222-0000-0000-000000000000"
    big = write_live(store, big_sid, [" ".join(rnd.choice(words) for _ in range(400)) for _ in range(300)])
    calls = 0
    with Index(store) as idx:
        while True:
            t = time.perf_counter()
            out = P._sync_sessions(store, idx, deadline=time.monotonic() + 1.0)
            assert time.perf_counter() - t < 1.0 + 5.0, out          # one deadline plus at most a chunk and a load
            calls += 1
            if out.get("skipped") or calls > 200:
                break
        assert idx.count("session") == 751 and idx.count("turn") == 750 + 300
        written = _count_writes(monkeypatch)
        _append_prompt(big, big_sid, "a new request about the reroll price")
        _append_prompt(store / "live" / "00000005-1111-0000-0000-000000000000.jsonl",
                       "00000005-1111-0000-0000-000000000000", "and one more small one")
        out = P._sync_sessions(store, idx, deadline=time.monotonic() + 30)
        assert out["indexed"] == 2 and not out.get("partial")
        # two session documents and the two new turns: not the 300 old turns of the big log again
        assert sorted(k for k, _ in written) == ["session", "session", "turn", "turn"], written[:10]
        assert idx.count("turn") == 750 + 300 + 2
        assert [r["key"] for r in idx.search("turn", "reroll price")][0].startswith(f"live/{big_sid}.jsonl#")
