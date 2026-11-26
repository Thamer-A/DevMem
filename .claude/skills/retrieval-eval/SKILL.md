---
name: retrieval-eval
description: Run the offline retrieval evaluation and compare it with the saved baseline, per case kind. Use after any change to plugin/devmem/pack.py, text.py, index.py, scan.py or embed.py, when asked whether a change affected retrieval, or when asked to save a new retrieval baseline.
---

# retrieval-eval

`evals/` holds 60 labelled cases over three recorded projects. The runner builds a context pack for each
and scores it. It needs no model and no network and takes a few seconds. Read `evals/README.md` for what
each number means and `evals/baselines/NOTES.md` for what each saved baseline is.

## Steps

1. Run the evaluation, writing the report outside the repository:

   ```bash
   python -m evals.retrieval.run --json "$TMPDIR/retrieval-now.json" --md "$TMPDIR/retrieval-now.md"
   ```

   (Any temp directory works. On Windows use the system temp folder.)

2. Find the reference: the most recent baseline in `evals/baselines/` that `NOTES.md` marks as using the
   current file-recall metric. Read its `.md` table.

3. Compare, overall and for each case kind (direct, followup_early_req, terse_bug, paraphrase,
   non_english, stale_memory, untouched_file, trivial): session recall, file recall, verbatim recall,
   noise, contentless prompts left empty, mean tokens. Report a small before/after table and name every
   case whose result changed, with its id and what moved.

4. Say plainly whether the change helped, hurt or made no difference. A drop in follow-up or terse bug
   report session recall (both at 100%) is a regression that needs explaining before anything is pushed.

## Rules

- Never edit `evals/retrieval/cases/` or `fixtures/` to change a score. If a label looks wrong, report the
  case id and the reason and leave it.
- Do not special-case fixture names, case ids or phrases from the cases in the retrieval code.
- These 60 cases are also what retrieval was tuned on, so the scores are optimistic. Say so when quoting
  them.
- Save a new baseline only when asked, or when a retrieval change is being committed. Name it like the
  existing ones (`retrieval-<date>-<short commit>-<what changed>.json` and `.md`), keep the old files, and
  add a line to `evals/baselines/NOTES.md` saying what the file is and which code state it measured.
- For timing questions use `python -m evals.perf --stdlib` and compare with the saved perf baseline.
