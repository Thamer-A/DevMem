"""`init_project` / `uninstall`: idempotent merge into .claude/settings.json, store .gitignore, scan."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from devmem import init as I

from hooks_helpers import load_payload, make_project

EXISTING = {
    "permissions": {"allow": ["Bash(npm test)"]},
    "env": {"FOO": "1"},
    "hooks": {
        "PostToolUse": [{"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "prettier --write"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "devmem hook"}]}],  # v1 install
        "Notification": [{"hooks": [{"type": "command", "command": "notify-send hi"}]}],
    },
}


def _settings(project: Path) -> dict:
    return json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _write_settings(project: Path, data: dict) -> None:
    (project / ".claude").mkdir(exist_ok=True)
    (project / ".claude" / "settings.json").write_text(json.dumps(data, indent=4), encoding="utf-8")


def test_init_preserves_existing_settings_and_is_idempotent(tmp_path):
    project = make_project(tmp_path, init_store=False)
    _write_settings(project, EXISTING)
    rep = I.init_project(project, scan=False)
    assert rep["settings_changed"] and rep["gitignore"] == "written"
    s = _settings(project)
    assert s["permissions"] == EXISTING["permissions"] and s["env"] == EXISTING["env"]
    assert s["hooks"]["Notification"] == EXISTING["hooks"]["Notification"]
    post = s["hooks"]["PostToolUse"]
    assert post[0] == EXISTING["hooks"]["PostToolUse"][0] and I.is_devmem_handler(post[1]["hooks"][0])
    ups = s["hooks"]["UserPromptSubmit"]
    assert len(ups) == 1 and I.handler_paths(ups[0]["hooks"][0])["event"] == "UserPromptSubmit"  # v1 replaced
    for ev in I.HOOK_EVENTS:
        assert sum(I.is_devmem_handler(h) for g in s["hooks"][ev] for h in g["hooks"]) == 1
    assert (project / ".devmemory" / "init-backups" / "settings.json").exists()        # in the store: never committed
    assert not (project / ".claude" / "settings.json.devmem-backup").exists()
    before = (project / ".claude" / "settings.json").read_bytes()
    rep2 = I.init_project(project, scan=False)
    assert rep2["settings_changed"] is False and rep2["gitignore"] == "kept existing"
    assert (project / ".claude" / "settings.json").read_bytes() == before


def test_uninstall_removes_only_ours(tmp_path):
    project = make_project(tmp_path, init_store=False)
    _write_settings(project, EXISTING)
    I.init_project(project, scan=False)
    rep = I.uninstall(project)
    assert rep["settings_changed"]
    s = _settings(project)
    assert s["permissions"] == EXISTING["permissions"]
    assert s["hooks"] == {"PostToolUse": EXISTING["hooks"]["PostToolUse"],
                          "Notification": EXISTING["hooks"]["Notification"]}
    assert I.uninstall(project)["settings_changed"] is False
    assert (project / ".devmemory").is_dir()  # memory itself is kept


def test_init_without_settings_file_and_gitignore_effect(tmp_path):
    project = make_project(tmp_path, init_store=False)
    rep = I.init_project(project, scan=False)
    assert set(_settings(project)["hooks"]) == set(I.HOOK_EVENTS)
    assert "backup" not in rep
    gi = (project / ".devmemory" / ".gitignore").read_text(encoding="utf-8")
    assert "*" in gi.splitlines()
    (project / ".devmemory" / "sessions").mkdir()
    (project / ".devmemory" / "sessions" / "x.meta.json").write_text("{}", encoding="utf-8")
    status = subprocess.run(["git", "-C", str(project), "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, text=True).stdout
    assert ".devmemory" not in status


def test_invalid_settings_are_not_touched(tmp_path):
    project = make_project(tmp_path, init_store=False)
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError):
        I.init_project(project, scan=False)
    assert (project / ".claude" / "settings.json").read_text(encoding="utf-8") == "{ not json"


def test_refuses_home_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(ValueError, match="home"):
        I.init_project(tmp_path, scan=False)


def test_installed_exec_form_command_runs_from_a_path_with_spaces(tmp_path):
    """Spawn the handler exactly as Claude Code's exec form does: command + args, no shell."""
    project = make_project(tmp_path, init_store=False)
    I.init_project(project, scan=False)
    (project / ".devmemory" / "config.json").write_text('{"deliver": false}', encoding="utf-8")
    h = _settings(project)["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert h["type"] == "command" and Path(h["command"]).is_absolute() and h["args"][0] == "-c"
    assert Path(h["args"][2]).is_absolute() and I.handler_paths(h)["shape"] == "bootstrap"
    (project / "json.py").write_text("raise SystemExit(2)\n", encoding="utf-8")  # the project is not on sys.path
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["CLAUDE_PROJECT_DIR"] = str(project)
    payload = load_payload("002_UserPromptSubmit", project)
    p = subprocess.run([h["command"], *h["args"]], input=json.dumps(payload).encode("utf-8"), cwd=project,
                       env=env, capture_output=True, timeout=60)
    assert p.returncode == 0 and not (project / ".devmemory" / "hook_errors.log").exists()
    assert list((project / ".devmemory" / "live").glob("*.jsonl"))


def test_shell_forms_quote_paths():
    h = I.hook_handler("Stop", python="C:/Program Files/Py/python.exe", form="powershell")
    assert h["command"].startswith("& '") and "Program Files" in h["command"].split("' '")[0]
    assert h["command"].endswith("' Stop") and h["shell"] == "powershell"
    h = I.hook_handler("Stop", python="/opt/my py/bin/python3", form="bash")
    assert h["command"].startswith("'") and "my py" in h["command"].split("' '")[0]
    assert h["command"].endswith("' Stop") and "\\" not in h["command"] and h["shell"] == "bash"


def test_init_runs_first_scan(tmp_path):
    project = make_project(tmp_path, init_store=False)
    rep = I.init_project(project)
    assert rep["scan"].get("files", 0) >= 1, rep["scan"]
    assert (project / ".devmemory" / "index" / "map.json").exists()


def test_uninstall_removes_the_settings_file_and_folder_init_created(tmp_path):
    project = make_project(tmp_path, init_store=False)
    assert not (project / ".claude").exists()
    I.init_project(project, scan=False, settings_file="settings.local.json")
    assert (project / ".claude" / "settings.local.json").exists()
    rep = I.uninstall(project, settings_file="settings.local.json")
    assert rep["settings_removed"] and rep["claude_dir_removed"]
    assert not (project / ".claude").exists()
    assert (project / ".devmemory").is_dir()


def test_uninstall_keeps_claude_folder_it_did_not_create_or_that_has_other_files(tmp_path):
    project = make_project(tmp_path, init_store=False)
    (project / ".claude").mkdir()
    (project / ".claude" / "notes.md").write_text("mine", encoding="utf-8")
    I.init_project(project, scan=False, settings_file="settings.local.json")
    rep = I.uninstall(project, settings_file="settings.local.json")
    assert rep["settings_removed"] and "claude_dir_removed" not in rep    # folder pre-existed
    assert (project / ".claude" / "notes.md").exists() and not (project / ".claude" / "settings.local.json").exists()
    # folder created by init but something else was put in it afterwards
    project2 = make_project(tmp_path / "two", init_store=False)
    I.init_project(project2, scan=False, settings_file="settings.local.json")
    (project2 / ".claude" / "other.txt").write_text("x", encoding="utf-8")
    I.uninstall(project2, settings_file="settings.local.json")
    assert (project2 / ".claude" / "other.txt").exists()


def test_uninstall_never_deletes_a_file_with_other_content_or_one_init_did_not_create(tmp_path):
    project = make_project(tmp_path, init_store=False)
    I.init_project(project, scan=False, settings_file="settings.local.json")
    path = project / ".claude" / "settings.local.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["env"] = {"KEEP": "1"}                                   # the user added something
    path.write_text(json.dumps(data), encoding="utf-8")
    rep = I.uninstall(project, settings_file="settings.local.json")
    assert "settings_removed" not in rep and json.loads(path.read_text(encoding="utf-8")) == {"env": {"KEEP": "1"}}
    # a file that existed before init and becomes empty is kept as {}
    project2 = make_project(tmp_path / "two", init_store=False)
    _write_settings(project2, {"hooks": {}})
    I.init_project(project2, scan=False)
    rep2 = I.uninstall(project2)
    assert "settings_removed" not in rep2 and _settings(project2) == {}


# ---- the installed command can never block the session (exit code 2 is a blocking error for Claude Code) ----

OLD_SHAPE = {"type": "command", "command": "C:/Python/python.exe", "args": ["C:/old place/devmem/hooks.py", "Stop"]}


def _spawn(h: dict, project: Path, tmp: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(CLAUDE_PROJECT_DIR=str(project), TMP=str(tmp), TEMP=str(tmp), TMPDIR=str(tmp))
    payload = load_payload("002_UserPromptSubmit", project)
    return subprocess.run([h["command"], *h["args"]], input=json.dumps(payload).encode("utf-8"), cwd=project,
                          env=env, capture_output=True, timeout=60)


def test_installed_command_exits_0_when_devmem_is_moved_broken_or_raises(tmp_path, monkeypatch):
    import shutil
    project = make_project(tmp_path, init_store=True)
    logs = tmp_path / "tmp"
    logs.mkdir()
    pkg = tmp_path / "moved pkg"
    shutil.copytree(Path(I.__file__).parent, pkg / "devmem", ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(I, "package_root", lambda: pkg)
    h = I.hook_handler("UserPromptSubmit")
    assert _spawn(h, project, logs).returncode == 0
    assert list((project / ".devmemory" / "live").glob("*.jsonl"))          # it works while the folder is there
    hooks_py = pkg / "devmem" / "hooks.py"
    for broken in ("def broken(:\n", "raise SystemExit(2)\n", "import no_such_module_xyz\n"):
        hooks_py.write_text(broken, encoding="utf-8")
        p = _spawn(h, project, logs)
        assert p.returncode == 0, (broken, p.stderr)
    shutil.rmtree(pkg)                                                     # the checkout was moved or deleted
    p = _spawn(h, project, logs)
    assert p.returncode == 0 and not p.stdout, p.stderr
    log = next(logs.glob("devmem_hook_errors*.log")).read_text(encoding="utf-8")
    assert "ImportError" in log and "SyntaxError" in log and "moved pkg" in log
    # the shape installed before this fix: Python cannot open the script and exits 2 (blocks the prompt)
    old = {"type": "command", "command": h["command"], "args": [str(hooks_py), "UserPromptSubmit"]}
    assert _spawn(old, project, logs).returncode == 2


def test_old_script_handlers_are_upgraded_by_init_and_removed_by_uninstall(tmp_path):
    project = make_project(tmp_path, init_store=False)
    _write_settings(project, {"hooks": {ev: [{"hooks": [dict(OLD_SHAPE, args=[OLD_SHAPE["args"][0], ev])]}]
                                        for ev in I.HOOK_EVENTS}})
    assert I.handler_paths(OLD_SHAPE)["shape"] == "script" and I.is_devmem_handler(OLD_SHAPE)
    I.init_project(project, scan=False)
    s = _settings(project)
    for ev in I.HOOK_EVENTS:
        hs = [h for g in s["hooks"][ev] for h in g["hooks"]]
        assert len(hs) == 1 and I.handler_paths(hs[0]) == {
            "shape": "bootstrap", "python": hs[0]["command"],
            "hooks_py": str(I.package_root() / "devmem" / "hooks.py"), "event": ev}
    # a settings file holding both shapes (e.g. merged by hand) loses both on uninstall
    s["hooks"]["Stop"].append({"hooks": [OLD_SHAPE]})
    _write_settings(project, s)
    I.uninstall(project)
    assert "hooks" not in _settings(project)


@pytest.mark.skipif(os.name == "nt", reason="the bash form runs under POSIX bash")
def test_bash_form_runs_and_survives_a_missing_folder(tmp_path, monkeypatch):
    import shutil
    project = make_project(tmp_path, init_store=True)
    pkg = tmp_path / "pkg dir"
    shutil.copytree(Path(I.__file__).parent, pkg / "devmem", ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(I, "package_root", lambda: pkg)
    h = I.hook_handler("UserPromptSubmit", form="bash")
    cmd = {"type": "command", "command": "bash", "args": ["-c", h["command"]]}
    assert _spawn(cmd, project, tmp_path).returncode == 0
    assert list((project / ".devmemory" / "live").glob("*.jsonl"))
    shutil.rmtree(pkg)
    assert _spawn(cmd, project, tmp_path).returncode == 0
