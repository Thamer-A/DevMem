"""`devmem connect` / `disconnect` (devmem/connect.py), `devmem pack --copy` (devmem/clipboard.py) and the
doctor's connected-tools check. Everything runs in tmp_path; the real clipboard is never touched."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cli_helpers import cli_env, make_project, run_cli

from devmem import clipboard, connect as C
from devmem.connect import ConnectError, connect, disconnect

FILE_TOOLS = [k for k, t in C.TOOLS.items() if t.mcp_file]
JSON_TOOLS = [k for k, t in C.TOOLS.items() if t.mcp_file and t.mcp_format == "json"]
ODD_NAME = "my proj é ü"           # spaces and non-ASCII characters in the project path


def fake_which(name):
    return f"/usr/bin/{name}"


@pytest.fixture(autouse=True)
def _bare_devmem_command(monkeypatch):
    """These tests check the portable entry shape (`command: devmem`); the Windows absolute form has its own test."""
    monkeypatch.setattr(C, "ABSOLUTE_DEVMEM", False)


def test_windows_entry_names_devmem_by_its_absolute_path(monkeypatch):
    monkeypatch.setattr(C, "ABSOLUTE_DEVMEM", True)
    assert C.server_command(None, which=lambda n: r"C:\Tools\bin\devmem.exe")[0] == r"C:\Tools\bin\devmem.exe"
    assert C.server_command("devmem", which=lambda n: None)[0] == "devmem"   # not found: bare, with a warning


def do_connect(project, tool, **kw):
    kw.setdefault("command", "devmem")
    kw.setdefault("which", fake_which)
    return connect(project, tool, **kw)


def snapshot(root: Path, skip_store: bool = True) -> dict[str, bytes]:
    """Every file under root (bytes), without devmem's store (backups live there; anything else left behind in the
    project, an old-style `.devmem-backup` file included, shows up)."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if skip_store and (rel == ".devmemory" or rel.startswith(".devmemory/")):
            continue
        out[rel] = (b"<link>" + os.readlink(p).encode() if p.is_symlink() else
                    p.read_bytes() if p.is_file() else b"<dir>")
    return out


def backups(project: Path) -> list[Path]:
    d = project / ".devmemory" / "connect-backups"
    return sorted(d.iterdir()) if d.is_dir() else []


@pytest.fixture()
def project(tmp_path):
    p = tmp_path / ODD_NAME
    p.mkdir()
    return p


def mcp_data(project, tool):
    t = C.TOOLS[tool]
    return json.loads(project.joinpath(*t.mcp_file.split("/")).read_text(encoding="utf-8-sig"))


# ---------------------------------------------------------------- empty project, each tool

