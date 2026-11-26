# What is captured

What DevMem records from a Claude Code session, and what it never records. How the recorded text is redacted is in [PRIVACY.md](PRIVACY.md).

Section names in quotes ("Install", "Privacy", "Optional embeddings", ...) are the headings of the pages in this folder; [README.md](README.md) lists which page holds each.

## What is captured, and what is not

Captured, when the hooks are installed:

- your prompts, word for word;
- the assistant's visible reply text, labelled `assistant-visible`;
- tool calls: which tool, which file, the command line, whether it failed, and for `Edit`, `MultiEdit` and `Write`
  the text written (each text capped at 4,000 characters and flagged when cut);
- files read, edited and deleted (deletions are observed on disk);
- the compaction summary, labelled as model-generated;
- model name and usage numbers, when Claude Code exposes them. These come from the transcript file Claude Code
  keeps, whose format is not a documented interface, so devmem reads it as best-effort and leaves fields empty when
  it cannot parse them. Each hook event reads a bounded part of it (about 200,000 characters of text; the next event
  continues, and SessionEnd, or finalising a session that ended without one, reads one more such part). Text left out (a very long reply at the end of a session, a
  single transcript line over 2 MB) is named in the record's `transcript_gaps`, which `devmem show` and the viewer
  display; each text is capped at 4,000 characters and flagged when cut.

Not captured:

- **Hidden reasoning.** Thinking blocks are never read or stored. devmem keeps only what a tool exposes: prompts,
  visible text, tool calls and results, and what changed on disk. Each stored text says who wrote it (the user, the
  assistant, a tool, or an observed diff); a field a tool did not expose stays empty.
- Anything from tools that are not hooked up (see "Other tools" for what those give you).
- The content of files that look like secrets (`.env`, `.envrc`, `*.env`, `.env-*`, `.env~`, `*.pem`, `*.key`,
  `*.ppk`, `id_rsa*`, `*secret*`, `*credentials*`, `*.tfstate`, `terraform.tfvars`, `*.auto.tfvars`, `.dockercfg`,
  `wp-config.php`, kubeconfig files and `~/.kube/config`, `~/.docker/config.json` and similar) is
  not scanned or indexed. One shared filter, used by the hooks, by `devmem run`/`import` stream capture and by
  `devmem codex`, decides what of a tool call is kept: a call that reads or writes such a file keeps only the fact
  that it happened, its paths, and of a command only the program (one of a fixed list of common programs, else
  "other") and the secret file names it writes to or gives as file operands (`echo NOTE=x >> .env` is stored as a
  marker naming `echo` and `.env`); none of its output and no other text of its input is kept. The filter reads the
  call's whole input. A call whose input it cannot read in full (a command longer than 24,096 characters, such as a
  long heredoc, or with a word longer than 4,096, a path longer than 4,096 characters, nesting deeper than 32, more
  than 20,000 values) is recorded as `input_not_checked` with the reason and the size, not as naming a secret file,
  and nothing of its input or its output is kept either (tests/test_secret_call_decision.py,
  tests/test_import_records.py). In the output of any other call, and in any other field of a session record,
  a part that names such a file next to other text (a structured diff, a patch, a file's content, an attachment) keeps
  its paths only (tests/test_import_transcript.py). That is any
  tool whose input names one, under any path-like key at any depth (`file_path`, `relative_path`, `pathInProject`,
  `filePath`, `args.path`, a `file://` URL, `glob`), or a command (`cat`/`type`/`Get-Content` anywhere in a pipeline,
  redirects, `git show REV:.env`, also spelled with quotes or backslashes a shell removes: `.e''nv`, `.\env`, with a
  brace expansion `.e{n,}v`, PowerShell's backtick `.e`nv` or cmd's caret `.e^nv`; also a command under the `input`,
  `args`, `argv`, `line` or `stdin` key of a terminal tool, there for a specific secret name only). A bare
  name under any other key counts only when it is a specific secret name (`.env`, `deploy.ppk`), so a search for
  `secrets.get` or text that mentions `.env` hides nothing. Lines or diff hunks of a secret file in `grep` output
  (including `-C` context lines), `git diff`, `git log -p`, `git show`, `diff -u` and `head`/`tail` of several files
  (`==> .env <==`) are replaced by a marker. Edit text and snapshot diffs (`devmem begin`/`end`, `wrap`, MCP) skip those files too; the
  trade-off is that a `begin`/`end` change that lies entirely inside a redacted value is listed as an edited file
  without the edit's text. Not covered: indirection (`cat $F`, `cat $(ls | grep env)`, `rg --type env`, a script that
  prints the file, `git cat-file` by hash, a name built from strings such as `gc '.e' + 'nv'`), config files with
  ordinary names (`database.yml`, `appsettings.Production.json`: only the value rules below apply), commands that print
  resolved values (`docker compose config`, `printenv`) beyond what the name rules catch, and a secret value with an
  innocent name in output that carries no file path. A secret in a file with an ordinary name is only
  caught by the redaction patterns below.
