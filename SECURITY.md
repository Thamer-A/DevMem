# Security

## Reporting a problem

Please report security problems privately: on GitHub open the repository's **Security** tab and choose
**Report a vulnerability** (<https://github.com/Thamer-A/DevMem/security/advisories/new>). Do not open a public issue for something that could put users at risk.
Reports get a reply as soon as the maintainer can manage; this is a one-person project.

Only the latest version on the `main` branch is supported; no release has been published yet.

## What devmem protects

devmem records AI coding sessions on your machine and gives later sessions short context packs. The rules it
is built to keep:

- **Nothing leaves the machine.** The core makes no network requests. Optional embeddings run a local model
  (the embedding library downloads it only in `devmem embed --download`; every other command, the hooks, the MCP
  server and the recall skill's lookups load it from local files only and never download it, so an agent running
  `devmem pack` cannot start a download; fastembed always gets an explicit cache folder, the config's `cache_dir` or
  one in devmem's per-user folder; a model folder or cache folder inside the project or its store is never used,
  whether the config names it or a cache variable in the environment does (`FASTEMBED_CACHE_PATH`, `HF_HOME`,
  `HF_HUB_CACHE`, `TRANSFORMERS_CACHE`, `XDG_CACHE_HOME`, `SENTENCE_TRANSFORMERS_HOME`, also a relative value):
  embeddings stay off for that call).
- **Nothing inside a project can switch recording on, switch lookups on, or weaken privacy.** The "on" switch
  is written outside the project by `devmem init` or `/devmem:setup`. A cloned repository that ships its own
  `.devmemory/` folder or hook settings is not recorded (with one exception while git cannot answer, below) and its content is not served until you adopt it
  (`devmem adopt`, which shows what the store holds first, or `--adopt-store`; a store devmem 0.3.0 made needs the
  same, since that version kept no note of it): no route (the hooks, `devmem pack`/`sessions`/`show`/`status`/`doctor`, `wrap`/`codex`, the
  MCP tools, the app) uses recorded sessions from a store devmem did not create for you (the app still lists its
  files, to look at; `wrap` and `codex` give the agent a pack of the project's own files only, which reads nothing
  from the store, not even its ignore file), because the code that reads recorded sessions asks that itself; none of
  these routes, the MCP server included, writes anything into one (a hook event that fails before the check logs in
  the temp folder), `scan` maps the project into devmem's per-user folder instead, `connect` refuses, and `run`,
  `import`, `begin` and `end` refuse to record there. A file git starts tracking in your store later (a
  pull) makes the store count as not yours, so its recordings are not used and the hooks stop recording there (a
  Claude Code session in a project you switched on says so when it starts), until you delete that file or adopt the
  store again. Which files that are is decided by where git's index entries land on
  your file system (compared by identity), so a pull that spells the store's path otherwise (`.DevMemory/`,
  `.DEVMEMORY/`, the 8.3 name `DEVMEM~1/` on Windows and macOS) is noticed too. When the git program cannot answer
  (not on the PATH devmem sees, failed, timed out, or refused the repository as "dubious ownership"), devmem reads
  git's index file itself with git's own checks (entry order, modes, flags, name lengths, names git refuses, extension
  bounds; an untracked cache, which changes no entry, is skipped by its size), and declines what it cannot read with
  certainty (a split or sparse index, an index whose entries git would read differently or refuse, a configuration
  naming another work tree or including files). What holds: the reader never lists entries other than those git lists
  for the same file. An index git cannot read at all may still be read: git can crash on a damaged untracked cache
  after it has read the entries (16 of 7,000 fuzzed cases), and the reader then lists exactly the entries git lists
  for the same file without that extension. On macOS, names holding a code point HFS+ ignores (`.dev` U+200C `memory`)
  are asked for on both routes and compared by identity; this was tested on Windows and Linux only, not on a Mac; while
  neither can say what git tracks there, no recorded session from the store is used (the hooks keep recording the
  session only when git's last answer for the store was clean, and then read nothing else from the store). That last
  answer is kept by the store's path only: a project deleted and cloned again at a path where you used devmem, whose
  repository ships a `.devmemory/`, gets this session's own files written into that store until git answers (and a
  `*` line added to the store's `.gitignore`, or that file created, so that git ignores them), and is refused then (`tests/test_hook_not_recorded.py`).
  Redaction can only be turned off by you
  (`--allow-unredacted`). A `.devmemory/connect.json` a repository ships is not read by `devmem disconnect` (nor
  written: nothing in a store that is not yours is changed), and `devmem init` never follows a linked `.claude/settings*.json`.
- **Nothing from a project reaches your terminal as a control sequence.** Everything devmem prints goes through one
  function that shows control, bidi and invisible characters escaped.
- **Files with secret-like names** (`.env`, `.envrc`, `*.env`, `.env-*`, `*.pem`, `*.key`, `*.ppk`, `id_rsa*`,
  `*secret*`, `*credentials*`, `*.tfstate`, `terraform.tfvars`, kubeconfig files and similar):
  the scan, `begin`/`end`/`wrap` and the index never read them, also when git names one by another spelling (an 8.3
  short name) or by a path outside the project. A tool call whose input names one has its output dropped: any
  path-like key at any depth (Read, Edit, MCP file tools whatever they call the argument, a `file://` URL), and a bare
  name under another key when it is a specific secret name (`.env`, not every name holding "secret"); other spellings
  of the name count (trailing dots or spaces, `::$DATA`, 8.3 short names, with the long name looked up for files in
  the project). For shell commands the check is best-effort text matching: it catches the plain name, the name as a
  shell would unquote it (`.e''nv`, `.\env`, `$'\x2eenv'`), brace expansions (`.e{n,}v`), PowerShell backtick and
  cmd caret escapes (`.e`nv`, `.e^nv`), `HOST:path` forms (`git show HEAD:.env`), 8.3 short names, trailing dots and
  globs with at least one literal character (`cat .en?`, `*.pe?`, `.[e]nv`), also under the `input`/`args`/`argv`
  keys of terminal tools, but not a name the command computes (`cat $(ls | grep env)`, `'.e' + 'nv'`) or a bare `*`;
  there, value redaction is the second layer.
- **Everything else that is recorded passes a redaction step first**, at the point where it is written to the
  store (every string of every record, whatever its field), and so do the project-file lines a context pack quotes
  and the pack log (`packs.jsonl`: one line per prompt with the keys of the items its pack carried, never prompt or
  pack text; `devmem forget` removes a forgotten session's lines and items from it).
  The rules also see JSON-serialised tool data as the plain text it holds (strings that sit next to each other, such
  as the lines of an Edit response's `structuredPatch` or a notebook's `source`, as one text, cut back into the
  strings by position so that no text moves from one string into another), lines as tools
  decorate them (grep, `cat -n`, `git blame` and `Select-String` prefixes, diff markers, comments, terminal colours),
  a diff as the old and the new text of each hunk (so a value whose name is on an unchanged line is redacted; a hunk
  that cannot be read back line by line is replaced), `diff -y` as its two columns, and values as their format
  writes them (a quoted value to its matching quote, a bare config value to the end of its line unless it is shaped
  like code, a reference or a type, or is a word of letters only that does not look like a credential); the project
  map and index are built from project files and are not redacted (secret files are never read).
  What the tests check of tool output: tests/test_redact_tool_views.py runs 23 real tool views (git, grep, ripgrep,
  diff, `cat -n`, `nl`, PowerShell `Select-String`, `findstr`) of five value shapes, each in a file with LF, CRLF,
  CR-only and mixed line ends, through the hook and searches the store; tests/test_redact_diff_edits.py does the same
  for a value edited inside 14 diff views of 13 shapes; [docs/PRIVACY.md](docs/PRIVACY.md) lists the views tested, the ones known to keep a
  value (a hunk whose head is cut off above the value's name: `git diff -U0`, a block or a list-item block edited more
  than three lines below its name in a default `git diff`, a cap that keeps the tail; `diff` in its normal format;
  `git range-diff` and a diff of a patch file without an inner hunk header; `--word-diff=porcelain`,
  `--color-words`; a `git blame` author holding a parenthesis; a `grep -C` window cutting a multi-line string; `diff
  -y` cutting a column) and the ones not tested.
  Redaction is a pattern-based safety net, not a guarantee ([docs/PRIVACY.md](docs/PRIVACY.md) and `devmem/redact.py` list what it does not
  catch, among them a secret with no recognisable shape and no secret-sounding name next to it, encoded text, a
  secret split across separate tool calls, and a diff hunk whose visible lines do not include the value's name):
  review what a store holds before you share it.
- **A path outside the project is not stored by the hooks.** A tool call's file or search path that is not a path
  inside the project is replaced by `path_omitted` with the reason (`devmem.sessions.project_file`, which serves the
  hooks and `devmem import`'s digest): outside the project (a sibling project, `//server/share/...`, a file under your
  home, `../other/x`, and the same as a `file:` URL in any of its forms, `file:///C:/x`, `file://host/share/x`,
  `file:/x`, percent escapes); not a local file path (any other scheme: `https:`, `vscode-remote:`, `untitled:`); a
  path devmem cannot take literally (`$HOME/x`, `${HOME}/x`, `%USERPROFILE%\x`, `~user/x`, a drive-relative `C:x`); or a name
  inside the project that is not a plain file name (an alternate data stream `app.py:stream`). So is such a path value
  in a tool's input summary (an MCP tool's `path`, `uri` or a nested key), in a tool's structured response
  (`filePath`, `filenames`), and a dictionary key of either that is such a path. A path inside the project in another
  spelling (`./x`, `\\?\C:\...`, `/c/...`, `/mnt/c/...`, `file:///<project>/x`, an 8.3 short name, other letter case, a
  link inside the project that resolves inside) is stored relative to the project. Only a path Claude Code's own file
  tools (Read, Write, Edit, MultiEdit, NotebookEdit, NotebookRead) read or wrote successfully, and git's own list,
  can make a turn record a file as deleted. `tests/test_hook_outside_paths.py` runs every spelling under every one of
  those fields and searches every store file for it. Free text is not taken apart:
  a shell command, a tool's printed output, an error message or a file's content can still hold an outside path, and
  what a tool read from a file outside the project is stored (redacted) like any tool output.
