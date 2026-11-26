"""The command line reports a failure in one line (exit 2), never a Python traceback; DEVMEM_DEBUG=1 shows it.

cli.main caught only CliError, StoreRefused, KeyboardInterrupt, BrokenPipeError and FileNotFoundError: `pack --out`
with `*` or a newline in the name, `app --port -5` or a huge port, a 30,000-character `-p` and an unreadable project
folder ended in a traceback (reviewer's h9_cli.py).
"""
from __future__ import annotations

import pytest
from cli_helpers import run_cli

from devmem import cli


@pytest.fixture()
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    (p / "a.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    assert run_cli(["scan"], cwd=p).returncode == 0
    return p


def _err(r) -> str:
    return r.stderr.decode("utf-8", "replace")


@pytest.mark.parametrize("args", [
    ["pack", "hello", "--out", "x" * 300 + ".md"],          # a name no file system accepts (over 255 characters)
    ["pack", "hello", "--out", "dir-that-does-not-exist/x/y.md"],
    ["scan", "-p", "x" * 30000],
    ["status", "-p", "x" * 30000],
])
def test_bad_arguments_end_in_one_line_not_a_traceback(project, args):
    r = run_cli(args, cwd=project)
    err = _err(r)
    assert r.returncode == 2, (r.returncode, err)
    assert "Traceback" not in err and err.startswith("devmem: "), err


@pytest.mark.parametrize("port", ["-5", "70000", "99999999999999999999", "abc"])
def test_port_is_checked_by_the_parser(project, port):
    r = run_cli(["app", "--dry-run", "--port", port], cwd=project)
    err = _err(r)
    assert r.returncode == 2 and "Traceback" not in err and "port" in err, err


def test_an_unexpected_error_is_one_line_and_debug_shows_the_traceback(project, monkeypatch, capsys):
    def boom(a):
        raise RuntimeError("something odd")
    parser = cli.build_parser

    def patched():
        ap = parser()
        for action in ap._subparsers._group_actions[0].choices.values():   # every subcommand fails the same way
            action.set_defaults(func=boom)
        return ap
    monkeypatch.setattr(cli, "build_parser", patched)
    monkeypatch.delenv("DEVMEM_DEBUG", raising=False)
    assert cli.main(["status", "-p", str(project)]) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err and "something odd" in err and err.count("\n") <= 2
    monkeypatch.setenv("DEVMEM_DEBUG", "1")
    assert cli.main(["status", "-p", str(project)]) == 2
    assert "Traceback" in capsys.readouterr().err
