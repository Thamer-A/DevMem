"""The gate analysis (evals/gate/analyze.py) on made-up result rows: no runs, no model calls."""
from __future__ import annotations

import json

import pytest

from evals.gate import analyze as A


def _row(arm, task, repeat, passed, kind="history", repo="r1", stage="A", hf=None, attempt=1, tokens=100.0):
    return {"stage": stage, "repo": repo, "task": task, "arm": arm, "repeat": repeat, "kind": kind,
            "passed": passed, "harness_failure": hf, "attempt": attempt, "price_weighted": tokens}


def _write(tmp_path, stage, rows):
    (tmp_path / f"{stage}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_sign_flip_is_exact():
    assert A.sign_flip_p([0.0] * 16) == 1.0
    assert A.sign_flip_p([1.0] * 16) == pytest.approx(2 / 2 ** 16)
    assert A.sign_flip_p([1.0, -1.0]) == 1.0
    assert A.sign_flip_p([1.0, 1.0, 0.0]) == pytest.approx(0.5)      # a zero difference flips to itself


def test_room_holds_when_the_oracle_beats_stock_by_twenty_points(tmp_path):
    rows = []
    for i in range(16):
        t = f"H{i:02d}"
        rows += [_row("O", t, 1, True), _row("O", t, 2, True),
                 _row("S", t, 1, i < 8), _row("S", t, 2, i < 8)]
    _write(tmp_path, "A", rows)
    result = A.analyse(*A.load(tmp_path, ["A"]))
    assert result["paired"]["O"]["gain_points"] == 50.0
    assert result["paired"]["O"]["p"] < 0.05
    assert result["thresholds"]["1_room"]["holds"] is True
    lo, hi = result["pass_rates"]["S/history"]["ci95"]
    assert lo < 0.5 < hi
    assert result["pass_rates"]["O/history"]["ci95"] == [1.0, 1.0]
    assert "2_delivery" not in result["thresholds"]


def test_room_fails_below_twenty_points(tmp_path):
    rows = []
    for i in range(16):
        t = f"H{i:02d}"
        rows += [_row("O", t, 1, i < 14), _row("O", t, 2, i < 14),
                 _row("S", t, 1, i < 12), _row("S", t, 2, i < 12)]
    _write(tmp_path, "A", rows)
    result = A.analyse(*A.load(tmp_path, ["A"]))
    assert result["paired"]["O"]["gain_points"] == 12.5
    assert result["thresholds"]["1_room"]["holds"] is False
    assert "FAILS" in A.report(result)


def test_a_harness_failure_is_replaced_by_its_rerun_and_counted(tmp_path):
    rows = [_row("S", "H01", 1, False, hf="interrupted", attempt=1), _row("S", "H01", 1, True, attempt=2),
            _row("O", "H01", 1, True)]
    _write(tmp_path, "A", rows)
    kept, log = A.load(tmp_path, ["A"])
    assert len(kept) == 2 and all(r["attempt"] == 2 or r["arm"] == "O" for r in kept)
    assert log["A"]["harness_failures"] == 1 and log["A"]["unresolved"] == 0
    assert log["A"]["planned"] == 64 and log["A"]["over_limit"] is False


def test_two_harness_failures_in_stage_a_are_over_the_limit(tmp_path):
    rows = [_row("S", "H01", 1, False, hf="x", attempt=1), _row("S", "H01", 1, True, attempt=2),
            _row("S", "H02", 1, False, hf="x", attempt=1), _row("S", "H02", 1, False, hf="x", attempt=2)]
    _write(tmp_path, "A", rows)
    rows_kept, log = A.load(tmp_path, ["A"])
    assert log["A"]["harness_failures"] == 3 and log["A"]["unresolved"] == 1 and log["A"]["over_limit"] is True
    assert len(rows_kept) == 1
    assert "OVER THE LIMIT" in A.report(A.analyse(rows_kept, log))


def test_delivery_and_no_harm_use_stage_a_stock_runs(tmp_path):
    a, b = [], []
    for i in range(16):
        t = f"H{i:02d}"
        a += [_row("O", t, r, True) for r in (1, 2)] + [_row("S", t, r, i < 4) for r in (1, 2)]
        b += [_row("D", t, r, i < 14, stage="B") for r in (1, 2)]
    for i in range(8):
        t = f"R{i:02d}"
        b += [_row("S", t, r, True, kind="ordinary", stage="B", tokens=100.0) for r in (1, 2)]
        b += [_row("D", t, r, i != 0 or r == 1, kind="ordinary", stage="B", tokens=105.0) for r in (1, 2)]
    _write(tmp_path, "A", a)
    _write(tmp_path, "B", b)
    result = A.analyse(*A.load(tmp_path, ["A", "B"]))
    t = result["thresholds"]
    assert t["1_room"]["holds"] and result["paired"]["O"]["gain_points"] == 75.0
    assert result["paired"]["D"]["gain_points"] == 62.5
    assert t["2_delivery"]["holds"] is True
    assert t["3_no_harm"]["S_minus_D_passes"] == 1 and t["3_no_harm"]["tokens_ratio"] == 1.05
    assert t["3_no_harm"]["holds"] is True


def test_the_command_writes_its_report(tmp_path, capsys):
    _write(tmp_path, "A", [_row("O", "H01", 1, True), _row("S", "H01", 1, False)])
    assert A.main(["--stages", "A", "--results", str(tmp_path)]) == 0
    saved = json.loads((tmp_path / "analysis-A.json").read_text(encoding="utf-8"))
    assert saved["thresholds"]["1_room"]["holds"] is True
    assert "threshold 1_room: HOLDS" in capsys.readouterr().out
