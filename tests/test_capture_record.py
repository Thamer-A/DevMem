"""Headless stream-json capture (record_session) and the freshness regression for Write-created files."""
from __future__ import annotations

import json
from pathlib import Path

from devmem import sessions as S
from devmem.capture import record_session

from hooks_helpers import make_project, run_git

KEY = "ghp_" + "Q9w8E7r6T5y4U3i2O1p0" * 2


def _stream(project: Path, extra_blocks: list[dict] | None = None) -> list[dict]:
    p = lambda n: str(project / n)  # noqa: E731
    big = "z" * 4300
    blocks = [
        {"type": "thinking", "thinking": "SECRET-THOUGHTS-SENTINEL", "signature": "sig"},
        {"type": "text", "text": "I will make the changes."},
        {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": p("new_mod.py"),
                                                                     "content": "def created():\n    return 42\n"}},
        {"type": "tool_use", "id": "t2", "name": "Write", "input": {"file_path": p("big.txt"), "content": big}},
        {"type": "tool_use", "id": "t3", "name": "MultiEdit", "input": {"file_path": p("greet.py"), "edits": [
            {"old_string": "def greet(name):", "new_string": "def greet(name: str) -> str:"},
            {"old_string": '"hello "', "new_string": '"hey "'}]}},
        {"type": "tool_use", "id": "t4", "name": "Read", "input": {"file_path": p(".env")}},
        {"type": "tool_use", "id": "t5", "name": "Bash", "input": {"command": f"echo {KEY}"}},
        {"type": "tool_use", "id": "t6", "name": "Bash", "input": {"command": "rm notes.txt"}},
    ] + (extra_blocks or [])
    return [
        {"type": "system", "subtype": "init", "session_id": "5e55107d-0000-4000-8000-000000000001", "model": "m"},
        {"type": "assistant", "message": {"content": blocks}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t4", "content": "API_TOKEN=supersecretvalue123"},
            {"type": "tool_result", "tool_use_id": "t5", "content": KEY}]},
         "tool_use_result": {"type": "text", "file": {"content": "API_TOKEN=supersecretvalue123"}},
         "timestamp": "2026-11-13T10:00:05.000Z"},
        {"type": "result", "result": f"Done. The token {KEY} was printed.", "session_id":
            "5e55107d-0000-4000-8000-000000000001", "usage": {"input_tokens": 1, "output_tokens": 2},
         "num_turns": 3},
    ]


def _apply(project: Path) -> None:
    (project / "new_mod.py").write_text("def created():\n    return 42\n", encoding="utf-8")
    (project / "big.txt").write_text("z" * 4300, encoding="utf-8")
    (project / "greet.py").write_text('def greet(name: str) -> str:\n    return "hey " + name\n', encoding="utf-8")
    (project / "notes.txt").unlink()


def _record(tmp_path, git_start=True):
    project = make_project(tmp_path)
    (project / ".env").write_text("API_TOKEN=supersecretvalue123", encoding="utf-8")
    start = S.git_snapshot(project) if git_start else None
    _apply(project)
    stream = tmp_path / "stream.jsonl"
    stream.write_text("\n".join(json.dumps(e) for e in _stream(project)) + "\n", encoding="utf-8")
    meta = record_session(project, stream, "make the changes", "2026-11-13T10:00:00Z", extra={"race_step": "X"},
                          store=project / ".devmemory", git_start=start)
    return project, meta, json.loads(meta.read_text(encoding="utf-8"))


def test_write_multiedit_delete_and_git_recorded(tmp_path):
    project, meta, rec = _record(tmp_path)
    assert rec["schema"] == 2 and len(rec["turns"]) == 1 and rec["race_step"] == "X"
    calls = rec["tool_calls"]
    w = next(c for c in calls if c["tool"] == "Write" and c["file"] == "new_mod.py")
    assert w["new"].startswith("def created()") and w["truncated"] is False
    big = next(c for c in calls if c.get("file") == "big.txt")
    assert len(big["new"]) == 4000 and big["truncated"] is True
    me = [c for c in calls if c["tool"] == "MultiEdit"]
    assert [c["edit_index"] for c in me] == [0, 1] and me[1]["new"] == '"hey "'
    assert rec["files_deleted"] == ["notes.txt"]
    assert {"tool": "Delete", "file": "notes.txt", "source": "observed on disk at capture"} in calls
    head = run_git(project, "rev-parse", "HEAD").strip()
    assert rec["git"]["head_start"] == head and rec["git"]["head_end"] == head
    assert rec["files_edited"] == ["big.txt", "greet.py", "new_mod.py"]


def test_raw_and_digest_are_redacted_and_thinking_dropped(tmp_path):
    project, meta, rec = _record(tmp_path)
    raw = (meta.parent / rec["raw_record"]).read_text(encoding="utf-8")
    text = meta.read_text(encoding="utf-8") + raw
    assert KEY not in text and "[REDACTED:github-token]" in text
    assert "supersecretvalue123" not in text  # content of .env (a secret file) never stored
    assert "SECRET-THOUGHTS-SENTINEL" not in text
    assert len(raw.splitlines()) == 4  # line numbers in the digest still point at the raw record


def test_freshness_sees_rewritten_or_deleted_write(tmp_path):
    """Regression (design doc problem 2): a file created with Write was reported fresh forever."""
    from devmem.pack import freshness
    project, meta, rec = _record(tmp_path)
    [s] = S.load_sessions(project / ".devmemory")
    s = dict(s, tool_calls=[c for c in s["tool_calls"] if c.get("file") == "new_mod.py"])
    assert freshness(s, project)["status"] == "fresh"
    (project / "new_mod.py").write_text("def other():\n    return 0\n", encoding="utf-8")
    assert freshness(s, project)["status"] == "stale"
    (project / "new_mod.py").unlink()
    assert freshness(s, project)["status"] == "stale"


