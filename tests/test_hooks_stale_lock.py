"""The session lock is an OS lock: a hook killed while holding it leaves nothing to take over, and a live holder is
never broken into.

Before, the lock was a file holding the holder's PID. A contender judged it stale (dead PID, or older than 30 s) and
removed it with a separate remove, and the holder's release removed whatever file was at the path: two live hooks
could hold it at once (security review L3: 3 of 30 trials, `state.json` lost turns), and a lock held longer than 30 s
was taken over while its holder still ran. When the lock could not be taken in time the handlers went on unlocked and
rebuilt the state and the record anyway (E2: concurrent Stop hooks wrote 9 records for one session id and reset the
turn count). Now the OS releases the lock when its holder exits; a handler without the lock only appends its event;
and a session whose state file is lost keeps writing its one record.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest
from cli_helpers import PKG_ROOT
from hooks_helpers import make_project

from devmem import hooks
from devmem import sessions as S


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_a_lock_file_left_by_a_dead_process_is_taken_at_once(tmp_path):
    lock = tmp_path / "live" / "s.lock"
    lock.parent.mkdir()
    lock.write_text(str(_dead_pid()), encoding="utf-8")
    t = time.monotonic()
    with hooks._Lock(lock) as lk:
        assert lk.held
    assert time.monotonic() - t < 1.0
    assert not lock.exists()


def test_an_old_lock_is_never_taken_from_a_live_holder(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "LOCK_TIMEOUT_S", 0.3)
    lock = tmp_path / "live" / "s.lock"
    with hooks._Lock(lock) as a:
        assert a.held
        old = time.time() - 3600                       # was taken over after 30 s whoever held it
        os.utime(lock, (old, old))
        with hooks._Lock(lock) as b:
            assert not b.held and not b.cannot_write
        assert lock.exists()                           # b's exit removed nothing
    assert not lock.exists()                           # the holder's release removes its own file


_HOLDER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, sys.argv[2])
    from pathlib import Path
    from devmem import hooks
    with hooks._Lock(Path(sys.argv[1])) as lk:
        print("held" if lk.held else "no", flush=True)
        time.sleep(60)
""")


def test_a_holder_killed_while_holding_the_lock_releases_it(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "LOCK_TIMEOUT_S", 0.3)
    lock = tmp_path / "live" / "s.lock"
    child = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock), str(PKG_ROOT)], stdout=subprocess.PIPE,
                             text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        with hooks._Lock(lock) as lk:
            assert not lk.held                          # a live holder is waited for
    finally:
        child.kill()
        child.wait()
    monkeypatch.setattr(hooks, "LOCK_TIMEOUT_S", 3.0)
    t = time.monotonic()
    with hooks._Lock(lock) as lk:
        assert lk.held
    assert time.monotonic() - t < 2.0


_COUNTER = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[3])
    from pathlib import Path
    from devmem import hooks
    hooks.LOCK_TIMEOUT_S = 60.0
    lock, counter = Path(sys.argv[1]), Path(sys.argv[2])
    for _ in range(25):
        with hooks._Lock(lock) as lk:
            assert lk.held
            n = int(counter.read_text() or 0)
            counter.write_text(str(n + 1))
