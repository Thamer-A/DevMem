"""Build a wheel from a copy of the source and check the app's UI files are inside it.

Skipped where pip or setuptools is missing. Builds only inside a temp directory, never in the repo."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.packaging


def _have(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


@pytest.mark.skipif(not (_have("pip") and _have("setuptools")), reason="pip and setuptools are needed to build a wheel")
def test_wheel_contains_ui_files_and_stays_dependency_free(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", src / "pyproject.toml")
    for extra in ("README.md", "LICENSE"):
        if (ROOT / extra).is_file():
            shutil.copy2(ROOT / extra, src / extra)
    shutil.copytree(ROOT / "plugin" / "devmem", src / "plugin" / "devmem", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--disable-pip-version-check",
                        "-w", str(out), str(src)], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=600)
    log = r.stdout + r.stderr
    if r.returncode != 0 and "bdist_wheel" in log:
        # old setuptools without the `wheel` package cannot build a wheel without build isolation
        pytest.skip("this environment cannot build wheels offline (no `wheel` package / setuptools < 70.1)")
    assert r.returncode == 0, log[-2000:]
    wheels = list(out.glob("dev_memory-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as z:
        names = set(z.namelist())
        meta = z.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode("utf-8")
    for must in ("devmem/ui/index.html", "devmem/ui/app.js", "devmem/ui/app.css", "devmem/cli.py", "devmem/ui_server.py"):
        assert must in names, must
    assert not any(n.startswith("devmem/__pycache__") for n in names)
    requires = [l for l in meta.splitlines() if l.startswith("Requires-Dist:")]
    assert all("extra ==" in l for l in requires), requires  # core has no hard dependency
    assert "Provides-Extra: dev" in meta and "Provides-Extra: embeddings" in meta
    assert "Project-URL: Issues, https://github.com/Thamer-A/DevMem/issues" in meta.splitlines()
    from devmem import __version__
    assert any(l == f"Version: {__version__}" for l in meta.splitlines())  # from devmem.__version__
    assert "License-Expression: MIT" in meta.splitlines() and "License-File: LICENSE" in meta.splitlines()