@pytest.mark.parametrize("tool", FILE_TOOLS)
def test_connect_empty_project_then_disconnect_leaves_nothing(project, tool):
    before = snapshot(project)
    r = do_connect(project, tool)
    t = C.TOOLS[tool]
    assert all(c["changed"] for c in r["changes"])
    if t.mcp_format == "json":
        entry = mcp_data(project, tool)[t.mcp_key]["devmemory"]
        assert entry["command"] == "devmem"
        assert entry["args"][:2] == ["mcp", "--project"]
        expected = t.project_ref if t.project_ref != "absolute" else str(project.resolve())
        assert entry["args"][2] == expected
        assert ("type" in entry) == t.stdio_type
    else:
        toml = (project / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert "[mcp_servers.devmemory]" in toml and 'command = "devmem"' in toml
        if sys.version_info >= (3, 11):
            import tomllib
            parsed = tomllib.loads(toml)["mcp_servers"]["devmemory"]
            assert parsed["args"] == ["mcp", "--project", str(project.resolve())]
    instr = project / t.instructions
    text = instr.read_text(encoding="utf-8")
    assert "get_context_pack" in text and "begin_session" in text and "end_session" in text and "STALE" in text
    assert "secrets" in text
    assert (project / ".devmemory" / ".gitignore").is_file()         # the state note went through ensure_store
    disconnect(project, tool)
    assert snapshot(project) == before                                  # created files and folders are gone


@pytest.mark.parametrize("tool", FILE_TOOLS)
def test_connect_is_idempotent(project, tool):
    do_connect(project, tool)
    once = snapshot(project, skip_store=False)
    r = do_connect(project, tool)
    assert not any(c["changed"] for c in r["changes"])
    assert snapshot(project, skip_store=False) == once
    assert not list(project.rglob("*.devmem-backup")) and backups(project) == []   # devmem's own files: no backup


@pytest.mark.parametrize("tool", FILE_TOOLS + ["generic", "claude-desktop"])
def test_print_writes_nothing(project, tool):
    (project / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    before = snapshot(project, skip_store=False)
    r = do_connect(project, tool, dry_run=True)
    assert snapshot(project, skip_store=False) == before
    assert not (project / ".devmemory").exists()
    assert r["dry_run"]
    for c in r["changes"]:
        assert c["preview"]


def test_print_shows_the_exact_json_that_connect_writes(project):
    r = do_connect(project, "cursor", dry_run=True)
    preview = r["changes"][0]["preview"]
    do_connect(project, "cursor")
    assert (project / ".cursor" / "mcp.json").read_text(encoding="utf-8") == preview


# ---------------------------------------------------------------- existing files

EXISTING_JSON = ('{\r\n    "editor.fontSize": 14,\r\n    "%KEY%": {\r\n        "other": {\r\n            "command": "node",\r\n'
                 '            "args": ["server.js"]\r\n        }\r\n    },\r\n    "zz": [1, 2]\r\n}')
EXISTING_MD = "# Team rules\r\n\r\nUse tabs. Ünïcödé ok.\r\n- no trailing newline here"


@pytest.mark.parametrize("tool", JSON_TOOLS)
def test_existing_config_and_instructions_are_kept_and_restored_byte_for_byte(project, tool):
    t = C.TOOLS[tool]
    cfg = project.joinpath(*t.mcp_file.split("/"))
    cfg.parent.mkdir(parents=True)
    cfg.write_bytes(EXISTING_JSON.replace("%KEY%", t.mcp_key).encode("utf-8"))
    (project / t.instructions).write_bytes(EXISTING_MD.encode("utf-8"))
    before = snapshot(project)

    r = do_connect(project, tool)
    data = mcp_data(project, tool)
    assert data["editor.fontSize"] == 14 and data["zz"] == [1, 2]
    assert list(data[t.mcp_key]) == ["other", "devmemory"]
    assert data[t.mcp_key]["other"] == {"command": "node", "args": ["server.js"]}
    raw = cfg.read_bytes()
    assert b"\r\n" in raw and b'\n    "editor.fontSize"' in raw          # line endings and indent kept
    md = (project / t.instructions).read_bytes()
    assert md.startswith(EXISTING_MD.encode("utf-8"))                    # the user's text is untouched
    assert b"\r\n<!-- devmemory:begin" in md and b"\n<!-- devmemory:end -->\r\n" in md
    made = [c["backup"] for c in r["changes"] if c["backup"]]
    assert len(made) == 2 and all(Path(b).is_file() for b in made)
    store = (project / ".devmemory").resolve()
    assert all(Path(b).resolve().parent.parent == store for b in made)   # in the git-ignored store, not the project
    assert sorted(Path(b).read_bytes() for b in made) == sorted([EXISTING_JSON.replace("%KEY%", t.mcp_key).encode(),
                                                                EXISTING_MD.encode("utf-8")])
    assert not list(project.rglob("*.devmem-backup"))

    disconnect(project, tool)
    assert snapshot(project) == before
    assert backups(project) == []                                        # restored: the copies are gone


def test_existing_codex_toml_is_appended_to_and_restored(project):
    cfg = project / ".codex" / "config.toml"
    cfg.parent.mkdir()
    original = 'model = "o5"\r\n\r\n[mcp_servers.other]\r\ncommand = "node"\r\n# a comment, kept\r\n'
    cfg.write_bytes(original.encode("utf-8"))
    before = snapshot(project)
    do_connect(project, "codex", command="python", python=sys.executable, probe=lambda py: None)
    text = cfg.read_bytes().decode("utf-8")
    assert text.startswith(original) and "\r\n# devmemory:end\r\n" in text
    assert "PYTHONPATH" in text                       # this devmem is not importable elsewhere (probe said so)
    if sys.version_info >= (3, 11):
        import tomllib
        d = tomllib.loads(text)
        assert d["model"] == "o5" and d["mcp_servers"]["other"]["command"] == "node"
        assert d["mcp_servers"]["devmemory"]["args"][:3] == ["-m", "devmem", "mcp"]
        assert Path(d["mcp_servers"]["devmemory"]["env"]["PYTHONPATH"], "devmem", "connect.py").is_file()
    disconnect(project, "codex")
    assert snapshot(project) == before


@pytest.mark.parametrize("existing", ['[mcp_servers.devmemory]\ncommand = "x"\n', 'mcp_servers = { devmemory = { command = "x" } }\n',
                                      '[mcp_servers]\ndevmemory.command = "x"\n', '[ mcp_servers . "devmemory" . env ]\nA = "1"\n',
                                      '"mcp_servers".devmemory.command = "x"\n', "'mcp_servers' . \"devmemory\" = {}\n"])
def test_codex_toml_that_already_defines_devmemory_is_not_touched(project, existing):
    cfg = project / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text(existing, encoding="utf-8")
    before = snapshot(project, skip_store=False)
    with pytest.raises(ConnectError, match="already defines"):
        do_connect(project, "codex")
    assert snapshot(project, skip_store=False) == before


@pytest.mark.parametrize("tool", JSON_TOOLS)
@pytest.mark.parametrize("bad", ['{"servers": {} // a comment\n}', "{not json", "[1, 2]", '{"%KEY%": []}'])
def test_malformed_config_is_left_untouched_and_nothing_is_written(project, tool, bad):
    t = C.TOOLS[tool]
    cfg = project.joinpath(*t.mcp_file.split("/"))
    cfg.parent.mkdir(parents=True)
    cfg.write_text(bad.replace("%KEY%", t.mcp_key), encoding="utf-8")
    (project / "AGENTS.md").write_text("keep\n", encoding="utf-8")
    before = snapshot(project, skip_store=False)
    with pytest.raises(ConnectError, match="not modified"):
        do_connect(project, tool)
    assert snapshot(project, skip_store=False) == before          # the instruction file was not written either


def test_unmatched_markers_are_an_error(project):
    (project / "AGENTS.md").write_text("<!-- devmemory:begin -->\nhalf a block\n", encoding="utf-8")
    with pytest.raises(ConnectError, match="marker"):
        do_connect(project, "cursor")
    assert not (project / ".cursor").exists()


def test_user_changes_after_connect_survive_disconnect(project):
    (project / "AGENTS.md").write_text("# mine\n", encoding="utf-8")
    do_connect(project, "cursor")
    mcp = project / ".cursor" / "mcp.json"
    d = json.loads(mcp.read_text(encoding="utf-8"))
    d["mcpServers"]["later"] = {"command": "x"}
    mcp.write_text(json.dumps(d), encoding="utf-8")
    with open(project / "AGENTS.md", "a", encoding="utf-8", newline="") as f:
        f.write("\nAdded by the user after connect.\n")
    disconnect(project, "cursor")
    assert json.loads(mcp.read_text(encoding="utf-8")) == {"mcpServers": {"later": {"command": "x"}}}
    agents = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert agents == "# mine\n\nAdded by the user after connect.\n"
    # the created mcp.json is the user's file now: a second connect backs it up before changing it
    r = do_connect(project, "cursor")
    assert r["changes"][0]["backup"]


def test_old_block_is_replaced_in_place(project):
    begin, _p, end = C.MARKERS["md"]
    (project / "AGENTS.md").write_text(f"top\n\n{begin}\nold text\n{end}\nbottom\n", encoding="utf-8")
    with pytest.raises(ConnectError, match="--force"):      # FG-2: no record that devmem wrote it
        do_connect(project, "cursor")
    do_connect(project, "cursor", force=True)
    text = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("top\n\n" + begin) and text.endswith(end + "\nbottom\n") and "old text" not in text
    assert text.count(begin) == 1


def test_shared_agents_md_block_is_kept_until_the_last_tool_disconnects(project):
    do_connect(project, "cursor")
    do_connect(project, "codex")
    do_connect(project, "gemini")
    assert (project / "AGENTS.md").read_text(encoding="utf-8").count("devmemory:begin") == 1
    r = disconnect(project, "cursor")
    assert any("kept" in n for n in r["notes"])
    assert "devmemory:begin" in (project / "AGENTS.md").read_text(encoding="utf-8")
    disconnect(project, "codex")
    assert not (project / "AGENTS.md").exists()
    assert (project / "GEMINI.md").exists()
    disconnect(project, "gemini")
    assert snapshot(project) == {}


def test_aliases_and_unknown_tools(project):
    assert C.find_tool("copilot").key == "vscode"
    assert C.find_tool("Windsurf").key == "devin"
    with pytest.raises(ConnectError, match="devmem init"):
        C.find_tool("claude-code")
    with pytest.raises(ConnectError, match="unknown tool"):
        C.find_tool("notepad")


def test_home_directory_is_refused(project, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: project))
    with pytest.raises(ConnectError, match="home directory"):
        do_connect(project, "gemini")
    assert snapshot(project, skip_store=False) == {}


def test_linked_folder_pointing_outside_is_refused(tmp_path, project):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, project / ".cursor", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a symbolic link here")
    with pytest.raises(ConnectError, match="outside the project"):
        do_connect(project, "cursor")
    assert list(outside.iterdir()) == []


# ---------------------------------------------------------------- print-only tools

def test_claude_desktop_prints_and_writes_no_tool_file(project):
    r = do_connect(project, "claude-desktop")
    assert r["changes"] == []
    title, text = r["printed"][0]
    assert "claude_desktop_config.json" in title
    entry = json.loads(text)["mcpServers"]["devmemory"]
    assert entry["args"] == ["mcp", "--project", str(project.resolve())]
    assert any("get_context_pack" in t for _, t in r["printed"])
    assert set(snapshot(project, skip_store=False)) == {".devmemory", ".devmemory/.gitignore", ".devmemory/connect.json"}
    r = disconnect(project, "claude-desktop")
    assert any("yourself" in n for n in r["notes"])


def test_generic_prints_standard_entry_and_writes_agents_md(project):
    r = do_connect(project, "generic", command="python", python=sys.executable, probe=lambda py: None)
    entry = json.loads(r["printed"][0][1])["mcpServers"]["devmemory"]
    assert entry["command"] == os.path.abspath(sys.executable)
    assert entry["args"] == ["-m", "devmem", "mcp", "--project", str(project.resolve())]
    assert "PYTHONPATH" in entry["env"]
    assert "devmemory:begin" in (project / "AGENTS.md").read_text(encoding="utf-8")
    assert [t["tool"] for t in C.connected_tools(project)] == ["generic"]
    disconnect(project, "generic")
    assert snapshot(project) == {}


def test_server_command_choice():
    cmd, lead, env, warn = C.server_command(None, which=lambda n: None, python=sys.executable, probe=lambda py: None)
    assert cmd == os.path.abspath(sys.executable) and lead == ["-m", "devmem"] and "PYTHONPATH" in env
    here = str(Path(C.__file__).resolve())
    cmd, lead, env, warn = C.server_command("python", which=fake_which, python=sys.executable, probe=lambda py: here)
    assert env == {}                                    # this devmem imports from anywhere: no PYTHONPATH needed
    assert C.server_command(None, which=fake_which)[0] == "devmem"
    assert C.server_command("devmem", which=lambda n: None)[3]       # forced but not on PATH: a warning


# ---------------------------------------------------------------- doctor

def test_connected_tools_flags_a_command_that_no_longer_resolves(project):
    do_connect(project, "cursor")
    do_connect(project, "codex", command="python", python=sys.executable, probe=lambda py: None)
    rows = {t["tool"]: t for t in C.connected_tools(project, which=lambda n: None)}
    assert "not on PATH" in rows["cursor"]["problem"]
    assert rows["codex"]["problem"] is None
    gem = project / ".gemini" / "settings.json"
    gem.parent.mkdir()
    gem.write_text(json.dumps({"mcpServers": {"devmemory": {"command": str(project / "gone" / "python.exe"),
                                                            "args": []}}}), encoding="utf-8")
    rows = {t["tool"]: t for t in C.connected_tools(project, which=fake_which)}
    assert "does not exist" in rows["gemini"]["problem"]


def test_doctor_reports_connected_tools(tmp_path):
    project = make_project(tmp_path)
    gem = project / ".gemini" / "settings.json"
    gem.parent.mkdir()
    gem.write_text(json.dumps({"mcpServers": {"devmemory": {"command": str(tmp_path / "missing" / "python"), "args": []}}}),
                   encoding="utf-8")
    connect(project, "cursor", command="python", python=sys.executable, probe=lambda py: None)
    r = run_cli(["doctor"], cwd=project)
    out = r.stdout.decode("utf-8", errors="replace")
    assert "connected" in out and "Cursor" in out
    assert "[FAIL] connected" in out and "Gemini CLI" in out and "does not exist" in out
    assert r.returncode == 1


def test_cli_connect_list_print_and_disconnect(tmp_path):
    project = tmp_path / ODD_NAME
    project.mkdir()
    r = run_cli(["connect"], cwd=project)
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and all(k in out for k in C.TOOLS)
    r = run_cli(["connect", "vscode", "--print", "--command", "devmem"], cwd=project)
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and '"servers"' in out and "${workspaceFolder}" in out and "nothing written" in out
    assert list(project.iterdir()) == []
    r = run_cli(["connect", "vscode", "--command", "devmem"], cwd=project)
    assert r.returncode == 0 and (project / ".vscode" / "mcp.json").is_file()
    r = run_cli(["disconnect", "copilot"], cwd=project)
    assert r.returncode == 0 and not (project / ".vscode").exists() and not (project / "AGENTS.md").exists()
    bad = project / ".vscode" / "mcp.json"
    bad.parent.mkdir()
    bad.write_text("{ // jsonc\n}", encoding="utf-8")
    r = run_cli(["connect", "vscode", "--command", "devmem"], cwd=project)
    assert r.returncode == 2 and b"not modified" in r.stderr


# ---------------------------------------------------------------- end to end: the written entry really starts

def test_written_cursor_entry_starts_a_working_mcp_server(tmp_path):
    """connect cursor, then start exactly the command it wrote (with ${workspaceFolder} replaced as Cursor documents)
    from a folder outside the project, and drive the MCP tools over stdio."""
    project = make_project(tmp_path, ODD_NAME)
    connect(project, "cursor", command="python")                # the real probe decides about PYTHONPATH
    (project / ".devmemory" / ".gitignore").unlink()            # the server must write it again itself
    entry = json.loads((project / ".cursor" / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["devmemory"]
    argv = [entry["command"], *[a.replace("${workspaceFolder}", str(project)) for a in entry["args"]]]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(entry.get("env") or {})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = subprocess.Popen(argv, cwd=elsewhere, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env)
    n = 0

    def rpc(method, params=None, notify=False):
        nonlocal n
        msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            n += 1
            msg["id"] = n
        proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        proc.stdin.flush()
        if notify:
            return None
        return json.loads(proc.stdout.readline().decode("utf-8"))

    try:
        init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "0"}})
        assert init["result"]["serverInfo"]["name"] == "devmemory"
        rpc("notifications/initialized", notify=True)
        names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
        assert {"get_context_pack", "begin_session", "end_session"} <= names
        pack = rpc("tools/call", {"name": "get_context_pack", "arguments": {"task": "fix negative_balance in wallet"}})
        assert pack["result"]["isError"] is False
        begin = rpc("tools/call", {"name": "begin_session", "arguments": {}})["result"]["content"][0]["text"]
        pending = begin.split("pending id: ")[1].split()[0]
        (project / "src" / "wallet.py").write_text("def negative_balance(amount):\n    return -abs(amount) or 0\n",
                                                   encoding="utf-8")
        end = rpc("tools/call", {"name": "end_session", "arguments": {
            "prompt": "fix negative_balance", "pending_id": pending, "summary": "returned 0 for zero", "client": "cursor"}})
        assert end["result"]["isError"] is False, end
        assert "1 files changed" in end["result"]["content"][0]["text"]
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
        proc.stdout.close()
        proc.stderr.close()
    metas = list((project / ".devmemory" / "sessions").glob("*.meta.json"))
    assert len(metas) == 1
    rec = json.loads(metas[0].read_text(encoding="utf-8"))
    assert rec["provider"] == "cursor" and rec["files_edited"] == ["src/wallet.py"]
    assert rec["turns"][0]["prompt_source"] == "reported-by-client"
    assert (project / ".devmemory" / ".gitignore").is_file()


