"""What a hook stores of git's state is small and fixed, whatever `git status` lists.

A hook takes git's list of changed paths at SessionStart and compares it at every Stop. That list used to be stored
whole in the session state (rewritten at every event) and in the event log, and every path went through the
redactor: an archive whose `.git` index listed 1,600 absent files with 3.8 KB paths (6 MB of `git status`) made
SessionStart take 35-46 s and UserPromptSubmit 52 s. Now the bytes a hook hands to the write boundary
(Redactor.record) per event stay under a fixed number, counted here with a stand-in, for every hostile shape: many
changed paths, long paths, deep paths, many untracked files, a status up to the output cap."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time

import pytest
from hooks_helpers import make_project

from devmem import consent, hooks, redact, sessions, store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
SID = "6b0b0b0b-0000-4000-8000-000000000001"
# what one event may hand to the write boundary: a few small lists of STORED_PATHS_BYTES each, the session's own
# fields and one record (whose lists are bounded the same way); independent of the project
PER_EVENT_BYTES = 8 * sessions.STORED_PATHS_BYTES + 64 * 1024


def _git(project, *args, input=None):
    return subprocess.run(["git", "-C", str(project), *args], input=input, capture_output=True, check=True).stdout


def _entries(project, paths) -> None:
    """Index entries only (no files: `git status` lists them as deleted), as a shipped `.git` index holds them."""
    blob = _git(project, "rev-parse", "HEAD:greet.py").decode().strip()
    _git(project, "update-index", "--add", "--index-info",
         input="".join(f"100644 {blob}\t{p}\n" for p in paths).encode("utf-8"))


class _Count:
    def __init__(self, monkeypatch):
        self.bytes = 0
        real = redact.Redactor.record

        def record(red, obj, *a, **k):
            self.bytes += len(json.dumps(obj, ensure_ascii=False).encode("utf-8", errors="surrogatepass"))
            return real(red, obj, *a, **k)
        monkeypatch.setattr(redact.Redactor, "record", record)


def _hook(project, event, **payload) -> None:
    store._FILTERS.clear()
    consent.forget_decisions()
    code, _ = hooks.run([event], json.dumps({"session_id": SID, "cwd": str(project), "hook_event_name": event,
                                              "transcript_path": str(project / "none.jsonl"),
                                              **payload}).encode("utf-8"))
    assert code == 0


EVENTS = (("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": "explain greet"}),
          ("Stop", {"last_assistant_message": "ok"}), ("UserPromptSubmit", {"prompt": "and again"}),
          ("Stop", {"last_assistant_message": "ok"}), ("SessionEnd", {"reason": "other"}))


def _session(project, monkeypatch, between=None) -> dict[str, int]:
    """Each event's bytes at the write boundary; `between` runs after the second prompt (the work tree changes)."""
    count = _Count(monkeypatch)
    out = {}
    for i, (ev, pl) in enumerate(EVENTS):
        before = count.bytes
        _hook(project, ev, **pl)
        out[f"{i}:{ev}"] = count.bytes - before
        if i == 3 and between:      # during the second turn (after its prompt): what the session itself changes
            between()
    return out


def _state_size(project) -> int:
    return (project / ".devmemory" / "live" / f"{SID}.state.json").stat().st_size


def _shape(kind: str):
    if kind == "many":          # many ordinary changed paths
        return [f"packages/app-{i % 50}/src/feature/module_{i:06d}_handler.tsx" for i in range(6000)]
    if kind == "long":          # 3.8 KB paths (hex components), as in the reviewer's archive
        return [f"h{i:05d}/" + "/".join(f"{i:04x}{j:02x}" * 25 for j in range(19)) for i in range(600)]
    return [f"d{i:03d}/" + "/".join(["a"] * 15000) for i in range(30)]     # deep: 30 KB paths of 15,000 folders


