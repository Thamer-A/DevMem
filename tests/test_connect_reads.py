"""L2, reads outside the store: the files `devmem connect` edits and `devmem doctor` checks (in the project), and the
final message `devmem codex` gets back from codex (in devmem's private temp folder). Each is read only as a plain
file of bounded size: a FIFO there must not block, and a link must not be followed out of the project (or, for
codex's message, at all).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import connect
from devmem.connect import ConnectError

posix = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX")
ENTRY = {"mcpServers": {"devmemory": {"command": "/elsewhere/python", "args": ["-m", "devmem", "mcp"]}}}


def _in_time(fn, timeout: float = 20.0, release=None):
    """fn() in a thread; fails when it has not finished after `timeout` (then opens `release`, a FIFO or a function
    giving one, for writing so the blocked reader gets end of file and the thread ends)."""
    out: dict = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - handed to the test
            out["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive() and release is not None:
        release = release() if callable(release) else release
        os.close(os.open(release, os.O_WRONLY | os.O_NONBLOCK))
        t.join(5)
        pytest.fail(f"blocked on {release}")
    assert not t.is_alive()
    return out


@posix
def test_connect_refuses_a_fifo_instruction_file_instead_of_blocking(tmp_path):
    project = make_project(tmp_path)
    os.mkfifo(project / "AGENTS.md")
    out = _in_time(lambda: connect.connect(project, "generic", dry_run=True), release=project / "AGENTS.md")
    assert isinstance(out.get("error"), ConnectError) and "not a plain file" in str(out["error"])


@posix
def test_doctor_check_of_mcp_files_never_blocks_or_leaves_the_project(tmp_path):
    project = make_project(tmp_path)
    (project / ".cursor").mkdir()
    os.mkfifo(project / ".cursor" / "mcp.json")
    out = _in_time(lambda: connect.mcp_entry(project.resolve(), connect.TOOLS["cursor"]),
                   release=project / ".cursor" / "mcp.json")
    assert out == {"value": None}
    outside = tmp_path / "elsewhere.json"
    outside.write_text(json.dumps(ENTRY), encoding="utf-8")
    (project / ".cursor" / "mcp.json").unlink()
    (project / ".cursor" / "mcp.json").symlink_to(outside)
    assert connect.mcp_entry(project.resolve(), connect.TOOLS["cursor"]) is None
    assert connect.connected_tools(project) == []
    inside = project / "cfg.json"                      # a link inside the project is still followed
    inside.write_text(json.dumps(ENTRY), encoding="utf-8")
    (project / ".cursor" / "mcp.json").unlink()
    (project / ".cursor" / "mcp.json").symlink_to(inside)
    assert connect.mcp_entry(project.resolve(), connect.TOOLS["cursor"]) == ENTRY["mcpServers"]["devmemory"]


def test_a_folder_in_place_of_an_mcp_or_instruction_file(tmp_path):
    project = make_project(tmp_path)
    (project / ".cursor" / "mcp.json").mkdir(parents=True)
    assert connect.mcp_entry(project.resolve(), connect.TOOLS["cursor"]) is None
    (project / "AGENTS.md").mkdir()
    with pytest.raises(ConnectError, match="not a plain file"):
        connect.connect(project, "generic", dry_run=True)


def test_an_oversized_instruction_file_is_refused(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    (project / "AGENTS.md").write_text("x" * 2048, encoding="utf-8")
    monkeypatch.setattr(connect, "CONNECT_READ_CAP", 1024)
    with pytest.raises(ConnectError, match="over"):
        connect.connect(project, "generic", dry_run=True)


def _fake_codex(tmp_path, monkeypatch, plant):
    """`devmem codex` with codex faked: `plant(last)` puts something at the path codex writes its last message to."""
    from devmem import store
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    exe = fake_bin / ("codex.exe" if os.name == "nt" else "codex")
    exe.write_text("@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
    store._PROGRAMS.clear()
    planted = {}
    from devmem import proc

    def fake_run(cmd, **kw):                             # devmem runs codex through proc.run_capped
        last = Path(cmd[cmd.index("-o") + 1])
        planted["last"] = last
        plant(last)
        return proc.Captured(0, ("", "", 0), ("", "", 0))
    monkeypatch.setattr(proc, "run_capped", fake_run)
    return planted


def test_codex_last_message_is_read_as_a_plain_file(tmp_path, monkeypatch):
    from devmem import generic
    project = make_project(tmp_path)
    _fake_codex(tmp_path, monkeypatch, lambda last: last.write_text("all done\n", encoding="utf-8"))
    meta = json.loads(generic.codex(project, "do it", store=project / ".devmemory", with_pack=False)
                      .read_text(encoding="utf-8"))
    assert meta["result_text"] == "all done"


def test_codex_last_message_link_is_not_followed(tmp_path, monkeypatch):
    from devmem import generic
    project = make_project(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("NOT-FOR-THE-RECORD", encoding="utf-8")
    probe = tmp_path / "probe"
    try:
        probe.symlink_to(secret)
    except OSError:
        pytest.skip("symbolic links need a privilege here")
    _fake_codex(tmp_path, monkeypatch, lambda last: last.symlink_to(secret))
    rec = generic.codex(project, "do it", store=project / ".devmemory", with_pack=False)
    assert not json.loads(rec.read_text(encoding="utf-8")).get("result_text")
    assert not any(b"NOT-FOR-THE-RECORD" in f.read_bytes() for f in (project / ".devmemory").rglob("*") if f.is_file())


@posix
def test_codex_last_message_fifo_never_blocks(tmp_path, monkeypatch):
    from devmem import generic
    project = make_project(tmp_path)
    planted = _fake_codex(tmp_path, monkeypatch, lambda last: os.mkfifo(last))
    out = _in_time(lambda: generic.codex(project, "do it", store=project / ".devmemory", with_pack=False),
                   release=lambda: planted["last"])
    if "error" in out:
        raise out["error"]
    assert "last" in planted and not json.loads(out["value"].read_text(encoding="utf-8")).get("result_text")
