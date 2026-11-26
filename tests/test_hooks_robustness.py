"""Hook capture under hostile input and environments: bounded time on huge or pathological tool payloads, lone
UTF-16 surrogates, secret files named by any tool, an unwritable store, config switches written loosely,
sessions devmem records itself, and sessions finalised long after their last turn."""
from __future__ import annotations

import base64
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

from devmem import hooks
from devmem import sessions as S

from hooks_helpers import SESSION_ID, TURN1, load_payload, make_project, one_meta, replay

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


def _log(project: Path, sid: str = SESSION_ID) -> str:
    p = project / ".devmemory" / "live" / f"{sid}.jsonl"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _run(project: Path, event: str, payload: dict, monkeypatch) -> tuple[int, str]:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    return hooks.run([event], json.dumps(payload).encode("utf-8"))


# ---------------------------------------------------------------- finding 1: bounded redaction time

def test_hook_time_is_bounded_on_pathological_tool_payloads(project):
    """Each event runs as a real hook process; before the fix the SVG Write took 27 s and the 12 MB output 7 s."""
    rnd = random.Random(2)
    svg = ('<svg viewBox="0 0 24 24"><path d="M' + "".join(
        f"{rnd.randint(0, 99)}.{rnd.randint(0, 9)}-{rnd.randint(0, 9)}.{rnd.randint(0, 9)}l" for _ in range(1500))
        + '"/></svg>\n')
    hyphens = "-".join("ab" for _ in range(6500))
    cases = [
        ("Write", {"file_path": str(project / "icon.svg"), "content": svg}, {"type": "create"}),
        ("Write", {"file_path": str(project / "ids.txt"), "content": hyphens}, {"type": "create"}),
        ("Bash", {"command": "cat big.log"}, {"stdout": "-".join("ab" for _ in range(4_000_000)), "stderr": ""}),
        ("mcp__db__query", {"sql": "select blob"},
         [{"type": "text", "text": base64.b64encode(rnd.randbytes(750_000)).decode()}]),
    ]
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project))
    for i, (tool, inp, resp) in enumerate(cases):
        payload = dict(load_payload("008_PostToolUse", project), tool_name=tool, tool_input=inp,
                       tool_response=resp, tool_use_id=f"slow-{i}")
        t = time.perf_counter()
        p = subprocess.run([sys.executable, str(REPO / "plugin" / "devmem" / "hooks.py"), "PostToolUse"],
                           input=json.dumps(payload).encode("utf-8"), env=env, cwd=project, capture_output=True,
                           timeout=120)
        dt = time.perf_counter() - t
        assert p.returncode == 0 and dt < 5.0, (tool, round(dt, 2))
    log = _log(project)
    assert all(f'"slow-{i}"' in log for i in range(len(cases)))
    assert not (project / ".devmemory" / "hook_errors.log").exists()


# ---------------------------------------------------------------- finding 6: lone surrogates

def _lone(payload: dict) -> bytes:
    """JSON in which LONE becomes the escape of an unpaired high surrogate (output cut mid-emoji)."""
    return json.dumps(payload).replace("LONE", "\\ud83d").encode("ascii")


