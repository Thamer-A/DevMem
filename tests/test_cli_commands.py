"""The command line: subcommands, help, init/uninstall/status/doctor/forget, error messages, output encoding."""
from __future__ import annotations

import json
import sys

import pytest
from cli_helpers import ROOT, cli_env, make_project, run_cli, write_session
from hooks_helpers import TURN1, replay
from hooks_helpers import make_project as make_hooks_project

from devmem import cli, hooks, pack

SID_A = "aaaa1111-0000-0000-0000-000000000001"
SID_B = "bbbb2222-0000-0000-0000-000000000002"


@pytest.fixture()
def project(tmp_path):
    return make_project(tmp_path)


def run(capsys, *argv) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# ---------------------------------------------------------------- help

def test_top_level_help_lists_every_command(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for name in ("init", "uninstall", "hook", "forget", "status", "scan", "doctor", "pack", "run", "import", "mcp",
                 "codex", "wrap", "begin", "end", "ui", "app"):
        assert name in out, name
    assert "getting started" in out


def test_every_subcommand_has_help_with_options(capsys):
    parser = cli.build_parser()
    names = [a for a in parser._subparsers._group_actions[0].choices]
    assert len(names) >= 17
    for name in names:
        with pytest.raises(SystemExit) as e:
            cli.main([name, "--help"])
        assert e.value.code == 0, name
        assert len(capsys.readouterr().out) > 80, name


def test_no_command_prints_help_and_succeeds(capsys):
    code, out, _ = run(capsys)
    assert code == 0 and "usage: devmem" in out


# ---------------------------------------------------------------- init / uninstall

def test_init_defaults_to_local_settings_and_explains_undo(project, capsys):
    code, out, err = run(capsys, "init", "--project", str(project))
    assert code == 0, err
    local = project / ".claude" / "settings.local.json"
    assert local.is_file() and not (project / ".claude" / "settings.json").exists()
    hooks_cfg = json.loads(local.read_text(encoding="utf-8"))["hooks"]
    assert "UserPromptSubmit" in hooks_cfg and "PostToolUse" in hooks_cfg
    assert (project / ".devmemory" / ".gitignore").is_file()
    assert "settings.local.json" in out and "devmem uninstall" in out and "devmem forget --all" in out


def test_init_twice_changes_nothing_and_shared_flag_writes_settings_json(project, capsys):
    run(capsys, "init", "--project", str(project), "--no-scan")
    code, out, _ = run(capsys, "init", "--project", str(project), "--no-scan")
    assert code == 0 and "already installed" in out
    code, out, _ = run(capsys, "init", "--project", str(project), "--no-scan", "--shared")
    assert (project / ".claude" / "settings.json").is_file()
    assert "usually committed" in out


def test_uninstall_removes_only_devmem_hooks(project, capsys):
    claude = project / ".claude"
    claude.mkdir()
    other = {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]},
             "permissions": {"allow": ["Read"]}}
    (claude / "settings.local.json").write_text(json.dumps(other), encoding="utf-8")
    run(capsys, "init", "--project", str(project), "--no-scan")
    code, out, _ = run(capsys, "uninstall", "--project", str(project))
    assert code == 0 and "removed devmem hooks" in out
    after = json.loads((claude / "settings.local.json").read_text(encoding="utf-8"))
    assert after == other
    code, out, _ = run(capsys, "uninstall", "--project", str(project))
    assert "nothing changed" in out
    assert (project / ".devmemory").is_dir()  # the store is kept


# ---------------------------------------------------------------- status / scan / doctor

def test_status_without_store_is_a_one_line_error(project, capsys):
    code, out, err = run(capsys, "status", "--project", str(project))
    assert code == 2 and out == ""
    assert err.startswith("devmem: no memory store") and err.count("\n") == 1 and "Traceback" not in err


def test_status_shows_turns_hooks_and_git_ignore(project, capsys):
    run(capsys, "init", "--project", str(project))
    store = project / ".devmemory"
    write_session(store, SID_A, "Make the wallet label show negative balances")
    code, out, _ = run(capsys, "status", "--project", str(project))
    assert code == 0
    assert "git-ignored" in out and "NOT git-ignored" not in out
    assert "hooks:   installed" in out
    assert "1 captured session(s)" in out and "aaaa1111" in out and "1 turn(s)" in out


