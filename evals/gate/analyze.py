"""The gate experiment's analysis (evals/gate/PREREGISTRATION.md sections 6, 7 and 10). No model calls.

    python -m evals.gate.analyze --stages A            # Stage A: room (threshold 1)
    python -m evals.gate.analyze --stages A,B          # with Stage B: delivery and no harm (thresholds 2 and 3)

Reads `<results>/<stage>.jsonl` for each stage named and keeps one row per run (a run's harness-failure row is
replaced by its re-run, as the runner does), prints the report and writes it to `<results>/analysis-<stages>.json`.
Everything is computed from those rows, so the same command always gives the same numbers.

- Pass rate per arm and task kind, with a 95% interval from a bootstrap that resamples tasks (10,000 resamples,
  seed 20261126).
- Per-task paired difference against S, d_i = mean(arm's runs of task i) - mean(S's runs of task i), and the exact
  two-sided sign-flip permutation test on the d_i (statistic: their sum).
- Gain: the difference of pooled pass rates over the history-dependent tasks, in percentage points.
- Tokens: the median over runs of the price-weighted total, per arm and task kind.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from itertools import product
from pathlib import Path

from evals.gate import run as R

SEED = 20261126
RESAMPLES = 10_000
ROOM_POINTS = 20.0          # threshold 1: O over S
DELIVERY_POINTS = 15.0      # threshold 2: D over S
DELIVERY_SHARE = 2 / 3      # threshold 2: D's gain as a share of O's
ALPHA = 0.05
HARM_RUNS = 1               # threshold 3: D passes at most one run fewer than S on the ordinary tasks
HARM_TOKENS = 0.10          # threshold 3: D's median price-weighted tokens at most 10% above S's
HARNESS_SHARE = 0.02        # section 10: more than 2% harness failures stops a stage


def load(results: Path, stages: list[str]) -> tuple[list[dict], dict[str, dict]]:
    """The final row of every run in these stages, and per stage the count of rows and of harness failures."""
    rows: list[dict] = []
    log: dict[str, dict] = {}
    for stage in stages:
        path = results / f"{stage}.jsonl"
        if not path.is_file():
            raise SystemExit(f"no results for stage {stage}: {path} does not exist")
        all_rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        final = list(R._done(path).values())
        repeats = 1 if stage == "extension" else 2      # the extension is one third repeat (section 9)
        planned = len(R.plan(stage, R.repos_present(), repeats)) if stage in R.STAGES else None
        failures = sum(1 for r in all_rows if r.get("harness_failure"))
        log[stage] = {"planned": planned, "runs": len(final), "harness_failures": failures,
                      "unresolved": sum(1 for r in final if r.get("harness_failure")),
                      "harness_share": failures / planned if planned else None,
                      "over_limit": bool(planned) and failures / planned > HARNESS_SHARE}
        rows += [r for r in final if not r.get("harness_failure")]
    return rows, log


def _task(r: dict) -> tuple[str, str]:
    return (r["repo"], r["task"])


def by_task(rows: list[dict], arm: str, kind: str) -> dict[tuple, list[bool]]:
    out: dict[tuple, list[bool]] = {}
    for r in rows:
        if r["arm"] == arm and r["kind"] == kind:
            out.setdefault(_task(r), []).append(bool(r["passed"]))
    return out


def pooled(tasks: dict[tuple, list[bool]], keys=None) -> float:
    """Pooled pass rate over these tasks' runs (every task in `tasks` has at least one run)."""
    runs = [p for k in (tasks if keys is None else keys) for p in tasks[k]]
    return sum(runs) / len(runs)


def bootstrap(tasks: dict[tuple, list[bool]]) -> tuple[float, float] | None:
    """95% interval of the pooled pass rate, resampling tasks (the repeats of one task are not independent)."""
    keys = sorted(tasks)
    if not keys:
        return None
    rng = random.Random(SEED)
    stats = sorted(pooled(tasks, rng.choices(keys, k=len(keys))) for _ in range(RESAMPLES))
    return stats[int(0.025 * RESAMPLES)], stats[int(0.975 * RESAMPLES) - 1]


def sign_flip_p(d: list[float]) -> float:
    """Exact two-sided sign-flip permutation test; statistic: the sum of the d_i."""
    obs = abs(sum(d))
    nonzero = [x for x in d if x]
    hits = sum(1 for signs in product((1, -1), repeat=len(nonzero))
               if abs(sum(s * x for s, x in zip(signs, nonzero))) >= obs - 1e-12)
    return hits / 2 ** len(nonzero)


def compare(rows: list[dict], arm: str, kind: str = "history") -> dict | None:
    """The arm against S on the tasks both ran: per-task d_i, the gain in points and the paired test."""
    a, s = by_task(rows, arm, kind), by_task(rows, "S", kind)
    keys = sorted(set(a) & set(s))
    if not keys:
        return None
    d = [statistics.mean(a[k]) - statistics.mean(s[k]) for k in keys]
    return {"arm": arm, "kind": kind, "tasks": len(keys),
            "gain_points": round(100 * (pooled(a, keys) - pooled(s, keys)), 2),
            "p": sign_flip_p(d),
            "d": {f"{k[0]}/{k[1]}": round(x, 3) for k, x in zip(keys, d)}}


