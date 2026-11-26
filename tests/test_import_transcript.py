"""`devmem import` of a Claude Code transcript (the JSONL Claude Code keeps per session), fifth review blocker 2: its
user events carry the tool output again under camelCase `toolUseResult` (`file.content`, `stdout`, `structuredPatch`,
`originalFile`, ...), which was stored with redaction only, and its ids are `sessionId`. Every content line is synthetic;
the field names follow the transcript format.

The rule is structural (capture._side): on an event that carries tool calls or results, every field besides the
message content and short metadata is kept only when every call and result on it is let through (_Calls), else it is
a fixed marker; on any other event a field that refers to a call by id takes that call's decision, one holding a call
or result block (or the id of no known call) is a marker, and one that names a secret file keeps its paths only."""
from __future__ import annotations

import json

import pytest
from hooks_helpers import make_project
from test_import_unknown_calls import ENV_TEXT, REASONS, SECRET, _stream

from devmem import capture as C

SID = "abcdef01-2222-3333-4444-555555555555"
NEEDLES = [SECRET.encode(), b"zq81", SECRET.encode("utf-16-le")]


def _hits(store) -> list[str]:
    return sorted({str(f.relative_to(store)) for f in store.rglob("*") if f.is_file()
                   for n in NEEDLES if n in f.read_bytes()})


def _base(project, n: int) -> dict:
    return {"parentUuid": f"u{n - 1}" if n else None, "isSidechain": False, "userType": "external",
            "cwd": str(project), "sessionId": SID, "version": "2.1.0", "gitBranch": "main", "uuid": f"u{n}",
            "timestamp": f"2026-11-16T10:00:{n % 60:02d}.000Z"}


def _transcript_result(event: dict) -> dict:
    """The camelCase side channel a transcript user event carries for its tool result."""
    return {"stdout": ENV_TEXT, "stderr": "", "interrupted": False, "isImage": False,
            "file": {"filePath": ".env", "content": ENV_TEXT, "numLines": 2, "startLine": 1, "totalLines": 2},
            "originalFile": ENV_TEXT, "oldString": ENV_TEXT, "newString": ENV_TEXT,
            "structuredPatch": [{"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 2,
                                 "lines": ["-" + x for x in ENV_TEXT.splitlines()]}]}


def _as_transcript(path, project) -> None:
    """The stream-json lines of test_import_unknown_calls._stream in the transcript's shape: no system/init and no
    result event, camelCase ids and `toolUseResult`, the transcript's metadata fields."""
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        try:
            e = json.loads(line)
        except ValueError:
            out.append(line)                  # a line that is not JSON stays what it is
            continue
        if not isinstance(e, dict) or e.get("type") in ("system", "result"):
            continue
        e = {**_base(project, n), **e}
        if "tool_use_result" in e:
            del e["tool_use_result"]
            e["toolUseResult"] = _transcript_result(e)
        out.append(json.dumps(e))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


@pytest.mark.parametrize("failing", [True, False], ids=["failing", "succeeding"])
@pytest.mark.parametrize("how", ["read", "bash"])
@pytest.mark.parametrize("reason", REASONS)
def test_a_transcript_stores_no_secret_file_text_for_any_reason(tmp_path, monkeypatch, reason, how, failing):
    project = make_project(tmp_path)
    (project / ".env").write_text(ENV_TEXT, encoding="utf-8")
    path = _stream(reason, how, failing, project, monkeypatch)
    _as_transcript(path, project)
    store = project / ".devmemory"
    meta = C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=store, info={})
    assert _hits(store) == []
    rec = json.loads(meta.read_text(encoding="utf-8"))
    assert rec["session_id"] == SID and SID[:8] in meta.name


# ---------------------------------------------------------------- the other channels of a transcript

def _ev(project, n, **fields) -> dict:
    return {**_base(project, n), **fields}


