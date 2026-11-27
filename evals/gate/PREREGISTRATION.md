# Gate experiment: pre-registration

Committed 2026-11-26, before any task, history or run exists. Everything below is fixed from this commit on.
Changes are allowed only as dated entries in "Amendments" at the end, committed **before the first run of Stage A**;
after that nothing in this file changes except to record that a stop rule fired. The git history of this file is the
record: a reader checks that this commit precedes every file under `evals/gate/results/`.

## 1. The question

On real repositories, does adding DevMem (requests-only pack) to stock Claude Code, with Claude Code's own automatic
memory on, raise the pass rate of tasks that depend on a requirement the user stated only in an earlier session,
and does it leave ordinary tasks alone?

## 2. Materials

- **Two repositories**: permissively licensed, little-known (so a model is unlikely to have memorised them), 200 to
  2,000 tracked files, one Python and one TypeScript, with an offline test suite that runs in under a minute. Their
  URLs and pinned commits are added as an amendment before the task set is frozen.
- **A scripted history per repository**: five or six headless sessions of ordinary feature work in which the user
  states eight to ten requirements with exact values (a limit, a format, a name, an order). The history is a list of
  prompts fixed before the replay; it is the same for every arm.
- **Tasks per repository**: 8 history-dependent tasks (each depends on one requirement stated only in the history),
  4 ordinary tasks (need nothing from the history), and 3 tuning tasks kept apart. In all: **24 scored tasks (16
  history-dependent, 8 ordinary) and 6 tuning tasks**. Each history-dependent task names its one relevant
  requirement as "oracle" text, copied verbatim from the history.
- **Who writes them**: a session that has not read `plugin/devmem/pack.py` or any other retrieval code.
- **Graders**: one automatic grader per task (tests or a script). Validation rule (from the hackathon benchmark):
  every grader accepts the task's reference solution and rejects a plausible guess that ignores the requirement. A
  task whose grader fails validation is fixed or replaced before freezing, never after.
- **Freeze**: task files, graders, histories and oracle texts are listed with their SHA-256 in
  `evals/gate/tasks/MANIFEST.sha256`; `verify_freeze` checks it before every run and the runner refuses to start on
  a mismatch.

## 3. Arms

All arms: the same pinned Claude Code version, the same model (`claude-haiku-5-5`, the small model), the same
headless flags, the same per-run limits; a fresh copy of the repository at its pinned commit plus the history's
resulting edits; a fresh configuration folder per run copied from the arm's replayed state; one process at a time.
How automatic memory is kept on, and kept apart per arm and per run, is recorded in `evals/gate/BASELINE-FACTS.md`
(Task 2); if that changes anything here, it is an amendment.

| Arm | What it is | Replayed state | Tasks |
|---|---|---|---|
| **S** stock | Claude Code as installed, automatic memory on | the history replayed once under S | 16 history + 8 ordinary |
| **O** oracle | S plus the task's oracle text placed before the task prompt in a fixed wrapper: `Earlier in this project the user said: "<oracle>"` then a blank line, then the task | S's | 16 history |
| **D** DevMem | S plus DevMem installed, `pack_mode: "requests"` | the history replayed once with DevMem installed (automatic memory also on) | 16 history + 8 ordinary |
| **DF** full pack (Stage C only) | as D with `pack_mode: "full"` | D's | 16 history + 8 ordinary |

Repeats: **two** runs of every task in every arm. Within a stage the runs are ordered by a seeded shuffle (seed
`20261126`) that interleaves arms, so a slow hour or a model change affects all arms alike.

Arms not run, and said so in the report: automatic memory off (S0) and irrelevant pasted text (X).

## 4. Stages and run counts

| Stage | Runs | Count | Condition |
|---|---|---|---|
| Replay | history sessions under S and under D, both repositories | about 24 sessions | owner's yes |
| A, room | S and O on the 16 history tasks, 2 repeats | 64 | owner's yes; stops the study if threshold 1 fails |
| B, delivery and harm | D on all 24 tasks, S on the 8 ordinary tasks, 2 repeats | 64 | owner's yes |
| C, full pack | DF on all 24 tasks, 2 repeats | 48 | only if the owner's usage limit allows |

The owner's limit is about 15% of one week's subscription usage for the test's own sessions (calibration, replay,
stages). The owner reads the usage meter before the first test session and after Stage A; if the projection from
the runner's token counts passes the limit, the study stops and the owner is asked. The design is never shrunk after
results are seen.

## 5. Measures (per run, one JSON line)

- **Primary**: pass or fail by the task's grader, run on the working copy after the session ends.
- Tokens in four categories summed over the run: uncached input, cache write, cache read, output.
- **Price-weighted total**, in units of the model's uncached input price:
  `uncached_input + 1.25 * cache_write_5m + 2 * cache_write_1h + 0.1 * cache_read + 5 * output`.
  The weights are the list-price ratios of `claude-haiku-5-5` read on 2026-11-26 ($0.10 input, $0.50 output per
  million tokens for prompts up to 100K tokens; cache writes 1.25x (5-minute) or 2x (1-hour), cache reads 0.1x of
  input). When the run's usage does not split cache writes by lifetime, all cache writes are weighted 1.25 and the
  report says so.
