"""`python -m devmem` (an MCP server entry, `devmem codex`) never imports a module from the current folder."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"


@pytest.mark.parametrize("module", ["devmem", "devmem.cli"])
def test_python_dash_m_devmem_never_imports_from_the_current_folder(tmp_path, module):
    """An MCP client starts `python -m devmem mcp` in the project (connect --command python): a json.py there must
    not replace the standard library's in the server (the parent of every refresh)."""
    sentinel = tmp_path / "imported.txt"
    (tmp_path / "json.py").write_text(f"open({str(sentinel)!r}, 'a').write('x')\n", encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", module, "--version"], cwd=tmp_path, capture_output=True, timeout=60,
                       env={**os.environ, "PYTHONPATH": str(PLUGIN)})
    assert r.returncode == 0, r.stderr
    assert not sentinel.exists()