def test_lone_surrogate_keeps_the_event_and_the_turn(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    first = load_payload("002_UserPromptSubmit", project)
    assert hooks.run(["UserPromptSubmit"], json.dumps(first).encode())[0] == 0
    tool = dict(load_payload("016_PostToolUse", project), tool_input={"command": "pytest -q"},
                tool_response={"stdout": "12 passed LONE", "stderr": ""})
    second = dict(first, prompt="fix the LONE label", prompt_id="p-second")
    tool2 = dict(tool, tool_use_id="after-second", prompt_id="p-second")
    for ev, pl in (("PostToolUse", tool), ("UserPromptSubmit", second), ("PostToolUse", tool2)):
        assert hooks.run([ev], _lone(pl)) == (0, "")
    assert not (project / ".devmemory" / "hook_errors.log").exists()
    events = S.read_events(project / ".devmemory" / "live" / f"{SESSION_ID}.jsonl")
    assert [e["event"] for e in events].count("UserPromptSubmit") == 2
    assert [e["event"] for e in events].count("PostToolUse") == 2
    rec = S.build_from_events(events, project, hash_now=False)
    assert rec["turns"][1]["prompt"] == "fix the � label"           # rest of the text intact
    assert rec["turns"][1]["tool_calls"][0]["command"] == "pytest -q"     # attributed to the right turn


def test_lone_surrogate_in_transcript_text_is_kept(project, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    replay(project, ["000_SessionStart", "002_UserPromptSubmit"], transcript, monkeypatch, hooks)
    line = json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
                       "message": {"id": "m1", "content": [{"type": "text", "text": "done LONE"}]}})
    transcript.write_text(line.replace("LONE", "\\ud83d") + "\n", encoding="utf-8")
    stop = load_payload("022_Stop", project, transcript)
    assert hooks.run(["Stop"], _lone(stop)) == (0, "")
    assert not (project / ".devmemory" / "hook_errors.log").exists()
    assert one_meta(project / ".devmemory")["turns"][0]["result_text"] is not None


def test_atomic_write_never_raises_on_a_surrogate(tmp_path):
    S._atomic_write(tmp_path / "x.json", "a\ud83db")
    assert (tmp_path / "x.json").read_text(encoding="utf-8") == "a�b"


# ---------------------------------------------------------------- finding 4: secret files named by any tool

def test_secret_file_named_by_any_tool_never_reaches_disk(project, monkeypatch):
    (project / ".env").write_text("DB_PASS=hunter2hunter2\nSTRIPE_LIVE=plainvalue12345\n", encoding="utf-8")
    base = load_payload("004_PostToolUse", project)
    env_text = "DB_PASS=hunter2hunter2\nSTRIPE_LIVE=plainvalue12345"
    calls = [
        ("Grep", {"pattern": "=", "path": str(project / ".env"), "output_mode": "content"},
         {"mode": "content", "content": env_text, "numLines": 2}),
        ("mcp__filesystem__read_text_file", {"path": str(project / ".env")}, [{"type": "text", "text": env_text}]),
        ("mcp__filesystem__write_file", {"path": ".env", "content": env_text}, [{"type": "text", "text": "ok"}]),
        ("Bash", {"command": "grep -r PASS ."}, {"stdout": f"./.env:{env_text.splitlines()[1]}\n./a.py:1:x",
                                                 "stderr": ""}),
        ("Grep", {"pattern": "STRIPE", "path": str(project), "output_mode": "content"},
         {"mode": "content", "content": f".env:2:{env_text.splitlines()[1]}", "numLines": 1}),
    ]
    for i, (tool, inp, resp) in enumerate(calls):
        pl = dict(base, tool_name=tool, tool_input=inp, tool_response=resp, tool_use_id=f"s{i}")
        assert _run(project, "PostToolUse", pl, monkeypatch) == (0, "")
    log = _log(project)
    assert "hunter2hunter2" not in log and "plainvalue12345" not in log
    events = [e for e in S.read_events(project / ".devmemory" / "live" / f"{SESSION_ID}.jsonl")
              if e.get("event") == "PostToolUse"]
    assert len(events) == len(calls)                                   # each call is still recorded
    assert all(e["calls"][0].get("secret_file") for e in events[:3])
    assert "./a.py:1:x" in events[3]["output"]                         # other files' lines are kept


def test_ordinary_tool_paths_are_not_treated_as_secret(project, monkeypatch):
    pl = dict(load_payload("004_PostToolUse", project), tool_name="mcp__filesystem__read_text_file",
              tool_input={"path": str(project / "greet.py")}, tool_response=[{"type": "text", "text": "def greet"}])
    _run(project, "PostToolUse", pl, monkeypatch)
    ev = S.read_events(project / ".devmemory" / "live" / f"{SESSION_ID}.jsonl")[-1]
    assert "def greet" in ev["output"] and not ev["calls"][0].get("secret_file")


# ---------------------------------------------------------------- finding 7: unwritable store

