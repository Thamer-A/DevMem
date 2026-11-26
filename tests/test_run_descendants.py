"""`devmem run` (proc.run_to_file) returns soon after its program ends even when a process the program started keeps
its output open, kills the program's tree at the timeout, and keeps what it counted in either case (fourth review:
it waited for any descendant holding stdout, 40 s, or forever; at its timeout it lost the count of bytes left out)."""
from __future__ import annotations

import json
import os
import sys
import time

import pytest
from hooks_helpers import make_project

from devmem import capture as C
from devmem import claude_cli, proc

PY = sys.executable


def _grandchild(seconds: int, new_session: bool, pid_file=None) -> str:
    """Program text: start a grandchild that inherits stdout and stderr and lives `seconds`."""
    flags = ("start_new_session=True" if os.name != "nt" else
             "creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008") if new_session else ""
    write_pid = f"open({str(pid_file)!r}, 'w').write(str(g.pid))\n" if pid_file else ""
    return ("import subprocess, sys, time\n"
            f"g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({seconds})'], close_fds=False, "
            f"{flags})\n"
            + write_pid)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259                                             # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:                                                                     # a zombie is not alive
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


@pytest.mark.parametrize("new_session", [False, True])
def test_a_descendant_holding_the_output_is_not_waited_for(tmp_path, new_session):
    code = _grandchild(40, new_session) + "sys.stdout.buffer.write(b'{\"type\": \"result\"}\\n'); sys.stdout.flush()\n"
    out = tmp_path / "s.jsonl"
    t0 = time.monotonic()
    r = proc.run_to_file([PY, "-c", code], out, timeout=120)
    took = time.monotonic() - t0
    assert took < 15, took                                    # old: 40 s, the grandchild's life
    assert r.returncode == 0 and r.output_not_waited and not r.timed_out
    assert out.read_bytes() == b'{"type": "result"}\n'


def test_a_program_that_ends_with_its_output_is_waited_for_whole(tmp_path):
    code = "import sys, time\nfor i in range(50):\n    sys.stdout.buffer.write(b'x' * 1000 + b'\\n'); sys.stdout.flush()\n"
    r = proc.run_to_file([PY, "-c", code], tmp_path / "s.jsonl", timeout=60)
    assert not r.output_not_waited and (tmp_path / "s.jsonl").stat().st_size == 50 * 1001


def test_at_the_timeout_the_tree_is_killed_and_run_returns(tmp_path):
    pid_file = tmp_path / "gc.pid"
    code = _grandchild(60, False, pid_file) + ("sys.stdout.buffer.write(b'{\"type\": \"system\"}\\n'); "
                                               "sys.stdout.flush()\ntime.sleep(60)\n")
    t0 = time.monotonic()
    r = proc.run_to_file([PY, "-c", code], tmp_path / "s.jsonl", timeout=4)
    took = time.monotonic() - t0
    assert r.timed_out and r.returncode == -1 and took < 20, took        # old: 30 s and more, or never
    deadline = time.monotonic() + 10
    while _alive(int(pid_file.read_text())) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _alive(int(pid_file.read_text()))                       # the grandchild of the same tree is gone
    assert (tmp_path / "s.jsonl").read_bytes() == b'{"type": "system"}\n'


def test_a_descendant_that_left_the_tree_does_not_hold_the_timeout(tmp_path):
    code = _grandchild(60, True) + "time.sleep(60)\n"
    t0 = time.monotonic()
    r = proc.run_to_file([PY, "-c", code], tmp_path / "s.jsonl", timeout=3)
    assert r.timed_out and time.monotonic() - t0 < 20


def test_run_keeps_the_count_of_bytes_left_out_when_killed_at_its_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(proc, "STREAM_FILE_HEAD", 20_000)
    monkeypatch.setattr(proc, "STREAM_FILE_TAIL", 20_000)
    code = ("import sys, time\nw = sys.stdout\nw.write('{\"type\": \"system\", \"subtype\": \"init\", "
            "\"session_id\": \"s1\"}\\n')\nfor i in range(100000): w.write('ab\\n')\nw.flush()\ntime.sleep(60)\n")
    monkeypatch.setattr(claude_cli, "build_command", lambda *a, **k: [PY, "-c", code])
    out = tmp_path / "s.jsonl"
    m = claude_cli.run_claude("x", tmp_path, out, timeout_s=4, parse=False)
    assert m["exit_code"] == -1 and m["timed_out"] and m["stream_bytes_left_out"] > 200_000    # old: no count
    assert out.stat().st_size <= 40_000


def test_a_stream_file_that_cannot_be_written_is_said_so(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": "s1"})] + ["ab"] * 50_000 + [
        json.dumps({"type": "result", "result": "done", "session_id": "s1"})]
    data = tmp_path / "fake.bin"
    data.write_bytes("\n".join(lines).encode() + b"\n")
    monkeypatch.setattr(claude_cli, "build_command", lambda *a, **k: [
        PY, "-c", "import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read())", str(data)])
    real = proc._StreamSink.put
    state = {"n": 0}

    def put(self, chunk):            # the disk fills after 30,000 bytes
        state["n"] += len(chunk)
        if state["n"] > 30_000 and self.f is not None:
            proc._close(self.f)
            self.f, self.failed = None, True
            return
        real(self, chunk)
    monkeypatch.setattr(proc._StreamSink, "put", put)
    info: dict = {}
    meta = C.run_and_capture(project, "x", isolated=False, store=project / ".devmemory", info=info)
    note = C.import_note(info)
    assert "could not be written" in note and "64 MB" not in note
    raw = meta.with_name(meta.name.replace(".meta.json", ".jsonl")).read_text(encoding="utf-8")
    assert "ab" not in raw.split("\n")                      # nothing of a file written in part is kept


def test_a_note_says_lines_were_dropped_not_that_the_file_was_read_in_part(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_STREAM_LINES", 10)
    project = make_project(tmp_path)
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": f"t{i}"}]}}) for i in range(40)) + "\n", encoding="utf-8")
    info: dict = {}
    C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory", info=info)
    note = C.import_note(info)
    assert "30 lines of the stream were not read" in note and "read only in part" not in note
