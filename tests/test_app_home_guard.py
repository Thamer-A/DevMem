"""FG-1: `devmem app --project <home>` (also a drive root, a junction to it, and DevMemory.bat, which runs `devmem app`)
skipped the "home folder / root / above home is not a project" guard every other command applies: the viewer opened
and created `<home>\\.devmemory`. cmd_app now goes through cli._project and make_server applies the same guard.
Every test uses a fake home folder (HOME/USERPROFILE in a temp folder), never the real one."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from cli_helpers import cli_env, make_project

from devmem import ui_server as U
from devmem.cli import main as cli_main

WIN = os.name == "nt"


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home" / "me"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "notes.py").write_text("def private(): pass\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    assert Path.home() == home
    return home


def _spellings(home: Path) -> list[str]:
    out = [str(home), str(home.parent), home.anchor]
    if WIN:
        out += ["\\\\?\\" + str(home), str(home).upper()]
    return out


def test_make_server_refuses_home_root_and_above(fake_home):
    for s in _spellings(fake_home):
        with pytest.raises(U.NotAProject):
            U.make_server(Path(s), None, 0)
    assert not (fake_home / ".devmemory").exists()


def test_make_server_refuses_a_home_store(fake_home, tmp_path):
    project = make_project(tmp_path)
    with pytest.raises(U.NotAProject):
        U.make_server(project, fake_home, 0)


def test_make_server_refuses_a_network_path_before_touching_it(monkeypatch):
    def no_touch(*a, **k):
        raise AssertionError("a network path was touched")
    monkeypatch.setattr(Path, "is_dir", no_touch)
    with pytest.raises(U.NotAProject, match="network"):
        U.make_server(Path("\\\\server.invalid\\share\\proj"), None, 0)


def test_make_server_still_opens_a_project(fake_home, tmp_path):
    project = make_project(tmp_path)
    srv, st = U.make_server(project, None, 0)
    try:
        assert st.project == project.resolve()
    finally:
        srv.server_close()


@pytest.mark.parametrize("which", ["home", "above", "root"])
def test_devmem_app_refuses_home(fake_home, tmp_path, capsys, which):
    target = {"home": fake_home, "above": fake_home.parent, "root": Path(fake_home.anchor)}[which]
    assert cli_main(["app", "--project", str(target), "--dry-run"]) == 2
    assert "not a project" in capsys.readouterr().err
    assert not (fake_home / ".devmemory").exists()


@pytest.mark.skipif(not WIN, reason="junctions are a Windows feature")
def test_devmem_app_refuses_a_junction_to_home(fake_home, tmp_path, capsys):
    j = tmp_path / "jhome"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(j), str(fake_home)], capture_output=True)
    if r.returncode:
        pytest.skip("cannot make a junction here")
    assert cli_main(["app", "--project", str(j), "--dry-run"]) == 2
    assert not (fake_home / ".devmemory").exists()


def test_devmem_app_still_works_on_a_project(fake_home, tmp_path, capsys):
    project = make_project(tmp_path)
    assert cli_main(["app", "--project", str(project), "--dry-run"]) == 0
    assert "launch=" in capsys.readouterr().out


@pytest.mark.skipif(not WIN, reason="DevMemory.bat is the Windows double-click launcher")
def test_devmemory_bat_refuses_home(fake_home, tmp_path):
    bat = Path(__file__).resolve().parent.parent / "DevMemory.bat"
    if not bat.is_file():
        pytest.skip("no DevMemory.bat in this tree")
    env = cli_env()
    env.update(HOME=str(fake_home), USERPROFILE=str(fake_home), DEVMEM_USER_DATA=str(tmp_path / "ud"))
    r = subprocess.run([str(bat), "--project", str(fake_home), "--dry-run"], capture_output=True, env=env,
                       timeout=120, cwd=str(tmp_path))
    assert r.returncode != 0 and b"not a project" in r.stderr
    assert not (fake_home / ".devmemory").exists()
    assert sys.executable                    # the .bat runs `python` from PATH: the test's own interpreter's folder
