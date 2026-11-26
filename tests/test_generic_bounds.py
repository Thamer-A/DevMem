"""`devmem begin` / `end` (and `wrap`, `codex`, MCP begin_session / end_session) do bounded work and write bounded files.

They used to read, hash and redact every changed file git listed (up to 40,000, each up to 512 KB) and write all of
that redacted text into one `pending/<id>.json`; `end` then refused a begin of more than MAX_BEGIN_FILES entries (or one
over the 64 MB it reads), started a `git show` per candidate without a total limit, ran a quadratic line diff on whole
files and wrote every edit and the whole unified diff. Now every step has a named bound (devmem.generic, above
_Budget), and past a bound the record lists the files it did not compare instead of guessing.

Most tests lower the bounds (monkeypatch) so that they run in seconds; the work they count (characters handed to the
redactor, bytes of the pending file and the record, seconds in `git show`) is measured where it happens. One test
runs at the real sizes (more dirty files than MAX_BEGIN_FILES)."""
from __future__ import annotations

import json
import shutil
import time

import pytest
from hooks_helpers import make_project, run_git

from devmem import generic as G
from devmem import redact
from devmem import sessions as S

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


class _Chars:
    """Characters handed to Redactor.text (the redaction work: 0.2-0.5 MB/s on the test machine)."""

    def __init__(self, monkeypatch):
        self.n = 0
        real = redact.Redactor.text

        def text(red, s, *a, **k):
            if isinstance(s, str):
                self.n += len(s)
            return real(red, s, *a, **k)
        monkeypatch.setattr(redact.Redactor, "text", text)


def _rec(meta):
    return json.loads(meta.read_text(encoding="utf-8"))


def _raw(meta):
    return meta.with_name(meta.name.replace(".meta.json", ".jsonl"))


def _small(monkeypatch, **over):
    """Bounds lowered for a test that must run in seconds (the same code paths as the real sizes)."""
    vals = dict(MAX_COMPARED_FILES=20, MAX_COMPARED_NAME_CHARS=10_000, MAX_REDACT_CHARS=60_000, MAX_READ_BYTES=400_000,
                MAX_PENDING_BYTES=300_000, MAX_UNKNOWN_IDS=40, MAX_BEGIN_FILES=100, MAX_RECORD_EDITS=30,
                MAX_EDIT_CHARS=40_000, MAX_UDIFF_CHARS=60_000, MAX_EVENTS_CHARS=20_000)
    vals.update(over)
    for k, v in vals.items():
        monkeypatch.setattr(G, k, v)
    return vals


def _lines(tag: str, n: int) -> str:
    return "".join(f"{tag} line {i} = {i * 7}\n" for i in range(n))


def _untracked(project, count: int, size_lines: int = 40, sub: str = "unpacked"):
    d = project / sub
    d.mkdir(exist_ok=True)
    for i in range(count):
        (d / f"f{i:04d}.txt").write_text(_lines(f"f{i}", size_lines), encoding="utf-8")
    return d


def _not_compared(t: dict) -> tuple[list[str], int]:
    return t.get("files_not_compared") or [], int(t.get("files_not_compared_more") or 0)


# ---- begin: what is read, redacted and written is bounded; end accepts what begin wrote ----

