"""A control secret in EVERY string field of every input devmem records never reaches the disk (verifier round 5:
the StopFailure `error` and SessionEnd `reason` fields were stored raw, because redaction was applied field by field).

Redaction now happens at the write boundary (hooks._append, sessions.write_record, the hook state): these tests put a
different GitHub-format token (a format the built-in rules always catch) into every string of every hook event of a
recorded session, every key, the transcript, every MCP tool argument, and every input of `devmem begin/end/import/run`,
then search every byte of the store, the per-user folder and the temp folder (and what the commands print) for any of
them. The tokens are built at run time (no literal token in the source)."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
from hooks_helpers import FIXTURES, TURN1, TURN2, load_payload, make_project

from devmem import hooks

_PREFIX = "gh" + "p_"


class Tokens:
    """One distinct control token per field, remembered with the field's name."""

    def __init__(self):
        self.where: dict[str, str] = {}

    def make(self, where: str) -> str:
        tag = f"Z{len(self.where):04d}"
        self.where[tag] = where
        return _PREFIX + (tag + "Q" * 40)[:36]

    def taint(self, x, where: str, skip_keys=()):
        """A copy of `x` with a token appended to every string (and an extra key holding one in every dict)."""
        if isinstance(x, str):
            return x + " " + self.make(where)
        if isinstance(x, list):
            return [self.taint(v, f"{where}[{i}]", skip_keys) for i, v in enumerate(x)]
        if isinstance(x, dict):
            out = {k: (v if k in skip_keys else self.taint(v, f"{where}.{k}", skip_keys)) for k, v in x.items()}
            out[self.make(f"{where}.<key>")] = self.make(f"{where}.<extra value>")
            return out
        return x

    def tails(self) -> dict[bytes, str]:
        return {(tag + "Q" * 40)[:36].encode("ascii"): where for tag, where in self.where.items()}


def _leaks(tokens: Tokens, dirs: list[Path], texts: dict[str, str] | None = None) -> list[str]:
    found = []
    tails = tokens.tails()
    for d in dirs:
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            try:
                data = p.read_bytes()
            except OSError:
                continue
            for tail, where in tails.items():
                if tail in data or tail.decode("ascii") in str(p):
                    found.append(f"{where} -> {p}")
    for name, text in (texts or {}).items():
        for tail, where in tails.items():
            if tail.decode("ascii") in (text or ""):
                found.append(f"{where} -> output of {name}")
    return found


@pytest.fixture()
def temp_dir(tmp_path, monkeypatch):
    t = tmp_path / "temp"
    t.mkdir()
    for k in ("TMP", "TEMP", "TMPDIR"):
        monkeypatch.setenv(k, str(t))
    monkeypatch.setattr(tempfile, "tempdir", str(t))
    return t


def _user_data() -> Path:
    return Path(os.environ["DEVMEM_USER_DATA"])