# ---------------------------------------------------------------- clipboard

class FakeRunner:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, argv, input=None, **kw):
        self.calls.append((argv, input))
        return subprocess.CompletedProcess(argv, self.returncode, b"", b"boom" if self.returncode else b"")


def test_clipboard_command_per_platform():
    assert clipboard.clipboard_command("win32", fake_which) == (["/usr/bin/clip"], "utf-16")
    assert clipboard.clipboard_command("darwin", fake_which) == (["/usr/bin/pbcopy"], "utf-8")
    assert clipboard.clipboard_command("linux", fake_which, {"WAYLAND_DISPLAY": "w-0"})[0] == ["/usr/bin/wl-copy"]
    assert clipboard.clipboard_command("linux", fake_which, {})[0] == ["/usr/bin/xclip", "-selection", "clipboard"]
    only_xsel = lambda n: "/bin/xsel" if n == "xsel" else None  # noqa: E731
    assert clipboard.clipboard_command("linux", only_xsel, {})[0] == ["/bin/xsel", "--clipboard", "--input"]
    for plat in ("win32", "darwin", "linux"):
        with pytest.raises(clipboard.ClipboardError):
            clipboard.clipboard_command(plat, lambda n: None, {})


def test_clipboard_copy_is_utf8_safe_on_windows():
    text = "pack: إصلاح الرصيد — ünïcode ✓\nline 2\n"
    run = FakeRunner()
    assert clipboard.copy(text, runner=run, platform="win32", which=fake_which, environ={}) == "clip"
    argv, data = run.calls[0]
    assert data[:2] == b"\xff\xfe"
    assert data[2:].decode("utf-16-le") == text.replace("\n", "\r\n")
    run = FakeRunner()
    clipboard.copy(text, runner=run, platform="darwin", which=fake_which, environ={})
    assert run.calls[0][1].decode("utf-8") == text
    with pytest.raises(clipboard.ClipboardError, match="exited with 1"):
        clipboard.copy(text, runner=FakeRunner(1), platform="linux", which=fake_which, environ={})


