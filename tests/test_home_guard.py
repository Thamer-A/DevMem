"""The home folder, a root or a folder above home is never a project or a store, whatever its spelling.

Fuzz findings: `\\\\?\\<home>` slipped past the guard (Path comparison), so `devmem init` wrote hooks into the home
folder's `.claude/` and `devmem connect cursor` edited Cursor's global config; `scan`/`begin`/`mcp -p ~` created
`~/.devmemory`; `--store ~` and `--store .` wrote a `*` .gitignore, PROJECT.md and index/ into the folder named.
Every test uses a fake home folder (HOME/USERPROFILE in a temp folder), never the real one.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from cli_helpers import make_project

from devmem import consent, store as ST
from devmem.cli import main as cli_main

WIN = os.name == "nt"


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home" / "me"
    (home / ".cursor").mkdir(parents=True)
    (home / ".cursor" / "mcp.json").write_text('{"mcpServers": {"mine": {"command": "x"}}}', encoding="utf-8")
    (home / "notes.txt").write_text("mine\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    assert Path.home() == home
    return home


def _spellings(p: Path) -> list[str]:
    s = [str(p)]
    if WIN:
        s += ["\\\\?\\" + str(p), "\\\\.\\" + str(p), "\\\\localhost\\" + str(p)[0] + "$" + str(p)[2:],
              "\\\\?\\UNC\\localhost\\" + str(p)[0] + "$" + str(p)[2:], str(p).upper()]
    return s


def _snapshot(d: Path) -> list[str]:
    return sorted(str(x.relative_to(d)) for x in d.rglob("*"))


def test_every_spelling_of_home_and_above_is_refused(fake_home):
    for s in _spellings(fake_home):
        assert ST.not_a_project(Path(s)) == "home", s
    for s in _spellings(fake_home.parent):
        assert ST.not_a_project(Path(s)) == "above-home", s
    assert ST.not_a_project(Path(fake_home.anchor)) == "root"
    assert ST.not_a_project(fake_home / ".cursor") is None


@pytest.mark.parametrize("cmd", [["init", "--no-scan"], ["connect", "cursor", "--command", "devmem"], ["scan"],
                                 ["begin"], ["status"], ["pack", "x"]])
def test_commands_refuse_home_in_any_spelling(fake_home, tmp_path, monkeypatch, capsys, cmd):
    monkeypatch.setenv("DEVMEM_USER_DATA", str(tmp_path / "ud"))
    before = _snapshot(fake_home)
    for s in _spellings(fake_home):
        assert cli_main([*cmd, "-p", s]) == 2, (cmd, s)
        assert "home folder" in capsys.readouterr().err
    assert _snapshot(fake_home) == before


@pytest.mark.parametrize("where", ["home", "project", "src"])
def test_store_option_never_writes_into_a_folder_that_is_not_a_store(fake_home, tmp_path, capsys, where):
    project = make_project(tmp_path)
    target = {"home": fake_home, "project": project, "src": project / "src"}[where]
    before = _snapshot(target)
    for cmd in (["scan"], ["pack", "balance"]):
        assert cli_main([*cmd, "-p", str(project), "--store", str(target)]) == 2, (cmd, where)
        capsys.readouterr()
    assert _snapshot(target) == before
    assert not (target / ".gitignore").exists() or where == "project"


def test_store_option_accepts_a_new_or_existing_store(tmp_path, capsys):
    project = make_project(tmp_path)
    new = tmp_path / "mem"
    assert cli_main(["scan", "-p", str(project), "--store", str(new)]) == 0
    assert (new / "index" / "map.json").is_file() and (new / ".gitignore").is_file()
    assert cli_main(["scan", "-p", str(project), "--store", str(new)]) == 0      # an existing devmem store
    fixture_like = tmp_path / "memory"
    (fixture_like / "sessions").mkdir(parents=True)                              # sessions only, no .gitignore yet
    assert cli_main(["scan", "-p", str(project), "--store", str(fixture_like)]) == 0
    capsys.readouterr()


@pytest.mark.skipif(not WIN, reason="Windows path spellings")
def test_project_key_is_the_same_for_every_spelling(tmp_path):
    project = make_project(tmp_path)
    keys = {consent.project_key(Path(s)) for s in _spellings(project)}
    assert len(keys) == 1
