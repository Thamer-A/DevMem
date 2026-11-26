# Using devmem with any AI tool

Claude Code gets the fullest record through hooks (`devmem init`). Every other tool connects through devmem's
MCP server, a wrapper command, a begin/end snapshot, or, for a chat window with no tools at all, a pack on the
clipboard.

## 1. One command per tool: `devmem connect`

```bash
devmem connect                  # list the tools and what each one writes
devmem connect cursor --print   # show exactly what would be written, write nothing
devmem connect cursor           # do it
devmem disconnect cursor        # remove exactly what connect added
```

For each tool, `connect` does two things, in project files only:

1. **Registers the MCP server** (`devmemory`, running `devmem mcp --project <project>`) in the tool's project MCP
   config file. Other servers and unrelated keys stay as they are, running it again changes nothing, and before
   the first change to an existing file a copy is saved in the store, `.devmemory/connect-backups/` (git-ignored,
   so it is never committed). A file that is not plain JSON (for example JSONC with comments, which VS Code
   allows) is left untouched: `connect` stops with an error and nothing is written; `--print` shows the entry to
   add by hand. The same happens when a file it would change is not a plain file (a folder, a FIFO) or is over
   16 MB. Codex's TOML is not rewritten: a marked block is appended, and on Python 3.11+ the result is parsed
   before it is written (refused if it would not parse or not hold exactly devmem's entry).
2. **Adds a short instruction block** to the tool's project instruction file, between
   `<!-- devmemory:begin ... -->` and `<!-- devmemory:end -->` lines. It tells the agent, in a few lines, to call
   `get_context_pack` with the user's request at the start of a task, to call `begin_session` before editing and
   `end_session` (pending id, the request, a short summary) when done, that STALE parts may be outdated, and never
   to put secrets in the summary, and to skip begin/end when devmem already records the session (`devmem init`
   hooks, `devmem codex`, `devmem wrap`). The block is loaded into every prompt of that tool, so it is kept short.
   Tools that read `AGENTS.md` share one block; it is removed when the last of them is disconnected.

If the instruction file is a link to another file in the project (`AGENTS.md` pointing at `CLAUDE.md`, say),
the block is written into that file and the link stays a link; a link leading outside the project is refused.
Everything is planned before anything is written; if a write then fails (a read-only or locked file), what that
run already changed is put back, so the project is as it was, and the error is reported in one line.

`disconnect` takes out what `connect` put in: the entry, the block (with the blank line before it, and a line
ending `connect` added to a file that had none), a file `connect` created when nothing else is in it (with the
folder `connect` created for it, if that folder is empty), and devmem's own backup copies of files that no longer
hold anything of devmem's. Your other content stays. A devmemory block is replaced (by `connect`) or removed (by `disconnect`) only when it is provably the one devmem wrote: `connect` records the SHA-256 of the exact block it wrote in `.devmemory/connect.json` (nothing is added to your file), and the block must still hash to that. Also accepted: a file that is still byte for byte what an earlier `connect` wrote (versions before block hashes recorded only that), and a block that is byte for byte what this version writes for the same entry. Marker lines inside a code fence or a TOML string, a block you edited, or a block in a project that was never connected are left alone: the command stops with an error and changes nothing. `--force` replaces or removes such a block anyway, after copying the whole file to `.devmemory/connect-backups/<name>.forced-<time>-<pid>`. After an upgrade, a block written by an older version whose wording differs and whose file you have edited since needs `--force`. A file is restored from its backup only when it is byte for
byte what `connect` wrote (a hash is kept in `.devmemory/connect.json`); if you changed it since, in content or
formatting, only devmem's entry or block is removed and the rest of the content stays as you have it. A text block
is cut out of the text; a JSON file is serialised again in its current style (indent or a single line, line
endings, final newline, BOM), so a number or escape written unusually (`1.0E2`, `\u00e9`) comes back in its plain
form (`100.0`, `é`). If `.devmemory/connect.json` is deleted, `disconnect` still
removes the entry, and the block when it is exactly what this version writes, but leaves the (then empty) file
in place. Backups that the first version of
`connect` left next to the files (`<file>.devmem-backup`) are removed by `disconnect` as well.

A user-global config file is never edited (`~/.codex/config.toml`, Claude Desktop's config, `~/.cursor/mcp.json`,
...). For a tool whose MCP config is only global, `connect` prints the entry to paste. `connect` refuses to run on
your home folder, on a folder that contains it, and on a drive root: the home's `.cursor/`, `.gemini/` and
`.codex/` are those tools' global configuration.

**The command** in the entry is `devmem` when `devmem` is on PATH, otherwise the absolute path of the current
Python with `-m devmem` (plus `PYTHONPATH` when that Python would not import this devmem from another folder).
`--command devmem|python` forces one. `devmem doctor` lists the connected tools and reports a FAIL when an
entry's command no longer resolves.

**The project path** in the entry: `${workspaceFolder}` for Cursor and VS Code, which document that variable for
these files. An absolute path for the others: they document no workspace variable and do not say which folder a
server is started in, so `--project .` could point at whatever folder the tool was launched from. A Cursor or
VS Code file works in every clone and can be committed only when the whole entry is free of this machine's paths,
that is when the command is `devmem` (on PATH) and no `--store` is given. Whenever the command, an argument or
an environment value is an absolute path (the project, the Python interpreter, `PYTHONPATH`, `--store`),
`connect` says so: the file is machine-specific, do not commit it; teammates run `devmem connect` themselves.

| Tool (`devmem connect ...`) | MCP entry written to | Instruction block | Project path | Format source (checked 2026-11-14) | Tried in the tool? |
|---|---|---|---|---|---|
| `cursor` | `.cursor/mcp.json` (`mcpServers`, `type: "stdio"`) | `AGENTS.md` | `${workspaceFolder}` | [cursor.com/docs/context/mcp](https://cursor.com/docs/context/mcp), [rules](https://cursor.com/docs/context/rules) | No. Config format from the documentation; the written entry was started by hand as Cursor would (variable replaced) and driven over stdio |
| `vscode` (`copilot`) | `.vscode/mcp.json` (`servers`, `type: "stdio"`) | `AGENTS.md` | `${workspaceFolder}` | [VS Code MCP configuration reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration), [custom instructions](https://code.visualstudio.com/docs/copilot/customization/custom-instructions) | No. Config format from the documentation |
| `codex` | `.codex/config.toml` (`[mcp_servers.devmemory]`, appended as a marked text block, never rewritten) | `AGENTS.md` | absolute | [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md) | No. Config format from the documentation |
| `gemini` | `.gemini/settings.json` (`mcpServers`) | `GEMINI.md` | absolute | [Gemini CLI MCP servers](https://geminicli.com/docs/tools/mcp-server/), [configuration](https://geminicli.com/docs/reference/configuration/), [GEMINI.md](https://geminicli.com/docs/cli/gemini-md/) | No. Config format from the documentation |
| `devin` (`windsurf`) | `.devin/mcp_config.local.json` (`mcpServers`) | `AGENTS.md` | absolute | [Devin Desktop MCP](https://docs.devin.ai/desktop/cascade/mcp), [MCP config format](https://docs.devin.ai/cli/extensibility/mcp/configuration.md), [rules](https://docs.devin.ai/desktop/cascade/memories) | No. Config format from the documentation |
| `claude-desktop` | nothing; prints the entry for `claude_desktop_config.json` | nothing; prints the text | absolute | [Connect local servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers) | No |
| `generic` | nothing; prints the standard `mcpServers` entry | `AGENTS.md` | absolute | [agents.md](https://agents.md/) | The printed entry was started by hand from an unrelated folder and driven over stdio: `initialize`, `tools/list`, `get_context_pack`, `begin_session`, `end_session`; the session was recorded and the store's `.gitignore` written |

Notes per tool, from the same documentation:

- **Cursor** marks `type` as required in its table but leaves it out of its examples; devmem writes it.
  `${workspaceFolder}` is documented as "the folder that contains `.cursor/mcp.json`" (the project root).
- **VS Code** reads `.vscode/mcp.json` in its Local harness, which the page says is deprecated and will be removed
  in February 2027; the Copilot harness does not read that file directly but "can forward" eligible entries from
  it. The Copilot harness reads a workspace-root `.mcp.json` (top-level `mcpServers`) directly; the experimental
  setting `chat.mcp.workspaceRootConfig.enabled` only decides whether **MCP: Add Server** offers that file, and
  an existing `.mcp.json` is read either way. devmem does not write `.mcp.json` because Claude Code also reads it
  as its project MCP config. With the Copilot harness you can rely on the forwarding until February 2027, or add
  the entry yourself: `devmem connect generic --print` prints it in the `mcpServers` shape to put in `.mcp.json`
  (Claude Code will then see the server too). VS Code allows comments in `mcp.json`; such a file is left alone
  (see above).
- **Codex CLI** reads a project's `.codex/config.toml` only for trusted projects. For all projects,
  `connect codex` prints the `codex mcp add devmemory -- ...` command, which edits your global config, so devmem
  does not run it. It is printed once for POSIX shells (bash, zsh, sh: single quotes, `'` written as `'"'"'`) and
  once for PowerShell (single quotes, quote characters doubled); in neither form is anything in a path expanded or
  run. There is no line for cmd.exe, which expands `%NAME%` even inside quotes. `connect` refuses (and changes
  nothing) when the TOML already defines `mcp_servers.devmemory` outside devmem's block (in any spelling of the
  key) or sets `mcp_servers` inline, or when it is not valid TOML (Python 3.11+; on 3.10 a text check covers the
  key spellings). Headless `codex exec` cannot ask for approval; use `devmem codex --mcp` there (it pre-approves
  devmem's tools for that run only).
- **Gemini CLI** reads `GEMINI.md` by default; `context.fileName` in its settings can add `AGENTS.md`. Its
  configuration page says string values in `settings.json` may reference environment variables as `$NAME`,
  `${NAME}` or `${NAME:-default}`, and its MCP page adds `%NAME%` on Windows and that an undefined variable becomes
  empty; no escape is documented. So `connect gemini` refuses (and writes nothing) when the entry would contain
  such a sequence, for example a project folder named `a$b` or `50%off%`. This has not been tried in a real Gemini
  CLI. Gemini's documentation also says that for unpaid-tier and Google One users Gemini CLI was replaced by
  Antigravity CLI on June 18th, 2026. devmem has no Antigravity target; use `devmem connect generic` and add the
  printed entry to Antigravity's MCP configuration (not tried).
- **Windsurf**: its documentation now redirects to Devin Desktop's (docs.devin.ai). devmem writes the documented
  local override file `.devin/mcp_config.local.json`, which Devin describes as git-ignored; check that your
  `.gitignore` covers it.
- **Claude Desktop** has only a global config (`%APPDATA%\Claude\claude_desktop_config.json` on Windows,
  `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `~/.config/Claude/` on Linux), with
  absolute paths. Merge the printed entry in, restart Claude Desktop, and paste the printed instruction text where
  you keep instructions.
- **Claude Code**: use `devmem init` (hooks). If your `CLAUDE.md` imports `AGENTS.md`, the block asks Claude to
  skip begin/end when hooks already record the session, so you do not get two records (whether the model
  follows that has not been tested).
- **`devmem codex` and `devmem wrap`** record the run themselves. Besides the line in the block, they set
  `DEVMEM_NO_CAPTURE=1` for the process they start (for `devmem codex --mcp` also in the devmemory server's own
  `env`), and a devmem MCP server that sees it answers `begin_session` and `end_session` with "already being
  recorded" and creates nothing. Whether a wrapped agent passes its environment on to the MCP servers it starts
  depends on that agent.

## 2. The MCP server itself

```
devmem mcp --project <your project>
```

Tools: `get_context_pack(task)`, `list_sessions()`, `read_session(id)` (turn by turn, edits including `MultiEdit`), `begin_session()` (returns a pending id) and `end_session(prompt, pending_id, summary)`. Pass the pending id back so two sessions that overlap do not collide. The server speaks the `initialize` handshake of protocol versions 2025-11-25 down to 2024-11-05, and `server/discover` with per-request `_meta` for 2026-07-28; the 2026-07-28 path comes from the specification text and has not been tried with a real client. Calls are not logged by default; set `"mcp_call_log": true` in `.devmemory/config.json` to keep a log of the tool calls in `.devmemory/mcp_calls.jsonl` (the arguments are redacted before they are written; every `devmem forget` deletes the whole file, since its lines are not tied to a session). The MCP server creates the store with its `.gitignore` if it is missing. When a client sends a summary through `end_session`, it is stored labelled as the assistant's self-report, and the `prompt` is stored with `prompt_source: "reported-by-client"`: the AI client wrote that text, not the user, so packs and `read_session` say so instead of calling it the user's verbatim words. The diff is computed by devmem itself.

- **Claude Code:** `claude mcp add devmemory -- devmem mcp --project <dir>`. Tried in the hackathon prototype: Claude called `get_context_pack` and used the STALE flag in its answer.
- **Codex CLI:** `devmem codex "<task>" --mcp` passes `-c mcp_servers.devmemory...` overrides for a single run and leaves your Codex config unchanged. Tried in the hackathon prototype.

## 3. A chat window with no tools: the pack on the clipboard

```
devmem pack "<task>" --copy
```

Puts the pack on the clipboard; paste it above your request. Uses Windows `clip` (fed UTF-16 so non-English
text survives), macOS `pbcopy`, or on Linux `wl-copy`, `xclip` or `xsel`. With none of them, it says so and exits
with status 2. When the task gives no retrieval evidence ("thanks", "yes go ahead") the pack is empty on purpose:
nothing is copied, the clipboard is left as it was, and the reason is printed. On Linux it was tried with real
clipboards in Docker: `xclip` and `xsel` under Xvfb and `wl-copy` under a headless sway, each returning in well
under a second, with the text read back intact (`xclip -o`, `xsel -o`, `wl-paste`). These programs leave a
process behind to serve the selection, so devmem does not read their output through pipes. Windows `clip` and
macOS `pbcopy` are covered only by tests with a stand-in program; copying to a real Windows or macOS clipboard
has not been tried.
Nothing is recorded in this mode: the chat's edits happen outside devmem's view. Use `devmem begin` / `devmem end`
around the work if you want it remembered.

## 4. Wrap any command-line agent

```
devmem wrap --prompt "<task>" --provider aider -- <agent command with {prompt} or {pack_file}>
```

devmem builds the pack, puts it into the command (`{prompt}` = pack + task, `{pack_file}` = path to the pack; also in `$DEVMEM_PACK_FILE`), snapshots the project before and after, and records the session. When the project's store is not yours (one a repository shipped, or one git tracks pulled files in: `devmem status` says why), `wrap` still runs the agent, with a pack of the project's own files only (mapped in devmem's per-user folder; nothing in the store is read), prints one line with the reason, and records nothing. The pack's temporary folder is removed when the command ends, also when it fails or cannot start. `{prompt}` is refused (nothing runs) where the program would read it as code: anywhere in the command line of a `.bat` or `.cmd` file on Windows (also when the name ends in dots or spaces, as in `tool.bat.`, which Windows still runs through cmd.exe), `cmd`, `powershell`, `pwsh` and `wsl`, and in the code argument of a shell or interpreter (`sh -c`, `bash -lc`, `python -c`, `node -e`, `perl -e`, `ruby -e`, `php -r`). The pack quotes project files, so a shell would run what they contain. Use `{pack_file}` or `$DEVMEM_PACK_FILE` there; `python agent.py {prompt}` (an argument of the script) is fine. On Windows a `.bat`/`.cmd` target, `cmd`, `powershell` or `pwsh` is also not started when any argument (or its own path) contains a character cmd.exe or PowerShell would read as a command: `& | < > ^ % ! " ( )`, a line break or another control character (for PowerShell also `$`, the backtick, `;`, `{`, `}`, `'` and `@`).

## 5. IDE agents without MCP

```
devmem begin                  # prints: pending id: <id>
... work in the IDE ...
devmem end --id <id> --prompt "<what you asked>" --provider cursor [--summary "<what it did>"]
```

`--id` picks the begin to close when several are open; without it, the most recent begin is used. The diff is of the whole working tree, so two sessions editing the same tree at once still see each other's changes. Give the agent the pack with `devmem pack "<task>" --out context.md` or `--copy`.

## 6. Codex CLI adapter

```
devmem codex "<task>" [--model M] [--no-pack | --mcp]
```

Uses Codex's documented flags: `exec --json`, `-o` (final message) and `--sandbox workspace-write`, with the project as the working folder (it is never passed as an argument). On Windows an npm-installed `codex.cmd` goes through cmd.exe, so devmem runs the `codex.js` it would start with node directly (the shim folder's `node.exe`, else `node` from PATH, never from the project); when that file is not where npm puts it, the shim is run only if no argument contains a cmd.exe command character, which rules out `--mcp` (its `-c` values contain `"`). Its JSONL events, final message, token usage and errors are stored with the observed diff.

## What capture can and cannot see per tool

- **Claude Code (hooks):** prompts, visible replies, tool calls and edits, files read, edited and deleted, and model and usage where Claude Code exposes them. Never hidden reasoning.
- **Codex CLI adapter:** prompt, agent messages, command runs, file changes and usage from `--json`, plus the observed diff.
- **Any tool through MCP `begin_session` / `end_session`:** the request as the client reports it (labelled so), the client's summary (labelled as its self-report) and the observed diff. Whether the agent actually calls the tools depends on the agent following the instruction block; nothing forces it.
- **Any other tool:** prompt, the observed diff, and the tool's visible output. Files it only *read* are not observable; that field is left empty, never guessed.

## Empty packs and optional embeddings

Whatever the tool, `devmem pack "<task>"` and the MCP `get_context_pack` build the pack the same way. A prompt with
no retrieval evidence gets an empty pack; with `devmem pack` the reason is on stderr (`"empty_reason"`). Local
embeddings are off by default and are enabled the same way for every tool (`.devmemory/config.json` or
`DEVMEM_EMBED`, see [PACKS.md](PACKS.md)); only `devmem embed --download` downloads the model, so a tool that runs devmem
commands never starts a download. They cost a model load of about 2 seconds per process, which suits the long-running
MCP server better than a command that starts a new process per prompt.
