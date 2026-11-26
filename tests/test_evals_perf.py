"""The perf probe on a tiny temp project: runs, reports sensible keys, leaves the target untouched."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from evals import perf  # noqa: E402


def test_probe_reports_and_does_not_touch_the_target(tmp_path):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "a.py").write_text("def parse(x):\n    return x\n", encoding="utf-8")
    (proj / "README.md").write_text("hello\n", encoding="utf-8")
    before = sorted(p.relative_to(proj).as_posix() for p in proj.rglob("*"))

    r = perf.probe(proj, ["fix parse", "yes go ahead"], repeats=2)

    assert r["cold_scan_s"] >= 0 and r["warm_scan_s"] >= 0
    assert r["files_on_disk"] == 2 and len(r["pack"]) == 2
    assert all(p["error"] is None for p in r["pack"])
    assert r["target_got_a_store"] is False
    assert before == sorted(p.relative_to(proj).as_posix() for p in proj.rglob("*"))
    assert "| proj |" in perf.table([r])
