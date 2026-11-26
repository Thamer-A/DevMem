"""No git command of devmem runs a program that the project's repository names, wherever the repository keeps its
configuration.

devmem used to look for filter drivers in `<project>/.git/config` only before asking git; a driver defined in
`config.worktree`, behind a `commondir` redirect, in an included file or in a submodule's own configuration was run by
devmem's `git status` (reviewer's v5a/git1.py to git5.py). Here every layout defines a clean filter that leaves a
marker file, and every entry point that starts git is run on each of them.
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
from cli_helpers import cli_env, run_cli
from hooks_helpers import make_project

from devmem import sessions, store

LAYOUTS = ["plain", "worktree_config", "commondir", "include", "include_if", "gitdir_file", "submodule",
           "nested_repository"]


def _git(p, *a):
    subprocess.run(["git", "-C", str(p), "-c", "user.email=a@b.invalid", "-c", "user.name=a", *a], check=True,
                   capture_output=True)


def _append(path: Path, text: str) -> None:
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _filter_section(command: str) -> str:
    quoted = command.replace("\\", "\\\\").replace('"', '\\"')
    return f'[filter "evil"]\n\tclean = "{quoted}"\n\tsmudge = "{quoted}"\n\trequired = true\n'


def _fresh() -> None:
    store._FILTERS.clear()
    store._git_down_until = 0.0


def _touch_tracked(folder: Path) -> None:
    later = time.time() + 5           # changed stat: git status has to read the content, through the clean filter
    for f in folder.iterdir():
        if f.is_file():
            os.utime(f, (later, later))


class Repo:
    def __init__(self, project: Path, marker: Path, dirty: list[Path]):
        self.project, self.marker, self.dirty = project, marker, dirty

    def touch(self) -> None:
        for d in self.dirty:
            _touch_tracked(d)

    def ran(self) -> str:
        return self.marker.read_text(encoding="utf-8") if self.marker.exists() else ""


def build(tmp_path: Path, layout: str) -> Repo:
    project = make_project(tmp_path)            # a git repository with a store this user switched on
    marker = tmp_path / "MARKER.txt"
    script = tmp_path / "mark.py"
    script.write_text("import sys\nopen(sys.argv[1], 'a').write(sys.argv[2] + '\\n')\n"
                      "sys.stdout.write(sys.stdin.read() if not sys.stdin.isatty() else '')\n", encoding="utf-8")
    command = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}" "{marker.as_posix()}" {layout}'
    section = _filter_section(command)
    (project / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
    _git(project, "add", ".gitattributes")
    _git(project, "commit", "-qm", "attributes")
    gitdir = project / ".git"
    dirty = [project]
    if layout == "plain":
        _append(gitdir / "config", section)
    elif layout == "worktree_config":
        _git(project, "config", "extensions.worktreeConfig", "true")
        (gitdir / "config.worktree").write_text(section, encoding="utf-8", newline="\n")
        assert "filter" not in (gitdir / "config").read_text(encoding="utf-8").lower()
    elif layout == "commondir":
        common = tmp_path / "common"
        common.mkdir()
        for name in ("objects", "refs", "config", "info", "hooks", "packed-refs", "description", "logs"):
            if (gitdir / name).exists():
                shutil.move(str(gitdir / name), str(common / name))
        (gitdir / "commondir").write_text("../../common\n", encoding="utf-8", newline="\n")
        (gitdir / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8", newline="\n")
        _append(common / "config", section)
    elif layout in ("include", "include_if"):
        (gitdir / "extra.cfg").write_text(section, encoding="utf-8", newline="\n")
        head = "[include]" if layout == "include" else '[includeIf "onbranch:**"]'
        _append(gitdir / "config", f"{head}\n\tpath = extra.cfg\n")
    elif layout == "gitdir_file":
        real = tmp_path / "realgit"
        shutil.move(str(gitdir), str(real))
        gitdir.write_text("gitdir: ../realgit\n", encoding="utf-8", newline="\n")
        _append(real / "config", section)
    elif layout in ("submodule", "nested_repository"):
        sub = project / "sub"
        sub.mkdir()
        _git(sub, "init", "-q")
        (sub / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
        (sub / "a.txt").write_text("a\n", encoding="utf-8")
        _git(sub, "add", "-A")
        _git(sub, "commit", "-qm", "sub")
        _append(sub / ".git" / "config", section)
        if layout == "submodule":
            _git(project, "add", "sub")
            _git(project, "commit", "-qm", "gitlink")
        (sub / "a.txt").write_text("changed\n", encoding="utf-8")
        dirty.append(sub)
    else:
        raise AssertionError(layout)
    if marker.exists():
        marker.unlink()               # building the repository may have run the filter
    _fresh()
    return Repo(project, marker, dirty)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_layout_would_run_its_filter_under_plain_git(tmp_path, layout):
    """The control: the same repository under an unprotected `git status` does run the program, so the tests below
    are about devmem's protection and not about a layout git ignores."""
    repo = build(tmp_path, layout)
    repo.touch()
    where = repo.dirty[-1] if layout == "nested_repository" else repo.project
    subprocess.run(["git", "-C", str(where), "status", "--porcelain", "--untracked-files=all"], capture_output=True,
                   timeout=60)
    assert layout in repo.ran()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_no_function_that_starts_git_runs_the_filter(tmp_path, layout):
    repo = build(tmp_path, layout)
    project = repo.project
    calls = [
        lambda: sessions.is_git_worktree(project), lambda: sessions.git_head(project),
        lambda: sessions.git_status(project), lambda: sessions.git_status(project, untracked="all"),
        lambda: sessions.git_snapshot(project), lambda: store._git_list(project),
        lambda: store.tracked_in_store(project, project / ".devmemory"),
        lambda: sessions._git(project, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "HEAD", "--"),
        lambda: sessions._git(project, "diff", "--no-ext-diff", "--no-textconv", "--name-only"),
    ]
    for n, call in enumerate(calls):
        repo.touch()
        _fresh()
        call()
        assert not repo.ran(), (layout, n, repo.ran())


