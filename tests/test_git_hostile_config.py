"""L1: no devmem command or hook makes git run a program that a project's own `.git/config` or `.git/hooks` names.

devmem's `git status` (hooks, begin, end) ran a shipped `filter.<x>.clean`/`process` program, and on Linux
`.git/hooks/post-index-change` (reviewer's verify3/git1/drive.py, linux/C/gitcfg.py). Here a repository plants a
marker program for every place git could take one from; it must never run.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cli_helpers import run_cli
from hooks_helpers import make_project

from devmem import store


def _git(p, *a):
    subprocess.run(["git", "-C", str(p), "-c", "user.email=a@b.invalid", "-c", "user.name=a", *a], check=True,
                   capture_output=True)


@pytest.fixture()
def hostile(tmp_path):
    project = make_project(tmp_path)            # a git repository with a store this user switched on
    marker = tmp_path / "MARKER.txt"
    script = tmp_path / "mark.py"
    script.write_text("import sys\nopen(sys.argv[1], 'a').write(sys.argv[2] + '\\n')\n"
                      "sys.stdout.write(sys.stdin.read() if not sys.stdin.isatty() else '')\n", encoding="utf-8")
    py = Path(sys.executable).as_posix()

    def cmd(tag: str) -> str:
        return f'"{py}" "{script.as_posix()}" "{marker.as_posix()}" {tag}'
    hooks = project / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("post-index-change", "pre-commit", "post-checkout", "reference-transaction", "fsmonitor-watchman"):
        h = hooks / name
        h.write_text(f"#!/bin/sh\n{cmd('hook-' + name)}\n", encoding="utf-8", newline="\n")
        h.chmod(0o755)
    (project / ".gitattributes").write_text("* filter=evil diff=evil\n", encoding="utf-8")
    cfg = {
        "filter.evil.clean": cmd("clean"), "filter.evil.smudge": cmd("smudge"), "filter.evil.process": cmd("process"),
        "filter.evil.required": "true", "diff.evil.textconv": cmd("textconv"), "diff.external": cmd("diff-external"),
        "core.fsmonitor": cmd("fsmonitor"), "core.pager": cmd("pager"), "core.sshCommand": cmd("ssh"),
        "core.askPass": cmd("askpass"), "credential.helper": "!" + cmd("credential"), "core.editor": cmd("editor"),
        "core.gitProxy": cmd("proxy"), "alias.status": "!" + cmd("alias-status"), "alias.diff": "!" + cmd("alias"),
        "pager.status": cmd("pager-status"), "core.hooksPath": hooks.as_posix(),
    }
    _git(project, "add", ".gitattributes")
    _git(project, "commit", "-qm", "attrs")
    for k, v in cfg.items():
        _git(project, "config", k, v)
    if marker.exists():
        marker.unlink()               # the commit above may have run some of them
    return project, marker


def _touch_tracked(project: Path) -> None:
    later = time.time() + 5           # changed stat: git status must look at the content (runs the clean filter)
    for f in project.iterdir():
        if f.is_file():
            os.utime(f, (later, later))


def test_no_marker_program_runs_from_any_command_or_hook(hostile):
    project, marker = hostile
    sid = "hostile-git-0001"
    steps = [["scan"], ["status"], ["doctor"], ["pack", "greet"], ["begin"], ["end", "--prompt", "x"]]
    for args in steps:
        _touch_tracked(project)
        r = run_cli(args, cwd=project)
        assert not marker.exists(), (args, marker.read_text(encoding="utf-8"), r.stderr[-300:])
    env_extra = {"CLAUDE_PROJECT_DIR": str(project)}
    from cli_helpers import cli_env
    for ev, kw in [("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": "greet", "prompt_id": "p"}),
                   ("Stop", {"last_assistant_message": "done"}), ("SessionEnd", {"reason": "exit"})]:
        _touch_tracked(project)
        payload = {"session_id": sid, "cwd": str(project), "hook_event_name": ev, "transcript_path": "x", **kw}
        r = run_cli(["hook", ev], cwd=project, stdin=json.dumps(payload), env=cli_env(**env_extra))
        assert r.returncode == 0
        assert not marker.exists(), (ev, marker.read_text(encoding="utf-8"))


def test_status_still_works_and_sees_changes(hostile):
    project, _marker = hostile
    (project / "greet.py").write_text("changed\n", encoding="utf-8")
    from devmem.sessions import git_status
    st = git_status(project)
    assert st is not None and "greet.py" in st


def test_a_filter_name_that_cannot_be_switched_off_stops_git(tmp_path):
    p = tmp_path / "p"
    p.mkdir()
    _git(p, "init", "-q")
    with open(p / ".git" / "config", "a", encoding="utf-8") as f:
        f.write('[filter "a=b"]\n\tclean = something\n')
    with pytest.raises(store.GitUnsafe):
        store.git_argv(p, "status")