- **What devmem keeps is private to your user** on Linux and macOS (folders 0700, files 0600).
- **Bounded hook cost** covers devmem's own work: the built-in redaction rules are tested to grow linearly with the input on the inputs the tests build (every pattern, and every replacement callback, which reads
  only its own match, a constant window around it or a memo built once per text; tests/test_redact_speed.py,
  tests/test_redact_growth.py, tests/test_redact_pattern_growth.py, the line-length sweep in tests/test_redact_line_sweep.py
  and one line of 4 KB against one of 64 KB after each rule's match in tests/test_redact_forward_scans.py time them), the readings
  of a text beyond the plain one (a diff's sides, `diff -y` columns and every reading made inside them) share one
  budget per text, three times what its other readings handed the rules plus 4,096 characters
  (tests/test_redact_read_budget.py checks the bound on every pair of ten stacked decorations), and what the budget
  refuses is replaced whole by a marker: every line of the refused hunk and of the hunks after it in the same text
  (later commits of a `git log -p` included), every row of the refused `diff -y` block and of the blocks after it in
  the same text, every line of a text whose second reading
  (of carriage returns inside lines) is refused, and a JSON string such a reading would have decoded unless it is one
  line that decodes to its own text (tests/test_redact_budget_rest.py, including a value placed after a decoy that
  uses the budget up; on the review's ordinary corpora at the hook caps no call reached the budget), a hook cuts
  every text to its cap before redacting it, what a hook stores of git's state is of a fixed size whatever git lists
  (HEAD, a dirty flag and counts; a list of changed or deleted paths keeps at most 200 paths of at most 300 characters,
  32 KB in all, and counts the rest; what a hook compares from one event to the next is kept as an 8-byte hash per
  path in `live/<session>.gitseen`, which holds no text and is not passed through the redactor; what the project's
  other sessions read of a session is small too: its last 8 turns' times in `live/<session>.turn`, an open turn's start
  in `.open`, a prompt's time in `.prompt`, and a 35-byte line (a path hash and a time) per path its file tools edited in `.edits`, cut back to the
  last 1,024 at a Stop once it holds more than 2,048, with at most 32 other sessions read per check), a tool's input or response is redacted within a budget (at most 1,000
  strings and a few thousand characters of it; a dictionary key is kept and redacted up to 1,024 characters), and the
  check whether an input names a secret file looks at no more than 20,000 of its keys and values (an input with more
  is treated as naming one, so its output is not kept, and its record says `[OMITTED: input too large to check]`); a
  PostToolUse hook with 10 MB of tool data in any shape the tests
  and the reviewers tried (long keys, deep nesting, hundreds of thousands of strings) measured under 1 s, extraction
  reads at most 256 KB of a file, a session's event log is read as a stream of at most 128 MB per event, and the refresh a hook or an MCP call starts runs in a separate process that is
  killed at its time budget (8 s, 20 s for `get_context_pack`), so no file, however it is shaped, can make that
  refresh take longer; a file that was being extracted when the process was killed at the budget is not extracted again
  (plain text, then its name only; when the process ended only because its parent did, after a second time), and a map
  build that was killed is built reduced from then on. The map is capped in size (at most 32 MB, whatever a manifest
  or the files hold), and the pack after the refresh indexes session records in small writes until its deadline (4 s,
  5 s for `get_context_pack`). Each git command of a hook or an MCP call has a limit of 5 s (its listing of filter
  drivers included), after which no devmem process starts git in that project for a minute (a marker in devmem's
  per-user folder); on Linux git is killed when the process that started it ends. The question which files of the
  store git tracks has one 5-second limit for git and devmem's own work on its answer, and fixed caps (250,000 index
  entries that could be in the store, 4,096 folders to compare on disk, 64 MB of git's output, read in pieces and git
  stopped past it), past which the store is not used until you adopt it; devmem's own reading of the index file has the
  same limit, a 128 MB and 1,000,000-entry cap, and keeps its memory within a small multiple of the file whatever it holds (paths kept
  only for candidates, capped in count and bytes); a devmem process that ends while asking leaves a mark, so that the
  next hook does not ask again for that index for a minute (an hour when it happened again within half an hour; a
  command the user runs asks at once). git's list of changed files is not compared beyond 40,000
  entries or 32 MB (the session records that it was too large), and a file list from git of more than 200,000 names or 64 MB is
  not used (the folder is walked).
  `devmem begin`/`end` (also `wrap`, `codex` and the MCP `begin_session` / `end_session`) have fixed bounds too, named
  constants in `devmem/generic.py`: one begin or one end (its record included) opens at most 5,000 project files
  besides the ignore files and reads 128 MB of them, redacts at most 8,000,000 characters of their text and keeps or
  compares the text of at most 2,000 files; a begin writes a pending file of at most 32 MB (20,000 entries; further
  changed files by an 8-byte hash of the name, 100,000 of them); `git status` gets 30 s, and an end starts no `git
  show` after 30 s of them (each has 10 s); the line diff of one file may take 4,000,000 steps and those of one record
  16,000,000 (difflib's algorithm with its work counted and cut; past that, one Edit marked `not_line_diffed`; on the
  Windows test machine the slowest shape measured took 1.7 s for one file, a record of files at 16,000,000 steps 5.3 s,
  and an end whose five files reach that total 9 s in all); a file the begin did not see is recorded as created only
  when its modification time lies inside the session (at or after the begin, at most 2 s after the end), otherwise,
  earlier or in the future, it is listed as not compared. That time is the file's own, which the project, an
  archive or a tool sets: a file written with an old or a future time is listed as not compared, and a file that was
  there with a time set inside the session counts as created; a record keeps at most 2,000 edits with
  2,000,000 characters, a 4,000,000-character unified diff, 2,000,000 characters of tool events and a
  100,000-character prompt. Past a bound, the files involved are listed in the turn's `files_not_compared` (200 names,
  the rest counted) with the reason, never as written by the session, and the command or tool result says so; a work
  tree with more changed paths than git's list keeps is recorded as such, not snapshotted. The tests check these
  bounds with lowered limits and one case at full size (20,100 dirty files); redaction runs at about 0.2-0.5 MB/s on
  the Windows test machine and opening a freshly written file about 8 ms, so one begin or end at its bounds took 60-85
  s there (7,000 dirty files: begin 64 s, end 82 s). The other inputs on these paths are bounded the same way
  (constants in `capture.py`, `proc.py`, `generic.py`, `mcp_server.py`): `devmem import` reads at most the first and
  last 64 MB of a stream file, decoded as the encoding its first bytes show (a UTF-8, UTF-16 or UTF-32 byte order
  mark, the first line's `{` as one of them writes it, or UTF-16 without either, which Windows PowerShell 5.1's `>`
  writes; else UTF-8) and, from a line that begins with another byte order mark or another encoding's `{`, as that
  one (at most 1,000 changes; the command says the file's encoding could not be read reliably, and how many lines
  were taken apart and how many are stubs, when it changes or holds bytes invalid in it), and keeps 200,000 of its
  lines (the first and last 100,000; the cap applies while reading), 10,000 entries per digest list and a
  100,000-character prompt, and says what it left out. Each line is counted before it is parsed: a line that is not
  a JSON object, nests deeper than 64 levels, is longer than 4 MB (never parsed: json holds up to about 100 times a
  line of small values), holds more than 100,000 values or 2,000 distinct keys, comes after the lines taken apart
  have used 600,000 values or 20,000 distinct keys (`capture.STREAM_VALUE_BUDGET`, `STREAM_KEY_BUDGET`; an ordinary
  session reaches the text budget first: in the large real transcripts a review imported, up to 66.5 MB, and in a
  synthetic ordinary session of 200 MB, the text budget ended the import before either was used up), comes after the
  first 8,388,608 characters of text (parsed then only for the session's metrics, while 4,000,000 values last), or
  that devmem cannot take apart is a stub, never stored as text, counted by its reason in the turn's
  `stream_lines_left_out` and in what the command prints; a field of the wrong type never stops an import. So a large
  ordinary session is recorded up to its text budget, and the rest of it as counted stubs. The counting is one pass
  over the line, linear in its length whatever it holds (a string that never closes, as in a line cut while it was
  written, runs to the line's end in one match; `tests/test_import_growth.py` checks every pass over a line's raw
  text with inputs built from its own patterns, 0.25 to 4 MB). Of a line longer than 4 MB the first 64 KB are read,
  without building more than what lies there (a longer string within them is passed over, not decoded), for its
  event type, assistant texts and tool calls: a call found there is counted, is recorded with its path (a Write of a
  5 MB file: the file is listed as edited, its text is not) and is not looked at, so its result's text is not kept
  (`tests/test_import_bounds.py`). One decision per tool call serves the raw record and the digest alike, taken on the
  call's whole input (`redact.call_decision`; `tests/test_secret_call_decision.py` plants the name before
  and after padding around every cap): "secret" when it names a secret file, "unchecked" when the check cannot read
  it in full (a command over 24,096 characters, a path over 4,096, ...: the call is recorded as `input_not_checked`
  with the reason and the size, and nothing of its input or output is kept), else "ok"; the hooks and `codex` take the
  same decision and store the same forms (`tests/test_import_records.py`): the first 10,000 well-formed calls of assistant events, at most 1,000 of one
  line, are checked for secret files; any other call (past those caps, another kind of call block such as
  `server_tool_use`, malformed, outside an assistant event, on a line past the text budget, or found in the first 64
  KB of a line too long to parse) is counted in `tool_calls_left_out`; a call on another stub line is not counted
  one by one (its line is), and an id written on any stub line counts as a call devmem did not look at. Neither the
  input of a call not looked at nor the text of a result that is not matched to a call devmem checked (for any
  reason: no id, an id never seen) is stored anywhere, only a fixed marker; `tests/test_import_unknown_calls.py` and,
  in the shape of a Claude Code transcript, `tests/test_import_transcript.py` search the whole store for each reason.
  Every field of an event besides its message's content and short metadata (the stream-json `tool_use_result`, a
  transcript's `toolUseResult` and `wireToolInputs`, any field devmem does not know) is the call or its output again:
  on an event that carries calls or results it is kept only when every one of them was let through, else it is the
  fixed marker of the worst; on any other event (a transcript's attachments, file history, summaries, hook output) a
  field that refers to a call by its id takes that call's decision, one that holds a call or result block, or the id
  of no call devmem knows (also under a field named for tool calls, such as `toolInputCopies`), is a marker. A field
  that is kept, and the output of a call let through (raw record, digest errors, `codex` items, the hooks' tool
  responses), are kept without the secret files in them: every object in them that names a secret file under a path
  key (a file's multi-line text under an Edit result's `originalFile` and the like is not taken for a path:
  `tests/test_scrub_file_text.py`), or by a key shaped like a path, keeps
  its paths and nothing else, whatever the keys around it are called (a
  transcript's `bashEditDiff.files[].hunks` of `.env` after an ordinary `python scripts/setup.py`, a
  `structuredPatch`, a file attachment, a diagnostics map; `redact.scrub_secret_parts`), and lines of secret files
  (`grep` and `diff` output) are removed from every string. Text that quotes a secret file's content without naming
  it (the assistant's own text, a summary, hook output, an image's data) is only redacted. A digest entry for a file outside the
  project keeps no path (`path_omitted`; `./x`, `\\?\C:\...`, Git Bash's `/c/...` and WSL's `/mnt/c/...` are read
  as the paths they are first). Measured in Docker (Linux, Python 3.10, 2 CPUs, a 1.5 or 3 GB cap) with no other
  work in the container (under load on the host a review measured 1.3-1.9 times these times): 200 MB of 3-byte lines
  7.6 s, peak 170 MB; 120 MB of empty lines 4.5 s, 33 MB; 120 MB of lines nested 63 deep 10 s, 220 MB; one 126 MB
  line (a stub) 1.1 s, 262 MB; 100 MB of lines of 4,000-digit integers 15 s, 526 MB, with a raw record about as
  large as what was read (numbers are not cut); 10 lines of 1,000 calls and their failing results 18.5 s; with
  10,000 texts and 200 MB of short lines added 34 s, 217 MB; the dearest shapes at the line and stream bounds, 128 MB
  each: small objects (`{"a":"b"}`) in the output of ordinary calls 30 s, 171 MB; lines of 2,000 keys of 1 KB 25 s,
  201 MB; 8 MB of text and then lines of 100,000 values 20 s, 229 MB; lists nested 57 deep, lines of distinct keys,
  text, and lines too long to parse in turn 12 s, 187 MB; an ordinary session of 200 MB 24 s, 243 MB. These are the
  cost of import's own bounds plus redaction of the ordinary text these shapes carry; text written to be dear to the
  redaction rules costs more on top (the redaction review measured `devmem import` of 20 texts of 64 KB at 26-40 s
  on its worst shapes, so minutes at the 8 MB text budget: see "Redaction is applied where devmem writes to the store"
  in [docs/PRIVACY.md](docs/PRIVACY.md)), and no time bound is claimed for that. A file the stream's tool calls name that is absent when it is
  imported is listed in the turn's `files_absent_at_capture` (200 names, the rest counted), not recorded as deleted:
  absence after the session is no evidence. `devmem run` records the stream as the session ends, so there such a
  file is recorded as deleted ("observed on disk at capture"), never one whose name a shell would expand (`$HOME/x`,
  `%USERPROFILE%\x`, `~/x`: no evidence about any file) or that the system cannot look up. `run` writes the program's standard output to a temp
  file of at most 128 MB: the whole lines within its first and last 64 MB and one line between them saying how many
  bytes were left out, which the turn's `stream_left_out.not_written` records and `run` prints (a line cut at either
  edge is not kept; `devmem run` with a stand-in for `claude` printing 300 MB, in Docker as above: of 3-byte lines that
  are no events 7.8 s, peak 171 MB; of short assistant text events 21.8 s, 204 MB (39.2 s, 207 MB in a review with
  the host loaded by other test runs); of Read calls and their results 31.5 s, 255 MB). When the program ends, `run`,
  `wrap` and `codex` wait at most 2 s more for its output
  (`proc.EXIT_GRACE_S`, one deadline for standard output and standard error: 2.0 s measured with a process the
  program started holding both), not for a process it started that keeps the output open (the record says so,
  `tests/test_proc_grace.py`); at its timeout `run` kills the program's process tree (POSIX: the program's own
  session; Windows: `taskkill /T`), returns after that grace at most and keeps what it counted, and `wrap` and
  `codex` do the same and fail with the timeout. A process that left the tree survives that: on Linux one that
  started a session of its own (`setsid`), and on Windows any descendant when `taskkill` cannot be run; `run` then
  returns after the timeout and the grace (5.0 s for a 3 s timeout, measured both ways on an otherwise idle machine;
  `run_capped` with a 3 s timeout: 3.4 s idle, 3.9 s in a review with the host loaded). On Linux and macOS the
  program has no controlling terminal (it cannot open `/dev/tty`; its input is /dev/null and its output pipes;
  checked with a stand-in under a pseudo-terminal, not with `claude` itself), so Ctrl-C reaches
  devmem only: devmem sends SIGINT to the program's process group, then SIGTERM after 3 s, then kills it after 3 s
  more (`proc.INTERRUPT_GRACE_S`); on Windows the console sends the program its Ctrl-C, and devmem sees the Ctrl-C
  only when the program ends (waiting on it cannot be interrupted there): a program that ignores Ctrl-C runs on until
  it ends by itself (a review measured 29-30 s for a stand-in that did; devmem kills nothing then). A Ctrl-C while devmem waits for output that a process the program started still holds (the program has
  ended) counts as the first one too (`tests/test_proc_grace.py`). `run` then records what the program wrote until then, with `stream_left_out.interrupted` and a line saying
  so, and exits with status 130 (a second Ctrl-C while it records: nothing is recorded, and it says so; checked on
  Linux, `tests/test_import_records.py`; on Windows only the recording of an interrupted program's output is
  tested, not a real console Ctrl-C); `wrap` and `codex` record nothing when interrupted. `wrap` and `codex` hold at
  most the first and last 8,000,000 characters of a program's output, `run` 8,000,000 bytes of each end of its
  standard error; a `codex` tool event that names a secret file (the hooks' and `import`'s decision, on the whole
  event) keeps what it was, its paths, and of its command the program (one of a fixed list, else "other") and the
  secret file names it writes to or gives as file operands, nothing of its output or other text; `end` opens at most 16 begin files (128 MB) looking
  for one (a begin file nested too deep is skipped, and named when no other can be used); every MCP tool argument has a length or range (task, prompt and
  summary 100,000 characters, ids 64, budget 100-20,000) and a call past it is a tool error that names it, but an
  integer of more than 100 characters (a minus sign counts) anywhere in a request makes it a JSON-RPC parse error
  with id null, which the client cannot match to its request.
  `devmem scan` and the app are not
  bounded this way. Your own
  patterns in `.devmemory/redact` run only when you approved that exact file (see [docs/PRIVACY.md](docs/PRIVACY.md)), pass a lint that refuses
  a repeat or alternation inside an unbounded repeat, and are measured once in a separate process stopped after 2 s
  on test inputs built from their own characters (a slower one is not used); each match attempt then sees at most
  4 KB. The lint and the measurement cannot prove a pattern fast on every input, so a pattern slow only on other
  input can still cost time.
