"""Grader for gate task md2conf H02.

Copies the files in hidden/ into the working copy's tests/ folder, runs them, removes them again, then runs the
repository's own test suite (tests/, offline). The task passes when both runs pass. Standard library only.

Both pytest runs share one time budget: the task's `timeout_s` (task.json) minus a margin for copying and cleanup.
When that budget cuts a run short, the result has `"grader_timeout": True` and `passed` False.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARGIN_S = 20           # kept back from timeout_s for starting Python, copying and removing the hidden files
MIN_RUN_S = 5           # a run with less time than this left is not started (counted as a grader timeout)


def _budget_s() -> float:
    task = json.loads((HERE / "task.json").read_text(encoding="utf-8"))
    return max(float(task.get("timeout_s", 300)) - MARGIN_S, 2 * MIN_RUN_S)


def _python(env: dict) -> str:
    exe = shutil.which("python", path=env.get("PATH", ""))
    if exe is None:
        raise RuntimeError("no python on the tool environment's PATH")
    return exe


def _pytest(work: Path, env: dict, targets: list[str], deadline: float) -> tuple[int | None, str]:
    "Runs pytest; returns (exit code, output), with exit code None when the time budget ran out."

    remaining = deadline - time.monotonic()
    if remaining < MIN_RUN_S:
        return None, "not started: grader time budget used up"
    try:
        p = subprocess.run([_python(env), "-m", "pytest", *targets, "-q", "-rA", "-p", "no:cacheprovider"], cwd=work,
                           env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=remaining)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {remaining:.0f}s (grader time budget)"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _outcomes(output: str) -> dict[str, bool]:
    found: dict[str, bool] = {}
    for m in re.finditer(r"^(PASSED|FAILED|ERROR|SUBFAILED)(?:\(.*?\))? (tests/test_gate_\S+?)(?: - .*)?$", output, re.MULTILINE):
        name = m.group(2).split("::", 1)[-1]
        found["hidden:" + name] = m.group(1) == "PASSED" and found.get("hidden:" + name, True)
    return found


def grade(work: Path, env: dict) -> dict:
    work = Path(work)
    deadline = time.monotonic() + _budget_s()
    checks: dict[str, bool] = {}
    hidden = sorted((HERE / "hidden").glob("*.py"))
    placed: list[tuple[Path, bytes | None]] = []
    try:
        for src in hidden:
            dest = work / "tests" / src.name
            placed.append((dest, dest.read_bytes() if dest.exists() else None))
            shutil.copyfile(src, dest)
        code, out = _pytest(work, env, [f"tests/{src.name}" for src in hidden], deadline)
    finally:
        for dest, before in placed:
            if before is None:
                dest.unlink(missing_ok=True)
            else:
                dest.write_bytes(before)
    checks["hidden_tests"] = code == 0
    checks.update(_outcomes(out))
    if code is None:
        code_all, out_all = None, "not started: hidden tests ran out of time"
    else:
        code_all, out_all = _pytest(work, env, ["tests"], deadline)
    checks["existing_tests"] = code_all == 0
    result = {"passed": all(checks.values()), "checks": checks,
              "output": {"hidden": out[-4000:], "existing": out_all[-2000:]}}
    if code is None or code_all is None:
        result["passed"] = False
        result["grader_timeout"] = True
    return result