def test_lock_gives_up_at_once_when_the_directory_cannot_be_written(tmp_path, monkeypatch):
    lock = tmp_path / "live" / "s.lock"
    real_open = os.open

    def deny(path, *a, **k):
        if str(path) == str(lock):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *a, **k)
    monkeypatch.setattr(hooks.os, "open", deny)
    t = time.perf_counter()
    with hooks._Lock(lock) as lk:
        assert not lk.held and lk.cannot_write
    assert time.perf_counter() - t < 0.5            # was LOCK_TIMEOUT_S (3 s) on every event


@pytest.mark.skipif(os.name != "nt", reason="delete-pending lock files exist only on Windows")
def test_lock_still_waits_while_a_held_lock_file_exists(tmp_path, monkeypatch):
    lock = tmp_path / "live" / "s.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")   # held (or being deleted) by a hook that is still running
    real_open = os.open

    def pending(path, *a, **k):                     # Windows reports a delete-pending file as access denied
        if str(path) == str(lock):
            raise PermissionError(13, "Access is denied", str(path))
        return real_open(path, *a, **k)
    monkeypatch.setattr(hooks.os, "open", pending)
    monkeypatch.setattr(hooks, "LOCK_TIMEOUT_S", 0.3)
    t = time.perf_counter()
    with hooks._Lock(lock) as lk:
        assert not lk.held and not lk.cannot_write
    assert time.perf_counter() - t >= 0.25           # contention: retried until the deadline


def test_error_log_falls_back_to_temp_when_the_store_log_cannot_be_written(tmp_path, monkeypatch):
    store = tmp_path / ".devmemory"
    (store / "hook_errors.log").mkdir(parents=True)   # opening it for append fails
    monkeypatch.setattr(hooks, "fallback_error_log", lambda: tmp_path / "fallback.log")
    hooks._log_error(store, "PostToolUse", PermissionError(13, "Permission denied", str(store / "live")))
    text = (tmp_path / "fallback.log").read_text(encoding="utf-8")
    assert "PermissionError" in text and str(store) in text


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions as a non-root user")
def test_read_only_store_costs_no_wait_and_is_logged(project, tmp_path, monkeypatch):
    store = project / ".devmemory"
    (store / "live").mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", None)
    os.chmod(store / "live", 0o555)
    os.chmod(store, 0o555)
    try:
        for name in ("004_PostToolUse", "002_UserPromptSubmit", "022_Stop"):
            t = time.perf_counter()
            assert _run(project, name.split("_", 1)[1], load_payload(name, project), monkeypatch) == (0, "")
            assert time.perf_counter() - t < 1.0, name
    finally:
        os.chmod(store, 0o755)
        os.chmod(store / "live", 0o755)
    text = hooks.fallback_error_log().read_text(encoding="utf-8")
    assert text.count("PermissionError") >= 3


# ---------------------------------------------------------------- finding 11: config switches

@pytest.mark.parametrize("value,expected", [
    (False, False), (0, False), ("false", False), ("False", False), ("off", False), ("no", False), ("0", False),
    (True, True), (1, True), ("true", True), ("on", True), ("yes", True), (None, True), ("maybe", True)])
def test_config_bool(value, expected):
    assert S.config_bool(value, True) is expected


@pytest.mark.parametrize("off", [0, "false", "off", "no"])
def test_capture_switch_written_loosely_still_turns_capture_off(project, monkeypatch, off):
    (project / ".devmemory" / "config.json").write_text(json.dumps({"capture": off, "deliver": "no"}),
                                                       encoding="utf-8")
    for name in ("000_SessionStart", "002_UserPromptSubmit", "008_PostToolUse", "022_Stop"):
        assert _run(project, name.split("_", 1)[1], load_payload(name, project), monkeypatch) == (0, "")
    assert not (project / ".devmemory" / "live").exists()
    assert not (project / ".devmemory" / "sessions").exists()


