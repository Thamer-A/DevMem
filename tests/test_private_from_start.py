"""L7: files that hold private data are created private, not created then narrowed. The sh launcher's Python cache
(`$CLAUDE_PLUGIN_DATA/python-path`, its temporary name and the `.probing` marker) was 0644 (umask 022) or 0666 (umask
000); `devmem init`'s settings backup was copied with the umask's mode and chmod-ed afterwards."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import init as I
from devmem import store as ST

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin"
posix = pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


@posix
@pytest.mark.parametrize("umask", ["000", "022"])
def test_launcher_python_cache_is_private(tmp_path, umask):
    data = tmp_path / "pd" / "nested"                  # created by the launcher
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env.update(CLAUDE_PLUGIN_ROOT=str(PLUGIN), CLAUDE_PLUGIN_DATA=str(data), TMPDIR=str(tmp_path))
    script = f"umask {umask}; exec sh '{PLUGIN / 'bin' / 'devmem'}' --plugin-hook Stop"
    r = subprocess.run(["sh", "-c", script], input=b"{}", capture_output=True, env=env, timeout=120)
    assert r.returncode == 0 and r.stdout == b""
    cache = data / "python-path"
    assert cache.read_text(encoding="utf-8").startswith("DEVMEM-PY ")
    assert _mode(cache) == 0o600 and _mode(data) == 0o700
    assert not list(data.glob("python-path.*"))         # the temporary name and the marker are gone


def test_init_settings_backup_is_private_without_a_chmod(tmp_path, monkeypatch):
    """The backup is written 0600 from the start: even with every chmod disabled it is not readable by others."""
    old = os.umask(0)
    try:
        project = make_project(tmp_path, init_store=False)
        (project / ".claude").mkdir(exist_ok=True)
        (project / ".claude" / "settings.json").write_text(json.dumps({"env": {"MY_TOKEN": "x"}}), encoding="utf-8")
        original = (project / ".claude" / "settings.json").read_bytes()
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(ST, "tighten", lambda *a, **k: None)
        monkeypatch.setattr(I, "tighten", lambda *a, **k: None, raising=False)
        rep = I.init_project(project, scan=False)
    finally:
        os.umask(old)
    backup = Path(rep["backup"])
    assert backup.read_bytes() == original
    if os.name != "nt":
        assert _mode(backup) == 0o600


def test_init_refuses_a_settings_link_out_of_the_project_and_changes_nothing(tmp_path):
    # A link out of the project (a cloned repository can ship `.claude/settings.json -> ~/.claude/settings.json`)
    # would copy the target's content, env values included, into a project file; init refuses and writes nothing
    project = make_project(tmp_path, init_store=False)
    (project / ".claude").mkdir(exist_ok=True)
    real = tmp_path / "dotfiles-settings.json"
    real.write_text(json.dumps({"env": {"A": "1"}}), encoding="utf-8")
    link = project / ".claude" / "settings.json"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("cannot make a symbolic link here")
    original = real.read_bytes()
    with pytest.raises(ValueError, match="link leading outside the project.*nothing was changed"):
        I.init_project(project, scan=False)
    assert real.read_bytes() == original and link.is_symlink() and os.path.samefile(link, real)
    assert not (project / ".devmemory").exists()            # no store, no backup
    from devmem import consent
    assert not consent.init_enabled(project)                # no switch


def test_init_refuses_a_settings_link_inside_the_project_and_backs_nothing_up(tmp_path):
    """A link inside the project is refused too (it could lead to a secret file): no backup, no store."""
    project = make_project(tmp_path, init_store=False)
    (project / ".claude").mkdir(exist_ok=True)
    real = project / "config" / "claude-settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"env": {"MY_TOKEN": "x"}}), encoding="utf-8")
    try:
        os.symlink(real, project / ".claude" / "settings.json")
    except (OSError, NotImplementedError):
        pytest.skip("cannot make a symbolic link here")
    with pytest.raises(ValueError, match="is a link or junction"):
        I.init_project(project, scan=False)
    assert not (project / ".devmemory").exists()