def _cli(argv, capsys):
    from devmem.cli import main
    code = main(argv)
    return code, capsys.readouterr()


def test_pack_copy(tmp_path, monkeypatch, capsys):
    project = make_project(tmp_path)
    (project / ".devmemory").mkdir()
    from devmem.consent import claim_store
    claim_store(project / ".devmemory")       # the user's own store (devmem notes one it creates)
    import devmem.pack
    real_copy = clipboard.copy
    run = FakeRunner()
    monkeypatch.setattr(clipboard, "copy", lambda text: real_copy(text, runner=run, which=fake_which))

    monkeypatch.setattr(devmem.pack, "build_pack", lambda *a, **k: ("", {"empty_reason": "no retrieval evidence"}))
    code, out = _cli(["pack", "thanks", "--copy", "-p", str(project)], capsys)
    assert code == 0 and "nothing copied" in out.out and "no retrieval evidence" in out.out and run.calls == []

    monkeypatch.setattr(devmem.pack, "build_pack", lambda *a, **k: ("## pack ✓\n", {"tokens_est": 3}))
    code, out = _cli(["pack", "fix it", "--copy", "-p", str(project)], capsys)
    assert code == 0 and "copied the pack" in out.out and "## pack" not in out.out and len(run.calls) == 1

    monkeypatch.setattr(clipboard, "copy", lambda text: real_copy(text, runner=run, which=lambda n: None))
    code, out = _cli(["pack", "fix it", "--copy", "-p", str(project)], capsys)
    assert code == 2 and "could not copy" in out.err