def _fake_pack(monkeypatch):
    import devmem.pack as pack
    monkeypatch.setattr(pack, "build_pack",
                        lambda *a, **k: ("PACK\n", {"files": ["greet.py"], "sessions": [], "keys": ["file:greet.py"]}))
    import devmem.scan as scan
    monkeypatch.setattr(scan, "scan", lambda *a, **k: {})
    import devmem.refresh as refresh
    monkeypatch.setattr(refresh, "refresh", lambda *a, **k: {"state": "fresh"})


def test_capture_off_keeps_only_the_delivery_state(project, monkeypatch):
    """With capture off and delivery on, the only file written is live/<sid>.state.json: what context was
    sent on which turn (so it is not sent twice). It holds no prompt or tool text."""
    (project / ".devmemory" / "config.json").write_text('{"capture": "off"}', encoding="utf-8")
    _fake_pack(monkeypatch)
    pl = dict(load_payload("002_UserPromptSubmit", project), prompt="a distinctive prompt zebra")
    assert _run(project, "UserPromptSubmit", pl, monkeypatch) == (0, "PACK\n")
    files = sorted(p.relative_to(project / ".devmemory").as_posix()
                   for p in (project / ".devmemory").rglob("*") if p.is_file())
    assert files == ["config.json", f"live/{SESSION_ID}.state.json"]
    assert "zebra" not in (project / ".devmemory" / "live" / f"{SESSION_ID}.state.json").read_text(encoding="utf-8")
    assert _run(project, "SessionEnd", load_payload("024_SessionEnd", project), monkeypatch) == (0, "")
    assert not (project / ".devmemory" / "live" / f"{SESSION_ID}.state.json").exists()  # tidied at the end


# ---------------------------------------------------------------- finding 8: sessions devmem records itself

def test_no_capture_env_delivers_but_records_nothing(project, monkeypatch):
    (project / ".devmemory" / "config.json").unlink()
    _fake_pack(monkeypatch)
    monkeypatch.setenv(hooks.NO_CAPTURE_ENV, "1")
    out = _run(project, "UserPromptSubmit", load_payload("002_UserPromptSubmit", project), monkeypatch)
    assert out == (0, "PACK\n")
    for name in ("000_SessionStart", "008_PostToolUse", "022_Stop", "024_SessionEnd"):
        assert _run(project, name.split("_", 1)[1], load_payload(name, project), monkeypatch) == (0, "")
    assert not _log(project) and not (project / ".devmemory" / "sessions").exists()


# ---------------------------------------------------------------- finding 9 and 10

