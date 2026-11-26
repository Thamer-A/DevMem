"""Which files of a store does git track? Decided by where an index entry lands on this file system, not by how the
path is spelled (devmem.store.store_index_entries, the one answer devmem.consent and devmem.store.tracked_in_store
share).

On Windows and macOS a pull can bring a session record committed as `.DevMemory/sessions/...`, `.DEVMEMORY/...`, the
8.3 short name `DEVMEM~1/...` or (when the repository's own configuration switches core.protectNTFS off) `.devmemory./`,
`.devmemory::$INDEX_ALLOCATION/` or `.devmemory\\sessions\\...`: git writes it into the user's own `.devmemory/sessions/`,
and `git ls-files -- <store>` does not list it (reviewer's v7a/b/c1_case_pull.py, c2_spelling_pull.py,
c3_case_config.py). On Linux those spellings are other folders and must not count.

Also here: an answer git could not give never counts as "nothing tracked" (v7a/b/f1b_failed_memory.py).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import consent, store

G = ["-c", "core.autocrlf=false", "-c", "core.protectNTFS=false", "-c", "user.email=t@example.invalid",
     "-c", "user.name=t"]
REC = "20261101T1000_abcdef12.meta.json"
REAL_GIT_RUN = store.git_run      # put back with monkeypatch.setattr: monkeypatch.undo() would also undo conftest's
                                  # DEVMEM_USER_DATA (the real per-user folder)


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
    p.write_text(json.dumps({"session_id": "abcdef12-0000-0000-0000-000000000000", "prompt": "pulled"}),
                 encoding="utf-8")
    return p


def _short_name(folder: Path) -> str | None:
    """The 8.3 short name of `folder` (Windows), None when the volume keeps none."""
    if os.name != "nt":
        return None
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(folder), buf, 1024)
    short = Path(buf.value).name if n else ""
    return short if short and short != folder.name else None


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _index_entry(where: Path, path: str, content: Path) -> None:
    """An index entry `path` with the content of `content`, as a pull leaves it (the file itself is already where git
    would write it on this file system)."""
    oid = _git(where, "hash-object", "-w", str(content)).strip()
    _git(where, "update-index", "--add", "--index-info", input=f"100644 {oid}\t{path}\n".encode("utf-8"))


SPELLINGS = [".DevMemory", ".DEVMEMORY", "short", ".devmemory.", ".devmemory::$INDEX_ALLOCATION",
             ".devmemory:$I30:$INDEX_ALLOCATION", "backslash",
             # the short name with what Windows drops from a name (a regression of round 5: ruled out unseen)
             "short.", "short::$INDEX_ALLOCATION", "shortlower"]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_an_index_entry_under_another_spelling_of_the_store_counts_where_it_lands(tmp_path, spelling):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    rec = _record(st)
    if spelling.startswith("short"):
        name = _short_name(st)
        if name is None:
            pytest.skip("no 8.3 short names here")
        name = name.lower() if spelling == "shortlower" else name + spelling[len("short"):]
        path = f"{name}/sessions/{rec.name}"
    elif spelling == "backslash":
        if os.name != "nt":
            pytest.skip("a backslash separates folders on Windows only")
        path = f".devmemory\\sessions\\{rec.name}"
    else:
        path = f"{spelling}/sessions/{rec.name}"
    _index_entry(project, path, rec)
    lands = _same(project / path, rec)              # what this file system does with that spelling
    if os.name == "nt":
        assert lands, path
    elif sys.platform.startswith("linux"):
        assert not lands, path                      # other folders here
    _fresh()
    why = consent.store_refusal(project, st)
    listed = store.tracked_in_store(project, st)
    if lands:
        assert why and "tracked by git" in why, why
        assert listed == [path]
    else:
        assert why is None, why
        assert listed == []
    _fresh()
    assert bool(consent.store_refusal(project, st, check_git="cached")) == lands


def test_on_a_case_sensitive_file_system_other_spellings_are_other_folders(tmp_path):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    other = project / ".DevMemory"
    try:
        other.mkdir()
    except FileExistsError:
        pytest.skip("this file system ignores letter case")
    _record(other)
    _git(project, "add", "-f", ".DevMemory")
    _git(project, "commit", "-qm", "another folder")
    _fresh()
    assert consent.store_refusal(project, st) is None
    assert store.tracked_in_store(project, st) == []


def test_a_pull_under_another_letter_case_lands_in_the_store_and_is_noticed(tmp_path):
    up = make_project(tmp_path / "up", init_store=False)
    own = tmp_path / "own"
    subprocess.run(["git", "clone", "-q", str(up), str(own)], check=True, capture_output=True)
    st = own / ".devmemory"
    st.mkdir()
    consent.claim_store(st)
    _record(st, "20261101T0900_11111111.meta.json")
    _record(up / ".DevMemory")
    _git(up, "add", "-f", ".DevMemory")
    _git(up, "commit", "-qm", "docs")
    _git(own, "pull", "-q")
    lands = (st / "sessions" / REC).exists()
    _fresh()
    why = consent.store_refusal(own, st, check_git="cached")
    assert bool(why) == lands
    if lands:
        assert ".DevMemory/sessions/" + REC in why
        assert store.tracked_in_store(own, st) == [".DevMemory/sessions/" + REC]


def test_a_project_below_the_repository_root_with_its_folder_spelled_otherwise(tmp_path):
    root = tmp_path / "root"
    project = root / "sub"
    project.mkdir(parents=True)
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    st = project / ".devmemory"
    st.mkdir()
    consent.claim_store(st)
    rec = _record(st)
    path = f"SUB/.DevMemory/sessions/{rec.name}"   # from the root; git prints it relative to the project
    _index_entry(root, path, rec)
    lands = _same(root / path, rec)
    _fresh()
    assert bool(consent.store_refusal(project, st)) == lands
    assert store.tracked_in_store(project, st) == ([f"../{path}"] if lands else [])
    _git(root, "rm", "-q", "--cached", path)      # the same file under its own spelling: always counted
    _index_entry(root, f"sub/.devmemory/sessions/{rec.name}", rec)
    _fresh()
    assert "tracked by git" in (consent.store_refusal(project, st) or "")
    assert store.tracked_in_store(project, st) == [f".devmemory/sessions/{rec.name}"]


def test_a_store_outside_the_repository_has_nothing_tracked_and_is_not_a_failure(tmp_path, monkeypatch):
    project = make_project(tmp_path / "p")
    st = tmp_path / "elsewhere" / "mystore"
    st.mkdir(parents=True)
    consent.claim_store(st)
    _record(st)
    _fresh()
    assert consent.store_refusal(project, st) is None
    assert consent.store_refusal(project, st, check_git="cached") is None
    assert store.tracked_in_store(project, st) == []


def test_a_store_with_its_own_name_in_another_letter_case(tmp_path):
    project = make_project(tmp_path)
    st = project / "My Store"
    st.mkdir()
    consent.claim_store(st, project)
    rec = _record(st)
    path = f"MY STORE/sessions/{rec.name}"
    _index_entry(project, path, rec)
    lands = _same(project / path, rec)
    _fresh()
    assert bool(consent.store_refusal(project, st)) == lands
    assert store.tracked_in_store(project, st) == ([path] if lands else [])


def test_a_store_name_with_other_unicode_forms_is_asked_for(tmp_path):
    """macOS treats the NFC and NFD spellings of a name as one folder; git is asked for both, and the file system
    decides (on Windows and Linux they are two names)."""
    project = make_project(tmp_path)
    name = unicodedata.normalize("NFC", "café store")
    st = project / name
    st.mkdir()
    consent.claim_store(st, project)
    rec = _record(st)
    path = unicodedata.normalize("NFD", name) + f"/sessions/{rec.name}"
    _index_entry(project, path, rec)
    lands = _same(project / path, rec)
    _fresh()
    got = store.store_index_entries(project, st)
    assert got is not None
    assert [e.split(" ", 1)[1] for e in got] == ([path] if lands else [])
    _fresh()
    listed = store._candidates(project, st)
    assert any(unicodedata.normalize("NFC", p) == unicodedata.normalize("NFC", path) for p in listed)


def test_name_globs_cover_case_and_unicode_spellings():
    assert store._name_glob(".devmemory") == ".devmemory"
    assert store._name_glob("café") == "ca*"          # f has ligature forms (ﬀ), é has NFD and other cases
    assert store._name_glob("Kits[1]*?") == "*1*"
    assert store._name_glob("My Store") == "My*ore"


def test_a_tracked_entry_named_like_the_store_counts(tmp_path):
    """A tracked file (or link, or gitlink) exactly where the store is: counted, so the store is refused."""
    project = make_project(tmp_path)
    st = project / ".devmemory"
    rec = _record(st)
    _index_entry(project, ".devmemory", rec)
    _fresh()
    assert "tracked by git" in (consent.store_refusal(project, st) or "")


def test_an_index_entry_that_is_not_in_the_work_tree_serves_nothing(tmp_path):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    rec = _record(st)
    _index_entry(project, f".devmemory/sessions/{rec.name}", rec)
    rec.unlink()
    _fresh()
    assert consent.store_refusal(project, st) is None
    _record(st)                       # back in the work tree: counted again, with no new git call needed
    _fresh()
    assert "tracked by git" in (consent.store_refusal(project, st, check_git="cached") or "")



@pytest.mark.parametrize("path", [".devmemory/config.json", ".DevMemory/CONFIG.JSON"])
def test_embeddings_from_a_config_json_git_tracks_in_any_spelling_are_ignored(tmp_path, monkeypatch, path):
    from devmem import embed
    monkeypatch.delenv(embed.ENV, raising=False)
    project = make_project(tmp_path)
    st = project / ".devmemory"
    cfg = st / "config.json"
    cfg.write_text(json.dumps({"embeddings": {"backend": "model2vec", "model": "someone/model"}}), encoding="utf-8")
    _fresh()
    assert embed.settings(st) is not None               # untracked: the user's own setting
    _index_entry(project, path, cfg)
    lands = _same(project / path, cfg)
    _fresh()
    assert (embed.settings(st) is None) == lands

# ---------------------------------------------------------------- an answer git could not give


def _own_then_pulled(tmp_path: Path) -> tuple[Path, Path]:
    project = make_project(tmp_path)
    st = project / ".devmemory"
    _record(st)
    _git(project, "add", "-f", str(st / "sessions" / REC))
    _git(project, "commit", "-qm", "pulled")
    return project, st


def _no_index_reader(monkeypatch) -> None:
    """devmem reads git's index file itself when git cannot answer (devmem.gitindex): off, for the rules about a
    question nobody could answer."""
    from devmem import gitindex

    def unreadable(*a, **k):
        raise gitindex.IndexUnknown("not read in this test")
    monkeypatch.setattr(gitindex, "index_candidates", unreadable)


def _count_ls_files(monkeypatch, fail: type[BaseException] | None = None, reader: bool = False) -> list[int]:
    """Counts devmem's question to git; `fail`: git fails that way (and, unless `reader`, the index file cannot be
    read either)."""
    calls: list[int] = []
    real = store.git_run
    if fail is not None and not reader:
        _no_index_reader(monkeypatch)

    def counted(project, *args, timeout=store.GIT_TIMEOUT, **kw):
        if "ls-files" in args and any(str(a).startswith(":(top") for a in args):
            calls.append(1)
            if fail is FileNotFoundError:
                raise FileNotFoundError("git was not found on PATH")
            if fail is not None:
                raise subprocess.TimeoutExpired(["git"], timeout)
        return real(project, *args, timeout=timeout, **kw)
    monkeypatch.setattr(store, "git_run", counted)
    return calls


@pytest.mark.parametrize("cached", ["cached", True])
def test_a_git_failure_never_makes_a_refused_store_trusted(tmp_path, monkeypatch, cached):
    project, st = _own_then_pulled(tmp_path)
    _fresh()
    assert "tracked by git" in (consent.store_refusal(project, st, check_git=cached) or "")
    calls = _count_ls_files(monkeypatch, fail=subprocess.TimeoutExpired)
    for _ in range(3):           # the same index: the last answer git gave stands, git is not asked
        _fresh()
        assert "tracked by git" in (consent.store_refusal(project, st, check_git=cached) or "")
    assert calls == []
    (project / "other.py").write_text("y = 2\n", encoding="utf-8")
    _git(project, "add", "other.py")          # the index changed and git cannot say what it tracks now
    _fresh()
    why = consent.store_refusal(project, st, check_git=cached)
    assert why and "could not say which files" in why and str(st) in why
    assert calls == [1]


def test_without_any_answer_from_git_the_store_is_not_trusted(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    _record(st)
    calls = _count_ls_files(monkeypatch, fail=subprocess.TimeoutExpired)
    _fresh()
    why = consent.store_refusal(project, st, check_git="cached")
    assert why and "could not say which files" in why
    _fresh()
    assert consent.store_refusal(project, st) == why         # the commands, the MCP server and the viewer too
    assert len(calls) == 2
    from devmem.sessions import LOAD_REFUSED, load_sessions
    _fresh()
    assert load_sessions(st, project=project) == []
    assert LOAD_REFUSED and "could not say which files" in LOAD_REFUSED[0]


def test_a_failure_is_remembered_for_a_minute_or_until_the_index_changes(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    calls = _count_ls_files(monkeypatch, fail=subprocess.TimeoutExpired)
    for _ in range(3):
        _fresh()
        assert consent.store_refusal(project, st, check_git="cached")
    assert len(calls) == 1                           # the hooks: one git call per minute at most
    cache = consent._checked_path(st)
    d = json.loads(cache.read_text(encoding="utf-8"))
    d["failed"]["at"] -= consent.FAILURE_SECONDS + 1
    cache.write_text(json.dumps(d), encoding="utf-8")
    _fresh()
    assert consent.store_refusal(project, st, check_git="cached")
    assert len(calls) == 2                           # a minute later: asked again
    (project / "other.py").write_text("y = 2\n", encoding="utf-8")
    _git(project, "add", "other.py")
    _fresh()
    assert consent.store_refusal(project, st, check_git="cached")
    assert len(calls) == 3                           # the index changed: asked again
    monkeypatch.setattr(store, "git_run", REAL_GIT_RUN)
    _fresh()
    assert consent.store_refusal(project, st, check_git="cached")      # still within the minute, same index
    d = json.loads(cache.read_text(encoding="utf-8"))
    d["failed"]["at"] -= consent.FAILURE_SECONDS + 1
    cache.write_text(json.dumps(d), encoding="utf-8")
    _fresh()
    assert consent.store_refusal(project, st, check_git="cached") is None   # git works again: trusted again


def test_a_command_asks_git_again_after_a_failure(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    calls = _count_ls_files(monkeypatch, fail=subprocess.TimeoutExpired)
    _fresh()
    assert consent.store_refusal(project, st, check_git="cached")
    monkeypatch.setattr(store, "git_run", REAL_GIT_RUN)
    _fresh()
    assert consent.store_refusal(project, st) is None        # a command, the MCP server, the viewer: asked again
    assert len(calls) == 1


def test_without_git_devmem_reads_the_index_itself(tmp_path, monkeypatch):
    """No git on the PATH the hooks see (GitHub Desktop's own git pulled the record): the index file answers, the
    first time too (reviewer's v8a/b/t13_nogit_hooks.py)."""
    project, st = _own_then_pulled(tmp_path)
    calls = _count_ls_files(monkeypatch, fail=FileNotFoundError, reader=True)
    _fresh()
    assert "tracked by git" in (consent.store_refusal(project, st, check_git="cached") or "")
    assert calls == [1]
    clean = make_project(tmp_path / "clean")
    _fresh()
    assert consent.store_refusal(clean, clean / ".devmemory", check_git="cached") is None


def test_without_git_and_an_index_devmem_cannot_read_a_work_tree_is_not_trusted(tmp_path, monkeypatch):
    project = make_project(tmp_path)               # nothing tracked in the store, but nobody can say so
    st = project / ".devmemory"
    calls = _count_ls_files(monkeypatch, fail=FileNotFoundError)
    _fresh()
    why = consent.store_refusal(project, st, check_git="cached")
    assert why and "could not say which files" in why and "not on the PATH" in why, why
    assert calls == [1]


def test_outside_a_work_tree_nothing_is_tracked_and_git_is_not_started(tmp_path, monkeypatch):
    project = make_project(tmp_path, git=False)
    st = project / ".devmemory"
    _record(st)
    calls = _count_ls_files(monkeypatch, fail=subprocess.TimeoutExpired)
    for cached in ("cached", True):
        _fresh()
        assert consent.store_refusal(project, st, check_git=cached) is None
    assert calls == []


def test_an_unchanged_index_is_not_asked_again_by_any_route(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    calls = _count_ls_files(monkeypatch)
    for cached in ("cached", True, True, "cached", True):
        _fresh()
        assert consent.store_refusal(project, st, check_git=cached) is None
    assert len(calls) == 1
    _record(st)                                      # the store changes with every event: no git call for that
    _fresh()
    assert consent.store_refusal(project, st) is None
    assert len(calls) == 1
    from devmem.mcp_server import Server
    srv = Server(project, st)
    for _ in range(3):
        srv.call("list_sessions", {})
    assert len(calls) == 1


def test_a_git_that_refuses_the_repository_names_its_reason(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    st = project / ".devmemory"

    def refuses(project, *args, timeout=store.GIT_TIMEOUT, **kw):
        return subprocess.CompletedProcess(["git"], 128, b"", b"fatal: detected dubious ownership in repository\n")
    monkeypatch.setattr(store, "git_run", refuses)
    _fresh()
    assert consent.store_refusal(project, st) is None          # the index file says: nothing tracked in the store
    _no_index_reader(monkeypatch)
    consent._checked_path(st).unlink()                        # forget that answer
    _fresh()
    why = consent.store_refusal(project, st)
    assert why and "dubious ownership" in why and "could not say which files" in why


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_hook_route_serves_nothing_from_a_pulled_record_whatever_git_does(tmp_path, monkeypatch):
    """The reviewer's f1b: refused with git; one prompt without git on PATH; then git again. Never served."""
    project, st = _own_then_pulled(tmp_path)
    _fresh()
    assert consent.store_refusal(project, st, check_git="cached")
    _record(st, "20261101T1100_cccccccc.meta.json")         # the hooks record into the store between prompts
    path = os.environ["PATH"]
    monkeypatch.setenv("PATH", str(tmp_path / "no-git-here"))
    store._PROGRAMS.clear()
    _fresh()
    assert consent.store_refusal(project, st, check_git="cached")
    monkeypatch.setenv("PATH", path)
    store._PROGRAMS.clear()
    for n in range(2):
        _record(st, f"20261101T120{n}_dddddddd.meta.json")
        _fresh()
        assert consent.store_refusal(project, st, check_git="cached")


def test_the_short_name_with_a_trailing_space_is_not_ruled_out_unseen(tmp_path):
    """`DEVMEM~1 ` names the store on Windows (as a last component); _Level strips what Windows drops before it
    compares with the short name, as _may_alias does for the long name, so the folder is looked up, not ruled out."""
    if os.name != "nt":
        pytest.skip("8.3 short names: Windows")
    project = make_project(tmp_path)
    st = project / ".devmemory"
    short = _short_name(st)
    if short is None:
        pytest.skip("no 8.3 short names here")
    lv = store._Level(str(project), ".devmemory")
    for c in (short + " ", short + ".", short + "::$INDEX_ALLOCATION", short + ". .", short.lower() + " "):
        assert not lv.rules_out(c), c
    assert lv.rules_out(short[:-1] + "9") and lv.rules_out("D~1")


def test_a_generated_short_name_is_looked_up_even_where_letter_case_is_kept_apart(tmp_path, monkeypatch):
    """The reviewer's b2: a Linux process on a case-sensitive Windows folder (a Docker bind mount, WSL's drvfs) sees
    `.DEVMEMORY` not resolve but `DEVMEM~1` resolve to the store. `keeps case apart` was taken for `makes no short
    names`: such entries were not even listed, and a pulled record was served. Now every component in the form a
    generated short name of the store can take is listed and compared with the store on disk (here a stand-in for
    the file system says which name resolves to it)."""
    project = make_project(tmp_path)
    st = project / ".devmemory"
    rec = _record(st)
    for path in (f"DEVMEM~1/sessions/{rec.name}", f"d~000001/sessions/{rec.name}", f"d12~1/{rec.name}"):
        _index_entry(project, path, rec)
    monkeypatch.setattr(store, "_short_name", lambda p: (False, None))   # as a process that cannot ask Windows
    real_same = store._same_folder
    looked = []

    def same(q, store_real, want):
        looked.append(os.path.basename(q))
        return os.path.basename(q) == "DEVMEM~1" or real_same(q, store_real, want)
    monkeypatch.setattr(store, "_same_folder", same)
    _fresh()
    got = store.store_index_entries(project, st)
    assert [g.split(" ", 1)[1] for g in got] == [f"DEVMEM~1/sessions/{rec.name}"]
    assert looked == ["DEVMEM~1"], looked                   # d~000001 and d12~1: not even listed
    tilde = store._tilde_rule(str(st.resolve()), {".devmemory"})
    raw = store._candidates_raw(project, st, 10.0, 1, tilde)
    assert sorted(p.decode() for _o, p in raw) == [f"DEVMEM~1/sessions/{rec.name}"]


def test_generated_short_name_forms():
    shapes = store._generated_short_names(".devmemory")
    for c in ("DEVMEM~1", "devmem~2", "DEVMEM~1.", "DEVMEM~1 ", "DEVMEM~1::$INDEX_ALLOCATION", "DEVME~12",
              "D~123456", "DE3F2A~1", "DEVMEM~1.ABC"):
        assert shapes.could_be(c), c
    for c in ("d~000001", "d~1", "d12~1", "DEVMEM~0", "DEVMEM~1.ABCD", "XDEVMEM~1", "DEVMEM~1~", "DEG3F2A~1"):
        assert not shapes.could_be(c), c
    assert store._generated_short_names("caf\u00e9") is None          # not modelled: every `~` entry is listed


@pytest.mark.skipif(os.name != "nt", reason="NTFS per-folder case sensitivity")
def test_a_short_name_in_a_case_sensitive_windows_folder_counts(tmp_path):
    folder = tmp_path / "cs"
    folder.mkdir()
    r = subprocess.run(["fsutil", "file", "setCaseSensitiveInfo", str(folder), "enable"], capture_output=True)
    if r.returncode:
        pytest.skip("fsutil cannot mark a folder case-sensitive here")
    project = make_project(folder)
    st = project / ".devmemory"
    rec = _record(st)
    name = _short_name(st)
    if name is None:
        pytest.skip("no 8.3 short names here")
    assert not (project / ".DEVMEMORY").exists() and (project / name).exists()
    path = f"{name}/sessions/{rec.name}"
    _index_entry(project, path, rec)
    _fresh()
    assert [g.split(" ", 1)[1] for g in store.store_index_entries(project, st)] == [path]
    _fresh()
    assert consent.store_refusal(project, st) and "tracked by git" in consent.store_refusal(project, st)


def test_names_holding_a_code_point_hfs_ignores_are_asked_for_on_the_git_route(tmp_path):
    """HFS+ takes `.dev<U+200C>memory` for `.devmemory`. The git route did not list such entries (devmem's reader
    did); now both do, and where they land is decided on disk (here: another folder, so nothing counts). A Mac was
    not available: this checks what git is asked for and lists, not HFS+ itself."""
    project = make_project(tmp_path)
    st = project / ".devmemory"
    rec = _record(st)
    path = f".dev\u200cmemory/sessions/{rec.name}"
    _index_entry(project, path, rec)
    _fresh()
    tilde = store._tilde_rule(str(st.resolve()), {".devmemory"})
    for depth in (1, None):
        listed = [p.decode() for _o, p in store._candidates_raw(project, st, 10.0, depth, tilde)]
        assert path in listed, (depth, listed)
    assert store.store_index_entries(project, st) == [] or sys.platform == "darwin"
