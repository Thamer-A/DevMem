"""PostToolUse hook: run ruff on a Python file right after it is edited.

Exit 2 sends ruff's findings back to Claude so it fixes them in the same turn. Lint only, never
auto-format. Does nothing when ruff is missing or the file is not project Python.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SKIP = ("fixtures/", ".devmemory/", "tests/test_hooks_fixtures/")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    raw = (payload.get("tool_input") or {}).get("file_path") or ""
    if not raw.endswith(".py"):
        return 0
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".").resolve()
    target = Path(raw)
    if not target.is_absolute():
        target = root / target
    try:
        rel = target.resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return 0
    if not target.is_file() or rel.startswith(SKIP):
        return 0
    try:
        r = subprocess.run([sys.executable, "-m", "ruff", "check", "--quiet", rel], cwd=root, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=25)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if r.returncode == 0 or "No module named ruff" in (r.stderr or ""):
        return 0
    print(f"ruff found problems in {rel}:\n{(r.stdout or r.stderr).strip()[:3000]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
