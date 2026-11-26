"""The Windows installer's build inputs (packaging/windows): the pinned runtime, the folder assembly, the `devmem`
shim, the Inno Setup script, the build script and the workflow. Runs on every platform; the tests that run the real
embeddable Python need Windows and the python.org zip (build\\windows\\download\\ after build.ps1, or DEVMEM_EMBED_ZIP)
and are skipped otherwise. Nothing here installs anything."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import devmem

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "packaging" / "windows"


def _load_assemble():
    spec = importlib.util.spec_from_file_location("devmem_assemble", PKG / "assemble.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = _load_assemble()
PIN = A.load_pin()


# ---------------------------------------------------------------- pinned runtime

def test_pinned_runtime_is_well_formed():
    assert re.fullmatch(r"3\.13\.\d+", PIN["version"])
    assert re.fullmatch(r"[0-9a-f]{64}", PIN["sha256"])
    assert PIN["url"] == f"https://www.python.org/ftp/python/{PIN['version']}/python-{PIN['version']}-embed-amd64.zip"
    assert PIN["size"] > 1_000_000 and "python.org" in PIN["source"]


@pytest.mark.parametrize("change", [
    {"sha256": "ABC"}, {"sha256": "0" * 63}, {"version": "3.13"}, {"version": "3.14.0rc1"}, {"arch": "win32"},
    {"url": "http://www.python.org/ftp/python/3.13.17/python-3.13.17-embed-amd64.zip"}, {"size": "11 MB"},
])
def test_a_malformed_pin_fails_the_build(tmp_path, change):
    bad = tmp_path / "pin.json"
    bad.write_text(json.dumps({**PIN, **change}), encoding="utf-8")
    with pytest.raises(ValueError):
        A.load_pin(bad)


def test_package_version_is_read_without_importing():
    assert A.package_version() == devmem.__version__


# ---------------------------------------------------------------- what the folder holds

def test_pth_puts_only_the_runtime_and_the_package_on_sys_path():
    assert A.pth_name("3.13.17") == "python313._pth"
    lines = [x for x in A.pth_text("3.13.17").splitlines() if x and not x.startswith("#")]
    assert lines == ["python313.zip", ".", "..\\app"]       # no `import site`: no site-packages, no .pth files


def test_shim_runs_the_bundled_python_and_passes_the_exit_status():
    text = A.SHIM
    assert text.isascii() and text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")   # cmd needs CRLF
    lines = text.split("\r\n")
    assert lines[0] == "@echo off"
    run = [x for x in lines if "-m devmem" in x]
    assert run == ['"%DM_HOME%\\python\\python.exe" -I -m devmem %*']    # quoted (spaces), every argument passed on
    assert lines[lines.index(run[0]) + 1] == "exit /b %ERRORLEVEL%"       # devmem's exit status, unchanged
    assert 'if not defined DM_HOME for %%I in ("%~n0.cmd") do set "DM_HOME=%%~dp$PATH:I.."' in lines   # bare name
    assert not [x for x in lines if x.startswith('set "DM_HOME=%~dp0')]   # %~dp0 only when started by a path
    assert "setlocal" in lines[2]                                        # leaves no variables in the caller's cmd
    assert A.SHORTCUT_EXE == r"python\pythonw.exe" and A.SHORTCUT_ARGS == "-I -m devmem app"


def test_package_files_skip_caches_and_tests(tmp_path):
    src = tmp_path / "devmem"
    for rel in ["__init__.py", "ui/index.html", "__pycache__/x.cpython-313.pyc", "ui/__pycache__/y.pyc",
                "tests/test_x.py", "hook.log", "index.db", "z.pyc"]:
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text("x", encoding="utf-8")
    assert [p.as_posix() for p in A.package_files(src)] == ["__init__.py", "ui/index.html"]
    real = [p.as_posix() for p in A.package_files()]
    assert "cli.py" in real and "ui/index.html" in real and "ui/app.js" in real
    assert not any("__pycache__" in p or p.endswith(".pyc") for p in real)


def _fake_embeddable(path: Path, version: str = "3.13.17", extra: dict | None = None) -> Path:
    files = {"python.exe": b"MZ", "pythonw.exe": b"MZ", "python313.zip": b"PK", "python313.dll": b"MZ",
             "python313._pth": b"python313.zip\r\n.\r\n\r\n# Uncomment to run site.main() automatically\r\n#import site\r\n"}
    files.update(extra or {})
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def _pin_for(zip_path: Path) -> dict:
    return {**PIN, "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest()}


def test_assemble_builds_the_installer_folder(tmp_path):
    z = _fake_embeddable(tmp_path / "embed.zip")
    out = tmp_path / "stage"
    summary = A.assemble(z, out, _pin_for(z))
    assert summary["devmem"] == devmem.__version__ and summary["python"] == PIN["version"]
    assert (out / "python" / "python.exe").is_file() and (out / "python" / "python313.zip").is_file()
    assert (out / "python" / "python313._pth").read_bytes() == A.pth_text("3.13.17").encode("ascii")
    assert (out / "app" / "devmem" / "__main__.py").is_file() and (out / "app" / "devmem" / "ui" / "index.html").is_file()
    assert not list((out / "app").rglob("__pycache__"))
    assert (out / "bin" / "devmem.cmd").read_bytes() == A.SHIM.encode("ascii")
    assert (out / "devmem.ico").read_bytes() == (PKG / "devmem.ico").read_bytes()
    A.assemble(z, out, _pin_for(z))      # a second build replaces the folder it made


def test_assemble_fails_closed_on_a_hash_mismatch(tmp_path):
    z = _fake_embeddable(tmp_path / "embed.zip")
    out = tmp_path / "stage"
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        A.assemble(z, out, {**PIN, "sha256": "0" * 64})
    assert not out.exists()               # nothing was extracted


def test_assemble_refuses_a_zip_for_another_version(tmp_path):
    z = _fake_embeddable(tmp_path / "embed.zip")
    with pytest.raises(ValueError, match="not the Python 3.12.1 embeddable"):
        A.assemble(z, tmp_path / "stage", {**_pin_for(z), "version": "3.12.1"})


def test_assemble_never_deletes_a_folder_it_did_not_make(tmp_path):
    z = _fake_embeddable(tmp_path / "embed.zip")
    out = tmp_path / "precious"
    out.mkdir()
    (out / "notes.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="not deleting"):
        A.assemble(z, out, _pin_for(z))
    assert (out / "notes.txt").read_text(encoding="utf-8") == "keep"


def test_extract_refuses_entries_outside_the_folder(tmp_path):
    z = _fake_embeddable(tmp_path / "evil.zip", extra={"../escape.txt": b"x"})
    with pytest.raises(ValueError, match="outside"):
        A.extract_runtime(z, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_icon_is_a_windows_icon_with_the_usual_sizes():
    data = (PKG / "devmem.ico").read_bytes()
    reserved, kind, count = __import__("struct").unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1) and count >= 4
    sizes = {data[6 + 16 * i] or 256 for i in range(count)}
    assert {16, 32, 48, 256} <= sizes


# ---------------------------------------------------------------- Inno Setup script

ISS = (PKG / "devmem.iss").read_text(encoding="utf-8")


def _directive(name: str) -> str:
    m = re.search(rf"^{name}=(.*)$", ISS, re.M)
    assert m, name
    return m.group(1).strip()


def test_iss_installs_per_user_without_admin_rights():
    assert _directive("PrivilegesRequired") == "lowest"
    assert "PrivilegesRequiredOverridesAllowed" not in ISS        # /ALLUSERS cannot turn it into a machine install
    assert _directive("DefaultDirName") == r"{localappdata}\Programs\DevMem"
    assert re.fullmatch(r"\{\{[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\}", _directive("AppId"))
    assert _directive("AppVersion") == "{#AppVersion}" and "#error AppVersion is not defined" in ISS
    assert _directive("OutputBaseFilename") == "DevMem-Setup-{#AppVersion}"
    assert _directive("ChangesEnvironment") == "yes"


def test_iss_references_files_that_exist():
    for name in ("LicenseFile", "SetupIconFile"):
        assert (PKG / _directive(name).replace("\\", "/")).is_file(), name
    sources = re.findall(r'^Source: "([^"]+)"', ISS, re.M)
    assert sources
    staged = {r"{#StageDir}\python\*": "python", r"{#StageDir}\app\*": "app", r"{#StageDir}\bin\devmem.cmd": "bin/devmem.cmd"}
    for s in sources:
        if s in staged:
            continue                                            # made by assemble.py (checked by the assemble test)
        assert (PKG / s.replace("\\", "/")).is_file(), s
    assert set(staged) <= set(sources)


def test_iss_shortcut_opens_the_app_without_a_console():
    icons = [x for x in ISS.splitlines() if x.startswith("Name: ") and "pythonw.exe" in x]
    start_menu = [x for x in icons if x.startswith('Name: "{userprograms}\\DevMem"')]
    assert len(start_menu) == 1
    assert f'Filename: "{{app}}\\{A.SHORTCUT_EXE}"' in start_menu[0]
    assert f'Parameters: "{A.SHORTCUT_ARGS}"' in start_menu[0]
    desktop = [x for x in icons if "{userdesktop}" in x]
    assert len(desktop) == 1 and "Tasks: desktopicon" in desktop[0]
    tasks = {m.group(1): m.group(0) for m in re.finditer(r'^Name: "(\w+)";.*$', ISS.split("[Tasks]")[1].split("[")[0], re.M)}
    assert "Flags: unchecked" in tasks["desktopicon"] and "unchecked" not in tasks["addtopath"]   # PATH on, desktop off


def test_iss_path_code_touches_only_the_user_path_and_keeps_user_data():
    code = ISS.split("[Code]", 1)[1]
    assert "HKEY_LOCAL_MACHINE" not in ISS and "HKLM" not in ISS
    assert code.count("HKEY_CURRENT_USER, EnvKey, 'Path'") >= 4
    assert "RegWriteExpandStringValue" in code and "RegWriteStringValue" not in code
    assert "Result := CompareText(E, Dir) = 0;" in code             # one entry, case-insensitive
    # an exact round trip: Dir + ';' after a trailing ';', ';' + Dir otherwise; uninstall drops the entry and rejoins
    assert "Value := Value + Dir + ';'" in code and "Value := Value + ';' + Dir;" in code
    assert "CurUninstallStep = usUninstall" in code                 # before the uninstaller refreshes the environment
    # nothing outside the program folder is deleted: user data and projects stay
    body = ISS.split("[UninstallDelete]", 1)[1].split("\n[", 1)[0]
    names = re.findall(r'Name: "([^"]+)"', body)
    assert names and all(n.startswith("{app}") for n in names), names
    assert "DevMemory folder in AppData\\Roaming" in _directive("UninstalledAll")


def _pascal(name: str) -> str:
    """The body of procedure/function `name` in [Code]."""
    code = ISS.split("[Code]", 1)[1]
    m = re.search(rf"^(?:procedure|function) {name}\b.*?^end;$", code, re.M | re.S)
    assert m, name
    return m.group(0)


def test_iss_has_no_folder_page_and_no_unchecked_recursive_delete():
    """C3: /DIR= could point the installer at any folder, and [InstallDelete]/[UninstallDelete] `filesandordirs`
    deleted {app}\\python and {app}\\app recursively, through a junction too. Now: no folder page, no section deletes
    recursively, and the [Code] deletes run only with the installer's marker and no link in the program folder."""
    assert _directive("DisableDirPage") == "yes"
    assert "[InstallDelete]" not in ISS.replace("There is no [InstallDelete] section", "")
    assert "filesandordirs" not in ISS
    for section in ("[UninstallDelete]",):
        body = ISS.split(section, 1)[1].split("\n[", 1)[0]
        types = re.findall(r"^Type: (\w+);", body, re.M)
        assert types and set(types) <= {"dirifempty", "files"}, types
    assert "Rec.Attributes and ReparsePointAttr" in _pascal("IsReparsePoint") and "$400" in ISS
    links = _pascal("LinkInProgramFolder")
    for sub in ("{app}'", "{app}\\python'", "{app}\\app'", "{app}\\bin'"):
        assert sub in links, sub
    delete = _pascal("DeleteProgramFolders")
    assert delete.count("DelTree(") == 2
    assert delete.index("FileExists(MarkerFile())") < delete.index("LinkInProgramFolder()") < delete.index("DelTree(")
    prep = _pascal("PrepareToInstall")
    assert "LinkInProgramFolder()" in prep and "InstalledHereBefore()" in prep and "nothing was changed" in prep
    assert "LinkInProgramFolder()" in _pascal("InitializeUninstall") and "Result := False" in _pascal("InitializeUninstall")
    unin = _pascal("CurUninstallStepChanged")
    assert unin.index("FileExists(MarkerFile())") < unin.index("DeleteProgramFolders()")
    step = _pascal("CurStepChanged")
    assert "ssInstall" in step and "DeleteProgramFolders()" in step and "SaveStringToFile(MarkerFile()" in step
    # DelTree appears nowhere else
    assert ISS.count("DelTree(") == 2