def test_begin_with_many_dirty_files_is_bounded_and_end_takes_it(tmp_path, monkeypatch):
    lim = _small(monkeypatch, MAX_UNKNOWN_IDS=1000)
    project = make_project(tmp_path)
    store = project / ".devmemory"
    _untracked(project, 300)                                   # 300 x ~1 KB untracked: 3x MAX_BEGIN_FILES
    chars = _Chars(monkeypatch)
    info: dict = {}
    p = G.begin(project, store, info=info)
    assert chars.n <= lim["MAX_REDACT_CHARS"]                   # old: every file's text (~300 KB)
    assert p.stat().st_size <= lim["MAX_PENDING_BYTES"]
    state = json.loads(p.read_text(encoding="utf-8"))
    assert G.validate_begin(state) is None                      # old: more than MAX_BEGIN_FILES entries -> refused
    full = sorted(r for r, e in state["dirty"].items() if isinstance(e, dict) and "text" in e)
    hashed = sorted(r for r, e in state["dirty"].items() if isinstance(e, dict) and set(e) == {"sha"})
    assert len(full) == lim["MAX_COMPARED_FILES"] and len(state["dirty"]) == lim["MAX_BEGIN_FILES"]
    assert len(state["unknown"]) == 16 * 200 and "unlisted" not in state
    assert info == {"kept": 20, "hashed": 80, "unknown": 200}
    assert "not kept in full" in G.begin_note(info)
    # the session edits one file kept in full, one kept by hash, one kept by name only, and creates one
    kept, by_hash, unknown = full[0], hashed[0], "unpacked/f0100.txt"
    assert unknown not in state["dirty"]
    for rel in (kept, by_hash, unknown):
        (project / rel).write_text((project / rel).read_text(encoding="utf-8") + "added\n", encoding="utf-8")
    (project / "unpacked" / "new.txt").write_text("brand new\n", encoding="utf-8")
    done: dict = {}
    rec = _rec(G.end(project, "edit three", store=store, info=done))
    t = rec["turns"][0]
    assert {c["file"] for c in rec["tool_calls"]} == {kept, "unpacked/new.txt"}
    assert next(c for c in rec["tool_calls"] if c["file"] == kept)["new"] == "added"
    # never shown as written by the session: the start of the other two was not kept
    assert by_hash not in rec["files_edited"] and unknown not in rec["files_edited"]
    names, more = _not_compared(t)
    assert by_hash in names and unknown in names and len(names) == S.STORED_PATHS
    assert len(names) + more == 1 + 200       # the hashed one, and every file kept by name only (git still lists them)
    assert t["not_compared_why"] == sorted([G.WHY_START_TEXT_NOT_KEPT, G.WHY_START_NOT_KEPT])
    assert "not compared" in G.end_note(done)


def test_a_begin_that_could_not_list_every_file_attributes_no_creation(tmp_path, monkeypatch):
    _small(monkeypatch)                                         # 100 entries, 40 names by hash, 160 not listed
    project = make_project(tmp_path)
    store = project / ".devmemory"
    _untracked(project, 300)
    p = G.begin(project, store)
    state = json.loads(p.read_text(encoding="utf-8"))
    assert state["unlisted"] == 160 and G.validate_begin(state) is None
    (project / "unpacked" / "new.txt").write_text("brand new\n", encoding="utf-8")
    (project / "greet.py").write_text("changed\n", encoding="utf-8")
    rec = _rec(G.end(project, "x", store=store))
    names, more = _not_compared(rec["turns"][0])
    # new.txt may be one the begin did not list; greet.py was clean, but the begin cannot say so either
    assert rec["files_edited"] == [] and rec["tool_calls"] == [] and "greet.py" in names
    assert len(names) + more == 40 + 160 + 2                    # the 200 not kept, new.txt and greet.py


def test_more_dirty_files_than_max_begin_files_at_real_sizes(tmp_path):
    """No lowered bound: 20,100 dirty files. The old begin wrote 20,100 entries and `end` refused the file."""
    project = make_project(tmp_path)
    store = project / ".devmemory"
    d = project / "many"
    d.mkdir()
    count = G.MAX_BEGIN_FILES + 100
    for i in range(count):
        (d / f"{i:05d}.txt").write_text(f"{i}", encoding="utf-8")
    t0 = time.monotonic()
    p = G.begin(project, store)
    state = json.loads(p.read_text(encoding="utf-8"))
    assert G.validate_begin(state) is None and len(state["dirty"]) == G.MAX_READ_FILES
    assert len(state["unknown"]) == 16 * (count - G.MAX_READ_FILES) and p.stat().st_size <= G.MAX_PENDING_BYTES
    (d / "00000.txt").write_text("changed", encoding="utf-8")           # kept in full at begin
    (d / f"{count - 1:05d}.txt").write_text("changed", encoding="utf-8")  # kept by name only
    rec = _rec(G.end(project, "two edits", store=store))
    assert rec["files_edited"] == ["many/00000.txt"]
    names, more = _not_compared(rec["turns"][0])
    assert len(names) == S.STORED_PATHS and len(names) + more == count - G.MAX_READ_FILES
    assert time.monotonic() - t0 < 600       # generous: 1-4 minutes on the Windows test machine (opening fresh files)


