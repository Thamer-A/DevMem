"""Test a commit exactly as committed: check it out into a temporary worktree, run the test suite
and ruff there, then remove the worktree. Uncommitted changes are not part of the check.

    python .claude/skills/prepush/scripts/prepush.py            # HEAD, on this machine
    python .claude/skills/prepush/scripts/prepush.py --linux    # also run the suite in Docker (python:3.10-slim)
    python .claude/skills/prepush/scripts/prepush.py --ref <commit>

Exit code 0 only if every step passed.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run(cmd, cwd, **kw):
    return subprocess.run(cmd, cwd=str(cwd), text=True, encoding="utf-8", errors="replace", **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", default="HEAD", help="commit to test (default HEAD)")
    ap.add_argument("--linux", action="store_true", help="also run the suite on Linux in Docker")
    ap.add_argument("--image", default="python:3.10-slim", help="Docker image for --linux")
    a = ap.parse_args()

    top = run(["git", "rev-parse", "--show-toplevel"], ".", capture_output=True)
    if top.returncode:
        print("not inside a git repository", file=sys.stderr)
        return 2
    repo = Path(top.stdout.strip())
    sha = run(["git", "rev-parse", "--short", a.ref], repo, capture_output=True).stdout.strip()
    dirty = run(["git", "status", "--porcelain"], repo, capture_output=True).stdout.strip()
    print(f"prepush: testing {a.ref} ({sha}) as committed")
    if dirty:
        lines = dirty.splitlines()
        print(f"note: {len(lines)} uncommitted change(s) are NOT included in this check:")
        for line in lines[:15]:
            print("   " + line)

    tmp = Path(tempfile.mkdtemp(prefix="devmem-prepush-"))
    wt = tmp / "wt"
    results = []
    try:
        if run(["git", "worktree", "add", "--detach", "-q", str(wt), a.ref], repo).returncode:
            print("could not create the temporary worktree", file=sys.stderr)
            return 2
        # Subprocesses the tests start must import this commit's package, not the editable install.
        # The package lives in plugin/ (the Claude Code plugin folder ships the same code).
        env = {**os.environ, "PYTHONPATH": str(wt / "plugin")}
        steps = [("tests", [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]),
                 ("ruff", [sys.executable, "-m", "ruff", "check", "plugin/devmem", "tests", "evals"])]
        if a.linux:
            inner = ("apt-get update -qq >/dev/null && apt-get install -y -qq git >/dev/null "
                     "&& cp -r /src /work && rm -rf /work/.git && cd /work "
                     "&& pip install -q --root-user-action=ignore pytest >/dev/null "
                     "&& python -m pytest tests -q -p no:cacheprovider")
            steps.append((f"linux ({a.image})", ["docker", "run", "--rm", "-v", f"{wt}:/src:ro", a.image, "sh", "-c", inner]))
        for name, cmd in steps:
            t0 = time.time()
            try:
                r = run(cmd, wt, env=env, capture_output=True)
                ok = r.returncode == 0
                # on success show the tool's own summary (stdout); on failure show everything
                shown = r.stdout if ok and r.stdout.strip() else r.stdout + "\n" + r.stderr
                tail = [x for x in shown.strip().splitlines() if x.strip()][-12:]
            except OSError as e:
                tail, ok = [f"could not run: {e}"], False
            results.append((name, ok))
            print(f"\n[{'PASS' if ok else 'FAIL'}] {name}  ({time.time() - t0:.0f}s)")
            for line in (tail if not ok else tail[-2:]):
                print("   " + line)
    finally:
        run(["git", "worktree", "remove", "--force", str(wt)], repo, capture_output=True)
        run(["git", "worktree", "prune"], repo, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok in results if not ok]
    print("\nprepush: " + ("all steps passed" if not failed else "FAILED: " + ", ".join(failed)) + f"  ({sha})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
