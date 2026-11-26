"""FG-6: store.is_remote_path is the first thing applied to a path the user gives the CLI. `--project
\\\\localhost\\C$\\...` was is_dir-ed in that spelling (an SMB round trip to this machine) before the UNC refusal, and
`\\\\host\\share` options (--store, --out, --python, the import stream, the plugin data folder) were stat-ed or opened."""
from __future__ import annotations

import os

import pytest
from cli_helpers import make_project

from devmem import cli

WIN = os.name == "nt"
REMOTE = "\\\\server.invalid\\share\\proj"


@pytest.fixture()
def no_unc_touch(monkeypatch):
    """Fail on any stat, open or listing of a `\\\\...` or `//...` path."""
    touched: list[str] = []
    real_stat, real_open, real_scandir = os.stat, os.open, os.scandir

    def check(p):
        try:
            s = os.fspath(p)
        except TypeError:
            return
        if isinstance(s, bytes):
            s = s.decode("utf-8", "replace")
        if s.startswith(("\\\\", "//")) and not s.startswith(("\\\\?\\", "\\\\.\\")):
            touched.append(s)
            raise AssertionError(f"a network spelling was touched: {s}")

    def stat(p, *a, **k):
        check(p)
        return real_stat(p, *a, **k)

    def open_(p, *a, **k):
        check(p)
        return real_open(p, *a, **k)

    def scandir(p=".", *a, **k):
        check(p)
        return real_scandir(p, *a, **k)
    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "open", open_)
    monkeypatch.setattr(os, "scandir", scandir)
    return touched


@pytest.mark.parametrize("argv", [
    ["status", "--project", REMOTE],
    ["scan", "--project", REMOTE],
    ["pack", "x", "--project", REMOTE],
    ["app", "--project", REMOTE, "--dry-run"],
    ["status", "--project", "//server.invalid/share/proj"],
])
def test_a_network_project_is_refused_before_any_touch(no_unc_touch, capsys, argv):
    assert cli.main(argv) == 2
    assert "network path" in capsys.readouterr().err
    assert not no_unc_touch


@pytest.mark.parametrize("opt", [["--store", REMOTE], ["--out", REMOTE + "\\pack.md"]])
def test_network_options_are_refused_before_any_touch(no_unc_touch, tmp_path, capsys, opt):
    project = make_project(tmp_path)
    cmd = ["pack", "x", "--project", str(project), *opt] if opt[0] == "--out" else \
        ["status", "--project", str(project), *opt]
    assert cli.main(cmd) == 2
    assert "network path" in capsys.readouterr().err
    assert not no_unc_touch


def test_a_network_import_stream_is_refused(no_unc_touch, tmp_path, capsys):
    project = make_project(tmp_path)
    assert cli.main(["import", REMOTE + "\\s.jsonl", "--prompt", "x", "--project", str(project)]) == 2
    assert "network path" in capsys.readouterr().err


def test_init_network_python_is_refused(no_unc_touch, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEVMEM_USER_DATA", str(tmp_path / "ud"))
    project = make_project(tmp_path)
    assert cli.main(["init", "--no-scan", "--project", str(project), "--python", REMOTE + "\\python.exe"]) == 2
    assert "network path" in capsys.readouterr().err


@pytest.mark.skipif(not WIN, reason="the localhost administrative share is a Windows spelling")
def test_localhost_share_spelling_is_folded_before_any_touch(no_unc_touch, tmp_path, capsys):
    project = make_project(tmp_path)
    s = str(project)
    spelled = "\\\\localhost\\" + s[0] + "$" + s[2:]
    assert cli.main(["scan", "--project", spelled]) == 0
    assert not no_unc_touch
