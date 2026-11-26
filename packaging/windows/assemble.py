"""Assemble the folder the DevMem Windows installer ships (build.ps1 runs this; tests/test_windows_installer.py tests it).

    <out>\\python\\          the official python.org embeddable runtime, its pythonXY._pth rewritten (see pth_text)
    <out>\\app\\devmem\\     the devmem package (with ui/; no __pycache__, no tests)
    <out>\\bin\\devmem.cmd   the `devmem` command (SHIM)
    <out>\\devmem.ico        the icon of the shortcuts and of Apps and features

The zip's SHA-256 is checked against packaging/windows/python-embed.json before anything is extracted (fail closed).
Standard library only, Python 3.10+: build.ps1 runs it with the embeddable python.exe it just verified, so building
needs no Python on the machine.

    python assemble.py --zip python-3.13.17-embed-amd64.zip --out build\\windows\\stage [--compile] [--check]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PIN_FILE = HERE / "python-embed.json"
PACKAGE_SRC = REPO / "plugin" / "devmem"
ICON = HERE / "devmem.ico"
STAGE_MARKER = ".devmem-stage"     # assemble() only ever empties a folder that holds this file (or an empty one)
URL_PREFIX = "https://www.python.org/ftp/python/"

# The `devmem` command. When cmd is given the bare name in quotes ("devmem" ...) and finds it through PATH, %~dp0 names
# the CURRENT folder, not this file's (a cmd quirk), and a folder with ..\python\python.exe would then run its own
# Python. So %~dp0 is used only when the command was started by a path (%0 holds \, / or :, and then it is right);
# for a bare name the shim looks itself up in the PATH folders only (%~$PATH: never searches the current folder).
# Exit status: devmem's own. The bundled python.exe reads its ._pth file, which already makes it isolated (no PYTHON*
# variables, no user site-packages, not the current folder on sys.path); -I says so explicitly as well.
SHIM_LINES = [
    "@echo off",
    "rem devmem command of the DevMem Windows installer: runs devmem with the Python bundled next to this folder.",
    "setlocal EnableExtensions DisableDelayedExpansion",
    'set "NoDefaultCurrentDirectoryInExePath=1"',
    'set "DM_ARG0=%~0"',
    'set "DM_HOME="',
    'if not "%DM_ARG0:\\=%"=="%DM_ARG0%" set "DM_HOME=%~dp0.."',
    'if not "%DM_ARG0:/=%"=="%DM_ARG0%" set "DM_HOME=%~dp0.."',
    'if not "%DM_ARG0::=%"=="%DM_ARG0%" set "DM_HOME=%~dp0.."',
    'if not defined DM_HOME for %%I in ("%~n0.cmd") do set "DM_HOME=%%~dp$PATH:I.."',
    'if "%DM_HOME%"==".." goto missing',
    'if not exist "%DM_HOME%\\python\\python.exe" goto missing',
    '"%DM_HOME%\\python\\python.exe" -I -m devmem %*',
    "exit /b %ERRORLEVEL%",
    ":missing",
    "echo devmem: the bundled Python is missing next to %~f0; reinstall DevMem. 1>&2",
    "exit /b 9009",
]
SHIM = "\r\n".join(SHIM_LINES) + "\r\n"

# What the shortcut runs: no console window (pythonw.exe), the app with no project (it opens on the home page).
SHORTCUT_EXE = r"python\pythonw.exe"
SHORTCUT_ARGS = "-I -m devmem app"

_SKIP_DIRS = {"__pycache__", "tests", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
_SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".db", ".tmp"}


# ---------------------------------------------------------------- pinned runtime

def expected_url(version: str, arch: str = "amd64") -> str:
    return f"{URL_PREFIX}{version}/python-{version}-embed-{arch}.zip"


def load_pin(path: Path = PIN_FILE) -> dict:
    """The pinned embeddable runtime; raises ValueError when the file is malformed (so a typo fails the build)."""
    pin = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = []
    if not re.fullmatch(r"3\.\d{1,2}\.\d{1,3}", str(pin.get("version", ""))):
        problems.append("version must look like 3.13.17 (a final release)")
    if pin.get("arch") != "amd64":
        problems.append("arch must be amd64 (the installer is x64 only)")
    if not re.fullmatch(r"[0-9a-f]{64}", str(pin.get("sha256", ""))):
        problems.append("sha256 must be 64 lower-case hex digits")
    if not isinstance(pin.get("size"), int) or pin["size"] <= 0:
        problems.append("size must be the file size in bytes")
    if not problems and pin.get("url") != expected_url(pin["version"], pin["arch"]):
        problems.append(f"url must be {expected_url(pin['version'], pin['arch'])}")
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return pin


def pth_name(version: str) -> str:
    """python313._pth for 3.13.x: the file the embeddable python.exe reads instead of the environment."""
    major, minor = version.split(".")[:2]
    return f"python{major}{minor}._pth"


def pth_text(version: str) -> str:
    """sys.path of the bundled interpreter, relative to the python folder: the standard library zip, the folder itself
    (the extension modules), and ..\\app, which holds the devmem package. No `import site` line, so site-packages,
    user site-packages and .pth files are never read; the current folder is never added (the ._pth makes Python
    isolated), so nothing in a project can shadow a module."""
    major, minor = version.split(".")[:2]
    return (f"python{major}{minor}.zip\r\n.\r\n..\\app\r\n"
            "# Written by the DevMem build (packaging/windows/assemble.py). No 'import site': nothing from the\r\n"
            "# environment, the user's site-packages or the current folder is added to sys.path.\r\n")


def package_version(src: Path = PACKAGE_SRC) -> str:
    """devmem.__version__, read from the source (never imported, so any Python can run the build)."""
    text = (Path(src) / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not m:
        raise ValueError(f"no __version__ in {src / '__init__.py'}")
    return m.group(1)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify_zip(path: Path, pin: dict) -> None:
    """Fail closed: raise ValueError unless the file's SHA-256 is the pinned one."""
    got = sha256_file(path)
    if got != pin["sha256"]:
        raise ValueError(f"SHA-256 mismatch for {path}: expected {pin['sha256']}, got {got}. Not using it")


