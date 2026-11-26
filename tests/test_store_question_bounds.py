"""The question "which files of the store does git track" costs a bounded amount, whatever git's index holds.

A project shipped with a hand-made `.git/index` of hundreds of thousands of entries like `d<i>~1/f.py` (entries only,
no files; the `~` matches the 8.3 short-name pathspec) used to make devmem look up every one of those folders on disk
after git answered, with no limit: SessionStart 22 s, a prompt after an index change 17-23 s, the MCP
`get_context_pack` 33-37 s (reviewer's v8a/a/t8_idx_win.py, bigidx3.py). Now one deadline covers git and devmem's own
work, more than MAX_STORE_CANDIDATES listed entries or MAX_STORE_FOLDERS distinct folders to compare count as "git
could not say", and a component that cannot name the store is never looked up. These tests count look-ups instead of
measuring time.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import consent, store

G = ["-c", "core.autocrlf=false", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]


def _git(where: Path, *args: str, input: bytes | None = None) -> str:
    return subprocess.run(["git", "-C", str(where), *G, *args], capture_output=True, input=input,
                          check=True).stdout.decode("utf-8", "replace")


def _entries(project: Path, paths) -> None:
    """Index entries only (no files), as a hand-made index holds them."""
    blob = _git(project, "rev-parse", "HEAD:greet.py").strip()
    _git(project, "update-index", "--add", "--index-info",
         input="".join(f"100644 {blob}\t{p}\n" for p in paths).encode("utf-8"))


def _fresh() -> None:
    store._FILTERS.clear()
    store._git_down_until = 0.0
    consent.forget_decisions()


@pytest.fixture()
def lookups(monkeypatch):
    calls: list[str] = []
    real = store._same_folder

    def counting(q, store_real, want):
        calls.append(q)
        return real(q, store_real, want)
    monkeypatch.setattr(store, "_same_folder", counting)
    return calls


def test_many_short_name_folders_are_ruled_out_without_a_look_up(tmp_path, monkeypatch, lookups):
    """Names that merely hold `~` are ruled out by what the file system says once (store._Level)."""
    project = make_project(tmp_path)
    _entries(project, [f"d{i}~1/f.py" for i in range(200)])
    _fresh()
    assert store.store_index_entries(project, project / ".devmemory") == []
    assert lookups == []


def test_folders_that_cannot_be_ruled_out_are_a_limit_not_a_look_up_each(tmp_path, monkeypatch, lookups):
    """The backstop, where the file system cannot rule a spelling out (MAX_STORE_FOLDERS)."""
    monkeypatch.setattr(store, "MAX_STORE_FOLDERS", 50)
    monkeypatch.setattr(store._Level, "rules_out", lambda self, c: False)
    monkeypatch.setattr(store, "_tilde_rule", lambda *a: None)      # git lists every `~` entry
    project = make_project(tmp_path)
    _entries(project, [f"d{i}~1/f.py" for i in range(200)])
    _fresh()
    with pytest.raises(store.GitLimit, match="folders"):
        store.store_index_entries(project, project / ".devmemory")
    assert len(lookups) <= 50
    _fresh()
    why = consent.store_refusal(project, project / ".devmemory")
    assert consent.is_git_limit(why) and "folders" in why and "does not clear by itself" in why, why
    checked = json.loads(consent._checked_path(project / ".devmemory").read_text(encoding="utf-8"))
    assert "failed" in checked and "ok" not in checked          # remembered as a failure, never as "nothing tracked"


def test_more_listed_entries_than_the_cap_is_git_could_not_say(tmp_path, monkeypatch, lookups):
    monkeypatch.setattr(store, "MAX_STORE_CANDIDATES", 100)
    project = make_project(tmp_path)
    _entries(project, [f".devmemory/sessions/r{i}.meta.json" for i in range(150)])
    _fresh()
    with pytest.raises(store.GitNoAnswer, match="entries"):
        store.store_index_entries(project, project / ".devmemory")
    assert lookups == []
    _fresh()
    why = consent.store_refusal(project, project / ".devmemory")
    assert consent.is_git_limit(why), why


def test_the_deadline_covers_devmems_own_work_after_git(tmp_path):
    raw = [("0" * 40, f"d{i}~1/f.py".encode()) for i in range(3000)]
    base = str(tmp_path)
    with pytest.raises(store.GitNoAnswer, match="in time"):
        store._landing(raw, base, str(tmp_path / ".devmemory"), None, time.monotonic() - 1)


def test_components_that_cannot_name_the_store_are_not_looked_up(tmp_path, lookups):
    project = make_project(tmp_path)
    (project / ".devmemory" / "sessions").mkdir()
    _entries(project, [f"src/backup~{i}/x.py" for i in range(300)]            # deep `~` folders: honest repositories
             + [f"notes{i}~/a.txt" for i in range(3)]                        # `~` at the store's depth: looked up
             + [f".devmemory/sessions/r{i}.meta.json" for i in range(300)]    # the store's own spelling: by string
             + [f".DevMemory/sessions/x{i}.meta.json" for i in range(300)]    # another case: one look-up for all
             + [f"other{i}/.devmemory/y" for i in range(300)])               # the name deeper down: not the store
    _fresh()
    got = store.store_index_entries(project, project / ".devmemory")
    assert len([g for g in got if "/r" in g]) == 300
    assert {Path(q).name for q in lookups} <= {".DevMemory", "notes0~", "notes1~", "notes2~"}
    assert len(lookups) <= 4


def test_with_the_top_known_git_is_asked_only_to_the_stores_depth(tmp_path):
    project = make_project(tmp_path)
    _entries(project, ["src/deep~1/x.py", "a/b/c/.devmemory/z.json", ".devmemory/sessions/r.meta.json"])
    assert store._top_is(project)
    _fresh()
    listed = store._candidates_raw(project, project / ".devmemory", 5.0, depth=1)
    names = sorted(p.decode() for _oid, p in listed)
    assert names == [".devmemory/sessions/r.meta.json"]
    _fresh()
    assert len(store._candidates(project, project / ".devmemory")) == 3        # the broad set, from the top


def test_an_unusual_layout_is_asked_with_the_broad_pathspecs(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    assert store._top_is(project)
    with open(project / ".git" / "config", "a", encoding="utf-8") as f:
        f.write("[include]\n\tpath = other.cfg\n")
    assert not store._top_is(project)
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    assert not store._top_is(tmp_path)


def test_a_status_listing_with_more_changes_than_the_cap_is_not_kept(tmp_path, monkeypatch):
    """The hooks keep git's list of changed paths and compare it at every event: a hand-made index of a million
    entries that are not on disk made that list a million long."""
    from devmem import sessions
    project = make_project(tmp_path)
    _entries(project, [f"gone/f{i}.py" for i in range(30)])
    assert len(sessions.git_status(project) or {}) >= 30
    monkeypatch.setattr(sessions, "MAX_STATUS_ENTRIES", 10)
    assert sessions.git_status(project) is None
    snap = sessions.git_snapshot(project)                # the record says why git's data is missing
    assert snap["status_too_large"] is True and "dirty_paths" not in snap


def test_a_file_list_longer_than_the_cap_is_walked_instead(tmp_path, monkeypatch):
    """The refresh lists the project's files through git and looks at every name: a hand-made index of 240,000 long
    names that are not on disk kept every prompt at the refresh's 8 s budget. Past MAX_GIT_LISTED the folder itself is
    walked (here: the files that are really there)."""
    project = make_project(tmp_path)
    _entries(project, [f"gone/{'x' * 50}{i}.py" for i in range(30)])
    assert store.list_files(project)[1] == "git"
    monkeypatch.setattr(store, "MAX_GIT_LISTED", 10)
    files, how = store.list_files(project)
    assert how == "walk" and {f for f, _s, _t in files} >= {"greet.py"}
