"""The viewer's store listing (/api/vault) never follows a link and stops at a depth and an entry count.

A store holding `loop -> .` (or a link to a big folder elsewhere) made _vault_tree walk until the path grew too long,
listing the same files over and over, or list a folder outside the store.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from ui_helpers import make_project, request, running_server, with_token

from devmem import ui_server


def _dir_link(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            raise
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))   # needs no privilege
        except (ImportError, AttributeError, OSError) as e:
            pytest.skip(f"cannot create a link here: {e}")


def _names(tree: list[dict]) -> list[str]:
    out = []
    for n in tree:
        out.append(n["path"])
        if n["type"] == "dir":
            out += _names(n["children"])
    return out


def test_a_link_loop_and_a_link_out_of_the_store_are_not_followed(tmp_path):
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)
    (store / "sessions" / "a.meta.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("not the store's\n", encoding="utf-8")
    _dir_link(store / "loop", store)
    _dir_link(store / "sessions" / "elsewhere", outside)
    tree, truncated = ui_server._vault_tree(store, store)
    names = _names(tree)
    assert names == ["sessions", "sessions/a.meta.json"], names
    assert not truncated


def test_depth_and_entry_count_are_capped_and_reported(tmp_path, monkeypatch):
    store = tmp_path / "store"
    deep = store
    # four levels past the cap, one-letter names: 30 levels of `d<i>` made a path that Windows (long paths off)
    # refused under a long --basetemp, before the code under test ran
    for i in range(ui_server.VAULT_TREE_MAX_DEPTH + 4):
        deep = deep / chr(ord("a") + i)
    deep.mkdir(parents=True)
    (deep / "bottom.txt").write_text("x", encoding="utf-8")
    tree, truncated = ui_server._vault_tree(store, store)
    assert truncated and "bottom.txt" not in " ".join(_names(tree))
    assert max(n.count("/") for n in _names(tree)) < ui_server.VAULT_TREE_MAX_DEPTH

    wide = tmp_path / "wide"
    wide.mkdir()
    for i in range(40):
        (wide / f"f{i:02}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(ui_server, "VAULT_TREE_MAX_ENTRIES", 10)
    tree, truncated = ui_server._vault_tree(wide, wide)
    assert truncated and len(tree) == 10


def test_the_page_is_told_when_the_listing_was_cut(tmp_path, monkeypatch):
    project, store = make_project(tmp_path)
    for i in range(20):
        (store / f"extra{i}.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(ui_server, "VAULT_TREE_MAX_ENTRIES", 5)
    with running_server(project, store) as (port, st):
        code, _h, body = request(port, "GET", "/api/vault", with_token(st))
    assert code == 200 and body["truncated"] is True and len(_names(body["tree"])) == 5
