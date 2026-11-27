# Baseline facts (PLAN.md section 4, Task 2)

Documentation read on 2026-11-27; the runner pins Claude Code 2.1.332 (`runner.json`). Each fact names the page and
section it comes from. "What the runs showed" comes from `python -m evals.gate.calibrate`: nine real sessions on
2026-11-27, `claude-haiku-5-5`, Claude Code 2.1.332, Windows 11, with the runner's isolation and sign-in by
`CLAUDE_CODE_OAUTH_TOKEN` (records in `<root>/calib/results/summary.json`, outside the repository). One session per
check: these show what can happen, not how often.

| Session | Prompt | Configuration | Memory written | Rule used |
|---|---|---|---|---|
| a | states a rule ("From now on, every log file name ... ends in .trail.log") and asks for a helper that follows it | new | none | yes (in that session) |
| b | "Add a helper that opens the audit log for today." | a's | (none to load) | no |
| c | as b | new | none | no |
| d | as a, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | new | none, no memory path in init | yes (in that session) |
| e | as a, DevMem installed as in arm D | new | none | yes (in that session) |
| f | as b | e's (DevMem store) | none | **yes**: the pack carried e's session |
| g | a small change, plus a rule for later work ("When we add an audit log later, its file name must end in .trail.log.") | new | **none** | (nothing to apply yet) |
| h | as b | g's | (none to load) | no |
| i | "Remember for future sessions in this project: every log file name ends in .trail.log." | new | `MEMORY.md` + one topic file | - |

## 1. Automatic memory in headless (`claude -p`) sessions

