"""The app's session views: turns, in-progress sessions, and the loader switch (old and new records)."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from cli_helpers import write_session
from hooks_helpers import TURN1, TURN2, replay
from hooks_helpers import make_project as make_hooks_project
from ui_helpers import request, running_server, with_token

from devmem import hooks
from devmem.scan import scan

APP_JS = Path(__file__).resolve().parent.parent / "plugin" / "devmem" / "ui" / "app.js"


def test_completed_two_turn_session_and_in_progress_session(tmp_path, monkeypatch):
    project = make_hooks_project(tmp_path / "a")
    store = project / ".devmemory"
    replay(project, TURN1 + TURN2, tmp_path / "t.jsonl", monkeypatch, hooks)       # one finished session, 2 turns
    write_session(store, "eeee5555-0000-0000-0000-000000000005", "old schema-1 request")  # a v1 record
    live = tmp_path / "b"
    scan(project, store)
    with running_server(project, store) as (port, st):
        _, _, body = request(port, "GET", "/api/sessions", with_token(st))
        rows = body["sessions"]
        assert len(rows) == 2
        old = next(r for r in rows if r["id"].startswith("eeee5555"))
        assert old["turn_count"] == 1 and old["in_progress"] is False
        new = next(r for r in rows if r["id"] != old["id"])
        assert new["turn_count"] == 2
        _, _, d = request(port, "GET", "/api/session?id=" + quote(new["id"][:8]), with_token(st))
        turns = d["meta"]["turns"]
        assert len(turns) == 2 and turns[0]["prompt"] and turns[1]["prompt"] != turns[0]["prompt"]
        assert any(c["tool"] in ("Edit", "Write") for c in turns[0]["tool_calls"])
        _, _, d1 = request(port, "GET", "/api/session?id=eeee5555", with_token(st))
        assert d1["meta"]["turns"][0]["prompt"] == "old schema-1 request"
    assert not live.exists()


def test_session_still_in_progress_is_flagged(tmp_path, monkeypatch):
    project = make_hooks_project(tmp_path / "a")
    store = project / ".devmemory"
    replay(project, TURN1[:5], tmp_path / "t.jsonl", monkeypatch, hooks)  # prompt and two tool calls, no Stop
    scan(project, store)
    with running_server(project, store) as (port, st):
        _, _, body = request(port, "GET", "/api/sessions", with_token(st))
        assert len(body["sessions"]) == 1 and body["sessions"][0]["in_progress"] is True
        sid = body["sessions"][0]["id"]
        _, _, d = request(port, "GET", "/api/session?id=" + quote(sid), with_token(st))
        assert d["meta"]["in_progress"] is True and d["meta"]["turns"][0]["tool_calls"]
        _, _, ov = request(port, "GET", "/api/overview", with_token(st))
        assert ov["session_count"] == 1


def test_captured_text_reaches_the_page_only_through_esc():
    """Everything captured is untrusted: each place app.js puts turn text into HTML must go through esc()."""
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("async function vSession")
    body = js[start:js.index("\nasync function", start + 10) if "\nasync function" in js[start + 10:] else start + 8000]
    for field in ("tn.prompt", "tn.result_text", "m.prompt", "m.result_text", "it.a.text", "t.command", "t.old", "t.new"):
        examined = 0
        for m in re.finditer(re.escape(field) + r"\b", body):
            examined += 1
            after = body[m.end():m.end() + 6]
            since = body[body.rfind("${", 0, m.start()):m.start()]
            # `${tn.prompt || ""}` is output; `${x ? ...}`, `a && b` and `String(x || "")` are conditions or hand-offs
            fallback_output = after.lstrip().startswith("||") and since.strip() == "${"
            if re.match(r"\s*(\?|\|\||&&|\))", after) and not fallback_output and "esc(" not in since:
                continue  # a condition or a hand-off to a helper that escapes (diffHtml), not output
            assert "esc(" in since or "diffHtml(" in since, (field, since)
        assert examined, f"{field} does not occur in vSession: the test would check nothing"
    assert "insertAdjacentHTML" not in body
