"""`devmem adopt` and what a user of devmem 0.3.0 sees after upgrading. 0.3.0 kept no note of the stores it made, so
a store it made for `devmem init --store <folder>` or for the commands used without `init` (`begin`/`end`, `scan`,
`wrap`) is refused by the current version (devmem.consent.store_refusal): nothing outside the project shows it is the
user's. Every refusal says so in words an upgrading user understands and names `devmem adopt`, which shows what the
store holds and marks it as the user's without installing anything; a switched-on project whose store is refused says
so at SessionStart (a systemMessage Claude Code shows the user), so recording does not stop silently."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from cli_helpers import make_project, write_session

from devmem import cli, consent, hooks
from devmem import init as I

MARK = "ADOPTMARKWORD"
TASK = "fix the wallet balance label"
SID = "11111111-2222-3333-4444-555555555555"


def _git(project: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(project), "-c", "core.autocrlf=false", "-c", "user.email=t@example.invalid",
                    "-c", "user.name=t", *args], check=True, capture_output=True)


def old_store(store: Path) -> None:
    """A store as devmem 0.3.0 left it: sessions, no note in the per-user folder."""
    store.mkdir(parents=True, exist_ok=True)
    (store / ".gitignore").write_text("*\n", encoding="utf-8")
    write_session(store, "abcdef12-0000-0000-0000-000000000000", f"{TASK} {MARK} please", claim=False,
                  started_at="2026-10-02T09:00:00Z", files_edited=["src/wallet.py"])
    write_session(store, "bcdef123-0000-0000-0000-000000000000", "second request", claim=False,
                  started_at="2026-10-20T09:00:00Z")


def _events(project: Path, sid: str = SID) -> list[tuple[str, dict]]:
    base = {"session_id": sid, "cwd": str(project), "transcript_path": str(project / "none.jsonl")}
    return [("SessionStart", {**base, "hook_event_name": "SessionStart", "source": "startup"}),
            ("UserPromptSubmit", {**base, "hook_event_name": "UserPromptSubmit", "prompt": TASK, "prompt_id": "p1"}),
            ("Stop", {**base, "hook_event_name": "Stop", "last_assistant_message": "done"}),
            ("SessionEnd", {**base, "hook_event_name": "SessionEnd", "reason": "other"})]


def _hooks(project: Path, store: Path | None = None) -> dict[str, str]:
    argv = ["--store", str(store)] if store else []
    return {ev: hooks.run([ev, *argv], json.dumps(p).encode())[1] for ev, p in _events(project)}


def _sessions(store: Path) -> int:
    return len(list((store / "sessions").glob("*.meta.json")))


@pytest.fixture()
def not_a_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin", type("NoTty", (), {"isatty": lambda self: False})())


def test_the_refusal_tells_an_upgrading_user_what_happened(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    old_store(store)
    why = consent.store_refusal(project, store) or ""
    assert "has no record of having been created by this version of devmem for you" in why
    assert "0.3.0" in why and "`devmem adopt`" in why and "--adopt-store" in why
    assert "was not created by devmem for you" not in why       # it may well have been, by an older devmem
    outside = tmp_path / "mem"
    old_store(outside)
    assert f"`devmem adopt --store {outside}`" in (consent.store_refusal(project, outside) or "")


def test_init_store_user_after_upgrade_sees_a_notice_and_adopt_brings_recording_back(tmp_path, monkeypatch, capsys,
                                                                                     not_a_tty):
    """0.3.0 `devmem init --store <folder>`: the switch (no store in it) and a store outside the project with no note."""
    project = make_project(tmp_path)
    store = tmp_path / "outside-store"
    old_store(store)
    consent.enable_init(project, store=store)        # the user's init switch, as 0.3.0 wrote it (project only)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    outs = _hooks(project, store)
    notice = json.loads(outs["SessionStart"])["systemMessage"]
    assert "devmem is not recording this project" in notice and f"devmem adopt --store {store}" in notice
    assert not outs["UserPromptSubmit"] and _sessions(store) == 2           # nothing served, nothing recorded
    monkeypatch.chdir(project)
    assert cli.main(["sessions", "--store", str(store)]) == 2
    assert "devmem adopt --store" in capsys.readouterr().err
    assert cli.main(["adopt", "--store", str(store), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "2 recorded session(s), named for 2026-10-02 to 2026-10-20" in out and "adopted:" in out
    consent.forget_decisions()
    outs = _hooks(project, store)
    assert not outs["SessionStart"] and MARK in outs["UserPromptSubmit"]
    assert _sessions(store) == 3                                            # recording resumed


def test_cli_only_user_after_upgrade(tmp_path, monkeypatch, capsys, not_a_tty):
    """0.3.0 `devmem begin`/`end` without `init`: the default store, no switch, no note."""
    project = make_project(tmp_path)
    store = project / ".devmemory"
    old_store(store)
    monkeypatch.chdir(project)
    assert cli.main(["begin"]) == 2
    err = capsys.readouterr().err
    assert "nothing recorded" in err and "`devmem adopt`" in err
    assert cli.main(["adopt"]) == 2                    # not a terminal and no --yes: shows, asks for --yes, changes nothing
    cap = capsys.readouterr()
    assert "2 recorded session(s)" in cap.out and "--yes" in cap.err
    assert consent.store_refusal(project, store)
    assert cli.main(["adopt", "--yes"]) == 0
    capsys.readouterr()
    assert not (project / ".claude").exists()          # nothing installed
    assert cli.main(["begin"]) == 0
    assert cli.main(["end", "--prompt", "third request"]) == 0
    assert _sessions(store) == 3
    assert cli.main(["sessions"]) == 0 and MARK in capsys.readouterr().out


def test_adopt_shows_what_the_store_holds(tmp_path, monkeypatch, capsys, not_a_tty):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    old_store(store)
    (store / "config.json").write_text('{"redact": false, "capture": true}', encoding="utf-8")
    (store / "redact").write_text("# mine\nACME-[0-9]{6}\n", encoding="utf-8")
    _git(project, "init", "-q")
    _git(project, "add", "-A")
    _git(project, "add", "-f", ".devmemory/config.json")
    _git(project, "commit", "-qm", "ship")
    monkeypatch.chdir(project)
    assert cli.main(["adopt"]) == 2
    out = capsys.readouterr().out
    assert "tracks 1 file(s) in it (.devmemory/config.json)" in out
    assert "config.json (capture, redact)" in out and "asks to turn redaction off" in out
    assert "1 redaction pattern(s)" in out and "adopting does not approve it" in out
    assert cli.main(["adopt", "--yes"]) == 0
    assert consent.store_refusal(project, store) is None
    assert cli.main(["adopt"]) == 0 and "already yours" in capsys.readouterr().out


def test_a_cancelled_adoption_changes_nothing(tmp_path, monkeypatch, capsys):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    old_store(store)
    monkeypatch.chdir(project)
    monkeypatch.setattr("sys.stdin", type("Tty", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    assert cli.main(["adopt"]) == 1
    assert "cancelled" in capsys.readouterr().out and consent.store_refusal(project, store)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert cli.main(["adopt"]) == 0
    assert consent.store_refusal(project, store) is None


def test_a_pulled_tracked_file_is_named_with_both_ways_out(tmp_path, monkeypatch, capsys):
    project = make_project(tmp_path)
    _git(project, "init", "-q")
    I.init_project(project, scan=False)
    store = project / ".devmemory"
    pulled = store / "sessions" / "20261102T1000_bbbbbbbb.meta.json"
    pulled.parent.mkdir(parents=True, exist_ok=True)
    pulled.write_text("{}", encoding="utf-8")
    _git(project, "add", "-f", str(pulled))
    _git(project, "commit", "-qm", "pulled")
    monkeypatch.chdir(project)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "20261102T1000_bbbbbbbb.meta.json" in out and "`git rm`" in out and "`devmem adopt`" in out
    consent.forget_decisions()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    assert "devmem adopt" in json.loads(_hooks(project)["SessionStart"])["systemMessage"]


def test_the_plugin_route_stays_quiet_when_the_init_route_says_it(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    old_store(store)
    consent.enable_init(project)
    _git(project, "init", "-q")
    _git(project, "add", "-f", ".devmemory")
    _git(project, "commit", "-qm", "ship")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    start = json.dumps(_events(project)[0][1]).encode()
    assert hooks.run(["SessionStart"], start, route="plugin") == (0, "")
    assert "systemMessage" in hooks.run(["SessionStart"], start)[1]


def test_adopt_refuses_a_store_with_links(tmp_path, monkeypatch, capsys):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    old_store(store)
    target = tmp_path / "elsewhere"
    target.mkdir()
    try:
        (store / "index").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("no symbolic links here")
    monkeypatch.chdir(project)
    assert cli.main(["adopt", "--yes"]) == 2
    assert "symbolic links" in capsys.readouterr().err and consent.store_refusal(project, store)


def test_the_summary_flags_what_the_store_author_chose_and_says_what_config_keys_do(tmp_path, monkeypatch):
    """Session dates come from file names the store's author chose (reviewer's v8a/b/t6_adopt_shipped.py showed
    "from 2999-12-30 to 2999-12-31" with no warning); config keys were only listed; a git failure read as "tracks no
    file in it"."""
    import json as _json

    from devmem import cli as C
    from devmem import gitindex
    from devmem import store as S
    from hooks_helpers import make_project as mk
    project = mk(tmp_path, switched_on=False)
    st = project / ".devmemory"
    (st / "sessions").mkdir()
    for name in ("29991230T1000_aaaaaaaa.meta.json", "29991231T1000_bbbbbbbb.meta.json",
                 "20261101T1000_cccccccc.meta.json", "notes.meta.json"):
        (st / "sessions" / name).write_text("{}", encoding="utf-8")
    (st / "config.json").write_text(_json.dumps({"embeddings": {"backend": "fastembed"}, "mcp_call_log": True,
                                                 "colour": "blue"}), encoding="utf-8")
    text = "\n".join(C.store_summary(project, st))
    assert "future" in text and "1 file name(s) are not in the form" in text, text
    assert "newest ids: bbbbbbbb, aaaaaaaa, cccccccc" in text, text
    assert "embeddings: turns semantic search on" in text and "mcp_call_log: true makes" in text, text
    assert "colour:" not in text
    assert "tracks no file in it" in text

    def broken(*a, **k):
        raise S.GitNoAnswer("fatal: index file corrupt")

    def unreadable(*a, **k):
        raise gitindex.IndexUnknown("not read in this test")
    monkeypatch.setattr(S, "_candidates_raw", broken)
    monkeypatch.setattr(gitindex, "index_candidates", unreadable)
    (project / "x.py").write_text("x = 1\n", encoding="utf-8")
    import subprocess as sp
    sp.run(["git", "-C", str(project), "add", "x.py"], check=True, capture_output=True)   # a new index: asked again
    text = "\n".join(C.store_summary(project, st))
    assert "could not say which files" in text and "index file corrupt" in text, text
    assert "tracks no file in it" not in text
