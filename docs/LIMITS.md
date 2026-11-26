# Current limits

Everything known not to work, not tested, or bounded, with the measurements behind it. Security-relevant limits are summarised in [SECURITY.md](../SECURITY.md).

Section names in quotes ("Install", "Privacy", "Optional embeddings", ...) are the headings of the pages in this folder; [README.md](README.md) lists which page holds each.

## Current limits

- Tested only by unit test, not by use: macOS and Linux (CI runs Linux and Windows on Python 3.10 and 3.13, but
  devmem has not been used by hand on macOS or Linux), and interactive terminal sessions (the hook tests replay
  payloads recorded from real Claude Code 2.1.323 headless runs; a long interactive session has not been observed).
- Seen on the first walk through the documentation with real headless sessions (`claude -p`, Claude Code on
  Windows, 2026-11-17, one machine): with the plugin route, Claude Code printed `SessionEnd hook [...] failed: Hook
  cancelled` on stderr at the end of 3 of 3 runs, and the end of the session was recorded all the same; with
  `devmem init` it was not printed in 10 of 10 runs. The cause is not established. On the same walk a prompt that
  paraphrased earlier work without sharing its words got no pack, and nothing in `devmem show`, `devmem sessions`,
  `devmem status` or the viewer says which prompts got a pack.
- Not measured: whether devmem saves tokens or improves results on a real repository. No benchmark on real
  projects exists yet, so no token-saving figure is claimed.
- Hook capture is for Claude Code. Codex, MCP clients and IDE tools record less (see
  [docs/ANY_TOOL.md](ANY_TOOL.md)). The MCP server's `2026-07-28` protocol path was written from the
  specification text and has not been tried with a real client.
- Retrieval is keyword-based over a local index (embeddings are optional and off; see above). Python files are parsed with the standard library; JavaScript,
  TypeScript and Luau use pattern matching; other files are indexed as plain text. For game projects the map does not
  yet link assets or editor state to code. The patterns read only the first 256 KB of a file (declarations further
  down are not listed, and the file's entry in `map.json` says `extract_truncated`) and skip lines longer than 4 KB
  (minified or generated code); the whole text is still indexed for search. A file whose extraction took longer than
  half a second is remembered by its content and indexed as plain text only from then on (`extract_skipped`).
  `tests/test_extract_speed.py` and `tests/test_extract_growth.py` check the extractors on adversarial files up to
  512 KB; the slowest shape measured on 2026-11-14 took about 0.9 s (lines of Python f-strings, one Windows machine
  that was running other work). That is not what bounds a prompt: the refresh a hook or an MCP `get_context_pack` call
  starts (listing the project, extracting changed files, the map) runs in a separate process that is killed after
  8 seconds (20 for `get_context_pack`), and the pack is then built from the index as it stands. The file that process
  was working on when it was killed at the budget is indexed as plain text only from then on, and if even that is
  stopped, by its name only: a file devmem cannot handle costs at most two slow prompts, not every prompt. A refresh
  process that ends because its prompt did (you cancelled it, or Claude Code stopped the hook) does not count against
  the file it was on, which is tried again the same way once; so a stuck file costs at most one more budget when that
  happens, and the second time it is found that way it is demoted as well. When nothing changed, no
  process is started; when something did, starting it added about 0.25-0.3 s per refresh on Windows and 0.1 s on Linux
  (measured 2026-11-14, a 200-file project). What a refresh did not reach is indexed on the next prompt. A refresh that
  ran out of time on its own (it stops reading files 1.5 s before the budget) writes a map, `PROJECT.md`, `devmem
  status` and `devmem doctor` that say the scan is incomplete; one that had to be killed leaves the previous map. A
  project whose listing alone takes longer than the budget is not indexed from the hooks at all: run `devmem scan`.
  `devmem scan`, the app, and `devmem pack` when there is no map yet scan in their own process with no time limit.
  The map is bounded whatever the project holds: a Rojo manifest adds at most 50,000 entries and 1,000,000
  characters, file entries keep only path, language and size past about 8 MB of them and are left out past 12 MB,
  `map.json` is at most 32 MB and `PROJECT.md` 64 KB, and both say what was left out. A map build that gets the
  refresh process killed is built reduced from then on (without the Rojo mapping, import graph and conventions, then
  file entries only), for a day or until `devmem scan`. After the refresh, the pack indexes new or changed session
  records while its 4 seconds (5 for `get_context_pack`) last, 8 documents per write, so it ends at most one such
  write and one record's load past them; another session that is still running costs its new turns only, and what
  is not reached is indexed on the next prompt.
- Overlapping `devmem begin` sessions in one working tree see each other's file changes.
- `devmem begin`/`end` (and `wrap`, `codex`, MCP `begin_session`/`end_session`) compare at most 2,000 changed files;
  each begin and each end reads at most 5,000 files and 128 MB and redacts 8,000,000 characters, and an end starts no
  `git show` after 30 s of them; a pending begin is at most 32 MB and a record's edits and diff are capped (SECURITY.md lists
  the bounds). Past a bound the record lists the files it did not compare in `files_not_compared`, never as your
  session's edits, and the command says so. A file the begin did not see counts as created only when its modification
  time lies inside the session; that time is the file's own, so a tool that sets file times (an archive extracted
  with its times) can move a file either way.
