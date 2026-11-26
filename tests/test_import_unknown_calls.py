"""One decision per tool result (devmem.capture._Calls): a result whose call devmem did not look at, for any reason,
has its text stored nowhere, neither in the raw record nor in the digest (meta.json: errors, tool calls, files).
Fourth review: the digest kept the .env text of a failing result whose call the raw record had left out."""
from __future__ import annotations

import json

import pytest
from hooks_helpers import make_project

from devmem import capture as C

SECRET = "QX7SECRETPLAIN"
ENV_TEXT = f"SERVICE_HOST=internal-db-zq81.corp.example\nNOTE={SECRET}\n"


def _call(tid, how, project):
    inp = {"file_path": str(project / ".env")} if how == "read" else {"command": "cat .env; exit 1"}
    b = {"type": "tool_use", "name": "Read" if how == "read" else "Bash", "input": inp}
    if tid is not None:
        b["id"] = tid
    return b


def _result(tid, failing):
    b = {"type": "tool_result", "content": ENV_TEXT}
    if tid is not None:
        b["tool_use_id"] = tid
    if failing:
        b["is_error"] = True
    return b


def _asst(blocks, **extra):
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}, **extra}


def _user(blocks):
    return {"type": "user", "message": {"role": "user", "content": blocks},
            "tool_use_result": {"stdout": ENV_TEXT, "stderr": "", "file": {"content": ENV_TEXT}}}


def _plain(i, project):
    return {"type": "tool_use", "id": f"p{i}", "name": "Read", "input": {"file_path": str(project / "greet.py")}}


REASONS = ["line_cap", "stream_cap", "deep_line", "long_line", "past_text_budget", "no_id", "id_never_seen",
           "user_event", "name_not_a_string", "input_not_an_object", "not_json", "known_secret_call"]


def _stream(reason, how, failing, project, monkeypatch):
    init = {"type": "system", "subtype": "init", "session_id": "11111111-2222-3333-4444-555555555555", "model": "m"}
    end = {"type": "result", "result": "done", "session_id": "11111111-2222-3333-4444-555555555555"}
    call, tid = _call("ts", how, project), "ts"
    lines: list = [init]
    if reason == "line_cap":
        monkeypatch.setattr(C, "MAX_LINE_TOOL_USES", 3)
        lines.append(_asst([_plain(i, project) for i in range(3)] + [call]))
    elif reason == "stream_cap":
        monkeypatch.setattr(C, "MAX_STREAM_ITEMS", 3)
        lines += [_asst([_plain(i, project)]) for i in range(3)] + [_asst([call])]
    elif reason == "deep_line":
        lines.append(_asst([call], deep=json.loads("[" * 70 + "]" * 70)))
    elif reason == "long_line":
        monkeypatch.setattr(C, "MAX_LINE_PARSE_CHARS", 2_000)
        lines.append(_asst([call], pad="x" * 3_000))
    elif reason == "past_text_budget":
        monkeypatch.setattr(C, "STREAM_TEXT_BUDGET", 1_000)
        lines += [_asst([{"type": "text", "text": "y" * 2_000}]), _asst([call])]
    elif reason == "no_id":
        call, tid = _call(None, how, project), None
        lines.append(_asst([call]))
    elif reason == "id_never_seen":
        lines.append(_asst([call]))
        tid = "other"
    elif reason == "user_event":
        lines.append({"type": "user", "message": {"role": "user", "content": [call]}})
    elif reason == "name_not_a_string":
        lines.append(_asst([{**call, "name": {"x": call["name"]}}]))
    elif reason == "input_not_an_object":
        lines.append(_asst([{**call, "input": [call["input"]]}]))
    elif reason == "not_json":
        lines.append("garbage " + json.dumps(_asst([call])))
    else:
        lines.append(_asst([call]))
    lines += [_user([_result(tid, failing)]), end]
    path = project.parent / "s.jsonl"
    path.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def _store_bytes(store):
    return [(f, f.read_bytes()) for f in store.rglob("*") if f.is_file()]


@pytest.mark.parametrize("failing", [True, False], ids=["failing", "succeeding"])
@pytest.mark.parametrize("how", ["read", "bash"])
@pytest.mark.parametrize("reason", REASONS)
def test_a_result_of_a_call_not_looked_at_stores_no_text_anywhere(tmp_path, monkeypatch, reason, how, failing):
    project = make_project(tmp_path)
    (project / ".env").write_text(ENV_TEXT, encoding="utf-8")
    store = project / ".devmemory"
    info: dict = {}
    meta = C.record_session(project, _stream(reason, how, failing, project, monkeypatch), "x",
                            "2026-11-16T00:00:00Z", store=store, info=info)
    hits = [str(f.relative_to(store)) for f, data in _store_bytes(store) if SECRET.encode() in data
            or "zq81".encode() in data]
    assert hits == [], hits
    rec = json.loads(meta.read_text(encoding="utf-8"))
    assert rec["result_text"] == "done"
    if failing and reason not in ("deep_line", "long_line", "past_text_budget", "not_json"):
        errors = [e["error"] for e in rec["turns"][0]["errors"]]   # the failure is kept, as a fixed text
        assert errors == [C.OMITTED_SECRET_FILE if reason == "known_secret_call" else C.LEFT_OUT_TOOL]


@pytest.mark.parametrize("event", [
    {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "bash -lc 'cat .env'",
                                        "aggregated_output": ENV_TEXT, "exit_code": 0, "status": "completed"}},
    {"type": "item.completed", "item": {"id": "i2", "type": "command_execution", "command": "grep -rn NOTE .",
                                        "aggregated_output": "./.env:2:" + ENV_TEXT.splitlines()[1] + "\n",
                                        "exit_code": 0}},
    {"type": "item.completed", "item": {"id": "i3", "type": "mcp_tool_call", "server": "fs", "tool": "read_file",
                                        "arguments": {"path": ".env"}, "result": {"content": ENV_TEXT}}},
], ids=["cat_env", "grep_lines", "mcp_read"])
def test_a_codex_tool_event_keeps_no_secret_file_text(tmp_path, event):
    """The same rule for `devmem codex`'s events (generic._tool_event): they were stored with redaction only."""
    from devmem import generic as G
    project = make_project(tmp_path)
    store = project / ".devmemory"
    G.record_generic(project, "x", {}, {}, "2026-11-16T00:00:00Z", "codex-cli", store=store, events=[event])
    hits = [str(f.relative_to(store)) for f, data in _store_bytes(store) if SECRET.encode() in data]
    assert hits == [], hits
