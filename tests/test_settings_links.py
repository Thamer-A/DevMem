"""L2 / L7: how the project's `.claude/settings*.json` is read.

Hook time and status (the plugin's route proposal, devmem.plugin.init_handlers -> init.installed_handlers) read it
only as a plain file: a link, a FIFO or a device counts as holding no handlers, and the read never blocks.
`devmem init` and `devmem uninstall` (init._read_settings with the project) never follow a link: a settings file or a
`.claude` folder that is a link or junction is refused with nothing changed, wherever it leads (out of the project: a
cloned repository can ship `.claude/settings.json -> ~/.claude/settings.json`; inside it: `-> ../credentials.json`
would copy a secret file into the backup and the settings file), and so is a file that is not a settings object
(credentials-shaped keys). A FIFO or /dev/zero is refused without being read or waited on. Neither writes through a
link, and the temporary file is never one a repository planted."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import consent, plugin
from devmem import init as I

fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX")


def _link(target: Path, link: Path, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("cannot make a symbolic link here")


def _within(fn, seconds: float = 20.0):
    """fn() run in a daemon thread: its result, or its exception re-raised; fails if it has not returned in time
    (a blocked open of a FIFO would never return)."""
    out: dict = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - handed back to the test
            out["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), f"{fn} blocked"
    if "error" in out:
        raise out["error"]
    return out.get("value")


def _snapshot(*paths: Path) -> dict:
    return {str(p): (os.path.islink(p), p.read_bytes() if p.exists() else None) for p in paths}


def _project_with_moved_handlers(tmp_path: Path, inside: bool) -> tuple[Path, Path, Path]:
    """A project after `devmem init` whose settings.json (holding devmem's handlers) was then moved elsewhere,
    inside the project or out of it, and linked back: (project, settings link, the real file)."""
    project = make_project(tmp_path, init_store=False)
    I.init_project(project, scan=False)
    settings = project / ".claude" / "settings.json"
    assert I.installed_handlers(project) and plugin.init_handlers(project)
    real = (project / "config" if inside else tmp_path / "dotfiles") / "settings.json"
    real.parent.mkdir()
    os.replace(settings, real)
    _link(real, settings)
    return project, settings, real


@pytest.mark.parametrize("inside", [True, False])
def test_hook_time_read_does_not_follow_a_settings_link(tmp_path, inside):
    project, settings, _real = _project_with_moved_handlers(tmp_path, inside)
    with pytest.raises(ValueError, match="not a plain file"):
        I._load_settings(settings)
    assert I.installed_handlers(project) == []
    assert plugin.init_handlers(project) == []


def test_uninstall_refuses_a_settings_link_inside_the_project_and_changes_nothing(tmp_path):
    project, settings, real = _project_with_moved_handlers(tmp_path, inside=True)
    before = _snapshot(settings, real)
    with pytest.raises(ValueError, match="is a link or junction.*nothing was changed.*real file"):
        I.uninstall(project)
    assert _snapshot(settings, real) == before
    assert consent.init_enabled(project)


def test_uninstall_refuses_a_settings_link_out_of_the_project_and_changes_nothing(tmp_path):
    project, settings, real = _project_with_moved_handlers(tmp_path, inside=False)
    before = _snapshot(settings, real)
    with pytest.raises(ValueError, match="link leading outside the project.*nothing was changed"):
        I.uninstall(project)
    assert _snapshot(settings, real) == before
    assert consent.init_enabled(project)                   # the switch was not turned off either


SECRET = {"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----\nCREDMARKER\n-----END PRIVATE KEY-----\n",
          "client_secret": "CREDMARKER"}


def _no_marker_anywhere(root: Path, allowed: set[Path]) -> list[str]:
    out = []
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink() and p not in allowed and b"CREDMARKER" in p.read_bytes():
            out.append(str(p))
    return out


@pytest.mark.parametrize("target", ["credentials.json", ".env", "sub/secret_settings.json", "config/claude.json"])
@pytest.mark.parametrize("name", ["settings.local.json", "settings.json"])
def test_init_refuses_a_settings_link_inside_the_project_and_copies_nothing(tmp_path, target, name):
    project = make_project(tmp_path, init_store=False)
    (project / ".claude").mkdir(exist_ok=True)
    real = project / target
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text(json.dumps(SECRET), encoding="utf-8")
    _link(real, project / ".claude" / name)
    before = _snapshot(project / ".claude" / name, real)
    with pytest.raises(ValueError, match="is a link or junction.*nothing was changed.*real file"):
        I.init_project(project, scan=False, settings_file=name)
    assert _snapshot(project / ".claude" / name, real) == before
    assert _no_marker_anywhere(project, {real}) == []
    assert not consent.init_enabled(project)


def test_init_refuses_a_linked_claude_folder_inside_the_project(tmp_path):
    project = make_project(tmp_path, init_store=False)
    (project / "other").mkdir()
    _link(project / "other", project / ".claude", directory=True)
    with pytest.raises(ValueError, match="is a link or junction"):
        I.init_project(project, scan=False)
    assert list((project / "other").iterdir()) == []


@pytest.mark.parametrize("bad", [SECRET, {"hooks": []}, {"env": "x"}, {"permissions": 3}])
def test_init_never_copies_or_rewrites_a_file_that_is_not_a_settings_object(tmp_path, bad):
    project = make_project(tmp_path, init_store=False)
    (project / ".claude").mkdir(exist_ok=True)
    settings = project / ".claude" / "settings.local.json"
    settings.write_text(json.dumps(bad), encoding="utf-8")
    before = settings.read_bytes()
    with pytest.raises(ValueError, match="not a Claude Code settings file|is not an object"):
        I.init_project(project, scan=False, settings_file="settings.local.json")
    assert settings.read_bytes() == before
    assert not (project / ".devmemory" / "init-backups").exists()


@pytest.mark.skipif(os.name == "nt", reason="planting a symbolic link needs privileges on Windows")
def test_init_never_writes_through_a_planted_temporary_file(tmp_path):
    project = make_project(tmp_path, init_store=False)
    (project / ".claude").mkdir(exist_ok=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    os.symlink(victim, project / ".claude" / "settings.local.json.devmem-tmp")
    I.init_project(project, scan=False, settings_file="settings.local.json")
    assert victim.read_text(encoding="utf-8") == "precious\n"
    assert I._load_settings(project / ".claude" / "settings.local.json")["hooks"]


@pytest.mark.parametrize("which", ["file", "folder", "junction"])
def test_init_refuses_a_settings_link_out_of_the_project_and_changes_nothing(tmp_path, which):
    project = make_project(tmp_path, init_store=False)
    outside = tmp_path / "home-claude"
    outside.mkdir()
    real = outside / "settings.json"
    real.write_text(json.dumps({"env": {"TOKEN": "x"}}), encoding="utf-8")
    if which == "file":
        (project / ".claude").mkdir(exist_ok=True)
        _link(real, project / ".claude" / "settings.json")
    elif which == "folder":                                # `.claude` itself links out (the file inside it, too)
        _link(outside, project / ".claude", directory=True)
    else:
        if os.name != "nt":
            pytest.skip("junctions are Windows only")
        import _winapi
        _winapi.CreateJunction(str(outside), str(project / ".claude"))
    before = _snapshot(project / ".claude" / "settings.json", real)
    with pytest.raises(ValueError, match="outside the project.*nothing was changed"):
        I.init_project(project, scan=False)
    assert _snapshot(project / ".claude" / "settings.json", real) == before
    assert sorted(p.name for p in outside.iterdir()) == ["settings.json"]
    assert not (project / ".devmemory").exists() and not consent.init_enabled(project)


def test_init_refuses_a_linked_claude_folder_out_of_the_project_with_no_settings_yet(tmp_path):
    project = make_project(tmp_path, init_store=False)
    outside = tmp_path / "home-claude"
    outside.mkdir()
    _link(outside, project / ".claude", directory=True)
    with pytest.raises(ValueError, match="outside the project"):
        I.init_project(project, scan=False)
    assert list(outside.iterdir()) == []                   # nothing created through the link


def test_a_file_swapped_after_the_check_is_not_read(tmp_path, monkeypatch):
    """The path is resolved and checked once; if what gets opened is not that file (a link swapped in between, here
    simulated by opening the outside file), nothing is read and nothing changes."""
    from devmem import store as ST
    project = make_project(tmp_path, init_store=False)
    (project / ".claude").mkdir(exist_ok=True)
    settings = project / ".claude" / "settings.json"
    settings.write_text(json.dumps({"env": {"A": "1"}}), encoding="utf-8")
    outside = tmp_path / "global.json"
    outside.write_text(json.dumps({"env": {"TOKEN": "x"}}), encoding="utf-8")
    real_open = ST._open_regular
    monkeypatch.setattr(ST, "_open_regular", lambda p, cap, follow=False: real_open(outside, cap))
    before = _snapshot(settings, outside)
    with pytest.raises(ValueError, match="changed while it was being read"):
        I.init_project(project, scan=False)
    assert _snapshot(settings, outside) == before and not (project / ".devmemory").exists()


@fifo
def test_a_fifo_as_settings_never_blocks(tmp_path):
    project = make_project(tmp_path, init_store=False)
    (project / ".claude").mkdir(exist_ok=True)
    os.mkfifo(project / ".claude" / "settings.json")
    assert _within(lambda: I.installed_handlers(project)) == []
    with pytest.raises(ValueError, match="not a regular file"):
        _within(lambda: I.init_project(project, scan=False))
    with pytest.raises(ValueError, match="not a regular file"):
        _within(lambda: I.uninstall(project))


@fifo
@pytest.mark.parametrize("kind", ["fifo-inside", "fifo-outside", "dev-zero"])
def test_a_link_to_a_fifo_or_a_device_is_refused_without_blocking(tmp_path, kind):
    project = make_project(tmp_path, init_store=False)
    (project / ".claude").mkdir(exist_ok=True)
    if kind == "dev-zero":
        target = Path("/dev/zero")
        if not target.exists():
            pytest.skip("no /dev/zero")
    else:
        target = (project if kind == "fifo-inside" else tmp_path) / "pipe"
        os.mkfifo(target)
    _link(target, project / ".claude" / "settings.json")
    refused = "is a link or junction" if kind == "fifo-inside" else "outside the project"
    assert _within(lambda: I.installed_handlers(project)) == []
    with pytest.raises(ValueError, match=refused):
        _within(lambda: I.init_project(project, scan=False))
    with pytest.raises(ValueError, match=refused):
        _within(lambda: I.uninstall(project))
