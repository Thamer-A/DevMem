"""Interactive capture through Claude Code hooks, replayed from real 2.1.323 payloads."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from devmem import hooks, sessions
from devmem.sessions import load_sessions

from hooks_helpers import (SENTINEL, SESSION_ID, TURN1, TURN2, load_payload, make_project, one_meta, replay)

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


def test_multi_turn_session_is_one_record(project, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    outs = replay(project, TURN1 + TURN2, transcript, monkeypatch, hooks)
    assert all(code == 0 for code, _ in outs)
    store = project / ".devmemory"
    assert not (store / "hook_errors.log").exists(), (store / "hook_errors.log").read_text(encoding="utf-8")
    rec = one_meta(store)
    assert rec["schema"] == 2 and rec["capture_method"] == "hooks"
    assert rec["session_id"] == SESSION_ID
    assert [t["prompt"][:20] for t in rec["turns"]] == ["In greet.py change \"", "Append a line '# end"]
    t0, t1 = rec["turns"]
    edit = next(c for c in t0["tool_calls"] if c["tool"] == "Edit")
    assert edit["file"] == "greet.py" and '"hello "' in edit["old"] and '"hi "' in edit["new"]
    write = next(c for c in t0["tool_calls"] if c["tool"] == "Write")
    assert write["file"] == "farewell.py" and "def bye" in write["new"] and write["op"] == "create"
    assert write["truncated"] is False
    assert t0["files_deleted"] == ["notes.txt"]
    assert {"tool": "Delete", "file": "notes.txt", "source": "observed on disk at end of turn"} in t0["tool_calls"]
    assert any("does_not_exist_dir" in e["error"] for e in t0["errors"])
    assert t0["result_text"].startswith("مرحبا")
    assert t0["result_source"].startswith("assistant-visible")
    assert any(c["tool"] == "Edit" and c["file"] == "farewell.py" for c in t1["tool_calls"])
    assert t1["result_text"] == "Appended `# end` to farewell.py."
    # aggregates (v1 readers)
    assert rec["prompt"] == t0["prompt"] and rec["result_text"] == t1["result_text"]
    assert rec["files_edited"] == ["farewell.py", "greet.py"] and rec["files_deleted"] == ["notes.txt"]
    assert len(rec["tool_calls"]) == len(t0["tool_calls"]) + len(t1["tool_calls"])
    # git HEAD at start and end, model/usage from the transcript (best-effort)
    assert rec["git"]["head_start"] and rec["git"]["head_end"] == rec["git"]["head_start"]
    assert rec["model"] and rec["usage"]["output_tokens"] > 0
    # hidden reasoning never stored; the raw record moved out of live/ at SessionEnd
    raw = store / "sessions" / rec["raw_record"]
    assert raw.exists() and not (store / "live" / f"{SESSION_ID}.jsonl").exists()
    for f in [raw, *(store / "sessions").glob("*.meta.json")]:
        assert SENTINEL not in f.read_text(encoding="utf-8")
    assert rec["ended_at"] and rec["ended_reason"] == "other"


def test_record_is_usable_without_session_end(project, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    replay(project, TURN1[:-1], transcript, monkeypatch, hooks)  # no SessionEnd: terminal closed
    store = project / ".devmemory"
    rec = one_meta(store)
    assert len(rec["turns"]) == 1 and rec["ended_at"] is None
    assert rec["turns"][0]["result_text"]
    # a second turn starts and crashes before Stop: the loader rebuilds from the live log
    replay(project, ["028_UserPromptSubmit", "030_PostToolUse"], transcript, monkeypatch, hooks)
    ss = load_sessions(store)
    assert len(ss) == 1 and ss[0]["in_progress"] and len(ss[0]["turns"]) == 2
    assert ss[0]["_meta"].endswith(".meta.json")


def test_garbage_stdin_exits_zero_and_logs(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    for data in (b"not json", b"[1, 2]", b"", b"\xff\xfe\x00", b'{"hook_event_name": "Stop"}'):
        assert hooks.run(["Stop"], data) == (0, "")
    log = (project / ".devmemory" / "hook_errors.log").read_text(encoding="utf-8")
    assert log.count("--- ") == 5 and "JSONDecodeError" in log and "no session_id" in log


def test_garbage_stdin_subprocess_both_launch_forms(project):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    env.pop("PYTHONPATH", None)
    for argv in ([sys.executable, str(REPO / "plugin" / "devmem" / "hooks.py"), "UserPromptSubmit"],
                 [sys.executable, "-m", "devmem.hooks", "PostToolUse"]):
        p = subprocess.run(argv, input=b"\x00garbage{", capture_output=True, cwd=REPO / "plugin" if "-m" in argv else project,
                           env=env, timeout=60)
        assert p.returncode == 0, p.stderr
        assert p.stdout == b""


def test_unknown_event_and_missing_store_do_nothing(tmp_path, monkeypatch):
    project = make_project(tmp_path, init_store=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    payload = load_payload("002_UserPromptSubmit", project)
    assert hooks.run(["UserPromptSubmit"], json.dumps(payload).encode()) == (0, "")
    assert not (project / ".devmemory").exists()


def test_capture_off_records_nothing(project, monkeypatch):
    (project / ".devmemory" / "config.json").write_text('{"capture": false, "deliver": false}', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    for name in ("002_UserPromptSubmit", "008_PostToolUse", "022_Stop"):
        hooks.run([name.split("_", 1)[1]], json.dumps(load_payload(name, project)).encode())
    assert not (project / ".devmemory" / "live").exists()
    assert not (project / ".devmemory" / "sessions").exists()


def test_secret_file_content_and_prompt_secrets_never_stored(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    key = "sk-ant-api03-" + "A1b2C3d4" * 6
    base = load_payload("004_PostToolUse", project)
    read_env = dict(base, tool_input={"file_path": str(project / ".env")},
                    tool_response={"type": "text", "file": {"filePath": str(project / ".env"),
                                                            "content": f"ANTHROPIC_API_KEY={key}\n"}})
    prompt = dict(load_payload("002_UserPromptSubmit", project), prompt=f"use this key {key} please")
    write_env = dict(load_payload("010_PostToolUse", project),
                     tool_input={"file_path": str(project / ".env"), "content": "DB_PASSWORD=hunter2hunter2"})
    bash = dict(load_payload("016_PostToolUse", project), tool_input={"command": "cat .env"},
                tool_response={"stdout": "DB_PASSWORD=hunter2hunter2", "stderr": ""})
    for ev, pl in (("UserPromptSubmit", prompt), ("PostToolUse", read_env), ("PostToolUse", write_env),
                   ("PostToolUse", bash)):
        assert hooks.run([ev], json.dumps(pl).encode())[0] == 0
    log = (project / ".devmemory" / "live" / f"{SESSION_ID}.jsonl").read_text(encoding="utf-8")
    assert key not in log and "hunter2hunter2" not in log
    assert "[REDACTED:anthropic-key]" in log and "[OMITTED" in log


def test_parallel_tool_events_all_recorded(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    base = load_payload("008_PostToolUse", project)
    payloads = [json.dumps(dict(base, tool_use_id=f"toolu_{i}")).encode() for i in range(24)]
    threads = [threading.Thread(target=hooks.run, args=(["PostToolUse"], p)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = _log_lines(project / ".devmemory" / "live" / f"{SESSION_ID}.jsonl")
    assert sorted(json.loads(l)["tool_use_id"] for l in lines) == sorted(f"toolu_{i}" for i in range(24))


def _log_lines(path: Path) -> list[str]:
    """The records of an event log (each is written as newline + JSON + newline, so blank lines separate them)."""
    return [ln for ln in path.read_text(encoding="utf-8").split("\n") if ln.strip()]


_CHILD = """
import json, sys, threading
from devmem import hooks
payload, tag, threads, per = json.loads(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
def work(t):
    for e in range(per):
        hooks.run(["PostToolUse"], json.dumps(dict(payload, tool_use_id=f"{tag}-{t}-{e}")).encode())
ts = [threading.Thread(target=work, args=(t,)) for t in range(threads)]
for t in ts:
    t.start()
for t in ts:
    t.join()
"""


@pytest.mark.parametrize("round_", range(3))
def test_appends_lossless_across_processes(project, round_):
    """Regression: under contention the old lock gave up (Windows PermissionError on a delete-pending lock
    file) and the unlocked CRT O_APPEND emulation overwrote lines, losing 2-3 of 480 events. Appends are now
    atomic without a lock, so every event of 6 processes x 4 threads must be present as an intact line.

    The store has git's clean answer before the children start (devmem asks git when the store is set up), so a git
    that does not answer in time under load means record-only there, not a refused store whose events are only
    counted (that case: tests/test_hook_not_recorded.py)."""
    from devmem import consent
    procs, threads, per = 6, 4, 10
    for _ in range(3):           # a command asks git again at once (no hook time limit); under load once may time out
        if consent.store_refusal(project, project / ".devmemory") is None:
            break
    assert consent.store_refusal(project, project / ".devmemory") is None
    payload = json.dumps(load_payload("008_PostToolUse", project))
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project), "PYTHONPATH": str(Path(hooks.__file__).parents[1])}
    children = [subprocess.Popen([sys.executable, "-c", _CHILD, payload, f"p{i}", str(threads), str(per)],
                                 env=env) for i in range(procs)]
    for c in children:
        assert c.wait(timeout=120) == 0
    raw = _log_lines(project / ".devmemory" / "live" / f"{SESSION_ID}.jsonl")
    ids = [json.loads(line)["tool_use_id"] for line in raw]  # every record parses: no torn or merged writes
    expected = {f"p{i}-{t}-{e}" for i in range(procs) for t in range(threads) for e in range(per)}
    assert len(ids) == len(expected) and set(ids) == expected
    assert not (project / ".devmemory" / "hook_errors.log").exists()
    assert hooks.unrecorded_left(project / ".devmemory") == []


def test_hooks_installed_mid_session(project, tmp_path, monkeypatch):
    """No SessionStart seen: the first event creates the session; git state is taken at the first prompt."""
    (project / "farewell.py").write_text("def bye(name):\n    return name\n", encoding="utf-8")
    replay(project, TURN2[1:], tmp_path / "t.jsonl", monkeypatch, hooks)
    rec = one_meta(project / ".devmemory")
    assert len(rec["turns"]) == 1 and rec["git"].get("head_start")


def test_late_event_after_session_end_reopens_full_log(project, tmp_path, monkeypatch):
    """An event arriving after SessionEnd (e.g. an async hook) must not shadow the finished record."""
    replay(project, TURN1, tmp_path / "t.jsonl", monkeypatch, hooks)
    late = dict(load_payload("004_PostToolUse", project), tool_use_id="toolu_late")
    assert hooks.run(["PostToolUse"], json.dumps(late).encode())[0] == 0
    [s] = load_sessions(project / ".devmemory")
    assert len(s["turns"]) == 1 and len(s["turns"][0]["tool_calls"]) >= 6
    assert s["turns"][0]["result_text"]


def test_compaction_summary_and_api_failure_are_labelled(project, tmp_path, monkeypatch):
    """PostCompact / StopFailure payloads follow the documented fields (not observed live in 2.1.323 runs)."""
    replay(project, TURN1[:2], tmp_path / "t.jsonl", monkeypatch, hooks)
    base = {k: v for k, v in load_payload("022_Stop", project).items()
            if k in ("session_id", "prompt_id", "cwd", "transcript_path")}
    hooks.run(["PostCompact"], json.dumps(dict(base, hook_event_name="PostCompact", trigger="auto",
                                               compact_summary="Summary: greet.py edited")).encode())
    hooks.run(["StopFailure"], json.dumps(dict(base, hook_event_name="StopFailure", error="rate_limit",
                                               error_details="429")).encode())
    [s] = load_sessions(project / ".devmemory")
    assert s["compactions"][0]["source"].startswith("model-generated")
    assert "rate_limit" in s["turns"][0]["errors"][-1]["error"]


def _edit_payload(project: Path, file_path: str, tool_use_id: str) -> bytes:
    base = load_payload("008_PostToolUse", project)
    inp = dict(base["tool_input"], file_path=file_path)
    return json.dumps(dict(base, tool_input=inp, tool_use_id=tool_use_id)).encode()


def test_native_paths_are_made_project_relative(project, monkeypatch):
    """Product check on every OS with the paths this OS's Claude Code sends (no fixture strings)."""
    (project / "src").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    forms = {"native": str(project / "src" / "a.py"), "posix": (project / "src" / "b.py").as_posix(),
             "dotted": str(project / "src" / ".." / "src" / "c.py"), "outside": str(project.parent / "x.py")}
    if os.name == "nt":  # drive-letter case and separators vary between Windows payload fields
        p = str(project / "src" / "d.py")
        forms["drive-case"] = p[0].swapcase() + p[1:]
    for i, fp in enumerate(forms.values()):
        assert hooks.run(["PostToolUse"], _edit_payload(project, fp, f"t{i}"))[0] == 0
    lines = _log_lines(project / ".devmemory" / "live" / f"{SESSION_ID}.jsonl")
    calls = {json.loads(line)["tool_use_id"]: json.loads(line)["calls"][0] for line in lines}
    got = {k: c.get("file") for k, c in calls.items()}
    want = {"native": "src/a.py", "posix": "src/b.py", "dotted": "src/c.py", "drive-case": "src/d.py"}
    for i, name in enumerate(forms):
        if name == "outside":  # not stored at all (devmem.sessions.project_file), never shortened to a relative path
            assert got[f"t{i}"] is None and calls[f"t{i}"]["path_omitted"] == sessions.OUTSIDE_PROJECT, calls[f"t{i}"]
        else:
            assert got[f"t{i}"] == want[name], (name, got[f"t{i}"])


@pytest.mark.skipif(os.name == "nt", reason="symlinked project roots (e.g. macOS /tmp -> /private/tmp) are POSIX")
def test_symlinked_project_root_is_relativised(tmp_path, monkeypatch):
    real = make_project(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(link))  # Claude Code reports the path the user opened
    assert hooks.run(["PostToolUse"], _edit_payload(real, str(real / "greet.py"), "t1"))[0] == 0
    line = _log_lines(real / ".devmemory" / "live" / f"{SESSION_ID}.jsonl")[0]
    assert json.loads(line)["calls"][0]["file"] == "greet.py"
