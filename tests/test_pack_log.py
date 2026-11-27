"""The pack log (`.devmemory/packs.jsonl`, `devmem packs`): one line per prompt of a recorded session with what its
pack carried (item keys, estimated tokens) or why it was empty; through the redactor; never prompt or pack text;
`devmem forget` removes a forgotten session's lines and its items in other sessions' lines (gate experiment, task 4)."""
from __future__ import annotations

import json

import pytest
from hooks_helpers import make_project

from devmem import hooks, store as store_mod
from devmem import sessions as S
from devmem.cli import main as cli_main

EARLIER, LATER = "earlier-0001", "later-0002"
SECRET = "ghp_" + "Ab3d" * 9          # made up, in the GitHub token format


def _send(project, event, sid, **kw):
    p = {"session_id": sid, "cwd": str(project), "hook_event_name": event,
         "transcript_path": str(project / "none.jsonl"), **kw}
    return hooks.run([event], json.dumps(p).encode("utf-8"))


def _config(project, **cfg):
    (project / ".devmemory" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _lines(project) -> list[dict]:
    path = project / ".devmemory" / "packs.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.fixture()
def project(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    (project / "parser.py").write_text("def parse_ledger(x):\n    return round(x, 2)\n", encoding="utf-8")
    _config(project)                                    # delivery on (make_project turns it off)
    _send(project, "SessionStart", EARLIER, source="startup")
    _send(project, "UserPromptSubmit", EARLIER, prompt="fix parse_ledger rounding in parser.py: always 2 decimals",
          prompt_id="a")
    _send(project, "Stop", EARLIER, last_assistant_message="parse_ledger now rounds to 2 decimals", prompt_id="a")
    _send(project, "SessionEnd", EARLIER, reason="exit")
    return project


def _five_prompts(project):
    _send(project, "SessionStart", LATER, source="startup")
    outs = []
    for i, prompt in enumerate(["parse_ledger rounding is wrong again", "thanks", "ok", "parse_ledger rounding"]):
        outs.append(_send(project, "UserPromptSubmit", LATER, prompt=prompt, prompt_id=f"p{i}"))
        _send(project, "Stop", LATER, last_assistant_message="done", prompt_id=f"p{i}")
    _config(project, deliver=False)
    outs.append(_send(project, "UserPromptSubmit", LATER, prompt="parse_ledger once more", prompt_id="p4"))
    return outs


def test_five_prompts_give_five_rows_each_empty_one_with_its_reason(project, capsys):
    outs = _five_prompts(project)
    assert all(code == 0 for code, _ in outs) and "## Past sessions" in outs[0][1]
    later = [e for e in _lines(project) if e["session_id"] == LATER]
    assert len(later) == 5
    assert [e["turn"] for e in later] == [0, 1, 2, 3, 4]
    first = later[0]
    assert first["keys"] and first["tokens_est"] > 0 and first["chars"] == len(outs[0][1]) and first["mode"] == "full"
    assert first["empty_reason"] is None
    assert any(v.startswith(EARLIER) for v in first["sources"].values())
    for e in later[1:]:
        assert e["keys"] == [] and e["tokens_est"] == 0 and e["empty_reason"], e
    assert later[3]["empty_reason"] == "everything relevant was already delivered in this session"
    assert later[4]["empty_reason"] == hooks.DELIVERY_OFF
    capsys.readouterr()
    assert cli_main(["packs", "-p", str(project), "--session", LATER]) == 0
    out = capsys.readouterr().out
    rows = [ln for ln in out.splitlines() if ln.startswith("20")]
    assert len(rows) == 5, out
    assert "5 prompt(s), 1 with a pack (20%)" in out
    assert sum("empty: " in r for r in rows) == 4 and "empty: " + hooks.DELIVERY_OFF in out
    assert "item(s): session:" in rows[0]


def test_no_prompt_or_pack_text_and_everything_redacted(project):
    _send(project, "SessionStart", LATER, source="startup")
    _send(project, "UserPromptSubmit", LATER, prompt="parse_ledger rounding is wrong again", prompt_id=SECRET)
    raw = (project / ".devmemory" / "packs.jsonl").read_text(encoding="utf-8")
    assert SECRET not in raw and "[REDACTED:github-token]" in raw
    assert "wrong again" not in raw and "always 2 decimals" not in raw and "DevMemory context pack" not in raw


def test_capture_off_writes_no_pack_line(project):
    _config(project, capture=False)
    _send(project, "SessionStart", LATER, source="startup")
    code, out = _send(project, "UserPromptSubmit", LATER, prompt="parse_ledger rounding is wrong again",
                      prompt_id="q")
    assert code == 0 and "## Past sessions" in out        # the pack is still delivered
    assert [e for e in _lines(project) if e["session_id"] == LATER] == []


def test_a_failing_pack_log_never_breaks_the_prompt(project, monkeypatch):
    real = hooks._append

    def failing(path, obj, red=None):
        if path.name == "packs.jsonl":
            raise OSError("disk full (test)")
        return real(path, obj, red)
    monkeypatch.setattr(hooks, "_append", failing)
    _send(project, "SessionStart", LATER, source="startup")
    code, out = _send(project, "UserPromptSubmit", LATER, prompt="parse_ledger rounding is wrong again",
                      prompt_id="q")
    assert code == 0 and "## Past sessions" in out
    log = (project / ".devmemory" / "hook_errors.log").read_text(encoding="utf-8")
    assert "pack_log" in log and "disk full (test)" in log


def test_forget_removes_a_sessions_lines_and_its_items_elsewhere(project, capsys):
    _five_prompts(project)
    store = project / ".devmemory"
    assert [e for e in _lines(project) if e["session_id"] == EARLIER]       # the earlier session's own prompt
    result = S.forget_session(store, EARLIER)
    assert result["pack_log"]["lines_removed"] == 1 and result["pack_log"]["lines_changed"] == 1
    lines = _lines(project)
    assert not [e for e in lines if e["session_id"] == EARLIER]
    raw = (store / "packs.jsonl").read_text(encoding="utf-8")
    assert EARLIER not in raw and EARLIER[:8] not in raw
    first = lines[0]
    assert first["session_id"] == LATER and first["forgotten_items"] >= 1
    assert not [k for k in first["keys"] if k.startswith(("session:", "req:"))] and first["sources"] == {}
    capsys.readouterr()
    assert cli_main(["packs", "-p", str(project)]) == 0
    assert "item(s) of a forgotten session removed" in capsys.readouterr().out
    S.forget_session(store, LATER)
    assert _lines(project) == []


def test_forget_cli_reports_the_pack_log(project, capsys):
    _five_prompts(project)
    capsys.readouterr()
    assert cli_main(["forget", "-p", str(project), EARLIER]) == 0
    assert "pack log (packs.jsonl): 1 line(s) removed, 1 line(s) lost items" in capsys.readouterr().out


def test_forget_all_deletes_the_pack_log(project):
    _five_prompts(project)
    store = project / ".devmemory"
    assert store / "packs.jsonl" in S.forget_all_files(store)
    S.forget_all(store)
    assert not (store / "packs.jsonl").exists()


def test_a_log_too_long_to_read_whole_is_deleted_by_forget(project, monkeypatch):
    _five_prompts(project)
    store = project / ".devmemory"
    monkeypatch.setattr(S, "LIVE_READ_CAP", 200)
    result = S.forget_pack_log(store, [LATER])
    assert result["deleted"] and not (store / "packs.jsonl").exists()


def test_forget_of_an_unrelated_session_leaves_the_log_untouched(project):
    _five_prompts(project)
    path = project / ".devmemory" / "packs.jsonl"
    before = path.read_bytes()
    assert S.forget_pack_log(project / ".devmemory", ["someone-else-0003"]) == {
        "lines_removed": 0, "lines_changed": 0, "deleted": False}
    assert path.read_bytes() == before


def test_the_pack_log_is_a_known_store_file_and_link_checked():
    assert "packs.jsonl" in hooks.LINK_CHECKED and "packs.jsonl" in store_mod.STORE_ENTRIES
    assert hooks.PACK_LOG == S.PACK_LOG == "packs.jsonl"


def test_packs_with_no_log(tmp_path, capsys):
    project = make_project(tmp_path)
    assert cli_main(["packs", "-p", str(project)]) == 0
    assert "no prompts in the pack log yet" in capsys.readouterr().out
