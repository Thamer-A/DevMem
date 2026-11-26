"""Path fields in tool input cost bounded time (security review R3).

Path.resolve() of a long path that does not exist is superlinear on Windows, and nothing capped path fields in tool
input: a Grep `path` of "a/" * 10000 (20 KB) kept one PostToolUse hook busy 9.8 s, an MCP call with 200 paths of
4 KB 36.6 s. Now a value longer than MAX_PATH_CHARS is not a path (recorded as `path_omitted`, its last name still
checked for a secret file), resolve() runs only for paths that exist, and the per-input checking is budgeted.
"""
from __future__ import annotations

import os
import time

import pytest

from devmem import redact as RD
from devmem import sessions as S


def _timed(fn, *a):
    t = time.perf_counter()
    out = fn(*a)
    return out, time.perf_counter() - t


@pytest.mark.parametrize("n", [2000, 10000, 30000])
def test_a_long_missing_path_is_cheap(tmp_path, n):
    p = "a/" * n
    red = RD.Redactor(None, enabled=True)
    entries, dt = _timed(S.tool_entries, "Grep", {"pattern": "x", "path": p}, tmp_path, red)
    assert dt < 1.0
    if len(p) > RD.MAX_PATH_CHARS:
        # a path the check cannot read in full: the call is recorded as not checked, nothing of its input is kept
        assert "path" not in entries[0] and entries[0]["input_not_checked"] is True
    entries, dt = _timed(S.tool_entries, "Read", {"file_path": "a\\" * n}, tmp_path, red)
    assert dt < 1.0
    _, dt = _timed(RD.input_names_secret_file, {"path": p}, tmp_path)
    assert dt < 1.0


def test_many_long_paths_in_one_input_are_cheap(tmp_path):
    inp = {"paths": [f"d{i}/" + "a/" * 2000 for i in range(200)]}
    out, dt = _timed(RD.tool_reads_secret_file, "mcp__fs__read_multiple_files", inp, tmp_path)
    assert out is False and dt < 2.0


def test_an_overlong_path_is_still_checked_by_its_name(tmp_path):
    assert RD.input_names_secret_file({"path": "a/" * 5000 + ".env"}, tmp_path)
    assert RD.input_names_secret_file({"paths": ["x"] * 20 + ["a/" * 3000 + "id_rsa"]}, tmp_path)
    red = RD.Redactor(None, enabled=True)
    e = S.tool_entries("Read", {"file_path": "a/" * 5000 + ".env"}, tmp_path, red)[0]
    assert e.get("secret_file") and "file" not in e


def test_too_many_path_values_count_as_a_secret_read(tmp_path):
    assert RD.input_names_secret_file({"paths": [f"f{i}.py" for i in range(RD.MAX_PATH_VALUES + 1)]}, tmp_path)
    assert not RD.input_names_secret_file({"paths": [f"f{i}.py" for i in range(50)]}, tmp_path)


def test_ordinary_paths_keep_their_project_relative_form(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x", encoding="utf-8")
    assert S.rel_path(str(tmp_path / "src" / "a.py"), tmp_path) == "src/a.py"
    assert S.rel_path(str(tmp_path / "src" / "new.py"), tmp_path) == "src/new.py"          # parent exists
    assert S.rel_path(str(tmp_path / "no" / "such" / "x.py"), tmp_path) == "no/such/x.py"  # lexical
    outside = str(tmp_path.parent / "elsewhere.py")
    assert S.rel_path(outside, tmp_path) == outside.replace("\\", "/")


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
def test_a_link_out_of_the_project_counts_as_outside(tmp_path):
    proj, home = tmp_path / "proj", tmp_path / "home"
    proj.mkdir()
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text("x", encoding="utf-8")
    os.symlink(home / ".ssh", proj / "keys")
    assert S.rel_path(str(proj / "keys" / "config"), proj) == str(proj / "keys" / "config")
    assert RD._project_rel(str(proj / "keys" / "config"), proj).endswith(".ssh/config")


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
def test_an_ssh_identity_reached_through_a_link_in_the_project_is_not_kept(tmp_path):
    # (the test's name, and so tmp_path, avoids words of the secret-file rule: an outside path is checked whole)
    proj, home = tmp_path / "proj", tmp_path / "home"
    proj.mkdir()
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("Host x\n", encoding="utf-8")
    (home / "notes.txt").write_text("x", encoding="utf-8")
    os.symlink(home / ".ssh", proj / "cfgdir")
    os.symlink(home / ".ssh" / "id_rsa", proj / "deploy_identity")
    os.symlink(home / "notes.txt", proj / ".env")              # a secret name linked elsewhere still counts
    assert not RD.is_secret_file(str(proj / "deploy_identity"))     # only the real target makes it one
    for value in (str(proj / "deploy_identity"), "deploy_identity", "cfgdir/../deploy_identity",
                  str(proj / "cfgdir" / "id_rsa"), str(proj / ".env")):
        cwd = os.getcwd()
        os.chdir(proj)
        try:
            assert RD.input_names_secret_file({"file_path": value}, proj), value
            assert RD.tool_reads_secret_file("Read", {"file_path": value}, proj), value
            text, omitted = RD.filter_tool_output("Read", {"file_path": value}, "Host x", proj)
            assert omitted and "Host x" not in text, value
        finally:
            os.chdir(cwd)
    assert not RD.input_names_secret_file({"file_path": str(home / "notes.txt")}, proj)
    assert not RD.input_names_secret_file({"file_path": "src/a.py"}, proj)