def test_status_shows_in_progress_session(tmp_path, monkeypatch, capsys):
    project = make_hooks_project(tmp_path / "h")
    replay(project, TURN1[:4], tmp_path / "t.jsonl", monkeypatch, hooks)  # no Stop / SessionEnd yet
    code, out, _ = run(capsys, "status", "--project", str(project))
    assert code == 0 and "in progress" in out and "turn(s)" in out
    assert "hooks:   not installed" in out


def test_scan_prints_incremental_counts(project, capsys):
    code, out, _ = run(capsys, "scan", "--project", str(project))
    assert code == 0 and "2 added" in out and "No model tokens" in out
    (project / "src" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    code, out, _ = run(capsys, "scan", "--project", str(project))
    assert "1 added" in out and "2 unchanged" in out


def test_doctor_healthy_and_read_only(project, capsys):
    run(capsys, "init", "--project", str(project))
    before = sorted(p.relative_to(project).as_posix() + str(p.stat().st_mtime_ns)
                    for p in project.rglob("*") if p.is_file())
    code, out, _ = run(capsys, "doctor", "--project", str(project))
    after = sorted(p.relative_to(project).as_posix() + str(p.stat().st_mtime_ns)
                   for p in project.rglob("*") if p.is_file())
    assert code == 0, out
    assert before == after
    for label in ("python", "store", "index", "hooks", "hook python", "git", "claude"):
        assert label in out, label
    assert "0 problem(s)" in out


def test_doctor_flags_hook_interpreter_that_does_not_exist(project, tmp_path, capsys):
    run(capsys, "init", "--project", str(project), "--no-scan", "--python", str(tmp_path / "no" / "python.exe"))
    code, out, _ = run(capsys, "doctor", "--project", str(project))
    assert code == 1
    assert "FAIL" in out and "does not exist" in out


def test_doctor_hook_message_names_the_effect_and_the_repair(project, tmp_path, capsys):
    run(capsys, "init", "--project", str(project), "--no-scan", "--python", str(tmp_path / "no" / "python.exe"))
    _, out, _ = run(capsys, "doctor", "--project", str(project))
    assert "Claude Code sessions" in out and "may" in out and "`devmem init` repairs it" in out


def _edit_handlers(project, fn):
    f = project / ".claude" / "settings.local.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    for groups in data["hooks"].values():
        for g in groups:
            for h in g["hooks"]:
                fn(h)
    f.write_text(json.dumps(data), encoding="utf-8")


def test_doctor_bootstrap_handler_with_missing_devmem_folder_is_a_problem(project, tmp_path, capsys):
    run(capsys, "init", "--project", str(project), "--no-scan")
    gone = str(tmp_path / "moved")

    def point_away(h):
        if h.get("args", [None])[0] == "-c":
            h["args"][2] = gone
    _edit_handlers(project, point_away)
    code, out, _ = run(capsys, "doctor", "--project", str(project))
    assert code == 1
    assert "not being recorded" in out and "`devmem init` repairs it" in out


def test_doctor_warns_about_the_old_script_hook_form(project, capsys):
    from devmem.init import hooks_script
    run(capsys, "init", "--project", str(project), "--no-scan")

    def old_form(h):
        h["args"] = [str(hooks_script()), h["args"][-1] if h["args"][-1] != "-c" else "Stop"]
    _edit_handlers(project, old_form)
    code, out, _ = run(capsys, "doctor", "--project", str(project))
    assert "old hook form" in out and "`devmem init` upgrades it" in out


def test_doctor_reports_a_store_the_user_cannot_write_to(project, monkeypatch, capsys):
    run(capsys, "init", "--project", str(project), "--no-scan")
    import tempfile

    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(tempfile, "mkstemp", denied)
    code, out, _ = run(capsys, "doctor", "--project", str(project))
    assert code == 1
    line = next(ln for ln in out.splitlines() if "FAIL" in ln and "store" in ln)
    assert str(project / ".devmemory") in line and "cannot be written" in line


def test_ui_help_does_not_claim_the_app_is_read_only(capsys):
    with pytest.raises(SystemExit):
        cli.main(["ui", "--help"])
    out = capsys.readouterr().out
    assert "(read-only)" not in out and "never modifies project files" in out


# ---------------------------------------------------------------- forget

@pytest.fixture()
def two_sessions(project):
    store = project / ".devmemory"
    store.mkdir()
    a = write_session(store, SID_A, "first request", "2026-10-01T10:00:00Z")
    b = write_session(store, SID_B, "second request", "2026-11-05T10:00:00Z")
    return project, store, a, b


def test_forget_one_session_by_prefix_prints_what_was_removed(two_sessions, capsys):
    project, store, a, b = two_sessions
    code, out, _ = run(capsys, "forget", SID_A[:6], "--project", str(project))
    assert code == 0
    assert not a.exists() and b.exists()
    assert f"session {SID_A}" in out and a.name in out and "removed 1 session(s) and 2 file(s)" in out


def test_forget_unknown_id_and_missing_selection_are_errors(two_sessions, capsys):
    project, store, a, b = two_sessions
    code, out, err = run(capsys, "forget", "zzzzzz", "--project", str(project))
    assert code == 2 and err.startswith("devmem: no session with id") and a.exists()
    code, out, err = run(capsys, "forget", "--project", str(project))
    assert code == 2 and "choose one of" in err


def test_forget_all_needs_yes_when_stdin_is_not_a_terminal(two_sessions, capsys):
    project, store, a, b = two_sessions
    code, out, err = run(capsys, "forget", "--all", "--project", str(project))
    assert code == 2 and "--yes" in err
    assert a.exists() and b.exists()
    code, out, err = run(capsys, "forget", "--all", "--yes", "--project", str(project))
    assert code == 0 and not a.exists() and not b.exists()
    assert "removed 2 session(s)" in out


def test_forget_all_counts_the_files_of_a_store_that_is_not_yours(tmp_path, capsys):
    """A store that is not this user's shows no session as theirs; `forget --all` still names what it would delete."""
    project = make_project(tmp_path)
    store = project / ".devmemory"
    write_session(store, "abcdef12-0000-0000-0000-000000000000", "shipped", claim=False)
    code, out, err = run(capsys, "forget", "--all", "--project", str(project))
    assert code == 2 and "delete all 0 captured sessions: 2 file(s)" in err and "not known to be yours" in err
    assert len(list((store / "sessions").iterdir())) == 2


def test_forget_before_date_and_bad_date(two_sessions, capsys):
    project, store, a, b = two_sessions
    code, out, err = run(capsys, "forget", "--before", "not-a-date", "--yes", "--project", str(project))
    assert code == 2 and "cannot read the date" in err and a.exists()
    code, out, err = run(capsys, "forget", "--before", "2026-11-01", "--yes", "--project", str(project))
    assert code == 0 and not a.exists() and b.exists()


def test_forget_asks_in_a_terminal_and_cancels_on_anything_but_yes(two_sessions, capsys, monkeypatch):
    project, store, a, b = two_sessions

    class Tty:
        def isatty(self):
            return True
    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    code, out, _ = run(capsys, "forget", "--all", "--project", str(project))
    assert code == 1 and "cancelled" in out and a.exists() and b.exists()
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    code, out, _ = run(capsys, "forget", "--all", "--project", str(project))
    assert code == 0 and not a.exists()


def test_forget_updates_the_search_index_when_available(two_sessions, capsys, monkeypatch):
    project, store, a, b = two_sessions
    calls = []
    monkeypatch.setattr(pack, "sync_session_index", lambda s: calls.append(s), raising=False)
    run(capsys, "forget", SID_A[:6], "--project", str(project))
    assert calls == [store]


def test_forget_works_without_sync_session_index(two_sessions, capsys, monkeypatch):
    project, store, a, b = two_sessions
    monkeypatch.delattr(pack, "sync_session_index", raising=False)
    code, _, _ = run(capsys, "forget", SID_A[:6], "--project", str(project))
    assert code == 0


# ---------------------------------------------------------------- begin / end

def test_begin_prints_pending_id_and_end_uses_it(project, capsys):
    from devmem.store import ensure_store
    ensure_store(project / ".devmemory")      # a store devmem made (a folder it did not make is refused: test_store_trust)
    code, out, _ = run(capsys, "begin", "--project", str(project))
    assert code == 0
    pid = out.split("pending id:")[1].split()[0]
    (project / "src" / "new.py").write_text("y = 2\n", encoding="utf-8")
    code, out, err = run(capsys, "end", "--id", pid, "--prompt", "add new.py", "--project", str(project))
    assert code == 0, err
    assert "captured ->" in out
    code, out, err = run(capsys, "end", "--id", pid, "--prompt", "again", "--project", str(project))
    assert code == 2 and err.startswith("devmem: ") and "Traceback" not in err


# ---------------------------------------------------------------- errors

def test_missing_project_is_a_one_line_error(tmp_path, capsys):
    for cmd in (["scan"], ["status"], ["pack", "x"], ["doctor"], ["init"], ["forget", "--all", "--yes"]):
        code, out, err = run(capsys, *cmd, "--project", str(tmp_path / "nope"))
        assert code == 2 and err.startswith("devmem: no such project folder"), cmd


def test_pack_without_store_says_what_to_run(project, capsys):
    code, out, err = run(capsys, "pack", "anything", "--project", str(project))
    assert code == 2 and "devmem scan" in err and out == "" and not (project / ".devmemory").exists()


def test_init_with_broken_settings_json_leaves_it_alone(project, capsys):
    f = project / ".claude" / "settings.local.json"
    f.parent.mkdir()
    f.write_text("{not json", encoding="utf-8")
    code, out, err = run(capsys, "init", "--project", str(project), "--no-scan")
    assert code == 2 and "not valid JSON" in err
    assert f.read_text(encoding="utf-8") == "{not json"


# ---------------------------------------------------------------- hook command (subprocess)

def test_hook_subcommand_records_a_prompt_and_old_no_argument_form_still_works(project):
    run_cli(["init", "--no-scan"], project)
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "cccc3333-0000", "cwd": str(project),
               "prompt": "negative balance in the wallet"}
    env = cli_env(CLAUDE_PROJECT_DIR=str(project))
    r = run_cli(["hook"], project, stdin=json.dumps(payload), env=env)  # old form: no event name
    assert r.returncode == 0, r.stderr
    live = project / ".devmemory" / "live" / "cccc3333-0000.jsonl"
    assert live.is_file() and "negative balance" in live.read_text(encoding="utf-8")
    r = run_cli(["hook", "Stop"], project, stdin=json.dumps({**payload, "hook_event_name": "Stop",
                                                              "last_assistant_message": "ok"}), env=env)
    assert r.returncode == 0