@pytest.mark.parametrize("kind", ["many", "long", "deep"])
def test_the_bytes_a_hook_stores_do_not_grow_with_git_status(tmp_path, monkeypatch, kind):
    project = make_project(tmp_path)
    paths = _shape(kind)
    _entries(project, paths)
    st = sessions.git_status(project)
    if not st or len(st) < len(paths):
        pytest.skip("git on this machine does not list these paths (MAX_PATH)")
    status_bytes = sum(len(p) for p in st)
    assert status_bytes > 2 * PER_EVENT_BYTES or kind == "many"   # old: the list in the state and the event

    def change():   # half the entries go, as many new ones appear: the Stop after it compares two large lists
        zero = "0" * 40
        _git(project, "update-index", "--index-info",
             input="".join(f"0 {zero}\t{p}\n" for p in paths[: len(paths) // 2]).encode("utf-8"))
        _entries(project, [p + "x" for p in paths[: len(paths) // 2]])

    sizes = _session(project, monkeypatch, change)
    assert max(sizes.values()) < PER_EVENT_BYTES, sizes
    assert _state_size(project) < 16 * 1024          # the live state keeps no path list
    # the record lists some of the deletions git showed during the turn and counts the rest
    rec = json.loads(next((project / ".devmemory" / "sessions").glob("*.meta.json")).read_text(encoding="utf-8"))
    t0, t = rec["turns"][0], rec["turns"][1]
    assert not t0["files_deleted"]                   # deleted before the session started: not the session's
    # git lists the new entries as deleted where it can look the path up, and as added only where it cannot (Linux:
    # a path past PATH_MAX, which then has no time to date it by: counted, not attributed); either way the turn holds
    # at most STORED_PATHS of them and counts the rest
    obs = t.get("files_changed_observed") or {}
    deleted, changed = t["files_deleted"], obs.get("files") or []
    undated = sum(v for k, v in (t.get("git_unattributed") or {}).items() if k.startswith("changed_"))
    assert len(deleted) <= sessions.STORED_PATHS and len(changed) <= sessions.STORED_PATHS
    assert (len(deleted) + int(t.get("files_deleted_more") or 0) + len(changed) + int(obs.get("more") or 0)
            + undated == len(paths) // 2)
    assert all(len(f) <= sessions.STORED_PATH_CHARS + 1 for f in deleted + changed)
    assert rec["git"].get("dirty_start") is True and "status_too_large" not in rec["git"]


def test_many_untracked_files(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    d = project          # files, not a folder: `git status` shows an untracked folder as one entry
    for i in range(2500):
        (d / f"untracked_{i:05d}_{'u' * 60}.txt").write_text("x", encoding="utf-8")
    assert len(sessions.git_status(project, untracked="all") or {}) >= 2500

    def more():
        for i in range(2500, 4000):
            (d / f"untracked_{i:05d}_{'u' * 60}.txt").write_text("x", encoding="utf-8")
    sizes = _session(project, monkeypatch, more)
    assert max(sizes.values()) < PER_EVENT_BYTES, sizes
    assert _state_size(project) < 16 * 1024
    rec = json.loads(next((project / ".devmemory" / "sessions").glob("*.meta.json")).read_text(encoding="utf-8"))
    obs = rec["turns"][1].get("files_changed_observed") or {}
    assert len(obs["files"]) + obs.get("more", 0) == 1500 and len(obs["files"]) <= sessions.STORED_PATHS


def test_a_status_up_to_the_output_cap(tmp_path, monkeypatch):
    """git_status stands in for a `git status` just under both caps: MAX_STATUS_ENTRIES paths, about
    MAX_STATUS_BYTES of them. Nothing of that size reaches the write boundary or the state."""
    project = make_project(tmp_path)
    n = sessions.MAX_STATUS_ENTRIES
    width = sessions.MAX_STATUS_BYTES // n - 8
    big = {f"p{i:06d}/" + "q" * (width - 8): " D" for i in range(n)}
    calls = {"n": 0}

    def status(project_, untracked="normal"):
        calls["n"] += 1
        if calls["n"] <= 2:
            return dict(big), False
        return {k + "z": v for k, v in big.items()}, False      # all different after the first Stop
    monkeypatch.setattr(sessions, "git_status_sized", status)
    assert sum(len(p) for p in big) > sessions.MAX_STATUS_BYTES * 0.9
    sizes = _session(project, monkeypatch)
    assert max(sizes.values()) < PER_EVENT_BYTES, sizes
    assert _state_size(project) < 16 * 1024
    seen = project / ".devmemory" / "live" / f"{SID}.gitseen"
    assert seen.stat().st_size <= 16 + 8 * 3 * n                  # 8 bytes a path, never the path's text


def test_a_status_past_the_entry_cap_counts_entries_not_separators(tmp_path, monkeypatch):
    """The cap counts entries (the figure docs/LIMITS.md and `devmem status` give), not NUL separators: a status of n
    entries, one of them a rename (two paths), is over a cap of n - 1."""
    project = make_project(tmp_path)
    _entries(project, [f"gone/f{i}.py" for i in range(30)])
    _git(project, "mv", "greet.py", "greet2.py")
    n = len(sessions.git_status(project) or {})
    assert n >= 31
    monkeypatch.setattr(sessions, "MAX_STATUS_ENTRIES", n)
    st, too_large = sessions.git_status_sized(project)
    assert st is not None and len(st) == n and not too_large
    monkeypatch.setattr(sessions, "MAX_STATUS_ENTRIES", n - 1)
    assert sessions.git_status_sized(project) == (None, True)


def test_an_older_state_with_path_lists_is_shrunk_and_still_compared(tmp_path, monkeypatch):
    """A session a devmem before 0.3.1 started kept the lists in its state: they are taken out at the next Stop and
    still used for that comparison (a file dirty at the start is not reported as changed by the session)."""
    project = make_project(tmp_path)
    (project / "greet.py").write_text("changed before\n", encoding="utf-8")
    _hook(project, "SessionStart", source="startup")
    live = project / ".devmemory" / "live"
    (live / f"{SID}.gitseen").unlink()
    stf = live / f"{SID}.state.json"
    st = json.loads(stf.read_text(encoding="utf-8"))
    st["git_start"].update({"deleted": [], "dirty_paths": ["greet.py"]})
    st["git_last_dirty"] = ["greet.py"]
    st["reported_deleted"] = []
    stf.write_text(json.dumps(st), encoding="utf-8")
    _hook(project, "UserPromptSubmit", prompt="explain greet")
    (project / "farewell.py").write_text("x = 1\n", encoding="utf-8")
    _hook(project, "Stop", last_assistant_message="ok")
    st = json.loads(stf.read_text(encoding="utf-8"))
    assert "git_last_dirty" not in st and "reported_deleted" not in st and "dirty_paths" not in st["git_start"]
    rec = json.loads(next((project / ".devmemory" / "sessions").glob("*.meta.json")).read_text(encoding="utf-8"))
    assert rec["turns"][0]["files_changed_observed"]["files"] == ["farewell.py"]


def test_bounded_paths():
    paths = [f"{i}/" + "x" * 1000 for i in range(1000)]
    kept, more = sessions.bounded_paths(paths)
    assert len(kept) + more == 1000 and len(kept) <= sessions.STORED_PATHS
    assert sum(len(p.encode()) for p in kept) <= sessions.STORED_PATHS_BYTES
    assert all(p.endswith(sessions.CUT_MARK) and len(p) == sessions.STORED_PATH_CHARS + 1 for p in kept)
    assert sessions.bounded_paths(["a", "b"]) == (["a", "b"], 0)


def test_reviewer_archive_end_to_end(tmp_path):
    """The reviewer's shape as Claude Code runs it (a process per event): 1,600 absent files with 3.8 KB paths in the
    index. Before: SessionStart 35-46 s, UserPromptSubmit 52 s. A generous bound: the assertion that matters is the
    byte count above."""
    project = make_project(tmp_path)
    _entries(project, [f"h{i:06d}/" + "/".join(f"{i:04x}{j:02x}" * 33 for j in range(19)) for i in range(1600)])
    st = sessions.git_status(project)
    if not st or len(st) < 1600:
        pytest.skip("git on this machine does not list these paths (MAX_PATH)")
    import os
    from pathlib import Path
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project), "PYTHONPATH": str(Path(hooks.__file__).parents[1])}
    worst = 0.0
    for ev, pl in EVENTS[:3]:
        t = time.perf_counter()
        r = subprocess.run([sys.executable, "-m", "devmem", "hook", ev], capture_output=True, env=env, cwd=str(project),
                           input=json.dumps({"session_id": SID, "cwd": str(project), "hook_event_name": ev,
                                             "transcript_path": str(project / "none.jsonl"), **pl}).encode(),
                           timeout=120)
        worst = max(worst, time.perf_counter() - t)
        assert r.returncode == 0, r.stderr
    assert worst < 20.0, worst
    assert _state_size(project) < 16 * 1024


def _record_of(project) -> dict:
    return json.loads(next((project / ".devmemory" / "sessions").glob("*.meta.json")).read_text(encoding="utf-8"))


def test_a_start_git_could_not_describe_attributes_nothing_that_was_there_before(tmp_path, monkeypatch):
    """The reviewer's h3 toolarge: git status was too large at SessionStart, the list came under the cap during the
    turn, and the Stop attributed every deletion that existed before the session to it. The start is unknown: nothing
    git shows is dated then; the turn counts it, the record says the start was unknown, and a deletion after that
    known check is attributed at the next Stop."""
    monkeypatch.setattr(sessions, "MAX_STATUS_ENTRIES", 20)
    project = make_project(tmp_path)
    paths = [f"absent/f{i:03d}.py" for i in range(25)]
    _entries(project, paths)
    _hook(project, "SessionStart", source="startup")
    assert not (project / ".devmemory" / "live" / f"{SID}.gitseen").exists()
    _hook(project, "UserPromptSubmit", prompt="explain greet")
    zero = "0" * 40
    _git(project, "update-index", "--index-info",
         input="".join(f"0 {zero}\t{p}\n" for p in paths[:6]).encode("utf-8"))     # now 19 + nothing else: under
    _hook(project, "Stop", last_assistant_message="ok")
    rec = _record_of(project)
    t0 = rec["turns"][0]
    assert t0["files_deleted"] == [] and not t0.get("files_changed_observed"), t0
    assert t0["git_unattributed"]["deleted"] == 19 and rec["git"]["start_unknown"] is True
    _hook(project, "UserPromptSubmit", prompt="now delete greet")
    (project / "greet.py").unlink()
    _hook(project, "Stop", last_assistant_message="deleted")
    t1 = _record_of(project)["turns"][1]
    assert t1["files_deleted"] == ["greet.py"] and "git_unattributed" not in t1, t1


@pytest.mark.parametrize("damage", ["garbage", "counts", "large", "folder", "missing"])
def test_a_damaged_gitseen_is_unknown_never_empty(tmp_path, monkeypatch, damage):
    project = make_project(tmp_path)
    _entries(project, [f"absent/f{i:03d}.py" for i in range(12)])           # deleted before the session
    _hook(project, "SessionStart", source="startup")
    seen = project / ".devmemory" / "live" / f"{SID}.gitseen"
    seen.unlink()
    if damage == "garbage":
        seen.write_bytes(b"not a gitseen file")
    elif damage == "counts":
        seen.write_bytes(b"DMS1" + (5).to_bytes(4, "little") * 3 + b"x" * 16)
    elif damage == "large":
        seen.write_bytes(b"DMS1" + b"\0" * (16 + 8 * 3 * sessions.MAX_STATUS_ENTRIES))
    elif damage == "folder":
        seen.mkdir()
    _hook(project, "UserPromptSubmit", prompt="explain greet")
    sleeps = []
    monkeypatch.setattr(sessions.time, "sleep", lambda s: sleeps.append(s))
    _hook(project, "Stop", last_assistant_message="ok")
    assert sleeps == [], sleeps                       # a folder there: no waiting on os.replace
    t0 = _record_of(project)["turns"][0]
    assert t0["files_deleted"] == [] and t0["git_unattributed"]["deleted"] == 12, t0
    assert seen.is_dir() if damage == "folder" else seen.is_file()
