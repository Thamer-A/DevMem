"""A scan inside a hook or the MCP server has a time budget: on overrun what is indexed is kept, the map says the
scan is incomplete, the prompt is never blocked, and the next scan finishes the rest."""
from __future__ import annotations

import json

from hooks_helpers import load_payload, make_project

from devmem import hooks, mcp_server
from devmem.scan import scan


def _many_files(project, n=12):
    for i in range(n):
        (project / f"mod{i}.py").write_text(f"def handler_{i}():\n    return {i}\n", encoding="utf-8")


def test_scan_out_of_time_keeps_what_it_indexed_and_says_so(tmp_path):
    project = make_project(tmp_path)
    _many_files(project)
    store = project / ".devmemory"
    m = scan(project, store, time_budget=0.0)
    si = m["scan"]
    assert si["incomplete"] is True and si["pending"] > 0
    assert 0 < len(m["files"]) < 12 + 1
    assert "Scan incomplete" in (store / "PROJECT.md").read_text(encoding="utf-8")
    m2 = scan(project, store)                      # no budget: finishes
    assert "scan" not in m2 and all(f"mod{i}.py" in m2["files"] for i in range(12))


def test_hook_scan_uses_the_budget_and_resumes_on_the_next_prompt(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    (project / ".devmemory" / "config.json").unlink()   # delivery on
    _many_files(project)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setattr(hooks, "HOOK_SCAN_BUDGET", 0.0)
    payload = dict(load_payload("002_UserPromptSubmit", project), prompt="change handler_3 return value")
    code, _out = hooks.run(["UserPromptSubmit"], json.dumps(payload).encode())
    assert code == 0                                # no time for a refresh: the prompt goes on without one
    assert not (project / ".devmemory" / "index" / "map.json").exists()
    monkeypatch.setattr(hooks, "HOOK_SCAN_BUDGET", 60.0)
    code, _out = hooks.run(["UserPromptSubmit"], json.dumps(payload).encode())
    m = json.loads((project / ".devmemory" / "index" / "map.json").read_text(encoding="utf-8"))
    assert code == 0 and "scan" not in m


def test_mcp_scan_is_budgeted(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    _many_files(project)
    monkeypatch.setattr(mcp_server, "MCP_SCAN_BUDGET", 0.0)
    srv = mcp_server.Server(project, project / ".devmemory")
    r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "get_context_pack", "arguments": {"task": "handler_3 return value"}}})
    assert r["result"]["isError"] is False
    assert not (project / ".devmemory" / "index" / "map.json").exists()     # no time for a refresh, no scan either
