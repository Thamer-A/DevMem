"""A tool's path is stored the same way whichever route recorded the call: the hooks, `devmem import` of a Claude Code
transcript, and `devmem import` / `devmem run` of a stream-json record (both are capture.record_session). Each takes
the one decision devmem.sessions.project_file (decode_path, respell_path): a path inside the project is stored
project-relative, any other is left out with the same reason (`path_omitted`), and `files_read` agrees. The review read
the import as keeping `$HOME/x` and `%USERPROFILE%\\x` as written; on 50d731e it already did not (this pins it)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import capture as C
from devmem import consent, hooks, sessions, store

SID = "55555555-6666-7777-8888-999999999999"
TOOLS = (("Read", "file_path"), ("Grep", "path"), ("mcp__fs__read_file", "path"))


def _spellings(project: Path) -> dict[str, str]:
    forms = {"var_home": "$HOME/zq81/diary.txt", "var_brace": "${HOME}/zq81/diary.txt",
             "var_windows": "%USERPROFILE%\\zq81\\diary.txt", "var_powershell": "$env:USERPROFILE\\zq81\\d.txt",
             "var_inside": "src/$X/a.py", "tilde_user": "~zq81user/diary.txt", "tilde": "~/zq81/diary.txt",
             "inside_absolute": str(project / "greet.py"), "inside_relative": "./greet.py",
             "project_folder": str(project), "project_folder_slash": str(project) + os.sep,
             "outside_absolute": str(project.parent / "zq81" / "x.py"),
             "file_url_inside": "file://" + ("" if project.as_posix().startswith("/") else "/") + (project / "greet.py").as_posix()}
    if os.name == "nt":
        forms["drive_relative"] = "C:zq81\\diary.txt"
    return forms


SPELLINGS = sorted(_spellings(Path("p")))


def _view(rec: dict) -> dict:
    t = rec["turns"][0]
    calls = [{k: c[k] for k in ("tool", "file", "path", "path_omitted") if k in c} for c in t["tool_calls"]
             if c.get("tool") in {n for n, _ in TOOLS}]
    return {"calls": calls, "files_read": t["files_read"]}


def _by_hooks(project: Path, v: str) -> dict:
    def hook(event, **payload):
        store._FILTERS.clear()
        consent.forget_decisions()
        code, _ = hooks.run([event], json.dumps({"session_id": SID, "cwd": str(project), "hook_event_name": event,
                                                  "transcript_path": str(project / "none.jsonl"),
                                                  **payload}).encode("utf-8"))
        assert code == 0
    hook("UserPromptSubmit", prompt="look")
    for i, (name, key) in enumerate(TOOLS):
        inp = {key: v, **({"pattern": "x"} if name == "Grep" else {})}
        hook("PostToolUse", tool_name=name, tool_input=inp, tool_response={"content": "text"}, tool_use_id=f"c{i}")
    hook("Stop", last_assistant_message="done")
    return _view(json.loads(next((project / ".devmemory" / "sessions").glob("*.meta.json")).read_text("utf-8")))


def _stream_lines(project: Path, v: str, transcript: bool) -> list[dict]:
    calls = [{"type": "tool_use", "id": f"c{i}", "name": name, "input": {key: v, **({"pattern": "x"} if name == "Grep"
                                                                                      else {})}}
             for i, (name, key) in enumerate(TOOLS)]
    results = [{"type": "tool_result", "tool_use_id": f"c{i}", "content": "text"} for i in range(len(TOOLS))]
    body = [{"type": "assistant", "message": {"role": "assistant", "content": calls}},
            {"type": "user", "message": {"role": "user", "content": results}}]
    if transcript:     # a transcript's shape: camelCase ids and its metadata, no system/init or result event
        return [{**e, "sessionId": SID, "uuid": f"u{n}", "parentUuid": f"u{n - 1}" if n else None, "cwd": str(project),
                 "timestamp": f"2026-11-16T10:00:0{n}.000Z", "isSidechain": False, "userType": "external"}
                for n, e in enumerate(body)]
    return [{"type": "system", "subtype": "init", "session_id": SID, "model": "m"}, *body,
            {"type": "result", "result": "done", "session_id": SID}]


def _by_import(project: Path, v: str, transcript: bool) -> dict:
    path = project.parent / "s.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in _stream_lines(project, v, transcript)) + "\n", encoding="utf-8")
    meta = C.record_session(project, path, "look", "2026-11-16T10:00:00Z", store=project / ".devmemory")
    return _view(json.loads(meta.read_text(encoding="utf-8")))


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_every_route_stores_a_path_the_same_way(tmp_path, monkeypatch, spelling):
    got = {}
    for route in ("hooks", "import_transcript", "import_stream"):
        project = make_project(tmp_path / route)
        (project / "src").mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
        v = _spellings(project)[spelling]
        got[route] = _by_hooks(project, v) if route == "hooks" else _by_import(project, v, route == "import_transcript")
    assert got["import_transcript"] == got["hooks"] and got["import_stream"] == got["hooks"], got
    calls = got["hooks"]["calls"]
    assert len(calls) == len(TOOLS)
    if spelling.startswith("project_folder"):    # the project folder itself is "." on every route (p10: imports said
        assert calls[0]["file"] == "." and calls[1]["path"] == "."    # "outside the project")
    elif spelling.startswith("inside") or spelling == "file_url_inside":
        assert got["hooks"]["files_read"] == ["greet.py"] and calls[0]["file"] == "greet.py"
    elif spelling == "var_inside":
        assert calls[0]["path_omitted"] == calls[1]["path_omitted"] == sessions.NOT_LITERAL_PATH
    else:
        assert got["hooks"]["files_read"] == [] and all("file" not in c and "path" not in c for c in calls), calls
        assert {c["path_omitted"] for c in calls} <= set(sessions.PATH_REASONS)
        if spelling.startswith("var_") or spelling in ("drive_relative", "tilde_user"):     # on every OS, whatever
            # users it has (`~user` was a lookup: Windows guessed a folder, Linux kept it as a project file name)
            assert {c["path_omitted"] for c in calls} == {sessions.NOT_LITERAL_PATH}
