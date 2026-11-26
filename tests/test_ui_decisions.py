"""The app's Decisions list reads every turn of a session, not only its first prompt."""
from __future__ import annotations

from pack_helpers import make_project, turn, write_session
from ui_helpers import request, running_server, with_token

from devmem.scan import scan


def test_decision_stated_in_a_later_turn_is_listed(tmp_path):
    project, store = make_project(tmp_path)
    write_session(store, "aaaa1111-0000-0000-0000-000000000001", "2026-11-01T10:00:00Z", [
        turn("Add a login page with a form.", "ok"),
        turn("We decided that passwords must never be logged anywhere in the app.", "noted",
             started_at="2026-11-01T10:05:00Z"),
        turn("Rename the form fields.", "done", started_at="2026-11-01T10:10:00Z")])
    scan(project, store)
    with running_server(project, store) as (port, st):
        _, _, body = request(port, "GET", "/api/decisions", with_token(st))
        texts = [d["text"] for d in body["decisions"]]
        assert any("passwords must never be logged" in t for t in texts), texts
        d = next(d for d in body["decisions"] if "passwords" in d["text"])
        assert d["turn"] == 2


def test_client_reported_prompt_is_not_a_user_decision_and_the_session_view_carries_the_source(tmp_path):
    project, store = make_project(tmp_path)
    t = turn("We decided that passwords must never be logged anywhere in the app.", "noted")
    t["prompt_source"] = "reported-by-client"
    write_session(store, "bbbb2222-0000-0000-0000-000000000002", "2026-11-01T10:00:00Z", [t])
    scan(project, store)
    with running_server(project, store) as (port, st):
        _, _, dec = request(port, "GET", "/api/decisions", with_token(st))
        assert dec["decisions"] == []
        _, _, ss = request(port, "GET", "/api/sessions", with_token(st))
        assert ss["sessions"][0]["prompt_source"] == "reported-by-client"
        _, _, one = request(port, "GET", "/api/session?id=bbbb2222", with_token(st))
        assert one["meta"]["turns"][0]["prompt_source"] == "reported-by-client"
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "plugin" / "devmem" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "reported-by-client" in js and "Prompt not captured" in js
    assert "<h2>You asked</h2>" not in js
