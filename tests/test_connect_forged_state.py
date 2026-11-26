"""`.devmemory/connect.json` comes from the project, so a repository can ship one. Whatever it names, `devmem
connect` and `devmem disconnect` delete only devmem's own files: the backup named after the target, inside the
store's backup folder, and folders connect itself would have created for that target. Victims live in tmp_path,
outside the project."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from devmem import connect as C
from devmem.connect import connect, disconnect


@pytest.fixture(autouse=True)
def _bare_devmem_command(monkeypatch):
    monkeypatch.setattr(C, "ABSOLUTE_DEVMEM", False)


def do_connect(project, tool, **kw):
    kw.setdefault("command", "devmem")
    kw.setdefault("which", lambda n: f"/usr/bin/{n}")
    return connect(project, tool, **kw)


def _ship_state(project: Path, state: dict) -> None:
    (project / ".devmemory").mkdir(exist_ok=True)
    (project / ".devmemory" / "connect.json").write_text(json.dumps(state), encoding="utf-8")


def _victims(tmp_path: Path) -> dict[str, Path]:
    out = {}
    for name in ("victim_rel.txt", "victim_abs.txt", "victim_store.txt"):
        p = tmp_path / name
        p.write_text("precious\n", encoding="utf-8")
        out[name] = p
    (tmp_path / "emptydir_outside").mkdir()
    out["dir"] = tmp_path / "emptydir_outside"
    return out


def _evil_names(project: Path, v: dict[str, Path]) -> list[str]:
    return ["../../../victim_rel.txt", "../../victim_rel.txt", str(v["victim_abs.txt"]), "..\\..\\..\\victim_rel.txt",
            "connect.json", "../config.json", ".", "", "AGENTS.md.devmem-backup"]


def _forged(project: Path, v: dict[str, Path]) -> dict:
    names = _evil_names(project, v)
    return {"tools": {"cursor": {"files": [".cursor/mcp.json", "AGENTS.md"]}},
            "created": {".cursor/mcp.json": {"dirs": ["../emptydir_outside", str(v["dir"]), "..", "."]},
                        "AGENTS.md": {"dirs": ["../emptydir_outside"]}},
            "written": {".cursor/mcp.json": {"sha256": "0" * 64, "backup": names[0], "old_backups": names[1:]},
                        "AGENTS.md": {"sha256": "0" * 64, "backup": names[2], "old_backups": names}}}


def _all_there(v: dict[str, Path]) -> list[str]:
    return [k for k, p in v.items() if not p.exists()]


def test_connect_then_disconnect_never_deletes_what_a_shipped_state_names(tmp_path):
    v = _victims(tmp_path)
    project = tmp_path / "a" / "b" / "clone"
    project.mkdir(parents=True)
    (project / "app.py").write_text("x = 1\n", encoding="utf-8")
    _ship_state(project, _forged(project, v))
    store_file = project / ".devmemory" / "config.json"
    store_file.write_text("{}", encoding="utf-8")
    with pytest.raises(C.ConnectError, match="nothing connected"):   # a store the user did not adopt: not written
        do_connect(project, "cursor")
    do_connect(project, "cursor", adopt_store=True)                    # the user adopts it (the forged state too)
    state = json.loads((project / ".devmemory" / "connect.json").read_text(encoding="utf-8"))
    for rec in state["written"].values():         # nothing but devmem's own backup names survive into the new state
        for name in [rec.get("backup"), *(rec.get("old_backups") or [])]:
            assert name in (None, C._backup_name(".cursor/mcp.json"), C._backup_name("AGENTS.md")), name
    r = disconnect(project, "cursor")
    assert _all_there(v) == [], r
    assert store_file.exists()


def test_disconnect_alone_never_deletes_what_a_shipped_state_names(tmp_path):
    """The repository also ships `.cursor/mcp.json` (no devmemory entry); the user runs only `devmem disconnect`."""
    v = _victims(tmp_path)
    project = tmp_path / "a" / "b" / "clone"
    (project / ".cursor").mkdir(parents=True)
    (project / ".cursor" / "mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    (project / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    _ship_state(project, _forged(project, v))
    r = disconnect(project, "cursor")
    assert _all_there(v) == []
    assert (project / ".cursor" / "mcp.json").exists() and (project / "AGENTS.md").exists()
    # a shipped store is not the user's: its connect.json is not read at all (nor written)
    assert any("is not yours" in n for n in r["notes"]), r["notes"]


def test_disconnect_keeps_a_shipped_backup_named_like_devmems_when_it_is_a_link(tmp_path):
    """Even devmem's own backup name is deleted only as a regular file in the real backup folder, never through a link."""
    v = _victims(tmp_path)
    project = tmp_path / "clone"
    project.mkdir()
    (project / ".cursor").mkdir()
    (project / ".cursor" / "mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    name = C._backup_name(".cursor/mcp.json")
    _ship_state(project, {"tools": {"cursor": {"files": [".cursor/mcp.json"]}}, "created": {},
                          "written": {".cursor/mcp.json": {"sha256": "0" * 64, "backup": name}}})
    bdir = project / ".devmemory" / "connect-backups"
    bdir.mkdir()
    try:
        os.symlink(v["victim_store.txt"], bdir / name)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    disconnect(project, "cursor")
    assert v["victim_store.txt"].read_text(encoding="utf-8") == "precious\n"


def test_honest_state_still_cleans_up_its_backups(tmp_path):
    """A connect.json written by the current release (names are devmem's own) keeps working: backups go at disconnect."""
    project = tmp_path / "p"
    project.mkdir()
    (project / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    (project / ".cursor").mkdir()
    (project / ".cursor" / "mcp.json").write_text('{"mcpServers": {"x": {"command": "x"}}}\n', encoding="utf-8")
    do_connect(project, "cursor")
    bdir = project / ".devmemory" / "connect-backups"
    assert sorted(p.name for p in bdir.iterdir()) == sorted([C._backup_name("AGENTS.md"),
                                                              C._backup_name(".cursor/mcp.json")])
    r = disconnect(project, "cursor")
    assert not bdir.exists()
    assert not any("not devmem's own" in n for n in r["notes"])
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == "# rules\n"


def test_created_dirs_outside_the_targets_folders_are_never_removed(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    (project / "emptydir_inside").mkdir()
    do_connect(project, "cursor")
    sp = project / ".devmemory" / "connect.json"
    state = json.loads(sp.read_text(encoding="utf-8"))
    state["created"][".cursor/mcp.json"]["dirs"] = [".cursor", "emptydir_inside", "../emptydir_outside"]
    sp.write_text(json.dumps(state), encoding="utf-8")
    (tmp_path / "emptydir_outside").mkdir()
    disconnect(project, "cursor")
    assert not (project / ".cursor").exists()           # devmem's own folder for the file goes
    assert (project / "emptydir_inside").is_dir() and (tmp_path / "emptydir_outside").is_dir()


@pytest.mark.skipif(os.name == "nt", reason="planting a symbolic link needs privileges on Windows")
def test_a_planted_temporary_file_link_is_never_written_through(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    project = tmp_path / "p"
    (project / ".cursor").mkdir(parents=True)
    (project / ".cursor" / "mcp.json").write_text("{}\n", encoding="utf-8")
    os.symlink(victim, project / ".cursor" / ("mcp.json" + C.TMP_SUFFIX))
    os.symlink(victim, project / ("AGENTS.md" + C.TMP_SUFFIX))
    do_connect(project, "cursor")
    assert victim.read_text(encoding="utf-8") == "precious\n"
    assert "devmemory" in (project / ".cursor" / "mcp.json").read_text(encoding="utf-8")
