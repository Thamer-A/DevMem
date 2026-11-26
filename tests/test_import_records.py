"""What the record says about a call, and about a run (sixth review, items 3 to 8):
3. a call whose input is too large to check for secret-file names is recorded as that (`input_not_checked`, the reason
   and the size), not as one that names a secret file, the same in import and the hooks, and nothing of it is kept;
4. `devmem run` stopped with Ctrl-C keeps what the program wrote until then and says it was interrupted;
5. the stored form of a secret call keeps a known program (else "other"), file operands and redirect targets only,
   and path values only when they are shaped like paths;
6. a stream with more ids on stub lines than MAX_MARKED_IDS says that no result text was kept;
7. a long text before a tool call on a line too long to parse does not hide the call;
8. a path with an unexpanded variable (`$HOME/x`, `%USERPROFILE%\\x`) is never recorded as deleted."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import capture as C
from devmem import redact as R

SECRET = "QX7SECRETPLAIN"
INIT = {"type": "system", "subtype": "init", "session_id": "11111111-2222-3333-4444-555555555555", "model": "m"}
END = {"type": "result", "result": "done", "session_id": "11111111-2222-3333-4444-555555555555"}
NEEDLE = "ZQ81PLAINVALUE"          # (a word holding "secret" would itself name a secret file: `*secret*`)
HEREDOC = "cat > notes.md <<'EOF'\n" + ("an ordinary line of notes " + NEEDLE + "\n") * 1200 + "EOF"     # ~ 49 KB


def _import(tmp_path, project, events, **kw):
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in events) + "\n", encoding="utf-8")
    info: dict = {}
    meta = C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory", info=info, **kw)
    raw = [json.loads(x) for x in meta.with_name(meta.name.replace(".meta.json", ".jsonl")).read_text(
        encoding="utf-8").splitlines() if x]
    return json.loads(meta.read_text(encoding="utf-8")), raw, info


def _store_text(project) -> str:
    return "".join(f.read_text(encoding="utf-8", errors="replace") for f in (project / ".devmemory").rglob("*")
                   if f.is_file() and f.suffix in (".json", ".jsonl"))


# ---------------------------------------------------------------- 3. too large to check

def test_a_command_too_large_to_check_is_recorded_as_that_in_import(tmp_path):
    project = make_project(tmp_path)
    call = {"type": "tool_use", "id": "h1", "name": "Bash", "input": {"command": HEREDOC}}
    rec, raw, info = _import(tmp_path, project, [INIT, {"type": "assistant", "message": {"content": [call]}},
                                                 {"type": "user", "message": {"content": [
                                                     {"type": "tool_result", "tool_use_id": "h1",
                                                      "content": "wrote " + NEEDLE, "is_error": True}]}}, END])
    entry = rec["tool_calls"][0]
    assert entry["input_not_checked"] is True and "secret_file" not in entry
    assert entry["chars"] > 24_096 and "24,096" in entry["reason"]
    assert raw[1]["message"]["content"][0]["input"]["input_not_checked"] is True
    assert raw[2]["message"]["content"][0]["content"] == R.UNCHECKED_OUTPUT
    assert rec["errors"][0]["error"] == R.UNCHECKED_OUTPUT[:300]
    assert NEEDLE not in _store_text(project) and "names a secret file" not in _store_text(project)
    assert info["tool_calls_not_checked"] == 1 and "too large to check" in C.import_note(info)


def test_a_command_too_large_to_check_is_recorded_the_same_way_by_the_hooks(tmp_path, monkeypatch):
    from devmem import hooks
    from devmem.redact import Redactor
    project = make_project(tmp_path)
    store = project / ".devmemory"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    hooks.on_tool({"session_id": "s1", "cwd": str(project), "tool_name": "Bash", "tool_use_id": "h1",
                   "tool_input": {"command": HEREDOC}, "tool_response": {"stdout": "wrote " + NEEDLE, "stderr": ""}},
                  project, store, Redactor(store, project=project))
    log = _store_text(project)
    assert NEEDLE not in log and '"input_not_checked": true' in log and R.UNCHECKED_OUTPUT in log
    assert "names a secret file" not in log


# ---------------------------------------------------------------- 5. the stored form keeps less

@pytest.mark.parametrize("cmd,program,names", [
    (f"{SECRET} > .env", "other", [".env"]),
    (f"echo API_KEY={SECRET}.pem >> .env", "echo", [".env"]),
    (f"printf 'pw={SECRET}.key' > .env", "printf", [".env"]),
    (f"id_rsa_{SECRET} > .env", "other", [".env"]),
    (f"echo {SECRET}.pem >> .env", "echo", [".env"]),
    ("cat .env", "cat", [".env"]),
    ("cp .env backup/old.pem", "cp", [".env", "old.pem"]),
    ("Get-Content .env | Set-Content copy.txt", "get-content", [".env"]),
])
def test_the_stored_command_keeps_a_known_program_and_file_operands_only(cmd, program, names):
    out = R.secret_command_marker(cmd)
    assert f"program: {program}" in out and SECRET not in out
    assert out.endswith(f"secret files: {' '.join(names)}]")


def test_a_path_key_keeps_only_a_value_shaped_like_a_path():
    out = R.secret_call_input({"path": ".env", "source": f"NOTE={SECRET}", "target": "backup/.env.old",
                               "root": f"{SECRET} with spaces", "glob": "*.env", "cwd": "src"})
    assert out["path"] == ".env" and out["target"] == "backup/.env.old" and SECRET not in json.dumps(out)


# ---------------------------------------------------------------- 6. MAX_MARKED_IDS reached

def test_a_stream_with_too_many_marked_ids_says_no_result_text_was_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_MARKED_IDS", 3)
    project = make_project(tmp_path)
    stub = "not json " + "".join(f'"id": "x{i}" ' for i in range(10))
    call = {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": str(project / "greet.py")}}
    rec, raw, info = _import(tmp_path, project, [INIT, stub, {"type": "assistant", "message": {"content": [call]}},
                                                 {"type": "user", "message": {"content": [
                                                     {"type": "tool_result", "tool_use_id": "r1", "content": "x"}]}},
                                                 END])
    assert "MAX_MARKED_IDS" in rec["turns"][0]["tool_result_texts_left_out"]
    assert "no tool result's text was kept" in C.import_note(info)


# ---------------------------------------------------------------- 7. a long text before the call

def test_a_long_text_before_a_write_on_a_line_too_long_to_parse_does_not_hide_it(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_LINE_PARSE_CHARS", 50_000)
    project = make_project(tmp_path)
    line = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "a plan " * 2000},                    # 14,000 characters, over LONG_SCAN_STRING
        {"type": "tool_use", "id": "w1", "name": "Write", "input": {"file_path": str(project / "big.py"),
                                                                   "content": "x = 1\n" * 20_000}}]}}
    rec, raw, info = _import(tmp_path, project, [INIT, line, END])
    assert rec["files_edited"] == ["big.py"] and info["tool_calls_left_out"] == 1
    assert "only those found in the first 64 KB" in C.import_note(info)


# ---------------------------------------------------------------- 8. unexpanded names

@pytest.mark.parametrize("name", ["$HOME/notes/diary.txt", "%USERPROFILE%\\notes\\diary.txt", "~/notes/diary.txt"])
def test_a_path_with_an_unexpanded_variable_is_never_recorded_as_deleted(tmp_path, name):
    project = make_project(tmp_path)
    call = {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": name}}
    rec, raw, info = _import(tmp_path, project, [INIT, {"type": "assistant", "message": {"content": [call]}},
                                                 {"type": "user", "message": {"content": [
                                                     {"type": "tool_result", "tool_use_id": "r1", "content": "x"}]}},
                                                 END], git_start={"head": None}, at_session_end=True)
    assert rec["files_deleted"] == [] and not rec["turns"][0].get("files_absent_at_capture")


# ---------------------------------------------------------------- 4. Ctrl-C during `devmem run`

@pytest.mark.parametrize("sid", ["abc\x00def", "../../escape", "a/b", "C:x", "NUL"])
def test_a_stream_session_id_that_cannot_name_a_file_is_like_a_missing_one(tmp_path, sid):
    """The reviewer's fuzz seed 57 case 1519: a session id holding NUL made record_session raise ValueError (embedded
    null character in path). An id that is no plain file name (a NUL, a separator, `..`, a device name) names the
    record's files by its hash only (two such ids never share a name); one holding NUL is not kept, as one too long."""
    project = make_project(tmp_path)
    init, end = dict(INIT, session_id=sid), dict(END, session_id=sid)
    rec, raw, _ = _import(tmp_path, project, [init, end])
    assert rec["session_id"] == (None if "\x00" in sid else sid) and rec["result_text"] == "done"
    files = list((project / ".devmemory" / "sessions").iterdir())
    assert len(files) == 2 and all(f.is_file() and "_id" in f.name for f in files), files
    assert not (project / ".devmemory" / "escape").exists() and not (project / "escape").exists()