def _asst(project, n, blocks, **extra) -> dict:
    return _ev(project, n, type="assistant", requestId=f"req_{n}", **extra,
               message={"id": f"msg_{n}", "type": "message", "role": "assistant", "model": "claude-test",
                        "content": blocks, "stop_reason": None, "usage": {"input_tokens": 3, "output_tokens": 5}})


def _user_result(project, n, tid, content, tur, failing=False) -> dict:
    return _ev(project, n, type="user", promptId="p1", sourceToolAssistantUUID=f"u{n - 1}", toolUseResult=tur,
               message={"role": "user", "content": [{"tool_use_id": tid, "type": "tool_result", "content": content,
                                                      **({"is_error": True} if failing else {})}]})


def _channels(project) -> list[dict]:
    env = str(project / ".env")
    ok = str(project / "greet.py")
    lines = [
        _ev(project, 0, type="user", message={"role": "user", "content": "update the env and the greeting"}),
        # a Read of .env, its result and its camelCase copy; the Write of a value into it, with the transcript's copy
        # of the call's input (wireToolInputs) and a later attachment that copies the input by id
        _asst(project, 1, [{"type": "text", "text": "Reading the settings."},
                           {"type": "tool_use", "id": "toolu_r", "name": "Read", "input": {"file_path": env}}],
              wireToolInputs={"toolu_r": {"file_path": env}}),
        _user_result(project, 2, "toolu_r", [{"type": "text", "text": ENV_TEXT}],
                     {"type": "text", "file": {"filePath": env, "content": ENV_TEXT, "numLines": 2}}),
        _asst(project, 3, [{"type": "tool_use", "id": "toolu_w", "name": "Bash",
                            "input": {"command": f"echo NOTE={SECRET} >> .env", "description": "set the note"}}],
              wireToolInputs={"toolu_w": {"command": f"echo NOTE={SECRET} >> .env", "description": "set the note"}}),
        _user_result(project, 4, "toolu_w", "", {"stdout": "", "stderr": "", "interrupted": False}),
        _ev(project, 5, type="attachment", attachment={"type": "tool_input_copies", "entries": [], "toolInputCopies": [
            {"id": "toolu_w", "copy": json.dumps({"command": f"echo NOTE={SECRET} >> .env"})}]}),
        # an Edit of .env: the structured patch, the original file and the strings
        _asst(project, 6, [{"type": "tool_use", "id": "toolu_e", "name": "Edit",
                            "input": {"file_path": env, "old_string": f"NOTE={SECRET}", "new_string": "NOTE=x"}}]),
        _user_result(project, 7, "toolu_e", "The file was updated.", {
            "filePath": env, "oldString": f"NOTE={SECRET}", "newString": "NOTE=x", "originalFile": ENV_TEXT,
            "structuredPatch": [{"oldStart": 2, "oldLines": 1, "newStart": 2, "newLines": 1,
                                 "lines": [f"-NOTE={SECRET}", "+NOTE=x"]}], "userModified": False}),
        # an image block in the result of a .env read (base64 of the file)
        _asst(project, 8, [{"type": "tool_use", "id": "toolu_i", "name": "Read", "input": {"file_path": env}}]),
        _user_result(project, 9, "toolu_i", [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                         "data": ENV_TEXT}}], "img"),
        # file history, attachments of files, summaries, hook output, a compaction, the queue, a request blob and a
        # progress event that nests a subagent's result
        _ev(project, 10, type="file-history-snapshot", messageId="msg_1", isSnapshotUpdate=False, snapshot={
            "messageId": "msg_1", "timestamp": "2026-11-16T10:00:10.000Z",
            "trackedFileBackups": {".env": {"backupFileName": "abc123@v1", "version": 1, "backupTime": "t"}}}),
        _ev(project, 11, type="attachment", attachment={"type": "edited_text_file", "filename": env,
                                                        "snippet": ENV_TEXT}),
        _ev(project, 12, type="attachment", attachment={"type": "files", "files": [
            {"path": env, "content": ENV_TEXT, "type": "text"}, {"path": ok, "content": "def greet(): ...", "type": "text"}]}),
        {"type": "summary", "summary": "Updated the greeting", "leafUuid": "u12"},
        _ev(project, 13, type="system", subtype="informational", content="PostToolUse hook ran", level="info"),
        _ev(project, 14, type="user", isCompactSummary=True, isVisibleInTranscriptOnly=True,
            message={"role": "user", "content": "This session is being continued: the greeting was changed."}),
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "t", "sessionId": SID,
         "content": "also run the tests"},
        _ev(project, 15, type="api-request-blob", hash="h1", message={"role": "user", "content": [
            {"tool_use_id": "toolu_r", "type": "tool_result", "content": ENV_TEXT}]}),
        _ev(project, 16, type="progress", toolUseID="agent_1", parentToolUseID="toolu_task", data={
            "type": "agent_progress", "message": {"type": "user", "message": {"role": "user", "content": [
                {"tool_use_id": "toolu_sub", "type": "tool_result", "content": ENV_TEXT}]}}}),
        # an MCP-style call and result block kinds devmem does not look at
        _asst(project, 17, [{"type": "server_tool_use", "id": "srv_1", "name": "web_fetch",
                             "input": {"url": "file:///" + env.replace("\\", "/")}}]),
        _user_result(project, 18, "srv_1", ENV_TEXT, {"content": ENV_TEXT}),
        _ev(project, 19, type="user", message={"role": "user", "content": [
            {"type": "mcp_tool_result", "tool_use_id": "mcp_1", "content": [{"type": "text", "text": ENV_TEXT}]}]}),
        # the side channel on an assistant event, an int id against a string id
        _asst(project, 20, [{"type": "tool_use", "id": 7, "name": "Read", "input": {"file_path": env}}],
              toolUseResult={"file": {"content": ENV_TEXT}}),
        _asst(project, 21, [{"type": "tool_use", "id": "7", "name": "Read", "input": {"file_path": ok}}]),
        _user_result(project, 22, "7", ENV_TEXT, {"file": {"content": ENV_TEXT}}),
        _asst(project, 23, [{"type": "text", "text": "Done: the greeting says hi."}]),
    ]
    return lines


