"""Every git command has the time limit, also the one that asks which store files git tracks.

The store-trust check ran `git ls-files -s` on its own with a 30 s limit, on every prompt and every MCP call; with a
named pipe as `.git/index` (a tar archive can carry one) each prompt waited 38 s (reviewer's v6a/fifo1.py to fifo3.py).

One limit covers a whole git_run (the filter listing took its own 5 s whatever limit the caller named: a 1 s check
took 5 s), a git that ran into its limit is not started again in that project by any devmem process for a minute,
and on Linux no git outlives the process that started it (reviewer's v7a/a/hostile1.py, hostile2.py, inc1.py,
slow1.py: 8.3 s per prompt and one git left blocked on a named pipe per prompt).
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import connect, consent, proc, store

PKG = Path(store.__file__).parent


def _fresh() -> None:
    store._FILTERS.clear()
    store._git_down_until = 0.0
    store._SLOW_PATHS.clear()


def test_only_store_builds_a_git_command_line():
    """git_argv is called in store.py alone: everything else goes through store.git_run, which has the time limit,
    ends the process tree at the limit and keeps this process from waiting for git a second time."""
    users = [f.name for f in sorted(PKG.glob("*.py")) if f.name != "store.py"
             and re.search(r"\bgit_argv\(", f.read_text(encoding="utf-8"))]
    assert users == []


def test_asking_for_tracked_store_files_has_the_time_limit(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    seen = []

    def slow(argv, timeout):
        seen.append(timeout)
        raise subprocess.TimeoutExpired(argv, timeout)
    _fresh()
    monkeypatch.setattr(store, "_git_exec", slow)
    assert consent.tracked_entries(project, project / ".devmemory") == []
    assert seen and max(seen) <= store.GIT_TIMEOUT
    _fresh()


def test_git_is_not_asked_when_its_index_is_not_a_regular_file(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    index = project / ".git" / "index"
    index.unlink()
    index.mkdir()                 # stands for a named pipe, which only POSIX has

    def refuse(*a, **k):
        raise AssertionError("git was started")
    monkeypatch.setattr(store, "git_run", refuse)
    assert consent._git_index_stamp(project).endswith(consent.NOT_A_FILE)
    assert consent._tracked(project, project / ".devmemory")[0] is None
    assert consent.tracked_entries(project, project / ".devmemory") == []
    for cached in (True, False):     # no answer is not "nothing tracked": the store is not used
        assert consent._tracked_extra(project, project / ".devmemory", set(), cached=cached)[0] is None
        consent.forget_decisions()
        assert "not a regular file" in (consent.store_refusal(project, project / ".devmemory",
                                                              check_git="cached" if cached else True) or "")


def test_an_answer_git_could_not_give_is_not_asked_for_again_for_a_minute_unless_the_index_changes(tmp_path,
                                                                                                  monkeypatch):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    (st / "sessions").mkdir(exist_ok=True)
    real = store._git_exec
    asked = []

    def hangs(argv, timeout, *rest):
        if "ls-files" in argv:
            asked.append(1)
            raise subprocess.TimeoutExpired(argv, timeout)
        return real(argv, timeout, *rest)
    monkeypatch.setattr(store, "_git_exec", hangs)
    from devmem import gitindex

    def unreadable(*a, **k):                             # nor can devmem read the index file itself (devmem.gitindex)
        raise gitindex.IndexUnknown("not read in this test")
    monkeypatch.setattr(gitindex, "index_candidates", unreadable)
    _fresh()
    assert consent._tracked_extra(project, st, set(), cached=True)[0] is None
    assert len(asked) == 1
    for n in range(3):                                   # the store changes with every recorded event
        (st / "sessions" / f"s{n}.meta.json").write_text("{}", encoding="utf-8")
        _fresh()
        assert consent._tracked_extra(project, st, set(), cached=True)[0] is None
    assert len(asked) == 1
    (project / "new.txt").write_text("x\n", encoding="utf-8")
    time.sleep(0.05)
    subprocess.run(["git", "-C", str(project), "add", "new.txt"], check=True, capture_output=True)
    _fresh()
    assert consent._tracked_extra(project, st, set(), cached=True)[0] is None
    assert len(asked) == 2                               # the index changed: git is asked once more
    _fresh()


def test_the_file_list_under_a_deadline_gives_git_half_of_the_time_left(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    seen = []

    def listing(p, timeout=60):
        seen.append(timeout)
        return None                                      # as after a git that was ended: the folders are walked
    monkeypatch.setattr(store, "_git_list", listing)
    files = store.list_files(project, deadline=time.perf_counter() + 8)[0]
    assert seen and 3.0 < seen[0] <= 4.0
    assert "greet.py" in [f[0] for f in files]


def test_the_import_probe_runs_in_a_private_folder(monkeypatch):
    seen = {}

    def fake(argv, **kw):
        seen["cwd"] = kw.get("cwd")
        seen["there"] = os.path.isdir(kw.get("cwd") or "")
        return subprocess.CompletedProcess(argv, 0, "x.py", "")
    monkeypatch.setattr(connect.proc, "run", fake)
    assert connect._probe_import("python") == "x.py"
    assert seen["there"] and os.path.normcase(seen["cwd"]) != os.path.normcase(tempfile.gettempdir())
    assert os.path.normcase(os.path.dirname(seen["cwd"])) == os.path.normcase(tempfile.gettempdir())
    assert not os.path.exists(seen["cwd"])               # removed afterwards


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_named_pipe_as_git_index_does_not_stall_the_store_check(tmp_path):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    index = project / ".git" / "index"
    index.unlink()
    os.mkfifo(index)
    _fresh()
    try:
        for cached in ("cached", True):
            t0 = time.monotonic()
            consent.store_refusal(project, st, check_git=cached)
            assert time.monotonic() - t0 < 2.0, cached
    finally:
        _fresh()


def test_one_limit_covers_the_filter_listing_and_the_command(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    seen: list[tuple[str, float]] = []

    def slow_config(argv, timeout, *rest):
        seen.append(("config" if "config" in argv else "command", timeout))
        if "config" in argv:
            time.sleep(0.3)
            return subprocess.CompletedProcess(argv, 1, b"", b"")         # no filter drivers
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    _fresh()
    monkeypatch.setattr(store, "_git_exec", slow_config)
    store.git_run(project, "status", timeout=1.0)
    assert [k for k, _ in seen] == ["config", "command"]
    assert seen[0][1] <= 1.0
    assert seen[1][1] <= 0.71                     # what is left of the same second

    def hangs(argv, timeout, *rest):
        seen.append(("hang", timeout))
        raise subprocess.TimeoutExpired(argv, timeout)
    seen.clear()
    _fresh()
    monkeypatch.setattr(store, "_git_exec", hangs)
    assert store._git_list(project, 1.0) is None
    assert seen and max(t for _, t in seen) <= 1.0
    _fresh()


def _sleeper(calls: list[int]):
    """proc.popen standing in for a git that never answers (a real process, ended at the limit)."""
    real = proc.popen

    def popen(argv, **kw):
        calls.append(1)
        return real([sys.executable, "-c", "import time; time.sleep(30)"], **kw)
    return popen


def test_a_git_that_did_not_answer_is_not_started_there_again_by_any_process_for_a_minute(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    calls: list[int] = []
    _fresh()
    monkeypatch.setattr(proc, "popen", _sleeper(calls))
    with pytest.raises(OSError):
        store.git_run(project, "status", timeout=0.5)
    assert len(calls) == 1
    marker = store._slow_marker(project)
    assert marker is not None and marker.is_file()
    assert Path(os.environ["DEVMEM_USER_DATA"]) in marker.parents         # devmem's per-user folder, not the project
    _fresh()                                       # another process: nothing of this one's memory
    with pytest.raises(store.GitUnsafe):
        store.git_run(project, "status")
    assert len(calls) == 1
    code = ("import sys\nfrom pathlib import Path\nfrom devmem import store\n"
            "def no(*a, **k):\n    raise AssertionError('git was started')\n"
            "from devmem import proc\nproc.popen = no\n"
            "try:\n    store.git_run(Path(sys.argv[1]), 'status')\nexcept store.GitUnsafe as e:\n    print('refused:', e)\n")
    r = subprocess.run([sys.executable, "-c", code, str(project)], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "PYTHONPATH": str(PKG.parent)})
    assert r.stdout.startswith("refused: git did not answer in time in this project"), (r.stdout, r.stderr)
    d = json.loads(marker.read_text(encoding="utf-8"))
    d["at"] -= store._GIT_COOLDOWN + 1             # a minute later: an honest slow repository is asked again
    marker.write_text(json.dumps(d), encoding="utf-8")
    _fresh()
    with pytest.raises(OSError):
        store.git_run(project, "status", timeout=0.3)
    assert len(calls) == 2
    _fresh()


def test_the_marker_is_kept_out_of_the_project(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.setenv("DEVMEM_USER_DATA", str(project / "ud"))
    _fresh()
    assert store._slow_marker(project) is None    # a project could ship one: only this process's own cooldown
    _fresh()


def _gits_in(project: Path) -> list[int]:
    """Live git processes whose command line names `project` (Linux /proc)."""
    out = []
    needle = os.fsencode(str(project))
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes()
            state = (d / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue
        if state != "Z" and needle in cmd and os.path.basename(cmd.split(b"\0", 1)[0]).startswith(b"git"):
            out.append(int(d.name))
    return out


def _hanging_config(project: Path) -> None:
    """Every git command in `project` hangs: its configuration includes a named pipe."""
    os.mkfifo(project / ".git" / "inc")
    with open(project / ".git" / "config", "a", encoding="utf-8") as f:
        f.write("[include]\n\tpath = inc\n")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG and /proc: Linux")
def test_no_git_outlives_the_process_that_started_it(tmp_path):
    project = make_project(tmp_path)
    _hanging_config(project)
    code = "import sys\nfrom pathlib import Path\nfrom devmem import store\nstore.git_run(Path(sys.argv[1]), 'status')\n"
    p = subprocess.Popen([sys.executable, "-c", code, str(project)], env={**os.environ, "PYTHONPATH": str(PKG.parent)},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    left: list[int] = []
    try:
        end = time.monotonic() + 4
        while not _gits_in(project) and time.monotonic() < end:
            time.sleep(0.02)
        assert _gits_in(project), "git did not start"
        p.send_signal(signal.SIGKILL)               # the devmem process ends while its git hangs
        p.wait(timeout=5)
        end = time.monotonic() + 3
        while _gits_in(project) and time.monotonic() < end:
            time.sleep(0.05)
        left = _gits_in(project)
        assert left == []
    finally:
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if p.poll() is None:
            p.kill()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="named pipes and /proc: Linux")
def test_a_refresh_leaves_no_git_behind_and_the_next_one_starts_none(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    _hanging_config(project)
    code = ("import sys, json\nfrom pathlib import Path\nfrom devmem import refresh\n"
            "P = Path(sys.argv[1])\nprint(json.dumps(refresh.refresh(P, P / '.devmemory', 4.0)))\n")
    env = {**os.environ, "PYTHONPATH": str(PKG.parent)}
    left: list[int] = []
    try:
        for n in range(2):
            t0 = time.monotonic()
            r = subprocess.run([sys.executable, "-c", code, str(project)], env=env, capture_output=True, text=True,
                               timeout=60)
            took = time.monotonic() - t0
            assert r.returncode == 0, r.stderr
            end = time.monotonic() + 3
            while _gits_in(project) and time.monotonic() < end:
                time.sleep(0.05)
            left = _gits_in(project)
            assert left == [], (n, r.stdout)
            if n == 1:                               # the marker of the first: no git, no wait for one
                assert took < 4.0, (took, r.stdout)
        assert store._slow_marker(project) is not None and store._slow_marker(project).is_file()
        assert (st / "index" / "map.json").is_file()  # the files were walked instead
    finally:
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        _fresh()