- **The local viewer** listens on 127.0.0.1 only and every data request needs a per-launch key. The key never
  appears in a URL: the address that opens the viewer carries a launch code that works once, within 60 seconds.
- **Recorded text is data.** Context packs label what was recorded; they are not instructions.

## In scope

Ways to make devmem record, serve or delete data without the user's action; ways for a project folder, a
recorded session or a web page to run code, read or write files outside the project and the store, or reach
the viewer; secrets in common formats that redaction misses; crashes or hangs in the hooks that stop a
coding session.

## Known limits

See "Current limits" in [docs/LIMITS.md](docs/LIMITS.md). They include Windows junctions and hard links, commands that a
repository's own `.git/config` can make git run, unusual characters in install paths, and a local program that
keeps the viewer busy with stalled connections. devmem notices files git tracks in your store; a record placed there
by any other means (an archive, a patch applied with `git apply`, `git reset` after a pull, which leaves the pulled
files untracked, another program) is taken as yours. If you pulled or applied something that touched `.devmemory/`,
look at `devmem sessions` and delete what you do not recognise (`devmem forget`). Untested: macOS spellings of the
store's name and git ending with devmem there, FAT and exFAT volumes, a split index. On a case-insensitive
file system (NTFS, macOS by default), two session ids that differ only in letter case share one session's live
files in `.devmemory/live/`, so their events are recorded as a single session.

## How it is checked

Every push runs the test suite on Windows and Linux, type checking and linting. The repository uses GitHub
code scanning, secret scanning and Dependabot. Windows installers are built in GitHub Actions with a build
provenance attestation (`gh attestation verify`).
