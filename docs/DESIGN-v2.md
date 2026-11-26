> **Note.** Internal design notes from November 2026, written before the rebuild. The [README](../README.md) and
> the other pages in `docs/` describe what is shipped; where they differ from this file, they are right.

# DevMemory v2 design (post-hackathon)

> **Status (2026-11-13).** This is the working contract from the rebuild, written before the work and kept
> as it was agreed. Its paths refer to the old layout: `bench/` and `demo/` do not exist in this repository, and
> the recorded projects now live read-only under `fixtures/<name>/project` and `fixtures/<name>/memory`.
> Where the code and this file differ, the code is right. One change to the contract: in `map.json`,
> `scanned_at` now means "content last changed" (it keeps its old value when a rescan finds the same content),
> so that a scan with no changes writes byte-identical files.

This is the shared contract for the post-hackathon rebuild. Several agents work on it in parallel; each
owns a set of files (see "Ownership"). Read the whole file before changing anything.

## Where we are

The hackathon build (`devmem/`, about 2,000 lines of stdlib Python plus a vanilla-JS app) proved one thing
on its own benchmark: carrying the user's earlier requests into a new session is what helps. The project
map alone and the STALE flags showed no measured benefit. The tool is shaped around the benchmark:
one-shot headless sessions, 27-file fixture projects, English prompts.

Problems reproduced on 2026-11-13 (each needs a regression test once fixed):

1. `devmem pack` / `devmem hook` crash with `UnicodeEncodeError` on a default Windows console or pipe
   (cp1252) when the pack contains a character such as U+2212.
2. Freshness reports `fresh` for a file a session created with `Write`, even after the file is rewritten or
   deleted. Cause: capture keeps `old`/`new` text only for `Edit`.
3. A non-editable install ships no `devmem/ui/` files (no package data in `pyproject.toml`).
4. The hook rescans and re-indexes everything on every prompt: on 735 files / 319k lines, scan 2 to 3 s and
   pack about 3 s, so 5 to 6 s per prompt. (A first measurement showed a 16 s first scan; the evaluation
   agent re-measured 1.9 to 3.0 s, so the 16 s was most likely a cold file cache on a freshly copied tree.) `PROJECT.md` reaches about 18k tokens there.
5. The tokenizer is ASCII-only (`[A-Za-z][A-Za-z0-9]+`), so an Arabic prompt retrieves nothing.
6. Every prompt, even "yes go ahead", receives the conventions block (about 200 tokens), and in an
   interactive session that stacks up on every turn.
7. Scan output is not byte-stable between runs (ordering of tied hub APIs changes; `scanned_at` always
   changes), which dirties tracked files.

Design gaps:

- Interactive Claude Code sessions are not captured at all. Only `devmem run` (headless), Codex, `wrap`
  and `begin`/`end` record anything, and a session holds exactly one prompt.