def test_status_too_large_is_recorded_as_such_not_snapshotted(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "MAX_STATUS_ENTRIES", 50)
    project = make_project(tmp_path)
    store = project / ".devmemory"
    _untracked(project, 120)
    chars = _Chars(monkeypatch)
    info: dict = {}
    p = G.begin(project, store, info=info)
    state = json.loads(p.read_text(encoding="utf-8"))
    assert state["kind"] == "git" and state["status_too_large"] is True and state["dirty"] == {}
    assert chars.n == 0 and p.stat().st_size < 1024             # old: a full-text snapshot of the whole tree
    assert G.validate_begin(state) is None and "more changed paths" in G.begin_note(info)
    monkeypatch.setattr(S, "MAX_STATUS_ENTRIES", 40_000)        # end sees the tree again
    (project / "greet.py").write_text("def greet(name):\n    return name\n", encoding="utf-8")
    rec = _rec(G.end(project, "x", store=store))
    t = rec["turns"][0]
    assert rec["files_edited"] == [] and rec["tool_calls"] == []   # nothing attributed: the start is unknown
    names, more = _not_compared(t)
    assert "greet.py" in names and len(names) + more == 121
    assert t["not_compared_why"] == [G.WHY_START_TOO_LARGE] and rec["git"]["status_too_large_start"] is True


