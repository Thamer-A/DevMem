"""L2 / L4 / L5: a FIFO (or another special file) a repository ships inside the store never blocks devmem.

Store files were read with a plain read_text: a FIFO shipped as `.devmemory/config.json` or `index/map.json` hung
pack, status, doctor and MCP and, after init, every hook (verify3/linux/G). A FIFO at a session lock blocked the
hook forever, and a lock naming a PID of 2**31 or more raised OverflowError on Linux (verify3/linux/E/crafted.py).
"""
from __future__ import annotations

import json
import os
import time

import pytest
from cli_helpers import cli_env, run_cli
from hooks_helpers import make_project

from devmem import hooks

pytestmark = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX")


@pytest.mark.parametrize("rel", ["config.json", "index/map.json", "index/index.db", "PROJECT.md"])
def test_a_fifo_in_the_store_does_not_block_commands_or_hooks(tmp_path, rel):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    target = store / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    os.mkfifo(target)
    for args in (["status"], ["doctor"], ["pack", "greet"]):
        t = time.monotonic()
        r = run_cli(args, cwd=project, timeout=60)
        assert time.monotonic() - t < 30, (args, r.stderr[-300:])
    payload = {"session_id": "fifo-0001", "cwd": str(project), "hook_event_name": "UserPromptSubmit",
               "prompt": "greet", "prompt_id": "p", "transcript_path": "x"}
    t = time.monotonic()
    r = run_cli(["hook", "UserPromptSubmit"], cwd=project, stdin=json.dumps(payload),
                env=cli_env(CLAUDE_PROJECT_DIR=str(project)), timeout=60)
    assert r.returncode == 0 and time.monotonic() - t < 30


def test_a_fifo_or_a_huge_pid_at_the_lock_is_stale(tmp_path):
    lock = tmp_path / "live" / "s.lock"
    lock.parent.mkdir()
    os.mkfifo(lock)
    with hooks._Lock(lock) as lk:
        assert lk.held
    lock.write_text(str(2 ** 40), encoding="utf-8")
    old = time.time() - 1
    os.utime(lock, (old, old))
    with hooks._Lock(lock) as lk:
        assert lk.held


def _returns_within(fn, seconds: float = 10.0):
    """(finished, error) of fn() run in a daemon thread: a call that blocks (an open of a FIFO without a reader)
    leaves `finished` False instead of hanging the test."""
    import threading
    box: dict = {}

    def run():
        try:
            fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e
        box["done"] = True
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(seconds)
    return bool(box.get("done")), box.get("error")


@pytest.mark.parametrize("name", ["live/fifo-0002.jsonl", "hook_errors.log", "hook_timing.log", "mcp_calls.jsonl"])
def test_a_fifo_at_an_appended_store_file_is_refused_at_once(tmp_path, name):
    """Appends opened O_WRONLY without O_NONBLOCK: a FIFO planted as `live/<session id>.jsonl` hung every hook event
    of that session until the hook timeout."""
    from devmem.store import open_append
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(target)
    done, err = _returns_within(lambda: hooks._append(target, {"event": "x"}))
    assert done and isinstance(err, OSError), err
    done, err = _returns_within(lambda: open_append(target).close())
    assert done and isinstance(err, OSError), err


def test_a_fifo_as_the_live_log_does_not_block_hook_events(tmp_path):
    project = make_project(tmp_path)
    sid = "fifo-0003"
    log = project / ".devmemory" / "live" / f"{sid}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(log)
    base = {"session_id": sid, "cwd": str(project), "transcript_path": "x"}
    for event, extra in (("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": "greet"}),
                         ("PostToolUse", {"tool_name": "Read", "tool_input": {"file_path": "x"}}),
                         ("Stop", {}), ("SessionEnd", {"reason": "other"})):
        t = time.monotonic()
        r = run_cli(["hook", event], cwd=project, stdin=json.dumps(dict(base, hook_event_name=event, **extra)),
                    env=cli_env(CLAUDE_PROJECT_DIR=str(project)), timeout=60)
        assert r.returncode == 0 and time.monotonic() - t < 15, (event, r.stderr[-300:])
