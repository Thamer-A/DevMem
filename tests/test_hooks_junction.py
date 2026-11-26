"""V7: the hooks never write through a Windows junction at `.devmemory` or one of its folders.

store_links_now and _not_a_link used os.path.islink only, which is False for a junction (mklink /J needs no special
rights), so a project could ship `.devmemory\\live` as a junction to any folder and the hooks wrote the event log
there. They now use devmem.store._is_link, which also recognises junctions (Python 3.10 to 3.13).
"""
from __future__ import annotations

import json
import os

import pytest
from hooks_helpers import load_payload, make_project

from devmem import hooks

pytestmark = pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows feature")


def _junction(link, target) -> None:
    import _winapi
    _winapi.CreateJunction(str(target), str(link))


@pytest.mark.parametrize("where", ["live", "store"])
def test_hooks_do_not_write_through_a_junction(tmp_path, monkeypatch, where):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if where == "live":
        _junction(store / "live", elsewhere)
    else:
        for p in sorted(store.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        store.rmdir()
        _junction(store, elsewhere)
    assert hooks.store_links_now(store, "s")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    for name in ("000_SessionStart", "002_UserPromptSubmit", "008_PostToolUse"):
        payload = load_payload(name, project)
        assert hooks.run([name.split("_", 1)[1]], json.dumps(payload).encode())[0] == 0
    assert list(elsewhere.iterdir()) == [], [p.name for p in elsewhere.iterdir()]