- `devmem import` lists the files its stream's tool calls name that are gone when you import it in
  `files_absent_at_capture`, not as deleted; `devmem run` captures as the session ends and records them as deleted.
  `run` keeps the whole lines of the first and last 64 MB of the program's output and says how much it left out;
  when the program ends it waits at most 2 s more for its output (one deadline for standard output and standard
  error), also when a process the program started keeps them open, and at its timeout it kills the program's
  process tree; `wrap` and `codex` wait the same way (tests/test_proc_grace.py). On Linux and macOS the program
  runs in a session of its own, without a controlling terminal: Ctrl-C reaches devmem, which passes it on as
  SIGINT, then SIGTERM after 3 s, then kills the program's process group after 3 s more. `run` then records what the
  program wrote until then, says that the run was interrupted, and exits with status 130; a second Ctrl-C while it
  records stops it, and it says that nothing was recorded (`wrap` and `codex` record nothing when interrupted).
  `import` reads the
  stream-json `claude -p --output-format stream-json` writes, and also a Claude Code transcript (the JSONL Claude
  Code keeps for a session, whose format is not a documented interface): its session id, and its other fields by
  the rule in SECURITY.md (tests/test_import_transcript.py). A stream file in UTF-16 (Windows PowerShell 5.1's `>`)
  is read as such, also from a line where the encoding changes (`>>` appending UTF-16 to a UTF-8 file), and the
  command says when a file's encoding could not be read reliably. A line `import` does not take apart (not a JSON
  object, nested too deep, longer than 4 MB, more values or distinct keys than one line may hold, or past the
  stream's budgets) is a stub, counted by its reason; of a line longer than 4 MB the first 64 KB are read for its
  tool calls, so a Write of a 5 MB file is listed as edited, without its text, unless more than 64 KB of the line
  comes before the call. A large ordinary session is recorded up to its text budget (8,388,608 characters of text):
  the lines after it are stubs, counted and named in what `import` prints. The text of a tool result is kept
  only when its call was one devmem checked for secret files (SECURITY.md).
- A session's event log (`.devmemory/live/<id>.jsonl`) is read at most 128 MB per hook event, as a stream. A longer
  log (some 30,000 ordinary events; a 70 MB one took about 1 s per Stop on a loaded Windows machine) keeps its record
  as it was at the last full read: every event stays in the raw log, `hook_errors.log` says so once, and that
  session's record is not brought up to date any more. A record is never replaced by one with fewer turns or tool
  calls than it held.
- What a hook keeps of `git status` is of a fixed size whatever the work tree holds: HEAD, a dirty flag and counts,
  and per turn at most 200 of the paths that were deleted or changed outside the session's file tools (each cut to
  300 characters, 32 KB in all; the record counts the rest). The lists it compares from one event to the next are kept
  as 8-byte hashes in `.devmemory/live/<id>.gitseen`. When git's state before a turn is unknown (git could not describe
