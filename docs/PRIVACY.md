# Privacy

The full privacy section: what to rely on, what redaction is and what it does not catch, the store's git-ignore, the switches in `config.json`, and exactly what `devmem forget` deletes. Read the first paragraph and the "Forgetting" item at the end if you read nothing else.

Section names in quotes ("Install", "Privacy", "Optional embeddings", ...) are the headings of the pages in this folder; [README.md](README.md) lists which page holds each.

## Privacy

**What to rely on.** devmem keeps the secret formats listed below out of the store in the shapes listed below, and
drops the content of secret files whole. Redaction matches patterns; it does not understand meaning. So do not paste a
secret into a prompt in prose without a name next to it, keep secrets in files with secret-like names (`.env`, `*.pem`,
`*secret*`) so that they are never read, and look at a store before you share it. A config file with an ordinary name
(`database.yml`, `docker-compose.yml`, `appsettings.Production.json`) is read and recorded like any other text, so only
the value rules below protect it, and a value shaped like code is kept as code: on a `name: value` or `name = value`
line a call, a reference or an index, a type (type syntax, a built-in type name, a dotted name without digits), and,
among lines that look like code, a class-like word; a word of letters only is kept unless it looks like a credential.
In short: tests/test_redact_whole_grid.py checks 14 forms under a secret name (a YAML line, a YAML flow mapping,
`.env`, INI and `.properties` lines, a quoted JSON value, JSON, SSM and Kubernetes name/value pairs, an XML element
and `<add key="..." value="..."/>`, `Authorization: Token` and `X-API-Key` headers, a YAML block scalar) with values
of 20, 300 and 3,000 characters made of letters and digits with one of 24 punctuation characters in the middle of
the value (less those each format excludes): in that grid nothing of the value is left. Outside it redaction is best effort and part of an unusual
value can stay visible; known cases: a backslash ends a header, query or `name=value` value (a JSON escape), `&`
and `;` end a query value, `@` ends a URL password, a multi-line string longer than 4 KB that is no PEM or PGP block
is not matched at all, and the rules not in the grid end at a quote, the end of the line or their limit (256
characters to 4 KB). Not caught: a lowercase dictionary word (except on a config line under a password name, such
as `password: postgres`), and values shaped like a type name or code. In prose ("the db password is X") only a
value right after the name that looks like a credential is caught (letters with digits, a symbol inside, CamelCase,
and six or more digits under a password, secret or API-key name but not under a token name, because counts such as
`token limit: 200000` are everywhere in AI sessions): a sentence that names no secret, a value after other words, or
a lowercase word is not.