- Scan patterns are fitted to the fixtures (`Remotes.define`, `Assets.get`, `data.coins`, "float() on
  money"). `.gitignore` is not respected. Symbols come from regexes.
- Retrieval is keyword-only; "decisions" are sentences containing words like must/never/only. In an
  active project most sessions get flagged PARTLY-STALE because a shared file such as `Config.lua` keeps
  changing, so the flag is noise.
- Raw session records keep tool output verbatim (it can contain secrets). Nothing gitignores
  `.devmemory`. There is no redact or delete command.
- The app server has no Host/Origin check and no token; browser paths and example projects are
  hard-coded for this Windows checkout.
- No tests for `devmem/`, no CI.

## Principles (unchanged, from AGENTS.md)

- Local-first, inspectable, deletable. Never upload or publish project content.
- **The core stays stdlib-only** (`dependencies = []`). Anything heavier is an optional extra behind an
  import guard, with a working fallback.
- Use documented interfaces only (hooks, CLI event streams, MCP). Verify against current official docs;
  where something is best-effort, say so in the code and the docs.
- Never claim hidden reasoning. Label who wrote each piece of text (user, assistant-visible, tool output,
  observed diff). Fields a tool did not expose stay empty, never guessed.
- Separate observed facts from inferences.
- Do not invent capabilities or numbers. Report what you ran and what you did not.

## Rules for every agent

- Work on branch `post-hackathon`. **Do not commit, push, or publish anything.**
- Edit only the files you own. If you need a change in someone else's file, do not make it: list it in
  your final report under "Needs wiring".
- **Do not modify** `bench/`, `demo/`, `results/`, `dist/`, `docs/deck/`, or the planning notes in the
  repo root. They are the hackathon archive.
- **Do not open** `bench/tasks/P1/H*`, anything under `bench/tasks/P2/`, or `bench/projects/P2/`. Those
  are held-out evaluation material and must stay unseen by anyone who tunes the tool.
- Keep these names and signatures working, because `bench/` and `demo/` import them:
  `devmem.scan.scan(project, store=None) -> dict`,
  `devmem.pack.build_pack(project, store, task, budget=1500, mode="full", ...) -> (text, info)`,
  `devmem.capture.record_session(...)`, `devmem.capture.run_and_capture(...)`,
  `devmem.claude_cli.run_claude(...)`, `devmem.claude_cli.claude_executable()`.
- Tests: `pytest`, in `tests/test_<area>_*.py`. Build fixtures in `tmp_path`; never write into the repo
  tree or into a real project. No shared `conftest.py` yet; keep helpers in `tests/<area>_helpers.py`.
  Tests must not need the network, an API key, or the `claude` binary.
- Do not install hooks or MCP servers into this repository's own `.claude/` or into the user's global
  Claude Code or Codex config. Try things in a temp project.
- Do not run `pip install` in the repo root (it leaves build directories). Run code with
  `python -m ...` from the repo root; build wheels from a copy in a temp directory.
- Windows is the primary platform, Linux and macOS must work. Always pass `encoding="utf-8"` for file
  and pipe I/O. Use the Write tool, not shell heredocs, for Python that contains backslashes.
- Python 3.10+ (developed on 3.13).

## Store layout v2 (`<project>/.devmemory/`)

```
.gitignore                 written by every command that creates the store (`devmem.store.ensure_store`); ignores everything in the store
config.json                optional settings (redaction on/off, pack budget, ...)
ignore                     extra ignore globs (existing feature)
redact                     extra redaction regexes, one per line
PROJECT.md                 human-readable map, size-capped
index/map.json             project map (same top-level keys as v1)
index/index.db             SQLite index (files, sessions, turns)
sessions/<stamp>_<sid8>.meta.json   one per finished session (schema below)
sessions/<stamp>_<sid8>.jsonl       raw record for that session
live/<session_id>.jsonl    append-only event log of a session in progress (hooks)
live/<session_id>.state.json   per-session delivery state (what has already been injected), turn start/open/closed
live/<session_id>.gitseen  hashes of git's changed paths at the last check (hooks)
live/<session_id>.turn / .open / .prompt / .edits   turn times, open turn start, a prompt that missed the lock,
                           hashes of paths its file tools edited: read by the project's other sessions (hooks)
```

## Session record, schema 2

A session has **turns** (one per user prompt). Aggregate v1 fields stay present and are derived from the
turns, so v1 readers keep working.

```json
{
  "schema": 2,
  "session_id": "...", "provider": "claude-code",
  "capture_method": "hooks | stream-json | snapshot-diff | snapshot-diff + tool-json",
  "started_at": "2026-11-13T20:07:00Z", "ended_at": "... or null",
  "model": null, "raw_record": "<file>.jsonl",
  "git": {"head_start": "...", "head_end": "...", "dirty_start": true},
  "turns": [{
    "index": 0, "started_at": "...", "prompt": "the user's words, verbatim",
    "prompt_source": "user | reported-by-client (MCP end_session: the AI client wrote it); absent in older records = user",
    "assistant_text": [{"text": "...", "source": "assistant-visible"}],
    "result_text": "... or null", "result_source": "...",
    "tool_calls": [{"tool": "Edit", "file": "src/a.py", "old": "...", "new": "...", "truncated": false,
                    "command": "...", "pattern": "...", "ok": true}],
    "files_read": [], "files_edited": [], "files_deleted": [], "errors": []
  }],
  "prompt": "= turns[0].prompt", "result_text": "= last turn's", "result_source": "...",
  "tool_calls": "= all turns concatenated", "files_read": [], "files_edited": [], "files_deleted": [],
  "file_hashes_at_capture": {}, "usage": {}, "assistant_text": [], "errors": []
}
```

- `Write` and `MultiEdit` must record their text too (`new` for Write; one entry per MultiEdit edit).
  Cap each text at 4,000 characters and set `truncated`.
- `devmem.sessions.load_sessions(store) -> list[dict]` is the one loader. It upgrades schema-1 files in
  memory to schema 2 (a single turn) and never rewrites old files.
- Captured text is redacted before session records and live logs are written (pattern based; `"redact": false`
  switches it off; the project map and index are not passed through it).

## Index API (`devmem/index.py`, owned by the index workstream)

```python
class Index:
    def __init__(self, store: Path): ...
    def sync(self, project: Path) -> dict            # incremental; returns counts {added, changed, removed, unchanged, seconds}
    def files(self) -> dict[str, dict]               # rel -> info (same shape as map.json "files")
    def upsert_doc(self, kind: str, key: str, fields: dict[str, str], meta: dict | None = None) -> None
    def delete_doc(self, kind: str, key: str) -> None
    def search(self, kind: str, query: str, limit: int = 20) -> list[dict]   # {key, score, matched: [terms], meta}
    def idf(self, kind: str, term: str) -> float
    def close(self) -> None
```

- `kind` is `"file"`, `"session"` or `"turn"`. Files are indexed by `sync`; sessions and turns are
  upserted by the retrieval layer.
- An unchanged project must sync in well under a second on a 1,000-file tree (stat-based skip, then hash).
- `devmem/text.py` owns `tokenize(text) -> list[str]`: Unicode-aware (Arabic, CJK, accented Latin),
  splits camelCase/snake_case, light English stemming, stop words. Everything that tokenizes uses it.
- `scan()` keeps its signature and map shape, uses the index internally, and writes byte-stable output
  when nothing changed.

## Hook entry point (`devmem/hooks.py`, owned by the capture workstream)

`python -m devmem.hooks <EventName>` reads the Claude Code hook JSON from stdin. It must never break the
user's session: catch everything, log to `<store>/hook_errors.log`, exit 0. It must be fast.
`UserPromptSubmit` both delivers context (through `devmem.pack.build_pack`) and records the turn.
`devmem init` (function `devmem.init.init_project(project, ...)`) merges the hooks into the project's
`.claude/settings.json` without disturbing existing entries, writes the store `.gitignore`, and scans.

## Ownership

| Workstream | Owns |
|---|---|
| Index and scan | `devmem/store.py`, `devmem/scan.py`, `devmem/index.py`, `devmem/text.py`, `devmem/extract*.py`, `tests/test_index_*`, `tests/test_scan_*`, `tests/test_text_*` |
| Capture and privacy | `devmem/capture.py`, `devmem/claude_cli.py`, `devmem/generic.py`, `devmem/sessions.py`, `devmem/hooks.py`, `devmem/init.py`, `devmem/redact.py`, `tests/test_capture_*`, `tests/test_hooks_*`, `tests/test_redact_*`, `tests/test_sessions_*` |
| App, packaging, CI | `devmem/ui_server.py`, `devmem/app.py`, `devmem/ui/*`, `pyproject.toml`, `.gitignore`, `.gitattributes`, `.github/`, `DevMemory.bat`, `tests/test_ui_*`, `tests/test_packaging_*` |
| Retrieval evaluation | `evals/` (new), `tests/test_evals_*` |
| Retrieval and CLI (second wave) | `devmem/pack.py`, `devmem/cli.py`, `devmem/mcp_server.py`, `devmem/embed.py`, `devmem/__init__.py`, `tests/test_pack_*`, `tests/test_cli_*`, `tests/test_mcp_*` |

## Final report format (every agent)

1. What you built, file by file, in a few lines.
2. What you verified, with the exact commands and their real output (test counts, timings).
3. What you did not finish or could not verify, stated plainly.
4. "Needs wiring": changes required in files you do not own.
