"""L5: the temp folder's hook error log is never written through a link or into another user's file.

Its name is predictable (devmem_hook_errors_<uid>.log), and hooks.py and the plugin / `devmem init` bootstraps opened
it with a plain append that follows links and creates it 0644: on a shared /tmp another user could plant a link (the
hook's traceback, with paths, appended to a file of their choice) or a file of their own (read the tracebacks). It is
now opened with O_NOFOLLOW, created 0600, and written only when it is a regular file this user owns. On Windows the
temp folder is per-user. The log stays where it was, so `devmem doctor` finds old entries there.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from devmem import hooks, init, plugin

posix = pytest.mark.skipif(os.name == "nt", reason="links and owners in a shared /tmp are a POSIX concern")


def _bootstrap(code: str, tmp, env_tmp) -> None:
    env = {**os.environ, "TMPDIR": str(env_tmp), "TEMP": str(env_tmp), "TMP": str(env_tmp)}
    r = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(tmp / "no-plugin-here"), "Stop"],
                       capture_output=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr


def _log_name() -> str:
    return f"devmem_hook_errors_{os.getuid()}.log" if hasattr(os, "getuid") else "devmem_hook_errors.log"


@pytest.mark.parametrize("code", [plugin.HOOK_BOOTSTRAP, init.BOOTSTRAP], ids=["plugin", "init"])
def test_the_bootstraps_log_a_failure_privately(tmp_path, code):
    logs = tmp_path / "tmp"
    logs.mkdir()
    _bootstrap(code, tmp_path, logs)
    log = logs / _log_name()
    assert log.is_file() and "no-plugin-here" in log.read_text(encoding="utf-8", errors="replace")
    if os.name != "nt":
        assert oct(log.stat().st_mode & 0o777) == oct(0o600)


@posix
@pytest.mark.parametrize("code", [plugin.HOOK_BOOTSTRAP, init.BOOTSTRAP], ids=["plugin", "init"])
def test_the_bootstraps_never_write_through_a_planted_link(tmp_path, code):
    logs = tmp_path / "tmp"
    logs.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")
    os.symlink(victim, logs / _log_name())
    _bootstrap(code, tmp_path, logs)
    assert victim.read_text(encoding="utf-8") == "keep\n"


@posix
def test_hooks_never_write_through_a_planted_link(tmp_path, monkeypatch):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")
    log = tmp_path / _log_name()
    os.symlink(victim, log)
    monkeypatch.setattr(hooks, "fallback_error_log", lambda: log)
    hooks._log_error(None, "Stop", RuntimeError("boom"))
    assert victim.read_text(encoding="utf-8") == "keep\n"


@posix
@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to give a file to another user")
def test_another_users_file_is_not_written(tmp_path, monkeypatch):
    log = tmp_path / _log_name()
    log.write_text("theirs\n", encoding="utf-8")
    os.chown(log, 12345, 12345)
    monkeypatch.setattr(hooks, "fallback_error_log", lambda: log)
    hooks._log_error(None, "Stop", RuntimeError("boom"))
    assert log.read_text(encoding="utf-8") == "theirs\n"


@posix
@pytest.mark.parametrize("code", [plugin.HOOK_BOOTSTRAP, init.BOOTSTRAP], ids=["plugin", "init"])
def test_the_bootstraps_never_block_on_a_planted_fifo(tmp_path, code):
    # another user can make a FIFO with the predictable name in a shared /tmp: opening it for writing would block
    # until a reader comes (the hook then hangs until Claude Code's timeout); O_NONBLOCK makes the open fail at once
    logs = tmp_path / "tmp"
    logs.mkdir()
    os.mkfifo(logs / _log_name())
    env = {**os.environ, "TMPDIR": str(logs), "TEMP": str(logs), "TMP": str(logs)}
    r = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(tmp_path / "no-plugin-here"), "Stop"],
                       capture_output=True, env=env, timeout=20)
    assert r.returncode == 0, r.stderr


@posix
def test_hooks_never_block_on_a_planted_fifo(tmp_path):
    log = tmp_path / _log_name()
    os.mkfifo(log)
    code = ("from pathlib import Path; from devmem import hooks; "
            f"hooks.fallback_error_log = lambda: Path({str(log)!r}); "
            "hooks._log_error(None, 'Stop', RuntimeError('boom')); print('done')")
    env = {**os.environ, "PYTHONPATH": str(Path(hooks.__file__).resolve().parent.parent)}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=20)
    assert r.returncode == 0 and r.stdout.strip() == b"done", r.stderr