def test_every_channel_of_a_transcript_keeps_no_secret_file_text(tmp_path):
    project = make_project(tmp_path)
    (project / ".env").write_text(ENV_TEXT, encoding="utf-8")
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in _channels(project)) + "\n", encoding="utf-8")
    store = project / ".devmemory"
    info: dict = {}
    meta = C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=store, info=info)
    assert _hits(store) == []
    rec = json.loads(meta.read_text(encoding="utf-8"))
    assert rec["session_id"] == SID and rec["model"] == "claude-test"
    texts = [t["text"] for t in rec["assistant_text"]]
    assert "Reading the settings." in texts and "Done: the greeting says hi." in texts
    assert [c["tool"] for c in rec["tool_calls"]][:3] == ["Read", "Bash", "Edit"]
    raw = [json.loads(x) for x in meta.with_name(meta.name.replace(".meta.json", ".jsonl")).read_text(
        encoding="utf-8").splitlines()]
    by_uuid = {e.get("uuid"): e for e in raw if isinstance(e, dict)}
    assert by_uuid["u2"]["toolUseResult"] == C.OMITTED_SECRET_FILE
    assert by_uuid["u3"]["wireToolInputs"] == C.OMITTED_SECRET_FILE
    assert ".env" in by_uuid["u3"]["message"]["content"][0]["input"]["command"]
    assert by_uuid["u5"]["attachment"] == C.OMITTED_SECRET_FILE                  # the copy of the secret call's input
    files = by_uuid["u12"]["attachment"]["files"]          # the field names a secret file: its paths only are kept
    assert [f["content"] for f in files] == [C.OMITTED_SECRET_FILE] * 2 and files[1]["path"].endswith("greet.py")
    assert by_uuid["u16"]["data"] == C.LEFT_OUT_TOOL
    assert by_uuid["u14"]["message"]["content"].startswith("This session is being continued")
    assert info["tool_calls_left_out"] == 1                                      # the server_tool_use


