"""The requests-only pack (`pack_mode: "requests"`, `devmem pack --mode requests`): past sessions and earlier
requirements only. No relevant files, no conventions, no freshness notes; the sessions are chosen exactly as in the
full pack (the gate experiment's arm D, evals/gate/PREREGISTRATION.md)."""
from __future__ import annotations

import json

import pytest
from cli_helpers import run_cli
from hooks_helpers import make_project as make_hook_project
from pack_helpers import setup, turn, write_session

from devmem import hooks
from devmem.pack import PACK_MODES, build_pack

TASK = "make the shop reroll price cheaper"


def test_requests_mode_shows_sessions_and_requirements_but_no_files_or_conventions(tmp_path):
    project, store, _ = setup(tmp_path)
    full, finfo = build_pack(project, store, TASK)
    assert "## Relevant files" in full and finfo["files"]          # the comparison means something
    text, info = build_pack(project, store, TASK, mode="requests")
    assert "## Past sessions on this topic" in text
    assert "## Relevant files" not in text and "## Conventions" not in text
    assert info["files"] == [] and not [k for k in info["keys"] if k.startswith("file:") or k == "conventions"]
    assert info["mode"] == "requests"
    # the same sessions and requirements as the full pack
    assert [s["session"] for s in info["sessions"]] == [s["session"] for s in finfo["sessions"]]
    assert info["decisions"] == finfo["decisions"]
    assert "Rerolling the offers costs 10 coins." in text
    assert "from past sessions." in text and "project index" not in text


def test_requests_mode_has_no_freshness_note(tmp_path):
    project, store, _ = setup(tmp_path)
    write_session(store, "abcd0000-0000-0000-0000-000000000009", "2026-11-05T10:00:00Z", [
        turn("Make the reroll price of the shop offers 25 coins.", "Done.", [
            {"tool": "Edit", "file": "src/config.py", "old": "REROLL_COST = 10", "new": "REROLL_COST = 25  # shop reroll"}])])
    full, _ = build_pack(project, store, TASK)
    assert "Changed since (checked now)" in full                   # the edit is gone from the file
    text, info = build_pack(project, store, TASK, mode="requests")
    assert "Changed since" not in text and info["stale"] == 0
    assert all(s["freshness"] is None for s in info["sessions"])
    assert any(s["session"].startswith("abcd0000") for s in info["sessions"])


def test_only_file_evidence_gives_an_empty_requests_pack(tmp_path):
    project, store, _ = setup(tmp_path, with_sessions=False)
    full, _ = build_pack(project, store, "reroll_offers in the shop")
    assert "## Relevant files" in full
    text, info = build_pack(project, store, "reroll_offers in the shop", mode="requests")
    assert text == "" and info["empty_reason"]


def test_a_refused_store_gives_an_empty_requests_pack_without_scanning(tmp_path, monkeypatch):
    project, store, _ = setup(tmp_path)
    monkeypatch.setattr("devmem.consent.recorded_refusal", lambda p, s: "not this user's store (test)")
    monkeypatch.setattr("devmem.pack._project_only_pack", lambda *a, **k: pytest.fail("files-only pack was built"))
    text, info = build_pack(project, store, TASK, mode="requests")
    assert text == "" and info["sessions_refused"] == "not this user's store (test)"


def test_cli_mode_requests(tmp_path):
    project, _store, _ = setup(tmp_path)
    r = run_cli(["pack", "--mode", "requests", TASK], cwd=project)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-400:]
    out = r.stdout.decode("utf-8")
    assert "## Past sessions on this topic" in out
    assert "## Relevant files" not in out and "## Conventions" not in out
    assert json.loads(r.stderr.decode("utf-8").strip().splitlines()[-1])["mode"] == "requests"


def _send(project, event, sid, **kw):
    p = {"session_id": sid, "cwd": str(project), "hook_event_name": event,
         "transcript_path": str(project / "none.jsonl"), **kw}
    return hooks.run([event], json.dumps(p).encode("utf-8"))


@pytest.mark.parametrize("mode, files_shown", [("requests", False), ("full", True), (["requests"], True)])
def test_the_hook_reads_pack_mode_from_the_config(tmp_path, monkeypatch, mode, files_shown):
    project = make_hook_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    (project / "parser.py").write_text("def parse_ledger(x):\n    return round(x, 2)\n", encoding="utf-8")
    sid = "earlier-0001"
    _send(project, "SessionStart", sid, source="startup")
    _send(project, "UserPromptSubmit", sid, prompt="fix parse_ledger rounding in parser.py: always 2 decimals",
          prompt_id="a")
    _send(project, "Stop", sid, last_assistant_message="parse_ledger now rounds to 2 decimals", prompt_id="a")
    _send(project, "SessionEnd", sid, reason="exit")
    (project / ".devmemory" / "config.json").write_text(json.dumps({"pack_mode": mode}), encoding="utf-8")
    _send(project, "SessionStart", "later-0002", source="startup")
    code, out = _send(project, "UserPromptSubmit", "later-0002", prompt="parse_ledger rounding is wrong again",
                      prompt_id="b")
    assert code == 0 and "## Past sessions on this topic" in out
    assert ("## Relevant files" in out) is files_shown         # a mode of the wrong type falls back to full


def test_modes_list():
    assert PACK_MODES == ("full", "static", "nofresh", "requests")