def median_tokens(rows: list[dict], arm: str, kind: str) -> float | None:
    vals = [r["price_weighted"] for r in rows
            if r["arm"] == arm and r["kind"] == kind and r.get("price_weighted") is not None]
    return statistics.median(vals) if vals else None


def analyse(rows: list[dict], log: dict) -> dict:
    cells = sorted({(r["arm"], r["kind"]) for r in rows})
    rates = {}
    for arm, kind in cells:
        tasks = by_task(rows, arm, kind)
        ci = bootstrap(tasks) or (0.0, 0.0)
        rates[f"{arm}/{kind}"] = {"tasks": len(tasks), "runs": sum(len(v) for v in tasks.values()),
                                  "passed": sum(sum(v) for v in tasks.values()),
                                  "rate": round(pooled(tasks), 4),
                                  "ci95": [round(ci[0], 4), round(ci[1], 4)],
                                  "median_price_weighted": median_tokens(rows, arm, kind)}
    o, d = compare(rows, "O"), compare(rows, "D")
    thresholds: dict[str, dict] = {}
    if o:
        thresholds["1_room"] = {"holds": o["gain_points"] >= ROOM_POINTS,
                                "rule": f"O beats S by at least {ROOM_POINTS:g} points on the history tasks",
                                "O_gain_points": o["gain_points"]}
    if d:
        size = d["gain_points"] >= DELIVERY_POINTS
        share = bool(o) and o["gain_points"] > 0 and d["gain_points"] >= DELIVERY_SHARE * o["gain_points"]
        thresholds["2_delivery"] = {"holds": size and share and d["p"] < ALPHA, "size": size,
                                    "share_of_O": share if o else None, "p_below_alpha": d["p"] < ALPHA,
                                    "D_gain_points": d["gain_points"], "p": d["p"]}
    ds, ss = by_task(rows, "D", "ordinary"), by_task(rows, "S", "ordinary")
    if ds and ss:
        fewer = sum(sum(v) for v in ss.values()) - sum(sum(v) for v in ds.values())
        mt_d, mt_s = median_tokens(rows, "D", "ordinary"), median_tokens(rows, "S", "ordinary")
        tokens_ok = mt_d is not None and mt_s is not None and mt_d <= (1 + HARM_TOKENS) * mt_s
        thresholds["3_no_harm"] = {"holds": fewer <= HARM_RUNS and tokens_ok, "S_minus_D_passes": fewer,
                                   "tokens_ratio": round(mt_d / mt_s, 4) if mt_d and mt_s else None}
    return {"stages": log, "pass_rates": rates,
            "paired": {c["arm"]: c for c in (o, d) if c}, "thresholds": thresholds}


def report(a: dict) -> str:
    out = []
    for stage, s in a["stages"].items():
        line = (f"stage {stage}: {s['runs']} of {s['planned']} runs with a result, "
                f"{s['harness_failures']} harness failure(s)")
        if s["harness_share"] is not None:
            line += f" ({100 * s['harness_share']:.1f}%, limit {100 * HARNESS_SHARE:g}%)"
        if s["over_limit"]:
            line += "  OVER THE LIMIT: the stage restarts from its first run"
        if s["unresolved"]:
            line += f"  {s['unresolved']} run(s) still a harness failure after the re-run"
        out.append(line)
    out.append("")
    out.append("pass rates (95% interval, bootstrap over tasks):")
    for cell, r in a["pass_rates"].items():
        tok = r["median_price_weighted"]
        out.append(f"  {cell:<14} {r['passed']:>3}/{r['runs']:<3} = {100 * r['rate']:5.1f}%  "
                   f"[{100 * r['ci95'][0]:5.1f}, {100 * r['ci95'][1]:5.1f}]  "
                   f"median weighted tokens {tok if tok is None else round(tok)}")
    for arm, c in a["paired"].items():
        out.append("")
        out.append(f"{arm} against S on {c['tasks']} history tasks: gain {c['gain_points']:+.1f} points, "
                   f"sign-flip p = {c['p']:.4f}")
        out.append("  per task d_i: " + ", ".join(f"{k} {v:+.2f}" for k, v in c["d"].items()))
    out.append("")
    if not a["thresholds"]:
        out.append("no threshold can be evaluated from these rows")
    for name, t in a["thresholds"].items():
        out.append(f"threshold {name}: {'HOLDS' if t['holds'] else 'FAILS'}  "
                   + ", ".join(f"{k}={v}" for k, v in t.items() if k not in ("holds", "rule")))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.gate.analyze", description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--stages", required=True, help="comma-separated, e.g. A or A,B")
    ap.add_argument("--results", type=Path, default=R.RESULTS)
    ap.add_argument("--no-write", action="store_true", help="print only")
    a = ap.parse_args(argv)
    stages = [s.strip() for s in a.stages.split(",") if s.strip()]
    rows, log = load(a.results, stages)
    result = analyse(rows, log)
    print(report(result))
    if not a.no_write:
        path = a.results / f"analysis-{'-'.join(stages)}.json"
        path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"\nwritten to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
