# devmem

Local-first project memory for AI coding sessions. Hooks record each Claude Code session into a project's
`.devmemory/` folder; on each new prompt a small context pack of relevant earlier requests and files is
added. The name in prose is "DevMem" (the command and the import package are `devmem`); internal identifiers stay `devmemory` (the store folder, the MCP
server name, the HTTP header). Do not rename them.

`README.md` is the short introduction for users; `docs/` holds the full description of behaviour. `docs/DESIGN-v2.md` is the contract the rebuild followed (its
paths refer to an older layout).

## Layout

- `plugin/devmem/` the package, about 30 modules (inside the Claude Code plugin folder `plugin/`, so
  pip and the plugin ship the same code). Entry points: `cli.py`, `hooks.py` (capture and delivery),
  `pack.py` (retrieval), `index.py` and `scan.py` (SQLite index and project map), `sessions.py` (records,
  forget), `redact.py`, `generic.py` (begin/end, wrap, Codex), `mcp_server.py`, `ui_server.py` and `ui/`,
  `gitindex.py` (git's index file read directly when the git program cannot say which store files it tracks).
- `plugin/` is also the Claude Code plugin: `.claude-plugin/plugin.json` (its `version` must equal
  `devmem.__version__`), `hooks/hooks.json`, `bin/devmem` + `bin/devmem.cmd` (launchers that find Python),
  `skills/` (setup, status, forget, view, and `recall`, the one Claude may invoke). No MCP server in the plugin.
  `devmem/plugin.py` holds the hook entry point, the bootstraps the launchers carry verbatim, the per-project
  switch (a marker in `${CLAUDE_PLUGIN_DATA}/projects/`, never inside the project) and setup.
  `devmem.hooks.session_route` decides once per session whether the init hooks or the plugin record it.
  **Consent lives outside the project** (`devmem/consent.py`): the plugin's marker in `${CLAUDE_PLUGIN_DATA}`, and
  the `devmem init` marker in devmem's per-user folder (`DEVMEM_USER_DATA` overrides it; `tests/conftest.py` points
  it at a temp folder). Nothing in a project (a `.devmemory/`, a committed settings file) switches recording, packs
  or lookups on; anything a project ships in `.devmemory/` needs `devmem adopt` or `--adopt-store`
  (`devmem.store.shipped_store`), and so does a store devmem 0.3.0 made (it wrote no note in the per-user folder).
  `.claude-plugin/marketplace.json` at the root lists it. Only `plugin/` is copied when the plugin is installed:
  nothing in it may refer to files outside it.
- `tests/` pytest. `evals/` offline retrieval evaluation and timing probe. `fixtures/` recorded projects.

## Commands

```bash
python -m pytest tests -q -p no:cacheprovider     # slow: 20-65 minutes per CI job; run the files you touched first
python -m ruff check plugin/devmem tests evals
python -m evals.retrieval.run                     # 60 labelled cases, a few seconds, no model calls
python -m evals.perf --stdlib                     # scan and pack timings on a 740-file tree
claude plugin validate plugin --strict && claude plugin validate . --strict   # after touching plugin files
PYTHONPATH=plugin python -m devmem <command>      # run the CLI from the working tree
```

Stress tests carry the `stress` marker and are skipped by every default run and CI (`addopts` in `pyproject.toml`):
`tests/test_stress.py` (50 parallel hook events on one session, 50 sessions in parallel, connect/disconnect round
trips over 200 generated files, every payload field with every wrong type, raw stdin shapes; about 1 minute) and the
full 64-512 KB sweep in `tests/test_extract_growth.py` (about 3.5 minutes). Run them before a release, or after
touching the hooks, connect or an extractor:

```bash
python -m pytest tests -m stress -q -p no:cacheprovider
```

`/prepush` tests the commit exactly as committed (in a temporary worktree). Run it before pushing.
`/retrieval-eval` compares retrieval scores with the saved baseline. Run it after touching `pack.py`,
`text.py`, `index.py` or `scan.py`.

## Rules

- **The core stays standard-library only** (`dependencies = []`). Heavier things are optional extras
  behind an import guard with a working fallback.
- **Recorded fixtures are read-only**: `fixtures/`, `evals/retrieval/cases/`, `tests/test_hooks_fixtures/`.
  A hook blocks edits there. Never change an evaluation case to improve a score; report a disputed label.
- **Never run the tool against `fixtures/` in place.** It writes an index into the store. Copy the project
  to a temp directory first, or pass a temp `--store`.
- **Never `pip install` in the repo root** for testing; build wheels from a copy in a temp directory.
- **Tests** build everything in `tmp_path`, and must not need the network, an API key or the `claude`
  binary. They must not write to real user files (the per-user recents file, a real `.claude/`).