def test_run_to_file_interrupted_keeps_what_the_program_wrote(tmp_path, monkeypatch):
    from devmem import proc
    code = "import sys, time\nprint('{\"type\": \"system\"}', flush=True)\ntime.sleep(60)\n"
    real_wait = subprocess.Popen.wait
    calls = []

    def wait(self, timeout=None):
        if not calls:
            calls.append(1)
            time.sleep(1.0)                 # the program has written its line
            raise KeyboardInterrupt
        return real_wait(self, timeout)
    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    monkeypatch.setattr(proc, "INTERRUPT_GRACE_S", 0.5)
    r = proc.run_to_file([sys.executable, "-c", code], tmp_path / "out.jsonl", timeout=60)
    assert r.interrupted is True and b'{"type": "system"}' in (tmp_path / "out.jsonl").read_bytes()


@pytest.mark.skipif(os.name == "nt", reason="a terminal's Ctrl-C is a POSIX signal to the foreground group here")
def test_devmem_run_interrupted_records_what_was_captured_and_says_so(tmp_path):
    project = make_project(tmp_path)
    fake = tmp_path / "fake_claude"
    events = [{"type": "system", "subtype": "init", "session_id": "ctrlc-1", "model": "m"},
              {"type": "assistant", "message": {"content": [{"type": "text", "text": "WORK_BEFORE_CTRL_C"}]}}]
    fake.write_text(f"#!{sys.executable}\nimport json, sys, time\n"
                    f"for e in {events!r}: print(json.dumps(e), flush=True)\n"
                    "open(sys.argv[0] + '.ready', 'w').close()\ntime.sleep(60)\n", encoding="utf-8")
    fake.chmod(0o755)
    plugin = str(Path(C.__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONPATH=plugin, DEVMEM_CLAUDE=str(fake))
    code = "import sys\nfrom devmem import proc\nproc.INTERRUPT_GRACE_S = 0.5\nfrom devmem.cli import main\n" \
           f"sys.exit(main(['run', 'x', '--project', {str(project)!r}]))\n"
    p = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True)
    deadline = time.monotonic() + 60
    while not Path(str(fake) + ".ready").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    p.send_signal(signal.SIGINT)
    out, err = p.communicate(timeout=120)
    assert p.returncode == 130, (out, err)
    assert "interrupted (Ctrl-C)" in out + err and "captured ->" in out + err
    assert "WORK_BEFORE_CTRL_C" in _store_text(project)
