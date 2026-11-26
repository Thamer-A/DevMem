"""devmem reads git's index file itself when the git program cannot answer (devmem.gitindex), and gives the same
answer as git wherever it answers at all.

Users who pull with a git that devmem's hooks or MCP server cannot see (GitHub Desktop's or Visual Studio's own git, a
git inside a conda, scoop or nix folder) used to have a pulled, tracked session record served: with no git on PATH
and no earlier answer, "nothing tracked" was assumed (reviewer's v8a/b/t13_nogit_hooks.py). Every answer here is
checked against git's own on the same repository.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import consent, gitindex, store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

G = ["-c", "core.autocrlf=false", "-c", "core.protectNTFS=false", "-c", "user.email=t@example.invalid",
     "-c", "user.name=t"]
REC = "20261101T1000_abcdef12.meta.json"


def _git(where: Path, *args: str, input: bytes | None = None) -> str:
    r = subprocess.run(["git", "-C", str(where), *G, *args], capture_output=True, input=input, check=True)
    return r.stdout.decode("utf-8", "replace")


def _fresh() -> None:
    store._FILTERS.clear()
    store._git_down_until = 0.0
    consent.forget_decisions()


def _record(st: Path, name: str = REC) -> Path:
    (st / "sessions").mkdir(parents=True, exist_ok=True)
    p = st / "sessions" / name
    p.write_text(json.dumps({"session_id": "abcdef12", "prompt": "pulled"}), encoding="utf-8")
    return p


def _entry(where: Path, path: str, content: Path) -> None:
    oid = _git(where, "hash-object", "-w", str(content)).strip()
    _git(where, "update-index", "--add", "--index-info", input=f"100644 {oid}\t{path}\n".encode("utf-8"))


def _by_git(where: Path, st: Path, monkeypatch) -> list[str]:
    def unreadable(*a, **k):
        raise gitindex.IndexUnknown("git must answer here")
    with monkeypatch.context() as m:
        m.setattr(gitindex, "index_candidates", unreadable)
        _fresh()
        return store.store_index_entries(where, st)


def _by_index(where: Path, st: Path, monkeypatch) -> list[str]:
    def no_git(*a, **k):
        raise FileNotFoundError("git was not found on PATH")
    with monkeypatch.context() as m:
        m.setattr(store, "_candidates_raw", no_git)
        _fresh()
        return store.store_index_entries(where, st)


def _agree(where: Path, st: Path, monkeypatch) -> list[str]:
    got = _by_git(where, st, monkeypatch)
    assert _by_index(where, st, monkeypatch) == got
    return got


SPELLINGS = [".devmemory", ".DevMemory", ".DEVMEMORY", "short", ".devmemory.", ".devmemory::$INDEX_ALLOCATION",
             "backslash"]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_every_spelling_gets_the_same_answer_as_from_git(tmp_path, monkeypatch, spelling):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    rec = _record(st)
    if spelling == "short":
        if os.name != "nt":
            pytest.skip("8.3 short names: Windows")
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(st), buf, 1024)
        name = Path(buf.value).name if n else ""
        if not name or name == st.name:
            pytest.skip("no 8.3 short names here")
        path = f"{name}/sessions/{rec.name}"
    elif spelling == "backslash":
        path = f".devmemory\\sessions\\{rec.name}"
    else:
        path = f"{spelling}/sessions/{rec.name}"
    _entry(project, path, rec)
    got = _agree(project, st, monkeypatch)
    if spelling == ".devmemory" or os.name == "nt":
        assert [g.split(" ", 1)[1] for g in got] == [path]


@pytest.mark.parametrize("version", ["2", "3", "4"])
def test_index_versions_extended_flags_and_conflict_stages(tmp_path, monkeypatch, version):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    rec = _record(st)
    _entry(project, f".devmemory/sessions/{rec.name}", rec)
    for i in range(40):                                       # shared prefixes: version 4 compresses them
        (project / "deep" / f"dir{i % 3}").mkdir(parents=True, exist_ok=True)
        (project / "deep" / f"dir{i % 3}" / f"file{i}.py").write_text(f"x = {i}\n", encoding="utf-8")
    (project / ".devmemory" / "PROJECT.md").write_text("map\n", encoding="utf-8")
    _git(project, "add", "deep")
    _git(project, "add", "-f", "-N", ".devmemory/PROJECT.md")              # intent to add: an extended flag
    _git(project, "update-index", "--skip-worktree", "greet.py")            # skip-worktree: an extended flag
    c = st / "sessions" / "c.json"                            # a conflicted merge: three stages of one path
    c.write_text("base\n", encoding="utf-8")
    _git(project, "add", "-f", str(c))
    _git(project, "commit", "-qm", "base")
    _git(project, "checkout", "-q", "-b", "other")
    c.write_text("other\n", encoding="utf-8")
    _git(project, "commit", "-qam", "other")
    _git(project, "checkout", "-q", "-")
    c.write_text("mine\n", encoding="utf-8")
    _git(project, "commit", "-qam", "mine")
    subprocess.run(["git", "-C", str(project), *G, "merge", "-q", "other"], capture_output=True)
    assert len(_git(project, "ls-files", "-u").splitlines()) == 3
    _git(project, "update-index", "--add", "--cacheinfo",
         f"100644,{_git(project, 'hash-object', '-w', str(rec)).strip()},.devmemory/sessions/{rec.name}")
    _git(project, "update-index", "--index-version", version)
    raw = (project / ".git" / "index").read_bytes()
    assert raw[4:8] == int(version).to_bytes(4, "big") or version == "2"      # git raises 2 to 3 for extended flags
    got = _agree(project, st, monkeypatch)
    assert len([g for g in got if g.endswith("c.json")]) == 3
    assert any(g.endswith("PROJECT.md") for g in got)


def test_a_project_below_the_repository_root_prints_paths_as_git_does(tmp_path, monkeypatch):
    root = tmp_path / "root"
    project = root / "sub" / "app"
    project.mkdir(parents=True)
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    st = project / ".devmemory"
    rec = _record(st)
    _entry(root, f"sub/app/.devmemory/sessions/{rec.name}", rec)
    _entry(root, f"SUB/APP/.DevMemory/sessions/{rec.name}", rec)
    _entry(root, f"sub/APP/.devmemory/sessions/{rec.name}", rec)
    _entry(root, "sub/other/.devmemory/x.json", rec)
    got = _agree(project, st, monkeypatch)
    assert f"{_git(root, 'hash-object', str(rec)).strip()} .devmemory/sessions/{rec.name}" in got


LAYOUTS = ["plain", "worktree_config", "commondir", "include", "include_if", "gitdir_file", "submodule",
           "nested_repository"]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_every_layout_of_the_layout_tests_gets_gits_answer_or_none(tmp_path, monkeypatch, layout):
    from test_git_layouts import build
    repo = build(tmp_path, layout)
    project = repo.project
    st = project / ".devmemory"
    rec = _record(st)
    _git(project, "add", "-f", str(rec))
    got = _by_git(project, st, monkeypatch)
    assert [g.split(" ", 1)[1] for g in got] == [f".devmemory/sessions/{rec.name}"]
    try:
        assert _by_index(project, st, monkeypatch) == got
    except store.GitNoAnswer as e:                   # it may decline, never answer otherwise
        assert layout in ("include", "include_if"), (layout, e)
    if layout in ("submodule", "nested_repository"):  # a store inside the inner repository
        inner = project / "sub" / ".devmemory"
        rec2 = _record(inner)
        _git(project / "sub", "add", "-f", str(rec2))
        assert _agree(project / "sub", inner, monkeypatch)


def test_a_linked_worktree(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    wt = tmp_path / "wt"
    _git(project, "worktree", "add", "-q", str(wt))
    st = wt / ".devmemory"
    rec = _record(st)
    _git(wt, "add", "-f", str(rec))
    assert _agree(wt, st, monkeypatch)
    assert _agree(project, project / ".devmemory", monkeypatch) == []


def test_a_sha256_repository(tmp_path, monkeypatch):
    p = tmp_path / "p"
    p.mkdir()
    try:
        _git(p, "init", "-q", "--object-format=sha256")
    except subprocess.CalledProcessError:
        pytest.skip("this git has no SHA-256 repositories")
    (p / "a.py").write_text("x = 1\n", encoding="utf-8")
    st = p / ".devmemory"
    rec = _record(st)
    _git(p, "add", "a.py")
    _git(p, "add", "-f", str(rec))
    got = _agree(p, st, monkeypatch)
    assert len(got) == 1 and len(got[0].split(" ", 1)[0]) == 64


def _declines(where: Path, st: Path, monkeypatch, why: str) -> None:
    with pytest.raises(store.GitNoAnswer, match=why):
        _by_index(where, st, monkeypatch)


def test_what_the_reader_does_not_handle_is_cannot_say(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    _record(st)
    assert _by_index(project, st, monkeypatch) == []
    idx = project / ".git" / "index"
    good = idx.read_bytes()
    idx.write_bytes(good[:-1] + bytes([good[-1] ^ 1]))                 # a wrong checksum: git refuses it too
    _declines(project, st, monkeypatch, "could not read")
    idx.write_bytes(good)
    monkeypatch.setattr(gitindex, "MAX_INDEX_BYTES", 64)
    _declines(project, st, monkeypatch, "larger than")
    monkeypatch.setattr(gitindex, "MAX_INDEX_BYTES", 128 * 1024 * 1024)
    monkeypatch.setenv("GIT_DIR", str(project / ".git"))
    _declines(project, st, monkeypatch, "environment")
    monkeypatch.delenv("GIT_DIR")
    with open(project / ".git" / "config", "a", encoding="utf-8") as f:
        f.write(f"[core]\n\tworktree = {tmp_path.as_posix()}\n")
    _declines(project, st, monkeypatch, "another work tree")


def test_a_split_index_and_a_sparse_index_are_cannot_say(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    _record(st)
    _git(project, "update-index", "--split-index")
    _declines(project, st, monkeypatch, "split")
    _git(project, "update-index", "--no-split-index")
    assert _by_index(project, st, monkeypatch) == []
    (project / "docs").mkdir()
    (project / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    _git(project, "add", "docs")
    _git(project, "commit", "-qm", "docs")
    try:
        _git(project, "sparse-checkout", "init", "--cone", "--sparse-index")
        _git(project, "sparse-checkout", "set", "other")
    except subprocess.CalledProcessError:
        pytest.skip("this git has no sparse index")
    if b"sdir" not in (project / ".git" / "index").read_bytes():
        pytest.skip("git did not write a sparse index")
    _declines(project, st, monkeypatch, "sparse")


def test_the_reader_has_a_deadline(tmp_path):
    project = make_project(tmp_path)
    with pytest.raises(gitindex.IndexUnknown, match="too long"):
        gitindex.index_candidates(project, [".devmemory"], time.monotonic() - 1, 1000)


def test_without_git_the_hooks_serve_no_pulled_record(tmp_path, monkeypatch):
    """The reviewer's t13 `plain`: `devmem init`, then a record pulled with a git the hook cannot see."""
    up = make_project(tmp_path / "up", init_store=False)
    project = tmp_path / "me"
    subprocess.run(["git", "clone", "-q", str(up), str(project)], check=True, capture_output=True)
    st = project / ".devmemory"
    st.mkdir()
    consent.claim_store(st, project)
    consent.enable_init(project)
    rec = _record(up / ".devmemory")
    _git(up, "add", "-f", str(rec))
    _git(up, "commit", "-qm", "a record")
    _git(project, "pull", "-q", "--no-rebase")
    assert (st / "sessions" / REC).exists()
    env = {**os.environ, "PATH": str(tmp_path / "no-git"), "PYTHONPATH": str(Path(store.__file__).parent.parent),
           "CLAUDE_PROJECT_DIR": str(project)}
    r = subprocess.run([sys.executable, "-m", "devmem", "pack", "--project", str(project), "pulled"],
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    assert "pulled" not in r.stdout and r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "tracked by git" in r.stderr


@pytest.mark.parametrize("how", ["init", "claim", "adopt"])
def test_setting_up_keeps_gits_answer_for_the_first_event(tmp_path, how):
    """`devmem init`, /devmem:setup, `devmem adopt` and a store devmem creates ask git once, where git is available,
    so that a hook that cannot run git later still has an answer for the same index."""
    from cli_helpers import run_cli
    project = make_project(tmp_path, init_store=False)
    st = project / ".devmemory"
    if how == "init":
        r = run_cli(["init", "--project", str(project)], cwd=tmp_path)
        assert r.returncode == 0, r.stderr
    elif how == "claim":
        store.ensure_store(st)
    else:
        st.mkdir()
        consent.claim_store(st, project, adopt=True)
    checked = json.loads(consent._checked_path(st).read_text(encoding="utf-8"))
    assert checked["ok"]["entries"] == [] and checked["ok"]["stamp"] == consent._git_index_stamp(project)


def test_a_new_repository_without_an_index_tracks_nothing(tmp_path, monkeypatch):
    p = tmp_path / "p"
    p.mkdir()
    _git(p, "init", "-q")
    st = p / ".devmemory"
    _record(st)
    assert not (p / ".git" / "index").exists()
    assert _agree(p, st, monkeypatch) == []


def test_any_value_in_an_entry_is_cannot_say_or_an_answer_never_an_error(tmp_path):
    """A mode field that fits no platform's mode_t made the reader raise OverflowError (seen with the 500,000-entry
    index of the reviewer's t8_idx_win.py, where the SHA-256 attempt misreads the entries)."""
    import hashlib
    import struct as st
    path = b".devmemory/x.json"
    head = st.pack(">4sII", b"DIRC", 2, 1)
    entry = bytes(24) + st.pack(">I", 0xFFFFFFFF) + bytes(12) + bytes(20) + st.pack(">H", len(path)) + path
    entry += bytes(8 - len(entry) % 8)
    body = head + entry
    idx = tmp_path / "index"
    idx.write_bytes(body + hashlib.sha1(body).digest())
    with pytest.raises(gitindex.IndexUnknown, match="mode"):     # not a mode git writes: cannot say (as git)
        gitindex.read_index(idx, time.monotonic() + 5)