the work tree when the session started, or that file is missing or damaged), nothing git shows at that turn's end is
attributed to the session (a deletion or a change cannot be dated): the turn counts it under `git_unattributed`, and
that check becomes the known one, so what changes after it is attributed at the next Stop, but only once that file
could be written again: while it cannot (it is a folder, say), every Stop of the session stays unknown, counts and
attributes nothing from git. A path that newly appears in git's list at a turn's end counts as changed by the session
only when its modification time falls inside that turn: at or after the turn's prompt (UserPromptSubmit, recorded in
the session's live state) and at most 2 s after the turn's end. A turn ends at its Stop (or StopFailure). Claude Code
sends no Stop for a turn you interrupt (Esc), so the next prompt, the SessionStart of a resumed session, or (for a
session that ended mid-turn) the next SessionStart in the project checks it first: it ended at its own last event,
which is its prompt or the last tool call of the main agent (PostToolUse or PostToolUseFailure without `agent_id`).
Other events do not move that end: a tool call reported as interrupted (`is_interrupt`, or a Bash or PowerShell result
marked `interrupted`), a sub-agent's or background agent's tool call (`agent_id`), a compaction (PostCompact,
SessionStart with source compact), a SessionStart of any source and a SubagentStop. A main-agent tool call reported
after you pressed Esc (Ctrl+Enter lets a running tool finish) counts as the turn's. A Stop whose hook did not get the
session's lock is noticed the same way, and its turn ends at that Stop. A SessionEnd with a turn open records only
that the session ended (one append; Claude Code gives SessionEnd hooks 1.5 s); the turn's check, the rest of the
transcript and the record are finished by a later SessionStart in the project (at most 8 sessions, 32 MB of logs and
one transcript read per SessionStart; a resumed session continues the same log instead). A SessionEnd cut off before
its append leaves the session to be finished as abandoned 12 h after its last event ("no SessionEnd seen"); one cut
off after it (also while it finishes the record of a session with no turn open: the record is replaced whole, the log
moves last) is finished by a later SessionStart with the reason SessionEnd gave, except one cut off in the moment
between its append and the mark that tells a later SessionStart so (`.devmemory/live/<id>.end`): that session is
finished as abandoned 12 h after its last event, with the reason SessionEnd gave, and the changes of a turn it left
open are then neither attributed nor counted. A Stop that comes again
without a prompt continues the same turn when Claude Code says a Stop hook continued it (`stop_hook_active`); any other
has an unknown start. A prompt whose hook does not get the session's lock (another hook of the session held it for
3 s) leaves its time in `.devmemory/live/<id>.prompt`, and the next event that gets the lock starts the turn there. A
new folder git lists (`gen/`) is dated by its own time, or else by the newest of up to 64 entries directly inside it.
While another Claude Code session of the same project is active (a turn of it open, or its tool calls still being
recorded after its turn; read from its files in `.devmemory/live/`, which this version writes), nothing dated in that
time is attributed to either session. An open turn of another session whose last recorded event is more than an hour
old (a session that crashed mid-turn) counts as active only until that event, and does not count toward the 32
sessions a check reads unless that event lies inside the turn. Everything else is counted under the turn's
`git_unattributed`, by reason:
`changed_between_turns` (a time after the previous check and before the turn's prompt, while no turn of this session
was open: your edit, another program or session, or work this session left running), `changed_not_in_interval` and
`folder_not_in_interval` (a time before the previous check, or before the prompt when that check is unknown, or in the
future: un-ignored, unpacked or restored with an old time), `changed_after_last_activity` (a time after the end of a
turn that was noticed later, which is its own last event or its Stop, and before it was noticed: a tool call still
running then, or your own edit; which one cannot be told), `changed_turn_end_unknown` (a time inside an earlier turn
whose end could not be checked: it ended without a Stop and the next prompt's hook did not get the lock, or git could
not be asked when it ended), `changed_turn_start_unknown`
(the turn's start is unknown: its prompt was not recorded, the live state was lost or damaged, the start it holds is
more than 7 days old, before the session's own start or more than a minute ahead, or a Stop came again without a prompt
and without `stop_hook_active`), `changed_by_other_session_tool` (another session's Write, Edit, MultiEdit or
NotebookEdit call named it during the turn), `changed_while_other_session_active` (any other path dated while another
session was active), `changed_other_sessions_not_read` (more than 32 other sessions could overlap the turn: an event
since it began, or an open turn that is not stale; more than one check reads), `changed_not_dated` (a path that could not be looked at), and
`changed_not_looked_at` (past the 2,000 looks per check). A file a turn leaves out of git's list (reverted with
`git checkout`, or hidden by a new ignore rule) is not recorded as a change. An edit you make by hand while a turn is
running, with no other session active, cannot be told apart from the session's and is attributed to it; on a file
system with coarse times (FAT: 2 s) a change made in the first seconds of a turn can be counted as made before it.
Changes the session's own file tools report (Write, Edit) are recorded from the tool calls, whatever their time. A work tree with more than 40,000 changed paths, or more than
  32 MB of `git status`, is recorded without git's changed files (`devmem status` says so).
