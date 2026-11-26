"""The evidence gate: a prompt with nothing to retrieve gets an empty pack (no header, no conventions)."""
from __future__ import annotations

import pytest

from devmem.pack import build_pack
from devmem.text import tokenize

from pack_helpers import setup

CONTENTLESS = ["yes go ahead", "thanks, that works", "continue", "ok, go ahead", "thanks!", "ok",
               "شكراً، ممتاز", "merci, c'est parfait", "好的，继续"]


@pytest.mark.parametrize("prompt", CONTENTLESS)
def test_contentless_prompt_gets_empty_pack(tmp_path, prompt):
    project, store, _ = setup(tmp_path)
    text, info = build_pack(project, store, prompt)
    assert text == ""
    assert info["files"] == [] and info["sessions"] == [] and info["tokens_est"] == 0
    assert info["empty_reason"]


def test_code_keywords_are_not_evidence(tmp_path):
    """`continue` and `ok` occur in several code bodies (keyword, local variable): still nothing to say."""
    project, store, _ = setup(tmp_path)
    for p in ("continue", "ok continue"):
        text, info = build_pack(project, store, p)
        assert text == "", info["signal"]
        assert info["signal"].get(tokenize("continue")[0]) in ("body", "none")


def test_short_real_request_gets_a_pack(tmp_path):
    project, store, _ = setup(tmp_path)
    text, info = build_pack(project, store, "fix the shop")
    assert "src/shop.py" in info["files"]
    assert info["signal"]["shop"] == "name"
    assert text.startswith("# DevMemory context pack")


def test_users_own_words_count_as_evidence(tmp_path):
    """'orbit radius' is not a name in the code, but the user said it in an earlier request."""
    project, store, recs = setup(tmp_path)
    text, info = build_pack(project, store, "the orbit radius feels wrong")
    assert any(s["session"] == recs["blades"]["session_id"] for s in info["sessions"])
    assert "orbit radius 7 studs" in text


def test_static_mode_has_no_sessions(tmp_path):
    project, store, _ = setup(tmp_path)
    text, info = build_pack(project, store, "make rerolling the shop offers cost more", mode="static")
    assert info["sessions"] == [] and "session " not in text
    assert "src/shop.py" in info["files"]


def test_no_terms_at_all(tmp_path):
    project, store, _ = setup(tmp_path)
    assert build_pack(project, store, "?!")[0] == ""
    assert build_pack(project, store, "")[0] == ""
