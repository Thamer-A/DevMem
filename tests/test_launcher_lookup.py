"""The plugin launchers never run a program from the project folder.

`plugin/bin/devmem.cmd` ran `forfiles` by bare name with the project as the current folder (in `:stalled`, before
`pushd`), and cmd looks in the current folder first unless NoDefaultCurrentDirectoryInExePath is set: a
`forfiles.bat` shipped by a project that also ships `.devmemory\\` ran (plugin route on Windows without Git Bash, no
cached Python, a `.probing` marker from today). The sh launcher runs find, date, id, mkdir, rm, mv and timeout by
name, and an empty or relative PATH entry is the current folder there.
"""
from __future__ import annotations

import os
import subprocess

import pytest
from test_plugin_hooks import HOOKS, PLUGIN, PWSH, SH, deliver_project, hook_env, payload_for

PLANTED = ("find", "date", "id", "mkdir", "rm", "mv", "timeout", "forfiles")


def _plant(project, shell):
    for name in PLANTED:
        if shell == "powershell":
            for ext in (".bat", ".cmd", ".exe.bat"):
                (project / f"{name}{ext}").write_text(f"@echo off\r\necho planted > \"%~dp0PWNED-{name}\"\r\n",
                                                      encoding="utf-8")
        else:
            p = project / name
            p.write_text(f"#!/bin/sh\necho planted > '{project.as_posix()}/PWNED-{name}'\n", encoding="utf-8")
            p.chmod(0o755)


def _run(shell, event, project, env):
    cmd = HOOKS[event][0]["hooks"][0]["command"]
    argv = [SH, "-c", cmd] if shell == "sh" else [PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd]
    import json
    return subprocess.run(argv, input=json.dumps(payload_for("002_UserPromptSubmit", project)).encode("utf-8"),
                          capture_output=True, env=env, timeout=120, cwd=str(project))


@pytest.mark.skipif(not PWSH, reason="Windows PowerShell / cmd only")
def test_the_cmd_launcher_runs_no_program_from_the_project(tmp_path):
    data, logs = tmp_path / "data", tmp_path / "logs"
    data.mkdir()
    logs.mkdir()
    project = deliver_project(tmp_path, data)
    _plant(project, "powershell")
    (data / "python-path-cmd.probing").write_text("", encoding="utf-8")       # a search from today never finished
    env = hook_env(PLUGIN, project, data, logs, "powershell")
    env.pop("NoDefaultCurrentDirectoryInExePath", None)
    env.pop("NODEFAULTCURRENTDIRECTORYINEXEPATH", None)
    r = _run("powershell", "UserPromptSubmit", project, env)
    assert r.returncode == 0, r.stderr
    assert not list(project.glob("PWNED-*")), [p.name for p in project.glob("PWNED-*")]


@pytest.mark.skipif(not SH, reason="no sh/Git Bash")
@pytest.mark.parametrize("entry", ["", ".", "rel"])
def test_the_sh_launcher_runs_no_program_from_the_project(tmp_path, entry):
    data, logs = tmp_path / "data", tmp_path / "logs"
    data.mkdir()
    logs.mkdir()
    project = deliver_project(tmp_path, data)
    _plant(project, "sh")
    if entry == "rel":
        (project / "rel").mkdir()
        for name in PLANTED:
            os.replace(project / name, project / "rel" / name)
    (data / "python-path.probing").write_text("", encoding="utf-8")           # makes the launcher run `find`
    env = hook_env(PLUGIN, project, data, logs, "sh")
    sep = ";" if os.name == "nt" else ":"
    env["PATH"] = entry + sep + env["PATH"]
    r = _run("sh", "UserPromptSubmit", project, env)
    assert r.returncode == 0, r.stderr
    found = list(project.rglob("PWNED-*"))
    assert not found, [p.name for p in found]