- Turns, wall time, model as reported, Claude Code version, exit status.
- For D and DF: what each prompt's pack contained (from `.devmemory/packs.jsonl`: item keys, estimated tokens, or
  the reason it was empty).

## 6. Analysis

- **Pass rate** per arm and task type, with a 95% interval from a bootstrap that resamples tasks (10,000 resamples,
  seed `20261126`), because the two repeats of one task are not independent.
- **Per-task paired difference** against S: `d_i = mean(arm runs of task i) - mean(S runs of task i)`.
- **Paired test**: exact two-sided sign-flip permutation test on the 16 `d_i` (statistic: their sum), alpha 0.05.
- "Gain" means the difference of pooled pass rates over the 16 history-dependent tasks, in percentage points.
- Token comparisons use the median over runs of the price-weighted total, per arm and task type.
- One command regenerates every number in the report from the raw logs.

## 7. Thresholds

1. **Room**: O beats S by at least **20 points** on the history-dependent tasks.
2. **Delivery**: on the history-dependent tasks, D beats S by at least **15 points**, D's gain is at least **two
   thirds** of O's gain, and the paired test of D against S gives **p < 0.05**.
3. **No harm** (coarse at this size): on the 8 ordinary tasks D passes **at most one run fewer** than S (of 16), and
   D's median price-weighted tokens are **at most 10% above** S's.
4. **Informational, Stage C only**: if DF is not better than D on both pass rate and median price-weighted tokens,
   the map, file pointers and conventions are deleted in milestone M1. Without Stage C that deletion is the owner's
   call.

## 8. What this size can detect

`python -m evals.gate.power` (seed fixed) simulates the paired test at 16 tasks and two repeats. With 80% power it
detects a D-over-S gain of about **35 to 40 points**; a true gain of 15 points reaches p < 0.05 in only about 15% of
such studies, and a true gain of 25 points in about 40%. So a real but moderate effect will often end
"Inconclusive", not "Pass". The table, for the record (share of simulated studies with p < 0.05):

| baseline pass rate | gain 15 | 20 | 25 | 30 | 35 | 40 |
|---|---|---|---|---|---|---|
| 0.2, equal tasks | 0.16 | 0.26 | 0.42 | 0.58 | 0.70 | 0.83 |
| 0.2, tasks differ | 0.19 | 0.34 | 0.49 | 0.67 | 0.81 | 0.90 |
| 0.3, equal tasks | 0.14 | 0.24 | 0.38 | 0.53 | 0.68 | 0.81 |
| 0.3, tasks differ | 0.17 | 0.30 | 0.43 | 0.62 | 0.75 | 0.88 |
| 0.5, equal tasks | 0.15 | 0.25 | 0.39 | 0.56 | 0.74 | 0.89 |
| 0.5, tasks differ | 0.15 | 0.27 | 0.42 | 0.58 | 0.73 | 0.83 |

## 9. Outcomes

| Result | Outcome |
|---|---|
| Threshold 1 fails | **Fail (no room)**. Stop after Stage A. |
| 1 holds; D's gain misses 15 points or two thirds of O's gain | the one retry (below); if it still misses: **Fail (no delivery)** |
| 1 holds; D's gain meets both sizes; p >= 0.05 | **Inconclusive**. The only extension: a third repeat of S, O and D on the 16 history tasks (48 runs), with the owner's yes for the usage; then thresholds 1 and 2 are evaluated once more on three repeats, and that result is final |
| 1 and 2 hold; 3 fails | **Fail (harm)**: not released, unless the cause is one small defect; the owner decides and the decision file says why |
| 1, 2 and 3 hold | **Pass** |

