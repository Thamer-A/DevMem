"""A path a tool names outside the project is not stored by the hooks (devmem.sessions.project_file, omit_outside_paths);
a path inside the project in an unusual spelling is recognised as inside and stored project-relative. The reviewer's
c10 outside_paths: files_read / files_edited kept a sibling project's path, `//server/share/...` and a file under the
user's Documents, in the record and the raw log."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import consent, hooks, sessions, store

SID = "0a0a0a0a-0000-4000-8000-000000000001"
NEEDLE = "zq81"                 # in every outside path; never in the project's own path or in tool output
WIN = os.name == "nt"


def _hook(project, event, **payload) -> None:
    store._FILTERS.clear()
    consent.forget_decisions()
    code, _ = hooks.run([event], json.dumps({"session_id": SID, "cwd": str(project), "hook_event_name": event,
                                              "transcript_path": str(project / "none.jsonl"),
                                              **payload}).encode("utf-8"))
    assert code == 0


def _tool(project, name, tool_input, response, tid) -> None:
    _hook(project, "PostToolUse", tool_name=name, tool_input=tool_input, tool_response=response, tool_use_id=tid)


def _outside_forms(tmp_path: Path) -> dict[str, str]:
    """Every spelling a path value can arrive in that is not a path inside the project (each holds NEEDLE)."""
    other = tmp_path / f"{NEEDLE}other"
    other.mkdir(exist_ok=True)
    (other / "notes.txt").write_text("other project\n", encoding="utf-8")
    fwd = (other / "notes.txt").as_posix()
    url = "file://" + ("" if fwd.startswith("/") else "/") + fwd
    forms = {"sibling": str(other / "notes.txt"), "sibling_fwd": fwd,
             "relative_up": f"../../{other.name}/notes.txt", "unc": f"\\\\{NEEDLE}srv\\share\\plan.txt",
             "unc_fwd": f"//{NEEDLE}srv/share/plan.txt",
             "home": str(Path.home() / "Documents" / f"{NEEDLE}diary.txt"),
             "tilde": f"~/{NEEDLE}diary.txt",
             "file_url": url, "file_url_localhost": "file://localhost" + url[len("file://"):],
             "file_url_host": f"file://{NEEDLE}srv/share/plan.txt",
             "file_url_one_slash": "file:" + url[len("file://"):],
             "file_url_escaped": url.replace("notes", "n%6Ftes"),
             "vscode_remote": f"vscode-remote://ssh-remote+{NEEDLE}/home/me/x.py",
             "https": f"https://{NEEDLE}.example/x.py",
             "var_home": f"$HOME/{NEEDLE}/diary.txt", "var_brace": f"${{HOME}}/{NEEDLE}/diary.txt",
             "var_windows": f"%USERPROFILE%\\{NEEDLE}\\diary.txt"}
    if WIN:
        forms["long_prefix"] = "\\\\?\\" + str(other / "notes.txt")
        forms["git_bash"] = "/" + str(other)[0].lower() + fwd[2:]
        forms["unc_long"] = f"\\\\?\\UNC\\{NEEDLE}srv\\share\\plan.txt"
        forms["drive_relative"] = f"C:{NEEDLE}\\diary.txt"
    return forms


OUTSIDE_SPELLINGS = ["sibling", "sibling_fwd", "relative_up", "unc", "unc_fwd", "home", "tilde", "file_url",
                     "file_url_localhost", "file_url_host", "file_url_one_slash", "file_url_escaped", "vscode_remote",
                     "https", "var_home", "var_brace", "var_windows"] + (
    ["long_prefix", "git_bash", "unc_long", "drive_relative"] if WIN else [])


def _store_hits(project) -> list[str]:
    hits = []
    for f in (project / ".devmemory").rglob("*"):
        if f.is_file():
            b = f.read_bytes()
            if NEEDLE.encode("utf-8") in b or NEEDLE.encode("utf-16-le") in b:
                hits.append(f.relative_to(project).as_posix())
    return hits


def _every_field(project, v: str) -> None:
    """The value `v` under every field a hook stores a path from: Claude Code's file and search tools (input and
    structured response), an MCP tool's `path`, `uri`, a nested key, a structured response's path keys, and a path used
    as a dictionary KEY of an input and of a response."""
    _tool(project, "Read", {"file_path": v}, {"type": "text", "file": {"filePath": v, "content": "text"}}, "r")
    _tool(project, "Edit", {"file_path": v, "old_string": "a", "new_string": "b"},
          {"filePath": v, "oldString": "a", "newString": "b", "structuredPatch": []}, "e")
    _tool(project, "Write", {"file_path": v, "content": "c"}, {"type": "update", "filePath": v}, "w")
    _tool(project, "MultiEdit", {"file_path": v, "edits": [{"old_string": "a", "new_string": "b"}]}, {"filePath": v}, "me")
    _tool(project, "NotebookEdit", {"notebook_path": v, "new_source": "s"}, {"ok": True}, "n")
    _tool(project, "Grep", {"pattern": "x", "path": v}, {"mode": "files_with_matches", "filenames": [v]}, "g")
    _tool(project, "Glob", {"pattern": "*.txt", "path": v}, {"filenames": [v], "numFiles": 1}, "l")
    _tool(project, "mcp__fs__read_file", {"path": v}, {"content": [{"type": "text", "text": "text"}]}, "m1")
    _tool(project, "mcp__fs__open", {"uri": v}, {"content": [{"type": "text", "text": "text"}]}, "m2")
    _tool(project, "mcp__fs__op", {"args": {"target": v, "options": {"source": v}}}, {"ok": True}, "m3")
    _tool(project, "mcp__lsp__diagnostics", {"files": {v: "check"}},
          {"structuredContent": {"filePath": v, "filenames": [v], "byFile": {v: [1, 2]}}}, "m4")


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize("spelling", OUTSIDE_SPELLINGS)
def test_no_store_file_keeps_a_path_that_is_not_one_inside_the_project(tmp_path, spelling):
    """Every spelling under every field (the reviewer's h21: `file:///C:/Users/<me>/Documents/diary.txt` under an MCP
    `path` was stored as `file:/C:/Users/...`; k20/h20: an outside path as a dict key was kept)."""
    project = make_project(tmp_path / "p")
    v = _outside_forms(tmp_path)[spelling]
    _hook(project, "SessionStart", source="startup")
    _hook(project, "UserPromptSubmit", prompt="look around")
    _every_field(project, v)
    _hook(project, "Stop", last_assistant_message="done")
    _hook(project, "SessionEnd", reason="other")
    assert not (project / ".devmemory" / "hook_errors.log").exists()
    assert _store_hits(project) == [], (spelling, _store_hits(project))
    if spelling == "file_url_escaped":
        stored = "".join(f.read_text(encoding="utf-8") for f in (project / ".devmemory").rglob("*.json*"))
        assert "n%6Ftes" not in stored
    rec = json.loads(next((project / ".devmemory" / "sessions").glob("*.meta.json")).read_text(encoding="utf-8"))
    t = rec["turns"][0]
    assert t["files_read"] == [] and t["files_edited"] == [] and t["files_deleted"] == [], t
    reasons = {c.get("path_omitted") for c in t["tool_calls"] if c.get("tool") in ("Read", "Edit", "Write", "Grep")}
    assert len(reasons) == 1 and reasons <= set(sessions.PATH_REASONS), reasons
    assert not [c for c in t["tool_calls"] if "file" in c or "path" in c], t["tool_calls"]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_path_devmem_cannot_take_literally_is_no_deleted_file(tmp_path):
    """The reviewer's h21: a successful MCP read of `$HOME/notes/diary.txt` or `%USERPROFILE%\\notes\\diary.txt` was
    recorded in files_deleted. A path is evidence of a deletion only when Claude Code's own file tools read or wrote it
    successfully (so it existed in the project then); a path another tool names, or one that is not literal, is not."""
    project = make_project(tmp_path / "p")
    (project / "gone.txt").write_text("soon gone\n", encoding="utf-8")
    _hook(project, "SessionStart", source="startup")
    _hook(project, "UserPromptSubmit", prompt="read")
    for i, v in enumerate(["$HOME/notes/diary.txt", "%USERPROFILE%\\notes\\diary.txt", "ghost.txt", "src/ghost.py"]):
        _tool(project, "mcp__fs__read_file", {"path": v}, {"content": [{"type": "text", "text": "dear diary"}]}, f"m{i}")
    _tool(project, "Read", {"file_path": str(project / "gone.txt")},
          {"type": "text", "file": {"filePath": str(project / "gone.txt"), "content": "soon gone"}}, "r1")
    (project / "gone.txt").unlink()           # by a shell command, say
    _hook(project, "Stop", last_assistant_message="done")
    rec = json.loads(next((project / ".devmemory" / "sessions").glob("*.meta.json")).read_text(encoding="utf-8"))
    assert rec["turns"][0]["files_deleted"] == ["gone.txt"], rec["turns"][0]
    omitted = {c.get("path_omitted") for c in rec["turns"][0]["tool_calls"] if c.get("tool") == "mcp__fs__read_file"}
    assert sessions.NOT_LITERAL_PATH in omitted


def _short_name(p: Path) -> str | None:
    if not WIN:
        return None
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024)
    return buf.value if n and buf.value != str(p) else None


def _inside_forms(project: Path) -> dict[str, str]:
    f = project / "src" / "x.py"
    forms = {"native": str(f), "dot_slash": "./src/x.py", "relative": "src/x.py", "dotted": str(project / "src" / ".." / "src" / "x.py")}
    url = "file://" + ("" if f.as_posix().startswith("/") else "/") + f.as_posix()
    forms["file_url"] = url
    forms["file_url_localhost"] = "file://localhost" + url[len("file://"):]
    forms["file_url_one_slash"] = "file:" + url[len("file://"):]
    forms["file_url_escaped"] = url.replace("x.py", "%78.py")
    if WIN:
        forms["long_prefix"] = "\\\\?\\" + str(f)
        forms["git_bash"] = "/" + str(f)[0].lower() + f.as_posix()[2:]
        forms["wsl"] = "/mnt/" + str(f)[0].lower() + f.as_posix()[2:]
        forms["cygdrive"] = "/cygdrive/" + str(f)[0].lower() + f.as_posix()[2:]
        forms["data_stream_type"] = str(f) + "::$DATA"           # the file itself, not a variable
        forms["upper_case"] = str(f).upper()
        forms["lower_case"] = str(f).lower()
        short = _short_name(f)
        if short:
            forms["short_8_3"] = short
    lnk = project / "lnk"
    try:
        if WIN:
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(lnk), str(project / "src")], capture_output=True)
            ok = r.returncode == 0
        else:
            lnk.symlink_to(project / "src", target_is_directory=True)
            ok = True
    except OSError:
        ok = False
    if ok:
        forms["through_link"] = str(lnk / "x.py")
    return forms


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_path_inside_the_project_in_any_spelling_is_stored_relative(tmp_path):
    project = make_project(tmp_path / "p")
    (project / "src").mkdir()
    (project / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")
    forms = _inside_forms(project)
    _hook(project, "SessionStart", source="startup")
    _hook(project, "UserPromptSubmit", prompt="read it")
    for i, fp in enumerate(forms.values()):
        _tool(project, "Read", {"file_path": fp}, {"type": "text", "file": {"filePath": fp, "content": "x = 1"}}, f"r{i}")
    _hook(project, "Stop", last_assistant_message="done")
    lines = [json.loads(x) for x in (project / ".devmemory" / "live" / f"{SID}.jsonl").read_text(
        encoding="utf-8").splitlines() if x.strip()]
    got = {e["tool_use_id"]: e["calls"][0].get("file") for e in lines if e.get("event") == "PostToolUse"}
    assert {name: got[f"r{i}"] for i, name in enumerate(forms)} == {name: "src/x.py" for name in forms}
    # an alternate data stream of a project file (Windows) or a name with a colon (POSIX): inside, not a plain name;
    # the hooks and the import store it the same way, as this reason (tool_entries serves both)
    assert sessions.project_file(str(project / "src" / "x.py") + ":stream", project) == (None, sessions.NOT_PLAIN_NAME)
    e = sessions.tool_entries("Read", {"file_path": "src/x.py:stream"}, project)[0]
    assert "file" not in e and e["path_omitted"] == sessions.NOT_PLAIN_NAME


def test_project_file_says_why_a_path_is_not_stored(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    assert sessions.project_file(str(project / "a.py"), project) == ("a.py", None)
    assert sessions.project_file(str(tmp_path / "b.py"), project) == (None, sessions.OUTSIDE_PROJECT)
    assert sessions.project_file("//host/share/x", project) == (None, sessions.OUTSIDE_PROJECT)
    assert sessions.project_file("x" * 5000, project)[1] == "5000 characters: not a path"
    assert sessions.project_file("", project) == (None, None)


def test_omit_outside_paths_is_bounded(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    big = {"filenames": [str(tmp_path / f"o{i}.txt") for i in range(100_000)] + [str(project / "in.txt")]}
    t = time.perf_counter()
    out = sessions.omit_outside_paths(big, project)
    assert time.perf_counter() - t < 10
    # compared one by one up to OUTSIDE_PATHS_LOOKED_AT, then by text, for the first OUTSIDE_NODES_LOOKED_AT values
    assert out["filenames"][0] == out["filenames"][5_000] == sessions.OUTSIDE_PROJECT
    assert len(out["filenames"]) == 100_001 and big["filenames"][0] != sessions.OUTSIDE_PROJECT   # a copy
    assert out["filenames"][-1] == str(project / "in.txt")