# ---------------------------------------------------------------- scripts and workflow

@pytest.mark.parametrize("name", ["build.ps1", "test-installer.ps1"])
def test_powershell_scripts_run_on_windows_powershell_5(name):
    text = (PKG / name).read_text(encoding="utf-8")
    code = "\n".join(x.split("#", 1)[0] if not x.lstrip().startswith("#") else "" for x in text.splitlines())
    assert "&&" not in code and "||" not in code               # pipeline chain operators are PowerShell 7 only
    assert not re.search(r"\?\?|\?\.|\)\s*\?\s*[^\s]", code)    # nor ?? ?. or the ternary
    assert "Set-StrictMode" in text


def test_build_script_checks_the_pinned_hash_before_using_the_zip():
    text = (PKG / "build.ps1").read_text(encoding="utf-8")
    assert "python-embed.json" in text and "Get-FileHash -Algorithm SHA256" in text
    assert text.index("Get-Sha256 $Part") < text.index("Move-Item -LiteralPath $Part")   # verified before it is kept
    assert text.index("SHA-256 verified") < text.index("Expand-Archive")
    assert "https://www.python.org/ftp/python/" in text and "Tls12" in text


WORKFLOW = REPO / ".github" / "workflows" / "installer.yml"


def test_workflow_pins_actions_by_commit():
    text = WORKFLOW.read_text(encoding="utf-8")
    uses = re.findall(r"uses:\s*(\S+)(.*)$", text, re.M)
    assert uses
    for ref, comment in uses:
        name, _, sha = ref.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", sha), ref
        assert re.search(r"#\s*v\d", comment), f"{ref}: version comment missing"


