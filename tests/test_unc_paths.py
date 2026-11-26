"""A path from a hook payload or a tool input that names a network share is never touched on disk.

sessions.rel_path and the secret-file check in redact resolved tool-input paths: `\\\\host\\share\\x.py` made Windows
contact that host (1.3 to 3 s per event, plus an authentication attempt; reviewer's r7_unc_path.py). Such paths
(`\\\\host\\...`, `//host/...`, `\\\\?\\UNC\\host\\...`) are now handled as text only. No host is contacted here:
Path.resolve, os.stat and os.lstat are replaced by functions that fail the test when given a UNC path.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from devmem import redact
from devmem import sessions as S
from devmem.store import is_remote_path

UNC = ["\\\\nonexistent-host-zz9\\share\\x.py", "//nonexistent-host-zz9/share/x.py",
       "\\\\?\\UNC\\nonexistent-host-zz9\\share\\x.py", "\\\\nonexistent-host-zz9\\share\\.env"]


@pytest.fixture()
def no_network(monkeypatch):
    touched = []

    def guard(fn):
        def wrapped(p, *a, **k):
            s = os.fspath(p)
            if s.startswith(("\\\\", "//")) and not s.lower().startswith(("\\\\?\\c:", "\\\\.\\c:")):
                touched.append(s)
                raise AssertionError(f"filesystem call on a UNC path: {s}")
            return fn(p, *a, **k)
        return wrapped
    real_resolve = pathlib.Path.resolve
    monkeypatch.setattr(pathlib.Path, "resolve", lambda self, *a, **k: guard(lambda p: real_resolve(p, *a, **k))(self))
    monkeypatch.setattr(os, "stat", guard(os.stat))
    monkeypatch.setattr(os, "lstat", guard(os.lstat))
    monkeypatch.setattr(os.path, "realpath", guard(os.path.realpath))
    return touched


def test_is_remote_path():
    for p in UNC:
        assert is_remote_path(p), p
    local = ["C:\\x\\y.py", "/home/me/y.py", "src/y.py"]
    if os.name == "nt":            # device spellings of a drive path are folded to it on Windows only
        local += ["\\\\?\\C:\\x\\y.py", "\\\\.\\C:\\x", "\\\\localhost\\C$\\x"]
    for p in local:
        assert not is_remote_path(p), p


def test_rel_path_never_touches_a_share(tmp_path, no_network):
    for p in UNC:
        assert S.rel_path(p, tmp_path) == p.replace("\\", "/")
    assert S.rel_path(str(tmp_path / "a.py"), tmp_path) == "a.py"
    assert not no_network


def test_secret_file_check_never_touches_a_share(tmp_path, no_network):
    assert redact.input_names_secret_file({"file_path": UNC[3]}, tmp_path)
    assert not redact.input_names_secret_file({"file_path": UNC[0]}, tmp_path)
    assert not no_network


def test_a_transcript_on_a_share_is_not_opened(no_network):
    assert S.read_transcript_since(UNC[0], 0) == ([], 0)
    assert not no_network