@pytest.mark.parametrize("layout", LAYOUTS)
def test_no_command_or_hook_runs_the_filter(tmp_path, layout):
    repo = build(tmp_path, layout)
    project = repo.project
    for args in (["status"], ["doctor"], ["begin"], ["end", "--prompt", "x"]):
        repo.touch()
        r = run_cli(args, cwd=project)
        assert not repo.ran(), (layout, args, repo.ran(), r.stderr[-300:])
    env = cli_env(CLAUDE_PROJECT_DIR=str(project))
    sid = "layout-git-0001"
    for ev, kw in [("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": "greet", "prompt_id": "p"}),
                   ("Stop", {"last_assistant_message": "done"}), ("SessionEnd", {"reason": "exit"})]:
        repo.touch()
        payload = {"session_id": sid, "cwd": str(project), "hook_event_name": ev, "transcript_path": "x", **kw}
        r = run_cli(["hook", ev], cwd=project, stdin=json.dumps(payload), env=env)
        assert r.returncode == 0
        assert not repo.ran(), (layout, ev, repo.ran())


@pytest.mark.parametrize("layout", LAYOUTS)
def test_status_still_answers(tmp_path, layout):
    repo = build(tmp_path, layout)
    (repo.project / "greet.py").write_text("changed\n", encoding="utf-8")
    st = sessions.git_status(repo.project)
    assert st is not None and "greet.py" in st
    assert not repo.ran()


def test_the_users_own_filter_keeps_working_unless_the_repository_redefines_it(tmp_path, monkeypatch):
    """A driver from the user's global configuration (git-lfs is one) is theirs; the repository's is not."""
    project = make_project(tmp_path)
    marker = tmp_path / "MARKER.txt"
    script = tmp_path / "mark.py"
    script.write_text("import sys\nopen(sys.argv[1], 'a').write(sys.argv[2] + '\\n')\n"
                      "sys.stdout.write(sys.stdin.read() if not sys.stdin.isatty() else '')\n", encoding="utf-8")

    def command(tag: str) -> str:
        return f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}" "{marker.as_posix()}" {tag}'
    home = tmp_path / "home.gitconfig"
    quoted = command("mine").replace("\\", "\\\\").replace('"', '\\"')
    home.write_text(f'[filter "mine"]\n\tclean = "{quoted}"\n', encoding="utf-8", newline="\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home))
    (project / ".gitattributes").write_text("* filter=mine\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", ".gitattributes"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.email=a@b.invalid", "-c", "user.name=a", "commit", "-qm",
                    "attributes"], check=True, capture_output=True)
    if marker.exists():
        marker.unlink()
    _fresh()
    assert store._filter_names(store.find_program("git", project), project) == []
    _touch_tracked(project)
    assert sessions.git_status(project) is not None
    assert "mine" in marker.read_text(encoding="utf-8")           # the user's own driver ran, as with plain git
    marker.unlink()
    quoted = command("theirs").replace("\\", "\\\\").replace('"', '\\"')
    _append(project / ".git" / "config", f'[filter "mine"]\n\tprocess = "{quoted}"\n')
    _fresh()
    assert store._filter_names(store.find_program("git", project), project) == ["mine"]
    _touch_tracked(project)
    assert sessions.git_status(project) is not None
    assert not marker.exists()                                    # redefined by the repository: every key emptied


