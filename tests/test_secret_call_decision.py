"""One decision about a tool call, taken on its whole input, and one stored form of a call that names a secret file,
for every writer (fifth review, blockers 1 and 3).

1. `devmem import`/`run` decided a call on a copy whose strings were cut at STREAM_TEXT_CAP + 2 * CAP_MARGIN: `echo
   <67,600 characters>; cat .env` was judged ordinary and the file's text stored in the raw record and meta.json. The
   hooks checked only the first 20,000 and last 4,096 characters of a command. Now the decision reads the whole input,
   and an input the check does not read in full (a command over COMMAND_CHECK_HEAD + _TAIL, a path over
   MAX_PATH_CHARS, ...) counts as naming a secret file (redact.call_names_secret_file).
3. A command that writes a value into a secret file (`echo NOTE=X >> .env`) was stored with redaction only, in
   import/run, codex and the hooks. Now its stored form keeps only the program and the secret file names
   (redact.secret_call_input), in the raw record, the digest, codex events and hook events alike."""
from __future__ import annotations

import json

import pytest
from hooks_helpers import make_project

from devmem import capture as C
from devmem import redact as R

SECRET = "QX7SECRETPLAIN"
HOST = "internal-db-zq81.corp.example"
ENV_TEXT = f"SERVICE_HOST={HOST}\nNOTE={SECRET}\n"
NEEDLES = [SECRET.encode(), b"zq81", SECRET.encode("utf-16-le")]


def _hits(store) -> list[str]:
    return sorted({str(f.relative_to(store)) for f in store.rglob("*") if f.is_file()
                   for n in NEEDLES if n in f.read_bytes()})


def _stream(path, calls, results) -> None:
    init = {"type": "system", "subtype": "init", "session_id": "11111111-2222-3333-4444-555555555555", "model": "m"}
    end = {"type": "result", "result": "done", "session_id": "11111111-2222-3333-4444-555555555555"}
    lines = [init, {"type": "assistant", "message": {"role": "assistant", "content": calls}}]
    lines += [{"type": "user", "message": {"role": "user", "content": [r]},
               "tool_use_result": {"stdout": ENV_TEXT, "stderr": "", "file": {"content": ENV_TEXT}}} for r in results]
    path.write_text("\n".join(json.dumps(x) for x in lines + [end]) + "\n", encoding="utf-8")


def _import(tmp_path, project, calls, results, info=None):
    path = tmp_path / "s.jsonl"
    _stream(path, calls, results)
    return C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory",
                            info=info if info is not None else {})