def test_every_hook_field_and_the_transcript_are_redacted_before_any_write(tmp_path, monkeypatch, temp_dir):
    project = make_project(tmp_path / "p")
    store = project / ".devmemory"
    (store / "config.json").write_text('{"deliver": true}', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    tok = Tokens()
    transcript = tmp_path / "transcript.jsonl"
    lines = (FIXTURES / "transcript_excerpt.jsonl").read_text(encoding="utf-8").splitlines()
    # every string of every transcript line (kept parseable: `type` stays as it is)
    tainted_lines = [json.dumps(tok.taint(json.loads(ln), f"transcript[{i}]", skip_keys=("type",)))
                     for i, ln in enumerate(lines)]
    transcript.write_text("", encoding="utf-8")
    outs = []

    def send(event: str, payload: dict):
        outs.append(hooks.run([event], json.dumps(payload).encode("utf-8"))[1])

    keep = ("session_id", "transcript_path")
    for name in TURN1 + TURN2:
        event = name.split("_", 1)[1]
        if event == "Stop":
            transcript.write_text("\n".join(tainted_lines) + "\n", encoding="utf-8")
        payload = load_payload(name, project, transcript)
        send(event, tok.taint(payload, name, skip_keys=keep))
        if event == "SessionStart":
            sid = payload["session_id"]
            base = {"session_id": sid, "cwd": str(project), "transcript_path": str(transcript)}
            # events the recorded fixture does not hold
            send("StopFailure", tok.taint({**base, "hook_event_name": "StopFailure", "error": "rate_limit",
                                           "error_details": "429 from the API"}, "StopFailure", skip_keys=keep))
            send("PostCompact", tok.taint({**base, "hook_event_name": "PostCompact", "trigger": "auto",
                                           "compact_summary": "the summary"}, "PostCompact", skip_keys=keep))
            send("UserPromptSubmit", tok.taint({**base, "hook_event_name": "UserPromptSubmit", "prompt": "p",
                                                "prompt_id": "pid-1"}, "UserPromptSubmit2", skip_keys=keep))
    # the fields kept above, one at a time (a session id that is not a plain id is refused: its error is logged)
    sid2 = "5e55107d-0000-4000-8000-0000000000aa"
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
        send(event, {"session_id": sid2, "cwd": str(project), "hook_event_name": event, "prompt": "q",
                     "transcript_path": str(transcript) + " " + tok.make(f"{event}.transcript_path")})
    send("UserPromptSubmit", {"session_id": "s " + tok.make("session_id"), "cwd": str(project), "prompt": "q"})
    send("PostToolUse", {"session_id": tok.make("session_id alone"), "cwd": str(project), "tool_name": "Bash",
                         "tool_input": {"command": "ls"}})

    from devmem.pack import build_pack
    text, _info = build_pack(project, store, "greet farewell notes summary rate limit")
    from devmem.sessions import load_sessions
    assert load_sessions(store, project)
    leaks = _leaks(tok, [store, _user_data(), temp_dir], {"hook stdout": "\n".join(outs), "pack": text})
    assert not leaks, "\n".join(leaks[:40])
    assert len(tok.where) > 300      # the sweep really covered the payloads


def test_every_mcp_tool_argument_is_redacted_before_any_write(tmp_path, temp_dir):
    from devmem.mcp_server import TOOLS, Server
    project = make_project(tmp_path / "p")
    store = project / ".devmemory"
    (store / "config.json").write_text('{"deliver": false, "mcp_call_log": true}', encoding="utf-8")
    tok = Tokens()
    srv = Server(project, store)
    replies = []
    begun = srv.call("begin_session", tok.taint({}, "begin_session"))
    pending = begun.split("pending id: ", 1)[1].split()[0]
    (project / "greet.py").write_text("def greet(name):\n    return 'hi ' + name\n", encoding="utf-8")
    for t in TOOLS:
        name = t["name"]
        args = {k: f"{k} value" for k in t["inputSchema"].get("properties", {})}
        args.pop("budget", None)
        if name == "end_session":
            replies.append(srv.call(name, {**tok.taint(args, name, skip_keys=("pending_id",)), "pending_id": pending}))
            args["pending_id"] = "x"
        try:
            replies.append(srv.call(name, tok.taint(args, name)))
        except (ValueError, RuntimeError, FileNotFoundError, KeyError) as e:
            replies.append(str(e))
    assert any(p.name.endswith(".meta.json") for p in (store / "sessions").iterdir())
    leaks = _leaks(tok, [store, _user_data(), temp_dir])
    assert not leaks, "\n".join(leaks[:40])


def _stream(tok: Tokens, project: Path) -> list[str]:
    events = [
        {"type": "system", "subtype": "init", "session_id": "5e55107d-0000-4000-8000-0000000000bb", "model": "m"},
        {"type": "assistant", "message": {"model": "m", "content": [
            {"type": "text", "text": "I will look"},
            {"type": "thinking", "thinking": "hidden"},
            {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "ls", "description": "list"}},
            {"type": "tool_use", "id": "b2", "name": "Edit", "input": {"file_path": str(project / "greet.py"),
                                                                       "old_string": "a", "new_string": "b"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "b1", "content": "greet.py"},
            {"type": "tool_result", "tool_use_id": "b2", "is_error": True,
             "content": [{"type": "text", "text": "failed"}]}]},
         "tool_use_result": {"stdout": "greet.py", "stderr": "warn"}},
        {"type": "result", "subtype": "success", "result": "done", "session_id": "x", "usage": {}},
    ]
    out = [json.dumps(tok.taint(e, f"stream[{i}]", skip_keys=("type", "subtype", "id", "tool_use_id")))
           for i, e in enumerate(events)]
    return out + ["not json " + tok.make("stream non-json line")]


def test_every_begin_end_import_and_run_input_is_redacted_before_any_write(tmp_path, monkeypatch, temp_dir, capsys):
    from devmem import capture, cli
    project = make_project(tmp_path / "p")
    store = project / ".devmemory"
    (store / "config.json").write_text('{"deliver": false}', encoding="utf-8")
    tok = Tokens()
    assert cli.main(["begin", "--project", str(project)]) == 0
    (project / "greet.py").write_text("def greet(name):\n    return 'hi ' + name\n", encoding="utf-8")
    assert cli.main(["end", "--project", str(project), "--prompt", "end prompt " + tok.make("end --prompt"),
                     "--summary", "sum " + tok.make("end --summary"),
                     "--provider", "cursor " + tok.make("end --provider")]) == 0
    stream = tmp_path / "in.jsonl"
    stream.write_text("\n".join(_stream(tok, project)) + "\n", encoding="utf-8")
    assert cli.main(["import", str(stream), "--project", str(project),
                     "--prompt", "import prompt " + tok.make("import --prompt")]) == 0

    def fake_run_claude(prompt, cwd, stream_out, **kw):
        Path(stream_out).write_text("\n".join(_stream(tok, project)) + "\n", encoding="utf-8")
        return {}
    monkeypatch.setattr(capture, "run_claude", fake_run_claude)
    assert cli.main(["run", "run prompt " + tok.make("run prompt"), "--project", str(project),
                     "--model", "haiku " + tok.make("run --model")]) == 0
    printed = capsys.readouterr()
    assert len(list((store / "sessions").glob("*.meta.json"))) == 3
    leaks = _leaks(tok, [store, _user_data(), temp_dir], {"stdout": printed.out, "stderr": printed.err})
    assert not leaks, "\n".join(leaks[:40])
