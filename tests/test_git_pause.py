"""While git cannot say which files of the store it tracks, the hooks serve nothing from the store, say so in words
that fit (not "run `devmem adopt`"), and keep recording the session when git's last answer for the store was clean.

A git command over its limit used to pause recording and packs for about a minute with no notice mid-session, and the
SessionStart notice asked for `devmem adopt`, which does not help with a git problem (reviewer's v8a/a/t3_honest_slow.py
`ss` and `mid`, t11_unsafe.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from cli_helpers import write_session
from hooks_helpers import make_project

from devmem import consent, gitindex, hooks, store
from devmem.sessions import load_sessions

MARK = "ZQXPAUSEMARK"
SID = "11111111-2222-3333-4444-555555555555"
REAL_GIT_RUN = store.git_run
REAL_READER = gitindex.index_candidates


@pytest.fixture
def project(tmp_path, monkeypatch):
    p = make_project(tmp_path)
    (p / ".devmemory" / "config.json").write_text("{}", encoding="utf-8")      # packs on
    write_session(p / ".devmemory", "abcdef12-0000", f"fix the wallet {MARK} label", files_edited=["greet.py"])
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(p))
    _event()
    assert consent.store_refusal(p, p / ".devmemory") is None                # git's answer: nothing tracked
    return p


def _event() -> None:
    """A new hook process: no answers kept in memory."""
    store._FILTERS.clear()
    store._git_down_until = 0.0
    consent.forget_decisions()
    consent._FRESH_FAILURES.clear()


def _hook(project: Path, event: str, **payload) -> str:
    _event()
    code, out = hooks.run([event], json.dumps({"session_id": SID, "cwd": str(project), "hook_event_name": event,
                                               **payload}).encode("utf-8"))
    assert code == 0
    return out


def _git_cannot_say(monkeypatch, project: Path, failure=None) -> None:
    """The index changes (a commit), and from then on git cannot answer the question, nor can the index be read."""
    (project / "other.py").write_text("y = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "other.py"], check=True, capture_output=True)
    real = store.git_run

    def broken(where, *args, timeout=store.GIT_TIMEOUT, **kw):
        if "ls-files" in args and any(str(a).startswith(":(top") for a in args):
            if failure is not None:
                return failure
            raise subprocess.TimeoutExpired(["git"], timeout)
        return real(where, *args, timeout=timeout, **kw)
    monkeypatch.setattr(store, "git_run", broken)

    def unreadable(*a, **k):
        raise gitindex.IndexUnknown("not read in this test")
    monkeypatch.setattr(gitindex, "index_candidates", unreadable)


def _notice(out: str) -> str:
    return json.loads(out)["systemMessage"] if out.strip() else ""


def test_recording_goes_on_and_nothing_is_served_while_git_cannot_say(project, monkeypatch):
    st = project / ".devmemory"
    _git_cannot_say(monkeypatch, project)
    msg = _notice(_hook(project, "SessionStart", source="startup"))
    assert "cannot tell right now" in msg and "still recorded" in msg and "within a minute" in msg, msg
    assert "devmem adopt" not in msg
    out = _hook(project, "UserPromptSubmit", prompt=f"fix the wallet label again {MARK}")
    assert MARK not in out and "wallet" not in out                         # no pack, and no second notice
    assert _hook(project, "Stop", last_assistant_message="done") == ""
    _hook(project, "SessionEnd", reason="other")
    recorded = [json.loads(p.read_text(encoding="utf-8")) for p in (st / "sessions").glob("*.meta.json")]
    assert any(r.get("session_id") == SID and "again" in (r.get("prompt") or "") for r in recorded)
    _event()
    assert load_sessions(st, project=project) == []                        # nothing served meanwhile
    monkeypatch.setattr(store, "git_run", REAL_GIT_RUN)                    # git answers again (not undo(): that
    monkeypatch.setattr(gitindex, "index_candidates", REAL_READER)         # would also undo the test's user folder)
    d = json.loads(consent._checked_path(st).read_text(encoding="utf-8"))
    d["failed"]["at"] -= consent.FAILURE_SECONDS + 1
    consent._checked_path(st).write_text(json.dumps(d), encoding="utf-8")
    _event()
    assert consent.store_refusal(project, st, check_git="cached") is None
    assert {s.get("session_id") for s in load_sessions(st, project=project)} >= {SID}


def test_mid_session_the_prompt_that_found_it_says_so(project, monkeypatch):
    _hook(project, "SessionStart", source="startup")
    _git_cannot_say(monkeypatch, project)
    msg = _notice(_hook(project, "UserPromptSubmit", prompt="next request"))
    assert "cannot tell right now" in msg and "still recorded" in msg, msg
    assert _hook(project, "UserPromptSubmit", prompt="and another") == ""      # once per failure, not every prompt


def test_a_store_whose_last_answer_was_not_clean_records_nothing(project, monkeypatch):
    st = project / ".devmemory"
    rec = st / "sessions" / "20261102T1000_bbbbbbbb.meta.json"
    rec.write_text('{"session_id": "bbbbbbbb", "prompt": "pulled"}', encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "-f", str(rec)], check=True, capture_output=True)
    _event()
    assert "tracked by git" in (consent.store_refusal(project, st) or "")
    _git_cannot_say(monkeypatch, project)
    msg = _notice(_hook(project, "SessionStart", source="startup"))
    assert "cannot tell right now" in msg and "records nothing" in msg, msg
    _hook(project, "UserPromptSubmit", prompt="not recorded")
    assert not (st / "live" / f"{SID}.jsonl").exists()


def test_other_sessions_files_are_not_touched_while_recording_without_an_answer(project, monkeypatch):
    st = project / ".devmemory"
    live = st / "live"
    live.mkdir(exist_ok=True)
    old = live / "99999999-0000-0000-0000-000000000000.jsonl"
    old.write_text('{"event": "UserPromptSubmit", "prompt": "abandoned, maybe pulled"}\n', encoding="utf-8")
    t = time.time() - hooks.ABANDONED_AFTER_S - 3600
    os.utime(old, (t, t))
    _git_cannot_say(monkeypatch, project)
    _hook(project, "SessionStart", source="startup")
    assert old.exists()                                                    # not finalised into a record


def test_dubious_ownership_names_gits_reason_and_the_fix(project, monkeypatch):
    failure = subprocess.CompletedProcess(["git"], 128, b"", b"fatal: detected dubious ownership in repository at "
                                          b"'/srv/shared/repo'\n")
    _git_cannot_say(monkeypatch, project, failure)
    msg = _notice(_hook(project, "SessionStart", source="startup"))
    assert "dubious ownership" in msg and "git config --global --add safe.directory /srv/shared/repo" in msg, msg
    why = consent.store_refusal(project, project / ".devmemory")
    assert why and "safe.directory" in why


def test_an_index_that_is_not_a_regular_file_stops_recording_too(project):
    """A named pipe (here a folder) where git's index belongs is a shape a repository was given, not a git that was
    slow once: recording would run git against it (reviewer's hostile1 gitdirfifo, fifo1)."""
    idx = project / ".git" / "index"
    idx.unlink()
    idx.mkdir()
    msg = _notice(_hook(project, "SessionStart", source="startup"))
    assert "records nothing" in msg, msg
    _hook(project, "UserPromptSubmit", prompt="not recorded")
    assert not (project / ".devmemory" / "live" / f"{SID}.jsonl").exists()
