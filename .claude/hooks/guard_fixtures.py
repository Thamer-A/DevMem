"""PreToolUse hook: refuse edits to recorded fixtures and evaluation cases.

Reads the Claude Code hook JSON on stdin. Exit 2 blocks the edit and shows the message to Claude;
any other outcome (including an error in this script) lets the edit through.
"""
import json
import os
import sys
from pathlib import Path

GUARDED = ("fixtures", "evals/retrieval/cases", "tests/test_hooks_fixtures")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    inp = payload.get("tool_input") or {}
    raw = inp.get("file_path") or inp.get("notebook_path") or ""
    if not raw:
        return 0
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".").resolve()
    target = Path(raw)
    if not target.is_absolute():
        target = root / target
    try:
        rel = target.resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return 0  # outside this project
    if os.name == "nt":
        rel = rel.lower()
    for g in GUARDED:
        if rel == g or rel.startswith(g + "/"):
            print(f"Blocked: {rel} is under {g}/, which holds recorded fixtures or labelled evaluation cases. "
                  "They are kept byte-for-byte so tests and evaluation scores stay comparable. "
                  "Work on a copy in a temp directory instead. If a fixture or label is wrong, tell the user; "
                  "do not change it to make a test or score pass.", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