- devmem notices files git tracks in your store. A record placed there by any other means (an archive, a patch
  applied with `git apply`, `git reset` after a pull, which leaves the pulled files untracked, another program) is
  taken as yours. If you pulled or applied something that touched `.devmemory/`, look at `devmem sessions` and delete
  what you do not recognise (`devmem forget <id>`).
- Untested: macOS spellings of the store's name (case folding, Unicode normalisation and ignorable code points are
  left to the file system's own lookup, which was not run there), and on macOS git is not killed when the devmem
  process that started it ends (no `PR_SET_PDEATHSIG`); FAT and exFAT volumes (no file ids: folders are compared by
  their resolved path); a split index (`core.splitIndex`), which devmem's own reader of git's index declines.
- On a case-insensitive file system (NTFS, macOS by default), two session ids that differ only in letter case
  (Claude Code's ids are lowercase UUIDs) share the same files in `.devmemory/live/`, so their events are recorded as
  one session under the first id.
- A program running as you (or as another user of the machine, which can reach 127.0.0.1) can keep the viewer
  busy by opening many connections that send nothing: the viewer serves 64 at a time and drops a connection whose
  request has not arrived within 10 seconds, but a program that keeps reopening them can keep you out of it while it
  runs. It cannot read anything without the key.
- The viewer's Content-Security-Policy allows inline `style` attributes (`style-src 'unsafe-inline'`), because the
  page uses them throughout. Scripts are limited to the viewer's own file, and every value from a store record is
  escaped before it is shown, so this matters only if an escaping mistake were found: injected markup could then
  change how the page looks (show misleading text, for example), but not run scripts.
- The plugin route is new, and these are untested: macOS; an install from a GitHub marketplace, where Claude Code
  runs the plugin from its cache copy (one install from a local-path marketplace, Claude Code 2.1.325 on Windows,
  ran the plugin from the source folder; there the data folder was `~/.claude/plugins/data/devmem-devmem`,
  `claude plugin uninstall` deleted it, and the setup skill's Bash rule was pre-approved, so `${CLAUDE_PLUGIN_ROOT}`
  and `${CLAUDE_PLUGIN_DATA}` are substituted in Bash rules); Claude Code on Windows without Git Bash (the
  PowerShell hook route is tested by running its command line in Windows PowerShell 5.1, not inside Claude Code);
  whether the same substitution happens in PowerShell permission rules (if not, the skills' PowerShell forms ask
  before running); interactive sessions (only short headless `claude -p` runs on Windows were tried).
- An apostrophe in the plugin's folder or data folder path (a Windows user name such as O'Brien) breaks the skills'
  command lines, which quote those paths in single quotes; the hooks are not affected.