# ---------------------------------------------------------------- encoding

def test_pack_survives_a_cp1252_pipe_with_a_character_it_cannot_encode(project):
    """Regression: `devmem pack` died with UnicodeEncodeError when the pack held U+2212 and stdout was cp1252."""
    assert run_cli(["scan"], project).returncode == 0
    write_session(project / ".devmemory", SID_A, "Use the − sign for the negative balance in the wallet")
    r = run_cli(["pack", "negative balance wallet"], project, env=cli_env(PYTHONIOENCODING="cp1252"))
    assert r.returncode == 0, r.stderr.decode("cp1252", "replace")
    assert b"Traceback" not in r.stderr
    out = r.stdout.decode("cp1252", "replace")
    assert "negative balance" in out and "context pack" in out
    r = run_cli(["status"], project, env=cli_env(PYTHONIOENCODING="cp1252"))
    assert r.returncode == 0 and b"Traceback" not in r.stderr


def test_pack_to_a_pipe_is_utf8_whatever_the_console_code_page(project):
    """Piped stdout carries the user's own words intact (here U+2212 and Arabic), even with PYTHONIOENCODING=cp1252."""
    assert run_cli(["scan"], project).returncode == 0
    write_session(project / ".devmemory", SID_A, "Use the − sign for the negative balance in the wallet, يا صديقي")
    r = run_cli(["pack", "negative balance wallet"], project, env=cli_env(PYTHONIOENCODING="cp1252"))
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    out = r.stdout.decode("utf-8")           # strict: the whole stream is valid UTF-8
    assert "−" in out and "يا صديقي" in out


