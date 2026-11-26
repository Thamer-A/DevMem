"""L1: `devmem pack --out FILE` never writes through a link.

A repository can ship `pack.md` as a symbolic link to another file (and the clipboard error message suggests
`--out pack.md`); the pack was written to the link's target. The output is now refused when FILE is a link, and is
written through a temporary file in the same folder that replaces FILE.
"""
from __future__ import annotations

import os

import pytest
from cli_helpers import run_cli


@pytest.fixture()
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    (p / "a.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    assert run_cli(["scan"], cwd=p).returncode == 0
    return p


def test_out_refuses_a_link(project, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    try:
        os.symlink(victim, project / "pack.md")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a symbolic link here")
    r = run_cli(["pack", "hello", "--out", "pack.md"], cwd=project)
    assert r.returncode == 2 and b"link" in r.stderr, r.stderr
    assert victim.read_text(encoding="utf-8") == "keep me\n"


def _dir_link(link, target) -> None:
    """A junction on Windows (needs no privilege), a symbolic link elsewhere."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.mark.parametrize("given", ["linked/pack.md", "sub/../linked/pack.md", "./linked/./pack.md", "abs"])
def test_out_refuses_a_folder_link_in_the_project_that_leads_out(project, tmp_path, given):
    """G1: only the last component was checked, so `--out linked/pack.md` wrote through a shipped folder link."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "sub").mkdir()
    _dir_link(project / "linked", outside)
    if given == "abs":
        given = str(project / "linked" / "pack.md")
    r = run_cli(["pack", "hello", "--out", given], cwd=project)
    assert r.returncode == 2 and b"leads out of it" in r.stderr, r.stderr
    assert list(outside.iterdir()) == []


def test_out_refuses_a_nested_folder_link_given_from_outside_the_project(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "deep").mkdir()
    _dir_link(project / "deep" / "linked", outside)
    r = run_cli(["pack", "hello", "--project", str(project), "--out", "proj/deep/linked/pack.md"], cwd=tmp_path)
    assert r.returncode == 2 and b"leads out of it" in r.stderr, r.stderr
    assert list(outside.iterdir()) == []


def test_out_follows_a_folder_link_that_stays_inside_the_project(project):
    (project / "real").mkdir()
    _dir_link(project / "alias", project / "real")
    r = run_cli(["pack", "hello", "--out", "alias/pack.md"], cwd=project)
    assert r.returncode == 0, r.stderr
    assert (project / "real" / "pack.md").is_file()


def test_out_may_go_outside_the_project_and_through_links_outside_it(project, tmp_path):
    """The user may write anywhere: `../x` and a link that does not live in the project are fine."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _dir_link(tmp_path / "shortcut", elsewhere)
    r = run_cli(["pack", "hello", "--out", "../pack.md"], cwd=project)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "pack.md").is_file()
    r = run_cli(["pack", "hello", "--out", str(tmp_path / "shortcut" / "pack.md")], cwd=project)
    assert r.returncode == 0, r.stderr
    assert (elsewhere / "pack.md").is_file()


def test_out_refuses_a_folder_name(project):
    r = run_cli(["pack", "hello", "--out", "sub/.."], cwd=project)
    assert r.returncode == 2 and b"names a folder" in r.stderr, r.stderr


def test_out_writes_a_regular_file_and_replaces_an_old_one(project):
    out = project / "pack.md"
    out.write_text("old\n", encoding="utf-8")
    r = run_cli(["pack", "hello", "--out", "pack.md"], cwd=project)
    assert r.returncode == 0, r.stderr
    assert out.is_file() and not out.is_symlink() and "old" not in out.read_text(encoding="utf-8")
    assert not [p for p in project.iterdir() if p.name.startswith(".pack.md")]     # no temporary file left
