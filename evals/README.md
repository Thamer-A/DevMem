# DevMem evals

Two fast, free, offline instruments for changing retrieval without running hundreds of agent sessions.
No LLM, no network, no API key. Run both from the repo root.

```bash
python -m evals.retrieval.run [--json out.json] [--md out.md] [--budget 1500] [--mode full]
                              [--project NAME] [--kind KIND] [--case ID]      # filters are repeatable
python -m evals.perf <project dir> [<project dir> ...] [--stdlib] [--json out.json]
python -m pytest tests/test_evals_*                                              # label sanity + harness tests
```

## 1. Retrieval eval (`evals/retrieval/`)

**What it does.** For each labelled case it copies the fixture project and its session records to a temp
directory (nothing under `demo/` or `bench/` is written), runs `devmem.scan.scan`, then calls
`devmem.pack.build_pack(project, store, task, budget=..., mode="full")` and inspects the pack text and the
returned `info`. Run time is a few seconds.

**What it measures** (all of it is "is the right thing in the pack", never "did an agent succeed"):

| metric | definition |
|---|---|
| session recall | share of `sessions_required` present in the pack. A session is present if `info["sessions"]` lists it or the text cites `session <id8>` (the verbatim-requirements block does that). Macro mean over cases that have required sessions. |
| file recall | share of `files_required` present: listed in `info["files"]` or a list item (before the pack's "Conventions" section) that leads with the path in backticks. Items inside Conventions and paths quoted in a convention line do not count (`config.file_recall_metric` = 2 in the JSON; baselines made before that change used metric 1, which counted the Conventions items, so their file-recall numbers are not comparable). `file_mention_recall` in the JSON is the looser "path appears anywhere in the text". |
| verbatim recall | share of `verbatim` strings found in the pack text (case- and whitespace-insensitive substring). This is the number that matters most: the hackathon result was that carrying the user's earlier words is what helps. |
| noise | pooled share of retrieved sessions + files that are in neither `*_required` nor `*_optional`. High noise costs tokens and attention but is not an error in the way a miss is. |
| empty ok | for `expect_empty` cases (trivial prompts): fraction whose pack contains no session and no file. The fixed header and conventions block are not counted; they show up in `tokens`. |
| tokens | `info["tokens_est"]`, the code's own chars/4 estimate. Not a real tokenizer count. |
| latency | wall time of `build_pack` alone (fixture scan time is reported separately). Varies between runs. |

**Case kinds:** `followup_early_req` (depends on a requirement stated sessions ago), `terse_bug` (a short
report that omits the spec), `untouched_file` (no session touched the file), `trivial` ("yes go ahead",
"thanks"), `paraphrase` (few literal words in common), `non_english` (Arabic, French, Chinese asking for
something an English session covered), `direct` (plainly worded task), `stale_memory` (the remembered edit
location has since moved).

**Diffing two runs.** `--json` writes sorted keys with `cases`, `summary`, `config` first-class and two
sections that legitimately change between runs on the same code: `timing` and `meta` (date, git commit,
Python version). To compare quality, diff everything else:

```bash
python - <<'EOF'
import json, sys
a, b = (json.load(open(p, encoding="utf-8")) for p in sys.argv[1:3])
for k in ("summary", "cases"):
    print(k, "identical" if a[k] == b[k] else "DIFFERENT")
EOF
```
(or `diff` the `--md` outputs). Results are deterministic for fixed code and fixtures; two baseline runs
were identical in `cases` and `summary`.

### Adding a case

Cases live in `evals/retrieval/cases/<project>.json`; the file name is the project key from
`evals/retrieval/projects.json`. To add a new project, add its entry there (project dir, sessions dir,
entries to exclude) and a cases file with the same stem. Fixtures must be dev material only.

The sessions under `fixtures/` were recorded by the hackathon prototype's capture on small demo projects. That
capture kept more than DevMem does today (usage and rate-limit events, and two short thinking texts in the
blade-swarm sessions). The user name in their recorded paths was replaced by `dev`.

```json
{
  "id": "bs-25", "kind": "followup_early_req",
  "task": "the prompt exactly as the user would type it",
  "sessions_required": ["3e2bb0e2"],     // first 8 chars of session_id; must appear
  "sessions_optional": ["e695866c"],     // useful but not required; never counted as noise
  "files_required": ["src/shared/Config.lua"],
  "files_optional": [],
  "verbatim": ["Common = gray, Rare = blue, Epic = purple"],   // exact text from a required session's prompt
  "expect_empty": false,                  // true for trivial prompts
  "why": "one line a human can audit: which session states what, and why this file"
}
```

Rules for labelling:

1. Read the session records (`prompt`, `files_edited`, the edits) and decide what a *correct* pack must
   hold. Write the justification first. Do not look at what the current retrieval returns and then adjust
   the label to match it.
2. `required` = the agent would likely get the task wrong, or re-explore needlessly, without it.
   `optional` = plausibly useful. When unsure, use optional: it avoids punishing either choice.
3. `verbatim` strings must be copied from the prompt of a required session. The test suite enforces this,
   and that every session id and file path exists, so a typo cannot silently change a score.
4. Never add cases from `bench/tasks/P1/H*`, `bench/tasks/P2/` or `bench/projects/P2/`; the runner and tests
   refuse those paths. They are the held-out set.
5. Do not delete or soften a case because the current retrieval fails it. A failing case is a finding.

Run `python -m pytest tests/test_evals_cases.py` after editing cases.

## 2. Performance probe (`evals/perf.py`)

`python -m evals.perf <dir> ...` scans each directory into a temp store (never the target), then reports
cold scan time (empty store), warm scan time (best of `--repeats` scans with nothing changed), pack time
for four generic sample prompts (first call and best repeat, override with `--prompt`), and the size of
`index/map.json` and `PROJECT.md` (with a chars/4 token estimate). `--stdlib` adds a temp copy of the Python
standard library (without `site-packages` or `__pycache__`) as a larger real tree. It refuses to scan the
repo root or `bench/projects/P2`. Wall-clock numbers depend on the machine, disk cache and antivirus; compare
only runs from the same machine, and run twice before believing a small difference.

## What this does and does not tell you

- It measures whether the pack contains the right memories, files and requirement text, and what it costs
  in estimated tokens. It does **not** measure whether an agent then succeeds, and a better score here is
  not evidence of a better outcome. The hackathon benchmark (`bench/`) is still the only end-to-end signal,
  and it is expensive.
- The cases come from **three small projects** (Blade Swarm, 16 real sessions; the task board, 8 real sessions;
  CoinRush P1, 5 scripted sessions) of 16 to 27 files. Behaviour on a large repository is measured only by the
  perf probe, which says nothing about retrieval quality.
- **All labels were written by one agent** in one sitting, from the session records, and have not been
  reviewed by a second person. Some `required` or `optional` choices are judgement calls. The per-case
  `why` line is there so you can disagree with them.
- The Arabic, French and Chinese prompts were written by that same agent; they have not been checked by a
  native speaker. Most `non_english` cases share almost no tokens with the English session, which is the point.
- The sessions in two of the projects were generated for demos and benchmarks, so their prompts are cleaner
  and more explicit than most real ones. Blade Swarm's repeated "I playtested it. Problems:" prompts are the
  closest thing to real conversational noise.
- CoinRush's dev tasks (A1 to D2) were used during the original tuning of the retrieval code. Scores on those
  cases are optimistic for that reason; the other cases were written fresh but by someone who had read the
  retrieval code.
- File labels depend on what the scanner indexes. Files the scanner does not index (for example `.css` and `.html`
  in the generic project type) can never be found by file retrieval; the cases are kept anyway, because that
  is a real gap.
- `noise` treats every session cited in the "what the user asked for earlier" block as a retrieved session.
  Whether such citations are noise or useful context is arguable; the metric is a cost indicator, not a
  correctness one.
- The stale flag (`**STALE**` / `PARTLY-STALE`) is not scored.
- Sizes are tiny: a "few dozen" cases means one flipped case moves a per-kind percentage by 10 to 25 points.
  Look at the per-case table, not only the kind averages.

Baselines are in `evals/baselines/`, named by date and short commit. The commit is where the measurement was
taken, but the working tree was being edited by other workstreams at the time; each file's `meta.git_dirty`
says whether `plugin/devmem/` (`devmem/` before the package moved) had uncommitted changes, and the notes below say which files differed.
