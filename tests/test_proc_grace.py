"""How devmem waits for a program it runs (fifth review, items 6 and 7): run_to_file (`devmem run`) and run_capped
(`devmem wrap`, `devmem codex`) wait EXIT_GRACE_S after the program ends for its output, one deadline for both
streams, not for a process it started that keeps them open; at a timeout they kill the tree and return (or raise)
after that grace at most; on POSIX, Ctrl-C is passed on to the program's process group (SIGINT, then SIGTERM, then
the tree is killed)."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from devmem import proc

PY = sys.executable


def _child(pidfile: Path, both: bool = True, life: int = 30) -> str:
    """A program that starts a process holding its stdout (and stderr), prints, and ends."""
    return (f"import subprocess, sys\n"
            f"g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({life})'], "
            f"stdout=sys.stdout, stderr={'sys.stderr' if both else 'subprocess.DEVNULL'})\n"
            f"open({str(pidfile)!r}, 'w').write(str(g.pid))\n"
            f"print('child', flush=True)\n")


def _kill(pidfile: Path) -> None:
    try:
        pid = int(pidfile.read_text())
    except (OSError, ValueError):
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def test_run_to_file_waits_one_grace_for_both_streams(tmp_path):
    """docs/LIMITS.md: `run` returns EXIT_GRACE_S after the program ends; with both streams held it took twice that."""
    pid = tmp_path / "gc.pid"
    try:
        t = time.monotonic()
        r = proc.run_to_file([PY, "-c", _child(pid)], tmp_path / "out.jsonl", grace=3.0)
        took = time.monotonic() - t
    finally:
        _kill(pid)
    assert r.output_not_waited is True and (tmp_path / "out.jsonl").read_bytes().strip() == b"child"
    assert took < 5.0, took                 # one 3 s grace (two sequential ones: over 6 s)


def test_one_ctrl_c_while_waiting_for_held_output_keeps_what_was_read(tmp_path, monkeypatch):
    """The reviewer's f10 phase "grace": the program ended, a process it started still held its output, and ONE Ctrl-C
    during that wait lost the whole run ("interrupted again ... nothing was recorded"): the wait was outside the
    handler. Now it is that run's first interrupt: what was read is returned, marked interrupted."""
    pid = tmp_path / "gc.pid"
    real = proc._wait_threads
    calls = []

    def ctrl_c_during_grace(threads, grace):
        calls.append(grace)
        if len(calls) == 1:
            real(threads, 1.0)              # the program's own output has arrived by then
            raise KeyboardInterrupt
        return real(threads, grace)
    monkeypatch.setattr(proc, "_wait_threads", ctrl_c_during_grace)
    try:
        r = proc.run_to_file([PY, "-c", _child(pid)], tmp_path / "out.jsonl", grace=3.0)
    finally:
        _kill(pid)
    assert r.interrupted is True and (tmp_path / "out.jsonl").read_bytes().strip() == b"child"


def test_run_capped_does_not_wait_for_a_process_that_holds_the_output(tmp_path):
    pid = tmp_path / "gc.pid"
    try:
        t = time.monotonic()
        r = proc.run_capped([PY, "-c", _child(pid)])
        took = time.monotonic() - t
    finally:
        _kill(pid)
    assert r.stdout.strip() == "child" and r.returncode == 0 and r.output_not_waited is True
    assert took < proc.EXIT_GRACE_S + 6, took      # it waited for the grandchild's 30 s


def test_run_capped_timeout_kills_the_tree_and_raises_after_the_grace(tmp_path):
    pid = tmp_path / "gc.pid"
    code = _child(pid) + "import time; time.sleep(60)\n"
    try:
        t = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired) as e:
            proc.run_capped([PY, "-c", code], timeout=2)
        took = time.monotonic() - t
    finally:
        _kill(pid)
    assert took < 2 + proc.EXIT_GRACE_S + 6, took
    assert "child" in (e.value.output or "")


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups and signals")
@pytest.mark.parametrize("ignores", [False, True], ids=["ends_on_sigint", "ignores_sigint"])
def test_ctrl_c_is_passed_on_to_the_program(tmp_path, ignores):
    marks = tmp_path / "marks"
    marks.mkdir()
    child = ("import signal, sys, time\n"
             f"def on(sig, _f):\n    open({str(marks)!r} + '/' + signal.Signals(sig).name, 'w').close()\n"
             f"    {'pass' if ignores else 'sys.exit(3)'}\n"
             "signal.signal(signal.SIGINT, on)\nsignal.signal(signal.SIGTERM, on)\n"
             f"open({str(marks)!r} + '/ready', 'w').close()\n"
             "while True: time.sleep(0.1)\n")
    parent = ("import sys\n"
              f"sys.path.insert(0, {str(Path(proc.__file__).resolve().parent.parent)!r})\n"
              "from devmem import proc\nproc.INTERRUPT_GRACE_S = 1.0\n"
              f"r = proc.run_to_file([sys.executable, '-c', {child!r}], {str(tmp_path / 'out')!r})\n"
              "print('interrupted' if r.interrupted else 'ended', flush=True)\n")   # it returns what it captured
    p = subprocess.Popen([PY, "-c", parent], stdout=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 30
    while not (marks / "ready").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    p.send_signal(signal.SIGINT)              # what the terminal sends: devmem's group only (the program has its own)
    out, _ = p.communicate(timeout=30)
    assert "interrupted" in out and (marks / "SIGINT").exists()
    assert (marks / "SIGTERM").exists() is ignores
