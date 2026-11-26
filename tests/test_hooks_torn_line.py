"""A short write to a session's event log (a full disk) must not swallow the next event.

`_append` raised on a short write but left the partial bytes without a newline; the next append landed on the same
line and read_events dropped the merged line, so an event written after space came back was lost as well
(reproduced in Docker with a 64 KB tmpfs: sec/fuzz/r6_docker_torn_line.py). Every record now starts with a newline,
so a torn record ends at the next one, and `devmem doctor` reports torn lines in live logs.
"""
from __future__ import annotations

import json
import os

from cli_helpers import run_cli
from hooks_helpers import make_project

from devmem import hooks
from devmem.sessions import read_events


def _short_once(monkeypatch):
    """The next low-level append writes only the first half of its bytes and fails, like a disk that filled up."""
    state = {"armed": True}
    if os.name == "nt":
        real = hooks._win_append

        def fake(path, data):
            if state["armed"]:
                state["armed"] = False
                real(path, data[:len(data) // 2])
                raise OSError("short write (simulated full disk)")
            return real(path, data)
        monkeypatch.setattr(hooks, "_win_append", fake)
    else:
        real_write = os.write

        def fake_write(fd, data):
            if state["armed"]:
                state["armed"] = False
                return real_write(fd, bytes(data)[:len(data) // 2])
            return real_write(fd, data)
        monkeypatch.setattr(hooks.os, "write", fake_write)


def test_a_torn_record_does_not_swallow_the_next_one(tmp_path, monkeypatch):
    log = tmp_path / "live" / "s.jsonl"
    hooks._append(log, {"event": "first", "n": 1})
    _short_once(monkeypatch)
    try:
        hooks._append(log, {"event": "torn", "big": "A" * 4000})
    except OSError:
        pass
    hooks._append(log, {"event": "after", "n": 3})
    events = [e["event"] for e in read_events(log)]
    assert events == ["first", "after"], events


def test_doctor_reports_torn_lines_in_live_logs(tmp_path):
    project = make_project(tmp_path)
    live = project / ".devmemory" / "live"
    live.mkdir(parents=True)
    good = json.dumps({"event": "SessionStart", "session_id": "s1"})
    (live / "s1.jsonl").write_text(f"{good}\n{{\"event\": \"PostTo\n{good}\n", encoding="utf-8")
    out = run_cli(["doctor"], cwd=project).stdout.decode("utf-8", "replace")
    assert "torn" in out and "s1.jsonl" in out, out
