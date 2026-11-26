"""B1, B2: every program devmem starts goes through devmem.proc. On Windows a .bat/.cmd target (also `x.bat.`, which
CreateProcess still runs through cmd.exe), cmd, PowerShell or pwsh gets no argument that cmd.exe or PowerShell would
read as code; `devmem codex` never passes the project folder as an argument (it is the working folder) and runs an
npm `codex.cmd` shim's codex.js with node directly; `devmem wrap` never puts the prompt where a program reads code."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from devmem import generic, proc, store

WIN = os.name == "nt"
win_only = pytest.mark.skipif(not WIN, reason="cmd.exe re-parses a batch file's command line on Windows only")


@pytest.fixture(autouse=True)
def _fresh_lookup():
    store._PROGRAMS.clear()
    yield
    store._PROGRAMS.clear()


# ---------------------------------------------------------------- the rules

@pytest.mark.parametrize("prog", ["x.bat", "X.CMD", "x.bat.", "x.cmd  ", "x.Bat. .", r"C:\w\cmd.exe", "cmd", "CMD.EXE."])
def test_these_run_through_cmd(prog):
    assert proc.runs_through_cmd(prog, nt=True)


@pytest.mark.parametrize("prog", ["x.exe", "x.batx", "x.bat.exe", "node", r"C:\cmd\node.exe", "x"])
def test_these_do_not(prog):
    assert not proc.runs_through_cmd(prog, nt=True)


@pytest.mark.parametrize("arg", ["a&b", "a|b", "a<b", "a>b", "a^b", "%PATH%", "!x!", 'a"b', "a(b", "a)b",
                                 "a\nb", "a\rb", "a\x00b", "a\x1bb"])
def test_cmd_metacharacters_are_refused(arg):
    assert proc._problem(["C:/t/tool.cmd", "ok", arg], nt=True)
    assert proc._problem(["C:/t/tool.cmd.", arg], nt=True)
    assert proc._problem([r"C:\Windows\System32\cmd.exe", "/c", arg], nt=True)
    assert proc._problem([r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-Command", arg], nt=True)
    # an ordinary program receives these as they are; only a NUL character no program can receive (test_nul_paths)
    assert (proc._problem(["C:/t/tool.exe", "ok", arg], nt=True) is None) == (chr(0) not in arg)


def test_the_batch_file_path_itself_is_checked():
    assert proc._problem([r"C:\a&b\tool.cmd", "x"], nt=True)
    assert proc._problem([r"C:\a b\tool.cmd", "plain words", "a;b", "a,b", "a=b", r"C:\x y\z.txt"], nt=True) is None


@pytest.mark.parametrize("arg", ["$env:PATH", "a;b", "`a", "{x}", "'q'", "@a"])
def test_powershell_characters_are_refused_for_powershell(arg):
    assert proc._problem(["pwsh", "-Command", arg], nt=True)


def test_an_ntfs_stream_name_is_refused():
    assert proc._problem([r"C:\t\tool.bat::$DATA", "x"], nt=True)


def test_shell_true_is_refused():
    with pytest.raises(proc.UnsafeCommand):
        proc.run([sys.executable, "-c", "pass"], shell=True)


def test_unsafe_command_is_an_oserror_and_a_valueerror():
    e = proc.UnsafeCommand("x")
    assert isinstance(e, OSError) and isinstance(e, ValueError)


def test_open_url_only_opens_the_local_viewer(monkeypatch):
    opened = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.append(u) or True)
    assert proc.open_url("http://127.0.0.1:8765/#c=abc")
    for bad in ("file:///C:/Windows/System32/calc.exe", "C:\\Windows\\System32\\calc.exe", "http://example.com/",
                "ms-settings:", "http://127.0.0.1:1/\"&calc"):
        with pytest.raises(proc.UnsafeCommand):
            proc.open_url(bad)
    assert opened == ["http://127.0.0.1:8765/#c=abc"]


@pytest.mark.parametrize("argv,code", [
    (["bash", "-c", "x", "y"], {2}), (["bash", "-lc", "x"], {2}), (["bash", "-o", "pipefail", "-c", "x"], {4}),
    (["bash", "script.sh", "-c"], set()), (["/bin/sh", "-e", "-c", "x"], {3}),
    (["python", "-c", "x"], {2}), (["python3.12", "-c", "x"], {2}), (["python", "-cprint(1)"], {1}),
    (["python", "agent.py", "-c", "x"], set()), (["node", "-e", "x"], {2}), (["node", "agent.js", "-e", "x"], set()),
    (["perl", "-E", "x"], {2}), (["claude", "-p", "x"], set()),
])
def test_code_arguments(argv, code):
    assert proc.code_arguments(argv) == code


@pytest.mark.parametrize("prog", ["pwsh", "powershell.exe", "wsl"])
def test_whole_command_line_is_code(prog):
    assert proc.code_arguments([prog, "-Command", "x"]) is None


def test_every_program_start_in_the_package_goes_through_proc():
    """No module but proc.py calls subprocess.run/Popen/call, os.system/startfile/exec*/spawn* or webbrowser.open."""
    import re
    pkg = Path(proc.__file__).parent
    bad = re.compile(r"subprocess\.(run|Popen|call|check_call|check_output|getoutput|getstatusoutput)\b|"
                     r"os\.(system|startfile|popen|exec\w*|spawn\w*|posix_spawn\w*)\(|webbrowser\.(open\w*|get)\b|"
                     r"ShellExecute")
    found = []
    for f in sorted(pkg.rglob("*.py")):
        if f.name == "proc.py":
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if bad.search(code) and "PROC_CALLS" not in code and "startswith" not in code:
                found.append(f"{f.name}:{n}: {line.strip()}")
    assert not found, "\n".join(found)


# ---------------------------------------------------------------- devmem codex through a real .cmd shim

NAMES = ["p&evil", "my p&evil", "%PATH%&evil", "!x!&evil", "a^&evil", "a(b)&evil", "a;b&evil", "a,b&evil",
         "a=b&evil", "plain"]


def _project(tmp_path: Path, name: str) -> tuple[Path, Path]:
    project = tmp_path / "projects" / name
    project.mkdir(parents=True)
    (project / "main.py").write_text("print(1)\n", encoding="utf-8")
    marker = tmp_path / "PWNED.txt"
    # what cmd.exe would run for `&evil`: it looks in the current folder (the project) first
    (project / "evil.bat").write_text(f'@echo off\r\necho ran > "{marker}"\r\n', encoding="ascii")
    return project, marker


def _logging_shim(tmp_path: Path, monkeypatch) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "shim_args.log"
    (bindir / "codex.cmd").write_text(f'@echo off\r\necho ARGS: %* >> "{log}"\r\nexit /b 0\r\n', encoding="ascii")
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return log


@win_only
@pytest.mark.parametrize("name", NAMES)
def test_codex_through_a_cmd_shim_never_runs_a_command_from_the_folder_name(tmp_path, monkeypatch, name):
    project, marker = _project(tmp_path, name)
    log = _logging_shim(tmp_path, monkeypatch)
    generic.codex(project, "say hi", with_pack=False)
    assert not marker.exists()
    text = log.read_text(encoding="utf-8", errors="replace")
    assert "exec --json" in text and str(project) not in text and " -C " not in text


@win_only
@pytest.mark.parametrize("name", ["my p&evil", "plain"])
def test_codex_mcp_through_a_bare_cmd_shim_is_refused(tmp_path, monkeypatch, name):
    project, marker = _project(tmp_path, name)
    log = _logging_shim(tmp_path, monkeypatch)
    with pytest.raises(proc.UnsafeCommand, match="batch file"):
        generic.codex(project, "say hi", with_pack=False, mcp=True)
    assert not marker.exists() and not log.exists()


def _npm_shim(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """An npm-style codex.cmd whose codex.js (run by the real node) logs its arguments and working folder."""
    bindir = tmp_path / "npm"
    js = bindir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    js.parent.mkdir(parents=True)
    shim_ran = tmp_path / "SHIM_RAN.txt"
    (bindir / "codex.cmd").write_text(f'@echo off\r\necho ran > "{shim_ran}"\r\n'
                                      f'node "%~dp0\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n',
                                      encoding="ascii")
    log = tmp_path / "js_args.json"
    js.write_text("require('fs').writeFileSync(" + json.dumps(str(log)) +
                  ", JSON.stringify({argv: process.argv.slice(2), cwd: process.cwd()}));\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return log, shim_ran


@win_only
@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
@pytest.mark.parametrize("name", ["my p&evil", "%PATH%&evil", "plain"])
@pytest.mark.parametrize("mcp", [False, True])
def test_codex_npm_shim_runs_codex_js_with_node(tmp_path, monkeypatch, name, mcp):
    project, marker = _project(tmp_path, name)
    log, shim_ran = _npm_shim(tmp_path, monkeypatch)
    generic.codex(project, "say hi", with_pack=False, mcp=mcp)
    assert not marker.exists() and not shim_ran.exists()
    got = json.loads(log.read_text(encoding="utf-8"))
    assert os.path.normcase(got["cwd"]) == os.path.normcase(str(project))
    assert got["argv"][:2] == ["exec", "--json"] and "-C" not in got["argv"]
    if mcp:   # the -c values reach codex exactly
        args = next(a for a in got["argv"] if a.startswith("mcp_servers.devmemory.args="))
        assert json.loads(args.split("=", 1)[1])[4] == project.as_posix()


def test_codex_program_keeps_a_real_executable(tmp_path):
    assert generic.codex_program(str(tmp_path / "codex.exe")) == [str(tmp_path / "codex.exe")]


@win_only
def test_codex_program_never_takes_node_from_the_project(tmp_path, monkeypatch):
    project, _ = _project(tmp_path, "proj")
    (project / "node.bat").write_text("@echo off\r\n", encoding="ascii")
    bindir = tmp_path / "npm"
    js = bindir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    js.parent.mkdir(parents=True)
    js.write_text("", encoding="utf-8")
    (bindir / "codex.cmd").write_text("@echo off\r\n", encoding="ascii")
    monkeypatch.setenv("PATH", str(project) + os.pathsep + str(bindir))     # only the project has a "node"
    monkeypatch.chdir(project)
    assert generic.codex_program(str(bindir / "codex.cmd"), project) == [str(bindir / "codex.cmd")]


# ---------------------------------------------------------------- devmem wrap

def _bat(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "bat_args.log"
    bat = tmp_path / "tools" / "echoargs.bat"
    bat.parent.mkdir(exist_ok=True)
    bat.write_text(f'@echo off\r\necho %* >> "{log}"\r\n', encoding="ascii")
    return bat, log


@win_only
@pytest.mark.parametrize("suffix", ["", ".", " ", ". ."])
def test_wrap_batch_file_with_trailing_dots_never_gets_the_prompt(tmp_path, suffix):
    project, marker = _project(tmp_path, "proj")
    bat, log = _bat(tmp_path)
    with pytest.raises(proc.UnsafeCommand, match="pack_file"):
        generic.wrap(project, "fix it & evil", [str(bat) + suffix, "{prompt}"], with_pack=False)
    assert not marker.exists() and not log.exists()


@win_only
@pytest.mark.parametrize("template", [["cmd", "/c", "echo", "{prompt}"], ["cmd.exe", "/c", "echo {prompt}"],
                                      ["powershell", "-NoProfile", "-Command", "Write-Output {prompt}"]])
def test_wrap_shells_never_get_the_prompt(tmp_path, template):
    project, marker = _project(tmp_path, "proj")
    with pytest.raises(proc.UnsafeCommand):
        generic.wrap(project, "fix it & evil; & ./evil.bat", template, with_pack=False)
    assert not marker.exists()


def test_wrap_python_c_never_gets_the_prompt_but_a_script_does(tmp_path):
    project, marker = _project(tmp_path, "proj")
    with pytest.raises(proc.UnsafeCommand):
        generic.wrap(project, "print(1)", [sys.executable, "-c", "{prompt}"], with_pack=False)
    script = tmp_path / "agent.py"
    out = tmp_path / "agent_args.json"
    script.write_text("import json, sys\nopen(" + repr(str(out)) + ", 'w', encoding='utf-8').write("
                      "json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    meta = generic.wrap(project, "fix it & evil", [sys.executable, str(script), "{prompt}"], with_pack=False)
    assert meta.exists() and json.loads(out.read_text(encoding="utf-8")) == ["fix it & evil"]


@win_only
@pytest.mark.parametrize("name", ["p&evil", "my p&evil", "%PATH%&evil", "a(b)&evil", "plain"])
def test_wrap_batch_file_with_pack_file_works_in_any_folder(tmp_path, name):
    project, marker = _project(tmp_path, name)
    bat, log = _bat(tmp_path)
    meta = generic.wrap(project, "fix it & evil", [str(bat), "--pack", "{pack_file}"], with_pack=False)
    assert meta.exists() and log.exists() and not marker.exists()