- Deep folders on Windows: with long paths turned off (Windows' default), devmem works for a project whose path is
  up to about 180 characters (measured: 183 works, 184 fails), because its store files need up to 76 more and
  Windows refuses a path of 260 characters or more; beyond that the hooks' writes fail (the session stays
  "in progress", then from about 200 characters nothing is recorded; the errors go to `.devmemory/hook_errors.log`),
  and from about 220 characters `devmem init`, `scan` and `pack` stop with "file not found" and say why. `devmem init`
  warns when the store's path is that long. Turning long paths on (`LongPathsEnabled`) or a shorter folder avoids it.
- The Windows installer's only tests are those of the `windows installer` workflow on GitHub's Windows runners
  (silent install, the installed `devmem`, a hook event, the app without a window, silent uninstall); an interactive
  install by hand, the Start Menu window opening and SmartScreen's prompt have not been tried. It is not code-signed.
- `/home` is accepted as a project folder when the home folder is somewhere else (for example `/root`); only a
  drive or file-system root, the home folder and folders that contain it are refused.
- A `BASH_ENV` file that redefines `exit` or `set` as shell functions can still change what the sh hook command
  does; only `set -e`/`set -u` and EXIT/ERR traps are neutralised.
- Links: symbolic links and Windows junctions at the store, at the folders devmem writes in and at a session's live
  files are refused or bypassed (see "Install"), and the viewer never lists or follows one. Hard links are not
  detected, and a junction deeper inside a folder devmem only reads is not looked for. A project's
  `.claude/settings*.json` that is a link: the hooks never follow it, and `devmem init` and `devmem uninstall` refuse
  it, or a `.claude` folder that is a link or junction, wherever it leads (out of the project a cloned repository
  could link `~/.claude/settings.json`; inside it, `../credentials.json` or `../.env`: init would copy either into a
  project file), without changing anything; replace the link with a real file. They also refuse a settings file that
  is not one (`hooks`, `env` or `permissions` not an object, or credential-like keys such as `private_key` or
  `client_secret`), so its content is never copied or rewritten.
