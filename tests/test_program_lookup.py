"""Programs devmem starts are found on PATH only, never in the current folder or the project.

On Windows, CreateProcess and shutil.which look in the current folder before PATH unless the environment variable
NoDefaultCurrentDirectoryInExePath is set. Claude Code's Bash tool sets it, so these tests unset it explicitly: in an
ordinary terminal it is not set, and a `git.exe` or `clip.bat` in a cloned project ran on `devmem init`/`scan`/
`status`/`doctor`/`pack --copy`.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from cli_helpers import PKG_ROOT, make_project

from devmem import clipboard, store

WIN = os.name == "nt"


def _plant(folder: Path, name: str, marker: Path) -> Path:
    """An executable `name` in `folder` that writes `marker` when it runs."""
    folder.mkdir(parents=True, exist_ok=True)
    if WIN:
        p = folder / f"{name}.bat"
        p.write_text(f'@echo off\r\necho ran > "{marker}"\r\n', encoding="ascii")
    else:
        p = folder / name
        p.write_text(f"#!/bin/sh\necho ran > '{marker}'\n", encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


@pytest.fixture()
def no_cwd_guard(monkeypatch):
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    store._PROGRAMS.clear()
    yield
    store._PROGRAMS.clear()


def test_find_program_ignores_the_current_folder_relative_path_entries_and_the_project(tmp_path, monkeypatch,
                                                                                       no_cwd_guard):
    project = make_project(tmp_path)
    good = tmp_path / "bin"
    _plant(project, "devmemfaketool", tmp_path / "planted.txt")            # in the project = the current folder
    _plant(project / "tools", "devmemfaketool", tmp_path / "planted2.txt")  # a project folder on PATH
    real = _plant(good, "devmemfaketool", tmp_path / "real.txt")
    monkeypatch.chdir(project)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "tools", str(project / "tools"), str(good)]))
    found = store.find_program("devmemfaketool", project)
    assert found and Path(found).resolve() == real.resolve()
    # nothing but the current folder and the project: not found, never the planted copies
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", str(project), str(project / "tools")]))
    assert store.find_program("devmemfaketool", project) is None
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", str(project)]))
    assert store.find_program("devmemfaketool") is None    # the current folder is refused even without `project`


@pytest.mark.skipif(not WIN, reason="\\\\?\\ spellings are Windows paths")
def test_a_project_folder_spelled_with_the_long_path_prefix_is_still_the_project(tmp_path, monkeypatch, no_cwd_guard):
    """V10: commonpath raised ValueError across `\\\\?\\C:\\...` and `C:\\...`, so _under said no and the planted program
    in the project ran; the current-folder check missed the same way."""
    project = make_project(tmp_path)
    _plant(project / "tools", "devmemfaketool", tmp_path / "planted.txt")
    _plant(project, "devmemfaketool2", tmp_path / "planted2.txt")
    monkeypatch.chdir(project)
    long_tools = "\\\\?\\" + str(project / "tools")
    long_project = "\\\\?\\" + str(project)
    monkeypatch.setenv("PATH", os.pathsep.join([long_tools, long_project]))
    assert store.find_program("devmemfaketool", project) is None
    assert store.find_program("devmemfaketool2") is None          # the current folder, spelled \\?\C:\...


@pytest.mark.skipif(not WIN, reason="volume GUID paths are Windows paths")
def test_home_spelled_by_volume_guid_is_not_a_project():
    """V6: local_path stripped `\\\\?\\` from any path, so `\\\\?\\Volume{...}\\Users\\me` became a relative path and
    not_a_project returned None (devmem init made a stray Volume{...} tree in the current folder)."""
    import ctypes
    home = Path.home()
    buf = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(home.anchor, buf, 64):
        pytest.skip("no volume GUID for the home drive")
    guid_home = buf.value + str(home)[len(home.anchor):]          # \\?\Volume{...}\Users\me
    assert store.not_a_project(Path(guid_home)) in ("home", "device")
    assert store.not_a_project(Path(buf.value)) in ("root", "device")
    assert store.not_a_project(Path("\\\\?\\GLOBALROOT\\Device\\HarddiskVolume3\\x")) == "device"
    assert store.local_path("\\\\?\\Volume{0}\\x") == "\\\\?\\Volume{0}\\x"     # never made relative
    assert store.local_path("\\\\?\\C:\\x") == "C:\\x" and store.local_path("\\\\.\\C:\\x") == "C:\\x"


def test_git_is_started_by_an_absolute_path_outside_the_project(tmp_path, monkeypatch, no_cwd_guard):
    project = make_project(tmp_path)
    _plant(project, "git", tmp_path / "planted.txt")
    monkeypatch.chdir(project)
    try:
        argv = store.git_argv(project, "status")
    except FileNotFoundError:
        pytest.skip("git is not installed")
    assert os.path.isabs(argv[0]) and not Path(argv[0]).resolve().is_relative_to(project.resolve())
    seen = []
    real_run = store._git_exec

    def spy(args, *a, **k):
        seen.append(list(args))
        return real_run(args, *a, **k)
    monkeypatch.setattr(store, "_git_exec", spy)
    from devmem import sessions
    store.list_files(project)
    sessions.git_status(project)
    assert seen and all(os.path.isabs(a[0]) and Path(a[0]).name.lower().startswith("git") for a in seen), seen
    assert not (tmp_path / "planted.txt").exists()


def test_clipboard_command_is_never_a_copy_in_the_current_folder(tmp_path, monkeypatch, no_cwd_guard):
    project = make_project(tmp_path)
    names = {"win32": "clip", "darwin": "pbcopy", "linux": "xclip"}
    for platform, name in names.items():
        _plant(project, name, tmp_path / f"{name}.txt")
    good = tmp_path / "bin"
    for name in names.values():
        _plant(good, name, tmp_path / f"real-{name}.txt")
    monkeypatch.chdir(project)
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(good)]))
    for platform, name in names.items():
        argv, _enc = clipboard.clipboard_command(platform, environ={})
        assert Path(argv[0]).resolve().parent == good.resolve(), (platform, argv)


@pytest.mark.skipif(not WIN, reason="the current-folder search is a Windows behaviour")
def test_pack_copy_from_inside_the_project_does_not_run_a_planted_clip(tmp_path):
    """End to end, as from an ordinary cmd window: the variable is unset and the project is the current folder."""
    project = make_project(tmp_path)
    planted, safe = tmp_path / "PLANTED.txt", tmp_path / "SAFE.txt"
    _plant(project, "clip", planted)
    _plant(tmp_path / "bin", "clip", safe)           # stands in for the real clip.exe: the clipboard is not touched
    env = {k: v for k, v in os.environ.items() if k.upper() != "NODEFAULTCURRENTDIRECTORYINEXEPATH"}
    env.update(PYTHONPATH=str(PKG_ROOT), PATH=os.pathsep.join([str(tmp_path / "bin"), os.environ.get("SystemRoot",
               r"C:\Windows") + r"\System32"]))
    (project / ".devmemory").mkdir()
    from devmem.consent import claim_store
    claim_store(project / ".devmemory")            # the user's own store (devmem notes one it creates)
    r = subprocess.run([sys.executable, "-m", "devmem", "pack", "negative balance label", "--copy"], cwd=project,
                       capture_output=True, env=env, timeout=120)
    assert not planted.exists(), r.stderr.decode("utf-8", "replace")
    assert safe.exists() or b"nothing copied" in r.stdout, (r.stdout, r.stderr)


def test_wrap_does_not_run_a_program_the_project_ships(tmp_path, monkeypatch, no_cwd_guard):
    from devmem import generic
    project = make_project(tmp_path)
    _plant(project, "devmemfaketool", tmp_path / "planted.txt")
    monkeypatch.chdir(project)
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(project)]))
    with pytest.raises(FileNotFoundError):
        generic.wrap(project, "do it", ["devmemfaketool", "{prompt}"], with_pack=False)
    assert not (tmp_path / "planted.txt").exists()


@pytest.mark.skipif(not WIN, reason="Windows only")
def test_windows_sets_the_no_current_folder_variable_for_child_processes():
    env = {k: v for k, v in os.environ.items() if k.upper() != "NODEFAULTCURRENTDIRECTORYINEXEPATH"}
    env["PYTHONPATH"] = str(PKG_ROOT)
    r = subprocess.run([sys.executable, "-c", "import os, devmem.store; "
                        "print(os.environ.get('NoDefaultCurrentDirectoryInExePath'))"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.stdout.strip() == "1", r.stderr