def test_pack_copy_empty_pack_end_to_end(tmp_path, monkeypatch, capsys):
    """Real scan and retrieval; the clipboard is a stand-in, so a pack that is not empty after all would land in
    `copied`, not on the developer's clipboard."""
    project = make_project(tmp_path)
    assert run_cli(["scan"], cwd=project, env=cli_env()).returncode == 0
    copied = []
    monkeypatch.setattr(clipboard, "copy", lambda text: copied.append(text) or "fake")
    code, out = _cli(["pack", "thanks", "--copy", "-p", str(project)], capsys)
    assert code == 0 and "nothing copied" in out.out and copied == []


# ---------------------------------------------------------------- review fixes

def _symlink(target, link):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a symbolic link here")


def test_symlinked_instruction_file_is_written_through_and_the_link_survives(project):
    (project / "CLAUDE.md").write_text("# shared rules\n", encoding="utf-8")
    _symlink("CLAUDE.md", project / "AGENTS.md")
    before = snapshot(project)
    r = do_connect(project, "cursor")
    assert (project / "AGENTS.md").is_symlink()
    assert "devmemory:begin" in (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert any("link" in n and "CLAUDE.md" in n for n in r["notes"])
    disconnect(project, "cursor")
    assert (project / "AGENTS.md").is_symlink() and snapshot(project) == before
    assert backups(project) == []


def test_symlinked_instruction_file_pointing_outside_is_refused(tmp_path, project):
    outside = tmp_path / "outside.md"
    outside.write_text("# not yours\n", encoding="utf-8")
    _symlink(outside, project / "AGENTS.md")
    with pytest.raises(ConnectError, match="outside the project"):
        do_connect(project, "cursor")
    assert outside.read_text(encoding="utf-8") == "# not yours\n" and not (project / ".cursor").exists()


def _fail_replace_for(monkeypatch, target: Path):
    """Writing `target` (the project file itself, not its backup in the store) fails as for a locked file."""
    real = os.replace
    calls = []

    def fake(src, dst, *a, **k):
        calls.append(Path(dst))
        if Path(dst).resolve() == target.resolve():
            raise PermissionError(13, "Access is denied", str(dst))
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "replace", fake)
    return calls


