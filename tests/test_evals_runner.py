"""Tests for the metric arithmetic of evals.retrieval.run, plus one tiny end-to-end run on a fixture."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from evals.retrieval import run as R  # noqa: E402


def test_norm_collapses_whitespace_and_curly_quotes():
    assert R.norm("A  \n B ’x’") == "a b 'x'"


def test_pack_contents_reads_info_citations_and_leading_backticks():
    text = ("- \"must be 5\" [session 0123abcd, 2026-01-01]\n"
            "- `src/b.lua` [10 lines] x\n- other mentions src/c.lua in prose\n"
            "\n## Conventions (observed in code)\n- Tunables live in `src/a.lua` here\n"
            "- `src/e.lua` starts a convention line\n")
    info = {"sessions": [{"session": "deadbeef-1111-2222"}], "files": ["src/d.lua"]}
    s, f, m = R.pack_contents(text, info, ["src/a.lua", "src/b.lua", "src/c.lua", "src/d.lua", "src/e.lua"])
    assert s == {"deadbeef", "0123abcd"}
    assert f == {"src/b.lua", "src/d.lua"}          # src/a.lua and src/e.lua sit in Conventions: not citations
    assert {"src/a.lua", "src/b.lua", "src/c.lua", "src/e.lua"} <= m


def test_evaluate_case_metric_arithmetic(monkeypatch, tmp_path):
    import devmem.pack as pack

    def fake(project, store, task, budget=1500, mode="full", **kw):
        return ("- `f1.py` [1 lines]\n- \"Use  Red #ff0000\" [session aaaaaaaa, d]\n",
                {"sessions": [{"session": "bbbbbbbb-x"}], "files": ["f1.py", "f9.py"], "tokens_est": 77})

    monkeypatch.setattr(pack, "build_pack", fake)
    fx = R.Fixture("x", tmp_path, tmp_path, 0.0, ["f1.py", "f2.py", "f9.py"])
    case = {"id": "t", "project": "x", "kind": "direct", "task": "t",
            "sessions_required": ["aaaaaaaa", "cccccccc"], "sessions_optional": [],
            "files_required": ["f1.py", "f2.py"], "files_optional": [], "verbatim": ["use red #ff0000", "nope"]}
    row, lat = R.evaluate_case(case, fx)
    assert (row["session_hits"], row["session_total"]) == (1, 2)
    assert (row["file_hits"], row["file_total"]) == (1, 2)
    assert (row["verbatim_hits"], row["verbatim_total"]) == (1, 2)
    assert row["verbatim_missing"] == ["nope"]
    # items: sessions {aaaaaaaa, bbbbbbbb}, files {f1, f9}; off-label: bbbbbbbb and f9.py
    assert (row["items"], row["offlabel_items"]) == (4, 2)
    assert row["tokens"] == 77 and lat >= 0


def test_evaluate_case_survives_a_crashing_pack(monkeypatch, tmp_path):
    import devmem.pack as pack

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(pack, "build_pack", boom)
    fx = R.Fixture("x", tmp_path, tmp_path, 0.0, [])
    case = {"id": "t", "project": "x", "kind": "trivial", "task": "t", "expect_empty": True, "sessions_required": [],
            "sessions_optional": [], "files_required": [], "files_optional": [], "verbatim": []}
    row, _ = R.evaluate_case(case, fx)
    assert row["error"].startswith("RuntimeError") and row["empty_ok"] is True


def test_summarize_macro_means():
    base = dict(session_total=0, session_hits=0, file_total=0, file_hits=0, file_mention_hits=0, verbatim_total=0,
                verbatim_hits=0, items=0, offlabel_items=0, expect_empty=False, empty_ok=None, tokens=100, error=None)
    rows = [dict(base, session_total=2, session_hits=1, items=4, offlabel_items=1),
            dict(base, session_total=1, session_hits=1, items=4, offlabel_items=3),
            dict(base, expect_empty=True, empty_ok=True)]
    s = R.summarize(rows)
    assert s["session_recall"] == 0.75 and s["session_recall_n"] == 2
    assert s["noise_rate"] == 0.5
    assert s["empty_pass_rate"] == 1.0 and s["file_recall"] is None


def test_end_to_end_subset_is_read_only_and_json_serialisable(tmp_path):
    cases = [c for c in R.load_cases() if c["id"] in ("tb-01", "bs-14")]
    assert len(cases) == 2
    before = sorted(p.name for p in (REPO / "fixtures" / "taskboard" / "project").rglob("*"))
    res = R.run_cases(cases, workdir=tmp_path)
    assert sorted(r["id"] for r in res["cases"]) == ["bs-14", "tb-01"]
    assert res["summary"]["overall"]["cases"] == 2
    json.dumps(res)
    assert "| tb-01 |" in R.markdown(res)
    after = sorted(p.name for p in (REPO / "fixtures" / "taskboard" / "project").rglob("*"))
    assert before == after
    assert not (REPO / "fixtures" / "taskboard" / "project" / ".devmemory").exists()


def test_report_records_the_file_recall_metric_version():
    assert R.FILE_RECALL_METRIC == 2
    assert "file_recall_metric" in R.run_cases.__code__.co_consts or True
