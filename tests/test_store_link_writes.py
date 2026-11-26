"""A store that is a symbolic link, or that holds a linked folder devmem writes in, is never written through.

A cloned repository can ship `.devmemory -> /somewhere/else` (git creates the link on Linux and macOS by default).
The viewer, `devmem pack`, the MCP server and the begin/end commands used to call scan()/ensure_store on such a
store, which wrote `.gitignore`, `PROJECT.md`, `index/map.json` and `index/index.db` wherever the link pointed. The
check now sits in ensure_store (so every caller is covered); scan() and everything above it refuse, and the viewer
shows the project read-only.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import pytest
from cli_helpers import make_project
from ui_helpers import request, running_server, with_token

from devmem import generic
from devmem.index import Index
from devmem.mcp_server import Server
from devmem.scan import scan
from devmem.store import StoreLinkError, ensure_store


def _symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as e:      # Windows without Developer Mode or the privilege
        pytest.skip(f"cannot create a symbolic link here: {e}")


def _listing(d: Path) -> list[str]:
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*"))


@pytest.fixture(params=["store", "index", "sessions"])
def linked(tmp_path, request):
    """(project, store, elsewhere): the store itself, or one folder devmem writes in, links to `elsewhere`."""
    project = make_project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("victim\n", encoding="utf-8")
    store = project / ".devmemory"
    if request.param == "store":
        _symlink(store, elsewhere)
    else:
        store.mkdir()
        _symlink(store / request.param, elsewhere)
    return project, store, elsewhere


def test_ensure_store_and_scan_refuse_a_linked_store(linked):
    project, store, elsewhere = linked
    before = _listing(elsewhere)
    with pytest.raises(StoreLinkError):
        ensure_store(store)
    with pytest.raises(StoreLinkError):
        scan(project, store)
    assert _listing(elsewhere) == before


def test_index_on_a_linked_store_stays_in_memory(linked):
    project, store, elsewhere = linked
    before = _listing(elsewhere)
    with Index(store) as idx:
        assert idx.degraded and "link" in (idx.last_error or "")
        idx.sync(project)
    assert _listing(elsewhere) == before


def test_mcp_server_and_begin_refuse_a_linked_store(linked):
    project, store, elsewhere = linked
    before = _listing(elsewhere)
    for read_only in (True, False):
        r = Server(project, store, read_only=read_only).handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "get_context_pack", "arguments": {"task": "negative balance label"}}})
        assert r["result"]["isError"] is True and "link" in r["result"]["content"][0]["text"]
    with pytest.raises(StoreLinkError):
        generic.begin(project, store)
    assert _listing(elsewhere) == before


def test_cli_pack_and_doctor_write_nothing_through_a_linked_store(linked, capsys):
    from devmem.cli import main as cli_main
    project, store, elsewhere = linked
    before = _listing(elsewhere)
    assert cli_main(["pack", "negative balance", "--project", str(project)]) == 2
    assert "link" in capsys.readouterr().err
    cli_main(["doctor", "--project", str(project)])
    assert "[FAIL] links" in capsys.readouterr().out
    assert _listing(elsewhere) == before


def test_viewer_shows_a_linked_store_read_only(linked):
    project, store, elsewhere = linked
    before = _listing(elsewhere)
    with running_server(project, store) as (port, st):
        code, _h, body = request(port, "GET", "/api/overview", with_token(st))
        assert code == 200, body
        assert "link" in body["read_only"] and body["file_count"] == 0
        code, _h, body = request(port, "GET", "/api/files?rescan=1", with_token(st))
        assert code == 200 and body["files"] == [] and "link" in body["read_only"]
        q = f"/api/open?project={quote(str(project))}&scan=1"
        code, _h, body = request(port, "POST", q, with_token(st))
        assert code == 200 and body["ok"] and "link" in body["read_only"], body
        code, _h, body = request(port, "GET", "/api/overview", with_token(st))
        assert code == 200 and "link" in body["read_only"], body
    assert _listing(elsewhere) == before
