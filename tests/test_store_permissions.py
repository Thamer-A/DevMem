"""POSIX: what devmem keeps (the store, its per-user folder, backups) is readable by this user only.

With the usual umask 022 the store's folders were 0755 and sessions/*.jsonl, *.meta.json and index/index.db 0644, so
every user of the machine could read recorded prompts, code and tool output. Folders are now 0700 and files 0600, an
older store is narrowed the next time devmem writes it, and `devmem doctor` warns about one that is still open.
Nothing changes on Windows (per-user ACLs), so these tests run on Linux and macOS only (CI, Docker).
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from cli_helpers import run_cli
from hooks_helpers import TURN1, TURN2, make_project, replay

from devmem import hooks, ui_server
from devmem.consent import user_data_dir
from devmem.pack import build_pack
from devmem.scan import scan
from devmem.store import ensure_store

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")


@pytest.fixture()
def umask022():
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def _open_to_others(root: Path) -> list[str]:
    out = []
    for p in [root, *root.rglob("*")]:
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode):
            continue
        if stat.S_IMODE(st.st_mode) & 0o077:
            out.append(f"{oct(stat.S_IMODE(st.st_mode))} {p.relative_to(root.parent)}")
    return out


def test_a_recorded_session_scan_and_index_are_private(tmp_path, monkeypatch, umask022):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    os.chmod(store, 0o755)                          # as the test helper (or an older devmem) left it
    outs = replay(project, TURN1 + TURN2, tmp_path / "t.jsonl", monkeypatch, hooks)
    assert all(code == 0 for code, _ in outs)
    scan(project, store)
    build_pack(project, store, "greet hello")
    ui_server.add_recent(project, store, "proj")      # recents_file() is in DEVMEM_USER_DATA (tests/conftest.py)
    names = {p.name for p in store.rglob("*")}
    assert {"index.db", "map.json", "PROJECT.md"} <= names and any(n.endswith(".meta.json") for n in names)
    assert _open_to_others(store) == []
    ud = user_data_dir()
    assert (ud / "recents.json").is_file() and any((ud / "projects").iterdir())
    assert _open_to_others(ud) == []


def test_an_older_open_store_is_narrowed_on_the_next_write(tmp_path, umask022):
    store = tmp_path / "proj" / ".devmemory"
    (store / "sessions").mkdir(parents=True)
    (store / "index").mkdir()
    for f in ("sessions/old.meta.json", "sessions/old.jsonl", "index/index.db", "PROJECT.md", "config.json"):
        (store / f).write_text("{}", encoding="utf-8")
    for p in [store, *store.rglob("*")]:
        os.chmod(p, 0o755 if p.is_dir() else 0o644)
    ensure_store(store)
    assert _open_to_others(store) == []


def test_a_store_already_private_is_not_walked_again(tmp_path, umask022, monkeypatch):
    store = tmp_path / "proj" / ".devmemory"
    ensure_store(store)
    (store / "sessions").mkdir(mode=0o700)
    deep = store / "sessions" / "x.jsonl"
    deep.write_text("", encoding="utf-8")
    os.chmod(deep, 0o644)                           # files below the private top level are left for their writer
    ensure_store(store)
    assert stat.S_IMODE(os.lstat(deep).st_mode) == 0o644
    assert stat.S_IMODE(os.lstat(store).st_mode) == 0o700


def test_doctor_warns_about_a_store_others_can_read(tmp_path, umask022):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    r = run_cli(["scan"], cwd=project)
    assert r.returncode == 0, r.stderr
    store = project / ".devmemory"
    out = run_cli(["doctor"], cwd=project).stdout.decode("utf-8", "replace")
    assert "permissions" not in out, out
    os.chmod(store, 0o755)
    out = run_cli(["doctor"], cwd=project).stdout.decode("utf-8", "replace")
    assert "permissions" in out and "other users" in out, out