def test_workflow_uploads_the_installer_before_installing_anything_else():
    """C1: the hash check and the upload the release job attests come right after the build step, before any further
    tool is installed in the job (pip, setup-python), so nothing installed later can touch what is shipped."""
    text = WORKFLOW.read_text(encoding="utf-8")
    build = text.index("build.ps1 -RequireInstaller")
    upload = text.index("actions/upload-artifact@")
    assert build < text.index("Check the .sha256 file") < upload
    after_build = text[build:upload]
    assert "pip install" not in after_build and "setup-python" not in after_build and "choco" not in after_build


@pytest.mark.parametrize("name", ["installer.yml", "tests.yml", "release.yml"])
def test_every_pip_installed_tool_is_pinned(name):
    text = (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")
    for line in re.findall(r"pip install ([^\n]+)", text):
        tools = [w for w in line.split() if not w.startswith("-")]
        assert tools, line
        for t in tools:
            assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9_.+-]*", t), f"{name}: {t} is not pinned"


def test_workflow_permissions_are_read_only_except_the_release_job():
    text = WORKFLOW.read_text(encoding="utf-8")
    top = text.split("\njobs:", 1)[0]
    assert re.search(r"^permissions:\n  contents: read$", top, re.M)
    assert text.count("contents: write") == 1 and "--draft" in text
    assert "actions/attest-build-provenance@" in text and "startsWith(github.ref, 'refs/tags/v')" in text