def test_a_transcript_and_its_stream_json_give_the_same_digest(tmp_path):
    """Recognising the format changes no decision: the digest of a transcript equals that of the same session as
    stream-json (but for the session id the transcript names)."""
    project = make_project(tmp_path)
    blocks = [{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": str(project / "greet.py")}}]
    stream = [{"type": "assistant", "message": {"role": "assistant", "content": blocks}},
              {"type": "user", "message": {"role": "user", "content": [
                  {"type": "tool_result", "tool_use_id": "toolu_1", "content": "def greet(): ..."}]},
               "tool_use_result": {"file": {"content": "def greet(): ..."}}}]
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text("\n".join(json.dumps(e) for e in stream) + "\n", encoding="utf-8")
    b.write_text("\n".join(json.dumps(e) for e in stream) + "\n", encoding="utf-8")
    _as_transcript(b, project)
    recs = []
    for p, store in ((a, tmp_path / "sa"), (b, tmp_path / "sb")):
        meta = C.record_session(project, p, "x", "2026-11-16T00:00:00Z", store=store, info={})
        rec = json.loads(meta.read_text(encoding="utf-8"))
        recs.append((rec["tool_calls"], rec["files_read"], rec["errors"]))
    assert recs[0] == recs[1] and recs[0][1] == ["greet.py"]


# ---------------------------------------------------------------- a secret file's content beside its path (round 2)
# Shapes surveyed in real transcripts (key structure only): an object that holds a path and that file's content side by
# side. Each sits in a field kept on an "ok" decision (an ordinary call: `python scripts/setup.py`, a Read of greet.py,
# or an event with no call); the part naming .env keeps its paths only, whatever the keys around it are called.

LINES = [f"-{x}" for x in ENV_TEXT.splitlines()]


def _side_shapes(env: str, ok: str) -> dict:
    hunks = [{"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 0, "lines": LINES}]
    return {
        "bash_edit_diff": {"stdout": "done", "stderr": "", "interrupted": False, "bashEditDiff": {
            "files": [{"filePath": env, "hunks": hunks}], "moreFiles": 0, "changedFiles": [env]}},
        "bash_edit_diff_other_key": {"stdout": "done", "editsSeen": [{"target": env, "chunks": [{"text": LINES}]}]},
        "structured_patch": {"filePath": env, "structuredPatch": hunks, "originalFile": ENV_TEXT, "oldString": LINES[0]},
        "file_content": {"type": "text", "file": {"filePath": env, "content": ENV_TEXT, "numLines": 2}},
        "file_list": {"file_list": {"files": [{"path": ok, "content_type": "text"},
                                              {"path": env, "content_type": "text", "preview": ENV_TEXT}]}},
        "files_written": {"files_written": [{"path": env, "bytes": ENV_TEXT}]},
        "map_keyed_by_path": {"diagnostics": {env.replace("\\", "/"): [{"message": ENV_TEXT, "severity": 1}]}},
        "uri_beside_text": {"content": [{"type": "resource", "resource": {"uri": "file:///" + env.replace("\\", "/"),
                                                                          "text": ENV_TEXT}}]},
    }


@pytest.mark.parametrize("shape", list(_side_shapes(".env", "a.py")))
@pytest.mark.parametrize("form", ["transcript", "stream_json"])
def test_a_secret_files_content_beside_its_path_in_an_ordinary_calls_result_is_not_stored(tmp_path, shape, form):
    project = make_project(tmp_path)
    (project / ".env").write_text(ENV_TEXT, encoding="utf-8")
    side = _side_shapes(str(project / ".env"), str(project / "greet.py"))[shape]
    call = {"type": "tool_use", "id": "toolu_b", "name": "Bash", "input": {"command": "python scripts/setup.py"}}
    events = [{"type": "assistant", "message": {"role": "assistant", "content": [call]}},
              {"type": "user", "message": {"role": "user", "content": [
                  {"type": "tool_result", "tool_use_id": "toolu_b", "content": "done", "is_error": True}]},
               "tool_use_result": side}]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    if form == "transcript":
        _as_transcript(path, project)
        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        lines[1]["toolUseResult"] = side                 # the shape itself, not the generic copy _as_transcript puts
        path.write_text("\n".join(json.dumps(e) for e in lines) + "\n", encoding="utf-8")
    store = project / ".devmemory"
    meta = C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=store, info={})
    assert _hits(store) == []
    raw = meta.with_name(meta.name.replace(".meta.json", ".jsonl")).read_text(encoding="utf-8")
    assert "done" in raw                               # the rest of the ordinary call's result is kept


@pytest.mark.parametrize("shape", ["attachment_files", "attachment_content_file", "skills", "diagnostics_uri",
                                   "snippet", "saved_copy"])
def test_a_secret_files_content_beside_its_path_on_an_event_with_no_call_is_not_stored(tmp_path, shape):
    project = make_project(tmp_path)
    (project / ".env").write_text(ENV_TEXT, encoding="utf-8")
    env = str(project / ".env")
    att = {"attachment_files": {"type": "files", "files": [{"path": env, "content": ENV_TEXT, "type": "text"}]},
           "attachment_content_file": {"type": "file", "content": {"file": {"filePath": env, "content": ENV_TEXT}}},
           "skills": {"type": "skills", "skills": [{"name": "x", "path": env, "content": ENV_TEXT}]},
           "diagnostics_uri": {"type": "diagnostics", "files": [{"uri": "file:///" + env.replace("\\", "/"),
                                                                 "diagnostics": [{"message": ENV_TEXT}]}]},
           "snippet": {"type": "edited_text_file", "filename": env, "snippet": ENV_TEXT},
           "saved_copy": {"type": "file", "displayPath": ".env", "filename": env, "content": ENV_TEXT,
                          "savedCopyFraming": "x"}}[shape]
    events = [_ev(project, 0, type="user", message={"role": "user", "content": "go"}),
              _ev(project, 1, type="attachment", attachment=att)]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    store = project / ".devmemory"
    C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=store, info={})
    assert _hits(store) == []


def test_tool_input_copies_of_an_id_no_call_has_are_not_stored(tmp_path):
    project = make_project(tmp_path)
    events = [_ev(project, 0, type="user", message={"role": "user", "content": "go"}),
              _ev(project, 1, type="attachment", attachment={"type": "deferred_tools_record", "entries": [],
                                                             "toolInputCopies": [{"id": "t_gone", "copy": json.dumps(
                                                                 {"command": f"echo NOTE={SECRET} >> .env"})}]})]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    store = project / ".devmemory"
    meta = C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=store, info={})
    assert _hits(store) == []
    raw = meta.with_name(meta.name.replace(".meta.json", ".jsonl")).read_text(encoding="utf-8")
    assert json.loads(raw.splitlines()[1])["attachment"] == C.LEFT_OUT_TOOL


def test_an_ordinary_result_without_a_secret_file_is_kept_as_it_was(tmp_path):
    """The rule changes nothing where no secret file is named: an Edit's structured patch of greet.py is kept."""
    project = make_project(tmp_path)
    ok = str(project / "greet.py")
    call = {"type": "tool_use", "id": "e1", "name": "Edit", "input": {"file_path": ok, "old_string": "a",
                                                                      "new_string": "b"}}
    side = {"filePath": ok, "oldString": "a", "newString": "b", "structuredPatch": [{"lines": ["-a", "+b"]}]}
    events = [{"type": "assistant", "message": {"content": [call]}},
              {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "e1", "content": "ok"}]},
               "tool_use_result": side}]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    meta = C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory", info={})
    raw = [json.loads(x) for x in meta.with_name(meta.name.replace(".meta.json", ".jsonl")).read_text(
        encoding="utf-8").splitlines()]
    assert raw[1]["tool_use_result"]["structuredPatch"] == [{"lines": ["-a", "+b"]}]