- **Hooks must never break or slow a session.** Catch everything, log, exit 0. Exit code 2 from a hook
  blocks the user's prompt. Per-event wall time, measured 2026-11-14 from outside the process (medians of 7, one
  Windows 11 machine, Python 3.13; Linux in Docker, Python 3.10), for a recording event (PostToolUse) and for
  UserPromptSubmit with a pack: `devmem init` exec form 106 / 314 ms; plugin under Git Bash 172 / 416 ms; plugin
  under Windows PowerShell 5.1 373 / 624 ms; Linux sh (two runs) 28 ms (init) and 27-29 ms (plugin) / 66-71 and 75-85 ms. A plugin hook
  in a project without `.devmemory/` costs 40 ms under Git Bash, 268 ms under PowerShell, 1-2 ms under Linux sh; with
  a `.devmemory/` that was not switched on, 130 / 345 / 17 ms. A prompt whose refresh finds a changed file starts the
  refresh child (`devmem.refresh`): +0.25-0.3 s on Windows (a loaded machine) and +0.1-0.13 s on Linux for a 200-file
  project, init exec form, 2026-11-14; prompts where nothing changed measured the same as before (interleaved runs
  against the previous commit). Do not let these grow; re-measure by timing the
  hook commands as Claude Code runs them. The plugin's hook command is a sh/PowerShell polyglot
  (`devmem.plugin.hook_command`); `tests/test_plugin_hooks.py` runs it through both shells. In a project not
  switched on with /devmem:setup (no marker in the plugin's data folder) the plugin hooks must do nothing at all.
- **Everything recorded is redacted before it is written to the store** (records, raw lines, events, hook state, the
  hook error log, the MCP call log): `Redactor.record` at the write boundary, and a tool's input or response is redacted
  before it is serialised (`Redactor.tree`, within a budget; strings next to each other in a list or a dictionary are
  redacted as one text, one per line, so a key block given one line per string is seen whole: `_join_items`,
  `_split_items`; stream capture does the same with `Redactor.joined`, and `Redactor.record` joins such a run too
  unless the run's joined text is one the same redactor produced as a whole, or every item was read back from the
  store). Redaction is pattern-based, so a secret no rule recognises is
  stored as written. Not redacted: the project map and index (`PROJECT.md`, `index/`), built from project files; the
  index holds tokens of their text (a pack's quoted lines are redacted when it is built). A new redaction rule needs a
  seed in `tests/test_redact_growth.py`, adversarial inputs in `tests/test_redact_speed.py` and benign look-alikes;
  `tests/test_redact_shapes_matrix.py` checks every corpus secret in 23 tool shapes and every secret value in six YAML flow-map and
  name/value wrappers. Secret-pattern files
  are never read. Every path that creates the store goes through `devmem.store.ensure_store` so its
  `.gitignore` exists.
- **Label who wrote text** (user, assistant-visible, reported by a client, observed diff). Hidden reasoning
  is never read or stored. A field a tool did not expose stays empty.
- **Do not claim what was not run.** State what is unverified. Current gaps: a real interactive terminal
  session, macOS, benefit on real repositories.
- Windows is the primary platform; Linux runs in CI. Always pass `encoding="utf-8"`. Redaction patterns
  and their replacement callbacks must stay linear: a callback reads only its match, a constant window around it, or a
  memo built once per text (`tests/test_redact_speed.py`, `tests/test_redact_growth.py`, the line-length sweep in
  `tests/test_redact_line_sweep.py`; its full sweep runs with `-m stress`).

## Gotchas

- Do not write Python through a shell heredoc: `\b` and `\n` inside string literals get interpreted, and a
  literal backspace once made a test pass while checking nothing. Use the Write or Edit tool.
- The `devmem init` hooks record only with the user's switch (`devmem.consent.init_enabled`). Handlers written by a
  devmem from before the switch existed record nothing until `devmem init` runs again (status/doctor say so). In a
  test, `hooks_helpers.make_project(..., switched_on=False)` makes a store the user never switched on; anything that
  runs `devmem init` or `/devmem:setup` from a script outside the tests should set `DEVMEM_USER_DATA` to a temp
  folder (absolute, outside the project: anything else is refused), or it writes into the real per-user folder.
- Nothing in a project may weaken privacy either: `"redact": false` in a store's config.json counts only with the
  user's `--allow-unredacted` (devmem.consent.allows_unredacted).
- `python -m pytest` imports `devmem` from `plugin/` (the `pythonpath` option in `pyproject.toml`).
  Subprocesses do not get that: set `PYTHONPATH` to `plugin/`, or they pick up whatever devmem is installed
  (an editable install made before the move points at a folder that no longer holds the package; run
  `pip install -e .` again).
- This repository uses devmem on itself. `.devmemory/` and `.claude/settings.local.json` are local and
  git-ignored. If a context pack is empty and the task refers to earlier work, `devmem sessions` and
  `devmem show <id>` (or the `devmemory` MCP tools `list_sessions` and `read_session`) can look it up. Do not call
  `begin_session` or `end_session` here: the hooks already record the session, and using both produces two records.

## Review

Changes to `redact.py`, `hooks.py`, `plugin.py`, `consent.py`, `store.py`, `gitindex.py`, `connect.py`, `sessions.py`, `generic.py`, `mcp_server.py`,
`ui_server.py` or the plugin's launchers, hooks.json and skills should go through the `privacy-reviewer` agent before
they are pushed.
