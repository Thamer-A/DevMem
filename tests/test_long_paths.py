"""Deep folders on Windows with long paths off: devmem does not work around the 260-character limit, but says why
when it is the cause (and only then), and `devmem init` warns when the store's path leaves too little room."""
import os
from pathlib import Path

import pytest

from devmem import cli

windows = pytest.mark.skipif(os.name != "nt", reason="the 260-character limit is Windows' (long paths off)")


def _long(n: int) -> str:
    return "C:\\" + "a" * (n - 3)


@windows
def test_hint_names_the_limit_for_a_long_path_that_was_not_found(monkeypatch):
    monkeypatch.setattr(cli, "long_paths_enabled", lambda: False)
    hint = cli._long_path_hint(FileNotFoundError(2, "No such file or directory", _long(265)))
    assert "265 characters" in hint and "LongPathsEnabled" in hint
    # os.replace and friends: the long one may be the second name
    assert "270 characters" in cli._long_path_hint(FileNotFoundError(2, "x", "C:\\short", None, _long(270)))
    # a new folder: Windows error 206 from 248 characters on
    assert "250 characters" in cli._long_path_hint(OSError(22, "x", _long(250), 206))


@windows
def test_hint_stays_silent_for_any_other_cause(monkeypatch):
    monkeypatch.setattr(cli, "long_paths_enabled", lambda: False)
    assert cli._long_path_hint(FileNotFoundError(2, "x", _long(259))) == ""                # under the limit
    assert cli._long_path_hint(FileNotFoundError(2, "x")) == ""                            # no file name
    assert cli._long_path_hint(PermissionError(13, "x", _long(300))) == ""                 # another error
    assert cli._long_path_hint(OSError(22, "x", _long(240), 206)) == ""                    # 206 under 248
    assert cli._long_path_hint(FileNotFoundError(2, "x", "\\\\?\\" + _long(300))) == ""    # already a long path
    monkeypatch.setattr(cli, "long_paths_enabled", lambda: True)
    assert cli._long_path_hint(FileNotFoundError(2, "x", _long(300))) == ""                # limit lifted


@pytest.mark.skipif(os.name == "nt", reason="elsewhere")
def test_no_hint_and_no_warning_off_windows(tmp_path):
    assert cli._long_path_hint(FileNotFoundError(2, "x", "/" + "a" * 400)) == ""
    assert cli.store_path_warning(Path("/" + "a" * 400)) is None


@windows
def test_cli_error_carries_the_hint_only_for_that_cause(monkeypatch, capsys):
    monkeypatch.setattr(cli, "long_paths_enabled", lambda: False)
    name = _long(270)

    def boom(exc):
        def run(_a):
            raise exc
        return run

    for exc, hinted in ((FileNotFoundError(2, "No such file or directory", name), True),
                        (PermissionError(13, "Access is denied", name), False),
                        (FileNotFoundError(2, "No such file or directory", "C:\\x\\y.txt"), False)):
        monkeypatch.setattr(cli, "cmd_status", boom(exc))
        assert cli.main(["status"]) == 2
        err = capsys.readouterr().err
        assert ("over the limit Windows applies" in err) is hinted, err


@windows
def test_store_path_warning_threshold(monkeypatch):
    monkeypatch.setattr(cli, "long_paths_enabled", lambda: False)
    assert cli.store_path_warning(Path(_long(260 - cli.STORE_NAME_ROOM - 1))) is None
    warn = cli.store_path_warning(Path(_long(260 - cli.STORE_NAME_ROOM)))
    assert warn and "LongPathsEnabled" in warn
    monkeypatch.setattr(cli, "long_paths_enabled", lambda: True)
    assert cli.store_path_warning(Path(_long(250))) is None


def _deep(tmp_path: Path, length: int) -> Path:
    if len(str(tmp_path)) > length - 20:
        pytest.skip(f"the test folder is already {len(str(tmp_path))} characters long")
    p = tmp_path / "p"
    while len(str(p)) < length - 12:
        p = p / ("d" * 10)
    p = Path(str(p / ("e" * 12))[:length])
    p.mkdir(parents=True)
    (p / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return p


@windows
def test_init_warns_in_a_deep_project_and_works_below_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "long_paths_enabled", lambda: False)
    for length, warned in ((150, False), (190, True)):
        project = _deep(tmp_path / str(length), length)
        assert cli.main(["init", "--project", str(project), "--no-scan"]) == 0
        out = capsys.readouterr()
        assert ("warning: the store path is" in out.out + out.err) is warned, out


@windows
@pytest.mark.skipif(os.name == "nt" and cli.long_paths_enabled(), reason="this machine has long paths on")
def test_scan_in_a_too_deep_project_fails_with_the_reason(tmp_path, capsys):
    project = _deep(tmp_path, 235)
    assert cli.main(["scan", "--project", str(project)]) == 2
    err = capsys.readouterr().err
    assert "file not found" in err and "over the limit Windows applies" in err, err
