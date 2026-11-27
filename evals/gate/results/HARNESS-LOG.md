# Harness failures and stage restarts

PREREGISTRATION.md section 10: a harness failure is logged and re-run once; more than 2% in a stage stops it, the
harness is fixed and the stage restarts from its first run. Each entry says what happened and what changed.

## Stage A (2026-11-27)

- `A_bonsai-js_H01_S_r1_a1`: the runner was stopped mid-session (Claude Code ended its background job because the
  machine was low on memory). Logged as a harness failure on the next call (`recover_interrupted`); its re-run
  (`a2`) passed. 1 of 64 (1.6%), under the limit; the stage went on.

## Stage B, attempt 1 (2026-11-27), restarted

- `B_md2conf_R04_D_r2_a1` (the stage's first run): `no result event (exit 1)` after 0.14 s. The task's prompt starts
  with `--skip-title-heading`, and the runner passed the prompt right after `-p`, so Claude Code read it as an
  unknown option (`error: unknown option '--skip-title-heading ...'`) and no session started. A re-run would have
  failed the same way.
- `B_md2conf_H01_D_r2_a1`: the runner was stopped by hand during this run, to fix the cause above before a second
  failure; logged as a harness failure (interrupted).
- 2 of 64 (3.1%), over the limit: the stage restarts from its first run. Its rows are kept in `B-attempt1.jsonl`
  (logs under `raw/attempt1-<run id>/`, git-ignored) and are not used in the analysis.
- Fix: the runner now passes the prompt last, after `--` (`run.run_session`); checked without a sign-in, so with no
  model call, that Claude Code 2.1.332 then takes a dash-led prompt as the prompt. Only md2conf R04 starts with a
  dash, and it is an ordinary task, run first in Stage B: Stage A, the replay and the calibration sessions are not
  affected.