**The one retry**: allowed only when threshold 1 holds and threshold 2 fails on size. One round of retrieval fixes,
developed on the 6 tuning tasks only (the scored tasks, their oracle texts and their packs are not looked at), then
one re-run of arm D on all 24 scored tasks with two repeats (48 runs, owner's yes). After that the result stands.

## 10. Harness failures and stop rules

- A **harness failure** is a run that did not test the arm: the runner or Claude Code crashed before the session
  ended, an API or rate-limit error ended it, the laptop was suspended during it, or the working copy could not be
  prepared or graded. It is logged and re-run once. A session that ends normally, hits the turn limit or the per-run
  time limit, or leaves code the grader rejects is a **fail** for the arm, never a harness failure.
- If more than 2% of a stage's runs are harness failures, the stage stops; the harness is fixed and the stage
  is run again from the start.
- **Stop and ask the owner** when: Task 2 shows that automatic memory does not operate in headless sessions (then
  the baseline needs re-planning before any stage); the usage projection passes about 15% of the week; threshold 1
  fails; the gate is not decided by 2026-12-24 (four weeks from the plan); Anthropic documents built-in recall of
  earlier sessions in the command-line tool (the gate is finished, and the owner may then require a larger margin
  for "Pass", decided before Stage B results are read).
- No arm, task or threshold is changed after the first run of Stage A, and none is dropped after its results are
  seen.

## 11. Amendments

**A1, 2026-11-26 (before any task, history or run exists).**
- The repositories, chosen by the owner: `hunyadi/md2conf` (Python, MIT) and `danfry1/bonsai-js` (TypeScript,
  MIT). Their pinned commits are written in `evals/gate/tasks/<repo>/repo.json` and in the freeze manifest.
- The owner confirmed the thresholds as written (section 7) and that the usage limit in section 4 covers the test's
  own Claude Code sessions only.
- The code a task starts from is fixed, the same for every arm and run: the repository at its pinned commit plus
  the **reference patch** of each history session, applied in order (the format is in
  `evals/gate/TASK-FORMAT.md`). The replay (section 4) runs the history's prompts as real sessions only to build each
  arm's memory: Claude Code's automatic-memory folder for S and O, that plus the DevMem store for D and DF. The code
  those replay sessions write is discarded. Reason: otherwise every arm would start from different code and the
  graders, frozen before any run, could not be validated against it. Consequence, stated in the report: a memory
  may describe code that differs in detail from the code the task starts from, alike in every arm.
- A history-dependent task depends on a requirement that the history states and that the starting code does not
  reveal (a rule or value for later work, not one the reference patches already implement); the grader's
  "plausible guess" is a solution that follows the code and the task prompt but not that requirement.

**A2, 2026-11-26 (after the md2conf set was written and reviewed, before any freeze or run).**
- A grader whose own time limit cuts a subprocess short reports `grader_timeout`; such a run is a harness failure
  (section 10: "the working copy could not be ... graded"), re-run once like any other.
- Stated limitations, recorded now so the report cannot choose them afterwards:
  - Many requirements are phrased as plans for later work ("When we add X, ...", "From now on, ..."), and the later
    task asks for X in similar words. The tasks were written that way by a session that had not read the retrieval
    code, because users talk that way; they were not reworded afterwards in either direction. Word overlap makes
    retrieval by shared words easier, and such phrasing is also what Claude Code's automatic memory is built to
    save, which may raise S's pass rate and shrink the room that threshold 1 measures.
  - Graders also run the repository's existing suite, including tests the agent itself added, so a correct change
    with a broken new test fails, in every arm alike.
  - Some requirement values remain guessable at a low rate (a date format, a file-name marker); the guess patches
    show the more likely wrong choice.

**A3, 2026-11-26 (after the bonsai-js set was written and reviewed, before any freeze or run).**
- bonsai-js ships only `bun.lock`; its dependencies are installed with `npm ci` from a `package-lock.json` made once
  from its `package.json` (`npm install --package-lock-only --before=2026-11-15`), kept in its task folder and
  frozen with the tasks. Its one test that runs the `bun` program is left out by name.
- Its suite has a few wall-clock assertions (500 ms to 3 s). Graders run it with `--retry=2`, so that a slow
  moment on the test machine does not fail a correct change; the hidden tests are deterministic, so a retry
  cannot turn a wrong change into a pass.
- Every Node working copy links one shared `node_modules`. When a run changes it (an agent's `npm install`, a
  cache written through the link), the row records `deps_changed` and the install is rebuilt before the next run;
  the run's own result stands, since the agent did it in its own session.
- Stated limitations: H06 reads "the two largest non-zero units" literally (1 h 0 m 5 s is "1h 5s"), so an agent
  that knows the requirement but reads it as "the largest unit and the next one" fails, in every arm alike.
  H04's character (•) and R9/R10 (Sunday weeks, KiB) are guessable at a low rate.

**A4, 2026-11-27 (after Task 2's calibration sessions, before the replay and any run).**
- `evals/gate/BASELINE-FACTS.md` records nine headless calibration sessions. Automatic memory is on in `-p`
  sessions, is kept in each run's own configuration folder, and saves when the user asks for it outright; but in
  the two sessions where the user stated a rule in passing, Claude saved nothing. The stop rule in section 10
  ("automatic memory does not operate in headless sessions") was put to the owner, who chose to go on: the replay
  runs as planned, and before Stage A the contents of the stock arm's replayed memory folder (which files, and which
  requirement values they mention) are reported to the owner, who then decides on Stage A. Looking at the replayed
  state shows no task result. Arm S stays "Claude Code as installed"; a stock arm told to save memory is not added.
- Consequence, stated in the report: on this model and in headless sessions the stock arm's memory may hold few of
  the history's requirements, so S may be close to a stock session without memory, and the room that threshold 1
  measures may be larger than with interactive use.
