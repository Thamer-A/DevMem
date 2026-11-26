"""FG-6 for the hooks: a network project folder (CLAUDE_PROJECT_DIR, or the payload's cwd when that is not set) is
refused before anything stats, resolves or opens it, as the CLI's _local_arg does for its arguments. On Windows a
stat of `\\\\host\\share\\...` contacts the host (seconds, and an authentication attempt with the user's
credentials). The init route's switch check resolved the path (os.path.realpath) and both routes is_dir-ed its store.
"""
from __future__ import annotations

import io
import json
import ntpath
import os
import sys

import pytest

from devmem import hooks, plugin

REMOTES = ["\\\\server.invalid\\share\\proj", "//server.invalid/share/proj", "\\\\?\\UNC\\server.invalid\\share\\p"]


@pytest.fixture()
def no_unc_touch(monkeypatch):
    touched: list[str] = []

    def check(p):
        try:
            s = os.fspath(p)
        except TypeError:
            return
        if isinstance(s, bytes):
            s = s.decode("utf-8", "replace")
        low = s.replace("/", "\\").lower()
        if low.startswith("\\\\") and (not low.startswith(("\\\\?\\", "\\\\.\\")) or low.startswith("\\\\?\\unc\\")):
            touched.append(s)
            raise AssertionError(f"a network spelling was touched: {s}")

    def wrap(fn):
        def inner(p, *a, **k):
            check(p)
            return fn(p, *a, **k)
        return inner
    for name in ("stat", "lstat", "open", "scandir", "listdir"):
        monkeypatch.setattr(os, name, wrap(getattr(os, name)))
    monkeypatch.setattr(os.path, "realpath", wrap(os.path.realpath))
    if hasattr(ntpath, "_getfinalpathname"):
        monkeypatch.setattr(ntpath, "_getfinalpathname", wrap(ntpath._getfinalpathname))
    return touched


def _payload(cwd=None, event="UserPromptSubmit") -> bytes:
    p = {"session_id": "remote-0001", "hook_event_name": event, "prompt": "x", "transcript_path": "t"}
    if cwd:
        p["cwd"] = cwd
    return json.dumps(p).encode()


@pytest.mark.parametrize("remote", REMOTES)
@pytest.mark.parametrize("route", ["init", "plugin"])
def test_a_network_claude_project_dir_is_never_touched(no_unc_touch, monkeypatch, remote, route):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", remote)
    assert hooks.run(["UserPromptSubmit"], _payload(remote), route=route) == (0, "")
    assert not no_unc_touch


@pytest.mark.parametrize("remote", REMOTES)
@pytest.mark.parametrize("route", ["init", "plugin"])
def test_a_network_payload_cwd_is_never_touched(no_unc_touch, monkeypatch, remote, route):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert hooks.run(["UserPromptSubmit"], _payload(remote), route=route) == (0, "")
    assert not no_unc_touch


def test_a_network_store_argument_is_never_touched(no_unc_touch, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert hooks.run(["UserPromptSubmit", "--store", REMOTES[0] + "\\.devmemory"], _payload()) == (0, "")
    assert not no_unc_touch


@pytest.mark.parametrize("remote", REMOTES)
def test_the_plugin_entry_point_never_touches_a_network_project(no_unc_touch, monkeypatch, remote):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", remote)
    assert plugin._project_from(b"{}") is None
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    assert plugin._project_from(_payload(remote)) is None
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(_payload(remote))))
    assert plugin.hook_main(["UserPromptSubmit"]) == 0
    assert not no_unc_touch


def test_local_paths_still_work(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert plugin._project_from(b"{}") == tmp_path
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    assert plugin._project_from(_payload(str(tmp_path))) == tmp_path
