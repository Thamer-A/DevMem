"""`devmem begin`/`end` (also `wrap`, `codex`, MCP begin_session/end_session) read only plain files inside the project.

They took names from `git status` and read `project / rel` directly: with a hand-written index holding
`../outside/notes.py`, an absolute path or a `C:/...` path, the outside file's text landed in `pending/<id>.json`,
then in the session record, and `devmem show` / MCP `read_session` returned it. Every name from git now goes through
project_path and every read through read_regular.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from devmem import generic as G
from devmem import sessions as S
from devmem.redact import Redactor


def _git(p, *args):
    return subprocess.run(["git", "-C", str(p), "-c", "core.autocrlf=false", *args], capture_output=True, text=True,
                          check=True).stdout


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.py").write_text("OUTSIDE_MARKER = 1\n", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    _git(project, "init", "-q")
    _git(project, "add", "-A")
    _git(project, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "init")
    absn = str(outside / "notes.py").replace("\\", "/")
    bad = ["../outside/notes.py", absn, absn[2:] if os.name == "nt" else absn.lstrip("/"), "C:/outside/notes.py",
           "sub/../../outside/notes.py"]
    real = S.git_status_sized

    def status(p, untracked="normal", **k):
        out, too_large = real(p, untracked, **k)
        out = out or {}
        out.update({b: " M" for b in bad})
        return out, too_large
    monkeypatch.setattr(S, "git_status_sized", status)
    return project, outside


def test_begin_never_reads_a_file_outside_the_project(setup):
    project, _outside = setup
    state = G.start_state(project, Redactor(None))
    assert "OUTSIDE_MARKER" not in json.dumps(state), state


def test_end_never_reads_a_file_outside_the_project(setup, monkeypatch):
    project, _outside = setup
    start = G.start_state(project, Redactor(None))
    shown = []
    real_show = G._git_show

    def show(p, rev, rel, **k):
        shown.append(rel)
        return real_show(p, rev, rel, **k)
    monkeypatch.setattr(G, "_git_show", show)
    (project / "app.py").write_text("def ok():\n    return 2\n", encoding="utf-8")
    before, after, _git_info = G.end_state(project, start, Redactor(None))
    assert "OUTSIDE_MARKER" not in json.dumps([before, after])
    assert "app.py" in after
    assert all(".." not in r and ":" not in r and not r.startswith("/") for r in shown), shown


def test_a_link_or_fifo_in_the_project_is_not_read(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (tmp_path / "target.txt").write_text("LINKED_MARKER\n", encoding="utf-8")
    try:
        os.symlink(tmp_path / "target.txt", project / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a symbolic link here")
    assert G._read_rel(project, "link.txt") is None
    snap = G.snapshot(project)
    assert "LINKED_MARKER" not in json.dumps(snap)