def test_status_too_large_at_end_is_said(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    G.begin(project, store)
    _untracked(project, 120)
    (project / "greet.py").write_text("changed\n", encoding="utf-8")
    monkeypatch.setattr(S, "MAX_STATUS_ENTRIES", 50)
    rec = _rec(G.end(project, "x", store=store))
    assert G.WHY_END_TOO_LARGE in rec["turns"][0]["not_compared_why"] and rec["git"]["status_too_large_end"] is True


def test_a_git_status_that_fails_at_end_is_said(tmp_path, monkeypatch):
    """A `git status` that fails at end (or runs past its limit) made the old end compare only what begin kept, without
    a word. begin/end give it GIT_STATUS_TIMEOUT_S (a hook gives it 5 s), and a status that still fails is said."""
    project = make_project(tmp_path)
    store = project / ".devmemory"
    (project / "draft.txt").write_text("one\n", encoding="utf-8")
    G.begin(project, store)
    (project / "draft.txt").write_text("two\n", encoding="utf-8")
    asked = []
    monkeypatch.setattr(S, "git_status_sized", lambda *a, **k: asked.append(k) or (None, False))
    rec = _rec(G.end(project, "x", store=store))
    assert asked == [{"untracked": "all", "timeout": G.GIT_STATUS_TIMEOUT_S}]
    assert rec["files_edited"] == ["draft.txt"]                     # what begin kept is still compared
    assert rec["turns"][0]["not_compared_why"] == [G.WHY_END_STATUS_FAILED]


def test_snapshot_without_git_is_bounded(tmp_path, monkeypatch):
    lim = _small(monkeypatch)
    project = make_project(tmp_path, git=False)
    store = project / ".devmemory"
    _untracked(project, 300)
    chars = _Chars(monkeypatch)
    p = G.begin(project, store)
    assert chars.n <= lim["MAX_REDACT_CHARS"] and p.stat().st_size <= lim["MAX_PENDING_BYTES"]
    state = json.loads(p.read_text(encoding="utf-8"))
    assert state["kind"] == "snapshot" and G.validate_begin(state) is None and state["unlisted"] > 0
    first = sorted(state["snapshot"])[0]
    (project / first).write_text("rewritten\n", encoding="utf-8")
    (project / "created.txt").write_text("new\n", encoding="utf-8")   # the start listing is incomplete: unknown
    rec = _rec(G.end(project, "x", store=store))
    t = rec["turns"][0]
    assert rec["files_edited"] == [first]
    names, more = _not_compared(t)
    assert "created.txt" in names and "created.txt" not in rec["files_edited"]
    assert len(names) <= S.STORED_PATHS and more > 0


def test_snapshot_start_complete_still_records_a_created_file(tmp_path):
    project = make_project(tmp_path, git=False)
    store = project / ".devmemory"
    G.begin(project, store)
    (project / "created.txt").write_text("new\n", encoding="utf-8")
    rec = _rec(G.end(project, "x", store=store))
    assert [(c["tool"], c["file"]) for c in rec["tool_calls"]] == [("Write", "created.txt")]
    assert "files_not_compared" not in rec["turns"][0]


# ---- a file whose start could not be kept is never shown as written by the session ----

@pytest.mark.parametrize("git", [True, False])
def test_a_large_file_that_shrinks_is_not_a_creation(tmp_path, git):
    project = make_project(tmp_path, git=git)
    store = project / ".devmemory"
    big = "x = 1\n" * (G.MAX_TEXT_BYTES // 6 + 10)          # over MAX_TEXT_BYTES: its text is not read at begin
    (project / "big.txt").write_text(big, encoding="utf-8")
    G.begin(project, store)
    (project / "big.txt").write_text("x = 1\n" * 10, encoding="utf-8")
    rec = _rec(G.end(project, "shrink", store=store))
    assert "big.txt" not in rec["files_edited"] and not rec["tool_calls"]    # old: a Write of the whole file
    assert rec["turns"][0]["files_not_compared"] == ["big.txt"]
    assert rec["turns"][0]["not_compared_why"] == [G.WHY_NOT_TEXT_START]


def test_a_clean_large_file_that_shrinks_is_not_a_creation(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    (project / "big.txt").write_text("x = 1\n" * (G.MAX_TEXT_BYTES // 6 + 10), encoding="utf-8")
    run_git(project, "add", "big.txt")
    run_git(project, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "big")
    G.begin(project, store)
    (project / "big.txt").write_text("small now\n", encoding="utf-8")
    rec = _rec(G.end(project, "shrink", store=store))
    assert "big.txt" not in rec["files_edited"] and not rec["tool_calls"]    # old: git show too large -> a Write
    assert rec["turns"][0]["files_not_compared"] == ["big.txt"]


@pytest.mark.parametrize("git", [True, False])
def test_a_file_that_grows_past_the_text_limit_is_not_a_deletion(tmp_path, git):
    project = make_project(tmp_path, git=git)
    store = project / ".devmemory"
    G.begin(project, store)
    (project / "greet.py").write_text("y = 2\n" * (G.MAX_TEXT_BYTES // 6 + 10), encoding="utf-8")
    rec = _rec(G.end(project, "grow", store=store))
    assert rec["files_deleted"] == [] and not rec["tool_calls"]              # old: a Delete of greet.py
    assert rec["turns"][0]["files_not_compared"] == ["greet.py"]
    assert rec["turns"][0]["not_compared_why"] == [G.WHY_NOT_TEXT_NOW]


def test_a_deleted_file_kept_by_hash_is_a_deletion(tmp_path, monkeypatch):
    _small(monkeypatch, MAX_COMPARED_FILES=0)                  # every dirty file by hash only
    project = make_project(tmp_path)
    store = project / ".devmemory"
    (project / "draft.txt").write_text("draft\n", encoding="utf-8")
    G.begin(project, store)
    (project / "draft.txt").unlink()
    rec = _rec(G.end(project, "remove", store=store))
    assert rec["files_deleted"] == ["draft.txt"] and not rec["tool_calls"]   # no old text to show


# ---- end: git show time, the commit range, the line diff and the record are bounded ----

def _commit_files(project, count: int, commits: int, lines: int = 30) -> list[str]:
    d = project / "gen"
    d.mkdir(exist_ok=True)
    names = []
    per = count // commits
    for c in range(commits):
        for i in range(per):
            rel = f"gen/c{c:02d}_{i:03d}.txt"
            (project / rel).write_text(_lines(rel, lines), encoding="utf-8")
            names.append(rel)
        run_git(project, "add", "-A", "--", ".", ":!.devmemory")
        run_git(project, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", f"c{c}")
    return names


def test_git_show_time_is_bounded(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    names = _commit_files(project, 40, 1)
    G.begin(project, store)
    for rel in names:                                           # clean at begin, changed now: needs `git show`
        (project / rel).write_text("changed\n", encoding="utf-8")
    monkeypatch.setattr(G, "GIT_SHOW_BUDGET_S", 0.3)
    spent, limits = [], []
    real = G._git_show

    def show(*a, **k):
        limits.append(k.get("timeout"))
        t0 = time.monotonic()
        try:
            time.sleep(0.02)        # (a fast machine shows 40 files in under the budget: each call costs 20 ms here)
            return real(*a, **k)
        finally:
            spent.append(time.monotonic() - t0)
    monkeypatch.setattr(G, "_git_show", show)
    rec = _rec(G.end(project, "x", store=store))
    assert sum(spent) <= 0.3 + max(spent) + 0.05               # stops at the budget (one call may cross it)
    assert len(spent) < len(names)
    # never cut short by the budget: a git that runs out of its limit pauses git in the project for a minute
    assert set(limits) == {G.GIT_SHOW_TIMEOUT_S} and G.WHY_SHOW_FAILED not in rec["turns"][0]["not_compared_why"]
    names_nc, more = _not_compared(rec["turns"][0])
    assert len(rec["files_edited"]) + len(names_nc) + more == len(names)
    assert G.WHY_SHOW in rec["turns"][0]["not_compared_why"]


def test_a_long_commit_range_is_bounded(tmp_path, monkeypatch):
    lim = _small(monkeypatch, MAX_REDACT_CHARS=200_000)
    project = make_project(tmp_path)
    store = project / ".devmemory"
    G.begin(project, store)
    names = _commit_files(project, 400, 20)
    chars = _Chars(monkeypatch)
    t0 = time.monotonic()
    meta = G.end(project, "generate", store=store)
    took = time.monotonic() - t0
    rec = _rec(meta)
    t = rec["turns"][0]
    assert len(rec["files_edited"]) == lim["MAX_COMPARED_FILES"]          # the first ones in name order, as Writes
    nc, more = _not_compared(t)
    assert len(rec["files_edited"]) + len(nc) + more == len(names) and len(nc) <= S.STORED_PATHS
    assert t["not_compared_why"] == [G.WHY_FILES]
    assert chars.n <= lim["MAX_REDACT_CHARS"] + 4 * lim["MAX_EDIT_CHARS"] + 2 * lim["MAX_UDIFF_CHARS"] + 50_000
    assert meta.stat().st_size < 4 * lim["MAX_EDIT_CHARS"] + 64 * 1024
    assert _raw(meta).stat().st_size < lim["MAX_UDIFF_CHARS"] + 64 * 1024
    assert took < 60


def test_a_commit_range_git_cannot_list_is_said(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    G.begin(project, store)
    _commit_files(project, 60, 2)
    monkeypatch.setattr(S, "MAX_STATUS_ENTRIES", 20)            # the range lists 60 names: more than kept
    rec = _rec(G.end(project, "x", store=store))
    assert rec["files_edited"] == [] and G.WHY_RANGE in rec["turns"][0]["not_compared_why"]


def _interleaved(n: int) -> tuple[str, str]:
    """The line diff's slow shape: every old line is still there, each followed by a new one (O(n^2) in difflib)."""
    old = [f"line {i}" for i in range(n)]
    new = [x for i in range(n) for x in (f"line {i}", f"inserted {i}")]
    return "\n".join(old) + "\n", "\n".join(new) + "\n"


def test_the_line_diff_is_bounded(tmp_path):
    a, b = _interleaved(6000)                                   # old: about 10 s for this one file
    t0 = time.monotonic()
    info: dict = {}
    edits, changed, udiff = G.diff_edits({"big.txt": a}, {"big.txt": b}, info)
    assert time.monotonic() - t0 < 3
    assert changed == ["big.txt"] and info["not_line_diffed"] == 1
    assert len(edits) == 1 and edits[0]["not_line_diffed"] is True and edits[0]["truncated"] is True
    assert "not diffed line by line" in udiff and len(udiff) < 1000


def test_every_line_rewritten_in_large_files_is_bounded():
    files_a = {f"f{i}.txt": "".join(f"a{i} {j}\n}}\n\n" for j in range(4000)) for i in range(5)}
    files_b = {f"f{i}.txt": "".join(f"b{i} {j}\n}}\n\n" for j in range(4000)) for i in range(5)}
    t0 = time.monotonic()
    info: dict = {}
    edits, changed, _udiff = G.diff_edits(files_a, files_b, info)
    assert time.monotonic() - t0 < 5                            # old: SequenceMatcher on 4,000 x 4,000 repeated lines
    assert len(changed) == 5 and info["not_line_diffed"] == 5


def test_a_small_change_in_a_large_file_is_still_diffed_line_by_line():
    a = "".join(f"line {i}\n" for i in range(20_000))
    b = a.replace("line 12345\n", "line 12345 changed\n")
    info: dict = {}
    edits, _changed, udiff = G.diff_edits({"big.txt": a}, {"big.txt": b}, info)
    assert edits == [{"tool": "Edit", "file": "big.txt", "old": "line 12345", "new": "line 12345 changed",
                      "truncated": False}]
    assert "not_line_diffed" not in info and "+line 12345 changed" in udiff


def test_the_record_is_bounded(tmp_path, monkeypatch):
    lim = _small(monkeypatch)
    after = {f"f{i:03d}.txt": _lines(f"f{i}", 300) for i in range(100)}
    info: dict = {}
    edits, changed, udiff = G.diff_edits({}, after, info)
    assert len(changed) == 100 and len(edits) <= lim["MAX_RECORD_EDITS"]
    assert sum(len(e.get("new") or "") + len(e["file"]) for e in edits) <= lim["MAX_EDIT_CHARS"]
    assert len(udiff) <= lim["MAX_UDIFF_CHARS"] + 200
    assert info["edits_left_out"] == 100 - len(edits) and info["diff_left_out"] > 0
    project = make_project(tmp_path)
    events = [{"type": "item", "text": "e" * 1000} for _ in range(500)]
    done: dict = {}
    meta = G.record_generic(project, "p" * 1_000_000, {}, after, "2026-11-15T00:00:00Z", "test",
                            store=project / ".devmemory", result_text="r" * 500_000, events=events,
                            errors=["boom " * 10_000] * 200, info=done)
    rec = _rec(meta)
    t = rec["turns"][0]
    assert t["prompt_truncated"] is True and len(t["prompt"]) <= G.PROMPT_CAP
    assert t["result_truncated"] is True and len(t["result_text"]) <= G.PROMPT_CAP
    assert len(t["errors"]) == G.MAX_ERRORS and t["errors_left_out"] == 150
    assert t["edits_left_out"] == info["edits_left_out"] and t["tool_events_left_out"] > 0
    # the record holds its edits twice (turn and top level), the prompt and the result text three times each, and
    # JSON writes a newline as two bytes
    assert meta.stat().st_size < 2 * 2 * lim["MAX_EDIT_CHARS"] + 6 * G.PROMPT_CAP + 64 * 1024
    assert _raw(meta).stat().st_size < lim["MAX_UDIFF_CHARS"] + lim["MAX_EVENTS_CHARS"] + 3 * G.PROMPT_CAP + 64 * 1024
    note = G.end_note(done)
    assert "edits were left out" in note and "tool events were left out" in note and "cut to" in note


# ---- an ordinary session is recorded exactly as before: no bound is met, no new field appears ----

def test_an_ordinary_session_meets_no_bound(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    for i in range(150):
        (project / f"m{i:03d}.py").write_text(_lines(f"m{i}", 80), encoding="utf-8")
    run_git(project, "add", "-A", "--", ".", ":!.devmemory")
    run_git(project, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "base")
    for i in range(10):                                          # dirty at begin
        (project / f"m{i:03d}.py").write_text(_lines(f"d{i}", 80), encoding="utf-8")
    info: dict = {}
    p = G.begin(project, store, info=info)
    state = json.loads(p.read_text(encoding="utf-8"))
    assert set(state) == {"id", "started_at", "kind", "v", "head", "dirty", "since"} and G.begin_note(info) is None
    assert all(set(e) == {"sha", "text"} for e in state["dirty"].values())
    for i in range(150):
        f = project / f"m{i:03d}.py"
        f.write_text(f.read_text(encoding="utf-8").replace(" = 7\n", " = 8\n"), encoding="utf-8")
    for i in range(30):
        (project / f"new{i}.py").write_text(f"n = {i}\n", encoding="utf-8")
    done: dict = {}
    meta = G.end(project, "the task", store=store, info=done)
    rec = _rec(meta)
    t = rec["turns"][0]
    assert G.end_note(done) is None
    for k in ("files_not_compared", "files_not_compared_more", "not_compared_why", "edits_left_out",
              "not_line_diffed", "prompt_truncated", "files_deleted_more", "tool_events_left_out"):
        assert k not in t
    assert len(rec["files_edited"]) == 180 and len(rec["tool_calls"]) == 180
    assert next(c for c in rec["tool_calls"] if c["file"] == "m005.py") == {
        "tool": "Edit", "file": "m005.py", "old": "d5 line 1 = 7", "new": "d5 line 1 = 8", "truncated": False}
    raw = [json.loads(line) for line in _raw(meta).read_text(encoding="utf-8").splitlines()]
    assert "unified_diff_left_out" not in raw[-1] and "# devmem" not in raw[-1]["unified_diff"]


# ---- MCP, pending files from earlier versions ----

def test_mcp_tools_say_what_was_left_out(tmp_path, monkeypatch):
    from devmem.mcp_server import Server
    _small(monkeypatch)
    project = make_project(tmp_path)
    _untracked(project, 300)
    server = Server(project, project / ".devmemory")
    began = server.call("begin_session", {})
    assert "pending id" in began and "not kept in full" in began
    (project / "unpacked" / "f0299.txt").write_text("changed\n", encoding="utf-8")
    ended = server.call("end_session", {"prompt": "x" * 100_000, "summary": "done"})
    assert "recorded session" in ended and "not compared" in ended
    server.call("begin_session", {})
    with pytest.raises(ValueError):
        server.call("end_session", {"prompt": "x", "summary": ["not text"]})


def test_v1_begin_with_much_raw_text_is_rewritten_within_the_bounds(tmp_path, monkeypatch):
    lim = _small(monkeypatch, MAX_COMPARED_FILES=5)
    project = make_project(tmp_path, git=False)
    store = project / ".devmemory"
    (store / "pending").mkdir(parents=True)
    texts = {f"f{i:02d}.txt": _lines(f"v1 {i}", 50) for i in range(30)}
    for rel, text in texts.items():
        (project / rel).write_text(text, encoding="utf-8")
    texts.update({rel: (project / rel).read_text(encoding="utf-8") for rel in ("greet.py", "notes.txt")})
    (store / "pending" / "old.json").write_text(json.dumps({"id": "old", "started_at": "2026-01-01T00:00:00Z",
                                                            "kind": "snapshot", "snapshot": texts}), encoding="utf-8")
    chars = _Chars(monkeypatch)
    assert G.secure_pending(store) == ["old"]
    assert chars.n <= lim["MAX_REDACT_CHARS"]
    state = json.loads((store / "pending" / "old.json").read_text(encoding="utf-8"))
    assert sum("text" in e for e in state["snapshot"].values()) == 5 and G.validate_begin(state) is None
    (project / "f00.txt").write_text("changed\n", encoding="utf-8")
    (project / "f29.txt").write_text("changed\n", encoding="utf-8")
    rec = _rec(G.end(project, "x", store=store, pending_id="old"))
    assert rec["files_edited"] == ["f00.txt"] and rec["turns"][0]["files_not_compared"] == ["f29.txt"]


def test_secure_pending_does_not_parse_a_current_begin(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    G.begin(project, store)
    read = []
    real = G.read_store_text
    monkeypatch.setattr(G, "read_store_text", lambda p, *a, **k: read.append(p) or real(p, *a, **k))
    assert G.secure_pending(store) == [] and read == []          # old: every begin file parsed whole, every time