- Local only. With the default settings devmem makes no network requests; the one exception is the optional
  embeddings, whose model (roughly 0.24 GB) only `devmem embed --download` fetches from Hugging Face (see "Optional
  embeddings").
  The context pack is text added to your prompt, so it reaches your model provider as part of that prompt, like
  anything else you type; the memory itself stays in the folder.
- On Linux and macOS the store, devmem's per-user folder and the backups `connect` and `init` make are readable by
  your user only (folders 0700, files 0600, whatever your umask). A store made by an older devmem is narrowed the
  next time devmem writes to it, and `devmem doctor` warns while it is still open to other users. Windows keeps its
  per-user permissions.
- What redaction is: pattern matching, best effort. It is a second line of defence for a store that stays on your
  machine, not a guarantee and not a reason to paste secrets into a session. It replaces the credentials it
  recognises in the formats and tool views this section lists as handled; a format or view not listed as handled may
  keep a value, and the views known to keep one are listed below. It can also replace ordinary text that looks like a
  credential (the trade-offs are listed too).
- Redaction is applied where devmem writes to the store: every string of every event, session record, raw record
  line and hook state, whatever field it is in, is redacted on its way to disk (`devmem run`/`import` keep at most
  64 KB of each text of a stream and redact at most 8 MB of it; what is left out is replaced by a note. They are
  commands you run, not hooks, and have no time limit: a review measured about 9 s for 200,000 strings of one
  list; measured after round 11 on one Windows
  machine, alone: the dearest shapes reviews found (runs of options and quotes such as `--password<x"""--password!!str`,
  argument-list pairs, runs of XML tags) cost 0.3-0.45 s per 64 KB piece, so about a minute for 8 MB; a measurement,
  not a bound: what the tests check is that the cost grows linearly with the text, tests/test_redact_line_sweep.py and
  tests/test_redact_growth.py (CPU time: 512 KB costs at most ten times what 100 KB does); the readings of a text
  beyond the plain one (a diff's old and new sides, `diff -y` columns, a text with carriage returns inside its lines
  read again inside one of those, and every reading made inside those) share one budget per redacted text (a text
  with such returns read outside them is read twice as plain readings, so it costs about twice as much; in the
  review's measurement a Stop with 60 texts of 4 KB of the dearest shape measured (a nested combined diff), each
  starting with such a return, took
  7.3-7.9 s of the hook's own CPU time): they may hand the rules at most
  three times what the text's other readings handed them, plus 4,096 characters, and what the budget refuses is
  replaced whole, not only the lines it would have changed (tests/test_redact_budget_rest.py): every line of the
  refused hunk and of every hunk after it in the same text (the hunks of later commits of a `git log -p` included),
  and every row of the refused `diff -y` block and of the blocks after it in the same text, by `[REDACTED:diff]`;
  every line of a text whose second reading (of carriage returns inside lines) is refused, and a JSON string such a
  reading would have decoded unless it is one line that decodes to its own text, by `[REDACTED:budget]`. (Measured,
  not a test: on the review's ordinary corpora, cut at 2,000, 4,000, 100,000 and 512,000 characters, no call reached
  the budget. A merge's combined diff with several parents can: in the tests' shape, one line removed in each parent
  per hunk, 8 parents from about 2 KB, 5 from about 3 KB, 4 from about 10 KB, 3 at about 100 KB, 2 not up to 100 KB;
  tests/test_redact_budget_rest.py.)
  tests/test_redact_read_budget.py checks that bound on every pair of ten stacked decorations (unified diff, combined
  diffs of 2, 3 and 8 parents, word diff, `diff -y`, `grep -n` numbering, JSON strings, marked runs without a header,
  YAML list items) at 6 and 24 KB, and that a redaction costs at most 16 times one plain pass of the rules in CPU time on
  each decoration alone and on eight stacks of up to three; tests/test_redact_diff_edits.py checks that the side
  reading grows linearly on hostile diffs, word diffs, columns, unclosed quotes and runs of sibling keys). The hook
  error log (`hook_errors.log`, and its fallback in the temp folder) gets the built-in patterns and your approved
  ones. Common API-key and
  token formats, PEM and PGP private key blocks, `Authorization` and `X-API-Key`-style headers, passwords in URLs and
  values assigned to secret-sounding names, in the assignment forms listed here, are replaced by
  `[REDACTED:<kind>]`. A value is taken as its format writes
  it: a quoted value up to its matching quote (escapes and the other quote character included; also backtick
  strings without `${...}`, C# verbatim `@"..."`, one-line triple-quoted strings, `r"..."`/`b"..."` literals and Rust
  `r#"..."#`), a bare value on a
  config line (`.env`, INI, `.properties`, YAML, `printenv`, also after a YAML anchor or tag) up to the end of the line
  (a `# comment` and a trailing `,` or `;` stay outside it), a plain value of a YAML flow mapping item up to its `,`,
  `}` or `]`, a YAML block scalar to the end of its indentation whatever its header (an anchor and a tag in either
  order, `|` or `>`, an indentation digit and a chomping sign in either order: `&k !!binary |`, `|2-`, `>-2`;
  tests/test_redact_round16.py; tests/test_redact_round11.py checks 40 to 5,000 lines), each item of a sequence right
  under a secret name that is one scalar (`api_keys: [a, b]` within its brackets, also over lines within 4 KB, and
  `- a` lines at the name's indentation or deeper, 256 at most), judged as a flow-mapping value is (six characters or
  more, no code, reference, type or placeholder; `tokens:` is no secret name),
  a TOML or Python multi-line string when it closes within 4 KB (see the grid and the cases above for what is checked).
  Unless it is shaped like code: calls, indexing and
  references such as `get_password()`, `os.environ["K"]` or `${DB_PASSWORD}` are kept, and so is a type on a
  `name: value` line (type syntax such as `Option<String>`, `string[]` or `*string`, a dotted name without digits such
  as `rsa.RSAPrivateKey`, a built-in type name such as `string`, `bytes` or `SecretStr`, a class-like word). A word
  with digits other than those of a type name (`Passw0rd!`, `Winter560,`) is a value; a word of letters only under a
  password, secret, token or API-key name is one when it looks like one (`CorrectHorse`, a random mix of cases, twelve
  or more lowercase letters, and on config lines a lowercase word of six or more letters under a password name, such as
  `postgres`, unless it is a setting word such as `required`; on a `name = value` line only among other config lines, so
  a lone `password = postgres` is kept) and the lines around it are not code. Also a
  connection-string, query or form value up to its separator. A typed declaration
  of a literal is an assignment too (`password: str = "..."`, `const apiKey: string = "..."`, Rust `const PASSWORD:
  &str = "..."`, Kotlin/Swift/Scala `val password: String = "..."`, Go `var password string = "..."`), while typed
  fields, parameters and schemas without a literal stay as written (`password: string;`, `token: Option<String>,`,
  `secret: bytes,`, `password: z.string().min(8),`). Every rule also sees
  text in the shapes tools give it: each JSON string (a tool response written as JSON, such as Claude Code's Edit
  response with the whole file, JSON inside JSON up to eight levels, a string cut off at a size cap) is redacted as the
  plain text it holds, and so is every string of a tool's input or response before devmem writes it as JSON, where
  strings that sit next to each other (an Edit response's `structuredPatch` lines, a notebook's `source` lines,
  content blocks, a dictionary's string values) are redacted as one text, one per line, so a key block or a YAML block
  scalar given one line per string is seen whole, and cut back into the strings by position: no rule changes the
  number of lines (a key block's lines are emptied in place, a pattern of your own whose match crosses a line break
  leaves the line breaks it replaced, and the omitted lines of a secret file's diff are emptied in place), so only the
  strings a redaction touched change and no text moves from one string into another. (In one plain text, the lines of
  a secret file's diff hunk or `head` section become one `[OMITTED: ...]` line, so such a text gets shorter.) The store's write boundary does
  the same for structures that reach it unredacted (a run of strings it produced itself as one text, or read back from
  the store's own event log, is looked up, not redacted again). Lines decorated by tools are also read without the decoration: path and line-number prefixes, a run of
  numbered lines (`N:` or `N-`, with or without a path: `grep -n`, `grep -n -C`, `rg -n`, `findstr /n`; with `> ` or
  two blanks before it: PowerShell `Select-String -Context`; `git blame`'s `hash (author date N)`) whatever each line
  starts with, `cat -n` and the Read tool's numbers, diff `+`/`-` markers, `>` quotes, comment markers, a trailing
  `# comment`, terminal colours. A diff is also read as the old and the new text of each hunk (after an `@@` header,
  `@@@` for a merge, or a run of `+`/`-`/context lines without one, as in an Edit tool's `structuredPatch`; also under
  `grep -n`, `cat -n` and Read numbers, and `git log --graph` columns), so a value whose name is on an unchanged line
  is redacted on both sides; a hunk header whose counts are too short does not end the hunk before the marked lines
  that follow it; a hunk whose sides cannot be read back line by line has every line replaced by `[REDACTED:diff]`
  (after its markers), and so has one refused by the budget above, together with every hunk after it in the same
  text; one over the bounds (100,000 lines, 100,000 hunks) has its changed lines replaced. `git diff --word-diff` is read the same way from its `[-...-]` and `{+...+}` parts (a part
  open over lines included), and because git moves blanks and line breaks into those parts, a changed part on a line
  that a redaction changed, or on the line after one (also across a hunk header right after it), is replaced too (fail
  closed). `diff -y` is read as its two columns as they came, counted as a terminal counts them (the `\r` of a CRLF
  file's left column included), and the column reading only adds to what the plain reading replaced. A carriage return
  inside a line (a file with CR-only line ends, which git shows as one line) is read as a line end by a second reading
  on top of the first; outside a diff's sides both count as plain readings for the budget. What is checked: the first
  30 to 2,500 lines of the Python library's argparse.py with CR-only line ends (and with every fifth line CR-only), one
  line edited, in `git log -p` form at 4,000, 100,000 and 512,000 characters, come out unchanged
  (tests/test_redact_cr_only_files.py); a file with CR-only line ends that holds `[-` or `{+` does not always (see the
  trade-offs below). Key material whose BEGIN line is not
  in view (the body of a key file changed: a default `git diff`
  shows three lines of context, and an encrypted key's `Proc-Type:`, `DEK-Info:` and blank line take them) is replaced
  when it is a run of three to 512 base64 lines of the width a key file is wrapped at (64, OpenSSH 70; the last
  shorter), each with both cases and a digit, or base64 lines right above a visible END line of a private key; not
  under a visible BEGIN line (RFC 4716's `---- BEGIN SSH2 PUBLIC KEY ----` included) or above a visible END line of a
  certificate, a public key or a signature, not above a line that names X.509 (the closing `</ds:X509Certificate>` of
  SAML metadata), not in a text whose armour lines all name public material, and not under a line that says base64
  data follows (`Content-Transfer-Encoding: base64`, a `;base64,` data URI) (tests/test_redact_round16.py,
  tests/test_redact_public_material.py). Trade-offs, ordinary text this can replace: base64 cannot tell a
  key from other data, so a run of three to 512 base64 lines of width 64 or 70 with none of that in view is replaced
  whatever it encodes; `openssl base64` and `base64 -w 64` / `-w 70` output of a 3,000-byte file lose 63, 63 and 57
  lines that way (longer runs are kept: by calculation, no private key body is that long; an OpenPGP RSA-16384 secret
  key with one subkey comes to about 475 lines). A file with CR-only line ends that holds an unclosed `[-` or `{+`
  (Python slicing such as `items[-1]`, a regex class such as `[-\w.]`, an f-string `{+x}`) can, in `git log -p` or
  `diff -u`, lose every line from that point to the end of that file's diff (the rest of the old file and all of the
  new one; other files and commits stay) to `[REDACTED:diff]` when that file's diff also holds anything a rule
  replaces, before or after it: a secret, or ordinary text a rule takes for one (in smtplib.py the docstring line
  `password combination.`). Read with its returns as line ends, the file's lines stand bare in the one-line hunk git
  makes of such a file, which is then read as a `--word-diff` hunk whose `[-` part never closes. Measured on the
  Python library's modules written with CR-only line ends: in real `git log -p` output of 30 of them, smtplib.py lost
  465 lines at the 65,536-character cap and 1,577 at 100,000, none at 4,000, and the other 29 none; in the tests'
  `git log -p` form of all 152 top-level modules at 100,000 characters, inspect.py lost 765 lines, smtplib.py 718 and
  tokenize.py 508, and the others none (tests/test_redact_known_damage.py pins a small file of this kind). A file
  whose lines mix CR-only and LF ends can lose its lines from the first line a rule replaces to the end of its diff
  in the same way (measured, not a test: the tests' `git log -p` form, every fifth line CR-only, 100,000 characters:
  smtplib.py 137 lines, inspect.py 134, tokenize.py 22). `openssl x509 -text` output has some of its colon-separated
  hex lines (a key's modulus, a signature) read as `name: value` and partly replaced,
  `83:c3:50:5c:06:[REDACTED:assignment]` (measured on two certificates' dumps: 16 of 59 and 18 of 65 lines;
  tests/test_redact_known_damage.py pins an excerpt). A quoted value
  that runs over lines (`password: "abc` / `  def"`) and `NAME=value\` continued on the next lines are redacted whole (a
  line that starts like another setting, such as `DEBUG=0`, ends the continuation; tests/test_redact_round15.py); a
  quoted value over lines that reads as a label or a sentence once its lines are folded (more than eight blanks, or a
  word such as "your", "must", "least", "the") is kept, as it is on one line (tests/test_redact_round16.py).
  What the tests check:
  <!-- view-matrix (generated: tests/test_redact_tool_views.py) -->
  Views tested with real tool output, each passed through the hook for a tool call and the store searched
  (tests/test_redact_tool_views.py): with each value shape (single-line pair, block scalar, list-item block, PEM key, quoted value over lines), in a file with LF, CRLF, CR-only or mixed (CRLF and LF by turns) line ends, nothing of the value is kept in `git diff`, `git diff -U1`, `git diff -U10`, `git show`, `git log -p`, `git log -p --graph`, `git diff --word-diff`, `git blame`, `git grep -n`, `grep -n`, `grep -n -A3`, `grep -n -B3`, `grep -n -C3`, `grep -rn`, `rg -n`, `rg -n -C3`, `diff -u`, `diff -y`, `cat -n`, `nl -ba`, `Select-String -Context`, `findstr /n`; in `git diff -U0` nothing is kept either, except (with LF, CRLF and mixed line ends: git shows a file with CR-only line ends as one line, name included): `git diff -U0` with the PEM key (the BEGIN line is outside the hunk: the key material alone has no header); `git diff -U0` with the block scalar (the name is outside the hunk: the body alone has no name); `git diff -U0` with the list-item block (the name is outside the hunk: the body alone has no name). On each of these outputs what the plain reading replaced stays replaced (every other reading only adds to it), and a second redaction changes nothing.
  A tool that is not installed is skipped, so on a machine without ripgrep (looked for on PATH, then in DEVMEM_TEST_RG, then as Claude Code's bundled one) `rg -n`, `rg -n -C3` are not run, without PowerShell `Select-String -Context`, and off Windows `findstr /n`.
  A value edited inside a diff (tests/test_redact_diff_edits.py): with each value shape (line pair, block scalar, list-item block, PEM body, triple-quoted, quoted value over lines, JSON value on its own line, backslash continuation, TOML multi-line, XML element over lines, k8s name/value, 24-line block scalar, 20-line PEM body), in a file with LF, CRLF, CR-only or mixed (CRLF and LF by turns) line ends, neither the old nor the new value is kept in `git diff`, `git diff -U1`, `git diff -U10`, `git diff --color=always`, `git show`, `git log -p`, `git diff --word-diff`, `diff -u`, `diff -y`, `diff -y -W200`, `grep -n on a patch`, `cat -n on a patch`, `Read tool on a patch`, `Edit structuredPatch`, `structuredPatch as JSON text`, `git show --cc of a merge`, except, on Windows, `grep -n on a patch` of a file with CR-only line ends for the block scalar, list-item block, backslash continuation, 24-line block scalar (Git for Windows' grep drops the returns: the file is one line without line breaks), and `diff -y` and `diff -y -W200` of a file with CRLF or mixed line ends for the triple-quoted, backslash continuation, TOML multi-line (the first reading takes the left column's return for a line end, and the value over lines runs on into the right column), and `git diff --color=always` of the 20-line PEM body in a file with CR-only line ends (the BEGIN line is replaced alone by the reading of decorated lines; the body then has no header).
  Not tested: `git diff --word-diff=porcelain`, `git diff --color-words`, `diff -c`, `diff (normal format)`, `grep -o`, `rg --json`, `sed -n`, `head/tail`, `less/more`, `jq`, `a pager or a terminal that wraps lines`.
  <!-- /view-matrix -->
  Also tests/test_redact_shapes_matrix.py (23 shapes) and tests/test_redact_list_item_views.py (plain, `cat -n`, `> `,
  diff `+` and `-`, `grep -n`; numbered runs with `:` and `-`; block bodies starting like a marker), on views built by
  the tests. Known to keep a value: a hunk whose head is cut off when the value's name is above its visible lines:
  `git diff -U0`, a cap that keeps the tail, and with git's default three lines of context a block scalar or list-item
  block whose edited line is more than three lines below its name, or a name and its value cut into two hunks (the body
  alone has no name; key material without its BEGIN line is caught by the headless-body rule above, with the
  exceptions that rule lists and those that follow here); headless key material of fewer than three lines with no END
  line in view; a key body edited in its middle in a default `git diff`: the
  body line git prints after the hunk header as its function context (`@@ -10,7 +10,7 @@ MIIE...`), and, for a key
  in a YAML block scalar or list-item block, the edited lines themselves (each stands alone on its side of the hunk)
  (tests/test_redact_known_gaps.py); a private key's body without its BEGIN line in a text that also shows public
  armour and no private armour (a certificate added in the same `git show`, or in another commit of the same
  `git log -p`: 8 of 8 body lines of a real key edited in its middle kept), or under a `Content-Transfer-Encoding:
  base64` or `;base64,` line (tests/test_redact_known_gaps.py); a file whose line ends mix CR-only lines with LF or CRLF lines (55 of 240 views
  in the review's measurement keep a value: list-item blocks and PEM keys in `git diff` at any context, `git show`,
  `git log -p` and `diff -u`, block scalars, list-item blocks, PEM keys and quoted values over lines in `grep -n`
  (also `-C3`), `cat -n`, `nl -ba` and `diff -y`; tests/test_redact_known_gaps.py), and `diff -y` of a quoted value
  over lines in a file whose CRLF and LF line ends alternate from another line than in the tested file (2 of 4
  values kept in that measurement); `git log -p --graph` of a history with a merge (a commit drawn
  beside an empty graph column has the prefix `|   `, which is not read as graph columns;
  tests/test_redact_known_gaps.py); `diff -y` with `-t`, `--left-column` or `--suppress-common-lines` of a block
  scalar or a PEM key (tests/test_redact_known_gaps.py); `diff -y` of a file with CRLF or mixed line ends holding a
  triple-quoted string, a TOML multi-line string or a backslash continuation (the first reading takes the left
  column's return for a line end; tests/test_redact_diff_edits.py); in YAML, a quoted key (`"password": |`), a
  complex key (`? password`), a block scalar as a sequence item, and a block sequence with a comment line before its
  items (tests/test_redact_known_gaps.py); `diff` in its normal format (`2c2`, `< old`, `> new`: no name in
  view) and in the context format (`diff -c`); `git range-diff`, `diff -u` of two patch files and a diff of a patch
  file when the inner change has no hunk header of its own (a value whose name is on an inner unchanged line: inside
  a side, runs of marked lines without a header are not read again); with one line of context (`-U1`), a Terraform
  sensitive value or Key Vault value whose `"sensitive"` or `"id"` item is outside the hunk; `diff -y` of a YAML flow
  sequence and of a JSON string holding escaped line breaks; `git log -p --graph` of a
  backslash continuation; `Select-String -Context` and `git blame` of `.pgpass` lines, and `Select-String -Context` of
  a value after a YAML anchor (their prefixes are not read as decoration for those rules); a
  `git blame` line whose author holds a parenthesis (its prefix is not read); a `--word-diff` part more than one line
  after the last redacted or replaced line when git moved a value's indentation there; `git diff
  --word-diff=porcelain` (one word per line) and `--color-words`; a `grep -C`/`rg -C` window that cuts a triple-quoted
  or TOML multi-line string before its closing quotes (the string is not read as closed); `diff -y` at a width that
  cuts a column (`-W` narrower than the lines: a value cut in each column is not seen whole); a secret split across
  separate tool calls (a name in one, its value in the other). Where these readings decline on purpose (each tested
  both ways): a run of marked lines without a hunk header is read as a diff only when it holds a context line and a
  changed one (tests/test_redact_diff_edits.py, benign edits); `diff -y` columns only in two or more lines with a
  gutter mark at one column (a mark followed by the tab to the right column counts first); a value over lines only
  under a secret name that starts its line, with the line ending at the closing quote (tests/test_redact_round15.py);
  a Key Vault value only as an item of the object that holds the vault id, before or after it; a token whose prefix is
  short and also a piece of identifiers (`key-`, `SK`, `dapi`, a bare `sk-`, the vendor prefixes of fewer than four
  characters) not right after `_` (`cache_key-<md5>` is a name), while a distinctive one (`ghp_`, `glpat-`, `xoxb-`,
  `AKIA`, `sk-proj-`) is caught there too (tests/test_redact_round15.py); a sequence under `tokens:` (no secret name);
  base64 lines without a BEGIN line only as described above (hex digests, two lines, lines of other lengths, 76-wide
  MIME, letters only, and certificate, public key and signature bodies with their BEGIN or END line or other public
  armour in view or above a line that names X.509, base64 under a line that says it is data, and runs of more than 512 lines are kept:
  tests/test_redact_round16.py, tests/test_redact_public_material.py); a
  carriage return followed only by blanks to the end of its line is no line end. The tests run every secret of their corpora
  through 23 such shapes with no miss, and every secret value of those corpora under a secret name in YAML flow
  mappings and name/value pairs (six wrappers; the misses that remain are listed in the test). A JSON item is judged together with its key (`{"password": "..."}`) wherever it
  is stored. A redacted key block (PEM, PGP, PuTTY), YAML block scalar, TOML multi-line string or XML element whose
  value is on lines of its own keeps its line breaks (the lines are emptied), so line numbers stay; a list of lines
  keeps its length. Add
  your own Python regular expressions, one per line, to `.devmemory/redact`. Patterns cover common key and token
  formats (including Slack and Discord webhooks, SendGrid, PyPI, Google OAuth client secrets, Telegram bot tokens,
  Discord bot tokens, DigitalOcean, Twilio, Databricks, Shopify, Docker Hub and Linear tokens, age secret keys, Firebase
  FCM server keys, Sentry DSN keys, API keys and Azure SAS signatures in URL query strings, Stripe webhook
  secrets and Vault tokens), properties/INI-style keys, connection strings (including Azure `AccountKey=`), `.npmrc`
  auth tokens, the `auth` field of a docker `config.json`, netrc passwords, `curl -u`, `docker login -p`, and
  `--password value`-style arguments. Values are also judged by the name they are stored under, in every shape the
  tests cover: XML elements and `key="..." value="..."` pairs, name/value pairs in JSON and YAML, URL query
  parameters, headers whose name ends in a secret word, cookies, long flags (`--http-password`), `aws configure set`
  and `git config`, credentials passed to a call (`auth=("u", "p")`), YAML block scalars, TOML multi-line strings,
  Terraform variable defaults, Dockerfile `ENV`; there is a table of about 50 vendor token prefixes and one of
  commands whose short flag takes a password (`redis-cli -a`, `sshpass -p`, `sqlcmd -P`, `keytool -storepass`,
  `openssl pass:`). Also SQL password clauses (`PASSWORD '...'`, `IDENTIFIED BY`), setters (`setPassword("...")`),
  options ending in a secret word (`-Password X`, `--passphrase X`), `--set a.b.password=` and `-Dname=value`,
  header directives in nginx, Apache and HAProxy configs, a password piped into `sudo -S` or `--password-stdin`,
  `.pgpass` lines, the word for password in other languages, and a secret named in a sentence ("the db password is
  X", when X looks like a credential: letters with digits, including lower case such as `summer2024` (not a technical
  word such as `sha256`, `python3` or `oauth2`), six or more digits under a password, secret or API-key name (not a
  token name: counts), a symbol inside, or CamelCase right after the name;
  not a lowercase word of letters only), the flag and value items of an argument list (`args: ["--password", "X"]`,
  `command: ["redis-server", "--requirepass", "X"]`, `["redis-cli", "-a", "X"]`, and block lists). Also typed forms (TypeScript `apiKey?: string = "..."`, C `char password[] = "..."`, C# property
  initializers, Rust `b"..."`, Swift `#"..."#`), defaults in placeholders (`${db.password:X}`, `${DB_PASSWORD:-X}`),
  a literal default after an environment lookup (`process.env.API_KEY || "..."`, `os.getenv("API_KEY", "...")`), Go
  `%+v` output and PHP `print_r`. Stripe publishable keys (`pk_live_`) are public and kept; a quoted value with spaces
  is redacted under a password, passphrase or secret name unless it reads as a label (`"Enter your password"`).
  Two-column name/value tables (PowerShell `Get-ChildItem env:`) are read by row (a type such as Go's `[]byte` or an
  enum constant stays). Round 15 (tests/test_redact_round15.py): a Terraform value marked `"sensitive": true`
  (`terraform output -json`, a state's `outputs`) is replaced whatever its name, as text (a string, a number, or a list
  or map of one level) and as a parsed structure (any value); an `az keyvault secret show` value (the object whose `id`
  is `https://<vault>.vault.azure.net/secrets/...`) is replaced (both checked on JSON written by hand in the shape
  those commands print; not tested with the real `terraform` or `az` tools); a token glued after `_` (`notes_ghp_...`) is caught,
  for each of the 22 token rules and every entry of the vendor table; and up to six other items of any key but `name`, `key` and `value` may stand
  between a name/value pair's name and its value. This is pattern matching, a safety net and not
  a guarantee: on a corpus of 69 secrets in formats the tests did not list, written after the rules of that round, 14
  were missed (`devmem/redact.py` lists the known gaps). Known gaps include: a secret with no recognisable shape and no
  secret-sounding name next to it; a value split over two lines by a shell continuation; URL-encoded or base64-encoded
  text; a URL password holding a raw `@` or `/`; a bare value with blanks inside (`password = my long pass`, a
  connection-string password with a blank); a single-quoted shell value ending in a backslash; CSV doubled quotes and
  HTML entities (`&quot;`, `&#x27;`) around a value; zero-width or no-break spaces inside a name; Python `repr` text
  where a literal `\n` sits right before `-p...` or `api_key:`; a secret split across separate tool calls or across
  the 64 KB pieces of a stream text; and patterns of your own `redact` file only see 4 KB windows. Also open after
  round 9: a literal passed to a call (`SecretStr("...")`, `String::from("...")`, `strcpy(cfg.password, "...")`: code
  by shape), Makefile `NAME := value` and fish `set -x NAME value`. Open after round 10: a value shaped like a type or
  a schema (type syntax, `z.string().min(8)`, a dotted name without digits such as `Admin.Pass`, a built-in type name);
  a value of letters only that reads as a name (CamelCase ending in a type word such as `WinterSecret`, a capitalised
  word of fewer than eight letters, letters with `_` or `-`, all capitals, a short lowercase word under a token,
  secret or API-key name, setting words, any class-like word among lines of code, every letters-only value under other
  names such as `key`); in a sentence, a value of letters only after other words or in lower case; a value of fewer
  than six characters; a name of two letters (`pw`); an API-key header value after a backslash or quote inside it; a
  value starting with `{` in a YAML block scalar, a
  PHP `define`, an ADO string or a query/JDBC/form value (`%7B...`); a value that starts like a path under a token or
  key name, or with a separator inside (`API_TOKEN=/Xk4.../...`); shell quoting `'"'"'` inside a value
  (`shlex.quote`); a value that starts like a reference (`$ecret...`, `@dmin...`, `!Pass...`); a URL password holding
  `"` or `'`; tables whose header row names the column (psql and mysql result rows, `kubectl` custom columns `NAME
  PASSWORD`, a three-column `Format-Table`, the aws CLI's `| Value | ... |` and its `--output text` rows); and a key block cut off inside a diff
  empties one short `+added` line after it. Open after round 11: a YAML flow value with blanks inside (`{password: my
  long pass}`) or a quoted one with blanks under a name other than password, passphrase or secret; a name and a value
  in the opposite order (`{value: X, name: DB_PASSWORD}`); a short flag in a block list (`- -a` / `- X`), or in a list
  whose first item is not the command (`["sh", "-c", ...]`); values longer than a rule's limit in the rules other than
  the config-line, quoted, flow-map, prose, environment, name/value-pair and XML rules (SQL clauses, setters, `define`, table rows: 256
  to 4,096 characters, then not matched); a multi-line JS template literal, C# `@"..."` or Swift `#"""..."""#`; a
  one-line triple-quoted Python literal with a prefix that is followed by more of an expression (`r"""X""" + y`:
  kept; alone on its line it is redacted, and its prefix and quotes go with it); Swift
  `ProcessInfo.processInfo.environment["X"] ?? "..."`; open after round 12: a value that starts with `$`, `!` or `@`,
  starts with several `/`, or ends in `(` (taken for a reference, a path or a call); a quoted name/value pair value
  longer than 8 KB (not matched); a YAML single-quoted value holding `''` read by the shell-style quoted rule; an XML
  element whose one-line value holds a blank under a name that is not a password, passphrase or secret name
  (`<apiKey>A B</apiKey>`: kept on purpose); names ending in a label word (`_help`, `_usage`, `_description`, `_doc`,
  `_message`, `_title`, `_example`, `_hint`, `_prompt`, ...: kept on purpose); a sensitive Terraform output written
  as text whose value or type nests a list or map inside a list or map (a parsed one is replaced whatever its shape;
  tests/test_redact_round15.py, an expected failure); a pair whose name stands more than six items before its value,
  whose name line ends in a comment (`- name: DB_PASSWORD # db`), or with an item between them whose unquoted value
  holds blanks (`Description: primary db password`);
  `api_key: str = """..."""`,
  `f"""`/`r"""` strings and `{"api_key": """..."""}`; a triple-quoted string whose body is clearly prose (three or
  more lines of four or more plain words: kept on purpose); a name/value pair whose name does not end in a secret word
  (`password_reset`) or whose value is a short lowercase word or a sentence (kept on purpose: event logs); header
  values end at a blank, a quote, `<` or (`Authorization:`) a `,`. Accepted cost: any text written as a YAML block
  scalar under a secret name (`password: >` followed by a description) is redacted, and code in a `password = """`
  string within 4 KB too; a lone `password = postgres` line with no config lines around it;
  a two-column table row indented more than eight spaces; a parenthesised value in prose ("The API key (X) stopped
  working") and non-ASCII values in prose and flow maps (`Passwört!`, `пароль123`); a word of letters only after a
  flag and a blank (`--requirepass AustinCowboys`); an XML CDATA value holding `<`; a Kubernetes `tls.key: |` block
  (`key` alone is no secret name). Redaction also
  over-omits a little: a command that mentions `.env` (`git commit -m "ignore .env"`, `echo '.env' >> .gitignore`)
  or reads `.env.example` or a file named like `secret_santa.py` loses its output, since such files are never stored,
  and every line after a secret file's diff hunk up to the next file header is dropped with it.
  Exceptions: redaction can be switched off
  with `"redact": false` in `.devmemory/config.json`, but only when you also allowed that outside the project
  (`devmem init --allow-unredacted` or `devmem setup --allow-unredacted`, kept with your switch; running either command
  again without the flag, `devmem uninstall` or `/devmem:setup off` takes it back). A config file can arrive with a `git pull` (git keeps tracking a file in an
  ignored folder) or in a zip, so on its own it never weakens redaction; `devmem doctor` and `devmem status` say when
  it asks for that, and doctor lists every file in the store that git tracks. Redaction does not
  touch the project map and index (`PROJECT.md`, `index/`), which are built from your project files (files with
  secret-like names are never read), but every project-file line a context pack quotes is redacted; and a secret in a
  prompt or file that matches no pattern is stored as written.
- `.devmemory/.gitignore` ignores the whole store, and `devmem status` and `devmem doctor` tell you whether git
  would pick it up. The scan skips files git ignores, a built-in list of build and cache folders, `.claude/`, and
  secret-looking files; add patterns to `.devmemory/ignore`.
- Switches in `.devmemory/config.json`: `"capture": false` records no prompts, tool calls or session records (the setting
  applies to the hooks; devmem itself sets `DEVMEM_NO_CAPTURE` for sessions it launches and records itself). The
  pack is still delivered unless `deliver` is also false, and for that devmem keeps a small per-session state
  file, `.devmemory/live/<session>.state.json`, with which context was already sent (no prompt or tool text), `"deliver": false` stops adding the
  pack to prompts, `"pack_budget"` sets the token budget (default 1500), `"redact": false` turns redaction off (with
  your permission only, above), and
  `"mcp_call_log": true` makes the MCP server keep a log of the calls it receives in `.devmemory/mcp_calls.jsonl`
  (off by default; written only when the value is true, so `"false"` and `"off"` keep it off; the arguments are
  redacted before they are written, one call's line is cut at 8,000 characters, and every `devmem forget` deletes the
  file, see "Forgetting"). What the other store files and
  keys can do if a repository supplies them: `capture: false`, `deliver: false`, `read_transcript: false`, an `ignore`
  file and extra patterns in a `redact` file only record, read or send less; `pack_budget` changes how much earlier
  work is added to a prompt (capped at Claude Code's 10,000-character hook output), not what is stored;
  `mcp_call_log` adds a local, redacted log inside the store. `embeddings` would mean a model download and a cache
  folder the config names, so a `config.json` that git tracks never turns them on (`DEVMEM_EMBED`, set by you, still
  does). The patterns in a `redact` file run only when it is the file you approved: `devmem init` and /devmem:setup
  record its SHA-256 (or that there is none) in the switch outside the project. A `redact` file that arrives or
  changes later, for example with a `git pull`, is not used until you look at it and run `devmem init` (or
  /devmem:setup) again, which lists the patterns it approves; `devmem doctor` says when that is the case, and so it does for a switch written by an older
  devmem, which approves no `redact` file. Approved patterns are checked when they are loaded: a pattern with a
  repeat or an alternation inside an unbounded repeat (`(a+)+`, `(a|aa)*`), one longer than 500 characters, and
  anything past the first 100 patterns are refused and listed by `doctor`. Each approved pattern is also measured once,
  when you approve the file, in a separate process stopped after 2 seconds, on test inputs made from its own
  characters (the result is kept in devmem's per-user folder); a slower one is not used and `doctor` lists it. When it
  runs, every match attempt sees at most 4 KB of text. A pattern that is slow only on input unlike those tests can
  still cost time; that remains your responsibility.
- Forgetting: `devmem forget <id-prefix>`, `devmem forget --before 2026-10-01`, or `devmem forget --all`. The last two
  ask for confirmation, or need `--yes` when stdin is not a terminal. All three delete the chosen sessions' records,
  raw records, live logs and their state files (also a record that no longer loads: a torn `.meta.json`, a raw record
  left without one, a state file without a log; files are matched by the session id in their names), and print every
  file deleted. All three also delete the MCP call log, if you switched it on: its lines carry no session id, so they
  cannot be matched to a session. `--all` also deletes every pending `begin` snapshot.
  Deleted sessions are removed from the search index, and the command then scrubs the index: `secure_delete` is on
  for every index connection, the first forget on an index written by an older version vacuums it once, and a
  truncating checkpoint clears the write-ahead log; the vector cache is vacuumed too. The scrub waits at most a few
  seconds for another program that has the index open (an SQLite browser, for example); if it cannot finish,
  `forget` says so and the text may remain in the index files until you run it again or delete the `index/` folder.
  Outside `forget`, when a prompt re-indexes a changed session, the old turn documents are deleted with
  `secure_delete` on but with no blocking checkpoint and no vacuum, so for a short time text from the old version
  can still sit in the `-wal` file; it goes at the next checkpoint or when the last connection closes. That is not a
  user-requested forget, and the alternative would be a prompt that can wait seconds on another program. It does
  not touch the project map, `config.json`, `ignore`, `redact`, or the hook entries
  in `.claude/` (`devmem uninstall` removes those).
