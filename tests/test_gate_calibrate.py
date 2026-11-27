"""The gate's calibration sessions (evals/gate/calibrate.py) with the stand-in claude (evals/gate/fake_claude.py): no
model, no network. Checks the planned session count, the --confirm refusal, that the run writes nothing outside its
root, what each check records, and that the sign-in token reaches no saved file."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.gate import calibrate as C

SECRET = "tok-calibration-made-up-5e1d"


def _snapshot(top: Path) -> dict[str, tuple]:
    out = {}
    for p in top.rglob("*"):
        if p.is_file():
            st = p.stat()
            out[p.relative_to(top).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


@pytest.fixture()
def calib(tmp_path, monkeypatch):
    outer_home, outer_tmp = tmp_path / "outer-home", tmp_path / "outer-tmp"
    outer_home.mkdir()
    outer_tmp.mkdir()
    for k, v in (("HOME", outer_home), ("USERPROFILE", outer_home), ("TEMP", outer_tmp), ("TMP", outer_tmp),
                 ("TMPDIR", outer_tmp)):
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-never-reach-a-session")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    root = tmp_path / "root"
    root.mkdir()
    (root / "oauth-token").write_text(SECRET + "\n", encoding="utf-8")
    return {"root": root, "home": outer_home, "tmp": outer_tmp}


def _main(calib, *args) -> int:
    return C.main(["--fake", "--root", str(calib["root"]), *args])


def test_plan_count_and_confirm_refusal(calib, capsys):
    assert len(C.SESSIONS) <= 12                     # "about a dozen" calibration sessions (PLAN.md operating rules)
    assert _main(calib, "--confirm", str(len(C.SESSIONS) - 1)) == 2
    out = capsys.readouterr().out
    assert f"{len(C.SESSIONS)} sessions planned" in out and f"--confirm {len(C.SESSIONS)} is needed" in out
    assert SECRET not in out
    assert _main(calib, "--only", "a,b", "--confirm", "6") == 2
    assert "2 sessions planned" in capsys.readouterr().out
    # b continues a's configuration: alone, with no saved state, it cannot start whatever --confirm says
    assert _main(calib, "--only", "b", "--confirm", "1") == 2
    assert "b needs state 'mem'" in capsys.readouterr().out
    assert not (calib["root"] / "calib").exists()


def test_a_root_inside_the_repository_is_refused(capsys):
    assert C.main(["--fake", "--root", str(Path(C.R.REPO_ROOT) / "evals"), "--confirm", "6"]) == 2
    assert "inside this repository" in capsys.readouterr().out


def test_fake_calibration_records_each_check_and_writes_only_under_its_root(calib, tmp_path):
    before = _snapshot(tmp_path)
    assert _main(calib, "--confirm", str(len(C.SESSIONS))) == 0
    changed = {k for k, v in _snapshot(tmp_path).items() if before.get(k) != v}
    assert changed and all(k.startswith("root/calib/") for k in changed), \
        sorted(k for k in changed if not k.startswith("root/calib/"))
    assert not (calib["home"] / ".fake-claude-home").exists() and not (calib["tmp"] / "fake-claude.tmp").exists()
    calib_dir = calib["root"] / "calib"
    assert not (calib_dir / "current").exists()
    summary = json.loads((calib_dir / "results" / "summary.json").read_text(encoding="utf-8"))
    assert sorted(summary) == [s["id"] for s in C.SESSIONS]
    a, b, c, d, e, f = (summary[k] for k in "abcdef")
    for r in summary.values():
        assert r["exit_code"] == 0 and r["harness_failure"] is None and r["auth_problem"] is None and r["fake"]
        assert r["claude_version"] == C.R.RUNNER["claude_version"] and r["model"] == C.R.RUNNER["model"]
        assert r["project_path"] == str((calib_dir / "current" / "work").resolve())     # one fixed path
        assert {"UserPromptSubmit", "PostToolUse"} <= set(r["hook_lines"])               # the project's hook fired
        assert r["uncached_input_tokens"] == 100 and r["output_tokens"] == 30
        assert r["token_found_in"] == []
        assert (calib_dir / "results" / r["id"] / "stream.jsonl").is_file()
    # a: a memory folder with the session's note; its copy is in the results
    assert a["memory_files"] == ["MEMORY.md"] and C.MARKER in a["memory_folders"][0]["files"][0]["text"]
    assert (calib_dir / "results" / "a" / "memory" / "fake" / "MEMORY.md").is_file()
    # b: a's configuration came back (fake_claude repeats its memory in the answer); c: a fresh one did not
    assert b["restored_state"] and b["marker_in_diff"] and b["marker_in_reply"]
    assert not c["restored_state"] and not c["marker_in_diff"] and not c["marker_in_reply"]
    # d: memory switched off, no memory folder
    assert d["auto_memory_env"] == "1" and d["memory_folders"] == [] and d["memory_files"] == []
    # e and f: DevMem recorded the session and logged a pack line per prompt; f started from e's store
    assert e["devmem_store"] and e["devmem_sessions"] >= 1 and e["pack_lines"] == 1
    assert f["restored_state"] and f["devmem_sessions"] >= 2 and f["pack_lines"] == 2
    assert (calib_dir / "results" / "e" / "packs.jsonl").is_file()
    assert "devmem_store" not in a
    # the sign-in token reached the sessions (fake_claude does not save it) and no saved file
    for p in calib_dir.rglob("*"):
        if p.is_file():
            assert SECRET.encode() not in p.read_bytes(), p
    # a subset continues from the saved state
    assert _main(calib, "--only", "b", "--confirm", "1") == 0
    again = json.loads((calib_dir / "results" / "summary.json").read_text(encoding="utf-8"))
    assert again["b"]["restored_state"] and again["b"]["ts"] >= b["ts"] and again["a"] == a


def test_the_token_reaches_the_session_environment(calib):
    cur = calib["root"] / "calib" / "current"
    env = C.calib_env(calib["root"], cur, C.SESSIONS[3])
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == SECRET
    assert not [k for k in env if k.upper().startswith("ANTHROPIC_")]
    assert env["CLAUDE_CONFIG_DIR"] == str(cur / "config") and env["HOME"] == str(cur / "home")
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"                       # session d
    assert C.calib_env(calib["root"], cur, C.SESSIONS[0])["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "0"
