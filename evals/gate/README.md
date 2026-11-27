# Gate experiment

Does DevMem beat stock Claude Code (automatic memory on) on tasks that depend on a requirement the user stated in an
earlier session? The question, arms, counts, measures, thresholds and stop rules are fixed in
[PREREGISTRATION.md](PREREGISTRATION.md); the task files' layout is in [TASK-FORMAT.md](TASK-FORMAT.md).

| File | What |
|---|---|
| `PREREGISTRATION.md` | committed before anything was run; amendments only before Stage A |
| `power.py` | the smallest effect the design can detect (`python -m evals.gate.power`) |
| `TASK-FORMAT.md`, `tasks/`, `tuning/` | the repositories, scripted histories, tasks and graders |
| `workspace.py` | pinned source, shared dependencies, the starting code, fresh working copies, grading |
| `validate.py` | every grader rejects the starting code and the plausible guess and accepts the reference |
| `verify_freeze.py`, `MANIFEST.sha256` | the hash freeze of the task set; the runner refuses a changed set |
| `run.py`, `runner.json` | the runner (pinned Claude Code version, model, flags) and the history replay |
| `fake_claude.py` | a stand-in for `claude -p` used by `tests/test_gate_runner.py`, `tests/test_gate_calibrate.py` and `--fake` dry runs |
| `analyze.py` | section 6 and 7: pass rates with task-bootstrap intervals, per-task differences against S, the sign-flip test and the thresholds, from `results/<stage>.jsonl` (`--stages A`, `--stages A,B`); writes `results/analysis-<stages>.json` |
| `calibrate.py`, `BASELINE-FACTS.md` | Task 2: the nine calibration sessions (a-i) with the runner's isolation in `<root>/calib/` (`--only a,b`, `--confirm N`), and what the documentation says next to what they showed |
| `results/` | one JSON line per run (`<stage>.jsonl`); `results/raw/<run id>/` the stream, prompt, diff, grade and pack log of each run (kept locally, not committed) |

Every command that starts real sessions needs `--confirm <number of sessions>`, and the owner's yes first.

```bash
python -m evals.gate.validate md2conf                 # graders (no model calls)
python -m evals.gate.verify_freeze                    # the task set is as frozen
python -m evals.gate.run plan --stage A               # the sessions a stage would start
python -m evals.gate.run run --stage dry --arms S,D --tasks H01,R01 --repeats 1 --fake --unfrozen --confirm 4
```

Open before any real run (Task 2, `BASELINE-FACTS.md`): whether automatic memory writes notes in `claude -p`
sessions, and how a run with its own `CLAUDE_CONFIG_DIR` is signed in to the subscription (the runner passes
`CLAUDE_CODE_OAUTH_TOKEN` through and never `ANTHROPIC_API_KEY`).
