"""begin/end after the independent review: the open/read bound holds for the whole command (the record included), the
line diff's work is counted and cut (not estimated), a file the begin did not see is not recorded as created without
evidence, and a broken begin file named with --id says what is wrong with it."""
from __future__ import annotations

import difflib
import json
import os
import random
import shutil
import time

import pytest
from hooks_helpers import make_project, run_git

from devmem import generic as G

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _rec(meta):
    return json.loads(meta.read_text(encoding="utf-8"))


class _Opens:
    """os.open on files inside the project (the store and .git/ aside), and the bytes they held."""

    def __init__(self, monkeypatch, project):
        self.opens = self.bytes = 0
        root = os.path.normcase(os.path.abspath(project))
        real = os.open

        def counting(path, flags, *a, **k):
            fd = real(path, flags, *a, **k)
            try:
                ap = os.path.normcase(os.path.abspath(os.fspath(path)))
                inside = ap[len(root):]
                if ap.startswith(root + os.sep) and not inside.startswith((os.sep + ".devmemory", os.sep + ".git" + os.sep)):
                    self.opens += 1
                    self.bytes += os.fstat(fd).st_size
            except (OSError, TypeError, ValueError):
                pass
            return fd
        monkeypatch.setattr(os, "open", counting)

    def take(self) -> tuple[int, int]:
        out = (self.opens, self.bytes)
        self.opens = self.bytes = 0
        return out