def test_git_is_not_run_when_the_filters_cannot_be_listed(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    real = store._git_exec

    def failing(argv, timeout, *rest):
        if "config" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: bad config")
        return real(argv, timeout, *rest)
    _fresh()
    monkeypatch.setattr(store, "_git_exec", failing)
    with pytest.raises(store.GitUnsafe):
        store.git_argv(project, "status")
    assert sessions.git_status(project) is None

    def odd(argv, timeout, *rest):
        if "config" in argv:
            return subprocess.CompletedProcess(argv, 0, b"local\0filter.a.clean\0local\0", b"")
        return real(argv, timeout, *rest)
    _fresh()
    monkeypatch.setattr(store, "_git_exec", odd)
    with pytest.raises(store.GitUnsafe):
        store.git_argv(project, "status")

    def old_git(argv, timeout, *rest):                 # a git without --show-scope: every driver is emptied
        if "--show-scope" in argv:
            return subprocess.CompletedProcess(argv, 129, b"", b"usage")
        if "config" in argv:
            return subprocess.CompletedProcess(argv, 0, b"filter.lfs.clean\0filter.x y.smudge\0", b"")
        return real(argv, timeout, *rest)
    _fresh()
    monkeypatch.setattr(store, "_git_exec", old_git)
    argv = store.git_argv(project, "status")
    assert "filter.lfs.clean=" in argv and "filter.x y.smudge=" in argv and "filter.x y.required=false" in argv
    _fresh()


def test_too_many_filter_drivers_stop_git(tmp_path):
    project = make_project(tmp_path)
    _append(project / ".git" / "config",
            "".join(f'[filter "f{i}"]\n\tclean = cat\n' for i in range(store._MAX_FILTERS + 1)))
    _fresh()
    with pytest.raises(store.GitUnsafe):
        store.git_argv(project, "status")
    _fresh()


def test_status_and_diff_always_leave_submodules_alone():
    off = store._submodules_off
    assert off(("status", "--porcelain")) == ["status", "--ignore-submodules=all", "--porcelain"]
    assert off(("-c", "core.quotepath=off", "diff", "--name-only")) == \
        ["-c", "core.quotepath=off", "diff", "--ignore-submodules=all", "--name-only"]
    assert off(("-c", "status", "status")) == ["-c", "status", "status", "--ignore-submodules=all"]
    assert off(("status", "--ignore-submodules=all")) == ["status", "--ignore-submodules=all"]
    assert off(("ls-files", "-z", "--", "status")) == ["ls-files", "-z", "--", "status"]
    assert off(("rev-parse", "HEAD")) == ["rev-parse", "HEAD"]
    assert off(()) == []


def test_a_git_that_does_not_answer_is_killed_with_its_children_and_not_started_again(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    code = ("import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
            "time.sleep(120)\n")
    prog = tmp_path / "slow.py"
    prog.write_text(code, encoding="utf-8")
    _fresh()
    t0 = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            store._git_exec([sys.executable, str(prog)], 3.0)
        took = time.monotonic() - t0
        assert took < 15, took
        with pytest.raises(store.GitUnsafe):                      # no second wait in this process
            store._git_exec([sys.executable, "-c", "pass"], 3.0)
        assert sessions.git_status(tmp_path) is None
        pid = int(pidfile.read_text(encoding="utf-8"))
        for _ in range(50):                                       # the grandchild is gone too
            if not _alive(pid):
                break
            time.sleep(0.1)
        assert not _alive(pid)
    finally:
        _fresh()


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:                                  # a killed child nobody waited for stays listed as a zombie
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_named_pipe_as_gitignore_costs_one_limit(tmp_path):
    project = make_project(tmp_path)
    os.mkfifo(project / ".gitignore")
    _fresh()
    try:
        t0 = time.monotonic()
        try:
            store.git_run(project, "status", "--porcelain", timeout=2.0)
            hung = False
        except subprocess.TimeoutExpired:
            hung = True
        first = time.monotonic() - t0
        assert first < 10, first
        if hung:                               # this git waits on the pipe: later commands do not wait again
            t0 = time.monotonic()
            assert sessions.git_status(project) is None
            assert sessions.git_head(project) is None
            assert store._git_list(project) is None
            assert time.monotonic() - t0 < 1.0
        t0 = time.monotonic()
        names = list(store.iter_files(project)) if hung else []
        assert time.monotonic() - t0 < 10
        assert ".gitignore" not in names
    finally:
        _fresh()
