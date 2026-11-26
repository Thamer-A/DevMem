"""A pack says who wrote a prompt: the user, or the AI client that reported it through MCP end_session."""
from __future__ import annotations

from pack_helpers import FILES, make_project, turn, write_session

from devmem.pack import build_pack
from devmem.scan import scan


def build(tmp_path, source):
    project, store = make_project(tmp_path)
    t = turn("Make the blades orbit the player: start with 2 blades, orbit radius 7 studs.", "Blades now orbit.",
             [{"tool": "Write", "file": "src/blades.py", "new": FILES["src/blades.py"], "truncated": False}])
    if source is not None:
        t["prompt_source"] = source
    write_session(store, "bbbb2222-0000-0000-0000-000000000002", "2026-11-01T11:00:00Z", [t])
    scan(project, store)
    return build_pack(project, store, "the orbit radius of the blades feels wrong")[0]


def test_user_prompts_keep_the_verbatim_label(tmp_path):
    for source in ("user", None):          # None: records written before the field existed
        text = build(tmp_path / str(source), source)
        assert 'User asked (verbatim): "Make the blades orbit' in text
        assert "reported by the AI client" not in text


def test_client_reported_prompts_are_not_called_the_users_words(tmp_path):
    text = build(tmp_path, "reported-by-client")
    assert "Request as reported by the AI client (not the user's verbatim text)" in text
    assert "User asked" not in text and "the user's words, verbatim" not in text
    assert "orbit radius 7 studs" in text            # still retrieved, just labelled honestly
