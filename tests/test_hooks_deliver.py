"""UserPromptSubmit delivers the context pack: UTF-8 on any console code page, size cap, delivery state."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from devmem import hooks

from hooks_helpers import SESSION_ID, load_payload, make_project

REPO = Path(__file__).resolve().parent.parent


def _project(tmp_path):
    project = make_project(tmp_path)
    (project / ".devmemory" / "config.json").unlink()  # delivery on (default)
    (project / "damage.py").write_text(
        "def apply_armor(damage, armor):\n    # damage − armor, floored at zero\n"
        "    return max(0, damage - armor)\n", encoding="utf-8")
    return project


def test_pack_printed_as_utf8_on_cp1252_pipe(tmp_path):
    """Regression (design doc problem 1): U+2212 in the pack crashed the hook on a cp1252 console/pipe."""
    project = _project(tmp_path)
    payload = dict(load_payload("002_UserPromptSubmit", project), prompt="change how damage armor is applied")
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project), "PYTHONIOENCODING": "cp1252"}
    env.pop("PYTHONUTF8", None)
    p = subprocess.run([sys.executable, str(REPO / "plugin" / "devmem" / "hooks.py"), "UserPromptSubmit"],
                       input=json.dumps(payload).encode("utf-8"), capture_output=True, env=env, cwd=project,
                       timeout=120)
    assert p.returncode == 0
    err_log = project / ".devmemory" / "hook_errors.log"
    assert not err_log.exists(), err_log.read_text(encoding="utf-8")
    out = p.stdout.decode("utf-8")
    assert "DevMemory context pack" in out and "damage.py" in out and "−" in out
    state = json.loads((project / ".devmemory" / "live" / f"{SESSION_ID}.state.json").read_text(encoding="utf-8"))
    assert state["delivered"][0]["files"] and state["delivered"][0]["prompt_id"] == payload["prompt_id"]
    assert state["scanned"] is True


def test_pack_capped_below_hook_output_limit(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    import devmem.pack as pack
    monkeypatch.setattr(pack, "build_pack", lambda *a, **k: ("line\n" * 5000, {"files": [], "sessions": []}))
    code, out = hooks.run(["UserPromptSubmit"], json.dumps(load_payload("002_UserPromptSubmit", project)).encode())
    assert code == 0 and len(out) < 10_000 and out.endswith("hook output limit]\n")


def test_session_id_passed_when_build_pack_accepts_it(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    seen = {}

    def fake(project, store, task, budget=1500, mode="full", session_id=None, delivered=None):
        seen.update(session_id=session_id, delivered=delivered)
        return "pack\n", {"files": ["a.py"], "sessions": []}
    import devmem.pack as pack
    monkeypatch.setattr(pack, "build_pack", fake)
    for _ in range(2):
        hooks.run(["UserPromptSubmit"], json.dumps(load_payload("002_UserPromptSubmit", project)).encode())
    assert seen["session_id"] == SESSION_ID and seen["delivered"][0]["files"] == ["a.py"]


def test_pack_failure_still_records_the_turn(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    import devmem.pack as pack

    def boom(*a, **k):
        raise RuntimeError("pack broke")
    monkeypatch.setattr(pack, "build_pack", boom)
    assert hooks.run(["UserPromptSubmit"], json.dumps(load_payload("002_UserPromptSubmit", project)).encode()) == (0, "")
    assert "pack broke" in (project / ".devmemory" / "hook_errors.log").read_text(encoding="utf-8")
    log = (project / ".devmemory" / "live" / f"{SESSION_ID}.jsonl").read_text(encoding="utf-8")
    assert '"UserPromptSubmit"' in log


def _run_with_fake_pack(project, monkeypatch, events):
    """Run hook events with a build_pack that records what it was told had been delivered."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    seen = []

    def fake(project, store, task, budget=1500, mode="full", session_id=None, delivered=None):
        seen.append(list(delivered or []))
        return "pack\n", {"files": ["a.py"], "keys": ["file:a.py"], "sessions": []}
    import devmem.pack as pack
    monkeypatch.setattr(pack, "build_pack", fake)
    for name, extra in events:
        base = "002_UserPromptSubmit" if name == "UserPromptSubmit" else "000_SessionStart"
        payload = dict(load_payload(base, project), hook_event_name=name, **extra)
        hooks.run([name], json.dumps(payload).encode())
    return seen


def test_delivered_state_resets_after_postcompact(tmp_path, monkeypatch):
    project = _project(tmp_path)
    seen = _run_with_fake_pack(project, monkeypatch, [
        ("UserPromptSubmit", {}), ("UserPromptSubmit", {}),
        ("PostCompact", {"trigger": "auto", "compact_summary": "summary"}),
        ("UserPromptSubmit", {})])
    assert [len(x) for x in seen] == [0, 1, 0]          # after the compaction nothing counts as delivered
    st = json.loads((project / ".devmemory" / "live" / f"{SESSION_ID}.state.json").read_text(encoding="utf-8"))
    assert len(st["delivered"]) == 3                       # history is kept, not erased
    assert [bool(d.get("cleared_at")) for d in st["delivered"]] == [True, True, False]
    assert st["delivered"][0]["cleared_by"] == "PostCompact"


def test_delivered_state_resets_on_session_start_compact_and_clear_but_not_resume(tmp_path, monkeypatch):
    for n, (source, expect) in enumerate((("compact", 0), ("clear", 0), ("resume", 1), ("startup", 1))):
        # a one-character folder per round: the store's live/ files need ~76 characters below the project, and with
        # Windows long paths off a longer name here made this the first test to fail under a long --basetemp
        project = _project(tmp_path / str(n))
        seen = _run_with_fake_pack(project, monkeypatch, [
            ("UserPromptSubmit", {}), ("SessionStart", {"source": source}), ("UserPromptSubmit", {})])
        assert len(seen[-1]) == expect, source
