"""L4: `wrap` and `codex` remove their temporary folder (it holds the full pack) however the command ends.
L6: `wrap` refuses to put the prompt in the arguments of a .bat/.cmd target, which cmd.exe would re-parse
(more cases, B2: tests/test_proc_cmd.py)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
from cli_helpers import make_project

from devmem import generic


def _leftovers(prefix: str) -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob(prefix + "*")}


def test_wrap_removes_its_folder_when_the_command_cannot_start(tmp_path):
    project = make_project(tmp_path)
    before = _leftovers("devmem-wrap-")
    missing = str(tmp_path / "no-such-program.exe")
    with pytest.raises(OSError):
        generic.wrap(project, "a task", [missing, "{pack_file}"])
    assert _leftovers("devmem-wrap-") == before


def test_wrap_removes_its_folder_when_recording_fails(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    before = _leftovers("devmem-wrap-")

    def boom(*a, **k):
        raise RuntimeError("recording failed")
    monkeypatch.setattr(generic, "record_generic", boom)
    with pytest.raises(RuntimeError):
        generic.wrap(project, "a task", [sys.executable, "-c", "pass"])
    assert _leftovers("devmem-wrap-") == before


def test_codex_removes_its_folder_when_codex_fails_to_start(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    before = _leftovers("devmem-codex-")
    monkeypatch.setattr(generic, "find_program", lambda *a, **k: str(tmp_path / "codex-gone.exe"))
    with pytest.raises(OSError):
        generic.codex(project, "a task")
    assert _leftovers("devmem-codex-") == before


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe re-parses batch-file arguments on Windows")
def test_a_batch_file_never_gets_the_prompt_as_an_argument(tmp_path):
    project = make_project(tmp_path)
    bat = tmp_path / "tool.cmd"
    marker = tmp_path / "ran.txt"
    bat.write_text(f'@echo off\r\necho %* > "{marker}"\r\n', encoding="ascii")
    with pytest.raises(ValueError, match="pack_file"):
        generic.wrap(project, "a task & echo injected", [str(bat), "{prompt}"])
    assert not marker.exists()
    meta = generic.wrap(project, "a task & echo injected", [str(bat), "{pack_file}"])   # the file form still works
    assert meta.exists() and marker.exists()
