"""Every path that creates the store also writes <store>/.gitignore, so memory is never committed by accident."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from cli_helpers import cli_env, make_project, run_cli
from ui_helpers import request, running_server, with_token

from devmem.index import Index
from devmem.mcp_server import Server
from devmem.scan import scan
from devmem.store import ensure_store

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git_repo(tmp_path: Path) -> Path:
    project = make_project(tmp_path)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"],
                   check=True)
    return project


def porcelain(project: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(project), "status", "--porcelain", "--untracked-files=all"],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ".devmemory" in ln]


# ---------------------------------------------------------------- ensure_store

def test_ensure_store_creates_folder_and_ignore_file(tmp_path):
    s = tmp_path / "a" / ".devmemory"
    assert ensure_store(s) == s
    assert (s / ".gitignore").read_text(encoding="utf-8").splitlines()[-1] == "*"


def test_ensure_store_is_idempotent_and_never_overwrites(tmp_path):
    s = tmp_path / ".devmemory"
    s.mkdir()
    (s / ".gitignore").write_text("# mine\n*\n", encoding="utf-8")
    ensure_store(s)
    ensure_store(s)
    assert (s / ".gitignore").read_text(encoding="utf-8") == "# mine\n*\n"


@pytest.mark.parametrize("shipped", ["# nothing ignored\n", "", "mine", "*\n!sessions/\n!sessions/**\n", " *\n"])
def test_ensure_store_makes_a_shipped_gitignore_ignore_everything(tmp_path, shipped):
    """A repository can ship .devmemory/.gitignore holding only a comment (or re-including files): the store's
    recordings would then be visible to `git add -A`. ensure_store appends `*` and keeps the rest."""
    s = tmp_path / ".devmemory"
    s.mkdir()
    (s / ".gitignore").write_text(shipped, encoding="utf-8")
    ensure_store(s)
    text = (s / ".gitignore").read_text(encoding="utf-8")
    assert text.startswith(shipped) and text.rstrip("\n").splitlines()[-1] == "*"
    ensure_store(s)
    assert (s / ".gitignore").read_text(encoding="utf-8") == text          # idempotent


def _symlink(link: Path, target: Path, is_dir: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as e:      # Windows without Developer Mode or the privilege
        pytest.skip(f"cannot create a symbolic link here: {e}")


def test_ensure_store_never_writes_through_a_linked_gitignore(tmp_path):
    """A repository can ship .devmemory/.gitignore as a link to a file elsewhere; appending `*` through it would change
    that file (and git does not read a linked .gitignore anyway). The link itself is replaced by the standard file."""
    victim = tmp_path / "victim" / "ignore"
    victim.parent.mkdir()
    victim.write_text("victim-original\n", encoding="utf-8")
    s = tmp_path / "proj" / ".devmemory"
    s.mkdir(parents=True)
    _symlink(s / ".gitignore", victim)
    ensure_store(s)
    assert victim.read_text(encoding="utf-8") == "victim-original\n"
    assert not (s / ".gitignore").is_symlink() and (s / ".gitignore").read_text(encoding="utf-8").endswith("*\n")


def test_a_gitignore_that_is_a_folder_is_left_alone_and_setup_and_init_refuse(tmp_path, capsys):
    from devmem.cli import main as cli_main
    project = make_project(tmp_path)
    (project / ".devmemory" / ".gitignore").mkdir(parents=True)
    ensure_store(project / ".devmemory")
    assert (project / ".devmemory" / ".gitignore").is_dir()
    for args in (["setup", "--plugin-data", str(tmp_path / "data")], ["init"]):
        assert cli_main([*args, "--project", str(project), "--no-scan", "--adopt-store"]) == 2
        assert "not a regular file" in capsys.readouterr().err
    cli_main(["doctor", "--project", str(project)])
    assert "store may be committed" in capsys.readouterr().out
    assert not (project / ".claude").exists()


def test_a_store_holding_links_is_refused_by_every_command_that_writes_it(tmp_path, capsys):
    from devmem.cli import main as cli_main
    victim = tmp_path / "victim.md"
    victim.write_text("victim-original\n", encoding="utf-8")
    project = make_project(tmp_path)
    (project / ".devmemory").mkdir()
    _symlink(project / ".devmemory" / "PROJECT.md", victim)
    for args in (["scan"], ["init", "--no-scan", "--adopt-store"],
                 ["setup", "--no-scan", "--adopt-store", "--plugin-data", str(tmp_path / "data")]):
        assert cli_main([*args, "--project", str(project)]) == 2, args
        assert "symbolic links" in capsys.readouterr().err
    assert victim.read_text(encoding="utf-8") == "victim-original\n"
    cli_main(["doctor", "--project", str(project)])
    assert "[FAIL] links" in capsys.readouterr().out


@needs_git
def test_cli_scan_on_a_cloned_repository_with_a_shipped_store_leaves_nothing_to_commit(tmp_path):
    project = git_repo(tmp_path)
    (project / ".devmemory").mkdir()
    (project / ".devmemory" / ".gitignore").write_text("# nothing ignored\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "s"],
                   check=True)
    r = run_cli(["scan", "--project", str(project)], cwd=project)
    assert r.returncode == 0, r.stderr
    assert [ln for ln in porcelain(project) if not ln.endswith(".gitignore")] == []


def test_ensure_store_does_not_raise_when_the_ignore_file_cannot_be_written(tmp_path, monkeypatch):
    s = tmp_path / ".devmemory"

    def boom(*a, **k):
        raise PermissionError("read-only store")
    monkeypatch.setattr(Path, "write_text", boom)
    assert ensure_store(s) == s and s.is_dir()
    monkeypatch.setattr(Path, "mkdir", boom)
    assert ensure_store(tmp_path / "other") == tmp_path / "other"


# ---------------------------------------------------------------- every creator, in a real git repo

@needs_git
def test_scan_and_index_leave_nothing_to_commit(tmp_path):
    project = git_repo(tmp_path)
    scan(project)
    assert porcelain(project) == []
    p2 = git_repo(tmp_path / "two")
    with Index(p2 / ".devmemory"):
        pass
    assert porcelain(p2) == []


@needs_git
def test_mcp_begin_and_end_session_leave_nothing_to_commit(tmp_path):
    project = git_repo(tmp_path)
    server = Server(project)
    begin = server.call("begin_session", {})
    pid = begin.split("pending id:")[1].split()[0]
    (project / "src" / "wallet.py").write_text("x = 1\n", encoding="utf-8")
    server.call("end_session", {"prompt": "change wallet", "pending_id": pid})
    assert (project / ".devmemory" / "sessions").is_dir()
    assert porcelain(project) == []


@needs_git
def test_documented_mcp_setup_over_stdio_leaves_nothing_to_commit(tmp_path):
    project = git_repo(tmp_path)
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "begin_session", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "end_session",
                                                                      "arguments": {"prompt": "secret plan"}}},
    ]
    r = subprocess.run([sys.executable, "-m", "devmem", "mcp", "--project", str(project)], cwd=project,
                       input=("\n".join(json.dumps(m) for m in msgs) + "\n").encode(), capture_output=True,
                       env=cli_env(), timeout=120)
    assert r.returncode == 0, r.stderr.decode()
    assert (project / ".devmemory").is_dir()
    assert porcelain(project) == []


@needs_git
def test_cli_scan_import_begin_end_leave_nothing_to_commit(tmp_path):
    project = git_repo(tmp_path)
    assert run_cli(["scan", "--project", str(project)], project).returncode == 0
    assert porcelain(project) == []
    p2 = git_repo(tmp_path / "two")
    assert run_cli(["begin", "--project", str(p2)], p2).returncode == 0
    assert porcelain(p2) == []
    stream = p2 / "stream.jsonl"
    stream.write_text('{"type":"system","subtype":"init","session_id":"s1"}\n', encoding="utf-8")
    p3 = git_repo(tmp_path / "three")
    r = run_cli(["import", str(stream), "--project", str(p3), "--prompt", "hello"], p3)
    assert r.returncode == 0, r.stderr.decode()
    assert porcelain(p3) == []


@needs_git
def test_app_open_with_scan_leaves_nothing_to_commit(tmp_path):
    project = git_repo(tmp_path)
    with running_server(None, None) as (port, st):
        status, _, body = request(port, "POST", "/api/open?scan=1&project=" + str(project).replace("\\", "/"),
                                  with_token(st))
        assert status == 200, body
    assert (project / ".devmemory" / "index").is_dir()
    assert porcelain(project) == []
