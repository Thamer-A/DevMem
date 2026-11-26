"""The plugin's hook command lines, run exactly as hooks/hooks.json declares them, through the shells Claude Code
uses (sh/bash; Windows PowerShell 5.1 on Windows), with the environment Claude Code exports to plugin hooks.

Also: the launchers' search for Python (shims that fail or hang, a stale cache), odd plugin folder names, and the
plugin folder copied on its own (as the plugin installer does), run with nothing else importable. Shells that are not
present are skipped."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from devmem import init as I
from devmem import plugin as P

from hooks_helpers import SESSION_ID, apply_effect, load_payload, make_project

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin"
HOOKS = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
CORE = ["000_SessionStart", "002_UserPromptSubmit", "008_PostToolUse", "022_Stop", "024_SessionEnd"]
PROMPT = "change how damage armor is applied"   # has evidence in damage.py: the pack is not empty


def _posix_shell() -> str | None:
    """`sh` on macOS/Linux (what Claude Code runs); Git Bash's bash.exe on Windows (likewise)."""
    if os.name != "nt":
        return shutil.which("sh")
    git = shutil.which("git")           # <Git>/cmd/git.exe -> <Git>/bin/bash.exe; never WSL's System32 bash.exe
    for d in Path(git).resolve().parents if git else []:
        if (d / "bin" / "bash.exe").is_file():
            return str(d / "bin" / "bash.exe")
    return None


SH = _posix_shell()
PWSH = shutil.which("powershell.exe") if os.name == "nt" else None
SHELLS = [pytest.param("sh", marks=pytest.mark.skipif(not SH, reason="no sh/Git Bash")),
          pytest.param("powershell", marks=pytest.mark.skipif(not PWSH, reason="Windows PowerShell only"))]
windows_ps = pytest.mark.skipif(not PWSH, reason="Windows PowerShell only")


