"""freshness() cost: one pass over the session's calls, lines looked up in sets, bounded searches.

Before: a session with 300 Edits whose lines were no longer in the file took 20-23 s in freshness(), on every prompt
that retrieved it and in `devmem sessions` (the later calls and the join of their `old` texts were rebuilt for every
call, and every line was searched for in that join and in the file). The answers must stay what the old code gave;
the reference below is the old code.
"""
from __future__ import annotations

import random
import time
from pathlib import Path

import pytest

from devmem import pack as P
from devmem.store import hash_files


def _old_freshness(s: dict, project: Path) -> dict:
    calls = [c for c in (s.get("tool_calls") or []) if c.get("tool") in P.TEXT_TOOLS and c.get("file")
             and P._inside(c.get("file")) and c.get("ok", True) is not False and not c.get("secret_file")]
    deleted = set(s.get("files_deleted") or [])
    texts: dict = {}

    def current(f):
        if f not in texts:
            p = project / f
            texts[f] = p.read_text(encoding="utf-8") if p.is_file() else None
        return texts[f]
    per_file: dict = {}
    missing: dict = {}
    superseded: set = set()
    for i, c in enumerate(calls):
        f = c["file"]
        if f in deleted:
            superseded.add(f)
            continue
        later = [e for e in calls[i + 1:] if e["file"] == f]
        if any(e.get("tool") == "Write" for e in later):
            superseded.add(f)
            continue
        lines = P._edit_lines(c)
        if not lines:
            continue
        later_old = "\n".join(e.get("old") or "" for e in later)
        txt = current(f) or ""
        lines = [ln for ln in lines if ln in txt or ln not in later_old]
        if not lines:
            continue
        gone = [ln for ln in lines if ln not in txt]
        acc = per_file.setdefault(f, [0, 0])
        acc[0] += len(lines) - len(gone)
        acc[1] += len(lines)
        for ln in gone:
            lst = missing.setdefault(f, [])
            if ln not in lst:
                lst.append(ln)
    intact = sorted(f for f, (ok, n) in per_file.items() if ok == n)
    changed = sorted(f for f, (ok, n) in per_file.items() if ok < n)
    edited = set(s.get("files_edited") or [])
    changed_reads = sorted(f for f, h in (s.get("file_hashes_at_capture") or {}).items()
                           if f not in edited and h and hash_files(project, [f])[f] != h)
    if not per_file:
        status = "context-changed" if changed_reads else "fresh"
    elif not changed:
        status = "fresh"
    elif not intact and all(ok / n < 0.5 for ok, n in per_file.values()):
        status = "stale"
    else:
        status = "partly-stale"
    return {"status": status, "edits_gone_from": changed, "edits_present_in": intact,
            "read_files_changed": changed_reads, "missing": {f: missing[f][:20] for f in changed},
            "superseded": sorted(superseded - set(per_file))}


WORDS = ["alpha_value", "beta = compute(x)", "return gamma + delta", "print('status ok')", "self.speed = 9001",
         "if player.health <= 0:", "for item in inventory:", "local Coins = 10"]


def _line(rng: random.Random) -> str:
    return "    " * rng.randint(0, 2) + rng.choice(WORDS) + (f"  # n{rng.randint(0, 9)}" if rng.random() < 0.5 else "")


@pytest.mark.parametrize("seed", range(25))
def test_freshness_matches_the_old_answers(tmp_path, seed):
    rng = random.Random(seed)
    files = ["a.py", "b.py", "src/c.luau"]
    for f in files:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("\n".join(_line(rng) for _ in range(12)) + "\n", encoding="utf-8")
    calls = []
    for _ in range(rng.randint(1, 14)):
        f = rng.choice(files + ["gone.py"])
        new = "\n".join(_line(rng) for _ in range(rng.randint(1, 4)))
        if rng.random() < 0.3:                       # a partial first line: part of a longer line in the file
            new = rng.choice(WORDS)[3:] + "\n" + new
        calls.append({"tool": rng.choice(["Edit", "Edit", "Edit", "Write", "MultiEdit"]), "file": f, "new": new,
                      "old": "\n".join(_line(rng) for _ in range(rng.randint(0, 3))),
                      "truncated": rng.random() < 0.1, "ok": rng.random() > 0.1})
    s = {"tool_calls": calls, "files_deleted": ["b.py"] if rng.random() < 0.2 else [], "files_edited": files}
    assert P.freshness(s, tmp_path) == _old_freshness(s, tmp_path)


def test_a_session_with_hundreds_of_vanished_edits_is_checked_quickly(tmp_path):
    rng = random.Random(1)
    (tmp_path / "engine.py").write_text("def engine_loop():\n    return 0\n", encoding="utf-8")
    calls = []
    for i in range(300):
        new = "\n".join(f"v{i:04d}_{j:03d}x = {rng.random():.6f}" for j in range(160))
        old = "".join(rng.choice("abcdefgh \n") for _ in range(4000))
        calls.append({"tool": "Edit", "file": "engine.py", "new": new, "old": old})
    s = {"tool_calls": calls, "files_edited": ["engine.py"]}
    t = time.perf_counter()
    out = P.freshness(s, tmp_path)
    took = time.perf_counter() - t
    assert out["status"] == "stale" and len(out["missing"]["engine.py"]) == 20
    assert took < 2.0, f"{took:.2f}s (about 0.05 s measured; 20 s before)"


def test_a_session_list_checks_freshness_for_a_bounded_time(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    s = {"tool_calls": [{"tool": "Edit", "file": "a.py", "new": "x = 1 and more text"}], "files_edited": ["a.py"]}
    assert P.freshness_statuses([s, {"tool_calls": "junk"}], tmp_path) == ["stale", "fresh"]
    assert P.freshness_statuses([s, s], tmp_path, seconds=-1) == ["unchecked", "unchecked"]
