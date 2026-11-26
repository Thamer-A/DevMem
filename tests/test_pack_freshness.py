"""Freshness: which remembered change is gone from which file; quiet when irrelevant to the task."""
from __future__ import annotations

from devmem.pack import build_pack, freshness

from pack_helpers import setup, turn, write_session


def _rec(calls, deleted=()):
    return {"tool_calls": calls, "files_edited": sorted({c["file"] for c in calls}), "files_deleted": list(deleted)}


def test_write_text_is_checked(tmp_path):
    project, _store, _ = setup(tmp_path, with_sessions=False)
    rec = _rec([{"tool": "Write", "file": "src/audio.py", "new": "def play_sound(name):\n    return name\n"}])
    assert freshness(rec, project)["status"] == "fresh"
    (project / "src/audio.py").write_text("def play(name):\n    pass\n", encoding="utf-8")
    fr = freshness(rec, project)
    assert fr["status"] == "stale"
    assert fr["edits_gone_from"] == ["src/audio.py"]
    assert "def play_sound(name):" in fr["missing"]["src/audio.py"]
    (project / "src/audio.py").unlink()
    assert freshness(rec, project)["status"] == "stale"


def test_v1_keys_and_values(tmp_path):
    project, _store, _ = setup(tmp_path, with_sessions=False)
    fr = freshness(_rec([]), project)
    assert set(fr) >= {"status", "edits_gone_from", "edits_present_in", "read_files_changed"}
    assert fr["status"] in ("fresh", "partly-stale", "stale", "context-changed")


def test_sessions_own_deletion_is_not_stale(tmp_path):
    project, _store, _ = setup(tmp_path, with_sessions=False)
    rec = _rec([{"tool": "Write", "file": "src/tmp_tool.py", "new": "print('temporary helper')\n"}],
               deleted=["src/tmp_tool.py"])
    fr = freshness(rec, project)
    assert fr["status"] == "fresh" and fr["superseded"] == ["src/tmp_tool.py"]


def test_later_write_in_same_session_supersedes(tmp_path):
    project, _store, _ = setup(tmp_path, with_sessions=False)
    rec = _rec([{"tool": "Edit", "file": "src/audio.py", "old": "x", "new": "def something_removed_later():"},
                {"tool": "Write", "file": "src/audio.py", "new": "def play_sound(name):\n    return name\n"}])
    assert freshness(rec, project)["status"] == "fresh"


def test_truncated_text_ignores_cut_last_line(tmp_path):
    project, _store, _ = setup(tmp_path, with_sessions=False)
    rec = _rec([{"tool": "Write", "file": "src/audio.py", "new": "def play_sound(name):\n    return nam",
                 "truncated": True}])
    assert freshness(rec, project)["status"] == "fresh"


def test_multiedit_entries(tmp_path):
    project, _store, _ = setup(tmp_path, with_sessions=False)
    rec = _rec([{"tool": "MultiEdit", "file": "src/config.py", "edit_index": 0, "old": "1", "new": "REROLL_COST = 10"},
                {"tool": "MultiEdit", "file": "src/config.py", "edit_index": 1, "old": "2", "new": "WAVE_SIZE = 12345"}])
    fr = freshness(rec, project)
    assert fr["status"] == "partly-stale" and fr["missing"]["src/config.py"] == ["WAVE_SIZE = 12345"]


def test_note_is_specific_and_only_when_relevant(tmp_path):
    project, store, _ = setup(tmp_path)
    write_session(store, "abcd0000-0000-0000-0000-000000000009", "2026-11-05T10:00:00Z", [
        turn("Make the reroll price of the shop offers 25 coins and add a menu sound.", "Done.", [
            {"tool": "Edit", "file": "src/config.py", "old": "REROLL_COST = 10", "new": "REROLL_COST = 25  # shop reroll"},
            {"tool": "Edit", "file": "src/audio.py", "old": "x", "new": "MENU_SOUND = 'click'  # menu"}])])
    # neither edit survives
    text, info = build_pack(project, store, "make the shop reroll price cheaper")
    entry = [s for s in info["sessions"] if s["session"].startswith("abcd0000")]
    assert entry and entry[0]["freshness"] == "stale"
    assert "no longer has this session's line `REROLL_COST = 25  # shop reroll`" in text
    assert "MENU_SOUND" not in text          # the audio change is irrelevant to this task: not mentioned
    _, info = build_pack(project, store, "make the shop reroll price cheaper", mode="nofresh")
    assert all(s["freshness"] is None for s in info["sessions"])


def test_redacted_edit_line_does_not_make_an_untouched_file_stale(tmp_path):
    from devmem.pack import freshness
    project = tmp_path
    (project / "app.py").write_text("def configure():\n    value = 1\n    return value\n", encoding="utf-8")
    s = {"session_id": "s", "tool_calls": [{"tool": "Write", "file": "app.py",
         "new": "def configure():\n    api_token = \"[REDACTED:assignment]\"\n    value = 1\n    return value\n"}],
         "files_edited": ["app.py"], "file_hashes_at_capture": {}}
    assert freshness(s, project)["status"] == "fresh"