# ---------------------------------------------------------------- folder assembly

def skip(rel: Path) -> bool:
    """Files of the package that are not shipped: caches, byte code, logs, databases, tests."""
    return any(p in _SKIP_DIRS for p in rel.parts[:-1]) or rel.name in _SKIP_DIRS or rel.suffix.lower() in _SKIP_SUFFIXES


def package_files(src: Path = PACKAGE_SRC) -> list[Path]:
    """The package's files to ship, relative to `src`, sorted (deterministic)."""
    src = Path(src)
    out = []
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(files):
            rel = (Path(root) / name).relative_to(src)
            if not skip(rel):
                out.append(rel)
    return out


def copy_package(src: Path, dest: Path) -> int:
    n = 0
    for rel in package_files(src):
        target = Path(dest) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(src) / rel, target)
        n += 1
    return n


def extract_runtime(zip_path: Path, dest: Path) -> None:
    """Extract the embeddable zip into `dest`, refusing entries that would land outside it."""
    dest = Path(dest).resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if target != dest and dest not in target.parents:
                raise ValueError(f"{zip_path}: entry {info.filename!r} points outside the folder")
        z.extractall(dest)


def prepare_out(out: Path) -> None:
    """Empty `out` for a new build, but only a folder this script made (it holds STAGE_MARKER) or an empty one."""
    out = Path(out)
    if out.exists():
        if not out.is_dir():
            raise ValueError(f"{out} is not a folder")
        if any(out.iterdir()) and not (out / STAGE_MARKER).is_file():
            raise ValueError(f"{out} is not empty and was not made by assemble.py ({STAGE_MARKER} missing); not deleting it")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / STAGE_MARKER).write_text("made by packaging/windows/assemble.py; deleted and rebuilt on every build\n",
                                    encoding="utf-8")


def assemble(zip_path: Path, out: Path, pin: dict, package_src: Path = PACKAGE_SRC, icon: Path = ICON) -> dict:
    """Build the installer's folder in `out` (see the module docstring). Returns a summary."""
    verify_zip(zip_path, pin)
    prepare_out(out)
    out = Path(out)
    py = out / "python"
    extract_runtime(zip_path, py)
    pth = py / pth_name(pin["version"])
    if not pth.is_file() or not (py / "python.exe").is_file() or not (py / "pythonw.exe").is_file():
        raise ValueError(f"{zip_path} is not the Python {pin['version']} embeddable package ({pth.name}, python.exe "
                         "or pythonw.exe missing)")
    pth.write_bytes(pth_text(pin["version"]).encode("ascii"))
    n = copy_package(package_src, out / "app" / "devmem")
    if not (out / "app" / "devmem" / "__main__.py").is_file() or not (out / "app" / "devmem" / "ui" / "index.html").is_file():
        raise ValueError(f"{package_src} does not look like the devmem package (no __main__.py or ui/index.html)")
    (out / "bin").mkdir()
    (out / "bin" / "devmem.cmd").write_bytes(SHIM.encode("ascii"))
    shutil.copy2(icon, out / "devmem.ico")
    return {"out": str(out), "python": pin["version"], "devmem": package_version(package_src), "package_files": n}


def compile_package(out: Path) -> None:
    """Byte-compile the package with the bundled interpreter (so the .pyc files match its version). Windows only."""
    py = Path(out) / "python" / "python.exe"
    subprocess.run([str(py), "-I", "-m", "compileall", "-q", "-j", "0", str(Path(out) / "app" / "devmem")], check=True)


def check(out: Path, version: str) -> str:
    """Run `python.exe -I -m devmem --version` with the bundled interpreter, from a folder that is not the stage, and
    return its output; raise when it is not `devmem <version>`. Windows only."""
    py = Path(out) / "python" / "python.exe"
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    r = subprocess.run([str(py), "-I", "-m", "devmem", "--version"], capture_output=True, text=True, encoding="utf-8",
                       cwd=str(Path(out).parent), env=env, timeout=120)
    got = (r.stdout or "").strip()
    if r.returncode != 0 or got != f"devmem {version}":
        raise RuntimeError(f"bundled interpreter check failed (exit {r.returncode}): {got!r} {r.stderr.strip()!r}")
    return got


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Assemble the DevMem Windows installer folder.")
    ap.add_argument("--zip", required=True, help="the python.org embeddable zip (its SHA-256 must be the pinned one)")
    ap.add_argument("--out", required=True, help="the folder to (re)build")
    ap.add_argument("--compile", action="store_true", help="byte-compile the package with the bundled interpreter")
    ap.add_argument("--check", action="store_true", help="run the bundled `python -m devmem --version` afterwards")
    a = ap.parse_args(argv)
    try:
        pin = load_pin()
        summary = assemble(Path(a.zip), Path(a.out), pin)
        if a.compile:
            compile_package(Path(a.out))
        if a.check:
            summary["check"] = check(Path(a.out), summary["devmem"])
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as e:
        print(f"assemble: {e}", file=sys.stderr)
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