def test_pack_out_file_keeps_the_character(project):
    run_cli(["scan"], project)
    write_session(project / ".devmemory", SID_A, "Use the − sign for the negative balance in the wallet")
    out = project / "pack.md"
    r = run_cli(["pack", "negative balance wallet", "--out", str(out)], project, env=cli_env(PYTHONIOENCODING="cp1252"))
    assert r.returncode == 0
    assert "−" in out.read_text(encoding="utf-8")


def test_python_dash_m_devmem_version():
    r = run_cli(["--version"], ROOT)
    assert r.returncode == 0 and r.stdout.decode().startswith(f"devmem {cli.__version__}")


def test_forget_with_unreadable_stdin_is_an_error_not_a_traceback(two_sessions, capsys, monkeypatch):
    project, store, a, b = two_sessions

    class Tty:
        def isatty(self):
            return True

    def eof(prompt=""):
        raise EOFError
    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr("builtins.input", eof)
    code, out, err = run(capsys, "forget", "--all", "--project", str(project))
    assert code == 2 and "--yes" in err and a.exists() and b.exists()


def test_doctor_temp_error_log_is_only_reported_for_the_project_it_names(project, tmp_path, monkeypatch, capsys):
    from devmem import hooks
    run(capsys, "init", "--project", str(project), "--no-scan")
    log = tmp_path / "hook_errors.log"
    monkeypatch.setattr(hooks, "fallback_error_log", lambda: log)
    other = tmp_path / "elsewhere" / ".devmemory"
    log.write_text(f"--- 2026-11-14T00:00:00Z Stop pid=1 store={other}\nTraceback\n", encoding="utf-8")
    _, out, _ = run(capsys, "doctor", "--project", str(project))
    assert "hook errors" not in out
    log.write_text(f"--- 2026-11-14T00:00:00Z Stop pid=1 store={project / '.devmemory'}\nTraceback\n", encoding="utf-8")
    _, out, _ = run(capsys, "doctor", "--project", str(project))
    assert "recent failure for this project's store" in out
    log.write_text("--- 2026-11-14T00:00:00Z Stop pid=1 store=None\nTraceback\n", encoding="utf-8")
    log.write_text("--- 2026-11-14T00:00:00Z Stop pid=1\nTraceback\n", encoding="utf-8")
    _, out, _ = run(capsys, "doctor", "--project", str(project))
    assert "does not say which project" in out


def test_status_rewrites_old_pending_begin_files_without_raw_text(project, capsys):
    from devmem import generic
    from devmem.store import ensure_store
    store = ensure_store(project / ".devmemory")       # the user's own store (status changes nothing in another one)
    d = generic._pending_dir(store)
    d.mkdir(parents=True, exist_ok=True)
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    f = d / "old1.json"
    f.write_text(json.dumps({"id": "old1", "kind": "snapshot", "started_at": "2026-11-01T00:00:00Z",
                             "snapshot": {"src/a.py": f"token = '{secret}'\n"}}), encoding="utf-8")
    code, out, _ = run(capsys, "status", "--project", str(project))
    assert code == 0
    data = f.read_text(encoding="utf-8")
    assert secret not in data and json.loads(data)["v"] == 2


def test_status_survives_a_failing_secure_pending(project, capsys, monkeypatch):
    from devmem import generic
    (project / ".devmemory").mkdir(exist_ok=True)
    monkeypatch.setattr(generic, "secure_pending", lambda *a, **k: 1 / 0)
    code, out, _ = run(capsys, "status", "--project", str(project))
    assert code == 0
