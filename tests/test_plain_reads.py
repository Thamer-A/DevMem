"""L2 remainder: the reads left after the store reads went through store.read_regular. A FIFO at any of these paths
must never block (a hook or a command would hang), and a link must not be followed out of the folder the file
belongs to (except the user's own Claude Code settings, which a dotfile manager may link).

Paths: the store's `.gitignore` (read by is_store_folder, ensure_store, gitignore_problem, shipped_store and init),
the consent markers and `recents.json` in devmem's per-user folder, the doctor's fallback error log in the temp
folder, the project's Claude Code settings files (init._load_settings, plugin.plugin_enabled).

Each FIFO case runs in a subprocess with a timeout, so a regression fails instead of hanging the suite. On Windows
there are no FIFOs; a folder (or a junction) in a file's place is covered instead.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from cli_helpers import cli_env, run_cli
from hooks_helpers import make_project

posix = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs and unprivileged symlinks are POSIX")


def _py(code: str, timeout: int = 30, **env) -> str:
    """Run `code` in a fresh interpreter (devmem from the tree); TimeoutExpired fails the test if it blocks."""
    r = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, timeout=timeout,
                       env=cli_env(**env))
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-2000:]
    return r.stdout.decode("utf-8", "replace").strip()


def _outside(tmp_path: Path, name: str, text: str) -> Path:
    f = tmp_path / "elsewhere" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return f


# ---------------------------------------------------------------- the store's .gitignore

@posix
def test_store_gitignore_fifo_swapped_in_after_the_check_never_blocks(tmp_path):
    # the readers check _regular() first; the read itself must not block when a FIFO arrives after that check
    project = make_project(tmp_path)
    gi = project / ".devmemory" / ".gitignore"
    if os.path.lexists(gi):
        gi.unlink()
    os.mkfifo(gi)
    out = _py(f"""
        from pathlib import Path
        from devmem import store
        store._regular = lambda p: True          # the swap happened between the check and the read
        s = Path({str(project / '.devmemory')!r})
        print(store.is_store_folder(s), store.gitignore_problem(s) is not None)
        store.ensure_store(s)
        print("done")
    """)
    assert out.splitlines()[-1] == "done"


@posix
def test_init_with_a_fifo_as_the_store_gitignore_finishes(tmp_path):
    project = make_project(tmp_path)
    gi = project / ".devmemory" / ".gitignore"
    if os.path.lexists(gi):
        gi.unlink()
    os.mkfifo(gi)
    r = run_cli(["init", "--project", str(project), "--no-scan"], cwd=project, timeout=60)
    assert b"Traceback" not in r.stderr
    assert gi.is_fifo() or not gi.exists() or gi.is_file()


@posix
def test_store_gitignore_link_is_not_read_through(tmp_path):
    from devmem import store
    project = make_project(tmp_path)
    s = project / ".devmemory"
    if os.path.lexists(s / ".gitignore"):
        (s / ".gitignore").unlink()
    (s / ".gitignore").symlink_to(_outside(tmp_path, "gi", "# devmem\n*\n"))
    assert store.read_regular(s / ".gitignore") is None
    assert store.gitignore_problem(s)            # a link is reported, not read as if it were the store's own


# ---------------------------------------------------------------- devmem's per-user folder

@posix
def test_consent_marker_fifo_or_link_does_not_allow_unredacted(tmp_path):
    from devmem import consent
    project = make_project(tmp_path)
    m = consent.init_marker(project)
    note = m.with_name(m.name + consent.PLUGIN_NOTE)
    allow = _outside(tmp_path, "allow.json", json.dumps({"allow_unredacted": True}))
    if os.path.lexists(m):
        m.unlink()
    m.symlink_to(allow)
    os.mkfifo(note)
    assert _py(f"""
        from pathlib import Path
        from devmem import consent
        print(consent._allows(Path({str(m)!r})), consent._allows(Path({str(note)!r})),
              consent.allows_unredacted(Path({str(project)!r})))
    """) == "False False False"


@posix
def test_recents_fifo_or_link_reads_as_empty(tmp_path):
    from devmem import ui_server
    rec = ui_server.recents_file()
    rec.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(rec)
    assert _py("from devmem import ui_server; print(ui_server.load_recents())") == "[]"
    rec.unlink()
    rec.symlink_to(_outside(tmp_path, "recents.json", json.dumps([{"project": "/elsewhere"}])))
    assert ui_server.load_recents() == []


@posix
def test_doctor_does_not_read_the_fallback_log_through_a_link(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    planted = _outside(tmp_path, "log", f"--- 2026-11-14T00:00:00Z Stop pid=1 store={store}\nboom\n")
    (tmp / f"devmem_hook_errors_{os.getuid()}.log").symlink_to(planted)
    r = run_cli(["doctor", "--project", str(project)], cwd=project, env=cli_env(TMPDIR=str(tmp)), timeout=60)
    text = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert "Traceback" not in text and str(planted) not in text
    assert "holds a recent failure for this project" not in text, text     # the planted lines were not read
    log = tmp / f"devmem_hook_errors_{os.getuid()}.log"                     # control: the same lines, a plain file
    log.unlink()
    log.write_text(planted.read_text(encoding="utf-8"), encoding="utf-8")
    r = run_cli(["doctor", "--project", str(project)], cwd=project, env=cli_env(TMPDIR=str(tmp)), timeout=60)
    assert "holds a recent failure for this project" in (r.stdout + r.stderr).decode("utf-8", "replace")


# ---------------------------------------------------------------- the project's Claude Code settings

@posix
@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
def test_project_settings_fifo_never_blocks_init_or_the_plugin_check(tmp_path, name):
    project = make_project(tmp_path)
    (project / ".claude").mkdir(exist_ok=True)
    f = project / ".claude" / name
    if f.exists():
        f.unlink()
    os.mkfifo(f)
    out = _py(f"""
        from pathlib import Path
        from devmem import init, plugin
        p = Path({str(project)!r})
        try:
            init._load_settings(Path({str(f)!r}))
            print("read")
        except ValueError:
            print("refused")
        print(init.installed_handlers(p), plugin.plugin_enabled(p, config_dir={str(tmp_path / 'cfg')!r}))
    """)
    assert out.splitlines() == ["refused", "[] None"]


@posix
def test_project_settings_link_is_not_followed_but_the_users_own_may_be(tmp_path):
    from devmem import init, plugin
    project = make_project(tmp_path)
    (project / ".claude").mkdir(exist_ok=True)
    on = _outside(tmp_path, "on.json", json.dumps({"enabledPlugins": {"devmem@devmem": True}}))
    local = project / ".claude" / "settings.local.json"
    if local.exists():
        local.unlink()
    local.symlink_to(on)
    with pytest.raises(ValueError):
        init._load_settings(local)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    assert plugin.plugin_enabled(project, config_dir=str(cfg)) is None      # the project's link is not followed
    (cfg / "settings.json").symlink_to(on)
    assert (plugin.plugin_enabled(project, config_dir=str(cfg)) or "").startswith("user settings")
    (cfg / "settings.json").unlink()
    os.mkfifo(cfg / "settings.json")
    assert _py(f"""
        from pathlib import Path
        from devmem import plugin
        print(plugin.plugin_enabled(Path({str(project)!r}), config_dir={str(cfg)!r}))
    """) == "None"


# ---------------------------------------------------------------- Windows (and everywhere): a folder in a file's place

def test_a_folder_in_a_files_place_is_refused_quietly(tmp_path):
    from devmem import consent, init, plugin, store, ui_server
    project = make_project(tmp_path)
    (project / ".claude").mkdir(exist_ok=True)
    for name in ("settings.json", "settings.local.json"):
        f = project / ".claude" / name
        if f.exists():
            f.unlink()
        f.mkdir()
    with pytest.raises(ValueError):
        init._load_settings(project / ".claude" / "settings.local.json")
    assert init.installed_handlers(project) == []
    cfg = tmp_path / "cfg"
    (cfg / "settings.json").mkdir(parents=True)
    assert plugin.plugin_enabled(project, config_dir=str(cfg)) is None
    rec = ui_server.recents_file()
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.mkdir()
    assert ui_server.load_recents() == []
    m = consent.init_marker(project)
    note = m.with_name(m.name + consent.PLUGIN_NOTE)
    note.mkdir()
    assert consent._allows(note) is False
    gi = project / ".devmemory" / ".gitignore"
    if os.path.lexists(gi):
        gi.unlink()
    gi.mkdir()
    assert store.read_regular(gi) is None and store.gitignore_problem(project / ".devmemory")


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows'")
def test_a_junction_in_a_files_place_is_refused_quietly(tmp_path):
    from devmem import init, ui_server
    target = tmp_path / "target"
    target.mkdir()
    project = make_project(tmp_path)
    (project / ".claude").mkdir(exist_ok=True)
    local = project / ".claude" / "settings.local.json"
    if local.exists():
        local.unlink()
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(local), str(target)], capture_output=True)
    if r.returncode:
        pytest.skip("mklink /J failed here")
    with pytest.raises(ValueError):
        init._load_settings(local)
    rec = ui_server.recents_file()
    rec.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(rec), str(target)], capture_output=True, check=True)
    assert ui_server.load_recents() == []
