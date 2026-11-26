# Baselines, 2026-11-13

Measured on git commit `68be62726432b5c0b6fc3c492fa82473fee43298` (branch `post-hackathon`) with an
uncommitted working tree. At measurement time `devmem/scan.py`, `devmem/store.py`, `devmem/pack.py` and
`devmem/capture.py` were identical to that commit; the tree differed in `devmem/app.py`,
`devmem/ui_server.py`, `devmem/ui/app.js`, `pyproject.toml`, `.gitignore` and a new `devmem/redact.py`.
So these files are the baseline of the hackathon retrieval code (pre-rewrite).

- `retrieval-2026-11-13-68be627.json` / `.md`: `python -m evals.retrieval.run --json ... --md ...` (60 cases, budget 1500, mode full).
  Two consecutive runs produced identical `cases` and `summary`.
- `perf-2026-11-13-68be627.json`: `python -m evals.perf demo/memory-example/blade-swarm-race demo/taskboard/devmemory bench/projects/P1/game --stdlib`
  (Windows 11, Python 3.13; the stdlib copy had 779 files / 346,686 lines, 735 indexed). Wall-clock; machine was otherwise busy with other agents.

## Every file in this folder (added 2026-11-13, after the rebuild)

File recall changed definition between two groups of files. **File-recall numbers in group A and group B are
not comparable.** Group A counted a list item that starts with a backticked path anywhere in the pack, which
included the "Conventions" section; group B (`config.file_recall_metric` = 2 in the JSON) counts only
`info["files"]` and such list items before the Conventions section. Session recall, verbatim recall, empty-ok
and tokens are defined the same in both groups. Noise changes slightly (it uses the same file set). On the
current code, re-measuring with the new definition left every recall figure unchanged and moved noise from 40.4%
to 39.2% (see the two group B files for 55543b4), so the change is small in practice, but it is a change.

Group A (file-recall metric 1, no `file_recall_metric` key in `config`):

- `retrieval-2026-11-13-68be627.json` / `.md`: described above; the hackathon retrieval code.
- `retrieval-2026-11-13-58ab826-before-rewrite.json` / `.md`: taken at commit `58ab826` (the design-contract
  commit, before the rebuild) with a dirty tree. No note was written when it was made. Its session recall
  (73%) and verbatim recall (75%) equal the 68be627 file; file recall differs (69% against 54%). The reason is
  not recorded and was not investigated.
- `retrieval-2026-11-13-5c7979d-retrieval-v2.json` / `.md`: the rebuilt, index-based retrieval (turn-level,
  evidence gate) with a dirty tree on top of `5c7979d`, keywords only. 71% / 67% / 72% sessions / files /
  verbatim, noise 40%, 8 of 8 contentless prompts left empty. This was the reference before the work below.
- `retrieval-2026-11-13-5c7979d-retrieval-v2-embed-minilm.json` / `.md`: the same tree with optional embeddings
  on (fastembed, `paraphrase-multilingual-MiniLM-L12-v2`). 87% / 71% / 88%, noise 44%. The non-English cases
  rise from 0% to 60% sessions; cost and caveats are in the README.

Group B (file-recall metric 2):

- `retrieval-2026-11-13-55543b4-filemetric2.json` / `.md`: commit `55543b4` exactly as committed (taken from
  `git archive`, with only the new `evals/retrieval/run.py` copied in), keyword retrieval, old tokenizer.
  Same recall figures as the `5c7979d-retrieval-v2` file; noise 39.2%. Use this as the like-for-like reference
  for the next file.
- `retrieval-2026-11-13-55543b4-tokenizer3-filemetric2.json` / `.md`: the working tree with tokenizer version 3
  (function words in the stop list, a stemmer that unifies word forms) and the pack's query-side word-form
  workarounds removed. Sessions 71%, files 65%, verbatim 72%, noise 39%, empty 8 of 8. File recall is one case
  lower (bs-02 no longer pulls `src/shared/Config.lua`); the reason is in the commit that made the change. The
  meta `git_head` is whatever HEAD was when the run was made; the tree was dirty.

Performance probes (wall-clock, Windows 11, Python 3.13, machine shared with other agents):

- `perf-2026-11-13-68be627.json`: described above (hackathon code).
- `perf-2026-11-13-5c7979d-retrieval-v2.json`: `python -m evals.perf` on the three fixture projects
  (`fixtures/blade-swarm`, `taskboard`, `coinrush`) and a temp copy of the Python standard library, rebuilt code.
  Not directly comparable with the 68be627 file: the projects are the fixtures, not the `demo/` and `bench/`
  copies, and the rebuilt code also builds the SQLite index during a scan.
