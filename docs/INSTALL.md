# Install and set up

How to install DevMem (Claude Code plugin, `pip`/`pipx`, or the Windows installer), how to switch a project on, the commands you will use, and how to connect tools other than Claude Code. The short version is in the [README](../README.md).

Section names in quotes ("Install", "Privacy", "Optional embeddings", ...) are the headings of the pages in this folder; [README.md](README.md) lists which page holds each.

## Install

There are two ways to use devmem with Claude Code. Both run the same code (the package lives in `plugin/devmem/`,
and the plugin is that folder) and both need Python 3.10 or newer on the machine. Pick one per project: if a
project has both, each session is still recorded once (see below).

devmem is not on PyPI (the names `devmem` and `devmemory` there belong to unrelated projects).

| | Claude Code plugin | Standalone (`pip`/`pipx` + `devmem init`) |
|---|---|---|
| install | in Claude Code: `/plugin marketplace add Thamer-A/DevMem`, then `/plugin install devmem@devmem`; from a shell: `claude plugin marketplace add Thamer-A/DevMem`, then `claude plugin install devmem@devmem` | `pipx install "git+https://github.com/Thamer-A/DevMem.git"` (or `pip install` with the same URL; from a clone, `pip install .`) |
| switch a project on | `/devmem:setup` in a Claude Code session in that project | `devmem init` in the project folder |
| what is written | `.devmemory/` in the project, one small file per switched-on project in the plugin's data folder (`~/.claude/plugins/data/...`) and a note in devmem's per-user folder (below); no settings file is touched | `.devmemory/`, hook entries in `.claude/settings.local.json`, and one small file per switched-on project in devmem's per-user folder (`%APPDATA%\DevMemory\projects`, `~/Library/Application Support/DevMemory/projects` or `~/.config/DevMemory/projects`) |
| Python | found at run time: `python3`, `python` or `py` (3.10+) on PATH; the plugin needs no `pip install` | the interpreter you installed with; the hook entries hold its absolute path |
| command line | the skills run the plugin's own launcher by its full path. Claude Code also puts the plugin's `bin/` on the PATH of Claude's Bash tool; its documentation does not say in which order relative to your own PATH, so a pip-installed `devmem` may win there. Elsewhere run `<plugin folder>/bin/devmem` (`devmem.cmd` on Windows) | `devmem` on your PATH |
| looking up earlier sessions | the `recall` skill, which Claude may use on its own: `devmem sessions`, `devmem show <id>`, `devmem pack "<task>"` | the same commands, or the MCP server (below) |
| updates | `/plugin marketplace update devmem` (the plugin's `version` pins the cached copy until it is bumped) | `pipx upgrade dev-memory` / reinstall; run `devmem init` again if the package folder moved |
| remove | `/plugin uninstall devmem@devmem` (and `/plugin marketplace remove devmem`). Uninstalling deletes the plugin's data folder (`~/.claude/plugins/data/devmem-devmem` for this marketplace), so every project's switch is forgotten; the stores stay in the projects, and after a reinstall `/devmem:setup` switches each on again without asking (the note in devmem's per-user folder remembers that you set it up). Per project, `/devmem:setup off` stops recording and keeps the store | `devmem uninstall` in each project (removes the hook entries), then `pipx uninstall dev-memory` |

**Nothing records until you switch a project on, and the switch lives outside the project.** The plugin is enabled
in every project, but its hooks record a project, and add packs to its prompts, only after `/devmem:setup` has run
there; it notes that in the plugin's data folder. `devmem init` likewise writes its switch to devmem's per-user
folder, and the hook entries it installs do nothing in a project without that switch. A repository can ship a
`.devmemory/` folder and a `.claude/settings.json` with hook entries of its own; neither is ever taken as your
consent, for recording, for packs or for the `recall` lookups. In a project without a `.devmemory/` folder each
plugin hook returns at once, before Python starts; in one that has the folder but was not switched on, Python
starts, finds no switch and does nothing (no file is written in the project, nothing is added to your prompts).

The rules for a `.devmemory/` folder that already exists, for which stores are used and written to, and for links
are at the end of this page, under [Which stores are used, in detail](#which-stores-are-used-in-detail).

The skills: `/devmem:setup` (`/devmem:setup off` switches recording off and keeps the store), `/devmem:status`
(what is recorded, `devmem doctor`), `/devmem:forget` (asks before it deletes) and `/devmem:view` (the local
viewer) are invoked by you only. `recall` is the one Claude may use by itself, when you refer to earlier work that
the pack added to your prompt does not cover; it runs `devmem sessions`, `devmem show -- <id>` and
`devmem pack -- "<task>"` with `--plugin-data`, pre-approved under Git Bash or sh. Everything after `--` is read as
text, and with `--plugin-data` these commands refuse `--store`, `--out` and `--copy`, so a pre-approved call reads
this project's memory only and writes no file. In a project that is not switched on (neither with `/devmem:setup` nor
with `devmem init` hooks that can run) they print one line saying so; run from a terminal without `--plugin-data`,
a project without a store is an error (exit status 2), as for any other command.

Notes on the plugin route:

- Hooks run through the shell Claude Code picks (`sh` on macOS and Linux, Git Bash on Windows, Windows PowerShell
  when Git Bash is not installed); one hook command is written to run under all of them. The first hook event
  in a project that has a `.devmemory/` folder looks for Python and remembers it in the plugin's data folder. A
  candidate counts only when it runs a Python 3.10+ probe: it must exit 0 and print a fixed marker followed by its
  own path, which must be an existing executable file. Microsoft Store aliases are skipped, and on Windows without Git
  Bash only `python.exe`, `py.exe` and `python3.exe` in PATH folders are tried (never a `.bat` shim or the current
  folder). The remembered line carries the same marker; one without it is ignored, and an interpreter that does not
  finish the hook's bootstrap is forgotten (one line in the temp folder's `devmem_hook_errors` log).
- The plugin has no MCP server. To use devmem's MCP tools inside Claude Code as well, install devmem with pip or
  pipx and add the standalone server yourself: `claude mcp add devmemory -- devmem mcp --project . --read-only
  --require-store` (`--read-only` leaves out `begin_session`/`end_session`, which would record the session a second
  time next to the hooks, so the server records nothing; it is not free of writes: `get_context_pack` still refreshes
  the derived project map and search index in the store, `PROJECT.md` and `index/`, like every pack, and writes the
  call log if you switched that on; in a store that is not yours, see "Install", it writes nothing at all.
  `--require-store` never creates a store).
- If a project also has `devmem init` hooks, both sets of hooks fire (Claude Code does not merge a plugin's handler
  with a settings handler). Which set records a session is decided at the session's first event (the init hooks,
  when they are installed then) and kept for the whole session, even if `devmem init` or `devmem uninstall` runs
  while it is open; a resumed session decides again. `devmem doctor` reports such a mixed setup and how to settle
  it; `/devmem:setup` in a project with init hooks changes nothing and says so.
