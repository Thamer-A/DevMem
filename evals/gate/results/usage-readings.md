# Owner's usage readings

Read by the owner with `/usage` in Claude Code (PREREGISTRATION.md section 4). The test's own sessions may use about
15 percentage points of one week's subscription usage.

| When | Weekly | Current session | Note |
|---|---|---|---|
| 2026-11-27, before the first test session | 14% | 2% | baseline; the test's sessions may take the weekly figure to about 29% |
| 2026-11-27, the owner's yes for Stage A | - | - | the owner set the stop at 35% weekly; the runner is started with `--stop-weekly 0.35` |
| 2026-11-27, after 11 calibration and 24 replay sessions | 14% | five-hour 5% | from the sessions' own `rate_limit` events (seven-day 0.14, five-hour 0.05), not read by the owner |
| 2026-11-27, after Stage A (64 runs and 1 re-run) | 15% | five-hour 4% | from the last run's `rate_limit` event (seven-day 0.15), not read by the owner |
| 2026-11-27, after Stage A, the owner's yes for Stage B | 15% | - | read by the owner; Stage B runs with `--stop-weekly 0.35` |