Documentation:
- "Auto memory is on by default in local sessions." (https://code.claude.com/docs/en/memory, "Enable or disable auto
  memory")
- Without `--bare`, "`claude -p` discovers and loads them as an interactive session would"; the list includes auto
  memory. (https://code.claude.com/docs/en/headless, "Start faster with bare mode")
- Memory is kept in `~/.claude/projects/<project>/memory/`; "The `<project>` path is derived from the git
  repository"; outside a git repository the project root is used. (memory, "Storage location")
- The first 200 lines or 25KB of `MEMORY.md` load at session start; topic files are read on demand. (memory, "How it
  works")
- "Claude doesn't save something every session." It skips what it can derive from the codebase. (memory, "Auto
  memory")
- Writing auto memory files needs no permission approval. (https://code.claude.com/docs/en/permissions, table at the top)
- The `/memory` toggle cannot turn auto memory back on in "A session that another Claude Code session started".
  (memory, "Enable or disable auto memory")
- None of the pages cited here says in so many words that auto memory writes notes in `-p` sessions.

What the runs showed: automatic memory is on in `-p` sessions and can write: every session with `=0` reported
`memory_paths.auto` in its `system/init` event, pointing inside the run's own `CLAUDE_CONFIG_DIR`, and session i, asked
outright to remember, wrote `MEMORY.md` and a topic file there with no permission prompt. But in the two sessions
where the user stated a rule in passing (a, implemented at once; g, a rule for later work phrased the way the
history phrases its requirements) Claude saved nothing and made no tool call on the memory folder, so the later
sessions b and h did not apply the rule. With DevMem (e, f) the rule came back through the pack. So the stock arm's
memory after the replay may be empty or nearly so; how often Claude saves a passing requirement was not measured.

## 2. Switching automatic memory off, or forcing it on, for one run

Documentation:
- `CLAUDE_CODE_DISABLE_AUTO_MEMORY`: "Set to `1` to disable auto memory. Set to `0` to force auto memory on even when
  `--bare` mode or `autoMemoryEnabled: false` would otherwise disable it. When disabled, Claude does not create or
  load auto memory files". (https://code.claude.com/docs/en/env-vars)
- A project can turn it off with `"autoMemoryEnabled": false` in its settings. (memory, "Enable or disable auto memory")

What the runs showed: with `=1` (session d) the `system/init` event had no `memory_paths` and no memory folder was
written; every other session ran with `=0`, as every arm does, and reported the memory path.

## 3. One clean configuration folder per run (`CLAUDE_CONFIG_DIR`), and what else lives in HOME on Windows

Documentation:
- `CLAUDE_CONFIG_DIR` overrides `~/.claude`: "All settings, session history, and plugins are stored under this path."
  It is ignored in project and local settings. (env-vars)
- With it set, "every `~/.claude` path on this page lives under that directory instead. The same applies to
  `.claude.json`". (https://code.claude.com/docs/en/claude-directory)
- With it set, `.credentials.json` is kept under that directory. (https://code.claude.com/docs/en/authentication,
  "Credential management")
- Transcripts are stored under `projects/<project>/`, where `<project>` is the working directory path with
  non-alphanumeric characters replaced by `-`; `CLAUDE_CODE_PROJECT_DIR_NAME`, set beside `CLAUDE_CONFIG_DIR`, names
  that folder for transcripts and auto memory (v2.1.234 or later). (https://code.claude.com/docs/en/sessions)
- On Windows `~/.claude` is `%USERPROFILE%\.claude`. (claude-directory; https://code.claude.com/docs/en/settings)
- Not under HOME, so not moved: the managed `CLAUDE.md` at `C:\Program Files\ClaudeCode\CLAUDE.md` (memory); the
  Anthropic profile directory, `%APPDATA%\Anthropic` on Windows (authentication, "Anthropic profiles and federation
  credentials"). (Not from the docs: `run.session_env` does not redirect `APPDATA`.)
- Temp files: `CLAUDE_CODE_TMPDIR` defaults to `os.tmpdir()` on Windows. (env-vars)

What the runs showed: the transcript, `.claude.json`, backups, policy and remote-settings files were written under
the run's `CLAUDE_CONFIG_DIR` (`projects/<path with - for separators>/`); nothing was written to the redirected HOME;
one session's own test file went to the redirected TEMP. Session c, with a fresh configuration at the same project
path, had nothing from a. Sign-in worked with HOME and USERPROFILE redirected (section 5).

## 4. Hooks in headless runs

Documentation:
- "Without `--bare`, a `-p` session runs the hooks in a project's `.claude/settings.json` ... even in a folder you've
  never trusted." (headless, "Start faster with bare mode")
- "`-p` or SDK session: Claude Code never shows the dialog and treats the folder as trusted". (https://code.claude.com/docs/en/hooks,
  "Workspace trust")
- "In non-interactive mode with the `-p` flag, Claude Code kills any async hook still running at teardown". (hooks)
  (Not from the docs: `devmem init`, as `run.install_devmem` calls it, installs no async hook; `plugin/devmem/init.py`.)
- A command hook with `args` runs in exec form, with no shell. (hooks, "Exec form and shell form")

What the runs showed: in all nine sessions the project's UserPromptSubmit hook fired once and PostToolUse once per
tool call. With DevMem installed (e, f) the store recorded the sessions and `packs.jsonl` got one line per prompt; f's
pack carried `session:` e's session. Note: the agent reads `.claude/settings.json` when it explores the project
(session a commented on the test hook), so a hook's file is visible to it.

## 5. Sign-in for headless runs

Documentation:
- `claude setup-token` makes a one-year OAuth token, printed and not saved; it is set as `CLAUDE_CODE_OAUTH_TOKEN`. It
  needs a Pro, Max, Ultra, Team or Enterprise plan and "can only make model requests". (authentication, "Generate a
  long-lived token")
- Precedence: cloud provider, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `apiKeyHelper`, `CLAUDE_CODE_OAUTH_TOKEN`,
  Anthropic profiles, then the `/login` subscription. "In non-interactive mode (`-p`), the key is always used when
  present." (authentication, "Authentication precedence")
- `CLAUDE_CODE_OAUTH_TOKEN` is ignored in project and local settings. (env-vars)
- A failure such as missing authentication is printed as the result on stdout. (headless, "Basic usage")

What the runs showed: every session signed in with HOME, USERPROFILE and CLAUDE_CONFIG_DIR redirected, no
`ANTHROPIC_*` variable, and only `CLAUDE_CODE_OAUTH_TOKEN` given (init reported `"apiKeySource": "none"`, so no API
key was used; no auth error). The token
was found in no saved file. The stream's `rate_limit` event reports the subscription's five-hour and seven-day use
(0.14 seven-day in all nine sessions, matching the owner's reading of 14%), so the runner can follow usage itself.

## 6. `--bare`

Documentation:
- `--bare` skips auto-discovery of hooks, skills, plugins, MCP servers, auto memory and CLAUDE.md. (headless;
  https://code.claude.com/docs/en/cli-reference)
- In bare mode Claude Code "never reads OAuth credentials"; it does not read `CLAUDE_CODE_OAUTH_TOKEN`. (headless;
  authentication)
- "`--bare` is the recommended mode for scripted and SDK calls, and will become the default for `-p` in a future
  release." (headless)
- `CLAUDE_CODE_SIMPLE=1` is equivalent to `--bare`. (env-vars)

What the runs showed: not run (the runner never passes `--bare`).

## 7. Other points for the runner

- "Native installations automatically update in the background." `DISABLE_AUTOUPDATER=1` turns that off.
  (https://code.claude.com/docs/en/setup, "Auto-updates"; env-vars)
- The `system/init` event reports "the model, tools, MCP servers, and loaded plugins"; the page lists no memory
  field. (headless, "Read session metadata")
- `--exclude-dynamic-system-prompt-sections` moves "the auto memory location" out of the system prompt.
  (cli-reference)

What the runs showed: the `system/init` event of 2.1.332 does carry `memory_paths` (`{"auto": <folder>}`) when auto
memory is on, and leaves it out when it is off. `run.session_env` now sets `DISABLE_AUTOUPDATER=1`. Cost: the nine
sessions took 25 to 45 s each (i: 8 s), about 120,000 cache-read and 5,000 to 10,000 cache-write tokens each, and did
not move the seven-day figure at two digits.
