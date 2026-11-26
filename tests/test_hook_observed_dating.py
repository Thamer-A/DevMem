"""What a Stop attributes from git between two checks (devmem.hooks._observe_files, _dated): a path that newly
appears in git's list counts as changed by the session only with a modification time inside the interval since the
previous check. The reviewer's h5: the session edited `.gitignore`, and files that existed before it (unchanged)
were listed as the turn's `files_changed_observed`."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest
from hooks_helpers import make_project

from devmem import consent, hooks, store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
SID = "7d7d7d7d-0000-4000-8000-000000000001"
DAY = 86400


def _hook(project, event, **payload) -> None:
    store._FILTERS.clear()
    consent.forget_decisions()
    code, _ = hooks.run([event], json.dumps({"session_id": SID, "cwd": str(project), "hook_event_name": event,
                                              "transcript_path": str(project / "none.jsonl"),
                                              **payload}).encode("utf-8"))
    assert code == 0


def _git(project, *args):
    subprocess.run(["git", "-C", str(project), "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def _turn(project, n=0) -> dict:
    rec = json.loads(next((project / ".devmemory" / "sessions").glob("*.meta.json")).read_text(encoding="utf-8"))
    return rec["turns"][n]


def _write(project, rel, text, mtime=None):
    f = project / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(f, (mtime, mtime))


def _start(project):
    _hook(project, "SessionStart", source="startup")
    _hook(project, "UserPromptSubmit", prompt="explain greet")


def _changed(project, n=0) -> list[str]:
    return (_turn(project, n).get("files_changed_observed") or {}).get("files") or []


def test_files_the_session_only_un_ignored_are_counted_not_attributed(tmp_path):
    project = make_project(tmp_path)
    _write(project, ".gitignore", "*.cfg\n*.log\n")
    _git(project, "add", ".gitignore")
    _git(project, "commit", "-qm", "ignore")
    now = time.time()
    _write(project, "local.cfg", "k = 1\n", now - 1)          # written just before the session
    _write(project, "old.log", "x\n", now - DAY)              # a day old
    _write(project, "ahead.cfg", "y\n", now + DAY)            # dated in the future (an archive from a clock ahead)
    _start(project)
    _write(project, ".gitignore", "")                         # the session's only change
    _hook(project, "Stop", last_assistant_message="ok")
    assert _changed(project) == [".gitignore"], _turn(project)
    un = _turn(project)["git_unattributed"]
    assert un["changed_not_in_interval"] == 3 and "deleted" not in un, un


def test_a_file_written_during_the_turn_is_attributed(tmp_path):
    project = make_project(tmp_path)
    _start(project)
    time.sleep(0.05)
    _write(project, "new_mod.py", "def f():\n    return 1\n")
    _hook(project, "Stop", last_assistant_message="ok")
    assert _changed(project) == ["new_mod.py"] and "git_unattributed" not in _turn(project)


def test_a_file_put_there_with_an_old_time_during_the_turn_is_counted(tmp_path):
    """A real change git sees only by a time from before the turn (unpacked from an archive, copied keeping its time):
    there is no evidence it was made in the turn, so it is counted, not attributed."""
    project = make_project(tmp_path)
    _start(project)
    _write(project, "lib_old.py", "x = 1\n", time.time() - 30 * DAY)
    _hook(project, "Stop", last_assistant_message="ok")
    assert _changed(project) == [] and _turn(project)["git_unattributed"]["changed_not_in_interval"] == 1


def test_an_ignored_file_touched_in_the_turn_then_un_ignored_is_attributed(tmp_path):
    """Its time is inside the turn: modified in it, as far as the file system says (the same rule as begin/end)."""
    project = make_project(tmp_path)
    _write(project, ".gitignore", "*.cfg\n")
    _git(project, "add", ".gitignore")
    _git(project, "commit", "-qm", "ignore")
    _write(project, "local.cfg", "k = 1\n", time.time() - DAY)
    _start(project)
    time.sleep(0.05)
    os.utime(project / "local.cfg")                            # touched, content unchanged
    _write(project, ".gitignore", "")
    _hook(project, "Stop", last_assistant_message="ok")
    assert sorted(_changed(project)) == [".gitignore", "local.cfg"]


def test_a_dirty_file_hidden_by_an_ignore_change_is_neither_changed_nor_deleted(tmp_path):
    project = make_project(tmp_path)
    _write(project, "scratch.tmp", "notes\n", time.time() - DAY)        # untracked and dirty before the session
    _start(project)
    _write(project, ".gitignore", "*.tmp\n")
    _hook(project, "Stop", last_assistant_message="ok")
    t = _turn(project)
    assert t["files_deleted"] == [] and _changed(project) == [".gitignore"], t


def test_a_dirty_file_restored_by_git_checkout_leaves_the_list_without_a_record(tmp_path):
    """`git checkout -- f` in the turn: f was dirty and is clean now. It leaves git's list; no path newly appears, so
    nothing is attributed from git (a revert is not recorded as a change; the session's own tool call names it)."""
    project = make_project(tmp_path)
    (project / "greet.py").write_text("changed before the session\n", encoding="utf-8")
    _start(project)
    _git(project, "checkout", "--", "greet.py")
    _hook(project, "Stop", last_assistant_message="ok")
    t = _turn(project)
    assert t["files_deleted"] == [] and _changed(project) == [] and "git_unattributed" not in t, t


def test_at_most_a_bounded_number_of_new_paths_are_looked_at(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "OBSERVED_STATS_PER_CHECK", 2)
    project = make_project(tmp_path)
    _start(project)
    looked = []
    real = os.lstat

    def counted(p, *a, **k):
        if str(p).endswith(".py") and "new" in os.path.basename(str(p)):
            looked.append(p)
        return real(p, *a, **k)
    time.sleep(0.05)
    for i in range(5):
        _write(project, f"new{i}.py", f"x = {i}\n")
    monkeypatch.setattr(hooks.os, "lstat", counted)
    _hook(project, "Stop", last_assistant_message="ok")
    assert len(looked) == 2, looked
    assert _changed(project) == ["new0.py", "new1.py"]
    assert _turn(project)["git_unattributed"]["changed_not_looked_at"] == 3


def _unattributed(project, n=0) -> dict:
    return _turn(project, n).get("git_unattributed") or {}


def _state_path(project):
    return project / ".devmemory" / "live" / f"{SID}.state.json"


def _edit_state(project, **kv):
    f = _state_path(project)
    st = json.loads(f.read_text(encoding="utf-8"))
    for k, v in kv.items():
        if v is _DROP:
            st.pop(k, None)
        else:
            st[k] = v
    f.write_text(json.dumps(st), encoding="utf-8")


_DROP = object()


def test_a_new_folder_made_in_the_turn_is_attributed(tmp_path):
    """The reviewer's c1 many / c2: git lists a new untracked folder once, as `gen/`; it was counted with the false
    reason "time outside the turn". A folder is dated by its own time (it changes when entries are added)."""
    project = make_project(tmp_path)
    _start(project)
    time.sleep(0.05)
    for i in range(30):
        _write(project, f"gen/f{i}.py", "x\n")
    _hook(project, "Stop", last_assistant_message="ok")
    assert _changed(project) == ["gen/"] and not _unattributed(project), _turn(project)


def test_an_old_folder_the_turn_only_un_ignored_is_counted_as_a_folder(tmp_path):
    project = make_project(tmp_path)
    _write(project, ".gitignore", "build/\n")
    _git(project, "add", ".gitignore")
    _git(project, "commit", "-qm", "ignore")
    old = time.time() - DAY
    _write(project, "build/out.txt", "o\n", old)
    os.utime(project / "build", (old, old))
    _start(project)
    _write(project, ".gitignore", "")
    _hook(project, "Stop", last_assistant_message="ok")
    assert _changed(project) == [".gitignore"] and _unattributed(project) == {
        "folder_not_in_interval": 1, "source": _unattributed(project)["source"]}, _turn(project)


def test_an_old_folder_with_a_file_changed_in_the_turn_is_attributed_by_that_file(tmp_path):
    project = make_project(tmp_path)
    _write(project, ".gitignore", "build/\n")
    _git(project, "add", ".gitignore")
    _git(project, "commit", "-qm", "ignore")
    old = time.time() - DAY
    _write(project, "build/out.txt", "o\n", old)
    os.utime(project / "build", (old, old))
    _start(project)
    time.sleep(0.05)
    (project / "build" / "out.txt").write_text("changed in the turn\n", encoding="utf-8")
    os.utime(project / "build", (old, old))          # a file's content changed: the folder's own time does not
    _write(project, ".gitignore", "")
    _hook(project, "Stop", last_assistant_message="ok")
    assert sorted(_changed(project)) == [".gitignore", "build/"], _turn(project)


def test_a_path_that_cannot_be_looked_at_has_its_own_count(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    _start(project)
    _write(project, "odd.py", "x\n")
    real = store.project_path
    monkeypatch.setattr(store, "project_path",
                        lambda pr, rel, *a: None if rel == "odd.py" else real(pr, rel, *a))
    _hook(project, "Stop", last_assistant_message="ok")
    un = _unattributed(project)
    assert _changed(project) == [] and un.get("changed_not_dated") == 1 and "changed_not_in_interval" not in un


def test_your_edit_between_turns_is_not_attributed_to_the_next_turn(tmp_path):
    """The reviewer's c1 between: the interval started at the previous Stop, so an edit while no turn ran was the next
    turn's. It starts at the turn's prompt now; the edit is counted as made between turns."""
    project = make_project(tmp_path)
    _start(project)
    _hook(project, "Stop", last_assistant_message="first")
    time.sleep(0.05)
    _write(project, "notes.md", "my own edit while idle\n")
    time.sleep(0.05)
    _hook(project, "UserPromptSubmit", prompt="second")
    _hook(project, "Stop", last_assistant_message="second")
    assert _changed(project, 1) == [] and _unattributed(project, 1)["changed_between_turns"] == 1, _turn(project, 1)


def test_your_edit_before_the_first_prompt_is_not_attributed(tmp_path):
    project = make_project(tmp_path)
    _hook(project, "SessionStart", source="startup")
    time.sleep(0.05)
    _write(project, "notes.md", "edited before the first prompt\n")
    time.sleep(0.05)
    _hook(project, "UserPromptSubmit", prompt="explain greet")
    _hook(project, "Stop", last_assistant_message="ok")
    assert _changed(project) == [] and _unattributed(project)["changed_between_turns"] == 1


def test_a_turn_whose_prompt_was_not_recorded_attributes_nothing(tmp_path):
    project = make_project(tmp_path)
    _start(project)
    _write(project, "new_mod.py", "x\n")
    _edit_state(project, prompt_at=_DROP)
    _hook(project, "Stop", last_assistant_message="ok")
    assert _changed(project) == [] and _unattributed(project)["changed_turn_start_unknown"] == 1


BAD_TIMES = [10 ** 400, -(10 ** 400), float("nan"), float("inf"), float("-inf"), "2026-01-01", [1], {"t": 1}, True,
             -1.0, 0, time.time() + 10 ** 7]


@pytest.mark.parametrize("field", ["git_checked_at", "prompt_at"])
@pytest.mark.parametrize("value", BAD_TIMES, ids=lambda v: repr(v)[:20])
def test_a_time_in_the_live_state_that_is_not_a_sane_number_is_unknown(tmp_path, field, value):
    """The reviewer's c1 state_big: git_checked_at = 10**400 raised OverflowError at every Stop of that session."""
    project = make_project(tmp_path)
    _start(project)
    time.sleep(0.05)                        # the file system's clock ticks coarser than time.time() (Windows: ~16 ms)
    _write(project, "new_mod.py", "x\n")
    _edit_state(project, **{field: value})
    _hook(project, "Stop", last_assistant_message="ok")
    assert not (project / ".devmemory" / "hook_errors.log").exists()
    t = _turn(project)
    if field == "prompt_at":                # no start for this turn: nothing attributed, counted
        assert _changed(project) == [] and _unattributed(project)["changed_turn_start_unknown"] == 1, t
    else:                                   # the previous check is unknown: the turn's own interval still dates it
        assert _changed(project) == ["new_mod.py"], t


@pytest.mark.parametrize("value", BAD_TIMES, ids=lambda v: repr(v)[:20])
def test_a_time_in_a_per_user_note_that_is_not_a_sane_number_never_raises(tmp_path, value):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    stamp = consent._git_index_stamp(project)
    for mark in ({"failed": {"stamp": stamp, "at": value, "why": consent.DIED, "died": 1}},
                 {"asking": {"stamp": stamp, "at": value, "pid": 1}}):
        consent._write_json_private(consent._checked_path(st), {"home": consent.project_key(project), **mark})
        consent.forget_decisions()
        for cached in (True, False):
            consent._tracked(project, st, cached=cached)         # no OverflowError, TypeError or ValueError
