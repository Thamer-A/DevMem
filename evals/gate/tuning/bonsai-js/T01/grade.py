"""Grader for gate task bonsai-js T01.

Copies the files in hidden/ into the working copy's tests/ folder, runs the repository's whole vitest suite once
with them (offline: the Postgres and MongoDB tests skip themselves without QUERY_SERVERS, which is never set; the one
test that runs the `bun` binary is left out by name), then removes them again. The task passes when every hidden
test and every other test passes. Standard library only.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The whole suite takes about 20 s; this leaves room on a slow machine and stays inside the task's timeout_s (300).
RUN_TIMEOUT_S = 240
# tests/round6-api.test.ts "keeps the names in a minified bundle" runs `bun build`; bun is not part of the install.
SKIP_PATTERN = "^(?!.*minified bundle)"
REPORT = ".gate-vitest-report.json"


def _vitest(work: Path, env: dict) -> list[str]:
    node = shutil.which("node", path=env.get("PATH", ""))
    if node is None:
        raise RuntimeError("no node on the tool environment's PATH")
    entry = work / "node_modules" / "vitest" / "vitest.mjs"
    # --no-cache and the runner config loader keep vitest from writing into the shared node_modules.
    # --retry=2: the suite has wall-clock assertions (tests/limits.test.ts, tests/partial.test.ts and others) that a
    # loaded machine can miss by luck. A test that passes on a retry is "passed" in the JSON report, and so is its
    # file (checked with vitest 4.1.12); one that fails all three attempts is "failed".
    return [node, str(entry), "run", "--no-cache", "--configLoader", "runner", "--maxWorkers=4", "--retry=2",
            "--reporter=json", "--outputFile", str(work / REPORT), "--testNamePattern", SKIP_PATTERN]


def grade(work: Path, env: dict) -> dict:
    work = Path(work)
    checks: dict[str, bool] = {}
    hidden = sorted((HERE / "hidden").glob("*.test.ts"))
    names = {src.name for src in hidden}
    placed: list[tuple[Path, bytes | None]] = []
    report = work / REPORT
    data = None
    tail = ""
    try:
        for src in hidden:
            dest = work / "tests" / src.name
            placed.append((dest, dest.read_bytes() if dest.exists() else None))
            shutil.copyfile(src, dest)
        report.unlink(missing_ok=True)
        try:
            p = subprocess.run(_vitest(work, env), cwd=work, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return {"passed": False, "grader_timeout": True, "checks": {"suite_finished": False}}
        tail = ((p.stdout or "") + (p.stderr or ""))[-3000:]
        if report.is_file():
            data = json.loads(report.read_text(encoding="utf-8"))
    finally:
        for dest, before in placed:
            if before is None:
                dest.unlink(missing_ok=True)
            else:
                dest.write_bytes(before)
        report.unlink(missing_ok=True)
    checks["suite_report"] = data is not None
    hidden_ran = {name: False for name in names}
    existing_ok = data is not None
    failures: list[str] = []
    for result in (data or {}).get("testResults", []):
        file_name = Path(result.get("name", "")).name
        assertions = result.get("assertionResults", [])
        if file_name in names:
            hidden_ran[file_name] = bool(assertions)
            for a in assertions:
                key = "hidden:" + a.get("title", "?")
                checks[key] = a.get("status") == "passed" and checks.get(key, True)
            if result.get("status") == "failed" and not assertions:
                failures.append(f"{file_name}: {str(result.get('message', ''))[:300]}")
        else:
            bad = [a.get("fullName", "?") for a in assertions if a.get("status") == "failed"]
            if bad or result.get("status") == "failed":
                existing_ok = False
                failures.append(f"{file_name}: {bad[:3] or str(result.get('message', ''))[:300]}")
    for name, ran in hidden_ran.items():
        checks[f"hidden_ran:{name}"] = ran
    checks["existing_tests"] = existing_ok
    return {"passed": all(checks.values()), "checks": checks, "output": {"failures": failures[:20], "tail": tail}}
