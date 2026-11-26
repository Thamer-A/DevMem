"""A path or argument holding a NUL character is refused cleanly, never with a bare `ValueError: embedded null
character` from the OS layer (found by the fuzzers: store.git_argv raised it through _filter_names and proc.run)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from devmem import proc, store

NUL = chr(0)


def test_git_argv_refuses_a_project_path_with_nul(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text('[filter "x"]\n\tclean = cat\n', encoding="utf-8")
    with pytest.raises(store.GitUnsafe) as e:
        store.git_argv(str(tmp_path) + NUL + "x", "status")
    assert isinstance(e.value, OSError)          # callers treat it as "no git here"
    assert "NUL" in str(e.value)


def test_git_argv_refuses_an_argument_with_nul(tmp_path):
    with pytest.raises(store.GitUnsafe):
        store.git_argv(tmp_path, "show", "HEAD:a" + NUL + "b")


def test_git_callers_work_as_without_git(tmp_path):
    bad = Path(str(tmp_path) + NUL + "x")
    assert store.tracked_in_store(bad, bad / ".devmemory") == []
    assert store._git_list(bad) is None
    assert store.list_files(bad) == ([], "walk")


@pytest.mark.parametrize("argv", [[sys.executable, "-c", "pass", "a" + NUL + "b"],
                                  [sys.executable + NUL + "x", "-c", "pass"]])
def test_proc_refuses_an_argument_with_nul(argv):
    for call in (lambda: proc.check(argv), lambda: proc.run(argv, capture_output=True),
                 lambda: proc.popen(argv)):
        with pytest.raises(proc.UnsafeCommand) as e:
            call()
        assert isinstance(e.value, OSError) and "NUL" in str(e.value)


def test_not_a_project_and_store_folder_refuse_nul(tmp_path):
    bad = Path(str(tmp_path) + NUL + "x")
    assert store.not_a_project(bad) == "nul"
    assert "NUL" in store.NOT_A_PROJECT_TEXT["nul"]
    with pytest.raises(store.NotAStoreError):
        store.check_store_folder(bad)
    with pytest.raises(store.NotAStoreError):
        store.ensure_store(bad / ".devmemory")
    assert store.not_a_project(tmp_path) is None