def hook_env(root: Path, project: Path, data: Path, logs: Path, shell: str, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    fwd = shell == "sh" and os.name == "nt"     # Git Bash gets C:/... paths, PowerShell native ones (docs)
    as_text = (lambda p: Path(p).as_posix()) if fwd else str
    env.update(CLAUDE_PLUGIN_ROOT=as_text(root), CLAUDE_PROJECT_DIR=as_text(project), CLAUDE_PLUGIN_DATA=as_text(data),
               TMP=str(logs), TEMP=str(logs), TMPDIR=str(logs), PYTHONUTF8="0", **extra)
    return env


def run_hook(shell: str, event: str, payload, env: dict, wait: bool = True, prefix: str = ""):
    cmd = prefix + HOOKS[event][0]["hooks"][0]["command"]
    argv = [SH, "-c", cmd] if shell == "sh" else [PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd]
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    if not wait:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        return p, data
    return subprocess.run(argv, input=data, capture_output=True, env=env, timeout=120)


def deliver_project(tmp_path: Path, data: Path | None = None, store: bool = True) -> Path:
    """A project with delivery on; switched on for the plugin (its marker in `data`) when `data` is given."""
    project = make_project(tmp_path, init_store=store)
    if store:
        (project / ".devmemory" / "config.json").unlink()      # delivery on
    (project / "damage.py").write_text("def apply_armor(damage, armor):\n    return max(0, damage - armor)\n",
                                       encoding="utf-8")
    if data is not None:
        P._write_marker(P.marker_path(data, project), project)
    return project


def amp_project(tmp_path: Path, data: Path) -> Path:
    """A switched-on project whose folder name has an ampersand and no space (PowerShell then passes it unquoted)."""
    project = shutil.move(str(deliver_project(tmp_path / "a", data=None)), str(tmp_path / "R&D"))
    project = Path(project)
    P._write_marker(P.marker_path(data, project), project)
    return project


def payload_for(name: str, project: Path) -> dict:
    p = load_payload(name, project)
    if name.endswith("UserPromptSubmit"):
        p["prompt"] = PROMPT
    return p


def tree(path: Path) -> list[str]:
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*"))


def metas(project: Path) -> list[Path]:
    return list((project / ".devmemory" / "sessions").glob("*.meta.json"))


def log_text(logs: Path) -> str:
    return "".join(p.read_text(encoding="utf-8", errors="replace") for p in logs.glob("devmem_hook_errors*.log"))


@pytest.fixture
def dirs(tmp_path):
    data, logs = tmp_path / "plugin data", tmp_path / "logs"
    data.mkdir()
    logs.mkdir()
    return data, logs


# ---------------------------------------------------------------- scenarios

@pytest.mark.parametrize("shell", SHELLS)
def test_project_without_a_store_is_left_alone(shell, tmp_path, dirs):
    data, logs = dirs
    project = deliver_project(tmp_path, store=False)
    before = tree(project)
    env = hook_env(PLUGIN, project, data, logs, shell)
    for name in CORE:
        r = run_hook(shell, name.split("_", 1)[1], payload_for(name, project), env)
        assert r.returncode == 0 and r.stdout == b"", (name, r.stdout, r.stderr)
    assert tree(project) == before and not any(data.iterdir()) and not any(logs.iterdir())


@pytest.mark.parametrize("data_exists", [True, False])
@pytest.mark.parametrize("shell", SHELLS)
def test_a_store_the_repository_ships_is_left_alone(shell, tmp_path, dirs, data_exists):
    """A `.devmemory` folder without the user's switch (the marker in the plugin's data folder): every event exits 0
    with no output (not even on stderr, also when the data folder does not exist yet) and nothing is written in the
    project (the launcher only remembers where Python is)."""
    data, logs = dirs
    if not data_exists:
        data.rmdir()
    project = deliver_project(tmp_path)
    (project / ".devmemory" / ".gitignore").write_text("# nothing ignored\n", encoding="utf-8")
    before = tree(project)
    env = hook_env(PLUGIN, project, data, logs, shell)
    for name in CORE:
        r = run_hook(shell, name.split("_", 1)[1], payload_for(name, project), env)
        assert r.returncode == 0 and r.stdout == b"" and r.stderr == b"", (name, r.stdout, r.stderr)
    assert tree(project) == before and not any(logs.iterdir())
    assert sorted(p.name for p in data.iterdir()) in ([], ["python-path"], ["python-path-cmd"])


@pytest.mark.parametrize("shell", SHELLS)
def test_project_switched_on_is_recorded_and_gets_a_pack(shell, tmp_path, dirs):
    data, logs = dirs
    project = deliver_project(tmp_path, data)
    env = hook_env(PLUGIN, project, data, logs, shell)
    outs = {}
    for name in CORE:
        apply_effect(name, project)
        r = run_hook(shell, name.split("_", 1)[1], payload_for(name, project), env)
        assert r.returncode == 0, (name, r.stderr)
        outs[name] = r.stdout.decode("utf-8")
    assert "DevMemory context pack" in outs["002_UserPromptSubmit"] and "damage.py" in outs["002_UserPromptSubmit"]
    assert all(v == "" for k, v in outs.items() if k != "002_UserPromptSubmit")
    store = project / ".devmemory"
    assert not (store / "hook_errors.log").exists()
    (meta,) = metas(project)
    rec = json.loads(meta.read_text(encoding="utf-8"))
    assert rec["session_id"] == SESSION_ID and rec["turns"][0]["prompt"] == PROMPT
    assert not (data / "runtime").exists()


@pytest.mark.parametrize("shell", SHELLS)
def test_init_hooks_and_plugin_hooks_together_record_once(shell, tmp_path, dirs):
    """Both routes fire for every event, started at the same moment, as Claude Code does (it does not merge a
    plugin's handler with a settings handler). Exactly one records and exactly one pack is printed."""
    data, logs = dirs
    project = deliver_project(tmp_path, data)
    I.init_project(project, settings_file="settings.local.json", scan=False)
    h = json.loads((project / ".claude" / "settings.local.json").read_text(encoding="utf-8"))["hooks"]
    env = hook_env(PLUGIN, project, data, logs, shell)
    init_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    init_env.update(CLAUDE_PROJECT_DIR=str(project), TMP=str(logs), TEMP=str(logs), TMPDIR=str(logs))
    packs = 0
    for name in CORE:
        event = name.split("_", 1)[1]
        apply_effect(name, project)
        payload = payload_for(name, project)
        ih = h[event][0]["hooks"][0]
        plug, data_in = run_hook(shell, event, payload, env, wait=False)
        init = subprocess.Popen([ih["command"], *ih["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=init_env)
        outs = [plug.communicate(data_in, timeout=120), init.communicate(data_in, timeout=120)]
        assert plug.returncode == 0 and init.returncode == 0, outs
        packs += sum(b"DevMemory context pack" in o for o, _ in outs)
    assert packs == 1
    store = project / ".devmemory"
    (raw,) = (store / "sessions").glob("*.jsonl")
    # records are written as newline + JSON + newline (devmem.hooks._append): skip the blank separators
    events = [json.loads(line)["event"] for line in raw.read_text(encoding="utf-8").split("\n") if line.strip()]
    assert events == ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"]
    assert len(metas(project)) == 1


@pytest.mark.parametrize("shell", SHELLS)
def test_broken_plugin_folder_and_garbage_input_still_exit_0(shell, tmp_path, dirs):
    data, logs = dirs
    project = deliver_project(tmp_path, data)
    copy = tmp_path / "half updated"
    shutil.copytree(PLUGIN, copy, ignore=shutil.ignore_patterns("__pycache__"))
    payload = payload_for("002_UserPromptSubmit", project)

    def ok(root: Path, body=None, prefix: str = "") -> None:
        r = run_hook(shell, "UserPromptSubmit", body if body is not None else payload,
                     hook_env(root, project, data, logs, shell), prefix=prefix)
        assert r.returncode == 0 and r.stdout == b"", (root, r.stdout, r.stderr)

    ok(PLUGIN, b"\x00garbage{")                                   # unreadable input: logged in the store
    assert "JSONDecodeError" in (project / ".devmemory" / "hook_errors.log").read_text(encoding="utf-8")
    ok(PLUGIN, b'["not", "an", "object"]')
    (copy / "devmem" / "hooks.py").write_text("def broken(:\n", encoding="utf-8")
    ok(copy)
    (copy / "devmem" / "plugin.py").unlink()
    ok(copy)
    (copy / "bin" / "devmem").write_text('if [ "$x" = 1 ]; then\n  echo "never closed"\n', encoding="utf-8")
    (copy / "bin" / "devmem.cmd").write_text("@echo off\r\n(\r\n", encoding="utf-8")
    ok(copy)
    shutil.rmtree(copy)
    ok(copy)                                                      # the plugin folder is gone altogether
    if shell == "powershell":
        ok(copy, prefix="$ErrorActionPreference = 'Stop'; ")      # a profile that makes every error terminating
    log = log_text(logs)
    assert "SyntaxError" in log and "ImportError" in log


@pytest.mark.skipif(not SH or os.name != "nt" and not shutil.which("bash"), reason="needs bash")
def test_set_eu_from_bash_env_cannot_fail_the_hook(tmp_path, dirs):
    """BASH_ENV runs in every non-interactive bash: a `set -eu` there must not end the hook with status 1."""
    data, logs = dirs
    project = deliver_project(tmp_path, data)
    rc = tmp_path / "env.sh"
    rc.write_text("set -eu\n", encoding="utf-8")
    bash = SH if os.name == "nt" else shutil.which("bash")
    env = hook_env(PLUGIN, project, data, logs, "sh", BASH_ENV=str(rc))
    cmd = HOOKS["UserPromptSubmit"][0]["hooks"][0]["command"]
    r = subprocess.run([bash, "-c", cmd], input=json.dumps(payload_for("002_UserPromptSubmit", project)).encode(),
                       capture_output=True, env=env, timeout=120)
    assert r.returncode == 0 and b"DevMemory context pack" in r.stdout, r.stderr
    env.pop("CLAUDE_PLUGIN_ROOT")
    r = subprocess.run([bash, "-c", cmd], input=b"{}", capture_output=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    # traps from BASH_ENV: `exit 2` from one of them would block the prompt
    for traps in ("set -euo pipefail\ntrap 'exit 3' ERR\n", "trap 'exit 2' EXIT\n", "set -e\ntrap 'exit 2' ERR EXIT\n"):
        rc.write_text(traps, encoding="utf-8")
        for root in (PLUGIN, tmp_path / "deleted plugin"):
            env = hook_env(root, project, data, logs, "sh", BASH_ENV=str(rc))
            r = subprocess.run([bash, "-c", cmd], input=json.dumps(payload_for("002_UserPromptSubmit", project)).encode(),
                               capture_output=True, env=env, timeout=120)
            assert r.returncode == 0, (traps, root, r.stderr)


@pytest.mark.parametrize("shell", SHELLS)
def test_no_python_on_path_exits_0_and_logs(shell, tmp_path, dirs):
    data, logs = dirs
    project = deliver_project(tmp_path, data)
    if os.name == "nt":
        path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        if shell == "sh":
            path = os.pathsep.join([path, str(Path(SH).parent)])           # bash.exe's own folder, no Python
            usr = Path(SH).parent.parent / "usr" / "bin"
            path += os.pathsep + str(usr) if usr.is_dir() else ""
    else:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for tool in ("sh", "mv", "id", "date", "rm", "find", "timeout"):
            if shutil.which(tool):
                os.symlink(shutil.which(tool), bin_dir / tool)
        path = str(bin_dir)
    env = hook_env(PLUGIN, project, data, logs, shell, PATH=path)
    r = run_hook(shell, "UserPromptSubmit", payload_for("002_UserPromptSubmit", project), env)
    assert r.returncode == 0 and r.stdout == b"", r.stderr
    assert "no Python 3.10 or newer" in log_text(logs)


# ---------------------------------------------------------------- the search for Python (C)

def _stubs(tmp_path: Path, kind: str) -> Path:
    """Shims that sit first on PATH: `out` prints a message on stdout and exits 9009 (what the Microsoft Store alias
    does), `hang` never returns."""
    d = tmp_path / f"stubs-{kind}"
    d.mkdir()
    body = {"out": 'echo "Python was not found; run without arguments to install from the Microsoft Store"\nexit 9009\n',
            "hang": "sleep 1000\n"}[kind]
    for name in ("python3", "python"):
        (d / name).write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
        os.chmod(d / name, 0o755)
    bat = {"out": "@echo Python was not found; run without arguments to install from the Microsoft Store\r\n"
                  "@exit /b 9009\r\n", "hang": "@ping -n 1000 127.0.0.1 >nul\r\n"}[kind]
    for name in ("python.bat", "python3.bat"):
        (d / name).write_bytes(bat.encode())
    where = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "where.exe"
    if kind == "out" and os.name == "nt" and where.is_file():
        shutil.copy(where, d / "python.exe")      # a real .exe that prints an error and exits 1 for the probe
    return d


@pytest.mark.parametrize("shell", SHELLS)
def test_a_failing_python_shim_first_on_path_is_skipped_not_cached(shell, tmp_path, dirs):
    data, logs = dirs
    project = deliver_project(tmp_path, data)
    stubs = _stubs(tmp_path, "out")
    env = hook_env(PLUGIN, project, data, logs, shell, PATH=str(stubs) + os.pathsep + os.environ["PATH"])
    for name in ("000_SessionStart", "002_UserPromptSubmit"):
        r = run_hook(shell, name.split("_", 1)[1], payload_for(name, project), env)
        assert r.returncode == 0, r.stderr
    assert b"DevMemory context pack" in r.stdout
    assert "stubs" not in cached_python(data) and "WindowsApps" not in cached_python(data)


def cached_python(data: Path) -> str:
    """The interpreter the launcher cached: the one line `DEVMEM-PY <path>` naming an existing file."""
    (cache,) = [p for p in data.iterdir() if p.name in ("python-path", "python-path-cmd")]
    (line,) = cache.read_text(encoding="utf-8").splitlines()
    assert line.startswith("DEVMEM-PY "), line
    assert Path(line[10:]).is_file(), line
    return line[10:]


NOT_PYTHON = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "where.exe" if os.name == "nt" \
    else Path("/bin/echo")


@pytest.mark.parametrize("shell", SHELLS)
def test_a_bad_cache_is_not_trusted(shell, tmp_path, dirs):
    """A cache line without the sentinel (an older cache, or one written by hand) is never run: Python is searched
    again. Among them an existing program that is not Python (/bin/echo would print the bootstrap into the prompt)."""
    data, logs = dirs
    project = deliver_project(tmp_path, data)
    env = hook_env(PLUGIN, project, data, logs, shell)
    name = "python-path" if shell == "sh" else "python-path-cmd"
    for i, bad in enumerate((f"DEVMEM-PY {sys.executable}\nsecond line\n", f"DEVMEM-PY {tmp_path / 'gone' / 'x.exe'}\n",
                             f"DEVMEM-PY {tmp_path}\n", f"{sys.executable}\n", f"{NOT_PYTHON}\n",
                             f'DEVMEM-PY {sys.executable}" & echo INJECTED & "\n')):
        (data / name).write_text(bad, encoding="utf-8")
        payload = dict(payload_for("002_UserPromptSubmit", project), session_id=f"bad-cache-{i}")  # a fresh pack each
        r = run_hook(shell, "UserPromptSubmit", payload, env)
        assert r.returncode == 0 and r.stdout.startswith(b"# DevMemory context pack"), (bad, r.stdout, r.stderr)
        assert b"INJECTED" not in r.stdout + r.stderr and b"import sys" not in r.stdout
        cached_python(data)


@pytest.mark.parametrize("shell", SHELLS)
def test_a_cached_program_that_does_not_run_the_bootstrap_is_dropped(shell, tmp_path, dirs):
    """A cache line with the sentinel naming a program that is not Python (it can only get there by hand): the
    bootstrap does not finish, so the cache is deleted and one line logged; the next event searches again."""
    data, logs = dirs
    project = deliver_project(tmp_path, data)
    env = hook_env(PLUGIN, project, data, logs, shell)
    name = "python-path" if shell == "sh" else "python-path-cmd"
    other = shutil.which("git")
    (data / name).write_bytes(f"DEVMEM-PY {other}\n".encode())       # as the launchers write it (LF)
    r = run_hook(shell, "UserPromptSubmit", payload_for("002_UserPromptSubmit", project), env)
    assert r.returncode == 0 and r.stdout == b"", r.stdout
    assert not (data / name).exists() and "did not run the devmem bootstrap" in log_text(logs)
    r = run_hook(shell, "UserPromptSubmit", payload_for("002_UserPromptSubmit", project), env)
    assert r.returncode == 0 and b"DevMemory context pack" in r.stdout
    cached_python(data)


@pytest.mark.skipif(not PWSH, reason="Windows PowerShell only")
def test_the_cmd_launcher_runs_only_an_exe_from_its_cache(tmp_path, dirs):
    """A cache line (with the sentinel) naming a .bat file is not run: cmd would execute it and its output would
    reach the prompt. Python is searched again."""
    data, logs = dirs
    project = deliver_project(tmp_path, data)
    bat = tmp_path / "fake python.bat"
    bat.write_bytes(b"@echo INJECTED-FROM-BAT\r\n@exit /b 0\r\n")
    (data / "python-path-cmd").write_bytes(f"DEVMEM-PY {bat}".encode())
    env = hook_env(PLUGIN, project, data, logs, "powershell")
    r = run_hook("powershell", "UserPromptSubmit", payload_for("002_UserPromptSubmit", project), env)
    assert r.returncode == 0 and b"INJECTED-FROM-BAT" not in r.stdout and b"DevMemory context pack" in r.stdout
    assert cached_python(data).lower().endswith(".exe")


def _csharp_stub(tmp_path: Path, body: str) -> Path:
    """A python.exe that is not Python, compiled with the .NET Framework's csc (Windows only)."""
    csc = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    if os.name != "nt" or not csc.is_file():
        pytest.skip("needs the .NET Framework C# compiler")
    d = tmp_path / "csstub"
    d.mkdir()
    (d / "s.cs").write_text("class P { static void Main() { " + body + " } }", encoding="utf-8")
    r = subprocess.run([str(csc), "/nologo", f"/out:{d / 'python.exe'}", str(d / "s.cs")], capture_output=True)
    if r.returncode:
        pytest.skip(f"csc failed: {r.stdout[-200:]!r}")
    (d / "s.cs").unlink()
    return d


@pytest.mark.parametrize("shell", SHELLS)
def test_a_program_that_prints_an_existing_file_is_not_taken_for_python(shell, tmp_path, dirs):
    """A python first on PATH that exits 0 and prints the path of an existing file (C:\\Windows\\win.ini, or
    /etc/passwd) is not cached: only output carrying the sentinel counts."""
    data, logs = dirs
    project = deliver_project(tmp_path, data)
    if os.name == "nt":
        stub = _csharp_stub(tmp_path, r'System.Console.Write(@"C:\Windows\win.ini");')
        if shell == "sh":
            (stub / "python3").write_text("#!/bin/sh\nprintf 'C:\\\\Windows\\\\win.ini'\n", encoding="utf-8",
                                          newline="\n")
    else:
        stub = tmp_path / "stub"
        stub.mkdir()
        for n in ("python3", "python"):
            (stub / n).write_text("#!/bin/sh\necho /etc/passwd\n", encoding="utf-8")
            os.chmod(stub / n, 0o755)
    env = hook_env(PLUGIN, project, data, logs, shell, PATH=str(stub) + os.pathsep + os.environ["PATH"])
    r = run_hook(shell, "UserPromptSubmit", payload_for("002_UserPromptSubmit", project), env)
    assert r.returncode == 0 and b"DevMemory context pack" in r.stdout, r.stderr
    assert "win.ini" not in cached_python(data) and "passwd" not in cached_python(data)


@pytest.mark.skipif(not PWSH, reason="Windows PowerShell only")
def test_a_quoted_path_entry_with_parentheses_does_not_break_the_cmd_search(tmp_path, dirs):
    data, logs = dirs
    project = deliver_project(tmp_path, data)
    odd = tmp_path / "Program Files (x86)" / "Foo"
    odd.mkdir(parents=True)
    path = f'"{odd}";' + os.environ["PATH"]
    env = hook_env(PLUGIN, project, data, logs, "powershell", PATH=path)
    r = run_hook("powershell", "UserPromptSubmit", payload_for("002_UserPromptSubmit", project), env)
    assert r.returncode == 0 and b"DevMemory context pack" in r.stdout, r.stderr
    assert not (data / "python-path-cmd.probing").exists()
    cached_python(data)


def _coreutils_timeout() -> bool:
    if os.name == "nt":
        return bool(SH) and (Path(SH).parent.parent / "usr" / "bin" / "timeout.exe").is_file()
    return bool(SH) and bool(shutil.which("timeout"))


@pytest.mark.skipif(not _coreutils_timeout(), reason="sh with coreutils timeout")
def test_a_hanging_python_shim_costs_one_bounded_wait(tmp_path, dirs):
    data, logs = dirs
    project = deliver_project(tmp_path, data)
    stubs = _stubs(tmp_path, "hang")
    env = hook_env(PLUGIN, project, data, logs, "sh", PATH=str(stubs) + os.pathsep + os.environ["PATH"])
    # The first event pays the search: each hanging shim on PATH (python3, python, and on Windows python.bat and
    # python3.bat: 4 there, 22.6 s measured) is cut off at 5 s. It is timed on a SessionStart, which does little else,
    # so that the bound is the search's and not the context pack's (a prompt adds the refresh and pack budgets, 8 s +
    # 4 s, which a loaded machine fills: 27.6 s was measured for search + prompt against the old 25 s limit).
    shims = len([f for f in stubs.iterdir() if f.is_file()])
    t = time.monotonic()
    r = run_hook("sh", "SessionStart", payload_for("000_SessionStart", project), env)
    first = time.monotonic() - t
    assert r.returncode == 0, r.stderr
    r = run_hook("sh", "UserPromptSubmit", payload_for("002_UserPromptSubmit", project), env)
    assert r.returncode == 0 and b"DevMemory context pack" in r.stdout, r.stderr
    t = time.monotonic()
    run_hook("sh", "Stop", payload_for("022_Stop", project), env)
    assert first < shims * 5 + 10 and time.monotonic() - t < 5     # the Python found is cached: no second wait


@pytest.mark.parametrize("shell", SHELLS)
def test_a_search_that_never_finished_is_not_repeated_at_once(shell, tmp_path, dirs):
    """Where a probe cannot be cut off (cmd; sh without `timeout`), the marker of an unfinished search makes the next
    events skip the search (logged) instead of hanging again; an old marker is ignored."""
    data, logs = dirs
    project = deliver_project(tmp_path, data)
    env = hook_env(PLUGIN, project, data, logs, shell)
    marker = data / ("python-path.probing" if shell == "sh" else "python-path-cmd.probing")
    marker.write_text("", encoding="utf-8")
    r = run_hook(shell, "UserPromptSubmit", payload_for("002_UserPromptSubmit", project), env)
    assert r.returncode == 0 and r.stdout == b"" and "did not finish" in log_text(logs), r.stderr
    old = time.time() - 3 * 86400
    os.utime(marker, (old, old))
    r = run_hook(shell, "UserPromptSubmit", payload_for("002_UserPromptSubmit", project), env)
    assert r.returncode == 0 and b"DevMemory context pack" in r.stdout and not marker.exists(), r.stderr


@pytest.mark.skipif(os.name == "nt" or not SH, reason="POSIX: a PATH that has only python3")
def test_only_python3_on_path(tmp_path, dirs):
    data, logs = dirs
    project = deliver_project(tmp_path, data)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("sh", "mv", "id", "date", "rm", "find", "timeout"):
        if shutil.which(tool):
            os.symlink(shutil.which(tool), bin_dir / tool)
    os.symlink(os.path.realpath(sys.executable), bin_dir / "python3")
    env = hook_env(PLUGIN, project, data, logs, "sh", PATH=str(bin_dir))
    r = run_hook("sh", "UserPromptSubmit", payload_for("002_UserPromptSubmit", project), env)
    assert r.returncode == 0 and b"DevMemory context pack" in r.stdout, r.stderr
    cached_python(data)                                                       # remembered for the next event


# ---------------------------------------------------------------- odd folder names on the cmd route (D)

@windows_ps
def test_plugin_folder_with_an_ampersand_works_and_runs_nothing_else(tmp_path, dirs):
    data, logs = dirs
    root = tmp_path / "x&hostname"
    shutil.copytree(PLUGIN, root, ignore=shutil.ignore_patterns("__pycache__"))
    project = amp_project(tmp_path, data)
    env = hook_env(root, project, data, logs, "powershell")
    r = run_hook("powershell", "UserPromptSubmit", payload_for("002_UserPromptSubmit", project), env)
    host = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip().encode()
    assert r.returncode == 0 and b"DevMemory context pack" in r.stdout, r
    assert host not in r.stdout and host not in r.stderr and r.stderr == b"", r.stderr


@windows_ps
@pytest.mark.parametrize("name", ["a%USERNAME%b", "c^d"])
def test_plugin_folder_cmd_cannot_pass_safely_fails_closed(tmp_path, dirs, name):
    data, logs = dirs
    root = tmp_path / name
    shutil.copytree(PLUGIN, root, ignore=shutil.ignore_patterns("__pycache__"))
    project = deliver_project(tmp_path, data)
    before = tree(project)
    env = hook_env(root, project, data, logs, "powershell")
    r = run_hook("powershell", "UserPromptSubmit", payload_for("002_UserPromptSubmit", project), env)
    assert r.returncode == 0 and r.stdout == b"", r
    assert tree(project) == before and "cmd cannot pass safely" in log_text(logs)


@windows_ps
def test_skill_commands_in_a_project_folder_with_an_ampersand(tmp_path, dirs):
    """The skills pass no project path: the launcher takes the current folder. The PowerShell form uses `--%`, so a
    quoted argument reaches cmd quoted."""
    data, logs = dirs
    project = amp_project(tmp_path, data)
    launcher = PLUGIN / "bin" / "devmem.cmd"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    host = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()
    for args in (f'--% sessions --plugin-data "{data}"', f'--% pack --plugin-data "{data}" "armor&hostname"'):
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", f"& '{launcher}' {args}"],
                           capture_output=True, text=True, encoding="utf-8", cwd=project, env=env, timeout=120)
        assert r.returncode == 0 and "not set up" not in r.stdout, (args, r.stdout, r.stderr)
        assert host not in r.stdout + r.stderr


# ---------------------------------------------------------------- the plugin folder on its own

@pytest.mark.parametrize("shell", SHELLS)
def test_plugin_folder_copied_alone_works(shell, tmp_path, dirs):
    """What the installer leaves: only plugin/ in a cache folder. Hooks and the CLI run from that copy with no
    PYTHONPATH (the launchers use python -I, the hooks also -S), and import the copy's devmem."""
    data, logs = dirs
    root = tmp_path / "cache dir" / "devmem" / "0.3.0"
    shutil.copytree(PLUGIN, root, ignore=shutil.ignore_patterns("__pycache__"))
    project = deliver_project(tmp_path, data)
    env = hook_env(root, project, data, logs, shell)
    outs = []
    for name in CORE:
        apply_effect(name, project)
        r = run_hook(shell, name.split("_", 1)[1], payload_for(name, project), env)
        assert r.returncode == 0, r.stderr
        outs.append(r.stdout)
    assert b"DevMemory context pack" in outs[1]
    assert len(metas(project)) == 1
    assert list((root / "devmem" / "__pycache__").glob("hooks.*.pyc"))     # the copy's code ran

    launcher = [SH, str(root / "bin" / "devmem")] if shell == "sh" else [str(root / "bin" / "devmem.cmd")]
    cli_env = {k: v for k, v in env.items() if not k.startswith(("CLAUDE_PLUGIN", "CLAUDE_PROJECT"))}

    def cli(*args: str, cwd: Path = project) -> str:
        r = subprocess.run([*launcher, *args], capture_output=True, env=cli_env, cwd=cwd, timeout=120)
        out = r.stdout.decode("utf-8", "replace")
        assert r.returncode == 0 and out, r.stderr.decode("utf-8", "replace")
        return out

    out = cli("doctor", "--plugin-data", str(data))
    assert f"from {root.resolve() / 'devmem'}" in out and "this devmem runs from the plugin folder" in out, out
    assert "enabled for this project: yes" in out
    assert SESSION_ID[:8] in cli("sessions", "--plugin-data", str(data))
    assert "prompt (written by the user)" in cli("show", "--plugin-data", str(data), SESSION_ID[:6])
    assert "damage.py" in cli("pack", "--plugin-data", str(data), PROMPT)
    bare = make_project(tmp_path / "other", init_store=False)              # a project that is not set up
    assert cli("sessions", "--plugin-data", str(data), cwd=bare).startswith("devmem memory is not set up")
    assert not (bare / ".devmemory").exists()
    assert list((root / "devmem" / "__pycache__").glob("mcp_server.*.pyc"))