def test_abandoned_session_keeps_hashes_from_its_last_stop(project, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    replay(project, TURN1[:-1], transcript, monkeypatch, hooks)   # no SessionEnd: the terminal was closed
    store = project / ".devmemory"
    hashes = one_meta(store)["file_hashes_at_capture"]
    assert hashes.get("greet.py")
    (project / "greet.py").write_text("def greet(name):\n    return 'changed later'\n", encoding="utf-8")
    old = time.time() - hooks.ABANDONED_AFTER_S - 60
    os.utime(store / "live" / f"{SESSION_ID}.jsonl", (old, old))
    other = dict(load_payload("000_SessionStart", project), session_id="another-session-0000")
    assert _run(project, "SessionStart", other, monkeypatch) == (0, "")
    from devmem.sessions import id_tag
    rec = json.loads(next((store / "sessions").glob(f"*_{id_tag(SESSION_ID)}.meta.json")).read_text(encoding="utf-8"))
    assert rec["ended_reason"].startswith("no SessionEnd") and rec["file_hashes_at_capture"] == hashes


def test_hook_turns_record_that_the_user_wrote_the_prompt(project, tmp_path, monkeypatch):
    replay(project, TURN1, tmp_path / "t.jsonl", monkeypatch, hooks)
    rec = one_meta(project / ".devmemory")
    assert rec["turns"][0]["prompt_source"] == "user" and rec["prompt_source"] == "user"


# ---- round 2: more routes to a secret file's content (git show REV:path, grep context, git log -p) ----

def test_secret_file_reached_through_git_or_grep_context_never_reaches_disk(project, monkeypatch):
    key = "0f9a8b7c6d5e4f3a2b1c"
    base = load_payload("016_PostToolUse", project)
    log_p = (f"commit abc\nAuthor: me\n\n    rotate\n\ndiff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n"
             f"@@ -1 +1 @@\n-OPENWEATHER={key}\n+OPENWEATHER=x\ndiff --git a/app.py b/app.py\n@@ -1 +1 @@\n+x = 2")
    calls = [
        ("Bash", {"command": "git show HEAD:.env"}, {"stdout": f"OPENWEATHER={key}\nDB_HOST=prod", "stderr": ""}),
        ("Bash", {"command": "grep -rn -C1 DB_HOST ."},
         {"stdout": f"./.env-1-OPENWEATHER={key}\n./.env:2:DB_HOST=prod\n./app.py:3:host = DB_HOST", "stderr": ""}),
        ("Bash", {"command": "git log -p -3"}, {"stdout": log_p, "stderr": ""}),
        ("mcp__git__git_diff", {"repo_path": str(project)}, [{"type": "text", "text": log_p}]),
        ("PowerShell", {"command": "Get-Content .env | Select-String OPEN"}, {"stdout": f"OPENWEATHER={key}"}),
    ]
    for i, (tool, inp, resp) in enumerate(calls):
        pl = dict(base, tool_name=tool, tool_input=inp, tool_response=resp, tool_use_id=f"r2-{i}")
        assert _run(project, "PostToolUse", pl, monkeypatch) == (0, "")
    failed = dict(base, hook_event_name="PostToolUseFailure", tool_name="Bash", tool_use_id="r2-f",
                  tool_input={"command": "grep -rn OPEN . && false"}, error=f"Exit 1\n./.env:1:OPENWEATHER={key}")
    assert _run(project, "PostToolUseFailure", failed, monkeypatch) == (0, "")
    log = _log(project)
    assert key not in log
    assert "./app.py:3:host = DB_HOST" in log and "+x = 2" in log   # output of other files is kept


def test_dotted_token_attributes_keep_their_text(project, monkeypatch):
    """Round-2 review: `token.markup = "linkify"` was redacted, so the record lost the edit and looked stale."""
    new = 'def rule(state):\n    token = state.push("link_open", "a", 1)\n    token.markup = "linkify"\n'
    pl = dict(load_payload("010_PostToolUse", project), tool_input={"file_path": str(project / "linkify.py"),
                                                                     "content": new})
    assert _run(project, "PostToolUse", pl, monkeypatch) == (0, "")
    ev = S.read_events(project / ".devmemory" / "live" / f"{SESSION_ID}.jsonl")[-1]
    assert ev["calls"][0]["new"] == new


def test_session_start_rewrites_an_old_raw_begin(project, monkeypatch):
    key = "sk-proj-" + "A1b2C3d4E5f6G7h8" * 2
    pend = project / ".devmemory" / "pending"
    pend.mkdir()
    (pend / "old.json").write_text(json.dumps({"id": "old", "started_at": "2026-01-01T00:00:00Z", "kind": "git",
                                               "head": None, "dirty": {"settings.py": f'KEY = "{key}"\n'}}),
                                   encoding="utf-8")
    assert _run(project, "SessionStart", load_payload("000_SessionStart", project), monkeypatch) == (0, "")
    text = (pend / "old.json").read_text(encoding="utf-8")
    assert key not in text and json.loads(text)["v"] == 2


def test_error_log_applies_the_approved_user_patterns(tmp_path):
    from devmem.consent import enable_init
    project = tmp_path / "p"
    store = project / ".devmemory"
    store.mkdir(parents=True)
    (store / "redact").write_text("ACME-[0-9]{6}\n", encoding="utf-8")
    enable_init(project)                           # the user approved this redact file
    hooks._log_error(store, "UserPromptSubmit", ValueError("session id 'ACME-123456' refused"))
    text = (store / "hook_errors.log").read_text(encoding="utf-8")
    assert "ValueError" in text and "ACME-123456" not in text and "[REDACTED:custom]" in text
