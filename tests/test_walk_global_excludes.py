"""The folder walk that stands in for git (no git, a git devmem does not run, a git that timed out) honours the
user's global git excludes, as git does: a file the user ignores everywhere stays out of the map (reviewer's
v8a/b/t14_walk_excludes.py: a repository with more than 200 filter drivers forced the walk, and a globally ignored
file landed in the map). Only the user's own configuration names that file, never the repository's."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from devmem import store


@pytest.fixture
def user(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    return home


def _project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    for name in ("a.py", "private_draft.md", "notes.txt"):
        (p / name).write_text("x\n", encoding="utf-8")
    return p


def _walked(project: Path) -> list[str]:
    return [r for r, _s, _t in store._walk_list(project, re.compile(r"(?!)"), ())]


def test_the_default_global_excludes_file(tmp_path, user):
    (tmp_path / "xdg" / "git").mkdir(parents=True)
    (tmp_path / "xdg" / "git" / "ignore").write_text("private_*.md\n", encoding="utf-8")
    assert _walked(_project(tmp_path)) == ["a.py", "notes.txt"]


@pytest.mark.parametrize("where", ["gitconfig", "xdg", "GIT_CONFIG_GLOBAL"])
def test_core_excludesfile_from_the_users_own_configuration(tmp_path, user, monkeypatch, where):
    ignore = tmp_path / "my ignore"
    ignore.write_text("# mine\nprivate_draft.md\n", encoding="utf-8")
    text = f'[user]\n\tname = me\n[core]\n\tautocrlf = false\n\texcludesFile = "{ignore.as_posix()}"\n'
    if where == "gitconfig":
        (user / ".gitconfig").write_text(text, encoding="utf-8")
    elif where == "xdg":
        (tmp_path / "xdg" / "git").mkdir(parents=True)
        (tmp_path / "xdg" / "git" / "config").write_text(text, encoding="utf-8")
    else:
        cfg = tmp_path / "global.cfg"
        cfg.write_text(text, encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    assert _walked(_project(tmp_path)) == ["a.py", "notes.txt"]


def test_a_repositorys_own_configuration_names_no_excludes_file(tmp_path, user):
    project = _project(tmp_path)
    (project / "x.ignore").write_text("notes.txt\n", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text(f"[core]\n\texcludesFile = {(project / 'x.ignore').as_posix()}\n",
                                             encoding="utf-8")
    assert "notes.txt" in _walked(project)


def test_a_huge_global_excludes_file_is_not_read(tmp_path, user, monkeypatch):
    (tmp_path / "xdg" / "git").mkdir(parents=True)
    (tmp_path / "xdg" / "git" / "ignore").write_text("private_draft.md\n" + "#" * 200, encoding="utf-8")
    monkeypatch.setattr(store, "MAX_IGNORE_FILE_BYTES", 64)
    assert "private_draft.md" in _walked(_project(tmp_path))