def _call(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _result(tid, failing=False):
    return {"type": "tool_result", "tool_use_id": tid, "content": ENV_TEXT, **({"is_error": True} if failing else {})}


# ---------------------------------------------------------------- 1. the decision reads the whole input

# padding lengths around every cap that applies: MAX_PATH_CHARS (4,096), COMMAND_CHECK_HEAD + _TAIL (24,096),
# STREAM_TEXT_CAP (65,536), STREAM_TEXT_CAP + 2 * CAP_MARGIN (67,584, the cut copy), and a line just under 4 MB
CAPS = [R.MAX_PATH_CHARS, R.COMMAND_CHECK_HEAD + R.COMMAND_CHECK_TAIL, C.STREAM_TEXT_CAP,
        C.STREAM_TEXT_CAP + 2 * R.CAP_MARGIN]
LENGTHS = sorted({n + d for n in CAPS for d in (-1, 1)} | {C.MAX_LINE_PARSE_CHARS - 4096})


def _input(tool, where, n, project):
    pad = "x" * n
    if tool == "Bash":
        return "Bash", {"command": f"echo {pad} > /dev/null; cat .env" if where == "before"
                        else f"cat .env; echo {pad} > /dev/null"}
    deep = "/".join(["a" * 50] * max(1, n // 51))           # a long path made of ordinary names
    path = f"{deep}/.env" if where == "before" else f".env/{deep}"
    if tool == "Read":
        return "Read", {"file_path": path}
    if tool == "Grep":
        return "Grep", {"pattern": "NOTE", "path": path}
    if tool == "MultiEdit":
        return "MultiEdit", {"file_path": path, "edits": [{"old_string": "a", "new_string": f"NOTE={SECRET}"}]}
    return "mcp__fs__read_text_file", {"path": path, "note": pad[:10]}


@pytest.mark.parametrize("where", ["before", "after"])
@pytest.mark.parametrize("n", LENGTHS)
@pytest.mark.parametrize("tool", ["Bash", "Read", "Grep", "MultiEdit", "mcp"])
def test_a_secret_file_call_is_decided_on_its_whole_input(tmp_path, tool, n, where):
    project = make_project(tmp_path)
    (project / ".env").write_text(ENV_TEXT, encoding="utf-8")
    name, inp = _input(tool, where, n, project)
    meta = _import(tmp_path, project, [_call("c1", name, inp)], [_result("c1", failing=True)])
    assert _hits(project / ".devmemory") == []
    rec = json.loads(meta.read_text(encoding="utf-8"))
    assert [e["error"] for e in rec["turns"][0]["errors"]] in ([R.OMITTED_SECRET_FILE], [C.LEFT_OUT_TOOL],
                                                                     [R.UNCHECKED_OUTPUT], [])


@pytest.mark.parametrize("inp", [
    {"command": "echo " + "x" * 30_000 + "; cat .env; echo " + "y" * 30_000},      # past the checked head, before the tail
    {"command": "cat " + "d/" * 3000 + ".kube/config"},                              # one word longer than a path
    {"file_path": "a/" * 3000 + ".kube/config"},                                      # a path longer than MAX_PATH_CHARS
    {"paths": ["src/" + "m" * 4000 + str(i) + ".py" for i in range(4)] + ["s/" * 170, "x/" * 10 + ".kube/config"]},
    # (a path past PATH_CHECK_BUDGET is checked by its last name only: `config`)
    {"deep": json.loads("[" * 40 + "]" * 40)},                                       # nested past MAX_INPUT_DEPTH
], ids=["middle_of_long_command", "long_word", "long_path", "path_budget", "deep"])
def test_an_input_the_check_does_not_read_in_full_counts_as_naming_a_secret_file(inp):
    why: list = []
    assert R.call_names_secret_file("Bash", inp, why=why) is True and why == ["too large"]


def test_an_ordinary_input_read_in_full_is_ordinary():
    for inp in ({"command": "ls -la src"}, {"file_path": "src/app.py"}, {"content": "x" * 1_000_000, "file_path": "a.py"},
                {"command": "python -c " + "'print(1)' " * 2000}, {"pattern": "a" * 10_000, "path": "src"}):
        assert R.call_names_secret_file("Write", inp) is False


def test_the_hooks_take_the_same_decision_for_a_long_command(tmp_path, monkeypatch):
    """A command whose `.env` lies past the first 20,000 characters it checked: its output was stored by the hooks."""
    from devmem import hooks
    from devmem.redact import Redactor
    project = make_project(tmp_path)
    (project / ".env").write_text(ENV_TEXT, encoding="utf-8")
    store = project / ".devmemory"
    payload = {"session_id": "s1", "cwd": str(project), "tool_name": "Bash", "tool_use_id": "b1",
               "tool_input": {"command": "echo " + "x" * 30_000 + "; cat .env; echo " + "y" * 30_000},
               "tool_response": {"stdout": ENV_TEXT, "stderr": ""}}
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    hooks.on_tool(payload, project, store, Redactor(store, project=project))
    assert _hits(store) == []


# ---------------------------------------------------------------- 3. a command that writes into a secret file

WRITES = [f"echo NOTE={SECRET} >> .env", f"printf 'SERVICE_HOST={HOST}' > .env", f"echo NOTE={SECRET} | tee -a .env",
          f"cat > .env <<'X'\nNOTE={SECRET}\nX", f"Set-Content .env 'NOTE={SECRET}'",
          f"sed -i s/OLD/{SECRET}/ .env", f"Add-Content -Path .env -Value \"NOTE={SECRET}\""]


def test_the_stored_form_keeps_the_program_and_the_secret_file_names_only():
    for cmd in WRITES:
        out = R.secret_call_input({"command": cmd, "description": f"set NOTE to {SECRET}",
                                   "unknown_key": {"text": SECRET}, "file_path": ".env", "timeout": 5})
        assert SECRET not in json.dumps(out) and HOST not in json.dumps(out), out
        assert out["file_path"] == ".env" and out["timeout"] == 5
        assert out["description"] == R.OMITTED_SECRET_FILE and out["unknown_key"]["text"] == R.OMITTED_SECRET_FILE
        assert ".env" in out["command"]
    assert "program: echo" in R.secret_command_marker("echo X >> .env")
    assert "program: other" in R.secret_command_marker("NOTE=X cmd > .env")      # the first word is no program


@pytest.mark.parametrize("cmd", WRITES)
@pytest.mark.parametrize("writer", ["import", "run", "codex", "hooks"])
def test_a_command_writing_into_a_secret_file_keeps_no_value(tmp_path, monkeypatch, writer, cmd):
    project = make_project(tmp_path)
    (project / ".env").write_text("OLD=1\n", encoding="utf-8")
    store = project / ".devmemory"
    if writer == "import":
        _import(tmp_path, project, [_call("c1", "Bash", {"command": cmd})],
                [{"type": "tool_result", "tool_use_id": "c1", "content": ""}])
    elif writer == "run":
        def fake_run_claude(prompt, cwd, stream_out, **kw):
            _stream(stream_out, [_call("c1", "Bash", {"command": cmd})],
                    [{"type": "tool_result", "tool_use_id": "c1", "content": ""}])
            return {"exit_code": 0}
        monkeypatch.setattr(C, "run_claude", fake_run_claude)
        C.run_and_capture(project, "x", store=store)
    elif writer == "codex":
        from devmem import generic as G
        G.record_generic(project, "x", {}, {}, "2026-11-16T00:00:00Z", "codex-cli", store=store, events=[
            {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": cmd,
                                                "aggregated_output": "", "exit_code": 0, "status": "completed"}}])
    else:
        from devmem import hooks
        from devmem.redact import Redactor
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
        hooks.on_tool({"session_id": "s1", "cwd": str(project), "tool_name": "Bash", "tool_use_id": "b1",
                       "tool_input": {"command": cmd}, "tool_response": {"stdout": "", "stderr": ""}},
                      project, store, Redactor(store, project=project))
    assert _hits(store) == []
    text = "".join(f.read_text(encoding="utf-8", errors="replace") for f in store.rglob("*") if f.is_file()
                   and f.suffix in (".jsonl", ".json"))
    assert "command that names a secret file" in text


@pytest.mark.parametrize("cmd", ["ls -la src", "python -m pytest tests -q", "git diff HEAD~1 -- src/app.py",
                                 "echo NOTE=1 >> notes.txt"])
def test_an_ordinary_command_is_stored_as_it_was(tmp_path, cmd):
    project = make_project(tmp_path)
    meta = _import(tmp_path, project, [_call("c1", "Bash", {"command": cmd})],
                   [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}])
    rec = json.loads(meta.read_text(encoding="utf-8"))
    assert rec["tool_calls"][0]["command"] == cmd
    raw = meta.with_name(meta.name.replace(".meta.json", ".jsonl")).read_text(encoding="utf-8")
    assert json.dumps(cmd)[1:-1] in raw