def test_failed_write_rolls_back_and_leaves_the_project_as_it_was(project, monkeypatch):
    (project / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    before = snapshot(project, skip_store=False)
    calls = _fail_replace_for(monkeypatch, project / "AGENTS.md")
    with pytest.raises(ConnectError, match="cannot write.*nothing was changed"):
        do_connect(project, "cursor")
    written = [p.name for p in calls]
    assert "mcp.json" in written and "AGENTS.md" in written and "connect.json" in written   # really got that far
    assert snapshot(project, skip_store=False) == before          # no store, backup, temp file or folder either


def test_failed_write_keeps_an_existing_state_file_as_it_was(project, monkeypatch):
    (project / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    do_connect(project, "gemini")
    before = snapshot(project, skip_store=False)
    calls = _fail_replace_for(monkeypatch, project / "AGENTS.md")
    with pytest.raises(ConnectError, match="nothing was changed"):
        do_connect(project, "cursor")
    assert "mcp.json" in [p.name for p in calls]
    assert snapshot(project, skip_store=False) == before


def test_failed_disconnect_rolls_back_and_a_later_disconnect_works(project, monkeypatch):
    (project / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    (project / ".cursor").mkdir()
    (project / ".cursor" / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    original = snapshot(project)
    do_connect(project, "cursor")
    connected = snapshot(project, skip_store=False)
    calls = _fail_replace_for(monkeypatch, project / "AGENTS.md")
    with pytest.raises(ConnectError, match="nothing was changed"):
        disconnect(project, "cursor")
    assert "mcp.json" in [p.name for p in calls]                # restored first, then put back
    assert snapshot(project, skip_store=False) == connected
    user_data = os.environ["DEVMEM_USER_DATA"]
    monkeypatch.undo()                   # keeps the test's per-user folder (the store is the user's)
    monkeypatch.setenv("DEVMEM_USER_DATA", user_data)
    disconnect(project, "cursor")
    assert snapshot(project) == original and backups(project) == []


def test_failed_rollback_leaves_state_for_a_later_disconnect(project, monkeypatch):
    (project / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    before = snapshot(project)
    calls = _fail_replace_for(monkeypatch, project / "AGENTS.md")
    real_unlink = Path.unlink

    def unlink(self, *a, **k):
        if self.name == "mcp.json":
            raise PermissionError(13, "Access is denied", str(self))
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(ConnectError, match="could not undo"):
        do_connect(project, "cursor")
    assert "mcp.json" in [p.name for p in calls]
    user_data = os.environ["DEVMEM_USER_DATA"]
    monkeypatch.undo()                   # keeps the test's per-user folder (the store is the user's)
    monkeypatch.setenv("DEVMEM_USER_DATA", user_data)
    assert "cursor" in C.read_state(project)["tools"]
    disconnect(project, "cursor")
    assert snapshot(project) == before


@pytest.mark.skipif(os.name != "nt", reason="the read-only attribute blocks a replace only on Windows")
def test_read_only_instruction_file_cli_message_and_nothing_changed(tmp_path):
    import stat
    project = tmp_path / ODD_NAME
    project.mkdir()
    agents = project / "AGENTS.md"
    agents.write_text("# rules\n", encoding="utf-8")
    os.chmod(agents, stat.S_IREAD)
    try:
        before = snapshot(project, skip_store=False)
        r = run_cli(["connect", "cursor", "--command", "devmem"], cwd=project)
        assert r.returncode == 2 and b"Traceback" not in r.stderr and b"nothing was changed" in r.stderr
        assert snapshot(project, skip_store=False) == before
    finally:
        os.chmod(agents, stat.S_IREAD | stat.S_IWRITE)


def test_user_reformatting_and_values_survive_disconnect(project):
    mcp = project / ".cursor" / "mcp.json"
    mcp.parent.mkdir()
    mcp.write_text('{"mcpServers": {"a": {"command": "x", "disabled": 1}}}', encoding="utf-8")
    do_connect(project, "cursor")
    d = json.loads(mcp.read_text(encoding="utf-8"))
    d["mcpServers"]["a"]["disabled"] = True
    mcp.write_text(json.dumps(d, indent=8) + "\n", encoding="utf-8")          # the user's style and value
    r = disconnect(project, "cursor")
    assert r["changes"][0]["verb"].startswith("remove")
    assert mcp.read_text(encoding="utf-8") == json.dumps({"mcpServers": {"a": {"command": "x", "disabled": True}}},
                                                         indent=8) + "\n"
    assert backups(project) == []


def test_one_line_json_edited_by_the_user_stays_one_line(project):
    mcp = project / ".cursor" / "mcp.json"
    mcp.parent.mkdir()
    mcp.write_text('{"mcpServers": {"a": {"command": "x", "disabled": 1}}}', encoding="utf-8")
    do_connect(project, "cursor")
    d = json.loads(mcp.read_text(encoding="utf-8"))
    d["mcpServers"]["a"]["disabled"] = True
    mcp.write_text(json.dumps(d), encoding="utf-8")
    disconnect(project, "cursor")
    assert mcp.read_text(encoding="utf-8") == '{"mcpServers": {"a": {"command": "x", "disabled": true}}}'


def test_user_edit_between_two_connects_survives_disconnect(project):
    mcp = project / ".cursor" / "mcp.json"
    mcp.parent.mkdir()
    mcp.write_text('{"mcpServers": {}}', encoding="utf-8")
    do_connect(project, "cursor")
    d = json.loads(mcp.read_text(encoding="utf-8"))
    d["mcpServers"]["mine"] = {"command": "y"}
    mcp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    do_connect(project, "cursor", command="python", python=sys.executable, probe=lambda py: None)   # rewrites it
    disconnect(project, "cursor")
    assert json.loads(mcp.read_text(encoding="utf-8")) == {"mcpServers": {"mine": {"command": "y"}}}
    assert backups(project) == []


def test_text_block_removed_from_a_file_without_final_newline_after_user_edit(project):
    (project / "AGENTS.md").write_bytes(b"# rules\r\nlast line")
    do_connect(project, "cursor")
    agents = project / "AGENTS.md"
    agents.write_bytes(b"# rules, edited\r\n" + agents.read_bytes().split(b"\r\n", 1)[1])
    disconnect(project, "cursor")
    assert agents.read_bytes() == b"# rules, edited\r\nlast line"


def test_old_style_backups_next_to_the_files_are_removed_on_disconnect(project):
    """A project connected by the previous version: backups as <file>.devmem-backup, no hashes in the state."""
    (project / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    before = snapshot(project)
    do_connect(project, "cursor")
    state = C.read_state(project)
    state.pop("written", None)
    C._write_state(project, state)
    for b in backups(project):
        b.unlink()
    (project / "AGENTS.md.devmem-backup").write_text("# rules\n", encoding="utf-8")
    disconnect(project, "cursor")
    assert snapshot(project) == before


def test_backup_names_do_not_collide():
    names = {C._backup_name(k) for k in ["AGENTS.md", ".cursor/mcp.json", ".cursor%2Fmcp.json", ".cursor_mcp.json"]}
    assert len(names) == 4 and not any("/" in n for n in names)


def test_machine_specific_paths_are_flagged_for_every_tool(project):
    r = do_connect(project, "cursor", dry_run=True)                       # `devmem` and ${workspaceFolder}
    assert not any("machine-specific" in n for n in r["notes"])
    for tool in C.TOOLS:
        r = do_connect(project, tool, command="python", python=sys.executable, probe=lambda py: None, dry_run=True)
        notes = [n for n in r["notes"] if "machine-specific" in n]
        assert notes and f"devmem connect {tool}" in notes[0], tool
        if C.TOOLS[tool].mcp_file:
            assert "commit" in notes[0]
    for tool in ("codex", "gemini", "devin", "claude-desktop", "generic"):  # absolute project path
        assert any("machine-specific" in n for n in do_connect(project, tool, dry_run=True)["notes"]), tool


@pytest.mark.parametrize("name", ["dollar$HOME", "brace${HOME}", "pct%PATH%"])
def test_gemini_refuses_values_it_would_expand(tmp_path, name):
    p = tmp_path / name
    p.mkdir()
    with pytest.raises(ConnectError, match="Gemini"):
        do_connect(p, "gemini")
    assert list(p.iterdir()) == []
    do_connect(p, "cursor", dry_run=True)                              # other tools do not expand: fine
    q = tmp_path / "plain"
    q.mkdir()
    with pytest.raises(ConnectError, match="Gemini"):
        do_connect(q, "gemini", store=p / "st", dry_run=True)


TRICKY = ["C:\\x$HOME %PATH% q'uote `n ${env:USERNAME} $(whoami) ;& | @a,b \u2018x\u2019", "PYTHONPATH=C:\\a b"]


def test_codex_add_lines_quote_every_shell_safely():
    import shlex
    entry = {"command": "devmem", "args": ["mcp", "--project", TRICKY[0] + ' "dq" \\'], "env": {"PYTHONPATH": "/a $b"}}
    text = C.codex_add_command(entry)
    lines = dict(ln.split(": ", 1) for ln in text.strip().splitlines() if ": codex" in ln)
    posix = next(v for k, v in lines.items() if "POSIX" in k)
    assert shlex.split(posix) == ["codex", "mcp", "add", "devmemory", "--env", "PYTHONPATH=/a $b", "--", "devmem",
                                  *entry["args"]]
    assert any("PowerShell" in k for k in lines) and "cmd.exe" in text
    assert '"' not in C.ps_quote("a$b") and C.ps_quote("it's") == "'it''s'" and C.ps_quote("\u2019") == "'\u2019\u2019'"


@pytest.mark.skipif(not __import__("shutil").which("sh"), reason="no POSIX shell")
def test_posix_quoting_in_a_real_shell():
    import shutil
    words = ["/p/dollar$HOME back`tick` q'uote \"dq\" %PATH% $(id) ;&|<>*?~ \\bs", "new\nline", "--env", "-m"]
    out = subprocess.run([shutil.which("sh") or "sh", "-c", "printf '%s\\0' " + " ".join(C.posix_quote(w) for w in words)],
                         capture_output=True, timeout=30).stdout
    assert out.split(b"\0")[:-1] == [w.encode() for w in words]


@pytest.mark.skipif(os.name != "nt" or not __import__("shutil").which("powershell"), reason="Windows PowerShell")
def test_powershell_quoting_in_a_real_shell():
    import base64
    words = [*TRICKY, "--", "--env", "-m"]
    prog = "import sys,json;print(json.dumps(sys.argv[1:]))"
    script = "& " + " ".join(C.ps_quote(w) for w in [sys.executable, "-c", prog, *words])
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], capture_output=True, timeout=60)
    assert json.loads(r.stdout.decode("utf-8")) == words, r.stderr


@pytest.mark.parametrize("s", ["del\x7fchar", "nul\x00", "esc\x1b", 'q"uote', "back\\slash", "tab\tnl\n", "uni é 😀"])
def test_toml_strings_escape_everything_toml_forbids(s):
    entry = {"command": s, "args": ["mcp", s], "env": {"PYTHONPATH": s}}
    block = C.toml_block(entry)
    assert not any(ord(ch) < 0x20 and ch != "\n" or ord(ch) == 0x7F for ch in block)
    if sys.version_info >= (3, 11):
        import tomllib
        assert tomllib.loads(block)["mcp_servers"]["devmemory"] == entry


def test_codex_entry_with_a_del_character_reads_back(project):
    entry = {"command": "py\x7fthon", "args": ["mcp"], "env": {"PYTHONPATH": "a\x7fb"}}
    cfg = project / ".codex" / "config.toml"
    cfg.parent.mkdir()
    begin, _p, end = C.MARKERS["toml"]
    cfg.write_text(f"{begin}\n{C.toml_block(entry)}{end}\n", encoding="utf-8")
    assert C.mcp_entry(project, C.TOOLS["codex"]) == {"command": "py\x7fthon", "env": {"PYTHONPATH": "a\x7fb"}}


def test_codex_inline_mcp_servers_table_is_refused(project):
    cfg = project / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('mcp_servers = { other = { command = "x" } }\n', encoding="utf-8")
    before = snapshot(project, skip_store=False)
    with pytest.raises(ConnectError, match="not modified"):
        do_connect(project, "codex")
    assert snapshot(project, skip_store=False) == before


@pytest.mark.skipif(sys.version_info < (3, 11), reason="needs tomllib")
def test_codex_toml_that_only_mentions_the_table_in_a_string_is_connected(project):
    cfg = project / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('notes = """\n[mcp_servers.devmemory]\n"""\n', encoding="utf-8")
    do_connect(project, "codex")
    import tomllib
    d = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert d["mcp_servers"]["devmemory"]["command"] == "devmem" and "[mcp_servers.devmemory]" in d["notes"]


def test_instruction_block_names_every_recorder_and_the_mcp_guard(tmp_path, monkeypatch):
    assert "devmem codex" in C.INSTRUCTIONS and "devmem wrap" in C.INSTRUCTIONS
    from devmem.mcp_server import Server
    project = make_project(tmp_path)
    srv = Server(project, project / ".devmemory")
    monkeypatch.setenv("DEVMEM_NO_CAPTURE", "1")
    for name, args in (("begin_session", {}), ("end_session", {"prompt": "x"})):
        assert "already" in srv.call(name, args)
    assert not list((project / ".devmemory").glob("pending/*")) and not (project / ".devmemory" / "sessions").exists()
    monkeypatch.delenv("DEVMEM_NO_CAPTURE")
    assert "pending id" in srv.call("begin_session", {})


def test_devmem_codex_mcp_tells_its_server_the_run_is_recorded(tmp_path, monkeypatch):
    from devmem import generic, store
    project = make_project(tmp_path)
    fake_bin = tmp_path / "bin"                           # a `codex` on PATH (never started: run is faked below)
    fake_bin.mkdir()
    exe = fake_bin / ("codex.exe" if os.name == "nt" else "codex")   # an .exe: a .bat gets no quotes (B1)
    exe.write_text("@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
    store._PROGRAMS.clear()
    seen = {}
    from devmem import proc

    def fake_run(cmd, **kw):                             # devmem runs codex through proc.run_capped
        seen["cmd"], seen["env"] = cmd, kw.get("env")
        return proc.Captured(0, ("", "", 0), ("", "", 0))
    monkeypatch.setattr(proc, "run_capped", fake_run)
    generic.codex(project, "do it", store=project / ".devmemory", mcp=True, with_pack=False)
    env_override = next(c for c in seen["cmd"] if str(c).startswith("mcp_servers.devmemory.env="))
    assert 'DEVMEM_NO_CAPTURE="1"' in env_override
    assert (seen["env"] or {}).get("DEVMEM_NO_CAPTURE") == "1"


def test_drive_root_and_folders_above_home_are_refused(project, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: project / "users" / "me"))
    with pytest.raises(ConnectError, match="home"):
        do_connect(project, "gemini", dry_run=True)
    with pytest.raises(ConnectError, match="root"):
        do_connect(Path(project.anchor), "gemini", dry_run=True)
    assert snapshot(project, skip_store=False) == {}


def test_clipboard_encoding_replaces_a_lone_surrogate():
    for enc in ("utf-16", "utf-8"):
        data = clipboard.encode_for("a \udc80 b \U0001F600", enc)
        text = data[2:].decode("utf-16-le") if enc == "utf-16" else data.decode("utf-8")
        assert text == "a \ufffd b \U0001F600"


HOLDER = """import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(25)"], stdout=sys.stdout, stderr=sys.stderr)
open(sys.argv[1], "w").write(str(child.pid))
sys.stdin.read()
"""


def test_clipboard_does_not_wait_for_a_forked_child_holding_the_output(tmp_path):
    """xclip, xsel and wl-copy fork a child that keeps serving the selection: copy() must return when the
    command itself exits, not when the child closes inherited pipes."""
    import signal
    import time
    script, pidfile = tmp_path / "holder.py", tmp_path / "pid"
    script.write_text(HOLDER, encoding="utf-8")
    runner = lambda argv, **kw: subprocess.run([sys.executable, str(script), str(pidfile)], **kw)  # noqa: E731
    t = time.time()
    try:
        assert clipboard.copy("text", runner=runner, platform="linux", which=fake_which, environ=None) == "xclip"
        assert time.time() - t < 10
    finally:
        try:
            os.kill(int(pidfile.read_text()), signal.SIGTERM)
        except (OSError, ValueError):
            pass


def test_clipboard_failure_message_comes_from_the_command(tmp_path):
    prog = "import sys; sys.stdin.read(); sys.stderr.write('Error: Can not open display'); sys.exit(1)"
    runner = lambda argv, **kw: subprocess.run([sys.executable, "-c", prog], **kw)  # noqa: E731
    with pytest.raises(clipboard.ClipboardError, match="exited with 1: Error: Can not open display"):
        clipboard.copy("text", runner=runner, platform="linux", which=fake_which, environ=None)