""")


def test_processes_never_hold_the_lock_at_the_same_time(tmp_path):
    """Read-modify-write of a counter under the lock from 8 processes, starting from a lock file a dead process left:
    no increment may be lost."""
    lock = tmp_path / "live" / "s.lock"
    lock.parent.mkdir()
    lock.write_text(str(_dead_pid()), encoding="utf-8")
    old = time.time() - 1.0
    os.utime(lock, (old, old))
    counter = tmp_path / "n"
    counter.write_text("0")
    procs = [subprocess.Popen([sys.executable, "-c", _COUNTER, str(lock), str(counter), str(PKG_ROOT)])
             for _ in range(8)]
    assert all(p.wait(timeout=180) == 0 for p in procs)
    assert counter.read_text() == str(8 * 25)


def test_something_else_at_the_lock_path_is_replaced_or_given_up_on(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "LOCK_TIMEOUT_S", 0.3)
    lock = tmp_path / "live" / "s.lock"
    lock.mkdir(parents=True)                            # an empty folder: removed
    with hooks._Lock(lock) as lk:
        assert lk.held
    lock.mkdir()
    (lock / "x").write_text("x")                       # a folder with content: never emptied, given up on at once
    t = time.monotonic()
    with hooks._Lock(lock) as lk:
        assert not lk.held
    assert time.monotonic() - t < 0.25 and (lock / "x").exists()


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
def test_a_link_at_the_lock_path_is_removed_never_followed(tmp_path):
    target = tmp_path / "target"
    target.write_text("keep", encoding="utf-8")
    lock = tmp_path / "live" / "s.lock"
    lock.parent.mkdir()
    os.symlink(target, lock)
    with hooks._Lock(lock) as lk:
        assert lk.held
    assert target.read_text(encoding="utf-8") == "keep"


def _payload(project, sid, event, **kw):
    p = {"session_id": sid, "cwd": str(project), "hook_event_name": event, "transcript_path": str(project / "none")}
    p.update(kw)
    return json.dumps(p).encode("utf-8")


def test_without_the_lock_an_event_is_appended_and_nothing_else_is_written(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    store = project / ".devmemory"
    sid = "nolock-session"
    hooks.run(["SessionStart"], _payload(project, sid, "SessionStart", source="startup"))
    hooks.run(["UserPromptSubmit"], _payload(project, sid, "UserPromptSubmit", prompt="first", prompt_id="p1"))
    hooks.run(["Stop"], _payload(project, sid, "Stop", last_assistant_message="one"))
    state = (store / "live" / f"{sid}.state.json").read_bytes()
    metas = sorted((store / "sessions").glob("*.meta.json"))
    meta = metas[0].read_bytes()

    class NoLock(hooks._Lock):
        def __enter__(self):
            return self                                  # never held, as when the deadline passes
    monkeypatch.setattr(hooks, "_Lock", NoLock)
    for ev, kw in [("UserPromptSubmit", {"prompt": "second", "prompt_id": "p2"}),
                   ("Stop", {"last_assistant_message": "two"}), ("PostCompact", {"compact_summary": "s"}),
                   ("SessionEnd", {"reason": "exit"})]:
        assert hooks.run([ev], _payload(project, sid, ev, **kw))[0] == 0
    assert (store / "live" / f"{sid}.state.json").read_bytes() == state
    assert sorted((store / "sessions").glob("*.meta.json")) == metas and metas[0].read_bytes() == meta
    events = [e["event"] for e in S.read_events(store / "live" / f"{sid}.jsonl")]
    assert events[-4:] == ["UserPromptSubmit", "Stop", "PostCompact", "SessionEnd"]
    assert not (store / "hook_errors.log").exists()


def test_a_session_whose_state_is_lost_keeps_its_one_record(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    store = project / ".devmemory"
    sid = "lost-state-session"
    hooks.run(["UserPromptSubmit"], _payload(project, sid, "UserPromptSubmit", prompt="first", prompt_id="p1"))
    hooks.run(["Stop"], _payload(project, sid, "Stop", last_assistant_message="one"))
    (store / "live" / f"{sid}.state.json").unlink()
    later = "2031-01-01T00:00:00+00:00"                  # a new state would name a new record after its start time
    monkeypatch.setattr(S, "now_iso", lambda ms=False: later)
    hooks.run(["UserPromptSubmit"], _payload(project, sid, "UserPromptSubmit", prompt="second", prompt_id="p2"))
    hooks.run(["Stop"], _payload(project, sid, "Stop", last_assistant_message="two"))
    metas = sorted((store / "sessions").glob("*.meta.json"))
    assert len(metas) == 1, metas
    rec = json.loads(metas[0].read_text(encoding="utf-8"))
    assert [t["prompt"] for t in rec["turns"]] == ["first", "second"]