# ---------------------------------------------------------------- the real embeddable Python (Windows only)

def _real_zip() -> Path | None:
    env = os.environ.get("DEVMEM_EMBED_ZIP")
    p = Path(env) if env else REPO / "build" / "windows" / "download" / f"python-{PIN['version']}-embed-amd64.zip"
    return p if p.is_file() else None


needs_runtime = pytest.mark.skipif(sys.platform != "win32" or _real_zip() is None,
                                   reason="needs Windows and the python.org embeddable zip (run build.ps1 first)")


@pytest.fixture(scope="module")
def stage(tmp_path_factory):
    z = _real_zip()
    assert z is not None
    out = tmp_path_factory.mktemp("installer") / "Dev Mem & co" / "DevMem"     # spaces and & in the path
    A.assemble(z, out, PIN)
    return out


def _env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    env.update(extra)
    return env


@needs_runtime
def test_bundled_interpreter_imports_the_bundled_package(stage, tmp_path):
    # a folder that would shadow devmem (or the standard library) if the current folder were on sys.path
    (tmp_path / "devmem").mkdir()
    (tmp_path / "devmem" / "__init__.py").write_text("raise SystemExit('the wrong devmem')", encoding="utf-8")
    (tmp_path / "json.py").write_text("raise SystemExit('the wrong json')", encoding="utf-8")
    py = stage / "python" / "python.exe"
    r = subprocess.run([str(py), "-I", "-m", "devmem", "--version"], cwd=tmp_path, env=_env(PYTHONPATH=str(tmp_path)),
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0 and r.stdout.strip() == f"devmem {devmem.__version__}", r.stderr
    r = subprocess.run([str(py), "-c", "import devmem,json,sys;print(devmem.__file__);print(sys.flags.isolated)"],
                       cwd=tmp_path, env=_env(), capture_output=True, text=True, encoding="utf-8", timeout=120)
    where, isolated = r.stdout.splitlines()
    assert Path(where).parent == stage / "app" / "devmem" and isolated == "1"


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe behaviour")
def test_quoted_shim_name_from_a_hostile_folder_runs_the_installed_python(tmp_path):
    """W1: `"devmem" ...` found through PATH made %~dp0 the current folder, so a folder holding ..\\python\\python.exe
    ran that program (verify3/launch). The installed folder here has no python.exe, so the shim must say so; with the
    old shim the planted stand-in (hostname.exe) ran instead and nothing was said (no runtime needed)."""
    import shutil
    sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    inst = tmp_path / "Dev Mem & co"
    (inst / "bin").mkdir(parents=True)
    (inst / "bin" / "devmem.cmd").write_bytes(A.SHIM.encode("ascii"))
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "python").mkdir()
    shutil.copy(sys32 / "hostname.exe", repo / "python" / "python.exe")
    env = _env(PATH=f"{inst / 'bin'};{sys32}")
    env.pop("NoDefaultCurrentDirectoryInExePath", None)
    for n, line in enumerate(('"devmem" --version', "devmem --version", f'"{inst / "bin" / "devmem.cmd"}" --version')):
        runner = tmp_path / f"run{n}.bat"         # a batch file: the quotes reach cmd exactly as typed
        runner.write_text(f"@echo off\r\ncall {line}\r\n", encoding="ascii")
        r = subprocess.run([str(runner)], cwd=repo / "sub", env=env, capture_output=True, text=True, timeout=60)
        out = (r.stdout + r.stderr).lower()
        assert "bundled python is missing" in out and r.returncode == 9009, (line, r.returncode, out)