- The plugin route does not use the optional embeddings (its hooks start Python without site-packages); packs there
  use keyword retrieval. `devmem doctor` says so when a project's config turns embeddings on.
- On Windows without Git Bash the launcher is a `.cmd` file, and cmd re-reads the line it is given. The hook command
  refuses to run it from a plugin folder whose path holds `%` or `^` (it writes one line to
  `%TEMP%\devmem_hook_errors.log` and records nothing); `&` and spaces work. The skills pass no project path, and
  their PowerShell form uses `--%` so that quoted arguments reach cmd quoted; `show` and `pack`, whose argument
  passes through cmd, are not pre-approved there and ask first.

Working on devmem itself, without installing: `PYTHONPATH=plugin python -m devmem ...` from the repository root,
or `claude --plugin-dir plugin` to load the plugin for one session.

### Windows installer

For Windows without Python: `DevMem-Setup-<version>.exe` (64-bit Windows 10 or 11) installs devmem for the current
user only, with no administrator rights. It is the standalone route of the table above with its own Python: the
official python.org embeddable Python 3.13 and the devmem package, nothing else. The pip/pipx route and the Claude Code
plugin remain the other ways to install; the installed `devmem init` writes the same hook entries as a pip install,
pointing at the bundled `python.exe`.

- **Where**: `%LOCALAPPDATA%\Programs\DevMem\` (`python\` the runtime, `app\devmem\` the package, `bin\devmem.cmd`
  the command). A Start Menu entry "DevMem" opens the viewer (`devmem app`, no console window); a desktop shortcut is
  an option, off by default. "Add the devmem command to PATH" (on by default) appends `...\DevMem\bin` to your user
  PATH, so `devmem` works in a terminal opened after the install.
- **Not code-signed yet**: Windows SmartScreen will warn that the publisher is unknown ("More info", then "Run
  anyway"). Check the file first. Each release has a `.sha256` file next to the installer: in PowerShell,
  `(Get-FileHash DevMem-Setup-<version>.exe -Algorithm SHA256).Hash` must equal the hash in it (the case differs). A
  release built by GitHub Actions also carries a build provenance attestation:
  `gh attestation verify DevMem-Setup-<version>.exe --repo Thamer-A/DevMem` shows that the file was built by this
  repository's workflow from the tagged commit.
- **Uninstall**: Settings > Apps > Installed apps > DevMem > Uninstall. It removes the program folder, the shortcuts
  and the PATH entry, nothing else: devmem's per-user folder (`%APPDATA%\DevMemory`, with the projects' switches and
  the recent projects) and every project's `.devmemory/` stay. Run `devmem uninstall` in projects you set up with
  `devmem init` *before* uninstalling: afterwards their hook entries point at a Python that is gone (Claude Code reports
  a hook error and records nothing; prompts are not blocked). Installing a newer version over an older one keeps the
  paths, so hook entries keep working.

Building the installer and bumping the bundled Python: [packaging/windows/README.md](../packaging/windows/README.md).

## Set up a project

With the plugin, run `/devmem:setup` in Claude Code inside the project. It creates `.devmemory/` (with its
`.gitignore`), runs the first scan and notes the project as switched on in the plugin's data folder. Recording starts
with the next hook event, so the rest of the setup turn is recorded too. `devmem setup --plugin-data <folder>` does the
same from a terminal, given the plugin's data folder.

Standalone:

```bash
cd path/to/your/project
devmem init
```

`devmem init` does four things:

1. Adds hook entries for eight Claude Code events (`SessionStart`, `UserPromptSubmit`, `PostToolUse`,
   `PostToolUseFailure`, `Stop`, `StopFailure`, `PostCompact`, `SessionEnd`) to `.claude/settings.local.json`.
   Entries already in the file are kept, and the first time devmem changes an existing file it saves a copy in the
   store, `.devmemory/init-backups/settings.local.json`, where git ignores it (earlier versions put
   `settings.local.json.devmem-backup` next to the file, where git shows it; `devmem doctor` points such a file out,
   and this repository's own `.gitignore` ignores `*.devmem-backup`). The hook command contains the absolute path of this Python and of
   the folder that holds the `devmem` package, so it belongs in the personal `settings.local.json`. `--shared` writes `settings.json`
   instead, which is normally committed; teammates would then get a command that does not exist on their machines.
2. Creates `.devmemory/` with a `.gitignore` that ignores everything in it. Every other command that creates the
   store (`devmem scan`, `run`, `import`, `begin`, `end`, `wrap`, `codex`, `devmem mcp`, the app's scan) writes that
   same `.gitignore` if it is missing; an existing one is kept, with `*` added at the end when it does not already
   end by ignoring everything (a cloned repository can ship one that ignores nothing), so memory is not committed by
   accident. If the store already exists and git tracks a file in it, or it holds anything besides that `.gitignore`
   while you have not set this project up before, `devmem init` stops and asks for `--adopt-store` (see "Install").
   A store devmem made for you (`devmem scan`, `begin`, the app) is used without the flag while git tracks nothing
   in it.
3. Writes this project's switch to devmem's per-user folder (outside the project). The hook entries record nothing
   without it; `devmem uninstall` turns it off (and keeps a note that you had set the project up).
4. Scans the project once and writes a map (`.devmemory/PROJECT.md` and `.devmemory/index/`).

**Upgrading from an earlier devmem:** hook entries written before the switch existed record nothing now. `devmem
status` and `devmem doctor` say so ("hooks are installed but this project is not switched on for this user"). Run
`devmem init` again in each such project; if the store holds anything, it asks for `--adopt-store`, because nothing
in the project can show that the store is yours (a repository can ship both the hook entries and the store).

**Upgrading from 0.3.0:** this version uses a store's recordings only when devmem's per-user folder notes the store as
yours, and 0.3.0 wrote no such note. Projects set up with plain `devmem init` or `/devmem:setup` keep working (your
switch for the project counts for its `.devmemory`). Two kinds of store need one step: a store outside the project
(`devmem init --store <folder>`) and a store made by commands used without `init` (`devmem begin`/`end`, `scan`,
`wrap`, or a tool set up with `devmem connect`). Until then their sessions are not used and nothing is recorded
there; the hooks say so when a Claude Code session starts, and `devmem status`, `doctor`, `sessions`, `pack`, `begin`
and the MCP tools give the reason. Run `devmem adopt` in the project (`devmem adopt --store <folder>` for a store
elsewhere): it shows what the store holds (how many sessions and from when, files git tracks in it, whether its
config asks to turn redaction off, its own `redact` and `ignore` files) and, after you answer `yes` (`--yes` when
not run from a terminal), notes the store as yours. It installs nothing, and nothing in the store is lost or changed
before that. Embedding models that 0.3.0 downloaded into fastembed's own default cache are not found there any more
(0.4.0 keeps them in devmem's per-user folder unless `cache_dir` says otherwise): run `devmem embed --download` once,
or set `cache_dir` to the old folder.

After that, in every Claude Code session in that project (restart a running one): each prompt is recorded and
gets a context pack when something relevant exists, each tool call and edit is recorded, and the session record
is finished when the session ends. Later scans are incremental. devmem sends nothing anywhere (the one exception is
the optional embeddings model download, see "Optional embeddings").

To undo: `devmem uninstall` removes exactly the hook entries devmem added (and the settings file and `.claude/` folder too, if `devmem init` created them and nothing else is in them) and turns the project's switch off. `devmem forget --all` deletes what was
captured (see "Privacy" for exactly what it deletes). Deleting `.devmemory/` removes the rest; with no store folder the hooks do nothing.

Commands you will use:

| command | what it does |
|---|---|
| `devmem status` | captured sessions (turns, in progress), whether hooks are installed, whether the store is git-ignored |
| `devmem pack "task"` | print the context pack a session would receive for that task (`--copy`: put it on the clipboard) |
| `devmem sessions` / `devmem show <id>` | list the recorded sessions / print one turn by turn with who wrote each text (what the MCP tools `list_sessions` and `read_session` return) |
| `devmem connect <tool>` / `disconnect <tool>` | set devmem up for another AI tool (MCP entry and instruction block in project files), or remove it; see "Other tools" |
| `devmem forget <id> / --before DATE / --all` | delete captured sessions (`--all` says how many files it deletes before it asks) |
| `devmem adopt` | show what an existing store holds and note it as yours (a store devmem 0.3.0 made, or one you trust); installs nothing |
| `devmem doctor` | self-check that changes nothing (it writes and removes one empty temporary file in the store to test that the store is writable): Python, store, index, hooks and their interpreter and script, tools set up with `devmem connect`, `claude`, git-ignore. A store you cannot write to, a hook whose Python or script no longer exists, or a connected tool's MCP command that no longer resolves is reported as a problem |
| `devmem scan` | update the project map; prints how many files were added, changed and removed |
| `devmem setup` | what `/devmem:setup` runs: create the store, scan, and note the project as switched on in the plugin's data folder (`--plugin-data`), without writing hooks; `--off` switches it off |
| `devmem ui` / `devmem app` | the local viewer (see "The app") |

`devmem <command> --help` lists the options with examples.

## Other tools

Claude Code hooks give the fullest record. Other tools work through an MCP server, a wrapper command, or a
begin/end snapshot, and record less. `devmem connect <tool>` sets a tool up in one step: it adds the `devmemory`
MCP server to the tool's project MCP file and a short instruction block (call `get_context_pack` first,
`begin_session` / `end_session` around edits) to its instruction file, and `devmem disconnect <tool>` removes
exactly that (a block is touched only while it hashes to what connect recorded writing; see docs/ANY_TOOL.md). Supported: `cursor`, `vscode` (Copilot), `codex`, `gemini`, `devin` (formerly Windsurf),
`claude-desktop` (printed only: its config is global) and `generic` (prints the standard `mcpServers` entry,
writes an `AGENTS.md` block). `--print` shows what would be written. Backups of changed files go to the
git-ignored `.devmemory/connect-backups/`, and an entry holding this machine's absolute paths is reported as
machine-specific (not for committing). What connect wrote is remembered in `.devmemory/connect.json`; that file is
inside the project, so a repository can ship one, and it is never trusted for a deletion: `disconnect` deletes only
the file it is disconnecting (once nothing but devmem's entry is left in a file connect created), that file's backup
under the one name devmem gives it, an old-style `<file>.devmem-backup` next to it, and empty folders connect created
for it, never through a link; other names in `connect.json` are ignored and reported. In a store devmem did not create
for you (or that devmem 0.3.0 made), the MCP tools refuse to work until you adopt it: `devmem adopt`, or `devmem
connect <tool> --adopt-store` (see "Install"). The formats were taken from each tool's
documentation on 2026-11-14; none of these tools has been run with devmem here. For a chat window with no tools,
`devmem pack "<task>" --copy` puts the pack on the clipboard. See [docs/ANY_TOOL.md](ANY_TOOL.md). For sessions recorded through MCP
`end_session`, the request text is supplied by the AI client, not typed by you; devmem stores it with
`prompt_source: "reported-by-client"`. Packs, `read_session` and the app label it that way instead of as your
verbatim words, and the app does not list it among Decisions (those are only sentences you wrote). In short:

```bash
devmem connect cursor                                           # or vscode, codex, gemini, devin, claude-desktop, generic
devmem pack "fix the login bug" --copy                          # chat without tools: paste the pack above your request
claude mcp add devmemory -- devmem mcp --project .              # MCP: pack and session tools for any MCP client
devmem wrap --prompt "add tests" -- aider --message {prompt}    # any command-line agent
devmem begin   # prints a pending id; later: devmem end --id <id> --prompt "what I asked"
devmem codex "refactor the parser"                              # Codex CLI adapter
```

## Which stores are used, in detail

If `/devmem:setup` or `devmem init` finds a `.devmemory/` folder that already holds anything besides devmem's
standard `.gitignore` (settings, an ignore list, a map, an index, sessions), and you have not set this project up
before with either route, it stops: "this repository ships a memory store" when git tracks files in it, otherwise
that the folder already exists and was not created by this setup. Both say its contents would be added to your
prompts; `--adopt-store` uses it anyway. Your own earlier work counts as set up before: `devmem init`, `devmem
uninstall` and then `/devmem:setup` (or `devmem init` again), `/devmem:setup` then `devmem init`, and a reinstalled
plugin need no flag, and so does a store devmem made for you (`devmem scan`, `devmem begin`, `devmem wrap`, the app:
devmem notes it in its per-user folder when it creates it) as long as git tracks no file in it. An ignore or config
file you wrote by hand in a store devmem did not make does ask once. A store whose `.gitignore` is a folder, or that holds symbolic links, is
refused even then (a linked `.gitignore` is replaced by the standard file). A moved or renamed project is a different
folder: it is not recorded until it is set up again there, and that asks for `--adopt-store`, since the store moves
with it.

The same rule decides what is served and where devmem writes. The recorded sessions in a store are used (by the
hooks, `devmem pack`, `sessions`, `show`, `status`, `wrap`, `codex`, the MCP tools and the app) only when devmem's
per-user folder says the store is yours: devmem created it for you (any command that creates a store notes it there),
you set the project up with `devmem init` or `/devmem:setup`, or you adopted it: `devmem adopt` (below), or
`--adopt-store` with `devmem init`, `devmem setup` or `devmem connect <tool>`. The code that reads recorded sessions
asks this itself, so a route cannot skip it. Any other store, one a repository ships, one that came in an archive, or
one devmem 0.3.0 made (it kept no such note; see "Upgrading from 0.3.0"), is refused with that reason and the command
to use, and nothing in it is changed: `sessions`, `show` and `pack` exit with status 2; `status` and `doctor` say
why and change nothing (doctor does not test such a store for writing); `scan` writes its map and index into devmem's
per-user folder instead (`maps/<project key>`, the folder of the files-only pack below) and says so; `connect` refuses
unless you pass `--adopt-store`; `disconnect <tool>` removes devmem's entry from the tool's own files by what they
hold and neither reads nor writes the store's `connect.json` or backups; `run`, `import`, `begin` and `end` record
nothing and exit with status 2; `wrap` and `codex` still run the agent, with a context pack of the project's own files
only, print one line with the reason and record nothing (that pack reads nothing from the store, not even its
`ignore` file; its map is kept in devmem's per-user folder, `maps/<project key>`, so a later run only updates it, at
most 8 projects' maps and none over 256 MB are kept, and `devmem adopt` removes the project's; the scan for it stops
after 20 seconds, and the agent then starts with a pack of the files read so far and a one-line note); the hooks record nothing and
add nothing to prompts (in a project you switched on, SessionStart shows you one line saying so and naming the command
that resumes recording; a hook event that fails before this check logs into the temp folder's log, not the store); the app shows the project read-only without sessions (its store browser still lists the
files, so you can look at them); and the MCP server, from its start on, answers every call with the reason and writes
nothing there (no map, no index, no call log, not even the `.gitignore`). When git starts tracking a file in a store
that was not there, or not the same, when you set the store up or adopted it (a `git pull` can bring one), the store
counts as not yours until you delete those files from the store (`git rm` them and commit that) or adopt it
again (`devmem adopt`), so the hooks stop recording there too (`devmem status` names the file). A file counts wherever its index entry lands on your file system, whatever the spelling (on Windows
and macOS `.DevMemory/sessions/x`, `.DEVMEMORY/...` or the short name `DEVMEM~1/...` land in `.devmemory/`; a short
name counts when it holds `~`, as Windows makes them). git's
answer is kept outside the project for its index, so git runs for this only after its index changed. When the git
program cannot answer (it is not on the PATH the hooks or the MCP server see, as with GitHub Desktop's or Visual
Studio's own git, or it failed, timed out or refused the repository, as with git's "dubious ownership", where the
index file answers instead), devmem reads git's index file itself (index versions 2 to 4, with git's own checks of
names, order, modes and extensions; an untracked cache is skipped; not a split or sparse index, nor a repository whose
configuration names another work tree or includes other files). While neither can say for the current index, no
recorded session from the store is used, and devmem asks again within a minute or as soon as the index changes: the
hooks say so when a session starts, or on the prompt that found it; meanwhile they keep recording the session when
git's last answer for the store was clean (otherwise they record nothing and count it, below), and for a cause that does not pass by itself
(no git and an index devmem cannot read) the notice names the fix. A devmem process that ended while asking (Claude Code
ends a hook at its time limit) is said as such: the hooks ask again a minute later, or as soon as the index changes, and
wait an hour only when it happens again within half an hour; `devmem status` and `doctor` ask at once, without a
hook's time limit, and their answer is kept for that index. Only outside a git work tree can nothing be tracked.
The whole question is bounded, git's part and devmem's: one 5-second limit; git's output is read up to 64 MB and devmem's
own reading of the index keeps memory within a small multiple of the file. Index entries whose names merely hold `~` are
not even listed when they cannot be the store's 8.3 short name: on Windows only those holding the store's own short name
are listed; elsewhere (Linux, macOS, and Windows when the short name cannot be asked for) only those in a form a
generated short name of the store could take (`DEVMEM~1`, `DEVME~12`, `DE3F2A~1`, with any letter case or extension),
whatever the file system says about letter case: a Linux process on a case-sensitive Windows folder (a Docker bind mount,
WSL) sees `DEVMEM~1` resolve to the store although `.DEVMEMORY` does not. Each such entry is compared with the store on
disk, by identity. devmem's own index reader skips the others the same way, as it skips non-ASCII names that cannot
spell the store's; so committed folders like `d~1/` or `d~000001/` cost nothing. Reaching a
limit (more than 250,000 index entries that could be in the store, more than 4,096 folders to compare, a path or a
version-4 index larger than devmem reads, an index of more than 1,000,000 entries read without git) is said as such, on every route including `devmem init`: it does not clear
by itself, the hooks record nothing meanwhile (even after an earlier clean answer), and if you trust the repository `devmem adopt` uses the store anyway (devmem then cannot see what a pull
brings into it while the limit holds); otherwise report it. Adopting covers the kind of limit you were shown (git's index
larger than devmem reads, or more of its entries in the store than devmem keeps) for git's index file as it was when
you adopted (its size, times and file id), whichever way devmem asks afterwards (git, or its own reading of the index).
Any rewrite of that file counts as a change (a commit, `git add`, or a `git status` that refreshes it): devmem then
asks again, and if it still cannot say, the store is not used until you look and adopt again.

Hook events that are not recorded because the store is refused (git cannot say and its last answer was not clean, a limit, the
store is not known to be yours, git tracks files in it that a pull brought) or holds a link where devmem writes are
counted outside the store, in devmem's per-user folder (`stores/<store key>.unrecorded/`, one small file per session
named by a hash of its id, one line per event with its time, the reason and the event's name, never its content). When
that session next records, its record gets one `not_recorded` field: how many events, by reason, and the times of the
first and last (the count's lines are marked as taken just before that field's event is written; if devmem is
killed at that moment the count can be lower than the events); the app shows it on the session's page and `devmem
status` next to the session. A session that has not
recorded since (it ended while the store was refused) is listed by `devmem status` with the same counts. The counts
are bounded: a session's file stops at 256 KB (the count then says "at least"), at most 64 sessions per store are
counted (a fully taken file makes room; past that a new session's events are not counted), and a file untouched for
30 days is deleted (`tests/test_hook_not_recorded.py`). A session devmem launches and records itself is not counted,
and when both the `devmem init` handlers and the plugin's fire, only one of them counts. The store's `config.json` is not read while the store is refused, so events are counted even with `"capture": false` there; such a count is then never taken into a record and stays listed by `devmem status` until its 30 days are over.

A store folder that is itself a link (`.devmemory -> elsewhere`, which a cloned repository can ship), or whose
`index/`, `sessions/`, `live/`, `pending/`, `connect-backups/` or `init-backups/` folder is one, is never written by any
command: the scan, `devmem pack`, the MCP server, `begin`/`end`/`wrap`/`run` and the hooks all refuse it (the CLI exits
with status 2 and says which link), the search index stays in memory, and the app shows such a project read-only with
that message instead of scanning it.

Links that arrive later (a `git pull` that adds `.devmemory/hook_errors.log -> elsewhere`): on every event the hooks
check with `lstat`, not by walking the store, the store folder, `live/`, `sessions/`, `index/`, `pending/`, the log
and SQLite files they write and this session's live files; if any is a symbolic link they write nothing for that
event (one line in the temp folder's `devmem_hook_errors` log at SessionStart). Appends are opened with `O_NOFOLLOW`
where the platform has it and refused when the target is a link; SQLite falls back to an in-memory index when its
file, folder or journal is a link. Session files are written by replacing them, which replaces a link rather than
following it.
