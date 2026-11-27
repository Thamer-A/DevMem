"""Validate the gate's tasks and graders (evals/gate/TASK-FORMAT.md). No model calls.

For every task: the starting code fails its grader, starting code + reference.patch passes, starting code +
guess.patch fails. Also checks that the files agree with each other: each oracle is its requirement's text, each
requirement's text is in its session's prompt, kinds and counts are as pre-registered.

    python -m evals.gate.validate md2conf                 # every scored and tuning task of one repository
    python -m evals.gate.validate md2conf --task H03      # one task
    python -m evals.gate.validate md2conf --static        # file checks only (no copies, no graders)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from evals.gate import workspace as W

COUNTS = {"history": 8, "ordinary": 4, "tuning": 3}


def static_checks(name: str) -> list[str]:
    problems = []
    cfg = W.load_repo(name)
    for key in ("name", "url", "commit", "language", "license", "deps", "test", "test_timeout_s"):
        if key not in cfg:
            problems.append(f"repo.json: no {key!r}")
    if len(str(cfg.get("commit", ""))) != 40:
        problems.append("repo.json: commit is not a full SHA")
    sessions = W.load_history(name)
    reqs = {}
    for s in sessions:
        if not (cfg["dir"] / "history" / s["patch"]).is_file():
            problems.append(f"history session {s['n']}: no patch {s['patch']}")
        for r in s.get("requirements", []):
            if r["text"] not in s["prompt"]:
                problems.append(f"requirement {r['id']}: its text is not verbatim in session {s['n']}'s prompt")
            if r["id"] in reqs:
                problems.append(f"requirement {r['id']}: id used twice")
            reqs[r["id"]] = r["text"]
    if not 5 <= len(sessions) <= 6:
        problems.append(f"history: {len(sessions)} sessions, expected 5 or 6")
    kinds: dict[str, int] = {}
    for tuning in (False, True):
        for d in W.task_dirs(name, tuning):
            t = W.load_task(d)
            kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
            where = f"{'tuning' if tuning else 'task'} {t['id']}"
            if (t["kind"] == "tuning") != tuning:
                problems.append(f"{where}: kind {t['kind']!r} in the wrong folder")
            for f in ("grade.py", "reference.patch", "guess.patch"):
                if not (d / f).is_file():
                    problems.append(f"{where}: no {f}")
            if t["kind"] == "history" and not t.get("requirement"):
                problems.append(f"{where}: a history task without a requirement")
            if t["kind"] == "ordinary" and (t.get("requirement") or t.get("oracle")):
                problems.append(f"{where}: an ordinary task with a requirement")
            if t.get("requirement"):
                if t["requirement"] not in reqs:
                    problems.append(f"{where}: requirement {t['requirement']} is not in the history")
                elif t.get("oracle") != reqs[t["requirement"]]:
                    problems.append(f"{where}: oracle is not the verbatim text of {t['requirement']}")
            for key in ("id", "repo", "kind", "prompt", "max_turns", "timeout_s"):
                if key not in t:
                    problems.append(f"{where}: no {key!r}")
    for k, n in COUNTS.items():
        if kinds.get(k, 0) != n:
            problems.append(f"{k} tasks: {kinds.get(k, 0)}, pre-registered {n}")
    return problems


def check_task(cfg: dict, root: Path, d: Path, env_base: dict) -> list[str]:
    t = W.load_task(d)
    deps = W.deps(cfg, root)
    problems = []
    for variant, patch, want in (("starting code", None, False), ("reference", d / "reference.patch", True),
                                 ("guess", d / "guess.patch", False)):
        work = root / "validate" / f"{cfg['name']}-{t['id']}-{variant.replace(' ', '_')}"
        W.rmtree(work)
        work.parent.mkdir(parents=True, exist_ok=True)
        W.work_copy(cfg, root, work)
        try:
            if patch is not None:
                W.apply_patch(work, patch)
            t0 = time.monotonic()
            res = W.grade(t, work, W.tool_env(cfg, deps, work, env_base))
            secs = time.monotonic() - t0
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            problems.append(f"{t['id']} {variant}: {exc!r}"[:400])
            continue
        finally:
            W.rmtree(work)
        mark = "ok" if res["passed"] == want else "WRONG"
        print(f"  {t['id']:>4} {variant:<13} {'pass' if res['passed'] else 'fail'} (want {'pass' if want else 'fail'})"
              f" {secs:5.1f}s {mark}" + (f" error={res.get('error')}" if res.get("error") else ""))
        if res["passed"] != want:
            failed = [k for k, v in (res.get("checks") or {}).items() if not v]
            problems.append(f"{t['id']} {variant}: grader says {'pass' if res['passed'] else 'fail'}, "
                            f"expected {'pass' if want else 'fail'} (failed checks: {failed})")
        if secs > t.get("timeout_s", 600):
            problems.append(f"{t['id']} {variant}: grading took {secs:.0f}s, over the task's timeout_s")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.gate.validate", description=__doc__.split("\n\n")[0])
    ap.add_argument("repo")
    ap.add_argument("--task", action="append", help="only this task id (repeatable)")
    ap.add_argument("--static", action="store_true", help="file checks only")
    ap.add_argument("--root", type=Path, default=W.DEFAULT_ROOT)
    a = ap.parse_args(argv)
    problems = static_checks(a.repo)
    for p in problems:
        print("STATIC", p)
    if a.static:
        return 1 if problems else 0
    cfg = W.load_repo(a.repo)
    import os
    env_base = dict(os.environ)
    dirs = W.task_dirs(a.repo) + W.task_dirs(a.repo, tuning=True)
    for d in dirs:
        if a.task and d.name not in a.task:
            continue
        problems += check_task(cfg, a.root, d, env_base)
    print(f"{len(problems)} problem(s)")
    for p in problems:
        print("  -", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
