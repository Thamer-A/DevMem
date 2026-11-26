---
name: prepush
description: Test the commit that is about to be pushed exactly as committed, in a temporary worktree (tests and ruff, optionally the suite on Linux in Docker). Use before pushing, when asked to check that a commit is safe to push, or when asked to "run prepush".
---

# prepush

The working tree often holds uncommitted files, so a passing test run there does not prove the commit
passes. This checks out the commit into a temporary worktree, runs the suite and ruff there, and removes
the worktree afterwards.

Run from the repository root:

```bash
python .claude/skills/prepush/scripts/prepush.py            # HEAD on this machine, about 90 s
python .claude/skills/prepush/scripts/prepush.py --linux    # also the suite in Docker (python:3.10-slim), a few minutes
python .claude/skills/prepush/scripts/prepush.py --ref <commit>
```

Arguments passed to the skill (for example `--linux`) go straight to the script.

How to use the result:

- Exit code 0 means every step passed for that commit. Report the commit hash and the counts.
- If the script notes uncommitted changes, say so: they were not tested and will not be pushed.
- If a step fails, show the failing lines and stop. Do not push, and do not "fix" a failure by editing
  tests, fixtures or evaluation cases to match.
- Use `--linux` when the change touches file paths, locking, subprocesses, encodings or the hook
  launcher, since CI only reports Linux results after a push. It needs Docker running.
- If `pack.py`, `text.py`, `index.py` or `scan.py` changed, run `/retrieval-eval` as well.
