"""Route x store-state matrix for "is this store this user's?" (devmem.consent.store_refusal, the one place that
decides). Recorded sessions in a store that this user's devmem did not create and the user did not adopt are not
served by any route: the MCP tools (and the server's start), `devmem pack` / `sessions` / `show` / `status` /
`doctor`, `wrap` and `codex` (which still run the agent, with a pack of project files only), `run` / `import` /
`begin` / `end` (which record nothing there), the viewer's API and the hooks. None of them changes anything in such a
store: every file's size, time and content and every folder stay as they were (`unchanged`)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cli_helpers import make_project, run_cli, write_session

from devmem import cli, consent, ui_server
from devmem import init as I
from devmem.mcp_server import Server

MARK = "ZQXMARKERWORD"
TASK = "fix the wallet balance label"
STATES = ["none", "own", "shipped", "shipped-adopted", "own-then-pulled", "untracked-foreign", "switched-on"]
REFUSED = ["shipped", "own-then-pulled", "untracked-foreign"]   # a store exists and is not this user's


def snapshot(store: Path) -> dict | None:
    """Every folder (with its time) and every file (size, time, SHA-256) in the store, None when there is none."""
    if not store.exists():
        return None
    out: dict = {".": os.stat(store).st_mtime_ns}
    for root, dirs, files in os.walk(store):
        for d in dirs:
            p = Path(root, d)
            out[p.relative_to(store).as_posix() + "/"] = os.stat(p).st_mtime_ns
        for f in files:
            p = Path(root, f)
            st = os.stat(p)
            out[p.relative_to(store).as_posix()] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def unchanged(store: Path, before: dict | None, what: str) -> None:
    after = snapshot(store)
    if before != after:
        changed = sorted(k for k in set(before or {}) | set(after or {}) if (before or {}).get(k) != (after or {}).get(k))
        raise AssertionError(f"{what} changed the store: {changed[:10]}")


def _git(project: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(project), "-c", "core.autocrlf=false", "-c", "user.email=t@example.invalid",
                    "-c", "user.name=t", *args], check=True, capture_output=True)


def _session(store: Path) -> None:
    write_session(store, "abcdef12-0000-0000-0000-000000000000", f"{TASK} {MARK} please", files_edited=["src/wallet.py"],
                  claim=False)


def build(tmp_path: Path, state: str) -> tuple[Path, Path]:
    project = make_project(tmp_path)
    store = project / ".devmemory"
    _git(project, "init", "-q")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    if state == "none":
        return project, store
    if state in ("own", "own-then-pulled"):
        I.init_project(project, scan=False)               # the user's own setup: the store is noted as theirs
        _session(store)
        if state == "own-then-pulled":                    # later a pull brings a tracked record into the store
            extra = store / "sessions" / "20261102T1000_bbbbbbbb.meta.json"
            extra.write_text((store / "sessions" / "20261101T1000_abcdef12.meta.json").read_text(encoding="utf-8")
                             .replace("abcdef12-0000", "bbbbbbbb-0000"), encoding="utf-8")
            _git(project, "add", "-f", str(extra))
            _git(project, "commit", "-qm", "pulled")
    elif state == "switched-on":                           # an older devmem: the switch, but no note for the store
        store.mkdir()
        _session(store)
        consent.enable_init(project)
    else:                                                  # the store came with the repository (or an archive)
        store.mkdir()
        _session(store)
        if state != "untracked-foreign":
            _git(project, "add", "-f", ".devmemory")
            _git(project, "commit", "-qm", "ship a store")
        if state == "shipped-adopted":
            I.init_project(project, scan=False, adopt_store=True)
    return project, store


SERVED = {"none": False, "own": True, "shipped": False, "shipped-adopted": True, "own-then-pulled": False,
          "untracked-foreign": False, "switched-on": True}


@pytest.mark.parametrize("state", STATES)
def test_mcp_tools(tmp_path, state):
    project, store = build(tmp_path, state)
    before = snapshot(store)
    srv = Server(project, store, require_store=True)
    for name, args in [("list_sessions", {}), ("read_session", {"id": "abcdef12"}),
                       ("get_context_pack", {"task": TASK})]:
        try:
            text = srv.call(name, args)
        except ValueError as e:
            text = f"error: {e}"
        assert (MARK in text) == SERVED[state], (name, text[:300])
        if state in ("shipped", "untracked-foreign"):
            assert "--adopt-store" in text
    if state in REFUSED:
        for name, args in [("begin_session", {}), ("end_session", {"prompt": "x"})]:
            with pytest.raises(ValueError):
                Server(project, store).call(name, args)
        unchanged(store, before, "the MCP tools")


@pytest.mark.parametrize("state", REFUSED)
@pytest.mark.parametrize("read_only", [False, True])
def test_mcp_server_start_writes_nothing(tmp_path, state, read_only):
    project, store = build(tmp_path, state)
    before = snapshot(store)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05",
             "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_sessions", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_context_pack",
                                                                            "arguments": {"task": TASK}}}]
    r = run_cli(["mcp", "--project", str(project)] + (["--read-only"] if read_only else []), cwd=project,
                stdin="".join(json.dumps(m) + chr(10) for m in msgs))
    assert r.returncode == 0, r.stderr[-500:]
    out = r.stdout.decode("utf-8")
    assert MARK not in out and "--adopt-store" in out
    unchanged(store, before, "the MCP server")


@pytest.mark.parametrize("state", STATES)
def test_cli_lookups(tmp_path, state, capsys, monkeypatch):
    project, store = build(tmp_path, state)
    monkeypatch.chdir(project)
    before = snapshot(store)
    for argv in (["sessions"], ["show", "abcdef12"], ["pack", TASK], ["status"], ["doctor"]):
        code = cli.main(argv)
        out = capsys.readouterr()
        text = out.out + out.err
        if argv[0] == "status" and state == "none":
            assert code == 2
            continue
        if argv[0] == "doctor":
            assert MARK not in text
            if state in REFUSED:
                assert "--adopt-store" in text and "not tested for writing" in text, text[-600:]
            continue
        assert (MARK in text) == SERVED[state], (argv, code, text[:400])
        if not SERVED[state] and state != "none":
            assert "--adopt-store" in text, (argv, text[:400])
            if argv[0] != "status":
                assert code == 2
    if state in REFUSED:
        unchanged(store, before, "sessions/show/pack/status/doctor")


@pytest.mark.parametrize("state", STATES)
def test_viewer_api(tmp_path, state):
    project, store = build(tmp_path, state)
    before = snapshot(store)
    st = ui_server.State(project, store)
    if state in REFUSED:
        opened = ui_server.api_post(st, "/api/open", {"project": [str(project)], "scan": ["1"]})
        assert isinstance(opened, dict) and opened.get("read_only"), opened
    seen = json.dumps([ui_server.api(st, p, q) for p, q in [
        ("/api/sessions", {}), ("/api/session", {"id": ["abcdef12"]}), ("/api/decisions", {}), ("/api/graph", {}),
        ("/api/overview", {}), ("/api/pack", {"task": [TASK]}), ("/api/file", {"path": ["src/wallet.py"]})]],
        default=str)
    assert (MARK in seen) == SERVED[state], seen[:500]
    if state in REFUSED:
        assert "--adopt-store" in seen
        unchanged(store, before, "the viewer")


def _hook_events(project: Path, sid: str) -> list[tuple[str, dict]]:
    base = {"session_id": sid, "cwd": str(project), "transcript_path": str(project / "none.jsonl")}
    return [("SessionStart", {**base, "hook_event_name": "SessionStart", "source": "startup"}),
            ("UserPromptSubmit", {**base, "hook_event_name": "UserPromptSubmit", "prompt": TASK, "prompt_id": "p1"}),
            ("PostToolUse", {**base, "hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_use_id": "t1",
                             "tool_input": {"file_path": str(project / "src" / "wallet.py"), "old_string": "str(amount)",
                                            "new_string": "f'{amount:.2f}'"}, "tool_response": {"ok": True}}),
            ("Stop", {**base, "hook_event_name": "Stop", "last_assistant_message": "fixed the label"}),
            ("SessionEnd", {**base, "hook_event_name": "SessionEnd", "reason": "other"})]


@pytest.mark.parametrize("state", ["own", "shipped", "untracked-foreign", "switched-on", "own-then-pulled"])
def test_hooks(tmp_path, state, monkeypatch):
    from devmem import hooks
    project, store = build(tmp_path, state)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    before = snapshot(store)
    outs = {}
    for event, payload in _hook_events(project, "11111111-2222-3333-4444-555555555555"):
        _code, outs[event] = hooks.run([event], json.dumps(payload).encode())
    switched = state in ("own", "switched-on", "own-then-pulled")   # the hooks also need the switch
    assert (MARK in outs["UserPromptSubmit"]) == (SERVED[state] and switched), outs["UserPromptSubmit"][:300]
    if state in REFUSED:      # own-then-pulled is switched on: nothing is recorded, read or delivered there either
        start = outs.pop("SessionStart")       # switched on: the user is told once, at SessionStart, how to resume
        assert not any(outs.values()) and MARK not in start
        assert ("devmem adopt" in json.loads(start)["systemMessage"]) if switched else not start
        unchanged(store, before, "the hooks")
    elif switched:
        assert list((store / "sessions").glob("*_11111111_*.meta.json"))      # recorded as usual


@pytest.mark.parametrize("state", ["own", "shipped", "own-then-pulled"])
def test_failing_hook_events_log_into_the_store_only_when_it_is_the_users(tmp_path, state, monkeypatch):
    """An event that fails before the trust check (a session id that is not a plain id, a payload that is not JSON)
    logs its error. Into the store only when the store is this user's; for any other store, into the temp-folder log
    (naming the store), and the store stays unchanged."""
    from devmem import hooks
    project, store = build(tmp_path, state)
    if state == "shipped":
        consent.enable_init(project)          # switched on (an older setup), so the init hooks run at all
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    fallback = tmp_path / "fallback.log"
    monkeypatch.setattr(hooks, "fallback_error_log", lambda: fallback)
    before = snapshot(store)
    base = {"cwd": str(project), "transcript_path": str(project / "none.jsonl")}
    bad_id = json.dumps({**base, "session_id": "../../evil", "hook_event_name": "PostToolUse", "tool_name": "Bash"})
    for event, data in (("PostToolUse", bad_id.encode()), ("Stop", b"{not json")):
        assert hooks.run([event], data) == (0, "")
    if state == "own":
        assert "not a plain id" in (store / "hook_errors.log").read_text(encoding="utf-8")
        return
    unchanged(store, before, "a failing hook event")
    text = fallback.read_text(encoding="utf-8")
    assert "not a plain id" in text and "JSONDecodeError" in text and f"store={store}" in text


AGENT = "import sys, shutil" + chr(10) + "shutil.copy(sys.argv[1], sys.argv[2])" + chr(10)
CODEX = "import os, sys" + chr(10) + "open(os.environ['STANDIN_OUT'], 'w', encoding='utf-8').write(sys.stdin.read())" + chr(10)


@pytest.mark.parametrize("state", STATES)
def test_wrap(tmp_path, state, capsys, monkeypatch):
    project, store = build(tmp_path, state)
    monkeypatch.chdir(project)
    agent, got = tmp_path / "agent.py", tmp_path / "pack-seen.md"
    agent.write_text(AGENT, encoding="utf-8")
    before = snapshot(store)
    code = cli.main(["wrap", "--prompt", TASK, "--", sys.executable, str(agent), "{pack_file}", str(got)])
    out = capsys.readouterr()
    assert code == 0, out.err[-500:]
    seen = got.read_text(encoding="utf-8")
    assert (MARK in seen) == SERVED[state], seen[:400]
    if state in REFUSED:
        assert "--adopt-store" in out.err and "not recorded" in out.out
        assert "wallet.py" in seen                 # the project's own files are still in the pack
        unchanged(store, before, "devmem wrap")
    else:
        assert "captured ->" in out.out


@pytest.mark.parametrize("state", STATES)
def test_codex(tmp_path, state, capsys, monkeypatch):
    from devmem import generic
    project, store = build(tmp_path, state)
    monkeypatch.chdir(project)
    standin, got = tmp_path / "codex_standin.py", tmp_path / "codex-input.txt"
    standin.write_text(CODEX, encoding="utf-8")
    monkeypatch.setenv("STANDIN_OUT", str(got))
    monkeypatch.setattr(generic, "find_program", lambda name, project=None: sys.executable)
    monkeypatch.setattr(generic, "codex_program", lambda exe, project=None: [sys.executable, str(standin)])
    before = snapshot(store)
    code = cli.main(["codex", TASK])
    out = capsys.readouterr()
    assert code == 0, out.err[-500:]
    seen = got.read_text(encoding="utf-8")
    assert TASK in seen and (MARK in seen) == SERVED[state], seen[:400]
    if state in REFUSED:
        assert "--adopt-store" in out.err and "not recorded" in out.out
        unchanged(store, before, "devmem codex")


@pytest.mark.parametrize("state", REFUSED)
def test_recording_commands_record_nothing_there(tmp_path, state, capsys, monkeypatch):
    project, store = build(tmp_path, state)
    monkeypatch.chdir(project)
    stream = tmp_path / "stream.jsonl"
    stream.write_text('{"type":"system"}' + chr(10), encoding="utf-8")
    before = snapshot(store)
    for argv in (["run", TASK], ["import", str(stream), "--prompt", TASK], ["begin"], ["end", "--prompt", TASK]):
        code = cli.main(argv)
        err = capsys.readouterr().err
        assert code == 2 and "nothing recorded" in err and "--adopt-store" in err, (argv, err[-400:])
    unchanged(store, before, "run/import/begin/end")


@pytest.mark.parametrize("state", REFUSED)
def test_disconnect_removes_devmems_entry_and_leaves_the_store_unchanged(tmp_path, state, capsys, monkeypatch):
    """The reviewer's d1: `devmem disconnect cursor` rewrote connect.json in a store that is not the user's. It still
    removes devmem's entry from the tool's own file (the user undoing a connection), and writes nothing in the store,
    nor reads its bookkeeping (a shipped connect.json naming files to delete)."""
    import json
    project, store = build(tmp_path, state)
    monkeypatch.chdir(project)
    (store / "connect.json").write_text(json.dumps({"version": 1, "tools": {"cursor": {"at": "2026-01-01"}},
                                                    "created": {".cursor/mcp.json": {"dirs": [".cursor"]}},
                                                    "written": {}}), encoding="utf-8")
    (project / ".cursor").mkdir()
    mcp = project / ".cursor" / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"devmemory": {"command": "devmem", "args": ["mcp"]},
                                              "other": {"command": "x"}}}), encoding="utf-8")
    before = snapshot(store)
    code = cli.main(["disconnect", "cursor"])
    text = capsys.readouterr().out
    assert code == 0, text
    assert json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"] == {"other": {"command": "x"}}
    assert "not yours" in text, text
    unchanged(store, before, "devmem disconnect")


@pytest.mark.parametrize("state", REFUSED)
def test_readers_refuse_by_themselves(tmp_path, state):
    """The functions that read recorded sessions ask the trust decision themselves (devmem.consent.recorded_refusal),
    so a caller that never asked store_refusal (a new route) still gets nothing from the store and changes nothing."""
    from devmem import pack, sessions
    project, store = build(tmp_path, state)
    consent.forget_decisions()
    before = snapshot(store)
    assert sessions.load_sessions(store, project=project) == []
    assert sessions.LOAD_REFUSED and "--adopt-store" in sessions.LOAD_REFUSED[0]
    text, info = pack.build_pack(project, store, TASK)
    assert MARK not in text and info.get("sessions_refused") and "wallet.py" in text
    pack.sync_session_index(store)
    assert pack._load_record(store, "20261101T1000_abcdef12.meta.json") is None
    unchanged(store, before, "the readers")


def test_refusal_names_the_reason_and_adopting_clears_it(tmp_path):
    project, store = build(tmp_path, "shipped")
    why = consent.store_refusal(project, store)
    assert why and "--adopt-store" in why and str(store) in why
    I.init_project(project, scan=False, adopt_store=True)
    assert consent.store_refusal(project, store) is None


def test_a_pull_that_changes_an_adopted_tracked_record_makes_the_store_foreign_again(tmp_path):
    project, store = build(tmp_path, "shipped-adopted")
    assert consent.store_refusal(project, store) is None
    meta = store / "sessions" / "20261101T1000_abcdef12.meta.json"
    meta.write_text(meta.read_text(encoding="utf-8").replace("please", "please now"), encoding="utf-8")
    assert consent.store_refusal(project, store) is None   # a change in the work tree only: devmem's own writes
    _git(project, "add", "-f", str(meta))
    _git(project, "commit", "-qm", "changed upstream")
    assert "tracked by git" in (consent.store_refusal(project, store) or "")


def test_connect_adopt_store_adopts(tmp_path):
    from devmem.connect import connect
    project, store = build(tmp_path, "shipped")
    connect(project, "cursor", command="devmem", which=lambda n: f"/usr/bin/{n}", adopt_store=True)
    assert consent.store_refusal(project, store) is None


def test_a_store_devmem_creates_is_noted_as_the_users(tmp_path):
    from devmem.store import ensure_store
    project = make_project(tmp_path)
    store = ensure_store(project / ".devmemory")
    _session(store)
    assert consent.store_refusal(project, store) is None
    assert consent.store_refusal(project, project / "missing") is None   # no store: nothing to refuse


@pytest.mark.parametrize("first", [["scan"], ["begin"]])
@pytest.mark.parametrize("route", ["init", "setup"])
def test_setup_accepts_a_store_devmem_made_for_this_user(tmp_path, capsys, monkeypatch, first, route):
    """`devmem scan` or `devmem begin` made the store (and noted it as this user's, outside the project): `devmem
    init` / `devmem setup` use it without --adopt-store, so the flag is not asked for by reflex."""
    project = make_project(tmp_path)
    _git(project, "init", "-q")
    monkeypatch.chdir(project)
    assert cli.main(first) == 0
    argv = ["init", "--no-scan"] if route == "init" else ["setup", "--no-scan", "--plugin-data", str(tmp_path / "pd")]
    assert cli.main(argv) == 0, capsys.readouterr().err
    assert consent.store_refusal(project, project / ".devmemory") is None


@pytest.mark.parametrize("state", ["shipped", "untracked-foreign"])
def test_a_shipped_store_still_needs_adopt_store(tmp_path, capsys, monkeypatch, state):
    project, store = build(tmp_path, state)
    monkeypatch.chdir(project)
    assert cli.main(["init", "--no-scan"]) == 2
    assert "--adopt-store" in capsys.readouterr().err


def test_a_store_devmem_made_but_git_now_tracks_still_needs_adopt_store(tmp_path, capsys, monkeypatch):
    project = make_project(tmp_path)
    _git(project, "init", "-q")
    monkeypatch.chdir(project)
    assert cli.main(["scan"]) == 0
    _git(project, "add", "-f", ".devmemory/PROJECT.md")
    assert cli.main(["init", "--no-scan"]) == 2
    assert "tracked by git" in capsys.readouterr().err


def test_a_project_cannot_forge_the_note(tmp_path, capsys, monkeypatch):
    """The note that says "devmem made this store for you" lives in devmem's per-user folder. A per-user folder inside
    the project (where a repository could ship notes) is refused, so a shipped store with a note of its own is not
    taken for this user's."""
    project, store = build(tmp_path, "untracked-foreign")
    inside = project / "userdata"
    monkeypatch.setenv(consent.USER_DATA_ENV, str(inside))
    with pytest.raises(ValueError):
        consent.store_note(store)
    (inside / consent.STORES).mkdir(parents=True)
    (inside / consent.STORES / consent.store_key(store)).write_text(json.dumps({"store": str(store)}), encoding="utf-8")
    assert not consent.made_here(store)
    assert consent.store_refusal(project, store)
    monkeypatch.chdir(project)
    assert cli.main(["init", "--no-scan"]) == 2
    capsys.readouterr()


@pytest.mark.parametrize("state", STATES)
def test_scan_and_connect_never_write_into_a_refused_store(tmp_path, state, capsys, monkeypatch):
    """`devmem scan` used to rewrite a refused store's tracked map, index and .gitignore, and put words of the user's
    own untracked files into the tracked index.db (reviewer's v8a/b/t5_scan_refused.py). It maps into devmem's
    per-user folder instead; `devmem connect` refuses."""
    from devmem.pack import files_only_store
    project, store = build(tmp_path, state)
    (project / "private_notes_local.txt").write_text("contoso acquisition notes\n", encoding="utf-8")
    monkeypatch.chdir(project)
    before = snapshot(store)
    code = cli.main(["scan"])
    out = capsys.readouterr()
    assert code == 0, out.err
    if state in REFUSED:
        unchanged(store, before, "devmem scan")
        assert "per-user folder" in out.out and "not changed" in out.out, out.out
        assert (files_only_store(project) / "PROJECT.md").is_file()
        code = cli.main(["connect", "codex"])
        out = capsys.readouterr()
        assert code != 0 and "nothing connected" in (out.out + out.err), out.out + out.err
        unchanged(store, before, "devmem connect")
        assert not (project / ".codex").exists() and not (project / "AGENTS.md").exists()
    else:
        assert (store / "PROJECT.md").is_file()
