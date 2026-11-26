"""A stream line longer than MAX_LINE_PARSE_CHARS is a counted stub, never parsed (json holds about 35-100 times a
line of small values: fourth review, one 126 MB line was killed at 3 GB); a line at the limit, of the shape that holds
the most per byte, stays under the stated memory (SECURITY.md)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import capture as C

ROOT = Path(__file__).resolve().parent.parent


def _line(size: int, unit: str) -> str:
    head, tail = '{"type":"assistant","message":{"content":[', "]}}"
    n = (size - len(head) - len(tail)) // (len(unit) + 1)
    return head + ",".join([unit] * n) + tail


def test_a_line_over_the_limit_is_a_stub_and_never_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_LINE_PARSE_CHARS", 50_000)
    project = make_project(tmp_path)
    parsed = []
    real = json.loads
    monkeypatch.setattr(C.json, "loads", lambda s, *a, **k: parsed.append(len(s)) or real(s, *a, **k))
    init = json.dumps({"type": "system", "subtype": "init", "session_id": "s1"})
    end = json.dumps({"type": "result", "result": "done", "session_id": "s1"})
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join([init, _line(60_000, '{"type":"tool_use"}'), _line(49_000, "{}"), end]) + "\n",
                    encoding="utf-8")
    info: dict = {}
    meta = C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory", info=info)
    assert not [n for n in parsed if 59_000 <= n <= 60_000] and info["stream_lines_not_read"] == 1
    rec = json.loads(meta.read_text(encoding="utf-8"))
    raw = meta.with_name(meta.name.replace(".meta.json", ".jsonl")).read_text(encoding="utf-8").splitlines()
    assert "MAX_LINE_PARSE_CHARS" in raw[1] and len(raw[1]) < 300 and rec["result_text"] == "done"


@pytest.mark.stress
@pytest.mark.skipif(sys.platform == "win32", reason="the memory cap is set with resource.setrlimit")
@pytest.mark.parametrize("unit", ["[" * 60 + "]" * 60, '{"a":0}', "{}"])
def test_the_worst_line_at_the_limit_stays_under_the_stated_memory(tmp_path, unit):
    project = make_project(tmp_path)
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}) + "\n"
                    + _line(C.MAX_LINE_PARSE_CHARS - 100, unit) + "\n", encoding="utf-8")
    code = ("import resource, sys\n"
            "resource.setrlimit(resource.RLIMIT_AS, (2048 * 1024 * 1024, 2048 * 1024 * 1024))\n"
            "from devmem import capture as C\n"
            f"C.record_session({str(project)!r}, {str(path)!r}, 'x', '2026-11-16T00:00:00Z', "
            f"store={str(project / '.devmemory')!r})\n"
            "print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)\n")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "plugin"))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    assert int(r.stdout.split()[-1]) < 500, r.stdout          # MB; SECURITY.md states the measured worst (419 MB)