@pytest.mark.parametrize("mode", ["dirty", "clean", "snap"])
def test_begin_and_end_open_and_read_within_the_bounds(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(G, "MAX_READ_FILES", 40)
    monkeypatch.setattr(G, "MAX_READ_BYTES", 200_000)
    project = make_project(tmp_path, git=(mode != "snap"))
    store = project / ".devmemory"
    names = [f"pkg/m{i:03d}.py" for i in range(70)]

    def fill(tag):
        for rel in names:
            (project / rel).parent.mkdir(exist_ok=True)
            (project / rel).write_text(f"v_{tag} = 1  # padding\n" * 200, encoding="utf-8")   # about 4.6 KB
    fill("a")
    if mode != "snap":
        run_git(project, "add", "-A", "--", ".", ":!.devmemory")
        run_git(project, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "files")
    if mode == "dirty":
        fill("b")
    count = _Opens(monkeypatch, project)
    G.begin(project, store)
    opens, read = count.take()
    assert opens <= 40 and read <= 200_000 + G.MAX_TEXT_BYTES
    fill("c")
    meta = G.end(project, "x", store=store)
    opens, read = count.take()                       # the record's hashes come from these reads: no second open
    assert opens <= 40 and read <= 200_000 + G.MAX_TEXT_BYTES, (opens, read)
    rec = _rec(meta)
    assert rec["files_edited"] and set(rec["file_hashes_at_capture"]) == set(rec["files_edited"])


def test_the_record_hashes_are_the_ones_file_hash_gives(tmp_path):
    from devmem.store import file_hash
    project = make_project(tmp_path)
    store = project / ".devmemory"
    G.begin(project, store)
    (project / "greet.py").write_bytes("def greet(n):\r\n    return 'hé ' + n\r\nlone\rcr\n".encode("utf-8"))
    (project / "new.txt").write_text("created\n", encoding="utf-8")
    rec = _rec(G.end(project, "x", store=store))
    assert rec["file_hashes_at_capture"] == {f: file_hash(project / f) for f in ("greet.py", "new.txt")}


# ---- the line diff: its work is counted and cut ----

def _shapes(n):
    yield "interleaved y", [x for i in range(n // 2) for x in (f"a{i}", "y")], \
        [x for i in range(n // 2) for x in (f"b{i}", "y")]
    yield "interleaved y,z", [x for i in range(n) for x in (f"a{i}", "y", "z")][:n], \
        [x for i in range(n) for x in ("z", f"b{i}", "y")][:n]
    yield "all y vs half y", ["y"] * n, ["y" if i % 2 else f"b{i}" for i in range(n)]
    yield "blank-separated, renamed", [x for i in range(n // 2) for x in (f"old_{i} = f({i})", "")], \
        [x for i in range(n // 2) for x in (f"new_{i} = f({i})", "")]
    yield "all lines equal", ["same"] * n, ["same"] * (n - 1) + ["other"]
    yield "a subsequence", [f"l{i}" for i in range(n)], [f"l{i}" for i in range(0, n, 2)]
    yield "a supersequence", [f"l{i}" for i in range(n)], [x for i in range(n) for x in (f"l{i}", f"i{i}")]
    yield "40 lines permuted", [f"l{i % 40}" for i in range(n)], [f"l{(i * 7) % 40}" for i in range(n)]
    yield "long common runs", [f"c{i}" for i in range(n)], [f"c{i}" if i % 500 else f"x{i}" for i in range(n)]


@pytest.mark.parametrize("n", [200, 2_000, 20_000, 50_000])
def test_adversarial_line_diffs_stay_within_their_steps(n):
    for name, al, bl in _shapes(n):
        a, b = "\n".join(al) + "\n", "\n".join(bl) + "\n"
        if len(a) > G.MAX_TEXT_BYTES or len(b) > G.MAX_TEXT_BYTES:
            continue
        info: dict = {}
        t0 = time.monotonic()
        edits, _changed, _udiff = G.diff_edits({"f": a}, {"f": b}, info)
        took = time.monotonic() - t0
        assert info["diff_steps"] <= G.MAX_DIFF_STEPS, name          # the work, counted
        assert took < 5, (name, n, took)                              # about 1 s at most on the test machine
        assert edits


def test_a_record_of_adversarial_files_stays_within_the_record_steps():
    before, after = {}, {}
    for i, (_name, al, bl) in enumerate(_shapes(4_000)):
        before[f"f{i}"], after[f"f{i}"] = "\n".join(al) + "\n", "\n".join(bl) + "\n"
    info: dict = {}
    t0 = time.monotonic()
    G.diff_edits(before, after, info)
    assert info["diff_steps"] <= G.MAX_DIFF_STEPS_TOTAL and time.monotonic() - t0 < 20
    assert info["not_line_diffed"] >= 1


def test_the_counted_diff_is_difflibs_diff():
    """Within its steps the counted matcher gives difflib's opcodes and unified diff exactly (random line lists with
    many repeats, the shapes where a shortcut would differ first)."""
    rnd = random.Random(5)
    for case in range(400):
        alphabet = [f"l{k}" for k in range(rnd.choice((2, 3, 5, 20, 200)))] + [""]
        al = [rnd.choice(alphabet) for _ in range(rnd.randint(0, 120))]
        bl = [x for x in al if rnd.random() > 0.2] + [rnd.choice(alphabet) for _ in range(rnd.randint(0, 30))]
        rnd.shuffle(bl) if case % 7 == 0 else None
        left = [10**9]
        for junk in (False, True):
            want = difflib.SequenceMatcher(None, al, bl, autojunk=junk).get_opcodes()
            assert G._Counted(al, bl, junk, left).get_opcodes() == want, case
        assert G._unified(al, bl, "a/f", "b/f", left) == list(difflib.unified_diff(al, bl, "a/f", "b/f", lineterm=""))


# ---- a file the begin did not see ----

def _ignored_at_begin(tmp_path, git: bool):
    project = make_project(tmp_path, git=git)
    (project / ".gitignore").write_text("local.cfg\n", encoding="utf-8")
    (project / "local.cfg").write_text("keep = 2\n", encoding="utf-8")
    if git:
        run_git(project, "add", ".gitignore")
        run_git(project, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "ignore")
    old = time.time() - 3600
    os.utime(project / "local.cfg", (old, old))
    return project


@pytest.mark.parametrize("git", [True, False])
def test_a_file_ignored_at_begin_is_not_recorded_as_created(tmp_path, git):
    project = _ignored_at_begin(tmp_path, git)
    store = project / ".devmemory"
    G.begin(project, store)
    (project / ".gitignore").write_text("\n", encoding="utf-8")        # the session only edits the ignore file
    rec = _rec(G.end(project, "edit gitignore", store=store))
    t = rec["turns"][0]
    assert "local.cfg" not in rec["files_edited"] and ("Write", "local.cfg") not in \
        [(c["tool"], c["file"]) for c in rec["tool_calls"]]             # old: Write local.cfg create
    assert ".gitignore" in rec["files_edited"]
    assert t["files_not_compared"] == ["local.cfg"] and t["not_compared_why"] == [G.WHY_NOT_SEEN]


@pytest.mark.parametrize("git", [True, False])
def test_a_file_the_session_writes_where_the_begin_did_not_look_is_created(tmp_path, git):
    project = _ignored_at_begin(tmp_path, git)
    store = project / ".devmemory"
    G.begin(project, store)
    (project / ".gitignore").write_text("\n", encoding="utf-8")
    (project / "local.cfg").write_text("keep = 3\n", encoding="utf-8")     # written during the session
    rec = _rec(G.end(project, "x", store=store))
    assert ("Write", "local.cfg") in [(c["tool"], c["file"]) for c in rec["tool_calls"]]
    assert "files_not_compared" not in rec["turns"][0]


def test_a_snapshot_file_ignored_at_end_is_not_a_deletion(tmp_path):
    project = make_project(tmp_path, git=False)
    store = project / ".devmemory"
    (project / "local.cfg").write_text("keep = 2\n", encoding="utf-8")
    G.begin(project, store)
    (project / ".gitignore").write_text("local.cfg\n", encoding="utf-8")   # visible at begin, ignored now
    rec = _rec(G.end(project, "x", store=store))
    assert rec["files_deleted"] == [] and "local.cfg" not in [c["file"] for c in rec["tool_calls"]]
    assert rec["turns"][0]["files_not_compared"] == ["local.cfg"]
    assert rec["turns"][0]["not_compared_why"] == [G.WHY_NOT_LISTED_NOW]


def test_a_git_file_ignored_at_end_is_still_compared(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    (project / "local.cfg").write_text("keep = 2\n", encoding="utf-8")   # untracked: dirty at begin
    G.begin(project, store)
    (project / ".gitignore").write_text("local.cfg\n", encoding="utf-8")
    (project / "local.cfg").write_text("keep = 3\n", encoding="utf-8")
    rec = _rec(G.end(project, "x", store=store))
    edit = next(c for c in rec["tool_calls"] if c["file"] == "local.cfg")
    assert edit["old"] == "keep = 2" and edit["new"] == "keep = 3"


def test_a_file_created_in_a_commit_during_the_session_is_still_created(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    G.begin(project, store)
    (project / "made.py").write_text("x = 1\n", encoding="utf-8")
    run_git(project, "add", "made.py")
    run_git(project, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "made")
    rec = _rec(G.end(project, "x", store=store))
    assert ("Write", "made.py") in [(c["tool"], c["file"]) for c in rec["tool_calls"]]


# ---- begin files: unlisted, and an --id that names a broken one ----

def _begin_file(store, name, body):
    d = store / "pending"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_bytes(body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))


def test_unlisted_past_what_a_begin_writes_is_refused():
    good = {"id": "x", "started_at": "2026-11-15T00:00:00Z", "kind": "snapshot", "v": 2, "snapshot": {}}
    assert G.validate_begin({**good, "unlisted": G.MAX_UNLISTED}) is None
    assert G.validate_begin({**good, "unlisted": G.MAX_UNLISTED + 1}) == "bad unlisted"
    assert G.validate_begin({**good, "unlisted": 2**63}) == "bad unlisted"
    assert G.validate_begin({**good, "since": "soon"}) == "bad since"


@pytest.mark.parametrize("body,why", [
    (b"{not json", "is not valid JSON"),
    ({"id": "b", "started_at": "--x", "kind": "git"}, "bad started_at"),
])
def test_end_with_the_id_of_a_broken_begin_says_what_is_wrong(tmp_path, body, why):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    _begin_file(store, "b", body)
    with pytest.raises(RuntimeError, match=why):
        G.end(project, "x", store=store, pending_id="b")
    assert (store / "pending" / "b.json").exists()


def test_end_with_the_id_of_an_oversized_begin_says_so(tmp_path, monkeypatch):
    from devmem import store as st
    project = make_project(tmp_path)
    store = project / ".devmemory"
    _begin_file(store, "big", {"id": "big", "pad": "p" * 3000})
    monkeypatch.setattr(st, "STORE_FILE_CAP", 1000)
    monkeypatch.setattr(st.read_store_text, "__defaults__", (1000,))
    with pytest.raises(RuntimeError, match="not a plain file of at most"):
        G.end(project, "x", store=store, pending_id="big")


def test_end_with_an_unknown_id_says_so(tmp_path):
    project = make_project(tmp_path)
    with pytest.raises(RuntimeError, match="no begin with id nothere"):
        G.end(project, "x", store=project / ".devmemory", pending_id="nothere")