def test_v1_call_signature_still_works(tmp_path):
    project = make_project(tmp_path, git=False)
    stream = tmp_path / "s.jsonl"
    stream.write_text("\n".join(json.dumps(e) for e in _stream(project)) + "\n", encoding="utf-8")
    meta = record_session(project, stream, "p", "2026-11-13T10:00:00Z")  # bench/ calls it like this
    rec = json.loads(meta.read_text(encoding="utf-8"))
    assert meta.parent == project / ".devmemory" / "sessions" and rec["git"] == {}
    for k in ("prompt", "result_text", "files_read", "files_edited", "file_hashes_at_capture", "usage",
              "assistant_text", "tool_calls", "errors", "raw_record", "model", "provider", "capture_method"):
        assert k in rec


def test_grep_or_mcp_read_of_a_secret_file_is_omitted_from_the_stream_record(tmp_path):
    project = make_project(tmp_path)
    blocks = [{"type": "tool_use", "id": "g1", "name": "Grep",
               "input": {"pattern": "PASS", "path": str(project / ".env"), "output_mode": "content"}},
              {"type": "tool_use", "id": "m1", "name": "mcp__filesystem__read_text_file", "input": {"path": ".env"}}]
    events = [
        {"type": "system", "subtype": "init", "session_id": "5e55107d-0000-4000-8000-000000000002", "model": "m"},
        {"type": "assistant", "message": {"content": blocks}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "g1", "content": "DB_PASS=hunter2hunter2"},
            {"type": "tool_result", "tool_use_id": "m1", "content": "STRIPE_LIVE=plainvalue12345"}]},
         "tool_use_result": {"mode": "content", "content": "DB_PASS=hunter2hunter2"}},
        {"type": "result", "result": "ok", "session_id": "5e55107d-0000-4000-8000-000000000002"}]
    stream = tmp_path / "s.jsonl"
    stream.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    meta = record_session(project, stream, "check", "2026-11-13T10:00:00Z", store=project / ".devmemory")
    rec = json.loads(meta.read_text(encoding="utf-8"))
    text = meta.read_text(encoding="utf-8") + (meta.parent / rec["raw_record"]).read_text(encoding="utf-8")
    assert "hunter2hunter2" not in text and "plainvalue12345" not in text
    assert rec["turns"][0]["prompt_source"] == "user"


def test_run_and_capture_tells_the_project_hooks_not_to_record_again(tmp_path, monkeypatch):
    """`devmem run` in a project with `devmem init` hooks: the session it launches must be recorded once (from
    its stream-json), so the hooks inside it are told to skip capture (they still deliver context)."""
    import devmem.capture as C
    project = make_project(tmp_path)
    seen = {}

    def fake_run_claude(prompt, cwd, stream_out, **kw):
        seen.update(kw)
        stream_out.write_text("\n".join(json.dumps(e) for e in _stream(project)[:1]) + "\n", encoding="utf-8")
        return {}
    monkeypatch.setattr(C, "run_claude", fake_run_claude)
    C.run_and_capture(project, "task", isolated=False, store=project / ".devmemory")
    assert seen["env"] == {"DEVMEM_NO_CAPTURE": "1"}
    assert len(list((project / ".devmemory" / "sessions").glob("*.meta.json"))) == 1


def test_stream_capture_filters_secret_file_lines_like_the_hooks(tmp_path):
    """`devmem run`/`import` apply the same output filter as the hooks (round-2 review: a `grep -rn` result
    line `./.env:1:KEY=...` was stored in the raw record)."""
    project = make_project(tmp_path)
    key = "0f9a8b7c6d5e4f3a2b1c"
    sid = "5e55107d-0000-4000-8000-000000000003"
    events = [
        {"type": "system", "subtype": "init", "session_id": sid, "model": "m"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "grep -rn OPENWEATHER ."}},
            {"type": "tool_use", "id": "b2", "name": "Bash", "input": {"command": "git diff"}},
            {"type": "tool_use", "id": "b3", "name": "Bash", "input": {"command": "git show HEAD:.env"}},
            {"type": "tool_use", "id": "b4", "name": "Bash", "input": {"command": "grep -rn X . ; false"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "b1",
             "content": f"./.env:1:OPENWEATHER={key}\n./app.py:2:weather = OPENWEATHER"},
            {"type": "tool_result", "tool_use_id": "b2", "content": [{"type": "text", "text":
             f"diff --git a/.env b/.env\n@@ -1 +1 @@\n-OPENWEATHER={key}\n+OPENWEATHER=x"}]},
            {"type": "tool_result", "tool_use_id": "b3", "content": f"OPENWEATHER={key}"},
            {"type": "tool_result", "tool_use_id": "b4", "is_error": True, "content": f"./.env-1-K={key}"}]},
         "tool_use_result": {"stdout": f"./.env:1:OPENWEATHER={key}", "stderr": ""}},
        {"type": "result", "result": "ok", "session_id": sid}]
    stream = tmp_path / "s.jsonl"
    stream.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    meta = record_session(project, stream, "where is the weather key used", "2026-11-14T00:00:00Z",
                          store=project / ".devmemory")
    hits = [f.name for f in (project / ".devmemory").rglob("*") if f.is_file() and key.encode() in f.read_bytes()]
    assert hits == []
    raw = (meta.parent / json.loads(meta.read_text(encoding="utf-8"))["raw_record"]).read_text(encoding="utf-8")
    assert "./app.py:2:weather = OPENWEATHER" in raw and "[OMITTED: line from a secret file]" in raw