@needs_runtime
def test_shim_from_a_path_with_spaces(stage, tmp_path):
    shim = stage / "bin" / "devmem.cmd"
    r = subprocess.run([str(shim), "--version"], cwd=tmp_path, env=_env(), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip() == f"devmem {devmem.__version__}", r.stderr
    r = subprocess.run([str(shim), "no-such-command"], cwd=tmp_path, env=_env(), capture_output=True, text=True, timeout=120)
    assert r.returncode == 2                          # argparse's status, passed through cmd


@needs_runtime
def test_init_with_the_bundled_interpreter_writes_runnable_handlers(stage, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text("print(1)\n", encoding="utf-8")
    shim = stage / "bin" / "devmem.cmd"
    r = subprocess.run([str(shim), "init"], cwd=project, env=_env(DEVMEM_USER_DATA=os.environ["DEVMEM_USER_DATA"]),
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert r.returncode == 0, r.stderr
    settings = json.loads((project / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    h = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert Path(h["command"]) == stage / "python" / "python.exe"       # sys.executable: the real bundled python.exe
    assert Path(h["args"][2]) == stage / "app"
    from devmem.init import handler_can_run
    assert handler_can_run(project, h)
    payload = {"session_id": "11111111-2222-3333-4444-555555555555", "cwd": str(project), "transcript_path": "",
               "hook_event_name": "UserPromptSubmit", "prompt": "rename main"}
    env = _env(DEVMEM_USER_DATA=os.environ["DEVMEM_USER_DATA"])
    for event in ("SessionStart", "UserPromptSubmit"):
        handler = settings["hooks"][event][0]["hooks"][0]
        r = subprocess.run([handler["command"], *handler["args"]], input=json.dumps({**payload, "hook_event_name": event}),
                           cwd=project, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert r.returncode == 0
    live = list((project / ".devmemory" / "live").glob("*.jsonl"))
    assert live and "rename main" in live[0].read_text(encoding="utf-8")
