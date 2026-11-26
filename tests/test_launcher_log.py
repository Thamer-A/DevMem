"""L6: the sh launcher's fallback error log (`_dm_log` in plugin/bin/devmem) wrote with a plain `>>`: it followed a
symlink planted at its name in a shared temp folder and had no owner check. It now writes only when the name is absent
(created under umask 077) or is a regular file of this user that is not a link."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin"


def _posix_shell() -> str | None:
    if os.name != "nt":
        return shutil.which("sh")
    git = shutil.which("git")
    for d in Path(git).resolve().parents if git else []:
        if (d / "bin" / "bash.exe").is_file():
            return str(d / "bin" / "bash.exe")
    return None


SH = _posix_shell()
pytestmark = pytest.mark.skipif(not SH, reason="no sh/Git Bash")
LOG = "devmem_hook_errors.log" if os.name == "nt" else f"devmem_hook_errors_{os.getuid()}.log"


def _run(tmp_path: Path, umask: str = "") -> tuple[subprocess.CompletedProcess, Path]:
    """One hook event that only logs: an unfinished Python search (a fresh marker) skips the search, so the launcher
    logs twice and exits 0 without starting Python."""
    data = tmp_path / "pd"
    data.mkdir(exist_ok=True)
    (data / "python-path.probing").write_text("", encoding="utf-8")
    logs = tmp_path / "tmp"
    logs.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PROJECT_DIR",)}
    env.update(CLAUDE_PLUGIN_ROOT=str(PLUGIN), CLAUDE_PLUGIN_DATA=str(data), TMPDIR=str(logs), TEMP=str(logs),
               TMP=str(logs))
    launcher = (PLUGIN / "bin" / "devmem").as_posix()
    script = (f"umask {umask}; " if umask else "") + f"exec sh '{launcher}' --plugin-hook Stop"
    r = subprocess.run([SH, "-c", script], input=b"{}", capture_output=True, env=env, timeout=60)
    return r, logs / LOG


def test_log_is_created_and_appended(tmp_path):
    r, log = _run(tmp_path)
    assert r.returncode == 0 and r.stdout == b""
    first = log.read_text(encoding="utf-8")
    assert "did not finish" in first
    _run(tmp_path)
    assert len(log.read_text(encoding="utf-8")) > len(first)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
@pytest.mark.parametrize("umask", ["000", "022"])
def test_a_new_log_is_private_whatever_the_umask(tmp_path, umask):
    _r, log = _run(tmp_path, umask)
    assert stat.S_IMODE(os.lstat(log).st_mode) == 0o600


def test_a_link_at_the_log_name_is_never_followed(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("victim-original\n", encoding="utf-8")
    (tmp_path / "tmp").mkdir()
    try:
        os.symlink(victim, tmp_path / "tmp" / LOG)
    except (OSError, NotImplementedError):
        pytest.skip("cannot make a symbolic link here")
    r, _log = _run(tmp_path)
    assert r.returncode == 0
    assert victim.read_text(encoding="utf-8") == "victim-original\n"


def test_a_dangling_link_is_not_created_through(tmp_path):
    target = tmp_path / "elsewhere" / "planted.log"
    target.parent.mkdir()
    (tmp_path / "tmp").mkdir()
    try:
        os.symlink(target, tmp_path / "tmp" / LOG)
    except (OSError, NotImplementedError):
        pytest.skip("cannot make a symbolic link here")
    r, _log = _run(tmp_path)
    assert r.returncode == 0 and not target.exists()


def test_a_folder_at_the_log_name_is_left_alone(tmp_path):
    (tmp_path / "tmp" / LOG).mkdir(parents=True)
    r, log = _run(tmp_path)
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b"" and log.is_dir() and not any(log.iterdir())