- Project files: the scan lists only plain relative paths inside the project (no `..`, absolute, drive, UNC or
  backslash names, which a hand-written `.git/index` can make git print) and never reaches a file through a linked
  folder (git still lists a tracked folder's files after the folder is replaced by a link). Files are read only when
  they are regular files and not links, up to 512 KB for indexing (FIFOs, devices and links to them are never
  opened); `default.project.json` and `.gitignore` files over 1 MB, and `.gitignore` lines over 4 KB, are not read;
  folders nested more than 100 deep are not walked. Paths recorded in sessions are checked the same way before a
  freshness check opens them. A file whose name holds a control or bidi character (a newline, an escape code, a
  right-to-left override) is not indexed; `devmem status`, `devmem doctor` and `PROJECT.md` say how many were
  skipped, and any such character that reaches a pack or `PROJECT.md` from a recorded path or text is shown escaped
  (`\n`, `\u202e`), so it cannot start a line of its own. Everything devmem prints to a terminal (command output and
  messages, also names and fields from a store a repository shipped) shows control, bidi and invisible characters
  escaped the same way, so no escape sequence (colours, a window title, an OSC 52 clipboard write) reaches the
  terminal; an MCP client's `client` name must be a short plain name. A path in a hook payload or a tool's input that names a network
  share (`\\host\share\...`, `//host/...`) is handled as text only: devmem never resolves, stats or opens it, so
  recording a tool call on a share does not contact that host.
- Programs devmem starts (git, the clipboard command, `codex`, `claude`, the program `devmem wrap` runs, the file
  manager) are looked up in the absolute folders on PATH only and started by their full path: never from the current
  folder, a relative PATH entry or the project (on Windows the current folder is otherwise searched first, so a
  `git.exe` or `clip.bat` in a cloned repository would run). The plugin's launchers do the same: the sh launcher keeps
  only absolute PATH entries for the programs it runs (find, date, id, mkdir, rm, mv, timeout), and the Windows one
  sets `NoDefaultCurrentDirectoryInExePath` and runs `forfiles` by its full path. On Windows `devmem connect` writes the full path of
  `devmem` into the tool's config for the same reason, which makes that entry machine-specific.
  Every program start goes through one function (`devmem/proc.py`). On Windows a `.bat` or `.cmd` file (npm's
  `codex.cmd`, for one) runs through cmd.exe, which reads `&`, `|`, `%VAR%` and `"` in its arguments as commands:
  such a program, and `cmd`, `powershell` or `pwsh`, is not started when an argument contains one of those characters
  (devmem says so in one line), and a project folder is passed as the working folder, never as an argument.
- devmem runs only read commands of git (status, ls-files, rev-parse, diff, show, check-ignore, config) and switches
  off what a `.git/config` or `.git/hooks` shipped with a project could make them run: hooks (`core.hooksPath` points
  at nothing, and `--no-optional-locks` keeps status from rewriting the index), `core.fsmonitor`, the clean, smudge
  and process command of every filter driver the repository configures, the pager, external diff and textconv, ssh,
  askpass and credential helpers, the editor and `core.gitProxy`. The filter drivers are listed by git itself before
  every burst of commands (`git config --show-scope`, which runs nothing), so it does not matter where the
  repository keeps them: `.git/config`, `config.worktree`, a `commondir` or `gitdir:` redirect, an included file.
  A driver from your own system or global configuration (git-lfs) keeps working unless the repository redefines
  one of its keys. Submodules and nested repositories are left alone (`--ignore-submodules=all` on status and diff,
  `submodule.recurse=false`): git would otherwise run `git status` inside them under their own configuration. A
  repository whose filter drivers cannot be listed, has more than 200 of them, or has one with a name that cannot be
  passed back to git safely is treated as if git were not installed; so, for the file list only, is a repository
  whose index lists more than 200,000 files (the folder is walked instead). One git command gets 5 seconds from a hook
  (up to 60 for the file list of a command you run, half of the time left for the file list inside a time-limited
  refresh), its filter listing included; at the limit git and its children are killed, that process does not start
  git again for a minute, and neither does any other devmem process in that project (a marker in devmem's per-user
  folder: the files are walked instead, and a repository that was only slow once is asked again after the minute). On
  Linux git is also killed when the devmem process that started it ends (not from the multi-threaded viewer, and not
  on macOS). The question which store files git tracks is asked under one 5-second limit that also covers devmem's
  own work on git's answer (and fixed caps on it), only after git's index changed, not at all when the index is not
  a regular file (the store is then not used), and after neither git nor devmem's reading of the index could answer,
  not again for a minute unless its index changes. During that minute recording goes on when git's last answer for
  the store was clean, but no recorded session is used. Tested with git 2.47 and 2.51; a git older than 2.26 cannot tell
  where a driver is defined, so there every driver is switched off, yours too. Not covered: programs that git
  itself is configured to run by the system or by your own global configuration outside these settings, variables
  such as `GIT_DIR` already set in the environment devmem is started from, and a git build with other extension
  points.
- The hook command, launchers, skills' command lines and setup are tested on
  Windows (Git Bash and Windows PowerShell 5.1) and on Linux in Docker. The plugin's hooks start a shell before Python, so on
  Windows each event costs more than with `devmem init` (measured on one machine, medians of 7, from outside the
  process: a recording event 106 ms with `devmem init`, 172 ms under Git Bash, 373 ms under Windows PowerShell; a
  prompt with a pack 314, 416 and 624 ms). In a project without a `.devmemory/` folder a plugin hook costs about
  40 ms under Git Bash and 270 ms under PowerShell, since no Python starts; with a `.devmemory/` folder that was not
  switched on, about 130 and 345 ms. On Linux (Docker, two runs) the same events took 27 to 85 ms.
