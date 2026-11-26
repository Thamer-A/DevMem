"""Redaction: strip secrets from captured text before anything reaches disk.

What is caught (each replaced by ``[REDACTED:<kind>]``):
  - well-known token formats: Anthropic/OpenAI ``sk-...``, GitHub ``ghp_``/``github_pat_``, GitLab ``glpat-``/
    ``glptt-``/``gldt-`` and similar, Mailgun ``key-<32 hex>``, Slack ``xox?-``,
    AWS access key ids, Google ``AIza...`` keys, Stripe ``sk_live_``/``rk_test_``/``whsec_``, JWTs, Slack ``xapp-``
    and webhook URLs, SendGrid ``SG.x.y``, PyPI ``pypi-AgE...``, Google OAuth ``GOCSPX-``, Telegram bot tokens,
    Vault ``hvs.``, Discord webhooks and bot tokens, DigitalOcean ``dop_v1_``, Twilio ``SK...``, Databricks ``dapi...``,
    Shopify ``shpat_``, Docker Hub ``dckr_pat_``, Linear ``lin_api_``, age ``AGE-SECRET-KEY-1...``, Firebase FCM server
    keys (``AAAA...:APA91b...``), the key part of a Sentry-style DSN (``https://<32 hex>@host``), query parameters
    ``sig=``/``appid=``/``api_key=`` (value looks generated), URL passwords with an empty user (``redis://:pw@host``)
  - PEM and OpenPGP private key blocks: the header, the lines of key material and the END line (also with `\n`
    escapes, blanks or a diff marker between them, or wrapped in string literals); a block cut off by truncation up
    to its last line of key material; a header mentioned without key material after it is left alone. The block's
    line breaks are kept (its lines are emptied), as are those of PuTTY ``Private-Lines``, YAML block scalars, TOML
    multi-line strings and XML elements whose value is on lines of its own: no built-in rule changes a text's
    line count
  - ``Authorization: Bearer <token>`` / ``Basic <b64>`` headers and ``bearer <token>`` strings; ``Authorization:
    Token <value>``, ``X-API-Key: <value>`` and similar headers when the value looks generated (also inside
    ``curl -H '...'``)
  - Azure ``AccountKey=``/``SharedAccessKey=``/``SharedAccessSignature=`` in connection strings, ``.npmrc``
    ``:_authToken=``/``:_password=``/``_auth=``, and the base64 ``"auth"`` field of a docker ``config.json`` (only
    when it decodes to ``user:password``)
  - every rule also inside JSON: each JSON string literal of 8+ characters is decoded and redacted as the plain text
    it is, up to eight levels of JSON inside JSON, also a literal cut off at the end of the text (_json_strings);
    string values with only JSON punctuation and keys between them (the items of an array of lines, a dictionary's
    values, the texts of content blocks) as one text, one value per line; quoted assignments escaped in JSON are
    matched directly too (``\\"api_key\\": \\"...\\"``)
  - every rule also on lines as tools print them (_line_view): grep ``path:N:``/``path-N-``/``path:``, ``cat -n``,
    the Read tool's ``N→``, ``N:``/``N |``/``N.`` numbering, diff ``+``/``-``/``!``/``<`` markers, quote markers
    (``>``), comment markers (``#``, ``//``, ``/*``, ``*``, ``--``, ``;``, ``%``, ``REM``, ``::``, ``<!--``), a
    trailing `` # ...`` or `` // ...`` comment and terminal escape sequences (``git diff --color``, ``grep
    --color=always``, ``jq -C``: _ANSI) (tests/test_redact_shapes_matrix.py: every secret of the corpora that plain
    text catches is caught in 23 shapes, including lists of lines, patch lines, notebook sources and content blocks
    as JSON text and as structures, and three coloured shapes; and every secret value of the corpora under a secret
    name in six YAML flow-map and name/value wrappers); also ``git blame``'s ``hash (author date N)`` and PowerShell
    ``Select-String -Context``'s ``> path:N:`` prefixes (_NUM_PREFIX)
  - a diff read as the old and the new text of each hunk (Redactor._diff_sides: after an ``@@`` header, ``@@@`` for a
    merge, or a run of ``+``/``-``/context lines without one, also under ``grep -n``/``cat -n``/Read numbers; ``git diff
    --word-diff`` from its ``[-...-]``/``{+...+}`` parts, a part open over lines included, _word_sides; ``git log
    --graph`` columns read past, _graph_runs; a header whose counts are too short read on over the marked lines after
    it), each side redacted and mapped back line by line; a hunk whose sides cannot be mapped has every line replaced
    by DIFF_FAILED, and so does one refused by the call's budget (READ_FACTOR) together with every hunk after it in
    the same text (later commits of a ``git log -p`` included); one over MAX_DIFF_HUNK_LINES / after MAX_DIFF_HUNKS has its changed lines replaced; in a word diff a changed part on a line a redaction changed, or within
    WORD_DIFF_REACH lines after one (also over a hunk header right after it), is replaced; ``diff -y`` read as its two
    columns (Redactor._columns: columns counted as a terminal does, a CRLF file's ``\\r`` included; only adds to the
    plain reading); a carriage return inside a line read as a line end by a second reading on top (Redactor._read;
    a file with CR-only line ends that holds ``[-`` or ``{+`` can lose its lines from there to the end of that
    file's diff to DIFF_FAILED when that file's diff also holds anything a rule replaces (a secret, or ordinary text
    taken for one): its lines, bare in git's one-line hunk, are read as a word diff whose ``[-`` part never closes;
    tests/test_redact_known_damage.py).
    What is checked: tests/test_redact_diff_edits.py (13 value shapes edited inside 14 real diff views, with old and
    new value; benign edits of this repository's files change nothing; growth on hostile diffs),
    tests/test_redact_tool_views.py (real tool views of five shapes with LF, CRLF, CR-only and mixed (CRLF and LF by
    turns) line ends through
    the hook; on each, what the plain reading replaced stays replaced and a second redaction changes nothing; docs/PRIVACY.md
    lists the views, those known to keep a value and those not tested), tests/test_redact_read_budget.py (the budget)
    and tests/test_redact_round16.py
  - key material without its BEGIN line in view (a default ``git diff`` of a key file whose body changed: the header,
    ``Proc-Type:``/``DEK-Info:`` and a blank line take the three lines of context): KEY_RUN_LINES or more lines of
    base64 of one width a key file is wrapped at (64: PEM, OpenPGP armor, PuTTY; 70: OpenSSH), the last possibly
    shorter, each full line holding both cases and a digit, or base64 lines right above a visible END line of a
    private key; not under a visible BEGIN line (RFC 4716's ``---- BEGIN`` included) or above a visible END line of
    a certificate, public key or signature, not above a line naming X.509, not in a text whose armour lines are all public,
    not under a line that says base64 data follows (``Content-Transfer-Encoding: base64``, ``;base64,``), and not a
    run of more than KEY_RUN_MAX lines (_key_run_repl; tests/test_redact_public_material.py, test_redact_round16.py)
  - a value that runs over lines: a quoted value not closed on its line when its name starts the line (to its quote
    within 4 KB, the line ending there), ``NAME=value\\`` continued on the next lines (64 lines of 4 KB; a line that
    starts like another setting ends it) (_spanning_repl)
  - a value as its format writes it: a quoted value to its matching quote with that format's escapes (backslash,
    ``''``, the shell's ``'\\''``; the other quote is content; a whole line whose quoted value holds its own quote
    unescaped to the last quote), a bare value on a config line (``.env``, INI, ``.properties``, YAML, ``printenv``,
    compose ``- X=``) to the end of the line minus a `` #`` / `` ;`` comment (a trailing ``,``/``;`` of a flow map or
    a log stays outside it), except code, references and types, kept by their shape (_code_value: calls, indexing,
    ``${...}``; on a ``name: value`` line also a type, _type_kept: type syntax, a dotted name without digits, a built-in
    type name, a class-like word; a word of letters only under a password, secret, token or API-key name is a value
    when it looks like one and the lines around it are no code: _letters_value, _line_context), a connection-string
    value to ``;`` (quoted with ``"``/``'``), a query or form value to ``&`` (a ``#`` is part of a form value), a
    command-line flag's value as a shell word
  - subscript assignments of a quoted literal (``os.environ["X_API_KEY"] = "..."``, ``config['password'] = '...'``)
  - credentials inside URLs (``scheme://user:password@host``)
  - assignments of a quoted literal to a secret-sounding name (``api_key = "..."``, ``"password": "..."``),
    dotenv/shell style ``UPPER_SECRET_NAME=value`` (also inside a string, the value ending at its closing quote:
    ``Client("API_KEY=...")``, ``{"v": "API_KEY=..."}``, ``curl -H "API_KEY=..."``, the same escaped in JSON), the
    same with a lowercase snake_case name when the value looks generated (letters and digits, 8+ characters:
    ``"db_password=..."``, ``docker run -e password=...``) and YAML-ish ``secret_name: value``; short forms count
    as the name's last part (``DB_PASS``, ``PGPWD``, ``mysql_pwd``; env-style ``STRIPE_KEY`` and ``AZURE_DEVOPS_PAT``
    with a generated-looking value)
  - whole ``name = value`` lines in any case: .properties (``spring.datasource.password=...``), INI
    (``aws_secret_access_key = ...``), lowercase dotenv (``db_password=...``); code on such a line
    (calls, attribute paths, plain identifiers) is left alone
  - ``Password=...;`` in a connection string (only on a line that also has Server=/Host=/User=/Database=...)
  - command-line passwords: mysql/mariadb ``-p<password>``, ``--password=...``, ``--token <value>`` (with a
    space only when the value looks generated), ``curl -u user:password``, ``docker login -p <pw>`` /
    ``--password <pw>`` (also podman); netrc ``machine <host> login <user> password <pw>``
  - the secret-ness of a name is decided by its last dotted segment: ``config.db.password`` is one,
    ``token.markup`` and ``Token.String`` are not; hex colours are never values
  - values judged by the name they are stored under (_judge, the same name test): XML elements
    (``<password>...</password>``) and list-style tags named by a key/name attribute, ``key="..." value="..."``
    and JSON/YAML name/value pairs (Kubernetes env), URL query parameters (``?password=``, ``X-Amz-Security-Token``,
    ``X-Goog-Signature``), any header whose name ends in a secret word (``Ocp-Apim-Subscription-Key``,
    ``DD-API-KEY``), cookies (``Cookie:``, ``Set-Cookie:``, ``curl -b``), long flags ending in a secret word
    (``--http-password``), ``aws configure set`` / ``git config`` / ``npm config set`` of a secret setting,
    credentials passed to a call (``auth=("u", "p")``, ``NetworkCredential``, ``server.login``), quoted name/value
    argument pairs (PHP ``define``, ``setdefault``), Terraform variable defaults, YAML block scalars, TOML
    multi-line strings, a passphrase with spaces under a hard secret name (not one that reads as a label), Dockerfile
    ``ENV NAME value``, dotted .properties names with a space, Markdown table rows, ``*_AUTH=<base64 user:pass>``,
    ODBC ``PWD={...}``; salts count as secrets (``SIGNING_SALT``, ``AUTH_SALT``)
  - about 50 vendor token formats from one prefix table (_VENDOR_TOKENS: Groq, xAI, LangSmith, Notion, Replicate,
    Sentry, Grafana, New Relic, Postman, Doppler, Atlassian, Google OAuth access tokens, Mailchimp, Figma, Square,
    ...), Terraform Cloud tokens, base64 of a PEM private key (kubeconfig ``client-key-data``), PuTTY
    ``Private-Lines``, PEM headers in any case with four or five dashes
  - short flags that take a password, from one command table (_CMD_FLAGS: ``redis-cli -a``, ``sshpass -p``,
    ``sqlcmd -P``, ``az -p``, ``keytool -storepass``, ``ldapsearch -w``, ``mongosh -p``, ``zip -P``, ``gh secret set
    --body``, ``htpasswd -b``), ``openssl ... pass:<pw>``, ``vault login <token>``, ``smbclient -U user%pw``,
    ``sqlplus user/pw@db``, PowerShell ``ConvertTo-SecureString "..."``; also ``mysqladmin password``, ``rabbitmqctl
    add_user``, ``steamcmd +login``, ``net use``, ``cqlsh``/``cypher-shell``/``hdbsql -p``, ``mosquitto_pub -P``,
    ``snmp* -A/-X``, ``ssh-keygen -N/-P``, ``7z``/``rar -p<pw>``, ``lftp -u user,pw``, ``htpasswd -b`` with options in
    any order and ``-nb``
  - round 7 (each judged by a name or a command list): a quoted value after a secret name and a blank (SQL
    ``PASSWORD '...'``, ``SET PASSWORD = PASSWORD('...')``, ``IDENTIFIED [WITH plugin] BY '...'`` and Oracle's bare
    form, a Groovy ``storePassword '...'``, ``PSK "..."``); setters and builders (``setPassword("...")``,
    ``.withSecret("...")``, not getters); options whose name ends in a secret word, single dash or dotted
    (``-Password X``, ``--passphrase X``, ``--spring.datasource.password=X``), Windows ``/pass:X``, ``/rp X``, ``-u
    user:pw`` of any command (generated-looking values); ``name=value`` options with camelCase or dotted names (``--set
    auth.postgresPassword=``, ``-Dgpg.passphrase=``, ``-o username=u,password=X``); header-setting directives (nginx,
    Apache, HAProxy); a password piped into a command that reads stdin (``echo pw | sudo -S``, ``--password-stdin``,
    ``chpasswd``, ``passwd --stdin``, ``docker secret create``); key/value commands (``consul kv put .../password X``,
    ``pulumi config set --secret``, ``--name <secret> ... --value X``); the word for password in other languages (names,
    config lines, prose); prose ("the db password is X", "Here is the API key: X", only a value that looks like a
    credential); config lines ``password <value>`` (msmtp, ``requirepass``); .pgpass and postfix ``sasl_passwd`` lines;
    Redis ``ACL SETUSER ... >pw``; Go DSN ``user:pw@tcp(``; Teams webhooks; Roblox's ``.ROBLOSECURITY`` cookie; a
    session cookie in a name/value pair (HAR); ``psk`` and ``passcode`` as names
  - round 8: two-column name/value tables (PowerShell ``Get-ChildItem env:``), ``terraform plan`` ``-> "new"``, a
    number under a secret name in JSON, a percent-encoded bearer token, an API-key header of letters only (24+,
    both cases), pipe-separated env pairs
  - round 9: typed declarations of a literal (_TYPED_ASSIGN: ``password: str = "..."``, ``const apiKey: string =
    "..."``, Rust ``static API_KEY: &'static str = "..."``, ``val password: String = "..."``, Go ``var password string =
    "..."``); the other string forms of a quoted value (backticks without ``${``, C# ``@"..."``, one-line triple quotes,
    ``r``/``b``/``rb``/``u`` prefixes, Rust ``r#"..."#``); a YAML anchor or tag before a value; a generated-looking value
    that only starts like a path under a hard secret name (``.Xk4...``, ``~Xk4...``); ODBC ``}}`` inside ``PWD={...}``;
    an API-key header value of any visible characters up to a blank, a quote, a backtick or a backslash (what follows
    one of them is kept: a gap); escapes and the other quote in
    a ``define('NAME', '...')`` value; tab-separated name/value rows and ``name | value`` rows (psql ``\\x``). Kept as
    code: typed fields, parameters and schemas (``password: string;``, ``token: Option<String>,``, ``secret:
    bytes,``, ``password: z.string().min(8),``), tuples of names, a value equal to its name, Maven ``@db.password@``
  - round 10 (verifier v9b): a type is kept only on evidence (_type_kept; in prose and table rows only type syntax and
    built-in type names), so ``password: Passw0rd!``, ``password: Winter560,`` and ``{password: X, user: admin}`` are
    values again; letters-only values under password, secret, token and API-key names (``password: CorrectHorse``,
    ``password = CorrectHorse``, ``api_key: AbCdEf...``; on config lines a lowercase word of six or more letters under
    a password name that is no setting word such as ``required``); prose values of six or seven characters with a digit
    or a symbol inside (``Qw3rty!``), CamelCase with digits, letters with a symbol inside (``P@ssword!``); TypeScript
    ``apiKey?: string = "..."``, C ``char password[] = "..."``, C# property initializers, Rust ``&[u8] = b"..."`` and
    ``br#"..."#``, a Go line in a ``var ( ... )`` block, Swift ``#"..."#``, defaults in placeholders (``${db.password:X}``,
    ``${DB_PASSWORD:-X}``), Go ``%+v`` and ``%v`` maps, PHP ``print_r``, a literal default after an environment lookup
    of a secret-named variable (``process.env.X || "..."``, ``?? '...'``, ``?: '...'``, ``ENV["X"] || "..."``,
    ``ENV.fetch("X", "...")``, ``os.getenv("X", "...")``, ``os.environ.get("X", "...")``, ``env('X', '...')``). Kept:
    a constant that holds the name of a thing (``ACCESS_TOKEN_KEY = 'accessToken'``, ``PASSWORD_PARAM = "password"``),
    Laravel validation rules, an enum constant in a table row, Go ``Password *string`` fields
  - round 11 (verifier v10b): YAML flow mapping items under any secret name, compound ones included
    (``environment: {POSTGRES_PASSWORD: X}``, ``- { name: alice, db_password: X }``, ``[{name: a, api_token: X}]``, on
    one line or continued over lines; a plain value to its ``,``, ``}`` or ``]``, judged like a block line's value:
    _flow_repl), so ``{password: X, user: admin}`` and ``{db_password: X, user: admin}`` are values; a flag and its
    value as two items of an argument list (``args: ["--password", "X"]``, ``command: ["redis-server", "--requirepass",
    "X"]``, ``["redis-cli", "-a", "X"]``, block lists); ``--requirepass``/``--masterauth`` anywhere on a line (a compose
    ``command: redis-server --requirepass X``); lower case and digits right after a secret name in prose and flow
    maps (``summer2024``, ``hunter22x``; not a technical word such as ``sha256``, ``python3``, ``oauth2``, ``node18``:
    _lower_digits_value), six or more digits only under a password, secret or API-key name (``token limit: 200000``
    is a count). Values longer than a rule's limit: the config-line, quoted, flow-map, prose, name/value and XML rules
    take the rest of the token in a ``(?P<more>...)`` group; a marker this pass produced, followed directly by more
    letters or digits, takes the rest of the token (_CUT_TOKEN); YAML block scalars run to the end of their
    indentation; multi-line strings must close within 4 KB or are not matched. What is checked is the grid of
    tests/test_redact_whole_grid.py (14 forms, 24 separators, 20 to 3,000 characters) and the cases of
    tests/test_redact_round11.py; anything else is best effort. Rules that decline a match on purpose (each tested
    from both sides, each a listed gap): a triple-quoted string whose body is clearly prose (three or more lines of four
    or more plain words, no code punctuation: _prose_lines); an XML element whose value is a sentence; a name/value pair
    whose name does not end in a secret word or whose value is a short lowercase word or a sentence
    (_pair_secret_name); names ending in a label word (``_help``, ``_doc``, ``_message``, ``_template``, ...). A YAML
    block scalar under a secret name is never declined (prose written under ``password: >`` is redacted: an accepted
    cost). Settings about a secret are kept: names ending in ``_hash``, ``_strength``, ``_mode``,
    ``_algo``, ``_scheme`` (``password_hash: argon2id``, ``TOKEN_MODE``), a parenthesised word (``Password: (hidden)``)
  - at the write boundary, a dictionary item is judged together with its key (``{"password": "..."}``) and a
    name/value pair as a pair (``{"name": "DB_PASSWORD", "value": "..."}``); an object's ``value`` is replaced when
    the object says ``"sensitive": true`` (Terraform, any value) or is an Azure Key Vault secret (its ``id`` under
    ``.vault.azure.net/secrets/``), and judged under the key that holds the object otherwise (Redactor._valued)
  - round 15 (tests/test_redact_round15.py): the same two in text (Terraform's ``"sensitive": true`` before ``"type"``
    and ``"value"``: a string, a number, or a list or map of one level; a Key Vault ``"value"`` with its ``id`` within
    2 KB before it in the same object, _key_vault_repl); a token glued after ``_`` (``notes_ghp_...``) for each token
    rule and vendor entry; up to six items of any key (not ``value``, ``name`` or ``key``) between a pair's name and value (_PAIR_META:
    compared with the earlier list of metadata keys, it changed no further line of the damage corpora)
  - user patterns: one Python regex per line in ``<store>/redact`` (``#`` starts a comment), run only while the
    file is the one this user approved with ``devmem init`` or /devmem:setup (``user_patterns_state``), after a
    lint and a one-time measurement with a time limit (probe_patterns), each match attempt within a 4 KB window

Where it runs: at the store's write boundary (``Redactor.record``: hooks._append, sessions.write_record, the hook
state), on every string whatever its field, and on every project-file line a context pack quotes. A tool input or
response that is written as JSON (an Edit response with ``originalFile``, a WebFetch or MCP result, the input summary
of an MCP call) has every string in it redacted as the plain text it is before it is serialised
(``Redactor.tree``, used by sessions.output_text and tool_entries), strings next to each other (a list's
consecutive strings, a dictionary's consecutive string values, consecutive content blocks) as one text, one per
line, cut back item by item by position (_join_items, _split_items: the lines of a key block are emptied in place, a
user pattern's match that crosses a line break leaves the line breaks it replaced, and in such a run the omitted lines
of a secret file are emptied in place with the marker on the first one, so each item gets its own lines and only the
items a redaction touched change; should a line count still differ, no text moves from one item into another and
each item between the first and the last changed line becomes ``[REDACTED:lines]``, a safety net no rule is known to
reach; a list keeps its length; strings up to CAP_MARGIN past a budget are redacted with the run, so a construct the
budget's cut splits is seen whole when it ends within CAP_MARGIN past the cut, and a TOML multi-line string or an XML
element under a secret name still open where the kept text ends is redacted from its opening to that end: _close_cut);
stream capture does the same (``Redactor.joined``), and so does the write boundary (``Redactor.record`` joins
every such run unless the run, joined, is a text this redactor produced itself as one text, or every item of it was read
back from the store's event log, which it then only looks up; items it redacted one by one are not enough). Not
joined: pieces that arrive separately (two tool calls, two hook events: a name in one and its value in the other is
not caught; a key printed half by each is caught in both halves, the second by the headless-body rule only when its
body lines stand right above the END line or make a run of KEY_RUN_LINES lines). hook_errors.log gets the built-in and the approved
user patterns. Not redacted: the project map and index (built from project files; secret files are never read).

Content of files that match the secret-file patterns (``.env``, ``.envrc``, ``*.env``, ``.env-*``, ``.env~``,
``*.pem``, ``*.key``, ``*.ppk``, ``id_rsa*``, ``*secret*``, ``*credentials*``, ``*.tfstate``, ``terraform.tfvars``,
``*.auto.tfvars``, kubeconfig, ``.netrc``, ``.npmrc``, ``.dockercfg``, ``wp-config.php`` and more:
``devmem.store.HARD_IGNORES``, the list the scanner ignores; plus ``.kube/config``, ``.docker/config.json`` and
``.config/gh/hosts.yml`` by location) is never stored at all, whichever tool names the file
(``input_names_secret_file``: any path-like key at any depth, ``file://`` URLs, a specific secret name under any other
key; shell commands: ``mentions_secret_file``, including ``git show HEAD:.env``, globs such as ``*.pem``, words as
a shell unquotes them, ``.e''nv``, ``.\\env``, brace expansions ``.e{n,}v``, PowerShell ``.e`nv`` and cmd ``.e^nv``,
also under ``input``/``args``/``argv``/``line``/``stdin`` keys of terminal tools), and in any other output, ``path:line`` / ``path-N-`` lines (grep), diff
hunks (``git diff``, ``git log -p``, ``diff -u``) and ``==> name <==`` sections (``head``/``tail``) of such a file are
replaced by an ``[OMITTED: ...]`` marker (``omit_secret_file_lines``). ``filter_tool_output`` is the one function
hooks and stream capture both use.

Cost: what the tests check is that the cost of every built-in pattern grows linearly on the inputs they build
(tests/test_redact_speed.py, tests/test_redact_pattern_growth.py; one line of 4 KB against one of 64 KB after each
rule's own match, tests/test_redact_forward_scans.py); every replacement
callback looks only at its own match, a constant window around it (at most 4 KB: a header's name, a printed
structure's brace, a list's first item, the line a `KEY=` starts) or a memo built once per text: the connection-string
rule judges each line once, and the lines around a value (_line_context) are read within 1 KB either way, each line's
kind and verdict once per text, so thousands of matches cost one pass (tests/test_redact_line_sweep.py sweeps line
lengths from 16 bytes to one line, with and without trailing blanks); a rule runs again only
while its last pass changed the text (at most 9 passes). tests/test_redact_growth.py runs each rule's own match
repeated to 100 KB and 512 KB through ``Redactor.text`` and checks, in CPU time of the process (best of three, each
size in turn, measured again after a pause before it fails), that 512 KB costs at most ten times what 100 KB does
and at most CEIL_S (15 s; three times that on a machine so loaded that 100 KB alone costs a tenth of it). Callers
with a cap use
``Redactor.capped``, which redacts only cap + CAP_MARGIN characters. omit_secret_file_lines is linear too
(tests/test_secret_lines_speed.py). A rule that can only match where one of its words occurs is skipped for a text
without any of them (_NEEDS; a stem only decides whether a rule runs, the rule itself tests a real name). The two
structure passes cost up to about twice the plain cost on text that is all decorated lines or JSON, and once more per
level of JSON inside JSON; a text with terminal escape sequences is seen twice without them instead of once with
them. The readings beyond those (a diff's sides, ``diff -y`` columns, the second reading of a text with carriage
returns inside lines, and every reading made inside them, JSON strings included) share one budget per call
(Redactor._read): READ_FACTOR (3) times the characters the call's other readings handed the rule pass so far, plus
READ_BASE (4,096). The second reading of a text with carriage returns inside lines is a plain reading itself when the
text is read outside every side reading (tests/test_redact_cr_only_files.py). A reading is granted before it runs
(hunks and blocks in order, gathered in batches), a further pass inside it that would go past the budget is not run,
and what the budget refuses fails closed, replaced whole (tests/test_redact_budget_rest.py): every line of the refused
hunk and of the hunks after it in the same text (later commits of a ``git log -p`` included), every row of the
refused ``diff -y`` block and of the blocks after it in the same text (DIFF_FAILED); a JSON string such a reading would have decoded, unless it is one line that decodes to its own text
(BUDGET_FAILED); every line of a text whose second reading is refused, or whose first reading had a reading refused
(BUDGET_FAILED). So the side readings hand the rule pass at most READ_FACTOR times the
other readings plus READ_BASE and one batch read again piece by piece: tests/test_redact_read_budget.py checks that
bound on every pair of ten decorations at 6 and 24 KB (every triple under the stress marker), and that a call costs at
most 16 times one plain rule pass in CPU time on the single decorations and eight stacks of up to three. A diff or
column reading inside another one is the last (MAX_SIDE_LEVEL, 2: the sides of a diff of a patch file, where runs of
marked lines without a hunk header are not read again). Redactor.tree redacts at most TREE_LEAVES strings and its budget of characters; a key is kept up to MAX_KEY
characters; input_names_secret_file looks at MAX_INPUT_NODES keys and values at most.

The patterns aim to keep ordinary code intact: ``token = tokenize(text)``, ``password: str``,
``os.environ["API_KEY"]``, ``max_tokens=100``, ``password_field = models.CharField()``, ``bypass``,
``connect(password=pw)``, ``f(token=args.token2)``, ``f(secret=SECRET_V2)``,
``docker run -p8080:80``, versions (``"@aws-sdk/credential-providers": "^3.645.0"``), ids and addresses
(``tokenId``, ``session_token_id``), Stripe publishable keys (``pk_live_``/``pk_test_``, public by design) and
placeholders such as ``"<your-key>"`` are left alone (tests/test_redact_round6.py and the earlier rounds hold the
look-alikes: git hashes, UUIDs, base64 images, lock-file integrity hashes, colours, long identifiers, paths,
``password = None``, ``--password`` help text, XML with empty or placeholder values, labels such as
``"Enter your password"``). Not caught (known gaps): a passphrase that reads like a label or sentence (it holds words
such as "the", "your", "new"), a shell line continuation that puts a flag's value on the next line (``NAME=\\`` and
``NAME=value\\`` continued are caught: _spanning_repl), a URL password holding
``@`` or ``/``, JSON escaped more than eight levels deep, a netrc entry without ``login``, ``docker login`` with
options before ``login``, a value that equals a placeholder word (``example...``, ``test``, ``changeme``), a bare
``PAT=`` without a qualifier, identifiers that are not secrets on their own (GCP ``private_key_id``), a secret with no
recognisable shape or name next to it (a bare AWS secret access key, Twilio auth token, Azure, Datadog, Cohere or
Mistral key in prose: those vendors have no prefix), a key split across string literals (``"SG.abc" + ".def"``),
URL-encoded JSON (``%22password%22%3A%22...``), a name and a value in the opposite order (``value="..." key="..."``),
command-line passwords of tools not in the command table, and a secret file a shell command reaches without naming it
(``cat $(ls -a | grep env)``, ``rg --type env``, ``git cat-file`` by hash, a name built from strings: ``gc '.e' +
'nv'``), a config file with an ordinary name (``database.yml``, ``appsettings.Production.json``: only the value rules
apply) or a bare secret name such as ``secrets.yaml`` under a key that is not path-like. Missed in the round-7
corpora (tests/test_redact_round7.py; corpus 8 was written after the rules and missed 14 of 69): a value under a
one-letter name (GraphQL ``"p": "..."``, JWK ``"d"``), positional arguments (``new PDO(dsn, 'u', 'pw')``,
``algoliasearch('ID', 'key')``), pgbouncer ``"user" "pw"`` lines, rsyncd.secrets and cloud-init ``chpasswd`` ``user:pw``
lines, tokens in a URL path (``/t/<token>/``), ``openvpn --askpass <(echo pw)``, a fallback literal
(``unwrap_or("pw")``), Makefile ``NAME := value``, ``rootpw`` (kickstart, slapd), debconf preseed ``password pw`` after
other words, unattend.xml ``<AdministratorPassword><Value>``, ``echo pw | keepassxc-cli``, ``openssl enc -k pw``, pdftk
``user_pw``, ``qpdf --encrypt``, prose without a "is"/":" (``set the wifi password to X``) and Japanese prose. Known gaps after
round 8 (verifier v7b): a bare config value with a blank inside (``password = my long pass``, a connection-string
password with a blank), a single-quoted shell value that ends in a backslash, a YAML value that starts with ``#``
and does not look generated (YAML reads it as a comment), CSV doubled quotes and HTML entities (``&quot;``,
``&#x27;``) around a value, zero-width and no-break spaces inside a name or between a name and its value, Python
``repr`` text where a literal ``\\n`` sits right before ``-p...`` or ``api_key:``, a ``mongodb+srv`` password with a
raw ``@``, an API-key header of letters only shorter than 24, a table row whose value has blanks, a secret split
across separate tool calls or hook events (above), and user patterns see 4 KB windows. Known gaps after round 9
(verifier v8b; tests/test_redact_round9_misses.py pins one example of several): a literal passed to a call
(``SecretStr("...")``, ``String::from("...")``, ``strcpy(cfg.password, "...")``, ``[]byte("...")``), Makefile
``NAME := value``, fish ``set -x NAME value``; a value that is shaped like a type or a schema (type syntax such as
``Pass<word>``, a schema call ``z.string().min(8)``, a dotted name without digits such as ``Admin.Pass``, a built-in type
name); a value of letters only that reads as a name (verifier v9b, measured with 300 generated passwords per style and
position: at most 4% kept outside the cases below): CamelCase whose last word is a type word (``WinterSecret``,
``...Token``, ``...Key``), a capitalised word shorter than eight letters, letters with ``_`` or ``-``
(``Correct-Horse``), all capitals, a lowercase word shorter than twelve letters under a token, secret or API-key name
or shorter than six under a password name, setting words (``required``, ``secret``, ``hidden``, ...), any class-like
word among lines of code (an interface, a dataclass), and every letters-only value under other names (``key``,
``auth``); in prose a value of letters only after other words or in lower case; a value shorter than six characters
on a config line (``Tig3r``), or than six in prose (eight unless it holds a digit or a symbol); a name of two letters
(``pw``); a value starting with ``{`` in a YAML block scalar, a PHP ``define``, an ADO string or a
query/JDBC/form value (``%7B...``); a value that starts like a path under a token or key name, or with a separator
inside (``API_TOKEN=/Xk4.../...``); shell quoting ``'"'"'`` inside a value; a value that starts like a reference
(``$ecret...``, ``@dmin...``, ``!Pass...``); a URL password holding ``"`` or ``'``; tables whose header row names the
column (psql and mysql result rows, ``kubectl`` custom columns ``NAME PASSWORD``, a three-column ``Format-Table``, the
aws CLI's ``| Value | ... |`` and ``--output text`` rows); a key block cut off inside a diff empties one short ``+added`` line after it. Known
gaps of the diff readings (rounds 15 and 16; docs/PRIVACY.md lists the views): a hunk whose head is cut off when the
value's name is above the visible lines (``git diff -U0``, a cap that keeps the tail, a block scalar or list-item
block edited more than three lines below its name in a default ``git diff``, a name and its value in two hunks; strict
expected failures in tests/test_redact_tool_views.py; key material without its BEGIN line is caught by
_key_run_repl unless fewer than KEY_RUN_LINES of its lines are in view and no END line is); ``diff`` in its normal
format (``2c2`` / ``< old`` / ``> new``: no name in view); ``git range-diff`` and a diff of a patch file whose inner
change has no hunk header (runs of marked lines without a header are not read inside a side); ``--word-diff=porcelain``
and ``--color-words``; a ``grep -C``/``rg -C`` window that cuts a triple-quoted or TOML multi-line string before its
closing quotes; a ``git blame`` author holding a parenthesis; ``diff -y`` at a width that cuts a column; a word-diff
part more than WORD_DIFF_REACH lines after the last changed line; a sensitive Terraform value nested two levels in
text (an expected failure in tests/test_redact_round15.py); a name/value pair whose name line ends in a comment, or
with an item between name and value whose unquoted value holds blanks (``Description: primary db password``). Known
gaps after round 11 (verifier v10b): a flow value with blanks (``{password: my long pass}``) or a quoted one with
blanks under a name other than password, passphrase or secret; a name and a value in the opposite order (``{value: X,
name: DB_PASSWORD}``); a short flag in a block list or in a list whose first item is not the command; a value longer
than its rule's limit in the other rules (SQL clauses, setters, ``define``, table rows: 256 to 4,096 characters, then
not matched at all); multi-line JS template literals, C# ``@"..."`` and Swift raw triple-quoted strings; Swift
``ProcessInfo.processInfo.environment["X"] ?? "..."``; a lone ``password = postgres`` line without config lines around
it; a two-column table row indented more than eight spaces; a parenthesised value in prose (``The API key (X)``) and
non-ASCII values in prose and flow maps; a lowercase word of letters only in prose or a flow map; a word of letters
only after a flag and a blank (``--requirepass AustinCowboys``); a CDATA value holding ``<``; ``tls.key: |``.
A call that is code by
shape is kept even when it holds a secret (``password = decrypt(Xk4...)``), and a generated value that looks like
a call or a reference is kept too (8 of 2,000 generated Django keys, measured, when they appear inline as in
``docker run -e SECRET_KEY=...``; none on their own config line). Over-omission (output dropped, nothing stored): a command
that mentions ``.env`` (``git commit -m "ignore .env ..."``, ``echo '.env' >> .gitignore && git status``), reads
``.env.example``, or names a file such as ``secret_santa.py`` (``*secret*``); every line after a secret file's diff
hunk up to the next file header (``diff ...``, ``commit ...``, ``--- ``/``+++ ``) counts as part of that hunk, also in a
list of lines where each such line becomes empty. Redaction is pattern-based, so it is
a safety net, not a guarantee; the store also stays local and gitignored.
"""
from __future__ import annotations

import fnmatch
import functools
import json
import os
import re
import threading
from typing import Any
from pathlib import Path

# Single source of truth is devmem.store (HARD_IGNORES: never listed, read, hashed or indexed); the v1 list
# is the fallback for an older store module.
_FALLBACK_SECRET_FILES = [".env", ".env.*", "*.pem", "*.key", "id_rsa*", "*secret*", "*credentials*"]
try:
    from .store import HARD_IGNORES as _HARD
    SECRET_FILE_PATTERNS = [p for p in _HARD if p not in (".git", ".devmemory")]
except ImportError:  # pragma: no cover
    SECRET_FILE_PATTERNS = list(_FALLBACK_SECRET_FILES)

OMITTED_SECRET_FILE = "[OMITTED: content of a secret file is never stored]"

# the word for password in other languages (names, prose, config lines)
_FOREIGN_PASSWORD = (r"passwort|kennwort|wachtwoord|l[öo]senord|passord|adgangskode|salasana|has[łl]o|heslo|jelsz[óo]|"
                     r"contrase[ñn]a|senha|[şs]ifre|пароль|密码|密碼|口令|パスワード|비밀번호|סיסמה|"
                     r"mot[ _]de[ _]passe|kata[ _]?sandi|m[ậa]t[ _]kh[ẩa]u|كلمة[ _]المرور|كلمة[ _]السر")
# Secret-sounding names. A strong word anywhere in the name is enough (`dbPassword`, `GITHUB_TOKEN`); a short
# form only counts as the name's last segment (`DB_PASS`, `mysql_pwd`, and `STRIPE_KEY` in env style), so
# `bypass`, `pass_through`, `monkey` or `sort_key = "created_at"` stay untouched.
_STRONG_NAME = re.compile(
    r"(?i)pass(?:word|wd|phrase|code)|secret|api[_-]?key|access[_-]?key|private[_-]?key|auth[_-]?token|"
    r"access[_-]?token|refresh[_-]?token|bearer[_-]?token|session[_-]?token|token(?!s|iz)|credential|"
    r"connection[_-]?string|" + _FOREIGN_PASSWORD)
_SHORT_LAST = {"pass", "pwd", "passwd", "dsn", "salt", "psk"}
# env style only (UPPER_SNAKE=value): FOO_KEY with a value that looks generated (letters and digits, 12+ chars)
_KEY_NOT_SECRET = {"sort", "primary", "foreign", "cache", "partition", "hot", "lookup", "group", "join", "map",
                   "dedup", "routing", "shard", "index", "unique", "composite", "public", "pub", "music"}
# names that contain a secret word but hold no secret (counts, kinds, labels, locations)
_BENIGN_NAME = re.compile(r"(?i)(tokens|tokeniz|_?(types?|kind|count|len|length|size|limit|max|min|name|field|label|"
                          r"hint|prompt|placeholder|url|uri|endpoint|path|file|dir|env|var|header|policy|rule|"
                          r"regex|pattern|format|expiry|expires|ttl|id|ids|address|addr|version|providers?|manager|"
                          r"rounds|length|stdin|hash|hashes|hashed|strength|mode|algo|algorithm|alg|scheme|help|usage|description|desc|"
                          r"doc|docs|message|msg|tooltip|title|example)$|(?:page|next|continuation|cursor|resume|sync|pagination)[_-]?token)")
# a version or version range (`^3.645.0`, `2.14.0b1`, `>=1.2`): a dependency or setting, never a credential
_VERSION = re.compile(r"^[\^~<>=v]{0,3}\d{1,9}(?:\.\d{1,9}){1,3}(?:[-+.]?(?:a|b|rc|alpha|beta|dev|post|pre)\d{0,4})?$",
                      re.I)
# a name with one of these words is a secret even when it ends in id: VAULT_SECRET_ID, DB_PASSWORD_ID (a version value
# is told apart by _VERSION, which allows only release suffixes such as b1 or rc2)
_HARD_SECRET = re.compile(r"(?i)pass(?:word|wd|phrase|code)|secret|pwd|psk|" + _FOREIGN_PASSWORD)
_SOFT_SUFFIX = re.compile(r"(?i)_?ids?$")


def _segments(name: str) -> list[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [x for x in re.split(r"[_\-.\s]+", name.lower()) if x]


def _secret_name(name: str, env: bool = False) -> str | None:
    """'strong', 'short', 'key' (env-style FOO_KEY) or None."""
    if _STRONG_NAME.search(name):
        return "strong"
    segs = _segments(name)
    if segs and (segs[-1] in _SHORT_LAST or segs[-1].endswith(("pwd", "passwd"))):  # DB_PASS, PGPWD
        return "short"
    if env and len(segs) >= 2 and segs[-1] == "key" and segs[-2] not in _KEY_NOT_SECRET:
        return "key"
    if env and len(segs) >= 2 and segs[-1] == "pat":     # AZURE_DEVOPS_PAT, GITHUB_PAT (personal access token)
        return "key"
    return None


# Every pattern below is linear in the input length: an unbounded run is only entered at its start (negative
# lookbehind) or consumed in one go, and repetitions that could otherwise backtrack are made atomic with the
# `(?=(?P<x>...))(?P=x)` idiom (Python 3.10 has no possessive quantifiers). tests/test_redact_speed.py times
# each pattern on adversarial inputs.

# (kind, compiled regex, replacement). Order matters: specific token formats first.
_RULES: list[tuple[str, re.Pattern, object]] = []


# a rule that can only match where one of its words occurs is skipped for a text without any of them (a substring
# test on the lower-cased text is far cheaper than a regex scan): `need` lists those words, in lower case
_NEEDS: dict[re.Pattern, tuple[str, ...]] = {}


def _rule(kind: str, pattern: str, repl=None, flags: int = 0, need: tuple[str, ...] = ()) -> None:
    rx = re.compile(pattern, flags)
    _RULES.append((kind, rx, repl if repl is not None else f"[REDACTED:{kind}]"))
    if need:
        _NEEDS[rx] = need


# PEM (`-----BEGIN RSA PRIVATE KEY-----`) and OpenPGP armour (`-----BEGIN PGP PRIVATE KEY BLOCK-----`): the header, the
# key material after it and the END line. Key material is lines of base64 (or `Proc-Type:`/`DEK-Info:`/`Version:` headers,
# a blank line in PGP armour), one per line, also with `\n` escapes for line breaks (a key in a .env value or a JSON
# string) or blanks between long base64 runs (a key whose newlines became spaces). A block without its END line (cut off
# by truncation) is redacted up to its last line of key material; a header with no key material after it (a mention in
# prose or code) is left as it is and nothing after it is touched. The replacement keeps the block's line breaks, so a
# diff, a numbered listing or a list of lines keeps its line count (_pem_repl).
_PEM_HEADER = r"-{4,5}BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-{4,5}"
_PEM_END = r"-{4,5}END [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-{4,5}"
# (a line break, blanks only before it so that no two separators share a blank; or blanks before a long base64 run)
_PEM_SEP = (r"(?:[ \t]{0,64}(?:\r?\n|(?:\\{1,8}r)?\\{1,8}n)"
            r"|[ \t]{1,64}(?=[A-Za-z0-9+/]{40}|[A-Za-z0-9+/=]{1,40}[ \t]{1,64}" + _PEM_END + r"))")
_PEM_HDR = r"(?:Proc-Type|DEK-Info|Comment|Version|Hash|Charset|MessageID)"
_PEM_LINE = (r"[ \t]{0,64}[\->!]?(?:[A-Za-z0-9+/=]{1,4096}|" + _PEM_HDR + r":[ \t][^\r\n\\]{0,512})"
             r"(?=[ \t]{0,64}(?:\r?\n|\\|-{4,5}END|\Z)|[ \t]{1,64}[A-Za-z0-9+/=]{1,40}[ \t]{1,64}-{4,5}END"
             r"|[ \t]{1,64}[A-Za-z0-9+/]{40})")
_PEM_MATERIAL = re.compile(r"[A-Za-z0-9+/]{16}")


def _pem_repl(m: re.Match) -> str:
    whole = m.group(0)
    if not m.group("end") and not _PEM_MATERIAL.search(m.group("body")):
        return whole            # a header mentioned without key material after it
    return "[REDACTED:private-key]" + "\n" * whole.count("\n")


_rule("private-key", _PEM_HEADER + r"(?=(?P<body>(?:" + _PEM_SEP + r"{1,64}" + _PEM_LINE + r"){0,4096}))(?P=body)"
      r"(?P<end>" + _PEM_SEP + r"{0,64}[ \t]{0,64}[\->!]?" + _PEM_END + r")?", _pem_repl, flags=re.I)
# a block whose lines are wrapped in code (string literals joined across lines: `"MIIE...\n" +`, a list of lines): an
# END line within 16 KB, every line between holding only key material and quoting/joining characters
_PEM_WRAPPED_LINE = re.compile(r"(?:[A-Za-z0-9+/=]{1,4096}|" + _PEM_HDR + r":[ \t][^\r\n]{0,512})?", re.I)
_PEM_WRAPPING = " \t\r\"'`+,.;()[]"


def _pem_wrapped_line(ln: str) -> bool:
    ln = ln.strip(_PEM_WRAPPING)
    while ln.endswith(("\\n", "\\r")):
        ln = ln[:-2].rstrip("\\").strip(_PEM_WRAPPING)
    return _PEM_WRAPPED_LINE.fullmatch(ln.lstrip("\\")) is not None


def _pem_wrapped_repl(m: re.Match) -> str:
    mid = m.group("mid")
    if not _PEM_MATERIAL.search(mid) or not all(_pem_wrapped_line(ln) for ln in mid.split("\n")):
        return m.group(0)
    return "[REDACTED:private-key]" + "\n" * m.group(0).count("\n")


def _b64_pem_repl(m: re.Match) -> str:
    """Base64 of a PEM block (kubeconfig `client-key-data:`, a key in an env variable): replaced only when it decodes to a
    private key header, so `certificate-authority-data` (a public certificate) stays."""
    import base64
    import binascii
    head = m.group(0)[:96]
    try:
        raw = base64.b64decode(head[: len(head) // 4 * 4], validate=True).decode("ascii", "replace")
    except (binascii.Error, ValueError):
        return m.group(0)
    return "[REDACTED:private-key]" if "PRIVATE KEY" in raw.upper() else m.group(0)


_rule("private-key", r"(?<![A-Za-z0-9+/])LS0tLS1CRUdJTi[A-Za-z0-9+/]{20,65536}={0,2}", _b64_pem_repl)
# PuTTY .ppk: the lines after `Private-Lines: N` are the private key, each a whole line (the `Private-MAC:` line after
# them is not one); the lines are emptied, so the text keeps its line count
_rule("private-key", r"(?m)(?P<pre>^[ \t]{0,64}Private-Lines:[ \t]{0,8}\d{1,4}[ \t]{0,8}\r?\n)(?!\[REDACTED)"
      r"(?P<body>(?:[ \t]{0,64}[A-Za-z0-9+/=]{1,256}\r?\n){0,255}[ \t]{0,64}[A-Za-z0-9+/=]{1,256})(?=\r?$)",
      lambda m: m.group("pre") + "[REDACTED:private-key]" + "\n" * m.group("body").count("\n"))
# Key material without its BEGIN line in view (a diff of a key file whose default three lines of context start below
# the header, `Proc-Type:`/`DEK-Info:` and a blank line taking them): a run of at least KEY_RUN_LINES lines that are
# base64 of one length a key file is wrapped at (64: PEM, OpenPGP armor, PuTTY; 70: OpenSSH), the last one possibly
# shorter, each full line holding upper and lower case letters and a digit (hex dumps, word lists and padding are not
# key material); or base64 lines of any length right above a visible END line of a private key. Not public
# material: not when the BEGIN line nearest above it (within 8 lines) or the END line right below it is there and names
# something that is no private key (CERTIFICATE, PUBLIC KEY, CERTIFICATE REQUEST, X509 CRL, PKCS7, SIGNATURE); not when
# the line below it, or the text before the run on its own first line, names X.509 (the closing
# `</ds:X509Certificate>` of SAML metadata; a line above alone does not count); and not anywhere in
# a text whose armour lines all name such public material (a certificate bundle viewed from its middle: the BEGIN and
# END lines of the other certificates are the evidence; RFC 4716's `---- BEGIN SSH2 PUBLIC KEY ----` counts). Not data
# either: not when the line above it (a blank line between allowed) says what it is, a MIME part's
# `Content-Transfer-Encoding: base64` or a data URI's `;base64,`; and not a run of more than KEY_RUN_MAX lines with no
# END line below it (by calculation, not measured, the body of one private key is shorter: an OpenPGP RSA-16384
# secret key with one subkey comes to about 475 lines of 64, an RSA-16384 PEM or OpenSSH key under 200). A text with no armour line at all, or with a private
# key's, keeps the rule: a headless run there errs towards redaction (key material cannot be told from a certificate
# body or other base64 by its base64 alone).
KEY_RUN_LINES = 3
KEY_RUN_MAX = 512
_KEY_WIDTHS = (64, 70)
_KEY_MIX = (re.compile(r"[A-Z]"), re.compile(r"[a-z]"), re.compile(r"[0-9]"))
_BEGIN_ABOVE = re.compile(r"-{4,5} ?BEGIN ([A-Z0-9 ]{1,60})-{4,5}")
# (matched after the line's number and diff markers are stripped, which takes the END line's own leading dashes too)
_END_BELOW = re.compile(r"[ \t]{0,64}(?:\S{0,16}[ \t]{0,8})?-{0,5} ?END ([A-Z0-9 ]{1,60})-{4,5}")
_KEY_LAST_LINE = re.compile(r"(?:\d{1,9}(?:→|\t|:)|[ +-]{0,8})?[ \t]{0,64}[A-Za-z0-9+/=]{1,15}")
_DATA_ABOVE = re.compile(r"(?i)content-transfer-encoding:[ \t]{0,8}base64[ \t\r]*$|;base64,[ \t\"'(\\\r]*$")
_X509_NEAR = re.compile(r"(?i)x509")
# (a private key already replaced counts as one: the rules before this one run on the same text)
_ARMOUR_PRIVATE = re.compile(r"-{4,5}(?:BEGIN|END) [A-Z0-9 ]{0,40}PRIVATE KEY|\[REDACTED\x02?:private-key\]")
_ARMOUR_ANY = re.compile(r"-{4,5}(?:BEGIN|END) [A-Z0-9 ]{1,60}-{4,5}")
_ARMOUR_MEMO = threading.local()


def _public_armour_only(s: str) -> bool:
    """Does `s` hold armour lines (BEGIN/END) and none of a private key? Once per text (a memo of the last one)."""
    memo = _ARMOUR_MEMO.__dict__
    if memo.get("s") is not s:
        memo["s"] = s
        memo["v"] = _ARMOUR_ANY.search(s) is not None and _ARMOUR_PRIVATE.search(s) is None
    return memo["v"]


def _key_run_repl(m: re.Match) -> str:
    run = m.group("run")
    core = [ln.strip() for ln in run.split("\n")]
    if all(c[:1] == "+" for c in core):
        return m.group(0)       # (added lines of a diff: the view without markers and the diff's sides read them)
    if m.group("end") is None:
        width = len(core[0])
        if width not in _KEY_WIDTHS or not KEY_RUN_LINES <= len(core) <= KEY_RUN_MAX or len(core[-1]) > width or \
                any(len(c) != width for c in core[:-1]) or \
                not all(rx.search(c) for c in core[:-1] for rx in _KEY_MIX):
            return m.group(0)
    s = m.string
    above = s[max(0, m.start() - 1024):m.start()].split("\n")[-9:]
    near = [ln for ln in above[-3:-1] if ln.strip()][-1:]      # (the line above, or the one above a blank line)
    if m.group("end") is None and near and _DATA_ABOVE.search(near[0]):
        return m.group(0)
    for ln in reversed(above):
        b = _BEGIN_ABOVE.search(ln)
        if b:
            if "PRIVATE" not in b.group(1).upper():
                return m.group(0)
            break
    nl = s.find("\n", m.end() + 1)
    below = s[m.end() + 1:nl if nl >= 0 else min(len(s), m.end() + 1024)][:1024] if m.end() < len(s) else ""
    e = _END_BELOW.match(below.lstrip("0123456789:\u2192\t +-"))
    if e is None and nl >= 0 and _KEY_LAST_LINE.fullmatch(below.strip()):
        # (the body's last line, shorter than the run's lines can be, between the run and its END line)
        nl2 = s.find("\n", nl + 1)
        e = _END_BELOW.match(s[nl + 1:nl2 if nl2 >= 0 else len(s)][:1024].lstrip("0123456789:\u2192\t +-"))
    if m.group("end") is None and e and "PRIVATE" not in e.group(1).upper():
        return m.group(0)
    if _X509_NEAR.search(above[-1] if above else "") or _X509_NEAR.search(below[:256]):
        return m.group(0)
    if m.group("end") is None and _public_armour_only(s):
        return m.group(0)
    lead = run[:len(run) - len(run.lstrip(" \t"))]
    return lead + "[REDACTED:private-key]" + "\n" * run.count("\n") + (m.group("end") or "")


_rule("private-key", r"(?m)^(?P<run>[ \t]{0,64}[A-Za-z0-9+/=]{16,100}[ \t]{0,64}\r?"
      r"(?:\n[ \t]{0,64}[A-Za-z0-9+/=]{16,100}[ \t]{0,64}\r?){0,8191})$"
      r"(?P<end>\n[ \t]{0,64}" + _PEM_END + r")?", _key_run_repl)
# (the wrapped key block: _pem_wrapped_repl; the text between header and END never holds another header)
_rule("private-key", _PEM_HEADER + r"(?P<mid>(?:(?!-{4,5}BEGIN )[^\x00]){0,16384}?)" + _PEM_END, _pem_wrapped_repl,
      flags=re.I)
_rule("anthropic-key", r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_\-]{16,}")
# (a short prefix that is also a common piece of an identifier (`key-`, `SK`, `dapi`, a bare `sk-`) needs a word
# boundary: `cache_key-<md5>`, `TASK_SK<hex>`, `build_sk-<id>` are names; a distinctive prefix may follow `_`)
_rule("openai-key", r"(?<![A-Za-z0-9])sk-(?:proj-|svcacct-|admin-)[A-Za-z0-9_\-]{20,}|"
      r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_\-]{20,}")
_rule("github-token", r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})")
_rule("gitlab-token", r"(?<![A-Za-z0-9])gl(?:pat|ptt|dt|rt|cbt|imt|soat|ffct)-[A-Za-z0-9_\-]{20,}")
_rule("mailgun-key", r"(?<![A-Za-z0-9_])key-[0-9a-f]{32}\b")
_rule("slack-token", r"(?<![A-Za-z0-9])(?:xox[abposre]-|xapp-\d-)[A-Za-z0-9\-]{10,}")
_rule("slack-webhook", r"(?<=hooks\.slack\.com/)(?:services|workflows|triggers)/(?!\[REDACTED)[A-Za-z0-9_/\-]{20,}")
_rule("sendgrid-key", r"(?<![A-Za-z0-9])SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}(?![A-Za-z0-9_\-])")
_rule("pypi-token", r"(?<![A-Za-z0-9])pypi-AgE[A-Za-z0-9_\-]{50,}")
_rule("google-oauth-secret", r"(?<![A-Za-z0-9])GOCSPX-[A-Za-z0-9_\-]{20,}")
# also in a Bot API URL (`api.telegram.org/bot<token>/...`), where the lookbehind alone would see the `t` of `bot`
_rule("telegram-bot-token", r"(?:(?<=/bot)|(?<![\w:]))\d{8,10}:AA[A-Za-z0-9_\-]{30,40}(?![A-Za-z0-9_\-])")
_rule("stripe-webhook-secret", r"(?<![A-Za-z0-9])whsec_[A-Za-z0-9+/=]{24,}")
_rule("vault-token", r"(?<![A-Za-z0-9])hv[sbr]\.[A-Za-z0-9_\-]{24,}")
_rule("discord-webhook", r"(?P<pre>discord(?:app)?\.com/api/(?:v\d{1,3}/)?webhooks/)\d{5,30}/[A-Za-z0-9_\-]{20,}",
      r"\g<pre>[REDACTED:discord-webhook]")
_rule("digitalocean-token", r"(?<![A-Za-z0-9])do[por]_v1_[a-f0-9]{64}\b")
_rule("twilio-key", r"(?<![A-Za-z0-9_])SK[0-9a-f]{32}\b")      # Twilio key SIDs are lowercase hex (`class SK<HEX>` is a name)
_rule("databricks-token", r"(?<![A-Za-z0-9_])dapi[0-9a-f]{32}(?:-\d)?\b")
_rule("shopify-token", r"(?<![A-Za-z0-9])shp(?:at|ca|pa|ss)_[0-9a-fA-F]{32}\b")
_rule("aws-access-key-id", r"(?<![A-Za-z0-9])(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b")
_rule("google-api-key", r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-]{35}")
_rule("stripe-key", r"(?<![A-Za-z0-9])(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{10,}")   # pk_ (publishable) keys are public
_rule("docker-token", r"(?<![A-Za-z0-9])dckr_pat_[A-Za-z0-9_\-]{20,}")
_rule("linear-key", r"(?<![A-Za-z0-9])lin_api_[A-Za-z0-9]{32,}")
_rule("age-secret-key", r"(?<![A-Za-z0-9])AGE-SECRET-KEY-1[0-9A-Z]{50,}")
_rule("fcm-server-key", r"(?<![A-Za-z0-9])AAAA[A-Za-z0-9_\-]{7,64}:APA91b[A-Za-z0-9_\-]{20,}")

# Vendor tokens recognisable by their prefix and shape, as one regex (one pass over the text whatever the number of
# vendors). A token must start after a character that cannot be part of it (so one inside a base64 run or a longer
# word is not cut out of it); `_` may stand before it (`notes_ghp_...`, `TOKEN_glpat-...`: an identifier glued to
# it), here and in the token rules above. Add a vendor as (name, regex of the whole token).
_VENDOR_TOKENS = [
    ("groq-key", r"gsk_[A-Za-z0-9]{40,64}"),
    ("xai-key", r"xai-[A-Za-z0-9]{60,100}"),
    ("langsmith-key", r"lsv2_(?:pt|sk)_[0-9a-f]{32}_[0-9a-f]{10}"),
    ("notion-token", r"secret_(?=[a-z0-9]{0,49}[A-Z])(?=[A-Za-z]{0,49}[0-9])[A-Za-z0-9]{40,50}(?![A-Za-z0-9])|ntn_[0-9]{11}[A-Za-z0-9]{35}"),
    ("replicate-token", r"r8_(?=[A-Za-z]{0,36}[0-9])[A-Za-z0-9]{37}(?![A-Za-z0-9])"),
    ("sentry-token", r"sntry[su]_[A-Za-z0-9_=+/\-]{40,512}"),
    ("grafana-token", r"gl(?:sa|c)_[A-Za-z0-9_=+/\-]{32,512}"),
    ("newrelic-key", r"NR(?:AK|AA|II|JS|SP)-[A-Z0-9]{27}"),
    ("postman-key", r"PMAK-[0-9a-f]{24}-[0-9a-f]{34}"),
    ("doppler-token", r"dp\.(?:st|ct|pt|sa|scim|audit)\.[A-Za-z0-9_\-.]{20,256}"),
    ("atlassian-token", r"ATATT3[A-Za-z0-9_\-=]{50,512}|ATBB[A-Za-z0-9_=.\-]{32,64}"),
    ("google-oauth-token", r"ya29\.[A-Za-z0-9_\-]{20,2048}"),
    ("mailchimp-key", r"[0-9a-f]{32}-us[0-9]{1,2}(?![A-Za-z0-9])"),
    ("figma-token", r"figd_[A-Za-z0-9_\-]{30,128}"),
    ("square-token", r"sq0(?:atp|csp|idp)-[A-Za-z0-9_\-]{22,64}"),
    ("perplexity-key", r"pplx-[A-Za-z0-9]{40,64}"),
    ("planetscale-token", r"pscale_(?:tkn|pw|oauth)_[A-Za-z0-9_\-.]{32,64}"),
    ("flyio-token", r"fo1_[A-Za-z0-9_\-]{40,64}"),
    ("pulumi-token", r"pul-[0-9a-f]{40}"),
    ("airtable-token", r"pat[A-Za-z0-9]{14}\.[0-9a-f]{64}"),
    ("contentful-token", r"CFPAT-[A-Za-z0-9_\-]{43}"),
    ("rubygems-key", r"rubygems_[0-9a-f]{48}"),
    ("nuget-key", r"oy2[a-z0-9]{43}(?![a-z0-9])"),
    ("adafruit-key", r"aio_[A-Za-z0-9]{28}(?![A-Za-z0-9])"),
    ("buildkite-token", r"bkua_[0-9a-f]{40}"),
    ("prefect-key", r"pnu_[A-Za-z0-9]{36}"),
    ("readme-key", r"rdme_[a-z0-9]{70}"),
    ("tailscale-key", r"tskey-(?:auth|api|client|scim|webhook)-[A-Za-z0-9]{6,64}-[A-Za-z0-9]{20,128}"),
    ("1password-token", r"ops_eyJ[A-Za-z0-9_\-]{40,4096}"),
    ("mapbox-secret", r"sk\.eyJ[A-Za-z0-9_\-]{20,1024}\.[A-Za-z0-9_\-]{20,256}"),
    ("dropbox-token", r"sl\.[A-Za-z0-9_\-]{130,256}"),
    ("frameio-token", r"fio-u-[A-Za-z0-9_\-=]{64}"),
    ("clojars-token", r"CLOJARS_[a-z0-9]{60}"),
    ("typeform-token", r"tfp_[A-Za-z0-9_\-.=]{59}"),
    ("cloudflare-ca-key", r"v1\.0-[0-9a-f]{24}-[0-9a-f]{146}"),
    ("braintree-token", r"access_token\$production\$[0-9a-z]{16}\$[0-9a-f]{32}"),
    ("easypost-key", r"EZ[AT]K[A-Za-z0-9]{54}"),
    ("resend-key", r"re_(?=[A-Za-z]{0,7}[0-9])[A-Za-z0-9]{8}_(?=[A-Za-z]{0,31}[0-9])[A-Za-z0-9]{20,32}(?![A-Za-z0-9])"),
    ("heroku-key", r"HRKU-[A-Za-z0-9_\-]{60,128}"),
    ("netlify-token", r"nfp_[A-Za-z0-9]{36}"),
    ("render-key", r"rnd_[A-Za-z0-9]{32}"),
    ("circleci-token", r"CCIPAT_[A-Za-z0-9]{22}_[0-9a-f]{40}"),
    ("jfrog-token", r"AKCp[A-Za-z0-9]{69}|cmVmdGtuOjAxOj[A-Za-z0-9]{40,128}"),
    ("asana-token", r"[12]/[0-9]{10,20}(?:/[0-9]{10,20})?:[0-9a-f]{32}"),
    ("facebook-token", r"EAA[A-Za-z0-9]{100,512}"),
    ("elevenlabs-key", r"sk_[0-9a-f]{48}(?![0-9a-f])"),
    ("supabase-key", r"sbp_[0-9a-f]{40}|sb_secret_[A-Za-z0-9_\-]{20,128}"),
    ("neon-key", r"napi_[a-z0-9]{64}"),
    ("brevo-key", r"xkeysib-[0-9a-f]{64}-[A-Za-z0-9]{16}"),
    ("huggingface-org-token", r"api_org_[A-Za-z0-9]{34}"),
    ("pinecone-key", r"pcsk_[A-Za-z0-9]{5,16}_[A-Za-z0-9]{40,128}"),
]


def _vendor_repl(m: re.Match) -> str:
    return f"[REDACTED:{_VENDOR_NAMES[m.lastgroup]}]" if m.lastgroup else m.group(0)


def _literal_prefix(rx: str) -> tuple[str, str]:
    """(the literal characters a token regex starts with, the rest of it): `gsk_[A-Za-z0-9]{40,64}` -> (`gsk_`,
    `[A-Za-z0-9]{40,64}`). A character a quantifier applies to stays in the rest."""
    lit: list[tuple[str, int]] = []
    i = 0
    while i < len(rx):
        if rx[i] == "\\" and i + 1 < len(rx) and rx[i + 1] in ".$-_":
            lit.append((rx[i + 1], i))
            i += 2
            continue
        if rx[i] in "[(.{?*+|^$\\":
            break
        lit.append((rx[i], i))
        i += 1
    if lit and i < len(rx) and rx[i] in "{?*+":
        i = lit.pop()[1]
    return "".join(c for c, _ in lit), rx[i:]


def _trie(entries: list[tuple[str, str]]) -> str:
    """One alternation of the tokens arranged as a trie of their literal prefixes, so a position is tested against the
    few tokens that share its first characters instead of every token."""
    leaves, by_first = [], {}
    for lit, rest in entries:
        if lit:
            by_first.setdefault(lit[0], []).append((lit[1:], rest))
        else:
            leaves.append(rest)
    alts = leaves + [re.escape(c) + (f"(?:{_trie(sub)})" if len(sub) > 1 else _trie(sub))
                     for c, sub in sorted(by_first.items())]
    return "|".join(alts)


def _top_alternatives(rx: str) -> list[str]:
    """`a(?:b|c)|d` -> [`a(?:b|c)`, `d`]: split at the `|` outside groups and character classes."""
    out, depth, cls, start, i = [], 0, False, 0, 0
    while i < len(rx):
        c = rx[i]
        if c == "\\":
            i += 2
            continue
        if cls:
            cls = c != "]"
        elif c == "[":
            cls = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            out.append(rx[start:i])
            start = i + 1
        i += 1
    return out + [rx[start:]]


GLUE_PREFIX_MIN = 4
VENDOR_GLUE_NO: set[str] = set()      # vendor entries with a short prefix: a word boundary before them, `_` included
_VENDOR_NAMES, _vendor_entries = {}, []
for _i, (_name, _rx) in enumerate(_VENDOR_TOKENS):
    for _j, _alt in enumerate(_top_alternatives(_rx)):
        _lit, _rest = _literal_prefix(_alt)
        _VENDOR_NAMES[f"v{_i}_{_j}"] = _name
        if len(_lit) < GLUE_PREFIX_MIN:
            # (a prefix of fewer than four characters, or none, is no token's own: not after `_` either)
            _rest = f"(?<![A-Za-z0-9_+/\\-]{'.' * len(_lit)})" + _rest
            VENDOR_GLUE_NO.add(_name)
        _vendor_entries.append((_lit, f"(?P<v{_i}_{_j}>{_rest})"))
_rule("vendor-token", r"(?<![A-Za-z0-9+/\-])(?:" + _trie(_vendor_entries) + ")", _vendor_repl)
# Terraform Cloud `<id>.atlasv1.<secret>`: the part after atlasv1 (the id before it is not secret on its own)
_rule("terraform-token", r"(?<=\.)atlasv1\.[A-Za-z0-9_\-]{60,128}")


def _discord_bot_repl(m: re.Match) -> str:
    """A Discord bot token's first part is the base64 of the bot's numeric id: only such a value is replaced, so a
    dotted name of the same shape stays."""
    import base64
    import binascii
    first = m.group(0).split(".", 1)[0]
    try:
        raw = base64.urlsafe_b64decode(first + "=" * (-len(first) % 4)).decode("ascii")
    except (binascii.Error, ValueError):
        return m.group(0)
    return "[REDACTED:discord-bot-token]" if raw.isdigit() and 15 <= len(raw) <= 21 else m.group(0)


_rule("discord-bot-token", r"(?<![\w\-.])[MNO][A-Za-z0-9_\-]{19,31}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,40}(?![\w\-])",
      _discord_bot_repl)
# the key of a Sentry-style DSN (`https://<32 hex>[:<32 hex>]@o1.ingest.sentry.io/2`): the user part of the URL
_rule("dsn-key", r"(?<=://)[0-9a-f]{32}(?::[0-9a-f]{32})?(?=@)")
_rule("huggingface-token", r"\bhf_[A-Za-z0-9]{30,}")
_rule("npm-token", r"\bnpm_[A-Za-z0-9]{36}\b")
_rule("jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")
_rule("bearer", r"(?i)\b(bearer|basic)([ \t]{1,16})(?!\[REDACTED)((?:[A-Za-z0-9._~+/\-]|%[0-9A-Fa-f]{2}){16,}=*)",
      r"\1\2[REDACTED:bearer]")
_rule("url-credentials", r"(?<![A-Za-z0-9+.\-])([a-z][a-z0-9+.\-]{0,31}://[^\s:/@\"']{0,256}):((?:[^\s@/\"';]|;(?![A-Za-z][\w ]{0,63}=)){3,256})@",
      r"\1:[REDACTED:url-credentials]@")

_PLACEHOLDER = re.compile(
    r"^(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\{\{.*\}\}|<[^>]*>|%[sd]|\*+|x+|X+|\.\.\.|…|"
    r"(?:your|my|the)[_\- ]?(?:[a-z]{1,16}[_\- ]){0,3}(?:api[_\- ]?)?(?:key|token|secret|password|pass|pwd|value|id|name|key[_\- ]?here)"
    r"(?:[_\- ]?here)?|changeme|change[_\- ]me|example\w*|placeholder|dummy|test|none|null|"
    r"true|false|redacted.*|\([a-z][a-z \-]{0,30}\)|\[REDACTED.*|"
    r"\{[A-Za-z_]\w{0,64}\}|%env\([\w.:\-]{1,128}\)%|\(\([ \t]?[\w.\-]{1,128}[ \t]?\)\))$", re.I)   # (Symfony `%env(X)%`, Concourse `((x))`)
# documentation of a flag (`curl -u user:password`, `--token TOKEN`), not a value
_FLAG_WORD = {"password", "passwd", "pass", "pwd", "secret", "token", "value", "key", "apikey", "api_key"}


def _looks_generated(value: str) -> bool:
    """A value that looks like a credential rather than a word: 8+ characters with letters and digits."""
    return len(value) >= 8 and bool(re.search(r"\d", value)) and bool(re.search(r"[A-Za-z]", value))


# a shell word: unquoted characters, backslash escapes, '...' and "..." segments (`-p'a'\''b'`, `--password "a\"b"`)
_SHELL_VALUE = r"""(?:[^\s'"\\]|\\\S|'[^'\n]{0,4096}'|"(?:[^"\\\n]|\\.){0,4096}")"""
_SHELL_UNQUOTE = re.compile(r"""'([^'\n]*)'|"((?:[^"\\\n]|\\.)*)"|\\(\S)""")


def _shell_unquote(word: str) -> str:
    return _SHELL_UNQUOTE.sub(lambda q: q.group(1) if q.group(1) is not None else (
        re.sub(r"\\(.)", r"\1", q.group(2)) if q.group(2) is not None else q.group(3)), word)


_HARD_FLAG = re.compile(r"(?i)pass|pwd|secret|masterauth")


def _flag_repl(m: re.Match) -> str:
    value = m.group("value")
    literal = value.startswith("'")           # single quotes: `$` and `%` are characters, not a reference
    word = _shell_unquote(value) if any(c in value for c in "'\"\\") else value
    core = word.rstrip(".,;:!?)]}>`")  # documentation: `--password=...,`, `-p...)`, `--api-key=<value>`
    if not re.search(r"\w", core) or _PLACEHOLDER.match(word) or _PLACEHOLDER.match(core) or \
            core.lower() in _FLAG_WORD or (word.startswith(("{", "<")) and (not literal or _CODE_REF.match(word))) or (
            not literal and word.strip("\"'\\").startswith(("$", "%"))):     # (`\"$PW\"` inside JSON)
        return m.group(0)
    # `--token <value>` / `--password <value>` with a space: the next word is only taken as the secret when it
    # looks generated (`git commit -m "rotate --token handling"` and `mysql --password mydb` stay intact)
    if m.groupdict().get("space") and not _looks_generated(word) and not (
            word.isdigit() and len(word) >= 6 and _HARD_FLAG.search(m.group("pre"))):
        return m.group(0)       # (six or more digits after a password flag are one: `--requirepass 20240131`)
    return m.group("pre") + "[REDACTED:argument]"


# command-line secrets: mysql-family `-p<password>` (no space; a bare `-p` prompts), `--password=...`,
# `--token <value>` / `--api-key=<value>`, `curl -u user:password`
_rule("argument", r"(?i)(?P<pre>\b(?:mysql(?:dump|admin|import|show|check|slap)?|mariadb(?:-dump|-admin)?)\b"
      r"[^\n|;&]{0,300}?[ \t]-p)(?!\[REDACTED)(?=(?P<value>" + _SHELL_VALUE + r"+))(?P=value)", _flag_repl)
# a long flag whose name ends in a secret word: --password, --http-password, --proxy-pass, --client-secret, --api-key,
# --auth-token, --oauth2-bearer, --storepass (`--password-stdin` and `--token-file` end in another word)
_rule("argument", r"(?i)(?P<pre>(?<![\w-])--(?:[a-z0-9]{1,24}[-_]){0,4}(?:password|passwd|pass|pwd|token|api[-_]?key|"
      r"apikey|secret|bearer|storepass|keypass|requirepass|masterauth)(?:=|(?P<space>[ \t]))(?!-))(?!\[REDACTED)"
      r"(?=\S{3})(?=(?P<value>" + _SHELL_VALUE + r"+))(?P=value)", _flag_repl)
_rule("argument", r"(?P<pre>\bcurl\b[^\n|;&]{0,300}?[ \t](?:-u|--user)[ \t]*[\"']?[^\s:\"'$]{1,128}:)"
      r"(?!\[REDACTED)(?=(?P<value>[^\s\"']+))(?P=value)", _flag_repl)
# `docker login -u me -p <pw>` / `--password <pw>` / `--password=<pw>` (also podman); `--password-stdin` is not one
_rule("argument", r"(?i)(?P<pre>\b(?:docker|podman)[ \t]+login\b[^\n|;&]{0,300}?[ \t](?:-p|--password)(?:=|[ \t]+))"
      r"(?!\[REDACTED)(?!-)(?=(?P<value>" + _SHELL_VALUE + r"+))(?P=value)", _flag_repl)
# commands whose short flag (or option word) takes a password: `redis-cli -a`, `sshpass -p`, `sqlcmd -P`, `az -p`,
# `keytool -storepass`, `ldapsearch -w`, `mongosh -p`, `zip -P`, `openssl ... pass:<pw>`, `gh secret set X --body`,
# `vault login <token>`, `smbclient -U user%pw`, `sqlplus user/pw@db`
_CMD_FLAGS = [
    (r"redis-cli", r"-a|--pass"), (r"sshpass", r"-p"), (r"sqlcmd|bcp|osql|isql", r"-P"), (r"az", r"-p"),
    (r"keytool|jarsigner", r"-(?:src|dest)?(?:storepass|keypass)"),
    (r"ldap(?:search|add|modify|delete|passwd|whoami|compare|modrdn|exop)", r"-w"),
    (r"mongo|mongosh|mongodump|mongorestore|mongoexport|mongoimport|mongostat|mongotop", r"-p"),
    (r"zip|unzip|zipcloak", r"-P"), (r"gh[ \t]+secret[ \t]+set", r"-b|--body"),
    (r"htpasswd", r"-(?=[a-zA-Z]{0,5}n)[a-zA-Z]{0,4}b[a-zA-Z]{0,4}(?:[ \t]+-[a-zA-Z]{1,5}){0,3}[ \t]+\S{1,128}|"
                  r"-[a-zA-Z]{0,4}b[a-zA-Z]{0,4}(?:[ \t]+-[a-zA-Z]{1,5}){0,3}[ \t]+\S{1,256}[ \t]+\S{1,128}"),
    (r"mysqladmin", r"password"), (r"rabbitmqctl", r"(?:add_user|change_password)[ \t]+\S{1,128}"),
    (r"steamcmd", r"\+login[ \t]+\S{1,128}"), (r"net[ \t]+use", r"\\\\\S{1,256}"),
    (r"cqlsh|cypher-shell|hdbsql|vsql|clickhouse-client", r"-p|--password"), (r"mosquitto_(?:pub|sub|rr)", r"-P"),
    (r"snmp\w{0,16}", r"-[AX]"), (r"ssh-keygen", r"-[NP]"),
]
_rule("argument", r"(?P<pre>(?:" + "|".join(
    f"(?:{cmd})\\b[^\\n|;&]{{0,300}}?[ \\t](?:{flags})" for cmd, flags in _CMD_FLAGS) +
    r")(?:=|[ \t]+))(?!\[REDACTED)(?![-$%{<])(?=(?P<value>" + _SHELL_VALUE + r"+))(?P=value)", _flag_repl)
_rule("argument", r"(?P<pre>openssl\b[^\n|;&]{0,300}?\bpass:)(?!\[REDACTED)(?![$%{<])(?=(?P<value>[^\s'\"]+))(?P=value)",
      _flag_repl)
_rule("argument", r"(?P<pre>vault[ \t]+login[ \t]+(?:-[\w-]{1,32}(?:=\S{1,256})?[ \t]+){0,4})(?!\[REDACTED)(?![-$%{<])"
      r"(?=(?P<value>[A-Za-z0-9._\-]{20,512}))(?P=value)", _flag_repl)
_rule("argument", r"(?P<pre>(?:smbclient|rpcclient|smbmap|net[ \t]+rpc)\b[^\n|;&]{0,300}?[ \t](?:-U|--user)[ \t]*[\"']?"
      r"[^\s%\"']{1,128}%)(?!\[REDACTED)(?=(?P<value>[^\s\"']+))(?P=value)", _flag_repl)
_rule("argument", r"(?P<pre>(?:sqlplus|sqlldr|expdp|impdp|rman)\b[ \t]+(?:-\w{1,16}[ \t]+){0,3}[\w$#]{1,30}/)"
      r"(?!\[REDACTED)(?=(?P<value>[^\s@/\"']{1,128}))(?P=value)(?=@)", _flag_repl)


def _config_set_repl(m: re.Match) -> str:
    """`aws configure set aws_secret_access_key <v>`, `git config --global github.token <v>`, `npm config set
    //host/:_authToken <v>`: the value is replaced when the setting's own name is a secret one."""
    name = re.split(r"[.:/]", m.group("name").strip("\"'"))[-1]
    strength = _secret_name(name)
    if not strength or _BENIGN_NAME.search(name):
        return m.group(0)
    return _flag_repl(m)


_rule("argument", r"(?P<pre>(?:aws[ \t]+configure[ \t]+set|(?:npm|pnpm|yarn|conda)[ \t]+config[ \t]+set|"
      r"dotnet[ \t]+user-secrets[ \t]+set|git[ \t]+config(?:[ \t]+--(?:global|local|system|worktree|add|replace-all))"
      r"{0,2})[ \t]+[\"']?(?P<name>[^\s\"'=]{1,256})[\"']?[ \t]+[\"']?)(?!\[REDACTED)(?![-$%{<])"
      r"(?=(?P<value>[^\s\"']{1,4096}))(?P=value)", _config_set_repl)

# netrc: `machine <host> [login <user>] password <pw>` (on one line or several), and the `default` entry
_rule("netrc", r"(?P<pre>\b(?:machine[ \t]+\S{1,256}|default)(?:\s{1,64}(?:login|account)[ \t]+\S{1,256}){1,2}\s{1,64}password[ \t]+)"
      r"(?!\[REDACTED)(?=(?P<value>\S{1,512}))(?P=value)", r"\g<pre>[REDACTED:netrc]")


def _generated_repl(kind: str):
    """Replace the `value` group when it looks generated (letters and digits, 8+ characters) and is no placeholder:
    `X-API-Key: your-api-key-here` and `Authorization: Token based auth` stay as they are."""
    def repl(m: re.Match) -> str:
        value = m.group("value")
        # (a header value of 24+ letters in both cases and no word separator is a key too: some vendors use no digits)
        letters = kind == "header" and len(value) >= 24 and value.isalpha() and not value.islower() and \
            not value.isupper()
        if not (_looks_generated(value) or letters) or _PLACEHOLDER.match(value):
            return m.group(0)
        if kind == "query-secret" and len(value) < 16 and re.fullmatch(r"[a-z0-9]+", value):
            return m.group(0)    # `?sig=newsletter2024`: a short lowercase word with digits, not a signature or key
        return m.group("pre") + f"[REDACTED:{kind}]"
    return repl


def _docker_auth_repl(m: re.Match) -> str:
    """`"auth": "<base64 of user:password>"` (docker config.json, .npmrc `_auth=`): only a value that decodes to
    printable text holding a colon, so `"auth": "required"` or `"auth": "oauth2"` stay."""
    import base64
    import binascii
    value = m.group("value")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), validate=True).decode("ascii")
    except (binascii.Error, ValueError):
        return m.group(0)
    if ":" not in raw or not raw.isprintable():
        return m.group(0)
    return m.group("pre") + "[REDACTED:basic-auth]"


# HTTP API-key headers, also inside `curl -H '...'` (the value must look generated)
# (a header value is any visible text up to a blank or the quote of the string it sits in: `[`, `(`, `^` are part of it)
_rule("header", r"(?i)(?P<pre>(?<![\w-])(?:x-api-key|api-key|x-auth-token|x-access-token|private-token|x-api-token)"
      r"[ \t]{0,16}:[ \t]{0,16})(?!\[REDACTED)(?=(?P<value>[^\s\"'`\\<]{8,512})(?P<more>[^\s\"'`\\<]*))(?P=value)(?P=more)",
      _generated_repl("header"))
# `Authorization: Token <hex>` and other schemes the bearer rule does not cover (`Token`, `Digest`, `ApiKey`, none)
_rule("header", r"(?i)(?P<pre>\bauthorization[ \t]{0,16}:[ \t]{0,16}(?:(?:token|apikey|api-key|key|digest|ssws)[ \t]{1,16})?)"
      r"(?!\[REDACTED)(?=(?P<value>[^\s\"'`\\,<]{8,512})(?P<more>[^\s\"'`\\,<]*))(?P=value)(?P=more)",
      _generated_repl("header"))   # (to a blank, a quote, a `<` (markup) or a `,` before the next parameter)
# any header whose name ends in a secret word (`Ocp-Apim-Subscription-Key`, `X-Figma-Token`, `X-Amz-Security-Token`,
# `DD-API-KEY`, `X-Auth-Key`, `X-Shopify-Access-Token`), when the value looks generated; a header-style name has a dash
_HEADER_NOT_SECRET = re.compile(r"(?i)^(?:idempotency|sec-websocket|cache|partition|sort|routing|public|x-request)-")
_HEADER_NAME = re.compile(r"(?:[A-Za-z0-9]{1,32}-){1,6}$")


def _named_header_repl(m: re.Match) -> str:
    """The header's name is the run of `word-` pieces before the matched secret word (one to six of them)."""
    start = m.start()
    head = m.string[max(0, start - 200):start + 1]
    h = _HEADER_NAME.search(head)
    if not h:
        return m.group(0)
    name_start = max(0, start - 200) + h.start()
    if name_start > 0 and (m.string[name_start - 1].isalnum() or m.string[name_start - 1] in "_-") or \
            _HEADER_NOT_SECRET.match(m.string[name_start:start + 1]):
        return m.group(0)
    return _generated_repl("header")(m)


_rule("header", r"(?i)(?P<pre>-(?:token|key|secret|signature|password|apikey|credentials?|session)"
      r"[ \t]{0,8}:[ \t]{0,8})(?!\[REDACTED)(?=(?P<value>[A-Za-z0-9._~+/\-]{12,4096}=*)(?P<more>[^\s\"'`\\]*))"
      r"(?P=value)(?P=more)", _named_header_repl)
_COOKIE_ATTRS = {"path", "domain", "expires", "max-age", "samesite", "secure", "httponly", "priority", "partitioned",
                 "version", "comment"}


def _cookie_repl(m: re.Match) -> str:
    """`Cookie: a=1; sid=<v>`, `Set-Cookie: sid=<v>; Path=/`, `curl -b "sid=<v>"`: each value that looks generated
    (letters and digits, 8+ characters, or 20+ characters) is replaced; attributes and short words stay."""
    def one(p: re.Match) -> str:
        name, value = p.group("n"), p.group("v")
        if name.strip().lower() in _COOKIE_ATTRS or _PLACEHOLDER.match(value) or value.startswith("[REDACTED"):
            return p.group(0)
        if _looks_generated(value) or len(value) >= 20:
            return f"{name}=[REDACTED:cookie]"
        return p.group(0)
    return m.group("pre") + re.sub(r"(?P<n>[^=;,\s]{1,256})=(?P<v>[^;,\s]{1,4096})", one, m.group("body"))


_rule("cookie", r"(?i)(?P<pre>cookie(?<!\wcookie)[ \t]{0,8}:[ \t]{0,8})(?=(?P<body>[^\r\n\"']{1,8192}))(?P=body)",
      _cookie_repl)
_rule("cookie", r"(?P<pre>curl\b[^\n|;&]{0,300}?[ \t](?:-b|--cookie)[ \t]+[\"']?)(?=(?P<body>[^\r\n\"']{1,8192}))(?P=body)",
      _cookie_repl)
# URL query parameters: an Azure SAS `sig=` and API keys (`appid=`, `api_key=`, ...) when the value looks generated
_rule("query-secret", r"(?i)(?P<pre>[?&](?:sig|appid|app_id|api_key|apikey|api-key|access_token|auth_token|"
      r"client_secret|private_token)=)(?!\[REDACTED)(?=(?P<value>[^&;\s\"'<>\\]{8,2048}))(?P=value)",   # (to `&`)
      _generated_repl("query-secret"))
_QUERY_SECRET_NAMES = {"sig", "signature", "x-amz-signature", "x-goog-signature", "x-amz-security-token", "code_verifier"}


def _query_repl(m: re.Match) -> str:
    """`?password=...`, `&X-Amz-Security-Token=...`, form bodies `user=u&password=...`, `;password=...` (JDBC, ADO and
    Npgsql connection strings): a parameter whose name is a secret one. A hard secret name (password, secret, pwd, salt)
    takes any value; others (token, key) one that looks generated. The value ends where its format ends one: `&` (and
    `;`) after `?`/`&`, a `#` is part of it (a form body's value; a URL fragment after a secret goes with it); `;` in a
    connection string, where a value may be quoted (`"a""b"`, `'a''b'`) and holds anything else."""
    gd = m.groupdict()
    name = gd["name"] or gd["name2"]
    value = gd["value"] if gd["value"] is not None else gd["value2"]
    pre = gd["pre"] or gd["pre2"]
    tail = ""
    if gd["value"] is not None:
        tail = _unbalanced_closers(value)       # `[link](https://x/?password=...)`: the `)` closes the link
        value = value[:len(value) - len(tail)]
    low = name.lower()
    if low not in _QUERY_SECRET_NAMES:
        if not _secret_name(name) or (_BENIGN_NAME.search(name) and not _HARD_SECRET.search(name)):
            return m.group(0)
    if _PLACEHOLDER.match(value) or value.startswith(("{", "$", "%7B", "%7b", "<", "[REDACTED")) or len(value) < 4 \
            or _code_value(value):     # `;password=getpw()`: code, not a URL
        return m.group(0)
    if not _HARD_SECRET.search(name) and (not (_looks_generated(value) or len(value) >= 16) or (
            len(value) < 16 and re.fullmatch(r"[a-z0-9]+", value))):
        return m.group(0)     # `?sig=newsletter2024`: a short lowercase word with digits is no signature or key
    return pre + "[REDACTED:query-secret]" + tail


# a connection-string value after `;name=`: quoted with `"` or `'` (the quote doubled inside), or bare up to `;`, a blank
# or the end of the line; a quote inside a bare value is part of it unless it ends the string the connection string
# sits in (`"...;Password=x"}` in JSON, `"...;Password=x",`)
_CONN_VALUE = (r"\"(?:[^\"\r\n]|\"\"){1,4096}\"|'(?:[^'\r\n]|''){1,4096}'|"
               r"(?:[^;\s\"\\`]|\\(?![\"\\])|\"(?![ \t]*(?:[,}\]:;\r\n]|\Z))|`(?![`\s,)}\];:.!?]|\Z)){1,4096}")
_rule("query-secret", r"(?:(?P<pre>[?&](?P<name>[A-Za-z][\w.\-]{0,63})=)(?!\[REDACTED)"
      r"(?=(?P<value>[^&;\s\"'<>\\]{1,4096}))(?P=value)"
      r"|(?P<pre2>;[ \t]{0,8}(?P<name2>[A-Za-z][\w.\-]{0,63})[ \t]{0,8}=[ \t]{0,8})(?!\[REDACTED)"
      r"(?=(?P<value2>" + _CONN_VALUE + r"))(?P=value2))", _query_repl)
# Azure storage / service bus connection strings: AccountKey=...; SharedAccessKey=...; SharedAccessSignature=...
_rule("connection-key", r"(?i)(?P<pre>(?<![\w-])(?:AccountKey|SharedAccessKey|SharedAccessSignature)[ \t]{0,16}="
      r"[ \t]{0,16})(?!\[REDACTED)(?![<$%{])(?=(?P<value>[^;\s\"'<>]{8,4096}))(?P=value)", r"\g<pre>[REDACTED:connection-key]")
# .npmrc `//registry/:_authToken=...`, `:_password=...` and `_auth=` lines
_rule("npmrc-token", r"(?im)(?P<pre>(?:(?<=:)|^)_(?:authToken|password)[ \t]{0,16}=[ \t]{0,16})(?!\[REDACTED)(?![$<{%])"
      r"(?=(?P<value>[^\s\"']{6,4096}))(?P=value)", r"\g<pre>[REDACTED:npmrc-token]")
_rule("basic-auth", r"(?im)(?P<pre>(?:(?<=:)|^)_auth[ \t]{0,16}=[ \t]{0,16})(?=(?P<value>[A-Za-z0-9+/]{8,4096}={0,2}))"
      r"(?P=value)(?![A-Za-z0-9+/=])", _docker_auth_repl)
_rule("basic-auth", r"(?P<pre>\"auth\"\s{0,64}:\s{0,64}\")(?=(?P<value>[A-Za-z0-9+/]{8,4096}={0,2}))(?P=value)(?=\")",
      _docker_auth_repl)
# `NPM_CONFIG__AUTH=<base64 of user:password>` and other names ending in _auth (only a value that decodes to user:pw)
_rule("basic-auth", r"(?P<pre>_(?:auth|AUTH|Auth)(?<=\w_[Aa][Uu][Tt][Hh])[ \t]{0,16}[=:][ \t]{0,16}[\"']?)"
      r"(?=(?P<value>[A-Za-z0-9+/]{8,4096}={0,2}))(?P=value)(?![A-Za-z0-9+/=])", _docker_auth_repl)


_CONN_CONTEXT = re.compile(r"(?i)\b(?:server|host|data source|user id|uid|user|username|database|initial catalog|"
                           r"address|port)[ \t]*=")
_conn_line = threading.local()   # the last line judged: (text object, line start, line end, verdict)


def _conn_line_verdict(m: re.Match) -> bool:
    """Is the line holding this match a connection string? Decided once per line: matches arrive in order, so the
    verdict of the line the previous match was on is reused (judging the whole line again for every match made a
    long line of `pwd=x;` quadratic)."""
    s, pos = m.string, m.start()
    last = getattr(_conn_line, "v", None)
    if last is not None and last[0] is s and last[1] <= pos < last[2]:
        return last[3]
    start = s.rfind("\n", 0, pos) + 1
    end = s.find("\n", pos)
    end = len(s) if end < 0 else end
    verdict = _CONN_CONTEXT.search(s, start, end) is not None
    _conn_line.v = (s, start, end, verdict)
    return verdict


def _conn_repl(m: re.Match) -> str:
    """`Password=...;` inside a connection string (Server=...;User Id=...;Password=...;)."""
    value = m.group("value").rstrip()
    if not _CONN_VALUE_END.match(m.string, m.end()) or not _conn_line_verdict(m):
        return m.group(0)  # not a connection string: `password = make(len);` in code
    closers = _unbalanced_closers(value)
    value = value[:len(value) - len(closers)]
    if not value or _PLACEHOLDER.match(value.strip("\"'")) or value.startswith(("$", "%", "{", "[REDACTED")) or \
            _code_value(value) or (closers and _IDENTIFIER.match(value) and not re.search(r"\d", value)):
        return m.group(0)        # `connect(host=host, user=user, password=password)`: a call's argument
    return m.group("pre") + "[REDACTED:assignment]" + m.group("value")[len(value):]


_rule("assignment", r"(?i)(?<![\w.\-])(?P<pre>(?:password|passwd|pwd)[ \t]{0,16}=[ \t]{0,16})"
      r"(?=(?P<value>" + _CONN_VALUE + r"))(?P=value)", _conn_repl)
_CONN_VALUE_END = re.compile(r"[ \t]*(?:;|\r?\n|\Z)")    # (checked in _conn_repl: a failing lookahead after a long value
#                                                           made `pwd={pwd={...` quadratic)


def _conn_brace_repl(m: re.Match, value: str) -> str:
    """ODBC `PWD={pa;ss}` (braces quote a value holding `;`) inside a connection string."""
    if not _conn_line_verdict(m) or _PLACEHOLDER.match(value) or value.startswith(("$", "%")):
        return m.group(0)
    return m.group("pre") + "[REDACTED:assignment]"


# (a `}}` inside the braces is a literal `}`: `PWD={a}}b}` holds `a}b`)
_rule("assignment", r"(?i)(?<![\w.\-])(?P<pre>(?:password|passwd|pwd)[ \t]{0,16}=[ \t]{0,16}\{)"
      r"(?:(?P<value>(?:[^}\r\n]|\}\}){1,256}?)(?=\}(?!\}))|(?=(?P<v2>[^}\r\n]{1,256}))(?P=v2)(?=\}))",
      lambda m: _conn_brace_repl(m, m.group("value") if m.group("value") is not None else m.group("v2")))


# ---- name-driven rules for config shapes the assignment rules below do not see. Each takes the secret-ness of a name
# from _judge (the same name and value tests as the assignment rules) and replaces only the value.

def _judge(name: str, value: str, env: bool = True, min_len: int = 4) -> bool:
    """Is `value` a credential, given the name it is stored under? The name must be a secret one (_secret_name, not a
    benign one such as `password_hint` or `token_count`) and the value no placeholder, colour, version, path or URL."""
    key = _last_segment(name)
    strength = _secret_name(key, env=env)
    hard = _HARD_SECRET.search(key)
    if not strength or (_BENIGN_NAME.search(key) and not (hard and _SOFT_SUFFIX.search(key))):
        return False
    v = value.strip()
    if len(v) < min_len or _PLACEHOLDER.match(v) or v.startswith("[REDACTED") or (v[0] in "$%{<!" and (
            _CODE_REF.match(v) or not _looks_generated(v) or " " in v)) or \
            _COLOR.match(v) or _VERSION.match(v) or v.lower() == key.lower():
        return False
    if strength == "key" and not (len(v) >= 12 and re.search(r"\d", v) and re.search(r"[A-Za-z]", v)):
        return False
    return not (re.match(r"^(?:[/~.]|[A-Za-z]:[\\/])[\w./\\\-]*$", v) and _path_shaped(key, v) or "://" in v or
                _not_a_credential(key, v))


def _path_shaped(key: str, value: str) -> bool:
    """A value that starts like a path (`/`, `~`, `.`, `C:\\`) is one when a separator follows inside it (`/run/secrets/db`,
    `~/.pgpass`, `./key.pem`) or when it is no generated-looking word under a hard secret name: a dot or a tilde
    followed by a generated-looking word under `DB_PASSWORD` is a password (`/tmp`, `.env` and `~` stay paths)."""
    if re.search(r"[/\\]", value[1:]) or (len(value) > 1 and value[1] in "./~:") or value[:1].isalpha():
        return True
    return not (_HARD_SECRET.search(key) and len(value) >= 9 and _looks_generated(value[1:]))


def _value_repl(kind: str = "assignment", generated: bool = False, symbol: bool = False):
    """Replace the `value` group when _judge(name, value) holds; `generated`: it must also look generated (letters and
    digits); `symbol`: it must hold a digit or a character other than a letter (a plain word is a label)."""
    def repl(m: re.Match) -> str:
        value = m.group("value")
        if not _judge(m.group("name"), value) or (generated and not _looks_generated(value)) or \
                (symbol and not re.search(r"[^A-Za-z ]", value)):
            return m.group(0)
        return m.string[m.start():m.start("value")] + f"[REDACTED:{kind}]" + m.string[m.end("value"):m.end()]
    return repl


_FOREIGN_RX = re.compile("(?i)" + _FOREIGN_PASSWORD)
_SESSION_COOKIE = re.compile(r"(?i)^(?:j?sessionid|phpsessid|asp\.net_sessionid|connect\.sid|sid|session|"
                             r"[\w.\-]{0,32}[_.\-]sess(?:ion)?(?:id)?|_?\w{0,32}_session|\.?roblosecurity)$")


def _name_value_repl(m: re.Match) -> str:
    """A name/value pair: the value under a secret name, or under a session cookie's name when it looks generated. A
    quoted value is the text between its quotes (escapes included); a bare one runs to a blank, less a trailing `,` and
    closing brackets it does not open (`{name: X, value: Y}`). A path-like name is judged by its last part (SSM
    `/prod/db/DB_PASSWORD`)."""
    gd = m.groupdict()
    vg = next(g for g in ("value", "value2", "value3") if gd.get(g) is not None)
    value = gd[vg]
    if vg == "value3":
        value = value.rstrip(",")
        value = value[:len(value) - len(_unbalanced_closers(value))].rstrip(",")
        if not value:
            return m.group(0)
    name = (m.group("name") or m.group("name2")).rstrip("/").rsplit("/", 1)[-1]
    head, tail = m.string[m.start():m.start(vg)], m.string[m.start(vg) + len(value):m.end()]
    if _SESSION_COOKIE.match(name) and (_looks_generated(value) or len(value) >= 24) and \
            not _PLACEHOLDER.match(value) and not value.startswith("[REDACTED"):
        return head + "[REDACTED:cookie]" + tail
    if not _judge(name, value) or not _pair_secret_name(name) or (value.isalpha() and value.islower() and
                                                                  len(value) < 12) or \
            (" " in value.strip() and (not _HARD_SECRET.search(name) or _LABEL_WORDS.search(value) or
                                       len(value.split()) > 4)):
        return m.group(0)       # (`password_reset` / `done`, a sentence: an event log, not a credential)
    return head + "[REDACTED:assignment]" + tail


_PAIR_LAST = {"password", "passwd", "pass", "pwd", "secret", "token", "apikey", "passphrase", "passcode", "psk", "dsn",
              "salt", "credential", "credentials", "pat"}


def _pair_secret_name(name: str) -> bool:
    """The name of a name/value pair ends in a secret word (`DB_PASSWORD`, `DbPassword`, `client_secret`, `API_KEY`):
    `password_reset`, `api_key_check`, `token_count` name events or counts."""
    segs = _segments(_last_segment(name))
    if not segs:
        return False
    last = segs[-1]
    return last in _PAIR_LAST or (last == "key" and len(segs) >= 2) or (last == "string" and segs[-2:-1] == ["connection"]) \
        or _FOREIGN_RX.search(last) is not None


_LIST_TAGS = {"add", "entry", "property", "param", "parameter", "string", "setting", "value", "item", "env", "var",
              "variable", "secret", "config"}


def _xml_repl(m: re.Match) -> str:
    """`<password>...</password>`, `<entry key="password">...</entry>`, `<string name="api_key">...</string>`."""
    tag, value = m.group("tag").rsplit(":", 1)[-1], m.group("value")
    name = tag
    if tag.lower() in _LIST_TAGS:
        a = re.search(r"(?i)\b(?:key|name)[ \t]{0,8}=[ \t]{0,8}[\"']([^\"'<>]{1,128})[\"']", m.group("attrs") or "")
        if not a:
            return m.group(0)
        name = a.group(1)
    v = value.strip()
    if " " in v and "\n" not in v and (not _HARD_SECRET.search(_last_segment(name)) or _LABEL_WORDS.search(v)):
        return m.group(0)       # `<token>This is a note about tokens</token>`: a sentence, not a value
    if not v or not _judge(name, value):
        return m.group(0)
    return m.string[m.start():m.start("value")] + _lines_replaced(value) + m.string[m.end("value"):m.end()]


_LINE_DECORATION = re.compile(r"(?=(?P<b1>[ \t]*))(?P=b1)[+\->!]?(?=(?P<b2>[ \t]*))(?P=b2)\Z")   # (runs taken whole)


def _lines_replaced(value: str) -> str:
    """The replacement of a value that may span lines (an XML element whose value is on lines of its own, a TOML
    multi-line string): its lines are emptied, so the text keeps its line count, and the indentation or diff marker
    before the closing delimiter on the last line stays (`+  </password>`)."""
    if "\n" not in value:
        return "[REDACTED:assignment]"
    last = value[value.rfind("\n") + 1:]
    return "[REDACTED:assignment]" + "\n" * value.count("\n") + (last if _LINE_DECORATION.match(last) else "")


# XML elements: `<password>...</password>` (Maven settings.xml, NuGet, Ant), list-style tags named by an attribute
_rule("assignment", r"<(?P<tag>[A-Za-z_][\w.:\-]{0,63})(?P<attrs>[ \t\r\n][^<>]{0,256})?>(?!\[REDACTED)"
      r"(?=(?P<value>[^<>]+))(?P=value)</(?P=tag)>", _xml_repl)    # (the value whole, whatever its length: each run
# between `>` and `<` is read once, from the tag before it)
# a name and a value side by side: XML `<add key="DbPassword" value="..."/>`, JSON `{"name": "DB_PASSWORD", "value":
# "..."}` (app settings, SSM, GitHub API), YAML `- name: DB_PASSWORD` / `  value: ...` (Kubernetes env)
# (between them up to six other items of one object, whatever their keys (SSM's `Type`, `KeyId`, `LastModifiedDate`,
# Azure's `slotSetting`, `tags`, a key of your own), each a key of up to 64 characters other than `value`, `name` and
# `key` and a short value (a quoted string or a word of up to 256 characters, `[]` or `{}`), separated by `,` or a line
# break; a
# bounded, atomic scan: round 15 compared it with the earlier list of metadata keys and it changed no further line of
# corpus2, corpus3, the v12 corpus, the stdlib, conda YAML, this repository or its history; Terraform's
# `"db_password": {"sensitive": true, "type": "string", "value": X}` names the pair by its key. A quoted value to its
# closing quote within 8 KB, all or nothing; a bare value to a blank)
_PAIR_META = r"(?:(?!(?:value|name|key)[\"']?[ \t]{0,8}[:=])[\w.\-]{1,64})"   # (a `name` or `key` item starts a
#                                                                              pair of its own: the scan stops there)
_rule("assignment", r"(?i)(?:(?:name(?<![\w-]name)|key(?<![\w-]key))[\"']?[ \t]{0,8}[:=][ \t]{0,8}(?P<q1>[\"']?)"
      r"(?=(?P<name>[\w.:/\-]{1,128}))(?P=name)(?P=q1)|\"(?P<name2>[\w.\-]{1,128})\"[ \t]{0,8}:[ \t]{0,8}\{)"
      r"(?=(?P<meta>(?:[ \t]{0,8},?[ \t\r\n]{0,64}[\"']?" + _PAIR_META + r"[\"']?[ \t]{0,8}:[ \t]{0,8}"
      r"(?:\"[^\"\r\n]{0,256}\"|[\w.:/\-]{1,256}|\[\]|\{\})){0,6}))(?P=meta)"     # (atomic: the name and the items are each read
      #                                                                  once; no backtracking into them)
      r",?[ \t\r\n]{0,64}(?:-[ \t]{1,8})?[\"']?value[\"']?[ \t]{0,8}[:=][ \t]{0,8}(?!\[REDACTED)"
      r"(?:\"(?=(?P<value>(?:[^\"\\\r\n]|\\.){1,8192}))(?P=value)\"|'(?=(?P<value2>(?:[^'\r\n]|''){1,8192}))(?P=value2)'"
      r"|(?![\"'])(?=(?P<value3>\S+))(?P=value3))", _name_value_repl)
# Terraform marks a value it holds as secret itself: `"sensitive": true` before `"type"` and `"value"` in one object
# (`terraform output -json`, a state's `outputs`): the value is replaced whatever its name: a quoted value to its quote
# within 8 KB, a number, or a list or map of one level (no `[`/`]` or `{`/`}` inside it) within 8 KB, its line breaks
# kept. The type between them is a quoted name or a bracketed list of one level within 1 KB (`["list", "string"]`,
# printed over lines). A value or type nested deeper is not matched (a listed gap; Redactor._valued replaces any value
# of a parsed structure)
def _sensitive_repl(m: re.Match) -> str:
    value = m.group("value")
    if value.startswith(("[", "{")):
        return m.group("pre") + value[0] + "\"[REDACTED:assignment]\"" + "\n" * value.count("\n") + value[-1]
    return m.group("pre") + "\"[REDACTED:assignment]\""


_rule("assignment", r"(?P<pre>\"sensitive\"[ \t]{0,8}:[ \t]{0,8}true[ \t]{0,8},[ \t\r\n]{0,64}(?:\"type\"[ \t]{0,8}:"
      r"[ \t]{0,8}(?:\"[^\"\r\n]{0,256}\"|\[[^\[\]]{0,1024}\])[ \t]{0,8},[ \t\r\n]{0,64})?\"value\"[ \t]{0,8}:[ \t]{0,8})"
      r"(?!\"\[REDACTED|\[\"\[REDACTED)(?=(?P<value>\"(?:[^\"\\\r\n]|\\.){1,8192}\"|\[[^\[\]]{1,8192}\]|\{[^{}]{1,8192}\}"
      r"|-?\d[\d.eE+\-]{0,64}))(?P=value)", _sensitive_repl, need=("sensitive",))
# the same with `"sensitive": true` after the value (a state file writes `"value"`, `"type"`, `"sensitive"` in that
# order): the type, if any, between them
_rule("assignment", r"(?P<pre>\"value\"[ \t]{0,8}:[ \t]{0,8})(?!\"\[REDACTED|\[\"\[REDACTED)"
      r"(?=(?P<value>\"(?:[^\"\\\r\n]|\\.){1,8192}\"|\[[^\[\]]{1,8192}\]|\{[^{}]{1,8192}\}|-?\d[\d.eE+\-]{0,64}))(?P=value)"
      r"(?=[ \t]{0,8},[ \t\r\n]{0,64}(?:\"type\"[ \t]{0,8}:[ \t]{0,8}(?:\"[^\"\r\n]{0,256}\"|\[[^\[\]]{0,1024}\])[ \t]{0,8},"
      r"[ \t\r\n]{0,64})?\"sensitive\"[ \t]{0,8}:[ \t]{0,8}true\b)", _sensitive_repl, need=("sensitive",))
# An Azure Key Vault secret as `az keyvault secret show` prints it: `"value"` in the object whose `"id"` is
# `https://<vault>.vault.azure.net/secrets/...`. The id is looked for within 2 KB before the value and within 2 KB after
# it, as an item of the same object (not inside an object or list nested in it: `"tags": {"value": ...}` is no secret's
# value) (_key_vault_repl)
_KEY_VAULT_ID = re.compile(r"https://[\w\-]{1,128}\.vault\.(?:azure\.net|azure\.cn|usgovcloudapi\.net|"
                           r"microsoftazure\.de)/secrets/")
_KEY_VAULT_ID_ITEM = re.compile(r"\"id\"[ \t]{0,8}:[ \t]{0,8}\"" + _KEY_VAULT_ID.pattern)


def _key_vault_repl(m: re.Match) -> str:
    start, s = m.start(), m.string
    window = s[max(0, start - 2048):start]
    last = None
    for last in _KEY_VAULT_ID_ITEM.finditer(window):
        pass
    if last is not None and _same_object(window[last.end():].split('"', 1)[-1]):     # (after the id's closing quote)
        return m.group("pre") + "[REDACTED:assignment]\""
    after = s[m.end():m.end() + 2048]
    for nxt in _KEY_VAULT_ID_ITEM.finditer(after):
        if _same_object(after[:nxt.start()]):
            return m.group("pre") + "[REDACTED:assignment]\""
        break
    return m.group(0)


def _same_object(between: str) -> bool:
    """Do two items with `between` between them belong to one JSON object (no bracket opened or closed between them
    at their level: a quoted string's brackets do not count)?"""
    depth, i, n = 0, 0, len(between)
    while i < n:
        c = between[i]
        if c == '"':
            j = i + 1
            while j < n and between[j] != '"':
                j += 2 if between[j] == "\\" else 1
            i = j + 1
            continue
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0


_rule("assignment", r"(?P<pre>\"value\"[ \t]{0,8}:[ \t]{0,8}\")(?!\[REDACTED)(?=(?P<value>(?:[^\"\\\r\n]|\\.){1,8192}))"
      r"(?P=value)\"", _key_vault_repl, need=(".vault.",))
# credentials passed to a call: `auth=("user", "pw")`, `HTTPBasicAuth("u", "pw")`, `server.login("me", "pw")`,
# `new NetworkCredential("u", "pw")`, `UsernamePasswordCredentials("u", "pw")`
_rule("assignment", r"(?i)(?P<pre>(?:auth|credentials?|login)\w{0,32}[ \t]{0,8}(?:=[ \t]{0,8})?[(\[][ \t]{0,8}"
      r"(?P<q1>[\"'])[^\"'\n]{1,128}(?P=q1)[ \t]{0,8},[ \t]{0,8}(?P<q>[\"']))(?!\[REDACTED)"
      r"(?=(?P<value>[^\"'\s]{4,256}))(?P=value)(?P=q)",
      lambda m: m.group(0) if _PLACEHOLDER.match(m.group("value")) or m.group("value").startswith(("$", "%", "{")) or not (
          re.search(r"[^A-Za-z]", m.group("value")) or len(m.group("value")) >= 12)
      else m.group("pre") + "[REDACTED:assignment]" + m.group("q"))
# PowerShell `ConvertTo-SecureString "<pw>" -AsPlainText`
_rule("argument", r"(?i)(?P<pre>\bConvertTo-SecureString\b(?:[ \t]+-String)?[ \t]+(?P<q>[\"']))(?!\[REDACTED)"
      r"(?=(?P<value>[^\"'\r\n]{1,512}))(?P=value)(?P=q)",
      lambda m: m.group(0) if m.group("value").startswith(("$", "%", "{")) or _PLACEHOLDER.match(m.group("value"))
      else m.group("pre") + "[REDACTED:argument]" + m.group("q"))
# a quoted name and a quoted value as two arguments: PHP `define('DB_PASSWORD', '...')`, `os.environ.setdefault("API_KEY",
# "...")`, `config.set("password", "...")` (the value must hold a digit or a symbol: `("password", "Password")` is a label;
# it runs to its own quote: a backslash escape such as `\'` and the other quote character are part of it)
_rule("assignment", r"(?P<q1>[\"'])(?=(?P<name>[A-Za-z_][\w.\-]{0,127}))(?P=name)(?P=q1)[ \t]{0,8},[ \t]{0,8}(?P<q>[\"'])"
      r"(?!\[REDACTED)(?=(?P<value>(?:[^\"'\s\\]|\\\S|(?!(?P=q))[\"']){6,512}))(?P=value)(?P=q)", _value_repl(symbol=True))
# Terraform `variable "db_password" { default = "..." }`
_rule("assignment", r"\bvariable[ \t]+\"(?P<name>[\w\-]{1,128})\"[ \t]*\{(?=(?P<body>[^{}]{0,1024}?\bdefault[ \t]*=[ \t]*\"))"
      r"(?P=body)(?!\[REDACTED)(?=(?P<value>[^\"\r\n]{1,512}))(?P=value)(?=\")", _value_repl())
# YAML block scalar under a secret name (`password: |` and the indented lines below it)
# (a list item `- password: |`: the block is indented past the key, not past the dash, so its sibling keys end it;
# a ` # comment` after the indicator is allowed)
_BLOCK_IN = r"(?P=lead)(?(d1)[ \t]{2}|(?(d2)[ \t]{3}|(?(d3)[ \t]{4})))[ \t]{1,64}"


def _block_repl(m: re.Match) -> str:
    value = m.group("value")
    lines = [ln.strip() for ln in value.split("\n") if ln.strip()]
    name = m.group("name")
    if not lines or not (_judge(name, lines[0]) or (len(lines) > 1 and _judge(name, _PROBE_VALUE))):
        return m.group(0)       # (a first line that is a placeholder keeps only a one-line block)
    return m.string[m.start():m.start("value")] + "\n" + m.group("lead") + "  [REDACTED:assignment]" + \
        "\n" * (value.count("\n") - 1)      # the value's lines become empty: the text keeps its line count


# the block scalar header as YAML writes it: an anchor `&a` and a tag `!t`/`!!binary` in either order (each at most once),
# the indicator `|` or `>`, then an indentation digit and a chomping sign in either order (`|2-`, `>-2`, `|+`)
_BLOCK_HEAD = (r"(?:(?:&[\w.\-]{1,128}|!{1,2}[\w.\-/:]{0,128})[ \t]{1,8}){0,2}"
               r"[|>](?:[1-9][+-]?|[+-][1-9]?)?")
_rule("assignment", r"(?m)^(?P<lead>[ \t]{0,64})(?:(?P<d1>-[ \t](?![ \t]))|(?P<d2>-[ \t]{2}(?![ \t]))|"
      r"(?P<d3>-[ \t]{3}(?![ \t])))?(?P<name>[\w.\-]{1,128})[ \t]{0,8}:[ \t]{0,8}" + _BLOCK_HEAD +
      r"[ \t]{0,8}(?:#[^\r\n]{0,256})?"
      r"(?=(?P<value>(?:\r?\n" + _BLOCK_IN + r"[^\r\n]*|(?:\r?\n[ \t]*)+\r?\n" + _BLOCK_IN + r"\S[^\r\n]*)+))"
      r"(?P=value)", _block_repl)  # (the whole block, to the end of its indentation; a run of blank lines inside it is
#                                    taken with the line after it, so each line is read once)
# TOML multi-line string: `api_key = '''...'''` / `"""..."""`
def _name_before(s: str, pos: int, quote: bool = True) -> str | None:
    """The name a separator at `pos` belongs to: `name = ` / `"name": ` (spaces and a closing quote skipped), at most
    128 characters of [\\w.-] with no such character before it; None when there is none."""
    i = pos
    while i > 0 and pos - i < 64 and s[i - 1] in " \t":
        i -= 1
    if quote and i > 0 and s[i - 1] in "\"'":
        i -= 1
    j = i
    while j > 0 and i - j < 128 and (s[j - 1].isalnum() or s[j - 1] in "_.-"):
        j -= 1
    if j == i or (j > 0 and (s[j - 1].isalnum() or s[j - 1] in "_.-")):
        return None
    return s[j:i]


_PROSE_LINE = re.compile(r"[^():=\[\]{};<>\"'`]*")
_WORDS = re.compile(r"[A-Za-z][a-z]{1,30}[.,!?]?")      # (a word of a sentence: no digits, no capitals inside)


def _prose_lines(value: str) -> bool:
    """A multi-line string that is clearly prose (the one decline of the triple-quote rule, a documented gap): three or
    more non-empty lines, each of four or more words and no code punctuation. Key material, a passphrase of two lines
    and code are not."""
    lines = [ln.strip() for ln in value.split("\n") if ln.strip()]
    return len(lines) >= 3 and all(_PROSE_LINE.fullmatch(ln) and sum(1 for w in ln.split() if _WORDS.fullmatch(w)) >= 4
                                   for ln in lines)


def _named_value_repl(m: re.Match) -> str:
    name = _name_before(m.string, m.start())
    if name is None or not _judge(name, m.group("value")) or _prose_lines(m.group("value")):
        return m.group(0)
    return m.string[m.start():m.start("value")] + _lines_replaced(m.group("value")) + m.string[m.end("value"):m.end()]


_rule("assignment", r"=[ \t]{0,8}(?P<q>'''|\"\"\")(?!\[REDACTED)(?=[ \t]*\r?\n|[^\s\"'`])"
      r"(?=(?P<value>[^\"']{1,4096}))(?P=value)(?P=q)",   # (within 4 KB or not at all; not a mention: `api_key = \"\"\"`)
      _named_value_repl)

def _spanning_repl(m: re.Match) -> str:
    """A value that runs over lines (a quoted scalar that does not close on its line, a backslash continuation): judged
    by the name before it and its first part (a first part another rule already replaced, read on one line, was judged
    then), redacted whole with its line breaks kept; the decoration that starts the last line (a diff marker) stays."""
    value = m.group("value")
    first = value.split("\n", 1)[0].rstrip("\\\r").strip()
    if not (first.startswith("[REDACTED") and _secret_name(_last_segment(m.group("name")))) and \
            not _judge(m.group("name"), first or _PROBE_VALUE):
        return m.group(0)
    if m.groupdict().get("q"):
        # a quoted scalar folds its line breaks into blanks: judged as the one-line value it is, a sentence or a label
        # (more than eight blanks, or a label word: "Your password must be / at least 12 characters") is kept, as
        # the passphrase rule keeps it on one line
        joined = " ".join(ln.strip() for ln in value.split("\n") if ln.strip())
        if joined.count(" ") > 8 or _LABEL_WORDS.search(joined):
            return m.group(0)
    last = value[value.rfind("\n") + 1:]
    lead = last[:len(last) - len(last.lstrip(" \t+-!>"))][:16]
    return m.group("pre") + "[REDACTED:assignment]" + "\n" * value.count("\n") + lead + m.string[m.end("value"):m.end()]


# a quoted value that does not close on its own line (a YAML double- or single-quoted scalar folded over lines:
# `password: "abc` / `  def"`, a dotenv `KEY="...` over lines): the name starts its line, the value runs to its closing
# quote within 4 KB, and the line ends there (or a `,`, `}`, `]` or `#` follows). It stops at the first quote of either
# kind, so the scan from one opening quote never passes the next quote: linear
_rule("assignment", r"(?m)(?P<pre>^[ \t]{0,64}(?:[+\-!>][ \t]{0,8})?(?:export[ \t]{1,8})?(?P<qn>[\"']?)(?P<name>[\w.\-]{1,128})"
      r"(?P=qn)[ \t]{0,8}[:=][ \t]{0,8}(?P<q>[\"']))"
      r"(?=(?P<value>[^\"'\n\x00]{0,4096}\n[^\"'\x00]{1,4096})(?P=q)[ \t]{0,64}(?:[,}\]#\r]|$))(?P=value)(?P=q)",
      _spanning_repl)     # (no value crosses a NUL: pieces redacted together are separated by one, _PIECE_SEP)
# `NAME=value\` continued on the next lines (a shell or .env line continuation; also `NAME=\` with the value on the
# next line): the value with every continued line, at most 64 of them of 4 KB each. A line that starts like another
# setting (`NAME=`, `- NAME=`, `key: `, `[section]`, after a diff marker too) is no continuation: a value that only ends
# in a backslash, as a generated password can, leaves the next setting alone
_NOT_A_SETTING = (r"(?![ \t]{0,64}[+\-!>]?[ \t]{0,8}(?:-[ \t]{1,8})?(?:export[ \t]{1,8})?[A-Za-z_][\w.\-]{0,127}[ \t]{0,8}"
                  r"(?:=|:(?:[ \t]|\r?$))|[ \t]{0,64}\[)")
_rule("assignment", r"(?m)(?P<pre>^[ \t]{0,64}(?:[+\-!>][ \t]{0,8})?(?:export[ \t]{1,8})?(?P<name>[A-Za-z_][\w.]{0,127})"
      r"[ \t]{0,8}=[ \t]{0,8})"
      r"(?=(?P<value>(?:[^\s\\\"'][^\s\\]{0,4095})?\\\r?\n(?:" + _NOT_A_SETTING + r"[^\n\\\x00]{0,4096}\\\r?\n){0,64}"
      + _NOT_A_SETTING + r"[ \t]{0,64}[^\s\\\x00][^\n\x00]{0,4096}))(?P=value)", _spanning_repl)
# a quoted value with spaces under a hard secret name (a passphrase: `"passphrase": "correct horse battery staple"`); a
# sentence (`"password": "Enter your password"`) is a label, not one
_LABEL_WORDS = re.compile(r"(?i)\b(?:password|passphrase|passwd|secret|enter|must|your|least|characters?|invalid|"
                          r"required|incorrect|wrong|confirm|forgot|reset|new|old|current|please|show|hide|type|field|"
                          r"the|is|are|not|and|or|for|to|of)\b|[.!?:]\s*$")


def _passphrase_repl(m: re.Match) -> str:
    name = _name_before(m.string, m.start())
    if name is None:
        return m.group(0)
    key, value = _last_segment(name), m.group("value")
    if not _HARD_SECRET.search(key) or " " not in value.strip() or value.count(" ") > 8 or _LABEL_WORDS.search(value) \
            or not _judge(name, value, min_len=8):
        return m.group(0)
    return m.string[m.start():m.start("value")] + "[REDACTED:assignment]" + m.string[m.end("value"):m.end()]


_rule("assignment", r"(?::=|=>|[:=])[ \t]{0,64}(?P<q>[\"'])(?!\[REDACTED)(?=[^\"'\r\n\\]{0,255} )"
      r"(?=(?P<value>[^\"'\r\n\\]{1,256}))(?P=value)(?P=q)", _passphrase_repl)
# a Markdown table row `| API_TOKEN | <value> |` (the value must look generated: `| password | string |` is a type)
_rule("assignment", r"\|[ \t]{0,16}`?(?P<name>[\w.\-]{1,128})`?[ \t]{0,16}\|[ \t]{0,16}`?(?!\[REDACTED)"
      r"(?=(?P<value>[^|\s`]{6,512}))(?P=value)(?=`?[ \t]{0,16}\|)", _value_repl(generated=True))
# Dockerfile `ENV NAME value` / `ARG NAME value`; a dotted .properties name with a space as separator
# (`mail.smtp.password value`)
_rule("assignment", r"(?m)^[ \t]{0,64}(?:ENV|ARG)[ \t]{1,16}(?P<name>[A-Za-z_][\w.\-]{0,127})[ \t]{1,16}(?![\"'=])"
      r"(?=(?P<value>\S{4,4096}))(?P=value)(?=[ \t]*$)", _value_repl())
_rule("assignment", r"(?m)^[ \t]{0,64}(?P<name>[A-Za-z_][\w\-]{0,63}(?:\.[\w\-]{1,63}){1,8})[ \t]{1,16}(?![\"'=:])"
      r"(?=(?P<value>\S{6,4096}))(?P=value)(?=[ \t]*$)", _value_repl(generated=True))

# ---- round 7: shapes found by the verifier's corpus 6 (each generalised by a name or a command table, not one sample)

_OPTION_NOT_SECRET = re.compile(r"(?i)[-_.](?:file|stdin|fd|env|cmd|command|helper|prompt|path|dir|name|id|type|length|"
                                r"min|max|policy|hash|algo|algorithm|expiry|ttl|url|uri|header|count)$")


def _secret_option(name: str) -> bool:
    """`-Password`, `--passphrase`, `--spring.datasource.password`, `-storepass`: the option's last part is a secret
    word (`--password-stdin`, `--token-file`, `--secret-name`, `--max-tokens` are not)."""
    key = _last_segment(name)
    if _OPTION_NOT_SECRET.search(key) or not _secret_name(key):
        return False
    return not (_BENIGN_NAME.search(key) and not _HARD_SECRET.search(key))


_PROSE_NOT_SECRET = re.compile(r"^(?:[a-z0-9]+(?:[-_.][a-z0-9]+)*|[A-Z][a-z]+(?:[A-Z][a-z]+)+\d{0,4}|[\d.:/\-]+|"
                               r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+)$")
_INNER_SYMBOL = re.compile(r"[A-Za-z][#$%^&*!?+=~@][A-Za-z0-9]")


_PROSE_WORD = re.compile(r"(?i)[^\W\d]+(?:[ _\-]key)?")
_NON_BLANKS = re.compile(r"\S{0,64}")
_CAMEL_DIGITS = re.compile(r"[A-Z][a-z]+(?:[A-Z][a-z]+)+\d{1,4}")


# words of lower-case letters and digits that name a thing, not a credential (the stems of _NUM_SEG are checked by
# _digits_ok: `sha256`, `base64`, `utf8`, `md5`, `ipv4`, `x86`, `int64`); built from the words the benign corpora and the
# damage texts hold after a secret name (round 11)
_TECH_WORD = re.compile(r"^(?:oauth|http|https|python|py|cpython|pypy|node|nodejs|php|ruby|java|jdk|jre|es|ecma|"
                        r"ecmascript|win|windows|ubuntu|debian|centos|rhel|fedora|alpine|macos|ios|android|gpt|tls|tlsv|"
                        r"ssl|sslv|arm|armv|aarch|amd|rfc|iso|cve|argon|pbkdf|secp|prime|curve|bzip|lz|mp|vp|av|level|"
                        r"gen|dotnet|net|cuda|sm|opengl|gl|vulkan|directx|dx|scala|kotlin|swift|go|golang|rust|perl|lua|"
                        r"luau|cpp|gcc|clang|llvm|msvc|vs|vc|ext|ddr|pcie|usb|hdmi|lte|eth|wlan|tty|sd|hd|uhd|fhd|top|"
                        r"step|phase|stage|round|part|page|chapter|section|item|line|row|col|column|day|week|month|q|"
                        r"h|x|v|ver|version|rev|r|build|release|rc|beta|alpha|draft|ed|edition|sha|md|base|utf)"
                        r"\d{1,4}(?:[a-z]{1,2}\d{0,2})?$")
_LOWER_DIGITS = re.compile(r"[a-z0-9]{6,128}")
# what may stand between a secret name and its value in prose for _lower_digits_value: blanks, a verb of the prose rule
# or a colon, an opening quote (no other word: `token limit: 200000` names a count)
_PROSE_LINK = re.compile(r"(?i)[ \t]{0,8}(?:(?:is|was|are|is now|set to|changed to|ist|lautet|est|es|era|é|è|är|er|on|"
                         r"jest|je|zijn)\b[ \t]{0,8}:?|:)[ \t]{0,8}[\"'`]?")


def _lower_digits_value(v: str) -> bool:
    """A value of lower-case letters and digits (`summer2024`, `hunter22x`, `john1985`) or of six or more digits
    (`12345678`), as a secret name's value in prose or a flow map: not a technical word (_TECH_WORD, numeric type and
    algorithm names: _digits_ok)."""
    if not _LOWER_DIGITS.fullmatch(v) or _ANY_DIGIT.search(v) is None:
        return False
    if v.isdigit():
        return True
    if _digits_ok(v) or _TECH_WORD.match(v):
        return False
    return True


def _prose_repl(m: re.Match) -> str:
    """A secret named in a sentence: only a value that looks like a credential (letters and digits, not a lowercase
    word-with-dashes such as `sha256-based`, a CamelCase type, a date, an address, a path or a dotted name)."""
    value, more = m.group("value"), m.group("more")
    pre, s, end = m.group("pre"), m.string, m.end()
    if more:
        # a token longer than the cap (`token: <a 240-character key>`): judged by its first part and redacted whole, never
        # in part (fail closed)
        if value.startswith(("$", "%", "{", "<", "/", "~", "\\")) or "://" in value or _code_value(value):
            return m.group(0)
        return pre + "[REDACTED:assignment]"
    if pre[-1] in "\"'`" and end < len(s) and s[end] in "\"'`" and (
            s[end] != pre[-1] or value.endswith("\\") or s[end + 1:end + 2] == pre[-1]):
        return m.group(0)       # `"password": "ab'cd"`: the value goes on past the other quote (the quoted rule's)
    if pre[-1] not in "\"'`" and end < len(s) and s[end] in "\"'`" and \
            re.search(r"[A-Za-z0-9]", s[end + 1:(_NON_BLANKS.match(s, end + 1) or m).end()]):
        return m.group(0)       # `password: )diZL@W?"RkJl...`: the value goes on past the quote (the line rules' value;
        # a quote that closes the string the sentence sits in has only punctuation after it: `..."}`, `...",`)
    v = value.rstrip(".,;:)]}>")
    n = len(v)
    if n < 6 or v.startswith(("$", "%", "{", "<", "[REDACTED", "/", "~", "\\")) or "://" in v or _PLACEHOLDER.match(v) \
            or _VERSION.match(v):
        return m.group(0)
    word = _PROSE_WORD.match(pre)
    name = word.group(0).replace(" ", "_") if word else ""
    digit = re.search(r"\d", v) is not None
    alpha = re.search(r"[A-Za-z]", v) is not None
    inner = _INNER_SYMBOL.search(v) is not None
    strong = letters = False
    if word and _lower_digits_value(v) and _PROSE_LINK.fullmatch(pre, word.end()) and (
            not v.isdigit() or _HARD_SECRET.search(name) or name.lower().endswith("key")):
        # (only right after the name: `token limit: 200000`, `input token count: 153422` are counts; digits alone only
        # under a password, secret or API-key name: `token: 128000 used` is a count too)
        # lower case and digits, or six or more digits, under a secret name (`summer2024`, `hunter22x`, `12345678`): a
        # value, unless it is a technical word (`sha256`, `python3`, `oauth2`: _lower_digits_value)
        if _code_value(value.rstrip(".:")) or _chain_call(value):
            return m.group(0)
        return m.group("pre") + "[REDACTED:assignment]" + value[len(v):]
    if n < 8:
        # six or seven characters only with a digit or a symbol inside, letters, and mixed case or a symbol (`Qw3rty!`,
        # `Summ3r`, `G@l@xy`; not `Base64`)
        if not (name and (digit or inner) and alpha and (re.search(r"[^\w]", v) or (
                re.search(r"[A-Z]", v) and re.search(r"[a-z]", v))) and not (digit and _digits_ok(v))):
            return m.group(0)
    else:
        # a CamelCase word with digits that are no part of a type name (`DragonSilver7`; `Base64Encoder` is a name), or
        # letters with a symbol inside (`P@ssword!`, `Y^H^$-oEPL`; an address is no such value)
        strong = bool(name) and ((_CAMEL_DIGITS.fullmatch(v) is not None and not _digits_ok(v) and
                                  _ITEM3_NAME.search(name) is not None) or (inner and not _PROSE_NOT_SECRET.match(v)))
        if not strong and not (digit and alpha):
            # letters only: a credential right after "password is" / "password:" (not across other words: "a secret
            # word, with ...: PowerShell") when it looks like one (`CorrectHorse`: _letters_value)
            if not (word and len(pre) - word.end() <= 16 and _letters_value(v, name, prose=True) == 1):
                return m.group(0)
            letters = True
    if (not strong and not letters and _PROSE_NOT_SECRET.match(v) and n < 20) or \
            (_IDENTIFIER.match(v) and "." in v and _digits_ok(v)) or _code_value(value.rstrip(".:")) or \
            _known_type(value.rstrip(".,;:)")) or _chain_call(value):
        # (`password: z.string().min(8),` is a schema, `token: Option<Vec<u8>>,` a field: the type rules here are only
        # type syntax and built-in type names, never a class-like word, so `password: Hunter2pass;` stays a value)
        return m.group(0)
    if letters and _line_context(s, m.start()) == "code":
        return m.group(0)       # `  password: CorrectHorse;` in an interface: a typed field
    return m.group("pre") + "[REDACTED:assignment]" + value[len(v):]

_SQL_SECRET_KEYWORD = re.compile(r"(?i)(?:identified|set)[ \t]")


def _spaced_value_repl(m: re.Match) -> str:
    """A quoted value after a name and a space (no `=` or `:`): judged by the name, or after SQL's IDENTIFIED BY."""
    name, value = m.group("name"), m.group("value")
    if value.startswith(("$", ":", "%", "{", "?", "<", "@")) or _PLACEHOLDER.match(value) or \
            value.lower() in ("values", "password"):
        return m.group(0)
    # `IDENTIFIED BY '...'` and `SET PASSWORD = '...'` are the construct itself (their name holds a blank); any other word
    # is judged as a name (`SETTINGS_FILE "config/settings.yaml"`, `setup '_setup()'` are not secrets)
    if not _SQL_SECRET_KEYWORD.match(name) and not (_judge(name, value) and re.search(r"[^A-Za-z]", value)):
        return m.group(0)
    return m.string[m.start():m.start("value")] + "[REDACTED:assignment]" + m.string[m.end("value"):m.end()]


# SQL `CREATE USER x WITH PASSWORD '...'`, `ALTER ROLE x PASSWORD '...'`, `CREATE LOGIN x WITH PASSWORD = '...'`,
# `SET PASSWORD [FOR u] = PASSWORD('...')`, MySQL/Oracle `IDENTIFIED BY '...'` (also `IDENTIFIED WITH plugin BY '...'`), a
# Groovy or Ruby DSL `storePassword '...'`, strongSwan `: PSK "..."`: a secret name, a space and a quoted value
_rule("assignment", r"(?i)(?<![\w.\-])(?P<name>identified[ \t]{1,8}(?:with[ \t]{1,8}\w{1,64}[ \t]{1,8})?by|"
      r"set[ \t]{1,8}password(?:[ \t]{1,8}for[ \t]{1,8}[^\s=]{1,128})?|"
      r"(?=(?P<w>[A-Za-z_][\w.\-]{0,63}))(?P=w))[ \t]{1,16}(?:=[ \t]{0,16})?(?:password[ \t]{0,4}\([ \t]{0,4})?"
      r"(?P<q>['\"])(?!\[REDACTED)(?=(?P<value>[^'\"\s\\]{4,256}))(?P=value)(?P=q)", _spaced_value_repl)
# Oracle `CREATE USER x IDENTIFIED BY pw` / `ALTER USER x IDENTIFIED BY pw` (unquoted; only after a
# user name: "identified by INDEX" in prose is no password)
_rule("assignment", r"(?i)(?P<pre>\buser[ \t]{1,8}[^\s;'\"]{1,128}[ \t]{1,8}identified[ \t]{1,8}by[ \t]{1,16})"
      r"(?![\"'])(?!values\b|password\b|\[REDACTED)"
      r"(?=(?P<value>[^\s;\"'()]{4,256}))(?P=value)",
      lambda m: m.group(0) if m.group("value").startswith(("$", ":", "%", "{", "&")) or _PLACEHOLDER.match(m.group("value"))
      else m.group("pre") + "[REDACTED:assignment]")

_SETTER_VERBS = re.compile(r"^(?:set|with|put|use)(?=[A-Z_])")
_NOT_A_SETTER = (r"(?!(?:get|is|has|check|verify|validate|valid|hash|compare|encode|decode|encrypt|decrypt|reset|forgot|"
                 r"confirm|change|update|prompt|ask|read|load|fetch|find|lookup|log|print|show|hide|mask|clear|remove|"
                 r"delete|require|expect|generate|create|new|is)(?:[A-Z_]|\())")


def _setter_repl(m: re.Match) -> str:
    """`ds.setPassword("...")`, `.withSecret("...")`, `builder.password("...")`: the setter's name without set/with/put,
    judged like any name; the value must hold a digit or a symbol (a word is a label)."""
    name, value = _SETTER_VERBS.sub("", m.group("name")), m.group("value")
    if not _judge(name, value) or not re.search(r"[^A-Za-z]", value):
        return m.group(0)
    return m.string[m.start():m.start("value")] + "[REDACTED:assignment]" + m.string[m.end("value"):m.end()]


# a literal passed to a setter or builder method of a secret
_rule("assignment", r"(?:(?<=\.)" + _NOT_A_SETTER + r"|\b(?=(?:set|with|put|use)[A-Z_]))(?=(?P<name>[A-Za-z_]\w{0,63}))"
      r"(?P=name)[ \t]{0,4}\([ \t]{0,4}(?P<q>[\"'])(?!\[REDACTED)(?=(?P<value>[^\"'\s\\]{6,512}))(?P=value)(?P=q)"
      r"[ \t]{0,4}[,)]", _setter_repl)
# header-setting directives: nginx `proxy_set_header X-Api-Key "..."`, `add_header`, `fastcgi_param DB_PASSWORD ...`, Apache
# `RequestHeader set X-Api-Key ...`, `Header set`, `SetEnv DB_PASSWORD ...`, HAProxy `http-request set-header ...`
_rule("header", r"(?i)(?P<pre>\b(?:proxy_set_header|add_header|more_set_headers|grpc_set_header|fastcgi_param|uwsgi_param|"
      r"scgi_param|RequestHeader[ \t]{1,8}(?:set|append|add|merge)|Header[ \t]{1,8}(?:always[ \t]{1,8})?(?:set|append|add)|"
      r"http-(?:request|response)[ \t]{1,8}(?:set|add)-header|SetEnv|PassEnv)[ \t]{1,16}[\"']?"
      r"(?P<name>[A-Za-z0-9_\-]{1,128})[\"']?[ \t]{1,16}[\"']?)(?!\[REDACTED)(?=(?P<value>[^\s\"';]{8,4096}))(?P=value)",
      _value_repl("header", generated=True))


def _pgpass_repl(m: re.Match) -> str:
    v = m.group("value")
    if _PLACEHOLDER.match(v) or v.startswith(("$", "[REDACTED")) or not (
            _looks_generated(v) or (len(v) >= 8 and re.search(r"\d", v) and re.search(r"[^\w]", v))):
        return m.group(0)
    return m.group("pre") + "[REDACTED:assignment]"


# a .pgpass line `host:port:database:user:password` (a field may be `*`; `\:` escapes a colon)
_rule("assignment", r"(?m)^(?P<pre>[ \t]{0,64}(?:[\w.\-]{1,253}|\*|/[\w./\-]{0,253}):(?:\d{1,5}|\*):"
      r"(?:[^\s:\\]|\\.){1,128}:(?:[^\s:\\]|\\.){1,128}:)(?!\[REDACTED)(?=(?P<value>\S{4,256}))(?P=value)"
      r"(?=[ \t]*(?:[ \t]#[^\n]*)?$)", _pgpass_repl)
# Microsoft Teams / Office 365 incoming webhooks
_rule("webhook", r"(?:(?<=\.webhook\.office\.com/)webhookb2|(?<=outlook\.office\.com/)webhook)/(?!\[REDACTED)"
      r"[A-Za-z0-9@/\-]{40,512}")
# Roblox's session cookie (.ROBLOSECURITY): a warning text, then the token
_rule("cookie", r"_\|WARNING:-DO-NOT-SHARE-THIS\.[^|\s]{0,256}\|_(?!\[REDACTED)[A-Za-z0-9]{40,8192}")
# a password piped into a command that reads it from stdin: `echo pw | sudo -S`, `| chpasswd`, `| docker login
# --password-stdin`, `| passwd --stdin`, `| docker secret create`, `| kinit`, `| smbpasswd -s`, `| gh auth login --with-token`
_STDIN_SECRET = (r"sudo\b[^\n|;&]{0,64}?[ \t]-[a-zA-Z]{0,4}S\b|[\w.\-]{1,32}\b[^\n|;&]{0,200}?--password-stdin\b|"
                 r"passwd\b[^\n|;&]{0,64}?--stdin\b|chpasswd\b|chpass\b|kinit\b|docker[ \t]+secret[ \t]+create\b|"
                 r"gh[ \t]+auth[ \t]+login\b[^\n|;&]{0,64}?--with-token\b|smbpasswd\b[^\n|;&]{0,64}?[ \t]-s\b|"
                 r"gcloud[ \t]+secrets\b[^\n|;&]{0,200}?--data-file=-|vault[ \t]+login\b[^\n|;&]{0,64}?[ \t]-(?![\w-])")
_rule("argument", r"(?P<pre>\b(?:echo|printf)[ \t]{1,8}(?:-[neE]{1,3}[ \t]{1,8})?(?:(?P<fq>[\"'])%s(?:\\n)?(?P=fq)[ \t]{1,8})?"
      r"(?P<q>[\"']?))(?!\[REDACTED)(?![-$%{<])(?=(?P<value>[^\s\"'|]{4,256}))(?P=value)(?P=q)"
      r"(?=[ \t]{0,8}\|[ \t]{0,8}(?:" + _STDIN_SECRET + "))", _flag_repl)
# long and single-dash options whose name ends in a secret word, with a value that looks generated: PowerShell and
# NuGet `-Password X`, `x11vnc -passwd X`, `influx -password X`, `gpg --passphrase X`, `--spring.datasource.password=X`
_rule("argument", r"(?i)(?P<pre>(?<![\w\-])--?(?=(?P<name>[a-z][\w.\-]{0,63}))(?P=name)(?P<space>[ \t]{1,8}|[:=]))"
      r"(?!-)(?!\[REDACTED)(?=\S{3})(?=(?P<value>" + _SHELL_VALUE + r"{1,512}))(?P=value)",
      lambda m: _flag_repl(m) if _secret_option(m.group("name")) else m.group(0))
# Windows `/pass:X` (cmdkey), `/password:X`, `/rp X` (schtasks run-as password)
_rule("argument", r"(?i)(?P<pre>(?<![\w/:.\\])/(?:pass|password|passwd|pwd|rp)(?P<space>:|[ \t]{1,8}))(?!\[REDACTED)"
      r"(?=(?P<value>[^\s/\"']{4,256}))(?P=value)", _flag_repl)
# `-u user:password` / `--user user:password` of any command (curl has its own rule): only a value that looks generated
# (`docker run -u 1000:1000` and `-u root:root` are ids)
_rule("argument", r"(?P<pre>(?<![\w\-])(?:-u|--user|--username)(?P<space>=|[ \t]{1,8})[\"']?[^\s:\"'$]{1,128}:)"
      r"(?!\[REDACTED)(?=(?P<value>[^\s\"']{4,256}))(?P=value)", _flag_repl)
# commands whose argument right after an option is the password: `7z a -pX`, `unrar x -pX`, `lftp -u user,X`
_rule("argument", r"(?P<pre>(?:\b(?:7z|7za|7zr|rar|unrar)\b[^\n|;&]{0,300}?[ \t]-p|\blftp\b[^\n|;&]{0,300}?[ \t]-u[ \t]{1,8}"
      r"[^\s,]{1,128},))(?!\[REDACTED)(?![-$%{<])(?=(?P<value>[^\s'\"]{4,256}))(?P=value)", _flag_repl)


def _kv_repl(m: re.Match, key: str) -> str:
    """`consul kv put service/db/password X`, `pulumi config set --secret dbPassword X`, `az keyvault secret set --name
    db-password --value X`, `aws ssm put-parameter --name /prod/db/password --value X`: the key's last part names it."""
    name = re.split(r"[/:.]", key.strip("\"'").rstrip("/"))[-1]
    value = m.group("value")
    if not _judge(name, value) or value.startswith(("-", "file://", "fileb://", "@")):
        return m.group(0)
    return m.group("pre") + "[REDACTED:argument]"


_rule("argument", r"(?P<pre>(?:\b(?:consul[ \t]+kv[ \t]+put|etcdctl[ \t]+put|pulumi[ \t]+config[ \t]+set|"
      r"(?:firebase[ \t]+)?functions:config:set)(?:[ \t]+--?[\w\-]{1,32}){0,3}[ \t]+[\"']?(?P<name>[^\s\"'=]{1,256})[\"']?"
      r"[ \t]+|--name[ \t=]+[\"']?(?P<n2>[^\s\"']{1,256})[\"']?[^\n|;&]{0,200}?[ \t]--(?:value|secret-string)(?:=|[ \t]+))"
      r"[\"']?)(?!\[REDACTED)(?![-$%{<])(?=(?P<value>[^\s\"']{1,4096}))(?P=value)",
      lambda m: _kv_repl(m, m.group("name") or m.group("n2")))
# a config line `password <value>` (msmtp, `requirepass`, `masterauth`; a netrc entry without `machine`): a value with
# a digit or a symbol (`password required` is a sentence)
_rule("assignment", r"(?im)^(?P<pre>[ \t]{0,64}(?:password|passwd|passphrase|requirepass|masterauth|psk|"
      + _FOREIGN_PASSWORD + r")[ \t]{1,16})(?!\[REDACTED)(?![\"'=:])(?=(?P<value>[^\s\"']{4,512}))(?P=value)(?=[ \t]*$)",
      lambda m: m.group(0) if _PLACEHOLDER.match(m.group("value")) or m.group("value").startswith(("$", "%", "<", "{"))
      or not re.search(r"[^A-Za-z]", m.group("value")) or _known_type(m.group("value"))   # (Go `Password *string`)
      else m.group("pre") + "[REDACTED:assignment]")
# postfix sasl_passwd `[smtp.host]:587 user:password`
_rule("assignment", r"(?m)^(?P<pre>[ \t]{0,64}\[[\w.\-]{1,253}\](?::\d{1,5})?[ \t]{1,16}[^\s:]{1,256}:)(?!\[REDACTED)"
      r"(?=(?P<value>\S{4,256}))(?P=value)(?=[ \t]*$)", _pgpass_repl)
# Redis ACL `ACL SETUSER alice on >password`, redis.conf `user alice on >password`
_rule("argument", r"(?im)(?P<pre>(?:\bACL[ \t]+SETUSER|^[ \t]{0,64}user)[ \t]+\S{1,128}[ \t]+(?:on|off|reset)\b"
      r"[^\n>]{0,300}?[ \t]>)(?!\[REDACTED)(?=(?P<value>\S{1,512}))(?P=value)", r"\g<pre>[REDACTED:argument]")
# Go MySQL DSN `user:password@tcp(host:3306)/db` (no scheme)
_rule("url-credentials", r"(?<![\w:/@.\[])(?P<pre>[\w.\-]{1,64}:)(?!\[REDACTED)(?=(?P<value>[^\s@/\"':\]]{3,256}))(?P=value)"
      r"(?=@(?:tcp|unix|udp)\()", r"\g<pre>[REDACTED:url-credentials]")
# a secret named in prose: "the staging db password is X", "Das Passwort ... lautet: X", "here is the deploy token: X",
# "le mot de passe est X" (the value must look like a credential: _prose_secret); the words between stay inside one
# sentence and one string (no backslash: a JSON `\n`, no `", ` between JSON strings)
_rule("assignment", r"(?i)(?P<pre>\b(?:passwords?|passwd|passphrase|passcode|pwd|secret|api[ _\-]?key|access[ _\-]?key|"
      r"token|" + _FOREIGN_PASSWORD + r")\b(?:[^\n.!?\\\"]|\"(?![ \t]{0,8}[,:\]}]))" r"{0,40}?(?:\b(?:is|was|are|is now|set to|changed to|ist|lautet|est|es|"
      r"era|é|è|är|er|on|jest|je|zijn)\b[ \t]{0,8}:?|:)[ \t]{1,8}[\"'`]?)(?!\[REDACTED)"
      r"(?=(?P<value>[^\s\"'`]{6,128})(?P<more>[^\s\"'`]*))(?P=value)(?P=more)", lambda m: _prose_repl(m))
# a row of a two-column name/value table (PowerShell `Get-ChildItem env:` and Format-Table, `env` in columns, `kubectl
# get secret -o custom-columns=...`): a first column that is a secret name, two or more blanks, and a second column of one
# word that holds a digit or a symbol; a row with more columns (`kubectl get secrets`: NAME TYPE DATA AGE) or a value
# with blanks (a description) is no such row, and a file name is no secret name (`password_reset.py  1.2 KB`)
_TABLE_REPL = _value_repl(symbol=True)
_CONSTANT_NAME = re.compile(r"[A-Z][A-Z]{1,31}(?:_[A-Z]{1,31}){1,8}")


def _table_repl(m: re.Match) -> str:
    """A table row's value: a type by its syntax or a built-in type name stays (Go `Secret []byte`, `Token *string`); a
    word of letters only is a value only when it looks like one (_letters_value) and the rows around it are no code."""
    value = m.group("value")
    if _known_type(value) or _CONSTANT_NAME.fullmatch(value):
        return m.group(0)       # (`token\tTOKEN_TYPE_ACCESS`: an enum constant)
    if value.isalpha():
        if _letters_value(value, m.group("name")) != 1 or _line_context(m.string, m.start()) == "code":
            return m.group(0)
        return _value_repl()(m)
    return _TABLE_REPL(m)


_rule("assignment", r"(?m)^(?P<pre>[ \t]{0,8}(?P<name>[A-Za-z_][\w.\-]{0,127})(?:[ \t]{2,64}|\t))(?![\"'=:|])(?!\[REDACTED)"
      r"(?=(?P<value>[^\s\"']{6,4096}))(?P=value)(?=[ \t]*\r?$)",
      lambda m: _table_repl(m))
# `terraform plan`: `~ master_password = "old" -> "new"` (the old value is the quoted rule's; the new one is this)
_rule("assignment", r"(?<![\w.\-])(?=(?P<name>[\w.\-]{1,128}))(?P=name)[ \t]{1,16}=[ \t]{1,16}"
      r"(?:\"[^\"\n]{0,4096}\"|\([\w ]{1,32}\)|[^\s\"]{1,4096})"
      r"[ \t]{1,16}->[ \t]{1,16}\"(?!\[REDACTED)(?=(?P<value>[^\"\n]{1,4096}))(?P=value)\"", _value_repl())
# a number under a secret name in JSON: `{"password": 84736251}`, `"db_password": 120934`
_rule("assignment", r"\"(?P<name>[\w.\-]{1,128})\"[ \t]{0,16}:[ \t]{0,16}(?=(?P<value>\d{6,64}))(?P=value)"
      r"(?=[ \t]*[,}\r\n])", _value_repl())
# a `name | value` row (psql's expanded display `\x`, a two-column table without outer pipes): blanks around the pipe
# (`a|b` is code) and one word of value with a digit or a symbol
_rule("assignment", r"(?m)^(?P<pre>[ \t]{0,8}(?P<name>[A-Za-z_][\w.\-]{0,127})[ \t]{1,16}\|[ \t]{1,16})(?!\[REDACTED)"
      r"(?=(?P<value>[^\s|\"']{6,512}))(?P=value)(?=[ \t]*\r?$)", _table_repl)
# round 10 (verifier v9b item 4): typed forms and printed structures
# C/C++ `char password[] = "..."`, `static const char API_KEY[32] = "..."` (a quoted literal into an array)
def _named_before_repl(m: re.Match) -> str:
    """The value of a match that starts at a bracket or brace after its name (the scan skips to that character; the
    name is read back by _name_before): judged like any name/value pair."""
    name = _name_before(m.string, m.start(), quote=False)
    if not name or not _judge(name, m.group("value")):
        return m.group(0)
    return m.string[m.start():m.start("value")] + "[REDACTED:assignment]" + m.string[m.end("value"):m.end()]


_rule("assignment", r"\[[ \t]{0,4}\w{0,32}[ \t]{0,4}\][ \t]{0,8}=[ \t]{0,8}(?:L|u8|u|U)?\"(?!\[REDACTED)"
      r"(?=(?P<value>(?:[^\"\\\n]|\\.){4,4096}))(?P=value)\"", _named_before_repl)
# a C# auto-property with an initializer: `public string Password { get; set; } = "..."`
_rule("assignment", r"\{[ \t]{0,8}(?:get|init|set)\b[^{}\n]{0,64}\}[ \t]{0,8}=[ \t]{0,8}\"(?!\[REDACTED)"
      r"(?=(?P<value>(?:[^\"\\\n]|\\.){4,4096}))(?P=value)\"", _named_before_repl)
# a default in a placeholder: Spring `${db.password:literal}`, shell and compose `${DB_PASSWORD:-literal}` /
# `${DB_PASSWORD:=literal}` (`${X:?message}` is an error message, `${X:+alt}` an alternative)
_rule("assignment", r"\$\{(?=(?P<name>[\w.\-]{1,128}))(?P=name):[-=]?(?![?+])(?!\[REDACTED)"
      r"(?=(?P<value>[^}\s\"'$]{4,512}))(?P=value)\}", _value_repl())


def _printed_field_repl(m: re.Match) -> str:
    """Go `%+v` (`{Host:db Password:X}`) and `%v` of a map (`map[password:X user:admin]`): only inside such a structure
    on the line, and not a pointer (`0xc000...`)."""
    s, a = m.string, m.start() + 1        # (the match starts at the character before the name: `{`, `[` or a blank)
    if not _judge(m.group("name"), m.group("value")):
        return m.group(0)       # (judged first: the scan below is the expensive part)
    ls = s.rfind("\n", max(0, a - 4096), a) + 1 or max(0, a - 4096)     # (at most 4 KB back: a long line stays linear)
    brace = s.rfind("{", ls, a)
    if (brace <= s.rfind("}", ls, a) or s[brace - 1:brace] == "$") and s.rfind("map[", ls, a) <= s.rfind("]", ls, a) \
            or m.group("value").startswith("0x"):
        return m.group(0)       # (`${X:?message}` is a placeholder, not a structure)
    return _value_repl()(m)


_rule("assignment", r"[{\[ ](?!REDACTED:)(?=(?P<name>[A-Za-z_]\w{0,63}))(?P=name):(?![ \t/])(?!\[REDACTED)"
      r"(?=(?P<value>[^\s{}\[\]\"'\\]{4,512}))(?P=value)(?=[ }\]])", _printed_field_repl)   # (no quote, no escape:
# `"secretKeyRef:\n", ` in JSON text is a YAML key, not a printed field)
# PHP `print_r`: `    [password] => X`
_rule("assignment", r"(?m)^[ \t]{0,64}\[(?P<name>[\w.\-]{1,128})\][ \t]{0,8}=>[ \t]{0,8}(?!\[REDACTED)"
      r"(?=(?P<value>\S{4,4096}))(?P=value)(?=[ \t]*\r?$)", _value_repl())
# a literal default after an environment lookup of a secret-named variable: `process.env.API_KEY || "..."`, `?? '...'`,
# PHP `getenv('API_KEY') ?: '...'`, Ruby `ENV["X"] || "..."` / `ENV.fetch("X", "...")`, Python `os.getenv("X", "...")` /
# `os.environ.get("X", "...")` / `... or "..."`, Laravel `env('X', '...')`, C# `GetEnvironmentVariable("X") ?? "..."`
_ENV_LOOKUP = (r"(?:process\.env|import\.meta\.env|ENV\.fetch|ENV|os\.environ|\$_ENV|\$_SERVER|getenv|environ\.get|"
               r"GetEnvironmentVariable|System\.getenv|env)"
               r"(?:\.(?P<e1>[A-Za-z_]\w{0,127})|\[[ \t]{0,4}[\"'`](?P<e2>[A-Za-z_]\w{0,127})[\"'`][ \t]{0,4}\]|"
               r"\([ \t]{0,4}[\"'](?P<e4>[A-Za-z_]\w{0,127})[\"'][ \t]{0,4}(?=[,)]))")


def _env_default_repl(m: re.Match) -> str:
    name = m.group("e1") or m.group("e2") or m.group("e4")
    s, a = m.string, m.start()
    if (a and (s[a - 1].isalnum() or s[a - 1] == "_")) or not _judge(name, m.group("value")):
        return m.group(0)       # (`myenv(...)`, `XENV[...]`: no lookup; the check is here so the scan skips to the names)
    if m.group("e1") and not s.startswith(("process.env", "import.meta.env"), a) or \
            not m.group("e4") and s[a:m.start("q")].rstrip().endswith(","):
        return m.group(0)       # (`Buffer.from(env.SECRET_KEY, "base64")`: a member and the next argument, no default)
    return s[a:m.start("value")] + "[REDACTED:assignment]" + s[m.end("value"):m.end()]


# (the pattern starts with the alternation of names, which lets the scan skip to their first letters)
_rule("assignment", _ENV_LOOKUP + r"[ \t]{0,8}(?:\)[ \t]{0,8})?(?:\|\||\?\?|\?:|\bor\b|,)[ \t]{0,8}"
      r"(?P<q>[\"'`])(?!\[REDACTED)(?=(?P<value>(?:[^\"'`\\\n]|\\.){4,512}))(?P=value)(?P=q)", _env_default_repl)

# name = "literal" / name: 'literal' / "name": "literal"   (code, JSON, YAML, TOML, dotenv, shell)
# A quoted value ends at the matching quote of its own kind: backslash escapes (JSON, TOML basic strings, Python, JS,
# YAML and shell double quotes), and in single quotes a doubled quote (YAML, SQL, TOML literal strings) or the shell's
# `'\''`; the other quote character is ordinary content (`"Xk4'Lm2"`). No blank inside (a phrase under a hard secret
# name is the passphrase rule's). The replacement keeps the quotes, so a document that parsed still parses.
_DQ_UNIT = r'(?:[^\s"\\]|\\\S)'
_SQ_UNIT = r"(?:[^\s'\\]|\\\S|'\\''|'')"
# The other string forms, each with its own end: a template literal without `${...}` and a Go raw string (backticks),
# C# verbatim `@"..."` (`""` is a quote), a triple-quoted string on one line (C# raw strings, Kotlin, Swift, Scala,
# Python), prefixed literals (`r"..."`, `b'...'`, `rb"..."`, `u"..."`; an f-string is code) and Rust raw `r#"..."#`.
_QUOTED_VALUE = (r"(?:(?P<q>\")(?=(?P<value>" + _DQ_UNIT + r"{6,4096})(?P<more>" + _DQ_UNIT + r"*))(?P=value)"
                 r"(?P=more)\""
                 r"|(?P<q2>')(?=(?P<value2>" + _SQ_UNIT + r"{6,4096})(?P<more2>" + _SQ_UNIT + r"*))(?P=value2)"
                 r"(?P=more2)'"
                 r"|`(?=(?P<value3>(?:[^\s`\\$]|\\\S|\$(?!\{)){6,4096}))(?P=value3)`"
                 r"|@\"(?=(?P<value4>(?:[^\s\"]|\"\"){6,4096}))(?P=value4)\""
                 r"|\"\"\"(?=(?P<value5>" + _DQ_UNIT + r"{6,4096}))(?P=value5)\"\"\""
                 r"|(?:[rR][bB]?|[bB][rR]?|[uU])(?:\"(?=(?P<value6>" + _DQ_UNIT + r"{6,4096}))(?P=value6)\""
                 r"|'(?=(?P<value7>" + _SQ_UNIT + r"{6,4096}))(?P=value7)')"
                 r"|[bB]?r(?P<hashes>\#{1,4})\"(?=(?P<value8>[^\s\"]{6,4096}))(?P=value8)\"(?P=hashes)"
                 r"|(?P<hashes2>\#{1,4})\"(?=(?P<value9>[^\s\"]{6,4096}))(?P=value9)\"(?P=hashes2))")   # Swift `#"..."#`
_VALUE_GROUPS = ("value", "value2", "value3", "value4", "value5", "value6", "value7", "value8", "value9")
# a YAML anchor or tag before a value (`password: &db_pw ...`, `password: !!str ...`)
_YAML_PROPS = r"(?:(?:&[\w.\-]{1,128}|!{1,2}[\w.\-/:]{0,128})[ \t]{1,8}){0,2}"
_QUOTED_ASSIGN = re.compile(
    r"(?<![\w.\-])(?=(?P<name>[\w.\-]{1,128}))(?P=name)(?P<sep>[\"']?\s{0,64}(?::=|=>|[:=])\s{0,64}" + _YAML_PROPS + r")"
    + _QUOTED_VALUE)
# a typed declaration of a literal: a type between the name and `=` (`password: str = "..."`, TypeScript `const
# password: string = "..."`, Rust `const PASSWORD: &str = "..."`, `static API_KEY: &'static str`, Kotlin/Swift/Scala
# `val password: String = "..."`, `var apiKey: String? = "..."`, `token: Optional[str] = "..."`, `x: str | None = "..."`)
# or after it (Go `var password string = "..."`, only after `var`/`const`); the right side must be a literal (a call or
# a reference stays). The match starts at the `:` (or at `var`/`const`), so the scan skips to those characters; the name
# before the `:` is read back by _name_before (_typed_repl). The identifiers are matched atomically.
_TYPE_ID = r"[A-Za-z_][\w$]{0,64}"
_DECL_TYPE = (r"(?:&(?:'[A-Za-z_]\w{0,16}[ \t]{1,4})?(?:mut[ \t]{1,4})?|\*)?(?:\[\d{0,8}\]){0,4}"
              r"(?=(?P<t>" + _TYPE_ID + r"(?:(?:\.|::)" + _TYPE_ID + r"){0,8}|\[[\w$;& \t]{1,64}\]))(?P=t)"
              r"(?:<[\w$.,:&'<>\[\]? \t*]{0,128}>)?(?:\[[\w$.,:'\"\[\] \t|]{0,128}\])?[?!]?(?:\[\]){0,4}"
              r"(?:[ \t]{0,4}\|[ \t]{0,4}(?=(?P<u>" + _TYPE_ID + r"(?:\." + _TYPE_ID + r"){0,8}))(?P=u)"
              r"(?:\[[\w$.,:'\" \t]{0,64}\])?){0,4}")
_TYPED_ASSIGN = re.compile(
    r"(?::[ \t]{0,8}|(?:v(?<![\w.\-]v)ar|c(?<![\w.\-]c)onst)[ \t]{1,8}(?=(?P<goname>[\w.\-]{1,128}))(?P=goname)"
    r"[ \t]{1,8}|(?:\A|\n)[ \t]{1,16}(?=(?P<goname2>[A-Za-z_]\w{0,127}))(?P=goname2)[ \t]{1,8}"
    r"(?=(?:string|\[\]byte)[ \t]{0,8}=))"       # (a Go line inside a `var ( ... )` block: `\tapiSecret string = "..."`)
    + _DECL_TYPE + r"[ \t]{0,8}(?::=|=)[ \t]{0,64}" + _QUOTED_VALUE)


def _typed_repl(m: re.Match) -> str:
    at = m.start()
    if m.string[at - 1:at] in ("?", "!") and m.string[at:at + 1] == ":":
        at -= 1         # TypeScript `apiKey?: string = "..."`, `apiKey!: string = "..."`
    name = m.group("goname") or m.group("goname2") or _name_before(m.string, at, quote=False)
    groups = m.groupdict()
    vg = next((g for g in _VALUE_GROUPS if groups.get(g) is not None), "value")
    more = "more" if groups.get("more") else "more2" if groups.get("more2") else None
    if not name or not (_judge(name, groups[vg]) if more else _assign_ok("quoted", name, groups[vg])):
        return m.group(0)
    return m.string[m.start():m.start(vg)] + "[REDACTED:assignment]" + m.string[m.end(more or vg):m.end()]
# the same inside a JSON string (nested tool output): \"api_key\": \"value\" and \\\"password\\\": \\\"value\\\"
_QUOTED_ESC_ASSIGN = re.compile(
    r"(?<![\w.\-])(?=(?P<name>[\w.\-]{1,128}))(?P=name)(?P<sep>\\{1,256}[\"']\s{0,64}(?::=|=>|[:=])\s{0,64})"
    r"(?P<q>\\{1,256}[\"'])(?=(?P<value>[^\s\"'\\]{6,4096}))(?P=value)(?P=q)")   # up to 8 levels of JSON in JSON
# a subscript assignment: os.environ["X_API_KEY"] = "...", process.env['X_TOKEN'] = '...', config["password"] = "..."
_SUBSCRIPT_ASSIGN = re.compile(
    r"(?<=\[[\"'])(?=(?P<name>[\w.\-]{1,128}))(?P=name)(?P<sep>[\"'][ \t]{0,16}\][ \t]{0,64}=[ \t]{0,64})" + _QUOTED_VALUE)
# a whole line assigning one quoted value that holds its own quote unescaped (`DB_PASSWORD='Xk4'Lm2'`, sloppy but
# what people write and tools print): the value runs to the last quote on the line (_quoted_line_repl)
_LINE_LEAD = r"[ \t]*(?:-[ \t]+)?(?:export[ \t]+|set[ \t]+)?"
_QUOTED_LINE = re.compile(
    r"(?m)^(?=(?P<lead>" + _LINE_LEAD + r"))(?P=lead)(?=(?P<name>[\w.\-]{1,128}))(?P=name)"
    r"(?P<sep>[\"']?[ \t]{0,64}(?::=|=>|[:=])[ \t]{0,64})(?P<q>[\"'])(?=(?P<value>[^\n]{1,4096}))(?P=value)")
# UPPER_SNAKE=bare-value (dotenv, shell export, docker env, or pasted into prose/a prompt), anywhere on a line; the value
# ends at whitespace, a quote or backtick (the end of the string it sits in: `Client("API_KEY=...")`), a JSON escape, or
# a comma before the next `name=` (`-o user=u,password=X`); brackets are part of it (`SECRET_KEY=k3j(x)...`), except
# closing ones it does not open (`f(API_KEY=...)`), and trailing sentence punctuation is not (_assign_repl). A `#`
# inside it is part of it (`#` starts a comment only after a blank). Code (`API_KEY=os.getenv(...)`) is kept by its shape.
_ENV_VALUE = r"(?:[^\s\"'`,|\\]|\\(?![nrt\"\\/u])|[,|](?![A-Za-z_][\w.\-]{0,63}=))"
_ENV_ASSIGN = re.compile(
    r"(?<![\w.\-])(?=(?P<name>[A-Z0-9_]{1,128}))(?P=name)(?P<sep>[ \t]{0,64}=[ \t]{0,64})(?![\"'])"
    r"(?=(?P<value>" + _ENV_VALUE + r"{6,4096}))(?P=value)")
# the same with a lowercase snake_case name (`"db_password=..."`, `docker run -e password=...`): code writes such
# names too (`connect(password=pw)`), so the value must look generated and not be an attribute path (_assign_repl)
_LOWER_ENV_ASSIGN = re.compile(
    r"(?:(?<=[ \t]-D)|(?<![\w.\-]))(?=(?P<name>[a-z][A-Za-z0-9_]{0,63}(?:\.[A-Za-z][A-Za-z0-9_]{0,63}){0,8}))(?P=name)"
    r"(?P<sep>[ \t]{0,64}=[ \t]{0,64})(?![\"'])"
    r"(?=(?P<value>" + _ENV_VALUE + r"{6,4096}))(?P=value)")
# yaml-ish: secret_name: bare-value on its own line, whatever characters it holds unless it is code or a type (_code_value,
# _type_kept; a ` #` comment after it is not part of it, nor a trailing `,`/`;`); a quoted value is the quoted rule's, and
# a YAML indicator alone (a block scalar `|-`, a tag `!vault`, an anchor or alias) is no value (_YAML_INDICATOR); a value
# that only starts with one of them is (`*k3j(x)...`: tools print such values unquoted)
_YAML_ASSIGN = re.compile(
    r"(?m)^(?=(?P<lead>[ \t]*(?:-[ \t]+)?))(?P=lead)(?=(?P<name>[\w.\-]{1,128}))(?P=name)(?P<sep>[ \t]{0,64}:[ \t]{1,64}"
    + _YAML_PROPS + r")(?![\"'])(?!#(?:[ \t]|$))(?=(?P<value>\S{6,4096})(?P<more>\S*))(?P=value)(?P=more)(?=[ \t]*(?:#[^\n]*)?$)")
_YAML_INDICATOR = re.compile(r"^(?:[|>][-+0-9]{0,3}|[!&*][\w.\-/!:]{0,128}|![\w.\-/!:]{0,128}[ \t].*)$")
# a whole `name = value` line in any case: .properties (`spring.datasource.password=...`), INI
# (`aws_secret_access_key = ...`), dotenv/shell (`db_password=...`, `export X=...`, a compose `- X=...` item); the value
# runs to the end of the line whatever it holds (an optional ` #` / ` ;` comment after it); code on such a line is kept
# by its shape (_code_value; a plain identifier is a variable unless a word of letters only looks like a credential
# among lines that are no code: _line_value)
_LINE_ASSIGN = re.compile(
    r"(?m)^(?=(?P<lead>" + _LINE_LEAD + r"))(?P=lead)(?=(?P<name>[A-Za-z_][\w.\-]{0,127}))(?P=name)"
    r"(?P<sep>[ \t]{0,64}=[ \t]{0,64})(?=(?P<value>\S{6,4096})(?P<more>\S*))(?P=value)(?P=more)"
    r"(?=[ \t]*(?:[#;][^\n]*)?$)")
_TRAIL = ".,;:!?"
_NEXT_OPTION = re.compile(r",[A-Za-z_][\w.\-]{0,63}=")
_ENV_END_SKIP = ".,;:!?)]`"
_BASE64_VALUE = re.compile(r"^(?:base64:)?[A-Za-z0-9+/]{16,4096}={0,2}$")
_COLOR = re.compile(r"^#(?:[0-9A-Fa-f]{3,4}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")
_IDENTIFIER = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_LEAD_ONLY = re.compile(_LINE_LEAD + r"\Z")
_LINE_REST = re.compile(r"\S*[ \t]*(?:[#;][^\n]*)?$", re.M)   # (to the end of the line: read once per line)
# (each run of blanks taken whole, never backtracked into: a run of 4,000 blanks before more text was rescanned from
# every split point, 0.3 s per line; tests/test_redact_forward_scans.py, one long line)
_QUOTED_TAIL = re.compile(r"(?=(?P<b1>[ \t]*))(?P=b1)[;,]?(?=(?P<b2>[ \t]*))(?P=b2)(?:(?:#|//)[^\n]*)?\Z")
_QUOTE_THEN_REDACTED = re.compile(r"[\"']{1,3}\[REDACTED")

# code and references where a config line would hold a value (_code_value): a template or variable reference, a call or
# an index (`get_password()`, `os.environ["K"]`, `cfg['token']`, `env("X")`), a structure, a prefixed string literal
_CODE_REF = re.compile(
    r"^(?:\$\{[^\s{}]{1,256}\}|\$\{\{.{0,256}\}\}|\$[A-Za-z_]\w{0,128}|\$\([^()\s]{0,256}\)|%\([\w.\-]{1,128}\)[sdrfi]|"
    r"%[A-Za-z_][\w.\-]{0,128}%|\{\{.{0,256}\}\}|\{%.{0,256}%\}|<%.{0,256}%>|#\{[^{}]{0,256}\}|@\{[^{}]{0,256}\}|"
    r"\{[A-Za-z_][\w.]{0,128}\}|@[A-Za-z_][\w.]{0,128}|![A-Za-z_][\w!]{0,64})$")
_ARG = r"[\w$.,:=/'\"\-]"
_CODE_CALL = re.compile(
    r"^(?:await|new|typeof)?[A-Za-z_$][\w$]{0,128}(?:(?:\?\.|\.|::|->)[A-Za-z_$][\w$]{0,128}|\(" + _ARG + r"{0,512}\)|"
    r"\(" + _ARG + r"{0,256}\(" + _ARG + r"{0,256}\)" + _ARG + r"{0,256}\)|\[" + _ARG + r"{0,256}\]){1,16}$")
_IDENT_CHAIN = re.compile(r"^[A-Za-z_$][\w$]{0,128}(?:\.[A-Za-z_$][\w$]{0,128}){0,16}$")
_PREFIXED_STRING = re.compile(r"""^[rRbBuU]?[fF][rRbB]?(["'`]).*\{.*\1$""")
_STRUCTURE = re.compile(r"^(?:\[.*\]|\{.*\}|[(\[{])$")
# a call or a parenthesised expression that opens a string literal: `re.compile(r"...`, `os.getenv("` (a value cut at the
# quote), `(r"passwort|...`
_CODE_OPEN = re.compile(r"""^(?:[A-Za-z_$][\w$]{0,128}(?:\.[A-Za-z_$][\w$]{0,128}){0,16})?\([rbfuRBFU]{0,2}(?:["'].*)?$""")
_CALL_START = re.compile(r"^[A-Za-z_$][A-Za-z_$]{2,128}(?:\.[A-Za-z_$][\w$]{0,128}){0,16}\(")
_CALL_QUOTE = re.compile(r"^[A-Za-z_][A-Za-z_]{2,128}(?:\.[A-Za-z_]\w{0,128}){0,16}\([rbfuRBFU]{0,2}[\"']")


def _code_value(value: str) -> bool:
    """A bare value on a config-looking line that is code or a reference, not a credential: `${DB_PASSWORD}`,
    `%(pw)s`, `{{ pw }}`, `!vault`, `get_password()`, `os.environ["K"]`, `cfg['token']`, `[...]`, `{...}`, `(` (an
    expression continued on the next line), `f"{a}{b}"`. Judged by shape, not by the characters it holds: a generated
    secret has brackets and quotes too (`k3j(x)...`, Django's alphabet)."""
    v = value.rstrip(";,")
    if not v or _CODE_REF.match(v) or _STRUCTURE.match(v) or _PREFIXED_STRING.match(v) or _CODE_OPEN.match(v):
        return True
    if ("(" in v or "[" in v) and _CODE_CALL.match(v):
        return True
    # a call whose first argument is a string literal (`re.compile(r"[^{}]+".format(`, `re.compile(r"^\s*([^=\s;,]+)")`):
    # a word-like name, `(`, a quote; a generated value that happens to look so is rare (`uGi$(a...` has no quote there)
    return _CALL_QUOTE.match(v) is not None


_WORD_RUN = re.compile(r"[A-Za-z0-9]+")
_DIGIT_RUN = re.compile(r"\d+")
# a method called on a name (`z.string().min(8)`, `yup.string().oneOf([...])`, `Joi.string()`): a schema, not a value;
# names of letters only and balanced parentheses (`AG.bX6(l'Ti...` is a generated value)
_CHAIN_START = re.compile(r"^[A-Za-z_$]{1,128}(?:\.[A-Za-z_$]{1,128}){1,16}\(")


def _chain_call(value: str) -> bool:
    return _CHAIN_START.match(value) is not None and value.count("(") == value.count(")")


# ---- a type or a value? Where a `name: value` line (YAML, a field or parameter declaration) or a table row holds one
# bare word, a type is kept as code only on evidence (_type_kept): type syntax (generics, arrays, references, `::`, an
# index such as `Optional[str]`), a dotted chain of names without digits (`rsa.RSAPrivateKey`, `PropTypes.string`), a
# built-in type name of the mainstream languages (_BUILTIN_TYPES), or a class-like name; a `?`/`!` after one of them
# (Swift, Kotlin, GraphQL) is part of it. Digits (other than in numeric type names such as `u8`, `Int32`,
# `Uint8Array`), a symbol before or inside a word, or `!`/`?` after a word with digits make it a value. A word of letters
# only under a password, secret, token or API-key name is a value when it looks like one (_letters_value), unless the
# lines around it are code (_line_context).
_STRONG_TYPE = re.compile(
    r"^(?P<pre>(?:&|\*|\[\d{0,8}\]){0,4})"
    r"(?P<id>[A-Za-z_][\w$]{0,128}(?:(?:\.|::)[A-Za-z_][\w$]{0,128}){0,16}|\[[A-Za-z_][\w$]{0,64}(?:;\d{1,8})?\])"
    r"(?P<args>(?:<[\w$.,:&'<>\[\]?*]{1,256}>|\[[\w$.,:'\"*<>|]{1,256}\]){0,4})(?P<opt>[?!]?)(?P<arr>(?:\[\]){0,4})$")
_DOTTED_TYPE = re.compile(r"^[A-Za-z_$]{1,128}(?:\.[A-Za-z_$]{1,128}){1,16}$")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])\d*|[A-Z]?[a-z]+\d*|\d+")
# a word with digits inside a type name: `u8`, `int64`, `Int32`, `Uint8Array`, `float64`, `Sha256Hash`, `Base64`
_NUM_SEG = re.compile(r"(?i)^(?:u|i|f|int|uint|float|complex|bytes?|base|sha|md|utf|ucs|ed|x|aes|rsa|hmac|blake|crc|ipv|"
                      r"v|bigint|biguint)\d{1,4}(?:_t)?$")
_BUILTIN_TYPES = frozenset((
    # TypeScript / JavaScript
    "string number boolean bigint symbol any unknown never void object undefined null String Number Boolean Object "
    "Symbol BigInt Function Date RegExp Error Buffer ArrayBuffer SharedArrayBuffer DataView Blob File URL Uint8Array "
    "Int8Array Uint16Array Int16Array Uint32Array Int32Array Float32Array Float64Array BigInt64Array BigUint64Array "
    "Uint8ClampedArray CryptoKey CryptoKeyPair KeyObject Promise Record Partial Readonly Map Set WeakMap Array "
    "ReadonlyArray NonSharedBuffer "
    # Python
    "str bytes bytearray memoryview int float complex bool dict list tuple set frozenset None Any Optional Union Literal "
    "Final ClassVar Required NotRequired SecretStr SecretBytes Text AnyStr Callable Sequence Mapping Decimal UUID "
    "datetime Path Json "
    # Rust
    "u8 u16 u32 u64 u128 usize i8 i16 i32 i64 i128 isize f32 f64 char Vec Box Rc Arc Option Result Cow SecretString "
    "SecretVec Secret Zeroizing "
    # Go
    "byte rune int8 int16 int32 int64 uint uint8 uint16 uint32 uint64 uintptr float32 float64 complex64 complex128 "
    "error "
    # Java, Kotlin, Scala, C#, Swift, C, PHP, GraphQL
    "Integer Long Short Byte Character Double Float Int UInt Char Unit Nothing List Seq CharArray ByteArray "
    "SecureString Guid Data Substring Bool long short double decimal sbyte ushort ulong nint nuint dynamic wchar_t "
    "size_t mixed iterable callable resource array ID").split())
# the last word of a class-like name that makes it a type (`AccessToken`, `ClientSecret`, `PasswordHash`, `TokenStore`)
_TYPE_NOUNS = frozenset((
    "str string text token tokens key keys secret secrets password passwords pass hash digest bytes buffer array list "
    "vec map dict set type types field fields input header value values data info schema model options option config "
    "settings store provider manager service handler credential credentials auth object interface struct class factory "
    "builder kind spec ref id param params props state error result response request payload wrapper box holder "
    "container validator encoder decoder cipher signer verifier pair entry record item node source client session "
    "context policy rule format mode enum union future promise stream reader writer loader parser serializer number "
    "int integer bool boolean float double char date time url uri path file event message callback func fn listener "
    "proxy adapter repository repo entity dto view form control component element template hook generator "
    "phrase code").split())
# lowercase words that are settings, not passwords (`password: required`, `token: bearer`, `secret: optional`)
_SETTING_WORDS = frozenset((
    "required optional enabled disabled enable disable hidden visible masked redacted secret secrets sensitive plaintext "
    "plain prompt random generated generate default inherit inherited ignore ignored omitted omit unset unknown "
    "environment vault keyring keychain stdin external internal manual interactive password passwords passwd passphrase "
    "bcrypt argon scrypt pbkdf hashed encrypted encrypt cleartext native local ldap kerberos oauth basic bearer digest "
    "token tokens login automatic never always forbidden allowed allow deny reject accept trust strict lax empty blank "
    "value values field input text number integer boolean object array static dynamic file files stored store cached "
    "session cookie header query body form literal expression reference variable parameter argument authorization "
    "authentication credentials mandatory nullable readonly writeonly sha md").split())
_ITEM3_NAME = re.compile(r"(?i)pass(?:word|wd|phrase|code)|secret|pwd|psk|token(?!s|iz)|api[_-]?key|" + _FOREIGN_PASSWORD)
_PASSWORD_NAME = re.compile(r"(?i)pass(?:word|wd|phrase|code)|pwd|" + _FOREIGN_PASSWORD)
_WORD_FOR_SECRET = re.compile(r"(?i)^(?:" + _FOREIGN_PASSWORD + r"|password|passwort|passphrase|secret|token|key)$")


_ANY_DIGIT = re.compile(r"\d")
_ARRAY_SIZE = re.compile(r"\[\d{0,8}\]|;\d{1,8}\]")
_TYPE_IDENT = re.compile(r"[A-Za-z_][\w$]*")
_STRAY_DIGIT = re.compile(r"(?<![\w$])\d")


def _digits_ok(t: str) -> bool:
    """The digits of a type expression are those of numeric type names (`u8`, `Int32`, `Uint8Array`) or array sizes."""
    if _ANY_DIGIT.search(t) is None:
        return True
    if "[" in t:
        t = _ARRAY_SIZE.sub("", t)
    for ident in _TYPE_IDENT.findall(t):
        if _ANY_DIGIT.search(ident) and not all(
                _NUM_SEG.match(s) or _TECH_WORD.match(s.lower()) for s in _CAMEL.findall(ident) if _ANY_DIGIT.search(s)):
            return False
    return _STRAY_DIGIT.search(t) is None


def _type_syntax(core: str) -> bool:
    """`Option<String>`, `string[]`, `&[u8]`, `*string`, `[]byte`, `std::string`, `Optional[str]`, `Scalars['String']`:
    a type by its syntax (digits only in numeric type names)."""
    m = _STRONG_TYPE.match(core)
    return m is not None and bool(m.group("pre") or "::" in m.group("id") or m.group("id")[0] == "[" or
                                  m.group("args") or m.group("arr")) and _digits_ok(core)


def _known_type(core: str) -> bool:
    """A type by its syntax or a built-in type name (also with `?`/`!` or `[]` after it)."""
    if _type_syntax(core):
        return True
    base = core[:-1] if core[-1:] in "?!" else core
    while base.endswith("[]"):
        base = base[:-2]
    return base in _BUILTIN_TYPES


def _letters_value(word: str, name: str, suffix: bool = False, prose: bool = False) -> int:
    """A word of letters only under `name`: 0 a type, a setting or a label; 1 a credential (`CorrectHorse`, a random
    mix of cases); 2 a lowercase word that is one only on a config line (`password: postgres`). Only under a password,
    secret, token or API-key name."""
    key = _last_segment(name)
    if not word.isalpha() or not _ITEM3_NAME.search(key) or word in _BUILTIN_TYPES or \
            word.lower() in _SETTING_WORDS or _WORD_FOR_SECRET.match(word) or word.lower() == key.lower() or \
            _PLACEHOLDER.match(word):
        return 0
    segs = _CAMEL.findall(word)
    if word[0].isupper():
        if word.isupper() or segs[-1].lower() in _TYPE_NOUNS or (prose and len(segs) < 2):
            return 0
        return 1 if len(word) >= (6 if suffix else 8) else 0
    if word.islower():
        if prose:
            return 0
        if len(word) >= 12:
            return 1
        return 2 if _PASSWORD_NAME.search(key) and len(word) >= 6 else 0
    # camelCase that starts in lower case is a variable name, unless its humps are as short as a random mix's
    return 1 if len(word) >= 8 and len(segs) >= 4 and len(word) / len(segs) < 3 else 0


# the lines around a value (_line_context): code (declarations, braces, statements, typed fields) or config (YAML
# mappings and lists with plain values, document markers, INI sections, `KEY=value`)
_CODE_LINE = re.compile(
    r"^(?:(?:import|from|interface|struct|class|def|fn|func|function|type|pub|public|private|protected|internal|static|"
    r"readonly|const|let|var|val|impl|enum|trait|record|abstract|override|async|package|namespace|using|module|return|"
    r"declare|sealed|data[ \t]+class|object|typedef|extern|export[ \t]+(?:default|interface|class|type|const|let|var|"
    r"function|async|enum|abstract|declare))(?=[ \t(<{])|[@}\])]|#\[|#include|#define|//|/\*|\*/|<\?php)"
    r"|(?:[{;(]|=>|->)$")
_CONFIG_SKIP = frozenset(("else", "try", "finally", "except", "default", "public", "private", "protected", "internal",
                          "case"))
_CONFIG_KV = re.compile(r"^(?:-[ \t]+)?[\w.\-\"']{1,128}:(?:[ \t]+(?P<v>.*))?$")
_CONFIG_EQ = re.compile(r"^(?:export[ \t]+)?[A-Za-z_][\w.\-]{0,127}[ \t]*=[ \t]*[^\s(=]")
_INI_SECTION = re.compile(r"^\[[^\]\n]{1,64}\]$")
_TYPED_LINE = re.compile(r"^[\w$]{1,128}[?!]?[ \t]*:[ \t]*(?P<t>[^\s,;=]{1,256})[,;]?$")


_CTX_MEMO = threading.local()   # _line_context's memo for the text being redacted (one at a time per thread)
_CTX_WINDOW = 1024               # how far the neighbours of a line are read, either way
_CTX_LINE_MAX = 4096             # a line longer than this has no neighbours worth reading


def _context_memo(s: str):
    """The memo of _line_context for `s`: the kind of each line read so far (by its start) and the verdict of each line
    asked about. A replacement callback gets the text it is run on (`m.string`), the same object for every match of one
    pass, so a pass reads each line at most once however many matches ask about it; the memo is dropped when the text
    changes (a new object) and at the end of each `_rules` call (_forget_context)."""
    st = _CTX_MEMO
    if getattr(st, "s", None) is not s:
        st.s, st.kinds, st.verdicts = s, {}, {}
    return st


def _forget_context() -> None:
    _CTX_MEMO.s = None
    _CTX_MEMO.kinds = _CTX_MEMO.verdicts = None


_FLOW_OPEN_LINE = re.compile(r"^(?:-[ \t]+)?[\w.\-]{1,128}:[ \t]*[{\[]$")


def _line_kind(t: str) -> int:
    """One stripped line around a value: 1 code (declarations, braces, statements, typed fields), 2 config (YAML
    mappings and lists with plain values, document markers, INI sections, `KEY=value`), 0 neither."""
    if _FLOW_OPEN_LINE.match(t):
        return 2                # `environment: {` / `args: [`: a YAML flow collection continued over lines
    if t in ("}", "]", "},", "],"):
        return 0                # a closing bracket alone: a flow map's end as much as a block's
    if _CODE_LINE.search(t):
        return 1
    if t in ("---", "...") or t == "-" or t.startswith("- ") or _INI_SECTION.match(t):
        return 2
    m = _TYPED_LINE.match(t)
    if m and _known_type(m.group("t")):
        return 1
    m = _CONFIG_KV.match(t)
    if m:
        v = (m.group("v") or "").strip()
        if not v:
            return 2 if t[:-1] not in _CONFIG_SKIP else 0
        return 2 if not v.endswith((",", ";", "{", "(")) and " = " not in v else 0
    if _CONFIG_EQ.match(t) and not t.endswith((";", "{", ",")):
        return 2
    return 0


def _neighbour_kind(memo, s: str, start: int, end: int) -> int:
    """The kind of the line s[start:end] (a whole line of at most _CTX_WINDOW characters), -1 for a blank line or a
    comment (not counted); each line is classified once per text."""
    k = memo.kinds.get(start)
    if k is None:
        ln = s[start:end].strip()
        if not ln or (ln[0] == "#" and not ln.startswith(("#[", "#include", "#define"))):
            k = -1
        else:
            k = _line_kind(ln)
        memo.kinds[start] = k
    return k


def _line_context(s: str, pos: int) -> str | None:
    """'code', 'config' or None: what the (up to) three non-blank lines before and after the line at `pos` look like.
    Called only for a word whose verdict depends on it. Every search is bounded: the line itself is found within 4 KB
    of `pos` (a longer line has no context), and its neighbours within _CTX_WINDOW either way; a neighbour that does
    not fit in that window whole is no evidence and ends the walk in its direction. Each line's kind and each line's
    verdict are computed once per text (_context_memo), so the cost does not grow with the number of matches that ask
    (tests/test_redact_speed.py, the line-length sweep)."""
    lo = max(0, pos - _CTX_LINE_MAX)
    a = s.rfind("\n", lo, pos) + 1
    b = s.find("\n", pos, pos + _CTX_LINE_MAX)
    if (a == 0 and lo > 0) or (b < 0 and len(s) - pos > _CTX_LINE_MAX):
        return None             # a line longer than 4 KB has no neighbours worth reading (and each search stays bounded)
    memo = _context_memo(s)
    if a in memo.verdicts:
        return memo.verdicts[a]
    b = len(s) if b < 0 else b
    kinds: list[int] = []
    i, lo = a - 1, max(0, a - 1 - _CTX_WINDOW)     # lines before: s[j + 1:i], walking back from the line's start
    while i > 0 and len(kinds) < 3:
        j = s.rfind("\n", lo, i)
        if j < 0 and lo > 0:
            break               # the line before starts outside the window: no evidence (and nothing past it is read)
        k = _neighbour_kind(memo, s, j + 1, i)
        if k >= 0:
            kinds.append(k)
        if j < 0:
            break
        i = j
    k0, hi, after = b + 1, min(len(s), b + 1 + _CTX_WINDOW), 0
    while k0 < hi and after < 3:                # lines after
        j = s.find("\n", k0, hi)
        if j < 0:
            if hi < len(s):
                break           # the line after ends outside the window: no evidence
            j = hi
        k = _neighbour_kind(memo, s, k0, j)
        if k >= 0:
            kinds.append(k)
            after += 1
        k0 = j + 1
    code, config = kinds.count(1), kinds.count(2)
    verdict = "code" if code > config else "config" if config > code else None
    memo.verdicts[a] = verdict
    return verdict


def _line_value(word: str, name: str, s: str, pos: int) -> bool:
    """A word of letters only on a `name = word` line: a credential when it looks like one (_letters_value) and the lines
    around it are no code; a lowercase word (`password = postgres`) only among config lines (`password = pw` is code)."""
    verdict = _letters_value(word, name)
    if not verdict:
        return False
    ctx = _line_context(s, pos)
    return ctx == "config" if verdict == 2 else ctx != "code"


def _type_kept(value: str, name: str, s: str, pos: int) -> bool:
    """The bare value of a `name: value` line (or a table row): kept as code when it is a type on evidence (see above);
    `s`/`pos` give the lines around it, read only for a word of letters only."""
    core = value
    if core[-1:] in (",", ";", "{", ":") and (core[-1] != ":" or core[-2:-1] == ")"):
        core = core[:-1]          # `string;`, `bytes,`, a parameter `String)` / `String),` / `String):`
    if core[-1:] == ")":
        core = core[:-1]
    if not core:
        return True
    if _ANY_DIGIT.search(core) and not _digits_ok(core):
        return False              # `Passw0rd!`, `Winter560`, `Admin.Pass1`: digits of no type name
    if _known_type(core):
        return True
    if _ANY_DIGIT.search(core) and _LOWER_DIGITS.fullmatch(core) and not _lower_digits_value(core):
        return True               # `sha256`, `python3`, `oauth2`: a technical word (_lower_digits_value)
    base = core[:-1] if core[-1] in "?!" else core
    if not base or _DOTTED_TYPE.match(base):
        return True
    if not base.isalpha():
        # letters with `_` or `-` (`secret_name`, `db-credentials`) are names; anything else is a value
        return not re.search(r"[^A-Za-z_\-]", core)
    verdict = _letters_value(base, name, suffix=base != core)
    return not verdict or _line_context(s, pos) == "code"


# a tuple or a list of names cut at its first blank (`TOKEN_TYPES = (TokenType.A, TokenType.B)`): code
_OPEN_NAMES = re.compile(r"^[(\[][A-Za-z_$][\w$]{0,128}(?:\.[A-Za-z_$][\w$]{0,128}){0,16}$")


def _last_segment(name: str) -> str:
    """The key itself: `token.markup` -> `markup`, `spring.datasource.password` -> `password`. A dotted path
    is an object or namespace followed by the key; only the key says whether the value is a secret."""
    return name.strip("\"'").rstrip(".").rsplit(".", 1)[-1]


def _unbalanced_closers(value: str) -> str:
    """`abc)` of `f(API_KEY=abc)`: the closing brackets at the end that the value itself does not open. One count of
    each bracket, then a walk back over the closers (recounting for each closer made a run of 4,096 of them cost 5 s
    per MB of text)."""
    k = len(value)
    if not k or value[-1] not in ")]}":
        return ""
    counts = {c: value.count(c) for c in "()[]{}"}
    while k and value[k - 1] in ")]}":
        close = value[k - 1]
        if counts[{")": "(", "]": "[", "}": "{"}[close]] >= counts[close]:
            break
        counts[close] -= 1          # (the closer is no longer in value[:k]; the openers in it stay the same)
        k -= 1
    return value[k:]


def _assign_ok(kind: str, name: str, value: str) -> bool:
    """The checks every assignment rule shares: a secret name, a value that is no placeholder, version, colour, path or
    URL; env-style `*_KEY` names only with a generated-looking value."""
    key = _last_segment(name)
    strength = _secret_name(key, env=(kind in ("env", "lower-env", "line")))
    hard = _HARD_SECRET.search(key)
    if not strength or (_BENIGN_NAME.search(key) and not (hard and _SOFT_SUFFIX.search(key))):
        return False
    if _PLACEHOLDER.match(value.strip()) or value.startswith("[REDACTED") or _COLOR.match(value) or \
            _VERSION.match(value.rstrip(";,")):
        return False
    if strength == "key" and not (len(value) >= 12 and re.search(r"\d", value) and re.search(r"[A-Za-z]", value)
                                  and ("/" not in value or _BASE64_VALUE.match(value))):
        return False  # CACHE_KEY=user_profile is a setting; STRIPE_KEY=4eC39HqLyjWDarjt is not
    if (re.match(r"^(?:[/~.]|[A-Za-z]:[\\/])[\w./\\\-]*(?:,|$)", value) and _path_shaped(key, value.rstrip(","))) or \
            "://" in value:
        return False  # a path or URL (PWD: '/home/me'); URL credentials have their own rule
    if value.lower() == key.lower() or re.fullmatch(r"@[\w.\-]{1,128}@", value):
        return False  # `PASSWORD = 'password'` (a field name), Maven's `@db.password@` (a placeholder filled at build)
    return not _not_a_credential(key, value)


# a reference with a shell or compose operator (`${DB_PASSWORD:-}`, `${X:?set it}`; a literal default is redacted
# inside it first), a Laravel validation rule (`'password' => 'required|min:8'`), and a constant that holds the name
# of a thing (`ACCESS_TOKEN_KEY = 'accessToken'`, `PASSWORD_PARAM = "password"`, `AUTH_TOKEN_COOKIE = "session_token"`)
_SHELL_PARAM = re.compile(r"\$\{[\w.\-]{1,128}(?::?[-=?+][^{}\n]{0,256})?\}")
_VALIDATION_RULES = re.compile(r"(?:bail|required|nullable|sometimes|string|confirmed|min|max|size|between|regex|"
                               r"different|same|alpha_num|alpha_dash|current_password|password|filled|present)"
                               r"(?::[\w,.\-]{0,64})?(?:\|[a-z_]{1,32}(?::[\w,.\-]{0,64})?){1,16}")
_THING_SUFFIX = {"param", "field", "cookie", "header", "key", "attr", "attribute", "prop", "property", "column", "col",
                 "label", "name", "env", "var"}
_SECRET_WORDS = {"token", "password", "passwd", "secret", "key", "auth", "session", "credential", "credentials", "pwd",
                 "pass", "apikey", "jwt", "csrf", "xsrf", "bearer"}
_NAME_WORDS = re.compile(r"[a-z]+(?:[A-Z][a-z]+)+|[a-z]+(?:[_\-.][a-z]+)+|[a-z]+")


def _not_a_credential(key: str, value: str) -> bool:
    if (value.startswith("${") and _SHELL_PARAM.fullmatch(value)) or ("|" in value and _VALIDATION_RULES.fullmatch(value)):
        return True
    if len(value) > 64 or not _NAME_WORDS.fullmatch(value):
        return False            # (the cheap tests first: most values have digits or symbols)
    segs = _segments(key)
    if len(segs) < 2 or segs[-1] not in _THING_SUFFIX:
        return False
    words = _segments(value)
    return (len(words) >= 2 or value.lower() in _SECRET_WORDS) and any(w in _SECRET_WORDS for w in words) and \
        any(w in _SECRET_WORDS for w in segs[:-1])


def _line_owns(m: re.Match) -> bool:
    """An env-style match that starts a config line whose value the whole-line rule takes (`DB_PASSWORD=Xk4"Lm2`: the
    env rule would stop at the quote): it leaves the line to _LINE_ASSIGN."""
    s, a = m.string, m.start()
    nl = s.rfind("\n", max(0, a - 80), a)
    if nl < 0 and a > 80:
        return False
    if not _LEAD_ONLY.match(s, nl + 1, a):
        return False
    end = m.end()
    if end >= len(s) or s[end] in "\r\n":
        return False
    return _LINE_REST.match(s, end) is not None


def _assign_repl(kind: str):
    def repl(m: re.Match) -> str:
        whole, name = m.group(0), m.group("name")
        groups = m.groupdict()
        vg = next((g for g in _VALUE_GROUPS if groups.get(g) is not None), "value")
        value: str = groups.get(vg) or ""
        tail = ""
        more = groups.get("more") or groups.get("more2")
        if more:
            # longer than the cap (4 KB): judged by its first part, redacted whole (never in part)
            if not _judge(name, value) or _code_value(value):
                return whole
            if kind in ("yaml", "line"):
                return f"{m.group('lead')}{name}{m.group('sep')}[REDACTED:assignment]"
            end = m.end("more") if groups.get("more") else m.end("more2")
            return m.string[m.start():m.start(vg)] + "[REDACTED:assignment]" + m.string[end:m.end()]
        if kind in ("env", "lower-env"):
            # the value ends at whitespace, the end, a JSON escape (`\n`, `\"`), a comma before the next `name=`, or the
            # quote that closes the string it sits in (`Client("API_KEY=...")`, `{"v": "API_KEY=..."}`, `\"API_KEY=...\"`
            # inside JSON: the backslashes before that quote are not part of it); sentence punctuation after it and
            # closing brackets it does not open are not part of it either
            s, j = m.string, m.end()
            if _line_owns(m):
                return whole        # a config line: the whole-line rule sees all of its value
            core = value.rstrip(_TRAIL)
            core = core[:len(core) - len(_unbalanced_closers(core))].rstrip(_TRAIL)
            if j < len(s) and s[j] in "\"'" and core == value:
                core = core.rstrip("\\")      # `\"` / `\\\"` closing a string inside a JSON string
            tail, value = value[len(core):], core
            if len(value) < 6 or _code_value(value) or (_CALL_START.match(value) and value.count("(") > value.count(")")):
                return whole    # `crypt($1, password_hash)`: a call cut at a blank is code
            if _OPEN_NAMES.match(value) and all(len(_DIGIT_RUN.findall(w)) <= 1 for w in _WORD_RUN.findall(value)):
                return whole    # `(TokenType.A, TokenType.B)` cut at its first blank: a tuple of names
            if value[0] == "#" and not (len(value) >= 12 and _looks_generated(value)):
                return whole    # `KEY=#anchor`; a generated value may start with `#` (no blank before it: no comment)
            digits = value.isdigit() and len(value) >= 6 and _HARD_SECRET.search(_last_segment(name)) is not None
            if kind == "lower-env" and not digits and (not _looks_generated(value) or (_IDENTIFIER.match(value) and (
                    "." in value or ("_" in value and value.isupper())))):
                return whole    # `f(password=pw)`, `token=args.token2`, `f(secret=SECRET_V2)`: code, not a credential
        if kind == "line":
            # a bare value on a whole line: code (`token = state.push(...)`, `password = pw`, `key = cfg.KEY`,
            # `password = get_password()`) is not a credential; INI/properties/dotenv values are, whatever they hold
            core = value.rstrip(";,")
            tail, value = value[len(core):], core
            if len(value) < 6 or _code_value(value) or (_IDENTIFIER.match(value) and not re.search(r"\d", value) and not (
                    value.isalpha() and _line_value(value, name, m.string, m.start()))):
                return whole      # (`password = pw`, `key = cfg.KEY`; `password = CorrectHorse` is a value: _line_value)
            ref = value[:len(value) - len(_unbalanced_closers(value))]
            if ref != value and (_code_value(ref) or (_IDENTIFIER.match(ref) and not re.search(r"\d", ref))):
                return whole      # `token=args.token)`: the last keyword argument of a call continued from the line above
            if value[0] in "\"'" and (value[-1] == value[0] or _QUOTE_THEN_REDACTED.match(value)):
                return whole      # a quoted literal: the quoted rules judge it (a value that only starts with a quote is bare;
                # `"""[REDACTED:assignment]` is the opening line of a multi-line string the TOML rule emptied)
            body = value.rstrip(")]},;")
            if value[0] in "\"'" and len(body) > 1 and body[-1] == value[0]:
                # a quoted literal followed by closing brackets (a keyword argument on its own line: `password='...')`):
                # the quotes and the brackets stay
                if body[1:-1].startswith("[REDACTED") or not _assign_ok(kind, name, body[1:-1]):
                    return whole
                return f"{m.group('lead')}{name}{m.group('sep')}{body[0]}[REDACTED:assignment]{body[-1]}" \
                       f"{value[len(body):]}{tail}"
        if kind == "yaml":
            if _code_value(value) or _YAML_INDICATOR.match(value) or (
                    value[0] == "#" and not (len(value) >= 12 and _looks_generated(value))) or _chain_call(value) or \
                    _type_kept(value, name, m.string, m.start()):
                return whole  # e.g. `privateKey: NonSharedBuffer`, `password: string;` (a type), `password: !vault`
            core = value.rstrip(",;")       # `password: Winter560,` (a flow map, a log): the comma stays
            if core:
                tail, value = value[len(core):], core
        if not _assign_ok(kind, name, value) or (kind == "quoted" and "[REDACTED" in value):
            return whole        # (`'''[REDACTED:assignment]'''`: an earlier rule's replacement inside the quotes)
        if kind == "quoted":        # the value between its quotes, whatever form they take
            return m.string[m.start():m.start(vg)] + "[REDACTED:assignment]" + m.string[m.end(vg):m.end()]
        lead = m.group("lead") if kind in ("yaml", "line") else ""
        return f"{lead}{name}{m.group('sep')}[REDACTED:assignment]{tail}"
    return repl


_DQ_BODY = re.compile(r'(?:[^"\\\n]|\\.)*')
_SQ_BODY = re.compile(r"(?:[^'\\\n]|\\.|'\\''|'')*")


def _quoted_line_repl(m: re.Match) -> str:
    """A whole line assigning one quoted value whose own closing quote is followed by more of it (`'Xk4'Lm2'`): the
    value is everything up to the last quote of the line. A normal quoted value is left to _QUOTED_ASSIGN."""
    q, rest = m.group("q"), m.group("value")
    if len(rest) == 4096:
        return m.group(0)
    if rest.startswith(q + q):
        # a triple-quoted string on one line (TOML `'''...'''`, Python): the value is between the triple quotes, which
        # stay; a quote inside it is content (`'''Xk4'Lm2'''`)
        stripped = rest.rstrip(" \t;,\r")
        value = stripped[2:-3]
        if len(stripped) < 5 or not stripped.endswith(q * 3) or len(value) < 6 or any(c.isspace() for c in value) or \
                value.startswith("[REDACTED") or not _assign_ok("quoted", m.group("name"), value):
            return m.group(0)
        return f"{m.group('lead')}{m.group('name')}{m.group('sep')}{q * 3}[REDACTED:assignment]{q * 3}" \
               f"{rest[len(stripped):]}"
    body = (_DQ_BODY if q == '"' else _SQ_BODY).match(rest)      # (a `*` pattern: it always matches)
    end = body.end() if body else 0
    if end < len(rest) and rest[end] == q and _QUOTED_TAIL.match(rest, end + 1):
        return m.group(0)               # an ordinary quoted value (or followed by a comment)
    stripped = rest.rstrip(" \t;,\r")
    last = len(stripped) - 1
    if last < 1 or stripped[last] != q:
        return m.group(0)
    value = stripped[:last]
    if len(value) < 6 or any(c.isspace() for c in value) or not _assign_ok("quoted", m.group("name"), value):
        return m.group(0)
    return f"{m.group('lead')}{m.group('name')}{m.group('sep')}{q}[REDACTED:assignment]{q}{rest[last + 1:]}"


# a marker directly followed by more of the token it replaced (a rule whose value stops at its cap, or ends inside a
# longer run of value characters): the rest of the token goes with it, so such a value does not keep its tail. A
# letter, a digit or `+=_` right after the marker counts, and `-` or `/` before one (`@host`, `:port`, `;`, `.`, a
# quote, a bracket or `-->` end a value: what follows them is kept)
_HELD_MARKER = "[REDACTED\x02:"      # a marker that was in the text before a pass (still starts with `[REDACTED`)
_CUT_TOKEN = re.compile(r"(?P<m>\[REDACTED:[a-z][\w\-]{0,40}\])(?=[A-Za-z0-9+=_]|[-/][A-Za-z0-9])"
                        r"[^\s\"'`<>,;|\\(){}\[\]]+")

_ASSIGN_RULES = [(kind, rx, repl) for kind, rx, repl in
                 [("quoted-line", _QUOTED_LINE, _quoted_line_repl), ("quoted", _QUOTED_ASSIGN, _assign_repl("quoted")),
                  ("quoted", _TYPED_ASSIGN, _typed_repl)] + [(kind, rx, _assign_repl(kind)) for kind, rx in
                  (("quoted", _QUOTED_ESC_ASSIGN), ("quoted", _SUBSCRIPT_ASSIGN), ("env", _ENV_ASSIGN),
                   ("lower-env", _LOWER_ENV_ASSIGN), ("yaml", _YAML_ASSIGN), ("line", _LINE_ASSIGN))]]


# ---- round 11 (verifier v10b): YAML flow mappings and argument lists

def _flow_repl(m: re.Match) -> str:
    """A plain value of a YAML flow mapping item (`{user: app, POSTGRES_PASSWORD: X}`, `- { name: alice, password: X }`,
    `[{name: a, db_password: X}]`): judged like the bare value of a block line (_YAML_ASSIGN: code, references and types
    are kept by their shape, _type_kept). A value longer than its cap goes whole under a secret name (fail closed)."""
    whole, name, s = m.group(0), m.group("name"), m.string
    head, value, more = s[m.start():m.start("value")], m.group("value"), m.group("more")
    if more:
        return head + "[REDACTED:assignment]" if _judge(name, value) else whole
    closers = _unbalanced_closers(value)     # `f(a, password: value)`: a closing bracket it does not open stays
    core = value[:len(value) - len(closers)].rstrip(";")
    if len(core) < 6 or _code_value(core) or _YAML_INDICATOR.match(core) or (
            core[0] == "#" and not (len(core) >= 12 and _looks_generated(core))) or _CHAIN_START.match(core) or \
            _type_kept(core, name, s, m.start("value")) or not _assign_ok("yaml", name, core):
        # (a method called on a name, `T.nilable(String)).void`, is code even when the flow item cuts it unbalanced)
        return whole
    return head + "[REDACTED:assignment]" + value[len(core):]


# a flow mapping item: after `{`, `,` or `[` (also on the next line or the one after: a flow map continued over lines), an
# unquoted name, `:` and a blank, and a plain scalar that ends where YAML ends it (`,`, `}`, `]`, a ` #` comment, the
# end of the line); a quoted value is the quoted rule's (_QUOTED_ASSIGN), and the JSON form (`{"name": "value"}`) is the
# JSON rules'. A plain scalar cannot hold `,[]{}` in a flow context, so each value is reached from one start only.
_FLOW_VALUE = r"[^\s,{}\[\]]"
_rule("assignment", r"(?P<pre>[{,\[][ \t]{0,64}(?:\r?\n[ \t]{0,64}){0,2})(?=(?P<name>[A-Za-z_][\w.\-]{0,127}))(?P=name)"
      r"[ \t]{0,8}:[ \t]{1,8}" + _YAML_PROPS + r"(?![\"'])(?!\[REDACTED)"
      r"(?=(?P<value>" + _FLOW_VALUE + r"{6,4096})(?P<more>" + _FLOW_VALUE + r"*))(?P=value)(?P=more)"
      r"(?=[ \t]*(?:[,}\]]|\r?\n|\Z)|[ \t]+#)", _flow_repl)


# ---- a sequence directly under a secret name: each scalar item judged like a value of that name
# (a plain item is taken in one go, its trailing blanks with it, and never backtracked into: a run of blanks inside or
# after an item is read once)
_SEQ_ITEM = re.compile(r"""(?P<lead>[ \t\r\n]*)(?P<v>"(?:[^"\\\r\n]|\\.)*"|'(?:[^'\r\n]|'')*'|"""
                       r"""(?=(?P<pv>[^,"'\r\n\[\]{}]*))(?P=pv))(?=[ \t\r\n]*(?:,|\Z))""")


def _seq_value(name: str, raw: str, s: str, pos: int) -> bool:
    """Is the scalar item `raw` (quoted or plain) of a sequence under `name` a credential? The conditions of a flow
    mapping value (_flow_repl): six characters or more, no code, indicator, type or reference, and _assign_ok."""
    core = raw[1:-1] if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0] else raw
    if len(core) < 6 or core.startswith("[REDACTED") or _code_value(core) or _YAML_INDICATOR.match(core) or (
            core[0] == "#") or _CHAIN_START.match(core) or _type_kept(core, name, s, pos):
        return False
    return _assign_ok("yaml", name, core)


def _flow_seq_repl(m: re.Match) -> str:
    """`api_keys: [a, b]`, `password: ["x"]` (the brackets on the line or over lines, 4 KB at most): every scalar item
    that is a value of the name is replaced, its quotes kept; an item that is a mapping or a sequence stops the reading
    (the whole match is kept)."""
    body, name, s = m.group("body"), m.group("name"), m.string
    out, i, changed, base = [], 0, False, m.start("body")
    while i <= len(body):
        it = _SEQ_ITEM.match(body, i)
        if it is None:
            return m.group(0)
        v = it.group("v")
        lead_end = it.start("v")
        out.append(body[i:lead_end])
        stripped = v.rstrip()
        if stripped and _seq_value(name, stripped.strip(), s, base + lead_end):
            q = stripped[0] if stripped[0] in "\"'" and len(stripped) >= 2 and stripped[-1] == stripped[0] else ""
            out.append(q + "[REDACTED:assignment]" + q + v[len(stripped):])
            changed = True
        else:
            out.append(v)
        j = it.end()
        k = j
        while k < len(body) and body[k] in " \t\r\n":
            k += 1
        if k >= len(body):
            out.append(body[j:])
            break
        if body[k] != ",":
            return m.group(0)
        out.append(body[j:k + 1])
        i = k + 1
    if not changed:
        return m.group(0)
    return s[m.start():m.start("body")] + "".join(out) + s[m.end("body"):m.end()]


_rule("assignment", r"(?m)^(?P<pre>[ \t]{0,64}(?:-[ \t]{1,8})?(?P<q>[\"']?)(?P<name>[A-Za-z_][\w.\-]{0,127})(?P=q)"
      r"[ \t]{0,8}:[ \t]{0,8}" + _YAML_PROPS + r"\[)(?=(?P<body>[^\[\]{}]{0,4096}))(?P=body)\]", _flow_seq_repl)


_ITEM_DASH = re.compile(r"[ \t]*-[ \t]+")
_ITEM_COMMENT = re.compile(r"[ \t]#")
_ITEM_MAPPING = re.compile(r"[\w.\-\"']{1,128}:(?:[ \t]|$)")


def _block_seq_repl(m: re.Match) -> str:
    """`api_keys:` and below it `- a`, `- "b"` lines (at the name's indentation or deeper, 256 at most): every item that
    is one scalar and a value of the name is replaced; an item that holds a mapping (`- name: x`) or a block stays."""
    name, s = m.group("name"), m.string
    lines = m.group("items").split("\n")
    out = []
    base = m.start("items")
    for ln in lines:
        dash = _ITEM_DASH.match(ln)
        if dash:
            # (the item to a ` #` comment, its blanks and `\r` left out: each found in one pass over the line)
            rest = ln[dash.end():]
            c = _ITEM_COMMENT.search(rest)
            v = rest[:c.start() if c else len(rest)].rstrip(" \t\r")
            if v and _SEQ_ITEM.fullmatch(v) and not _ITEM_MAPPING.match(v) and _seq_value(name, v, s, base):
                q = v[0] if v[0] in "\"'" and len(v) >= 2 and v[-1] == v[0] else ""
                ln = ln[:dash.end()] + q + "[REDACTED:assignment]" + q + rest[len(v):]
        out.append(ln)
        base += len(ln) + 1
    new = "\n".join(out)
    return m.group(0) if new == m.group("items") else s[m.start():m.start("items")] + new


_rule("assignment", r"(?m)^(?P<lead>[ \t]{0,64})(?:-[ \t]{1,8})?(?P<q>[\"']?)(?P<name>[A-Za-z_][\w.\-]{0,127})(?P=q)"
      r"[ \t]{0,8}:[ \t]{0,8}(?:#[^\r\n]{0,256})?(?=\r?\n)"
      r"(?=(?P<items>(?:\r?\n(?P=lead)[ \t]{0,64}-[ \t][^\r\n]{0,4096}){1,256}))(?P=items)", _block_seq_repl)


_LIST_SECRET_FLAGS = {"requirepass", "masterauth"}
# (a compose healthcheck `test: ["CMD", "redis-cli", "-a", "X", "ping"]`: the item after `CMD` is the command)
_LIST_CMD = re.compile(r"\[[ \t]{0,8}(?:[\"']CMD[\"'][ \t]{0,8},[ \t]{0,8})?[\"']?(?:[^\s\"',\[\]]{0,256}/)?"
                       r"(?P<cmd>[\w.\-]{1,64})[\"']?[ \t]{0,8}(?:,|\r?\n|$)")
_CMD_FLAG_TABLE = [(re.compile(f"(?:{cmd})"), re.compile(f"(?:{flags})")) for cmd, flags in _CMD_FLAGS
                   if " " not in cmd and "[" not in cmd]


def _list_flag_repl(m: re.Match) -> str:
    """The item after a password flag in an argument list: `args: ["--password", "X"]`, `command: ["redis-server",
    "--requirepass", "X"]`, a block list `- --requirepass` / `- X`, `["redis-cli", "-a", "X"]` (a short flag only after
    a command of the command table that takes it, the list's first item, read at most 512 characters back)."""
    flag, value = m.group("flag"), m.group("value")
    bare = flag.lstrip("-")
    short = False
    if len(flag) - len(bare) == 2 or len(bare) > 1:
        ok = _secret_option(bare) or bare.lower() in _LIST_SECRET_FLAGS
    else:
        short = True
        s, a = m.string, m.start()
        lo = max(0, a - 512)
        start = s.rfind("[", lo, a)
        c = _LIST_CMD.match(s, start) if start >= 0 else None
        ok = c is not None and any(cr.fullmatch(c.group("cmd")) and fr.fullmatch(flag) for cr, fr in _CMD_FLAG_TABLE)
    if not ok or _PLACEHOLDER.match(value) or value.startswith(("-", "$", "%", "{", "<", "[REDACTED")) or \
            value.lower() in _FLAG_WORD or not (_looks_generated(value) or (
                len(value) >= 6 and re.search(r"[^A-Za-z]", value) and re.search(r"[A-Za-z0-9]", value)) or (
                short and len(value) >= 4) or (value.isalpha() and _letters_value(value, "password", True) == 1)):
        # (a command-table flag takes any value, as on a command line; a long password flag a word of letters that looks
        # like a credential, `AustinCowboys`, but not `TOKEN` or a setting word)
        return m.group(0)
    return m.string[m.start():m.start("value")] + "[REDACTED:argument]" + m.string[m.end("value"):m.end()]


# a flag and its value as two items of a list: `["--password", "X"]` (flow, quoted) and `- --password` / `- X` (block)
_rule("argument", r"(?P<q1>[\"'])(?P<flag>--?[A-Za-z][\w.\-]{0,63})(?P=q1)[ \t]{0,8},[ \t]{0,64}(?:\r?\n[ \t]{0,64})?"
      r"(?P<q>[\"'])(?!\[REDACTED)(?=(?P<value>(?:[^\"'\\\r\n]|\\.){1,4096}))(?P=value)(?P=q)", _list_flag_repl)
_rule("argument", r"(?m)^[ \t]{0,64}-[ \t]{1,8}(?P<q1>[\"']?)(?P<flag>--?[A-Za-z][\w.\-]{0,63})(?P=q1)[ \t]*\r?\n"
      r"[ \t]{0,64}-[ \t]{1,8}(?P<q>[\"']?)(?!\[REDACTED)(?=(?P<value>[^\s\"']{1,4096}))(?P=value)(?P=q)(?=[ \t]*\r?$)",
      _list_flag_repl)
# an XML element whose value is a CDATA section (`<password><![CDATA[X]]></password>`), judged like the element rule's
# value without `<` (each section is read to the next `<` at most)
_rule("assignment", r"<(?=[A-Za-z_])(?P<tag>[\w.:\-]{1,64})(?P<attrs>[ \t\r\n][^<>]{0,256})?>[ \t\r\n]{0,64}<!\[CDATA\["
      r"(?!\[REDACTED)(?=(?P<value>(?:[^\]<]|\](?!\]>)|\]\]><!\[CDATA\[)+))(?P=value)\]\]>", _xml_repl)
# (sections one after another, `<![CDATA[X]]><![CDATA[Y]]>`, are one value)

# words without which a rule cannot match (_NEEDS; lower case, any one of them). A rule judged by its name (_judge,
# _secret_name) needs one of the stems a secret name holds; the others need their command or keyword.
_FOREIGN_WORDS = ("kennwort", "wachtwoord", "lösenord", "losenord", "adgangskode", "salasana", "hasło", "haslo", "heslo",
                  "jelszó", "jelszo", "contraseña", "contrasena", "senha", "şifre", "sifre", "parola", "пароль", "密码",
                  "密碼", "口令", "パスワード", "비밀번호", "סיסמה", "κωδικός", "sandi", "mật", "mat khau", "كلمة")
_SECRET_STEMS = ("pass", "pwd", "secret", "key", "token", "credential", "connection", "dsn", "salt", "psk", "pat",
                 "auth", "sig", "cookie", "sess", "login", "identified") + _FOREIGN_WORDS
_NEED_TABLE = [
    (r"(?P<pre>\b(?:mysql(?:dump", ("mysql", "mariadb")),
    (r"(?P<pre>\bcurl\b[^\n|;&]{0,300}?[ \t](?:-u|--user)", ("curl",)),
    (r"(?:docker|podman)[ \t]+login", ("login",)),
    (r"(?P<pre>openssl\b", ("openssl",)),
    (r"(?P<pre>vault[ \t]+login", ("vault",)),
    (r"(?P<pre>(?:smbclient|rpcclient", ("smbclient", "rpcclient", "smbmap", "net")),
    (r"(?P<pre>(?:sqlplus|sqlldr", ("sqlplus", "sqlldr", "expdp", "impdp", "rman")),
    (r"(?P<pre>(?:aws[ \t]+configure", ("config", "user-secrets")),
    (r"(?P<pre>\b(?:machine[ \t]+", ("machine", "default")),
    (r"\d{8,10}:AA", (":aa",)),
    (r"(?:x-api-key|api-key|x-auth-token", ("api-key", "auth-token", "access-token", "private-token", "api-token")),
    (r"\bauthorization[ \t]{0,16}:", ("authorization",)),
    (r"(?P<pre>cookie(?<!\wcookie)", ("cookie",)),
    (r"(?P<pre>curl\b[^\n|;&]{0,300}?[ \t](?:-b|--cookie)", ("curl",)),
    (r"(?:AccountKey|SharedAccessKey", ("accountkey", "sharedaccess")),
    (r"_(?:authToken|password)[ \t]{0,16}=", ("_authtoken", "_password")),
    (r"^)_auth[ \t]{0,16}=", ("_auth",)),
    (r"(?P<pre>\"auth\"\s{0,64}:", ('"auth"',)),
    (r"(?P<pre>_(?:auth|AUTH|Auth)", ("_auth",)),
    (r"(?P<pre>(?:password|passwd|pwd)[ \t]{0,16}=[ \t]{0,16})(?=(?P<value>", ("password", "passwd", "pwd")),
    (r"(?P<pre>(?:password|passwd|pwd)[ \t]{0,16}=[ \t]{0,16}\{)", ("password", "passwd", "pwd")),
    (r"<(?P<tag>[A-Za-z_]", ("</",)),
    (r"<(?=[A-Za-z_])(?P<tag>", ("<![cdata[",)),
    (r"(?i)(?:(?:name(?<![\w-]name)|key(?<![\w-]key))", ("value",)),
    (r"(?P<pre>(?:auth|credentials?|login)\w{0,32}", ("auth", "credential", "login")),
    (r"\bConvertTo-SecureString\b", ("convertto-securestring",)),
    (r"\bvariable[ \t]+\"", ("variable",)),
    (r"(?:ENV|ARG)[ \t]{1,16}(?P<name>", ("env", "arg")),
    (r"(?i)(?<![\w.\-])(?P<name>identified", _SECRET_STEMS),
    (r"(?P<pre>\buser[ \t]{1,8}[^\s;'\"]{1,128}[ \t]{1,8}identified", ("identified",)),
    (r"(?:(?<=\.)(?!(?:get|is|has", _SECRET_STEMS),
    (r"(?P<pre>\b(?:proxy_set_header", ("header", "param", "setenv", "passenv")),
    (r"(?:(?<=\.webhook\.office\.com/)", ("office.com",)),
    (r"_\|WARNING:-DO-NOT-SHARE-THIS", ("_|warning",)),
    (r"(?P<pre>\b(?:echo|printf)", ("echo", "printf")),
    (r"(?P<pre>(?<![\w\-])--?(?=(?P<name>", _SECRET_STEMS),
    (r"/(?:pass|password|passwd|pwd|rp)(?P<space>", ("/pass", "/pwd", "/rp")),
    (r"(?:-u|--user|--username)(?P<space>", ("-u", "--user")),
    (r"(?:7z|7za|7zr|rar|unrar)", ("7z", "rar", "lftp")),
    (r"(?:consul[ \t]+kv[ \t]+put", ("kv", "etcdctl", "config", "--name")),
    (r"(?:password|passwd|passphrase|requirepass|masterauth|psk|", ("pass", "requirepass", "masterauth", "psk") +
     _FOREIGN_WORDS),
    (r"(?:\bACL[ \t]+SETUSER", ("acl", "user")),
    (r"(?=@(?:tcp|unix|udp)\()", ("@tcp(", "@unix(", "@udp(")),
    (r"(?P<pre>\b(?:passwords?|passwd", ("pass", "pwd", "secret", "key", "token") + _FOREIGN_WORDS),
    (r"(?P<name>[A-Za-z_][\w.\-]{0,127})(?:[ \t]{2,64}|\t))", _SECRET_STEMS),
    (r"[ \t]{1,16}->[ \t]{1,16}\"(?!\[REDACTED)", ("->",)),
    (r"(?=(?P<value>\d{6,64}))(?P=value)", _SECRET_STEMS),
    (r"[ \t]{1,16}\|[ \t]{1,16})(?!\[REDACTED)", _SECRET_STEMS),
    (r"\[[ \t]{0,4}\w{0,32}[ \t]{0,4}\]", _SECRET_STEMS),
    (r"\{[ \t]{0,8}(?:get|init|set)\b", ("get;", "set;", "init;")),
    (r"\$\{(?=(?P<name>[\w.\-]{1,128}))(?P=name):[-=]?", ("${",)),
    (r"[{\[ ](?!REDACTED:)", _SECRET_STEMS),
    (r"\][ \t]{0,8}=>[ \t]{0,8}(?!\[REDACTED)", ("=>",)),
    (r"(?:process\.env|import\.meta\.env|ENV\.fetch", ("env", "_server")),
]
_NEED_MISSES: list[str] = []      # table entries that matched no rule or more than one (a test keeps this empty)
for _sub, _words in _NEED_TABLE:
    _hits = [rx for _k, rx, _r in _RULES if _sub in rx.pattern]
    if len(_hits) == 1:
        _NEEDS[_hits[0]] = _words
    else:
        _NEED_MISSES.append(_sub)
for _rx in (_QUOTED_LINE, _QUOTED_ASSIGN, _TYPED_ASSIGN, _QUOTED_ESC_ASSIGN, _SUBSCRIPT_ASSIGN, _ENV_ASSIGN,
            _LOWER_ENV_ASSIGN, _YAML_ASSIGN, _LINE_ASSIGN):
    _NEEDS[_rx] = _SECRET_STEMS

# Text longer than a caller's cap is cut before it is redacted (redaction is linear, but a 12 MB tool output
# still costs seconds). CAP_MARGIN extra characters are redacted with it so that a secret straddling the cap is
# still recognised, and none of those extra characters is kept afterwards (see Redactor.capped).
CAP_MARGIN = 1024
# characters of already-redacted strings a Redactor remembers (Redactor.record looks text up instead of redacting it
# twice)
CLEAN_CACHE_CHARS = 4 * 1024 * 1024


MAX_USER_PATTERNS = 100
MAX_USER_PATTERN_LEN = 500
MAX_REDACT_FILE = 64 * 1024


def redact_file_hash(store: Path | None) -> str:
    """SHA-256 of `<store>/redact`, or "absent" (also for a file that is not a plain, small regular file)."""
    import hashlib
    from .store import read_regular
    if not store:
        return "absent"
    data = read_regular(Path(store) / "redact", MAX_REDACT_FILE)
    return hashlib.sha256(data).hexdigest() if data is not None else "absent"


def _sre():
    try:
        from re import _constants as C, _parser as P   # type: ignore[attr-defined]  # Python 3.11+
    except ImportError:                                 # Python 3.10
        import sre_constants as C  # type: ignore[no-redef]
        import sre_parse as P  # type: ignore[no-redef]
    return P, C


def lint_pattern(pat: str) -> str | None:
    """Why a user pattern is refused, or None: too long, invalid, or a repeat inside an unbounded repeat (`(a+)+`,
    `(a|aa)*`, the shapes that take exponential time on a near-miss)."""
    if len(pat) > MAX_USER_PATTERN_LEN:
        return f"longer than {MAX_USER_PATTERN_LEN} characters"
    P, C = _sre()
    try:
        parsed = P.parse(pat)
    except Exception as e:  # noqa: BLE001 - re.error and anything the parser raises
        return f"invalid ({e})"
    repeats = {C.MAX_REPEAT, C.MIN_REPEAT}
    possessive = getattr(C, "POSSESSIVE_REPEAT", None)        # Python 3.11+
    if possessive is not None:
        repeats.add(possessive)

    def walk(items, in_unbounded: bool) -> bool:
        for op, av in items:
            if op in repeats:
                lo, hi, sub = av
                if in_unbounded:
                    return True
                if walk(sub, hi == C.MAXREPEAT):
                    return True
            elif op == C.SUBPATTERN:
                if walk(av[-1], in_unbounded):
                    return True
            elif op == C.BRANCH:
                if in_unbounded:      # `(a|aa)*`: alternatives under a repeat can split a run in many ways
                    return True
                if any(walk(alt, in_unbounded) for alt in av[1]):
                    return True
            elif op in (C.ASSERT, C.ASSERT_NOT):
                if walk(av[1], in_unbounded):
                    return True
        return False
    try:
        if walk(parsed, False):
            return "a repeat inside an unbounded repeat (can take exponential time)"
    except (TypeError, ValueError, AttributeError):
        return "could not be checked"
    return None


# The lint cannot see every slow shape (`(?:a*){40}b`, `\w*\w*\w*\w*!`, `(\w+)\1+!` pass it), so each approved pattern
# is also measured once, in a child process killed after PROBE_TIMEOUT_S, on adversarial inputs of USER_PATTERN_FRAGMENT
# characters made from its own literal characters; one that takes longer is not used, and `devmem status` / `doctor`
# say so. The verdict is kept in devmem's per-user folder (outside the project, so a project cannot vouch for a
# pattern), keyed by the pattern's SHA-256. At run time every match attempt of a user pattern is confined to a window of
# USER_PATTERN_FRAGMENT characters, the size it was measured on (_windowed_subn).
PROBE_TIMEOUT_S = 2.0
USER_PATTERN_FRAGMENT = 4096
_FRAGMENT_OVERLAP = 256
PROBE_VERSION = 1
_PROBE_FILE = "pattern_checks.json"
_PROBE_CHILD = """
import json, re, sys, time
pats, texts = json.loads(sys.stdin.read())
for i, p in enumerate(pats):
    rx = re.compile(p)
    t = time.perf_counter()
    for s in texts[i]:
        rx.subn("X", s)
    print(i, round((time.perf_counter() - t) * 1000), flush=True)
"""


def _probe_texts(pat: str) -> list[str]:
    """Adversarial inputs for one pattern: runs of each of its literal characters (and of a few common ones) at the
    window size, with a near-miss end, interleaved with spaces, and as short lines."""
    lits = sorted({c for c in pat if c.isprintable() and c not in "\\()[]{}?*+|^$."} | {"a", "1", " ", "x", "_"})[:24]
    n = USER_PATTERN_FRAGMENT
    out = ["ab" * (n // 2), "word " * (n // 5), ("a" * 30 + "\n") * (n // 31)]
    for c in lits:
        out += [c * 30, c * (n - 1) + "!", (c + " ") * (n // 2), (c * 7 + "\n") * (n // 8)]
    return [t[:n] for t in out]


def _probe_cache_path() -> Path | None:
    try:
        from .consent import user_data_dir
        return user_data_dir() / _PROBE_FILE
    except Exception:  # noqa: BLE001 - no usable per-user folder: nothing is cached
        return None


def _read_probe_cache() -> dict:
    path = _probe_cache_path()
    if path is None:
        return {}
    try:
        from .store import read_regular
        d = json.loads((read_regular(path, 1024 * 1024) or b"{}").decode("utf-8"))
        return d if isinstance(d, dict) and d.get("version") == PROBE_VERSION else {}
    except (OSError, ValueError):
        return {}


def _write_probe_cache(d: dict) -> None:
    path = _probe_cache_path()
    if path is None:
        return
    try:
        from .store import private_mkdir, write_private
        private_mkdir(path.parent)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        write_private(tmp, json.dumps({**d, "version": PROBE_VERSION}).encode("utf-8"))
        os.replace(tmp, path)
    except OSError:
        pass


def _pattern_sha(pat: str) -> str:
    import hashlib
    return hashlib.sha256(pat.encode("utf-8", "surrogatepass")).hexdigest()


def probe_patterns(pats: list[str], timeout: float = PROBE_TIMEOUT_S) -> dict[str, str | None]:
    """pattern -> None (fast enough) or why it is not used. A pattern measured before is looked up; the others run in a
    child process that reports each one as it finishes; the one running when the child is killed at `timeout` is the
    slow one, and a new child measures the rest. Bounded: at most (slow patterns + 1) * timeout, once per pattern."""
    import subprocess
    import sys

    from . import proc as _proc
    cache = _read_probe_cache()
    out: dict[str, str | None] = {}
    todo = []
    for pat in pats:
        v = cache.get(_pattern_sha(pat))
        if isinstance(v, dict) and "why" in v:
            out[pat] = v["why"]
        elif pat not in todo:
            todo.append(pat)
    changed = bool(todo)
    while todo:
        data = json.dumps([todo, [_probe_texts(t) for t in todo]])
        try:
            proc = _proc.popen([sys.executable, "-I", "-c", _PROBE_CHILD], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        except OSError as e:
            for pat in todo:
                out[pat] = f"could not be measured ({e})"      # not cached: measured again next time
            break
        lines: list[str] = []

        def reader(stream=proc.stdout, sink=lines):
            for line in stream or ():
                sink.append(line)
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            proc.stdin.write(data)            # type: ignore[union-attr]
            proc.stdin.close()                # type: ignore[union-attr]
        except OSError:
            pass
        t.join(timeout)
        timed_out = proc.poll() is None
        if timed_out:
            proc.kill()
        t.join(2.0)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            pass
        done = min(len(lines), len(todo))
        for pat in todo[:done]:
            out[pat] = None
            cache[_pattern_sha(pat)] = {"why": None}
        if done < len(todo):
            slow = todo[done]
            out[slow] = (f"took more than {timeout:g} s on a test input (a regex that can take exponential time)"
                         if timed_out else "failed when it was measured")
            cache[_pattern_sha(slow)] = {"why": out[slow]}
            todo = todo[done + 1:]
        else:
            todo = []
    if changed:
        _write_probe_cache(cache)
    return out


def _windowed_subn(rx: re.Pattern, s: str, repl: str, keep_lines: bool = False) -> tuple[str, int]:
    """rx.subn(repl, s) with every match attempt confined to a window of USER_PATTERN_FRAGMENT characters (windows overlap
    by _FRAGMENT_OVERLAP; a match that overlaps the previous one, or that a later window finds longer, extends it): the
    cost is that of about len(s) / USER_PATTERN_FRAGMENT runs on inputs of the size the pattern was measured on.
    keep_lines: `repl` is literal and is followed by the line breaks of what it replaces, so the text keeps its line count
    (a user pattern whose match crosses a line break, such as `api_key:\\s*\\S+`, empties the lines after the first)."""
    if len(s) <= USER_PATTERN_FRAGMENT:
        if keep_lines:
            return rx.subn(lambda m: repl + "\n" * m.group(0).count("\n"), s)
        return rx.subn(repl, s)
    spans: list[list[int]] = []
    step = USER_PATTERN_FRAGMENT - _FRAGMENT_OVERLAP
    for i in range(0, len(s), step):
        for m in rx.finditer(s, i, min(len(s), i + USER_PATTERN_FRAGMENT)):
            a, b = m.span()
            if b == a:
                continue
            if spans and a < spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], b)
            else:
                spans.append([a, b])
        if i + USER_PATTERN_FRAGMENT >= len(s):
            break
    if not spans:
        return s, 0
    out, prev = [], 0
    for a, b in spans:
        out += [s[prev:a], repl + "\n" * s.count("\n", a, b) if keep_lines else repl]
        prev = b
    out.append(s[prev:])
    return "".join(out), len(spans)


def user_patterns_state(store: Path | None, project: Path | None = None) -> tuple[list[re.Pattern], list[str]]:
    """(patterns to run, problems): the regexes of `<store>/redact` when the file's content is the one this user
    approved outside the project (its SHA-256 in the `devmem init` switch or the /devmem:setup note, written when
    those ran; devmem.consent), each pattern linted. A file that arrived or changed later (a `git pull`) is not run
    until the user approves it by running `devmem init` or /devmem:setup again."""
    if not store:
        return [], []
    current = redact_file_hash(store)
    if current == "absent":
        return [], []
    try:
        from .consent import approved_redact_hashes
        approved = approved_redact_hashes(Path(project) if project else Path(store).parent)
    except Exception:  # noqa: BLE001 - when in doubt, do not run them
        approved = set()
    if current not in approved:
        return [], [f"{Path(store) / 'redact'} is not approved (it is new or changed since you set this project up); "
                    "its patterns are not used. Look at it, then run `devmem init` (or /devmem:setup) again to approve it"]
    return _patterns_of(store)


def _patterns_of(store: Path) -> tuple[list[re.Pattern], list[str]]:
    """The patterns of `<store>/redact` that pass the lint and the measurement (probe_patterns), and the problems."""
    from .store import read_regular
    data = read_regular(Path(store) / "redact", MAX_REDACT_FILE) or b""
    out, problems = [], []
    for line in data.decode("utf-8", "replace").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if len(out) >= MAX_USER_PATTERNS:
            problems.append(f"more than {MAX_USER_PATTERNS} patterns: the rest are not used")
            break
        why = lint_pattern(line)
        if why:
            problems.append(f"pattern {line[:60]!r} refused: {why}")
            continue
        try:
            out.append(re.compile(line))
        except re.error as e:
            problems.append(f"pattern {line[:60]!r} refused: {e}")
    verdicts = probe_patterns([rx.pattern for rx in out]) if out else {}
    kept = []
    for rx in out:
        why = verdicts.get(rx.pattern)
        if why:
            problems.append(f"pattern {rx.pattern[:60]!r} not used: {why}")
        else:
            kept.append(rx)
    return kept, problems


def check_patterns_file(store: Path | None) -> list[str]:
    """Measure the patterns of `<store>/redact` now (the user is approving the file: `devmem init`, /devmem:setup), so
    the hooks find the verdicts cached and never wait for a measurement. Returns the problems; never raises."""
    try:
        if not store or redact_file_hash(store) == "absent":
            return []
        return _patterns_of(Path(store))[1]
    except Exception:  # noqa: BLE001
        return []


def load_user_patterns(store: Path | None, project: Path | None = None) -> list[re.Pattern]:
    """The approved, linted regexes of `<store>/redact` (user_patterns_state); never raises."""
    try:
        return user_patterns_state(store, project)[0]
    except Exception:  # noqa: BLE001
        return []


# ---- the shapes text takes on its way to the store. Tools serialise (a tool response becomes JSON: quotes and newlines
# escaped) and decorate lines (grep `path:12:`, `cat -n`, the Read tool's `    12→`, diff `+`/`-`, quote and comment
# markers, a trailing comment). The rules above are written for plain lines; two passes give every rule, present and
# future, the plain text as well:
#   - Redactor._json_strings: each JSON string literal of 8 or more characters is decoded and redacted as the plain
#     text it is (all of them together, one pass of every rule), also a literal cut off at the end of the text, up to
#     MAX_JSON_DEPTH levels of JSON inside JSON. Only a literal whose redaction changed is written back (encoded
#     again); every other one keeps its exact text.
#   - Redactor._line_view: the lines with a tool prefix (_LINE_PREFIX) or a trailing comment are redacted once more
#     without them, by every rule (a rule that needs a line start or end, a lookbehind that rejects the `-` of a diff,
#     netrc across lines), and only what the rules change is written back, the prefix and the comment kept.
# Both are linear: a literal is only entered at an unescaped quote and its body taken in one go (a quote that starts
# one ends every earlier one, so at most one start per line fails), and a line's prefix is matched once.

TREE_LEAVES = 1000       # strings (keys and values) one Redactor.tree call redacts; the rest of the structure is left out
MAX_KEY = 1024           # characters of a dictionary key that are redacted and kept (Redactor._key)
MAX_JSON_DEPTH = 8
_PIECE_SEP = "\n\x00\n"      # between pieces redacted together (no rule's run crosses a NUL)
_JSON_STRING = re.compile(r'(?<!\\)"(?=(?P<body>(?:[^"\\\n]|\\.)*))(?P=body)(?:"|\\?(?:\Z|(?=\n\x00\n)))')
_JSON_PUNCT = re.compile(r"[ \t\r\n,:{}\[\]]*")       # between two string literals of one JSON run (_json_strings)
_KEY_AFTER = re.compile(r"[ \t\r\n]*:")                # a literal followed by this is a key, not a value
_LENIENT_ESCAPE = re.compile(r"\\(.)", re.S)
_LENIENT_MAP = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "/": "/"}
_SURROGATE = re.compile("[\ud800-\udfff]")


def _decode_literal(body: str) -> tuple[str, bool]:
    """(text, strict): a JSON string body decoded. strict=False when it is not valid JSON (cut off inside an escape, a
    Windows path, `\\x41`): then only \\n \\r \\t \\" \\/ are decoded and every other pair is kept as it is."""
    if "\\" not in body:
        return body, True
    try:
        return json.loads('"' + body + '"', strict=False), True
    except ValueError:
        return _LENIENT_ESCAPE.sub(lambda m: _LENIENT_MAP.get(m.group(1), m.group(0)), body), False


def _encode_literal(text: str, strict: bool) -> str:
    if not strict:
        return text.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t").replace('"', '\\"')
    return json.dumps(text, ensure_ascii=bool(_SURROGATE.search(text)))[1:-1]


# a tool's decoration of a line: grep `path:12:` / `path-12-` / `path:` (a path with a slash or a dot), then up to four
# of: a line number (`cat -n` `     5<TAB>`, the Read tool `     5→`, `12: `, `12 | `, `12. `), a diff or quote marker
# (`+ - ! < >`), a comment marker (`#`, `//`, `/*`, `*`, `--`, `;`, `%`, `REM`, `::`, `<!--`)
_LINE_PREFIX = re.compile(
    r"(?:(?=[\w.~/\\])(?:[A-Za-z]:(?=[\\/])(?!//))?[^\s:]{1,1024}?(?::\d{1,9}[:\-]|-\d{1,9}-)"
    r"|(?=[\w.~/\\])(?:[A-Za-z]:(?=[\\/])(?!//))?(?=[^\s:]{0,1024}[/\\.])[^\s:]{1,1024}:(?!//))?"
    r"(?:[ \t]{0,64}(?!-{1,5}(?i:BEGIN|END)\b)(?:\d{1,9}(?:\t|\u2192|[ \t]{0,4}[|\u2502:](?!\d)|[.)](?=[ \t])|[ \t]{1,4}(?=\S))"
    r"|[+!<]|-(?![ \t]{1,8}[\w.\-\"']{1,128}:[ \t]{0,8}[|>])|(?:>|#{1,6}|/{2,3}|/\*{1,2}|\*{1,2}|--|;{1,2}|%|REM\b|::|<!--)[ \t]?)){0,4}")
# the same with a `-` before a YAML key taken for a diff marker (the second reading of `-  key: |`)
_LINE_PREFIX_DASH = re.compile(
    r"(?:(?=[\w.~/\\])(?:[A-Za-z]:(?=[\\/])(?!//))?[^\s:]{1,1024}?(?::\d{1,9}[:\-]|-\d{1,9}-)"
    r"|(?=[\w.~/\\])(?:[A-Za-z]:(?=[\\/])(?!//))?(?=[^\s:]{0,1024}[/\\.])[^\s:]{1,1024}:(?!//))?"
    r"(?:[ \t]{0,64}(?!-{1,5}(?i:BEGIN|END)\b)(?:\d{1,9}(?:\t|\u2192|[ \t]{0,4}[|\u2502:](?!\d)|[.)](?=[ \t])|[ \t]{1,4}(?=\S))"
    r"|[+\-!<]|(?:>|#{1,6}|/{2,3}|/\*{1,2}|\*{1,2}|--|;{1,2}|%|REM\b|::|<!--)[ \t]?)){0,4}")
# one decoration only (a line number, a diff or quote marker): what follows it is content, also when it starts like a
# marker itself (a block body line `- item`, `# x`); run as a further reading next to the full one
_LINE_PREFIX_ONE = re.compile(
    r"(?:(?=[\w.~/\\])(?:[A-Za-z]:(?=[\\/])(?!//))?[^\s:]{1,1024}?(?::\d{1,9}[:\-]|-\d{1,9}-)"
    r"|(?=[\w.~/\\])(?:[A-Za-z]:(?=[\\/])(?!//))?(?=[^\s:]{0,1024}[/\\.])[^\s:]{1,1024}:(?!//))?"
    r"(?:[ \t]{0,64}(?!-{1,5}(?i:BEGIN|END)\b)(?:\d{1,9}(?:\t|\u2192|[ \t]{0,4}[|\u2502:](?!\d)|[.)](?=[ \t])|[ \t]{1,4}(?=\S))"
    r"|[+!<]|-(?![ \t]{1,8}[\w.\-\"']{1,128}:[ \t]{0,8}[|>])|(?:>|#{1,6}|/{2,3}|/\*{1,2}|\*{1,2}|--|;{1,2}|%|REM\b|::|<!--)[ \t]?)){0,1}")
_DASH_BLOCK = re.compile(r"(?m)^-[ \t]{1,8}[\w.\"'][\w.\-\"']{0,127}:[ \t]{0,8}[|>]")   # (a diff marker is at column 0)
_TRAILING_COMMENT = re.compile(r"[ \t](?:#|//)")   # one blank before the marker (a run of blanks is never retried)
# terminal escape sequences: CSI (colours `\x1b[31m`, `\x1b[K`), OSC (titles, hyperlinks `\x1b]8;;url\x07`), and the
# two-character ones
_ANSI = re.compile(r"\x1b(?:\[[0-9;:?<=>]{0,64}[ -/]{0,8}[@-~]|\][^\x07\x1b\n]{0,4096}(?:\x07|\x1b\\)?|[@-Z\\-_])")


def _splice(lines: list[str], spans: list, view: list[str], red: list[str]) -> str:
    """Write a redacted view back when a multi-line match (a key block, a YAML block scalar) folded lines into one: the
    lines are aligned greedily (an unchanged line is the same in both; a folded group ends where the next lines agree
    again), each folded group becomes its first line's prefix, the redacted line and its last line's comment."""
    out, i, j, n, m = [], 0, 0, len(view), len(red)
    while i < n and j < m:
        if view[i] == red[j]:
            out.append(lines[i])
            i, j = i + 1, j + 1
            continue
        extra = max(0, (n - i) - (m - j))
        k = i + 1 + extra
        for kk in range(i + 1, i + 2 + extra):
            if (kk == n and j + 1 == m) or (kk < n and j + 1 < m and view[kk] == red[j + 1]):
                k = kk
                break
        first, last = spans[i], spans[min(k, n) - 1]
        out.append((lines[i][:first[0]] if first else "") + red[j] + (lines[min(k, n) - 1][last[1]:] if last else ""))
        i, j = k, j + 1
    out.extend(lines[i:])
    return "\n".join(out)


def _join_items(items: list[str]) -> tuple[str, list[tuple[int, bool]]]:
    """Sibling strings as one text, one item after the other on its own line(s) (an item that ends with a newline, a
    notebook `source` line, is not given a second one), and the layout _split_items needs to cut it back."""
    parts, layout = [], []
    for t in items:
        nl = t.endswith("\n")
        body = t[:-1] if nl else t
        parts.append(body)
        layout.append((body.count("\n") + 1, nl))
    return "\n".join(parts), layout


_UNALIGNED = "[REDACTED:lines]"     # an item whose lines a redaction that changed the line count ran through


def _split_items(layout: list[tuple[int, bool]], joined: str, red: str, cut: bool = False,
                 kept: bool = False) -> tuple[list[str], int]:
    """(`red`, a redaction of the text _join_items made, cut back into one string per item; the number of items that
    got any of it). Every rule keeps the line count (a built-in one empties a key block's lines, a user pattern's
    replacement is followed by the line breaks of what it replaced, and the omission of a secret file's lines in a run
    empties each line in place: Redactor._run), so line i of `red` is line i of `joined` and the text is cut back by
    position: also when `red` was cut at the end (`cut`) and the redaction before the cut kept the line count (`kept`).
    Should the count still differ (no rule is known to do it: a safety net), no text moves from one item into another:
    the equal lines at the start and (without a cut) at the end keep their items, and each item with a line between
    them gets one `[REDACTED:lines]` in place of those lines. Only text of `red` and that marker are returned; a list
    keeps its length."""
    old, new = joined.split("\n"), red.split("\n")
    line_item: list[int] = []
    for t, (c, _nl) in enumerate(layout):
        line_item += [t] * c
    got: list[list[str]] = [[] for _ in layout]
    n, m = len(old), len(new)
    if (n == m and not cut) or (cut and kept and m <= n):
        for j in range(m):
            got[line_item[j]].append(new[j])
    else:
        p, lim = 0, min(n, m)
        while p < lim and old[p] == new[p]:
            p += 1
        q = 0
        while not cut and q < lim - p and old[n - 1 - q] == new[m - 1 - q]:
            q += 1
        for j in range(p):
            got[line_item[j]].append(new[j])
        # (after a cut, the items as far as `red` goes: those after it are left out)
        end = n - q if not cut else min(n, m) if p < m else p
        for t in dict.fromkeys(line_item[p:end]):
            got[t].append(_UNALIGNED)
        for j in range(q):
            got[line_item[n - q + j]].append(new[m - q + j])
    used = max((t + 1 for t, ls in enumerate(got) if ls), default=0)
    return ["\n".join(ls) + ("\n" if nl and ls else "") for ls, (_c, nl) in zip(got, layout)], used


def _text_block(v) -> bool:
    """A content block `{"type": "text", "text": "..."}` (MCP results, stream-json messages): its text is one of the
    sibling strings of the list it is in."""
    return isinstance(v, dict) and v.get("type") == "text" and isinstance(v.get("text"), str)


def _run_end(items: list | tuple, i: int, limit: int) -> int:
    """The end of the run of consecutive strings (or text blocks) that starts at items[i], at most `limit` long; a second
    empty string in a row ends it (a blank line ends every multi-line construct, and a list of empty strings costs
    nothing to walk one by one)."""
    j, n = i + 1, min(len(items), i + max(1, limit))
    while j < n and (isinstance(items[j], str) or _text_block(items[j])) and not (items[j] == "" and items[j - 1] == ""):
        j += 1
    return j


def _pair_key(key: str) -> bool:
    """A dictionary key under which a string value is judged together with its key (Redactor._pair)."""
    if len(key) > 128 or not _secret_name(_last_segment(key), env=True):
        return False
    return not (_BENIGN_NAME.search(key) and not (_HARD_SECRET.search(key) and _SOFT_SUFFIX.search(key)))


def _pair_name(d: dict) -> str | None:
    """`{"name": "DB_PASSWORD", "value": ...}` (Kubernetes env, app settings): the secret name, else None."""
    if not isinstance(_value_of(d), str):
        return None
    for k in ("name", "key", "Name", "Key", "NAME", "KEY"):
        n = d.get(k)
        if isinstance(n, str):
            n = n.rstrip("/").rsplit("/", 1)[-1]       # (an SSM parameter path: `/prod/db/DB_PASSWORD`)
            if _pair_key(n) or _SESSION_COOKIE.match(n):
                return n
    return None


def _value_of(d: dict):
    """The value of a name/value pair under `value` in any case (`Value`: SSM, Azure)."""
    for k in ("value", "Value", "VALUE"):
        if k in d:
            return d[k]
    return None


_CUT_XML_OPEN = re.compile(r"<(?P<tag>[A-Za-z_][\w.:\-]{0,63})(?:[ \t\r\n][^<>]{0,256})?>")
_CUT_XML_NAME = re.compile(r"(?i)\b(?:key|name)[ \t]{0,8}=[ \t]{0,8}[\"']([^\"'<>]{1,128})[\"']")
_PROBE_VALUE = "A1b2C3d4E5f6"


def _close_cut(red: str) -> str:
    """The kept part of a text cut by a cap, when it ends inside a construct opened under a secret name whose end lay
    past the cap and the margin: from the opening to the end of the kept text is redacted (fail closed), its lines
    emptied. Each branch asks what the rule it stands in for asks of the text so far: a TOML/Python multi-line string
    whose opener ends its line or starts a value, within 4 KB; an XML element whose value so far is one token per line
    (no blank inside a line: the element rule keeps a sentence); a quoted value whose real opening quote (found by
    scanning forward from its name, escapes honoured) is not closed, holding nothing the quoted rule would refuse."""
    for q in ("'''", '"""'):
        i = red.rfind(q)
        if i > 0:
            j = i
            while j > 0 and red[j - 1] in " \t":
                j -= 1
            rest = red[i + 3:]
            if j > 0 and red[j - 1] == "=" and len(rest) <= 4096 and _TRIPLE_OPEN_REST.match(rest):
                name = _name_before(red, j - 1)
                if name and rest.strip() and not rest.startswith("[REDACTED") and _judge(name, _PROBE_VALUE) and \
                        not _prose_lines(rest):
                    return red[:i + 3] + "[REDACTED:assignment]" + "\n" * rest.count("\n")
    lt = red.rfind("<")
    if lt > 0 and red.startswith("<![CDATA[", lt) and "]]>" not in red[lt:]:
        lt = red.rfind("<", 0, lt)          # an open CDATA section: the element before it names the value
    if lt >= 0:
        m = _CUT_XML_OPEN.match(red, lt)
        rest = red[m.end():] if m else ""
        if m and rest.strip() and not rest.startswith("[REDACTED") and \
                not any(" " in ln.strip() or "\t" in ln.strip() for ln in rest.split("\n")):
            tag = m.group("tag").rsplit(":", 1)[-1]
            name = tag
            if tag.lower() in _LIST_TAGS:
                a = _CUT_XML_NAME.search(m.group(0))
                name = a.group(1) if a else ""
            if name and _judge(name, _PROBE_VALUE):
                return red[:m.end()] + "[REDACTED:assignment]" + "\n" * rest.count("\n")
    # a quoted value still open at the cut: `{"password": "...`, `KEY="...`, the `"value": "...` / `value="...` of a
    # name/value pair. The openers in the last 8 KB are tried from the last one back (at most eight): the first whose
    # string, read with its escapes, runs to the end of the text unclosed is the value
    lo = max(0, len(red) - 8192)
    opens = list(_CUT_QUOTE_OPEN.finditer(red, lo))[-8:]
    for m in reversed(opens):
        qc = red[m.end() - 1]
        body = (_DQ_BODY if qc == '"' else _SQ_BODY).match(red, m.end())
        if body is None or body.end() != len(red):
            continue
        rest = red[m.end():]
        if not rest.strip() or rest.startswith("[REDACTED"):
            break
        name = m.group("name")
        pair = name is None or name.lower() == "value"
        if pair:
            pairs = list(_CUT_PAIR_NAME.finditer(red, max(0, m.start() - 400), m.start()))
            name = pairs[-1].group("n").rstrip("/").rsplit("/", 1)[-1] if pairs else None
        elif re.search(r"\s", rest):
            break                   # (the quoted rule takes no blank inside a value; a pair value may hold one)
        if name and _judge(name, _PROBE_VALUE):
            return red[:m.end()] + "[REDACTED:assignment]" + "\n" * rest.count("\n")
        break
    return red


_TRIPLE_OPEN_REST = re.compile(r"[ \t]*\r?\n|[^\s\"'`]")
_CUT_QUOTE_OPEN = re.compile(r"(?:(?i:(?<![\w-])value)[\"']?[ \t]{0,8}[:=][ \t]{0,8}|(?P<qn>[\"']?)"
                             r"(?P<name>[\w.\-]{1,128})(?P=qn)[ \t]{0,8}[:=][ \t]{0,8})[\"']")
_CUT_PAIR_NAME = re.compile(r"(?i)(?<![\w-])(?:name|key)[\"']?[ \t]{0,8}[:=][ \t]{0,8}[\"']?(?P<n>[\w.:/\-]{1,128})")


# `N:`/`N-` with or without a path (grep, rg, findstr /n; PowerShell Select-String -Context puts `> ` or two blanks
# before the path), and `git blame`'s `<hash> [path] (<author> <date> N) ` (an author holding a parenthesis is not read)
_NUM_PREFIX = re.compile(r"(?:[> ] (?=[\w.~/\\]))?(?:(?=[\w.~/\\])[^\s:]{1,1024}?[:\-])?(\d{1,9})[:\-]"
                         r"|\^?[0-9a-f]{7,40}(?: [^\s(]{1,1024})? \([^()\n]{0,512}? (\d{1,9})\) ")


def _numbered_runs(lines: list[str]) -> dict[int, int]:
    """Lines of numbered output (`grep -n`, `grep -n -C2` on one file or several, `rg -n`, `nl`-like `N:`): a run of
    two or more lines whose numbers go up by one, each followed by `:` or `-` (grep's `--` group separator may stand
    between groups), is numbered whatever each line's content starts with (a digit, a `-`, a `#`). Returns the end of
    each such line's prefix. One pass over the lines, each read once."""
    found: dict[int, int] = {}
    prev_num, prev_i, run = None, -2, []
    for i, ln in enumerate(lines):
        m = _NUM_PREFIX.match(ln)
        if m:
            num = int(m.group(1) or m.group(2))
            if prev_num is not None and num == prev_num + 1 and prev_i == i - 1:
                run.append((i, m.end()))
            else:
                if len(run) >= 2:
                    found.update(run)
                run = [(i, m.end())]
            prev_num, prev_i = num, i
        elif ln == "--" and run:
            prev_i = i            # (a group separator: the next group's numbers need not follow on)
            prev_num = None
            if len(run) >= 2:
                found.update(run)
            run = []
        else:
            if len(run) >= 2:
                found.update(run)
            run, prev_num = [], None
    if len(run) >= 2:
        found.update(run)
    return found


# --- a diff that edits a value: each hunk read as its old text and its new text (Redactor._diff_sides) ----------------
# In a unified diff the name of a value is often on an unchanged context line and the value on `-`/`+` lines, so the
# old and the new value interleave and no rule sees name and value together. Each hunk (after an `@@ -a,b +c,d @@`
# header, `@@@` for a combined diff; or a run of marked lines without a header: an Edit tool's structuredPatch) is
# rebuilt as its old text (context and removed lines) and its new text (context and added lines), markers stripped,
# each side is redacted, and the lines are mapped back: a removed line from the old side, an added line from the new
# side, a context line from whichever side changed it. A line the mapping cannot place fails closed.
MAX_DIFF_HUNKS = 100_000        # hunks of one text read as sides; the changed lines of the rest fail closed
MAX_DIFF_HUNK_LINES = 100_000   # lines of one hunk; the changed lines of a longer hunk fail closed
DIFF_FAILED = "[REDACTED:diff]"   # a line of a hunk that could not be read as sides
WORD_DIFF_REACH = 1              # lines after a redacted (or replaced) line whose --word-diff changes are replaced
# --- the work of one redaction call (Redactor.text) --------------------------------------------------------------------
# The plain reading of a text hands it to the rule pass once (_rules, with its decorated lines once more: _line_view).
# Every other reading hands the rule pass a text rebuilt from it: the JSON string literals decoded (_json_strings), the
# sides of a diff's hunks (_diff_sides), the columns of `diff -y` (_columns); and a rebuilt text is read the same way
# again (a diff inside a JSON string inside a diff). All of these draw from ONE budget per call: READ_FACTOR times the
# text's length plus READ_BASE characters, each reading charged its length plus READ_CALL (a call of the rule pass costs
# that much even on a short text) before it runs; identical texts are read once per call (a literal on a context line
# is in both sides of its hunk; parents of a combined diff often have the same old side). A diff or column reading
# inside another one is the last (MAX_SIDE_LEVEL: the sides of a diff of a patch file); JSON inside JSON stops at
# MAX_JSON_DEPTH as before. A reading the budget refuses fails closed instead of being skipped, and what it would have
# read is replaced whole: every line of the refused hunk and of every hunk after it in the same text (later commits of
# a `git log -p` included), and every row of a refused `diff -y` block and of every block after it, by DIFF_FAILED; a JSON string a side reading would have
# decoded by BUDGET_FAILED (unless it is one line that decodes to its own text: the plain reading of the text around
# it read exactly that); a text whose second reading (_cr_text) is refused, every line of it by BUDGET_FAILED.
# A reservation is used up by the main pass of the reading it was made for, so no work is counted twice.
READ_FACTOR = 3
class _BudgetSpent(Exception):
    """A side reading's further pass would go past the call's budget (Redactor._view_pass)."""


# (a return followed only by blanks and terminal colour codes to its line's end is no line end: git colours a CRLF
# line's trailing `\r` as a whitespace error, `\r\x1b[m`)
_LONE_CR = re.compile(r"\r(?!(?:[ \t]|\x1b\[[0-9;:]{0,32}[A-Za-z]){0,4096}(?:\n|\Z))")
_LF = re.compile(r"\n")
# (4,096: at the 2,000-character cap of a tool's output the sides and columns of an ordinary `diff -y` hand the rules
# up to 3.3 times the plain readings; with 1,024 such windows of the Python library's `diff -y` used the budget up)
READ_BASE = 4096
READ_CALL = 64
READ_BATCH = 4096
READ_PASS = 16
MAX_SIDE_LEVEL = 2
BUDGET_FAILED = "[REDACTED:budget]"
# a hunk header; a combined diff has one `@` more per parent and one `-a,b` range per parent (at most eight)
_DIFF_HEADER = re.compile(r"(@@@{0,8}) ((?:-\d{1,10}(?:,\d{1,10})? ){1,8})\+\d{1,10}(?:,(\d{1,10}))? \1(?!@)")
_DIFF_RANGE = re.compile(r"-\d{1,10}(?:,(\d{1,10}))?")
_CAT_PREFIX = re.compile(r"[ \t]{0,12}(\d{1,9})(?:\t|→)")     # `cat -n`, `nl`, the Read tool's `N→`
_DIFF_HINTS = ("\n+", "\n-", "@@ -", ":+", ":-", "-+", "--", "\t+", "\t-", "→+", "→-")


def _diff_hint(s: str) -> bool:
    """Can `s` hold a changed diff line (a `+`/`-` marker at a line start or after a line-number decoration)?"""
    return s[:1] in ("+", "-") or any(h in s for h in _DIFF_HINTS)


# `git log --graph`: the graph's columns before each line (`| `, `| | `, `* `, a `|` alone on an empty line)
_GRAPH_GUTTER = re.compile(r"(?:[|*/\\] ){1,8}|\|$")


def _graph_runs(lines: list[str]) -> dict[int, int]:
    """Lines of `git log --graph -p`: a run of two or more lines that each start with graph columns and hold a hunk
    header or a `diff --git` line after them. Returns the end of each such line's graph columns. One pass."""
    found: dict[int, int] = {}
    run: list[tuple[int, int]] = []
    hdr = False
    for i, ln in enumerate(lines):
        m = _GRAPH_GUTTER.match(ln) if ln[:1] in "|*/\\" else None
        if m:
            run.append((i, m.end()))
            hdr = hdr or ln.startswith(("@@", "diff --git"), m.end())
            continue
        if len(run) >= 2 and hdr:
            found.update(run)
        run, hdr = [], False
    if len(run) >= 2 and hdr:
        found.update(run)
    return found


def _or(v: str | None, default: str) -> str:
    return default if v is None else v


def _undecorated(plain: list[str]) -> list[str]:
    """Lines without their tool decoration: a numbered run's prefix, `cat -n`/Read numbers, `git log --graph` columns."""
    deco = _numbered_runs(plain)
    deco.update(_cat_runs(plain))
    deco.update(_graph_runs(plain))
    return [p[deco[i]:] if i in deco else p for i, p in enumerate(plain)]


def _cat_runs(lines: list[str]) -> dict[int, int]:
    """Lines of `cat -n`/`nl`/Read-tool output: a run of two or more lines whose numbers go up by one. Returns the end
    of each such line's prefix. One pass, each line read once."""
    found: dict[int, int] = {}
    prev, run = None, []
    for i, ln in enumerate(lines):
        m = _CAT_PREFIX.match(ln)
        num = int(m.group(1)) if m else None
        if m and prev is not None and num == prev + 1 and run and run[-1][0] == i - 1:
            run.append((i, m.end()))
        else:
            if len(run) >= 2:
                found.update(run)
            run = [(i, m.end())] if m else []
        prev = num
    if len(run) >= 2:
        found.update(run)
    return found


def _diff_hunks(body: list[str], headerless: bool = True) -> list[tuple[int, int, int]]:
    """The hunks of a diff, as (first content line, end, marker columns), in order. A hunk after a header runs as far as
    the header's line counts and its lines' markers allow (a text cut short ends it early; an empty line is an empty
    context line), and on over the marked lines that follow (a header whose counts are too short); `\\ No newline at
    end of file` lines stay in it and belong to neither side. A NUL line (between texts read together) ends every
    hunk. Among the lines outside such hunks, each maximal run of lines that start with ` `, `+` or `-` and hold at
    least one context line and one changed line is a hunk of one column (an Edit tool's structuredPatch has no header;
    with headerless=False such runs are not read). Linear: each line is read once by the header scan and once by the
    run scan."""
    hunks: list[tuple[int, int, int]] = []
    n = len(body)
    taken = [False] * n
    i = 0
    while i < n:
        m = _DIFF_HEADER.match(body[i])
        cols = len(m.group(1)) - 1 if m else 0
        ranges = _DIFF_RANGE.findall(m.group(2)) if m else []
        if not m or len(ranges) != cols:
            i += 1
            continue
        olds = [1 if c == "" else int(c) for c in ranges]
        new = 1 if m.group(3) is None else int(m.group(3))
        taken[i] = True
        if cols == 1:
            # `git diff --word-diff`: no line markers; `[-old-]` and `{+new+}` inside the lines, and lines that start
            # with no marker at all. The hunk runs to the next hunk, file or commit header (column count 0: read by
            # _word_sides). Each line is read once here, once more if the hunk is a unified one after all
            k, marked, bare, changed = i + 1, False, False, False
            while k < n and k - i <= MAX_DIFF_HUNK_LINES and not _WORD_HUNK_END.match(body[k]) and body[k] != "\x00":
                marked = marked or "[-" in body[k] or "{+" in body[k]
                bare = bare or body[k][:1] not in ("", " ", "+", "-", "\\")
                changed = changed or body[k][:1] in ("+", "-")
                k += 1
            # (a hunk of indented lines only, none of them a changed line of a unified diff, is a word diff too)
            if marked and (bare or not changed):
                hunks.append((i + 1, k, 0))
                for t in range(i + 1, k):
                    taken[t] = True
                i = k
                continue
        j = i + 1
        while j < n and (new > 0 or any(olds)):
            b = body[j]
            if b.startswith("\\"):
                j += 1
                continue
            mk = b[:cols] if b else " " * cols
            if len(mk) < cols or mk.strip(" +-"):
                break
            in_new = "-" not in mk
            if (in_new and new <= 0) or any(olds[k] <= 0 for k in range(cols)
                                             if mk[k] == "-" or (in_new and mk[k] != "+")):
                break       # (more lines than the header counts: the rest is no part of this hunk)
            for k in range(cols):
                if mk[k] == "-" or (in_new and mk[k] != "+"):
                    olds[k] -= 1
            if in_new:
                new -= 1
            j += 1
        # (a header whose counts are too short: the marked lines right after the counted ones are read with the hunk,
        # up to an empty line, a line without markers or the next header; fail closed rather than trust the counts)
        while j < n and body[j] and (body[j].startswith("\\") or (
                len(body[j]) >= cols and not body[j][:cols].strip(" +-") and not _DIFF_HEADER.match(body[j]))):
            j += 1
        if j > i + 1:
            hunks.append((i + 1, j, cols))
            for k in range(i + 1, j):
                taken[k] = True
        i = max(j, i + 1)
    i = 0
    while i < n and headerless:
        if taken[i] or not body[i] or body[i][0] not in " +-":
            i += 1
            continue
        j, ctx, chg = i, False, False
        while j < n and not taken[j] and body[j] and body[j][0] in " +-\\":
            ctx = ctx or body[j][0] == " "
            chg = chg or body[j][0] in "+-"
            j += 1
        if ctx and chg:
            hunks.append((i, j, 1))
        i = j
    hunks.sort()
    return hunks


_WORD_HUNK_END = re.compile(r"@@ |diff --git |commit [0-9a-f]{7,64}\b")
# `diff -y` (side by side): the gutter mark (`|` changed, `<` only left, `>` only right) after blanks or tabs
_SDIFF_GUTTER = re.compile(r"(?m)[\t ] {0,7}[|<>](?:\t|\r?$)")


def _display_col(s: str) -> int:
    """The column a terminal is at after printing `s`: a tab goes to the next multiple of 8, a carriage return back to
    0 (as GNU `diff -y` counts: the left column of a CRLF file ends with its `\\r`, and the padding after it counts
    from 0). expandtabs restarts its count after a `\\r` too, so only the part after the last one is the column."""
    e = s.expandtabs(8)
    return len(e) - (e.rfind("\r") + 1)


def _sdiff_cells(ln: str, g: int, rs: int) -> tuple[int, int, str] | None:
    """A line of `diff -y` output whose gutter mark is at column g and right column at rs: (end of the left column's
    text, start of the right column, the gutter mark or " "), or None when the line has text where the gutter is.
    Columns as _display_col counts them; one pass over the line up to the right column."""
    col, i, n, end, mark = 0, 0, len(ln), 0, " "
    while i < n and col < rs:
        c = ln[i]
        if c == "\t":
            col = (col // 8 + 1) * 8
        elif c == "\r":
            col = 0
        elif c == " ":
            col += 1
        else:
            if col == g and c in "|<>" and mark == " " and i and ln[i - 1] in " \t":
                mark = c
            elif col >= g - 1:
                return None
            else:
                end = i + 1
            col += 1
        i += 1
    return end, i, mark


def _sdiff_runs(lines: list[str]) -> list[tuple[int, int, int, int]]:
    """The blocks of `diff -y` output in `lines`, as (first line, end, gutter column, right column start), columns
    counted as a terminal does (_display_col). The gutter column is the one most lines with a gutter mark agree on; a
    block is a run of two or more lines that each have that mark there (with a blank before it), or blanks from just
    before it to the right column with text on both sides (an unchanged line), or nothing; it holds at least one gutter
    mark. Linear: each line is read a constant number of times."""
    cols: dict[int, int] = {}
    tabbed: dict[int, int] = {}
    marks: dict[int, int] = {}
    for i, ln in enumerate(lines):
        if "\t" not in ln and "  " not in ln:
            continue
        m = _SDIFF_GUTTER.search(ln)
        if m:
            col = _display_col(ln[:m.start() + len(m.group()) - len(m.group().lstrip("\t "))])    # the mark's
            marks[i] = col
            cols[col] = cols.get(col, 0) + 1
            if m.group().endswith("\t"):
                tabbed[col] = tabbed.get(col, 0) + 1
    if not cols:
        return []
    # (a mark followed by the tab to the right column counts first: a `|` that ends a line, as a YAML `key: |` in the
    # right column does, is no gutter of its own; then the column most lines agree on, then the rightmost: a `key: |`
    # of the left column followed by the padding tab is left of the gutter)
    g = max(cols, key=lambda c: (tabbed.get(c, 0), cols[c], c))
    if g < 2:
        return []
    rs = ((g + 1) // 8 + 1) * 8         # the tab after the mark reaches the next tab stop
    out: list[tuple[int, int, int, int]] = []
    i, n = 0, len(lines)
    while i < n:
        j, has = i, False
        while j < n:
            ln = lines[j][:-1] if lines[j].endswith("\r") else lines[j]
            c = _sdiff_cells(ln, g, rs) if ln.strip() else (0, 0, " ")
            if c is None:
                break
            if c[2] != " " and marks.get(j) == g:
                has = True
            elif ln.strip() and not (c[0] and ln[c[1]:].strip()):
                break
            j += 1
        if j - i >= 2 and has:
            out.append((i, j, g, rs))
        i = max(j, i + 1)
    return out


def _added(orig: str, plain: str, other: str) -> str:
    """A cell read by the plain reading (`plain`) and by another reading of the cell as it came (`other`): the other
    reading's result where it changed the cell, the plain one's where it did not or where both made the same change,
    DIFF_FAILED where both changed it differently (so neither reading's replacement is undone)."""
    if other == orig or other == plain:
        return plain
    return other if plain == orig else DIFF_FAILED


def _word_sides(ln: str) -> tuple[str, str, bool, bool, bool]:
    """A `--word-diff` line as (old text, new text, on the old side, on the new side, has markers): `[-x-]` is in the
    old text only, `{+x+}` in the new text only (a marker not closed on its line runs to its end). A line made only of
    added parts is no old line, one made only of removed parts no new line. Linear: each marker is found once."""
    old: list[str] = []
    new: list[str] = []
    i, n, marked, adds_only, dels_only = 0, len(ln), False, True, True
    while i < n:
        a, b = ln.find("[-", i), ln.find("{+", i)
        nxt = min(x for x in (a, b) if x >= 0) if a >= 0 or b >= 0 else n
        plain = ln[i:nxt]
        old.append(plain)
        new.append(plain)
        if plain.strip():
            adds_only = dels_only = False
        if nxt == n:
            break
        marked = True
        close = "-]" if nxt == a else "+}"
        e = ln.find(close, nxt + 2)
        e = n if e < 0 else e
        seg = ln[nxt + 2:e]
        if nxt == a:
            old.append(seg)
            adds_only = False
        else:
            new.append(seg)
            dels_only = False
        i = e + 2
    if not marked:
        return ln, ln, True, True, False
    return "".join(old), "".join(new), not adds_only, not dels_only, True


def _word_open(ln: str) -> str:
    """The opener (`[-` or `{+`) of a --word-diff part still open at the end of `ln`, else "". Linear."""
    i, n = 0, len(ln)
    while i < n:
        a, b = ln.find("[-", i), ln.find("{+", i)
        if a < 0 and b < 0:
            return ""
        nxt = min(x for x in (a, b) if x >= 0)
        close = "-]" if nxt == a else "+}"
        e = ln.find(close, nxt + 2)
        if e < 0:
            return ln[nxt:nxt + 2]
        i = e + 2
    return ""


def _word_masked(ln: str) -> str:
    """A `--word-diff` line with the text of every `[-...-]` and `{+...+}` replaced by DIFF_FAILED. Linear."""
    out: list[str] = []
    i, n = 0, len(ln)
    while i < n:
        a, b = ln.find("[-", i), ln.find("{+", i)
        nxt = min(x for x in (a, b) if x >= 0) if a >= 0 or b >= 0 else n
        out.append(ln[i:nxt])
        if nxt == n:
            break
        close = "-]" if nxt == a else "+}"
        e = ln.find(close, nxt + 2)
        # (a carriage return inside the part stays: a second reading takes it for a line end)
        cr = ln.count("\r", nxt + 2, n if e < 0 else e)
        out.append(ln[nxt:nxt + 2] + "\r".join([DIFF_FAILED] * (cr + 1)) + (close if e >= 0 else ""))
        i = n if e < 0 else e + 2
    return "".join(out)


class Redactor:
    """Apply built-in and user patterns. `enabled=False` (config.json {"redact": false}) disables the
    text patterns but never the secret-file omission."""

    def __init__(self, store: Path | None = None, enabled: bool | None = None,
                 extra: list[str] | None = None, project: Path | None = None):
        self.enabled = _config_enabled(store, project) if enabled is None else enabled
        self.user = load_user_patterns(store, project)
        for pat in extra or []:
            try:
                self.user.append(re.compile(pat))
            except re.error:
                pass
        self.secret_patterns = list(SECRET_FILE_PATTERNS)
        self.hits: dict[str, int] = {}
        # strings this redactor produced (redaction is idempotent: a second pass changes nothing), so the redaction
        # at the store's write boundary (record) costs a lookup for text a handler already redacted. Bounded.
        self._clean: dict[str, None] = {}
        self._clean_chars = 0
        # the strings of _clean that were read back from the store (mark_clean): each was written through `record`, so a
        # run made only of them is the store's own text again (a rebuilt session record), not lines first seen alone
        self._stored: set[str] = set()
        self._sides = 0     # diff or column readings around the text being read (_side_texts): MAX_SIDE_LEVEL at most
        self._work: list[int] | None = None    # [charged, plain, reserved] of the call running (Redactor._read)
        # what each granted reading that is running has left of its reservation, innermost last: its passes, and the
        # readings granted inside it, draw from it first (a reading's work is counted once: Redactor._grant)
        self._frames: list[list[int]] = []
        self._split = False     # inside the second reading of a text with returns inside its lines (_cr_text)
        self._memo: dict[str, tuple[str, int, int]] = {}    # a text read in this call -> (result, depth, side level)
        self._batch = READ_BATCH   # characters of side readings gathered before they are read (Redactor._read)
        self.last_work: tuple[int, int] = (0, 0)     # the last call's side readings' and plain readings' characters
        self._outputs: dict[str, str] = {}   # a run of strings joined -> its redaction (Redactor._run, bounded)
        self._outputs_chars = 0

    def _remember(self, s: str, stored: bool = False) -> None:
        if len(s) > CLEAN_CACHE_CHARS // 4:
            return
        while self._clean and self._clean_chars + len(s) > CLEAN_CACHE_CHARS:
            old = next(iter(self._clean))
            del self._clean[old]
            self._stored.discard(old)
            self._clean_chars -= len(old)
        if s not in self._clean:
            self._clean[s] = None
            self._clean_chars += len(s)
        if stored:
            self._stored.add(s)

    def _remember_runs(self, out: list[str]) -> None:
        """The output of a run redacted as one text, as the runs the write boundary will form of it (a second empty
        string in a row ends a run: _run_end), each joined: pieces of a text redacted whole, so record looks them up."""
        i, n = 0, len(out)
        while i < n:
            j = _run_end(out, i, n)
            if j - i > 1:
                self._remember(_join_items(out[i:j])[0])
            i = j

    def mark_clean(self, x) -> None:
        """Remember every string in `x` as already redacted: only for data read back from what this store's write
        boundary wrote (the hook event log, every line of which went through `record`)."""
        if isinstance(x, str):
            self._remember(x, stored=True)
        elif isinstance(x, list):
            for v in x:
                self.mark_clean(v)
        elif isinstance(x, dict):
            for k, v in x.items():
                self.mark_clean(k)
                self.mark_clean(v)

    def record(self, x, _depth: int = 0) -> Any:
        """THE redaction at the store's write boundary: a copy of a JSON-like value with every string in it redacted,
        dictionary keys included, whatever field it is in (hooks._append, sessions.write_record and the hook state
        call it for everything they write, so a field added later cannot skip redaction). Text a handler already
        redacted with this redactor costs a lookup. A dictionary item under a secret-sounding key is judged together with
        its key (`{"password": "..."}`: the value alone looks like any word), and so is a name/value pair (`{"name":
        "DB_PASSWORD", "value": "..."}`); a string that holds JSON is redacted leaf by leaf (_json_strings)."""
        return self._walk(x, _depth, None, False)

    def tree(self, x, budget: int, omit: bool = False) -> tuple[Any, bool]:
        """`record` before serialisation, with a budget: (a copy of `x` with every string leaf redacted as the plain text
        it is, truncated). The leaves are visited in the order json.dumps writes them and each is capped to what is left
        of `budget` characters, keys included; once the budget or TREE_LEAVES strings are used up the rest of `x` is
        left out (the walk stops: an input with 60,000 keys costs what its first few hundred do), so the cost is
        bounded whatever the size of `x`, and the first `budget` characters of the dump are redacted leaves. omit=True
        also drops lines of secret files from each leaf (omit_secret_file_lines), as filter_tool_output does for a
        plain output."""
        state = [max(0, int(budget)), False, TREE_LEAVES]
        return self._walk(x, 0, state, omit), state[1]

    def _leaf(self, s: str, budget: list | None, omit: bool) -> str:
        if budget is None:
            return s if s in self._clean else self.text(s)
        if budget[0] <= 0 or budget[2] <= 0:
            budget[1] = budget[1] or bool(s)
            return ""
        if s:
            budget[2] -= 1
        if omit:
            s = self.omit_secret_file_lines(s[:budget[0] + 2 * CAP_MARGIN])
        out, cut = self.capped(s, budget[0])
        budget[0] -= len(out)
        budget[1] = budget[1] or cut
        return out

    def _run(self, items: list[str], budget: list | None, omit: bool) -> list[str]:
        """Sibling strings (a list's consecutive strings, a dictionary's consecutive string values, the texts of
        consecutive content blocks) redacted as ONE text, one item per line, and cut back item by item (_join_items,
        _split_items): a key block, a YAML block scalar or a TOML multi-line string a tool gives one line per string
        (Edit's `structuredPatch[].lines`, a notebook's `source`) is seen whole. An item that held part of a folded
        construct comes back empty, so the list keeps its length. With a budget, as many items as it allows (each cut
        to what is left of it, the whole capped as one text): fewer items come back when it ran out."""
        if budget is None:
            if self._stored and all(s in self._stored for s in items):
                return list(items)      # the store's own strings read back (mark_clean): a rebuilt record
            joined, layout = _join_items(items)
            if joined in self._clean:
                # the run, joined, is a text this redactor produced (a run tree or record cut back): it was redacted as
                # one text already. Its items known one by one from text() are not enough: a key block whose lines were
                # each redacted alone is no key block to any rule (verifier v9b item 5a)
                return list(items)
            red = self._outputs.get(joined)
            if red is None:
                # (a run met before in this redactor -- the same hunk of a structuredPatch twice -- is looked up)
                red = self.text(joined)
                if len(joined) + len(red) <= CLEAN_CACHE_CHARS // 64:
                    if self._outputs_chars > CLEAN_CACHE_CHARS // 4:
                        self._outputs.clear()
                        self._outputs_chars = 0
                    self._outputs[joined] = red
                    self._outputs_chars += len(joined) + len(red)
            out = list(items) if red == joined else _split_items(layout, joined, red)[0]
            for s in out:
                self._remember(s)
            self._remember_runs(out)
            return out
        room, size, take, leaves = budget[0], 0, [], 0
        for s in items:
            # (items up to CAP_MARGIN past the budget are seen too: a construct the cut splits, such as an XML element
            # whose closing tag is in the next item, is redacted whole before the cut)
            if room + CAP_MARGIN - size <= 0 or leaves >= budget[2]:
                break
            s = s[:room - size + 2 * CAP_MARGIN]
            take.append(s)
            size += len(s) + 1
            leaves += 1 if s else 0
            if size > room + CAP_MARGIN:
                break
        if not take:
            budget[1] = True
            return []
        joined, layout = _join_items(take)
        # (omitted lines of a secret file keep their place: each string keeps its own lines)
        src = omit_secret_file_lines(joined, self.secret_patterns, keep_lines=True) if omit else joined
        red, cut, kept = self._capped(src, room)
        if red == joined and not cut:
            out = take
        else:
            out, used = _split_items(layout, joined, red, cut=cut, kept=kept)
            if cut:
                out = out[:max(1, used)]
        for s in out:
            self._remember(s)
        self._remember_runs(out)
        budget[0] -= len(red)
        budget[2] -= leaves
        budget[1] = budget[1] or cut or len(out) < len(items) or any(len(a) < len(b) for a, b in zip(take, items))
        return out

    def joined(self, items: list[str]) -> list[str]:
        """Sibling strings redacted as one text and cut back item by item (see _run), without a budget: the caller
        bounds their size (stream capture: devmem.capture._bounded)."""
        if not self.enabled:
            return list(items)
        joined, layout = _join_items(items)
        red = self.text(joined)
        return list(items) if red == joined else _split_items(layout, joined, red)[0]

    def key(self, k: str) -> str:
        """A dictionary key redacted with bounded cost (at most MAX_KEY characters of it are kept)."""
        return self._key(k, None) if self.enabled else k

    def _key(self, k: str, budget: list | None) -> str:
        """A dictionary key, redacted; at most MAX_KEY characters of it are redacted and kept (a 5,000-character key is
        text like any other, and an input can have thousands of them), and with a budget it is charged like a value."""
        if len(k) > MAX_KEY:
            key = self.capped(k, MAX_KEY)[0] + f" [devmem: key of {len(k)} characters cut]"
        else:
            key = k if k in self._clean else self.text(k)
        if budget is not None and key:
            budget[0] -= len(key)        # a key is part of the dump and costs a pass of the rules like a value
            budget[2] -= 1
        return key

    def _walk(self, x, depth: int, budget: list | None, omit: bool) -> Any:
        if isinstance(x, str):
            return self._leaf(x, budget, omit)
        if depth > 200:
            return "[OMITTED: nested too deeply]"
        if isinstance(x, (list, tuple)):
            items: list = []
            i, n = 0, len(x)
            while i < n:
                if budget is not None and (budget[0] <= 0 or budget[2] <= 0):
                    budget[1] = True         # used up: the rest is left out, not walked
                    break
                v = x[i]
                j = _run_end(x, i, 2 * budget[2] + 2 if budget is not None else n) if (
                    isinstance(v, str) or _text_block(v)) else i + 1
                if j - i < 2:
                    items.append(self._walk(v, depth + 1, budget, omit))
                    i += 1
                    continue
                run = x[i:j]
                texts = self._run([b if isinstance(b, str) else b["text"] for b in run], budget, omit)
                for b, t in zip(run, texts):
                    if isinstance(b, str):
                        items.append(t)
                    else:      # a text block: its other fields walked as usual, its text from the run
                        rest = self._walk({k: w for k, w in b.items() if k != "text"}, depth + 1, budget, omit)
                        items.append({**rest, "text": t})
                if len(texts) < len(run):
                    break
                i = j
            return items
        if not isinstance(x, dict):
            return x
        named = _pair_name(x)
        out: dict = {}
        pending: list[tuple[Any, Any, str]] = []      # (key as stored, key, value): consecutive string values

        def flush() -> bool:
            if not pending:
                return True
            if len(pending) == 1:
                vals = [self._leaf(pending[0][2], budget, omit)]
            else:
                vals = self._run([v for _k, _o, v in pending], budget, omit)
            for (key, k, v), new in zip(pending, vals):
                if self.enabled and isinstance(k, str) and new and (
                        (k.lower() == "value" and named) or _pair_key(k)):
                    if len(new) <= 4096:
                        judged = self._pair(named if k.lower() == "value" else None, k, new)
                    else:       # a long value: judged by its first 4 KB, redacted whole when that is (fail closed)
                        judged = self._pair(named if k.lower() == "value" else None, k, new[:4096])
                        judged = judged if judged == "[REDACTED:assignment]" else None
                    if judged is not None:
                        new = judged
                out[key] = new
            whole = len(vals) == len(pending)
            pending.clear()
            return whole

        reserved = [0, 0]       # characters and strings of the pending values (charged when they are redacted)
        for k, v in x.items():
            if budget is not None and (budget[0] - reserved[0] <= 0 or budget[2] - reserved[1] <= 0):
                budget[1] = True
                break
            key = self._key(k, budget) if isinstance(k, str) else k
            if isinstance(v, str):
                pending.append((key, k, v))
                reserved[0] += len(v)
                reserved[1] += 1 if v else 0
                continue
            reserved = [0, 0]
            if not flush():
                break
            if self.enabled and isinstance(v, int) and not isinstance(v, bool) and isinstance(k, str) and \
                    len(str(abs(v))) >= 6 and _HARD_SECRET.search(k) and _pair_key(k):
                out[key] = "[REDACTED:assignment]"      # `{"password": 84736251}`: a number is a password too
                continue
            sub = self._walk(v, depth + 1, budget, omit)
            if self.enabled and isinstance(v, dict) and isinstance(sub, dict) and "value" in sub:
                sub = self._valued(k, v, sub)
            out[key] = sub
        flush()
        if self.enabled and "value" in out and "value" in x:
            out = self._valued(None, x, out)       # (the key that holds this object, if any, judges it in the caller)
        return out

    def _valued(self, k, v: dict, sub: dict) -> dict:
        """An object with a `value` item, judged with what stands around it (`sub` is `v` already walked): Terraform's
        `{"sensitive": true, "value": X}` (`terraform output -json`, a state's `outputs`) has X replaced whatever its
        type and name; an Azure Key Vault secret (`"id": "https://<vault>.vault.azure.net/secrets/..."`, as `az keyvault
        secret show` prints it) has its string value replaced; otherwise a string value is judged under the key that
        holds the object (`{"db_password": {"type": "string", "value": X}}`)."""
        value = sub.get("value")
        if v.get("sensitive") is True and value not in (None, ""):
            return {**sub, "value": "[REDACTED:assignment]"}
        if not isinstance(value, str) or not value or value.startswith("[REDACTED"):
            return sub
        vid = v.get("id")
        if isinstance(vid, str) and _KEY_VAULT_ID.match(vid):
            return {**sub, "value": "[REDACTED:assignment]"}
        if isinstance(k, str) and _pair_key(k):
            judged = self._pair(k, "value", value[:4096])
            if judged is not None and (len(value) <= 4096 or judged == "[REDACTED:assignment]"):
                return {**sub, "value": judged if len(value) <= 4096 else "[REDACTED:assignment]"}
        return sub

    def _pair(self, name: str | None, key: str, value: str) -> str | None:
        """The value of `key` (or of the "value" next to `"name": name`) redacted together with its name, as the rules
        see `{"password": "..."}` in a text; None when that changes nothing more than the value alone."""
        frag = json.dumps({"name": name, "value": value} if name else {key: value}, ensure_ascii=False)
        red = self._read(frag)
        if red == frag:
            return None
        try:
            d = json.loads(red)
            new = d.get("value") if name else (list(d.values()) or [None])[-1]
        except (ValueError, AttributeError):
            new = None
        return self._leaf(new, None, False) if isinstance(new, str) else "[REDACTED:assignment]"

    def text(self, s):
        if not isinstance(s, str) or not s or not self.enabled:
            return s
        s = self._read(s)
        self._remember(s)     # (looked up only by `record`: text() itself always redacts)
        return s

    def _read(self, s: str) -> str:
        """One redaction call: _text with a budget of READ_FACTOR * len(s) + READ_BASE characters for the side readings
        (a call made inside a running one draws from that call's budget)."""
        if self._work is not None:
            return self._text(s)
        self._work = [0, 0, 0]
        self._frames = []
        self._batch = max(READ_BATCH, len(s) // 4)
        try:
            return self._cr_text(s, 0)
        finally:
            self.last_work = (self._work[0], self._work[1])      # (side readings', plain readings' characters: tests)
            self._work = None
            self._frames = []
            self._memo = {}

    def _cr_text(self, s: str, depth: int) -> str:
        """_text, where a carriage return inside a line (a file with old Mac line ends shown as one diff line, a line
        overwritten in place) ends a line: the text is read first with each such return taken for a line end (the text
        keeps its own line ends), then that result is read again as it is (a CRLF file's `diff -y` holds the left
        column's `\r` inside each row), so the second reading only adds to the first. A return followed only by blanks
        to the end of its line (`diff -y` pads the right column of a CRLF file after it) is no line end. Used for a
        redacted text (Redactor._read) and for each JSON string read (_json_strings). A text is split at its returns
        once: inside the second reading a JSON string holding such returns is read as it is. The second reading of a
        text the plain reading reads (outside every side reading) is a plain reading too, when the first did not make
        the text longer than twice its length plus READ_BASE: its passes count as the plain readings' (a file with old
        Mac line ends costs about what two plain readings of it cost). Otherwise it is charged like a side reading;
        refused (or come back with other line counts, or the budget refused a reading inside the first one), what it
        would have read, the whole text, is replaced: every line that is not empty becomes BUDGET_FAILED."""
        if "\r" not in s or not _LONE_CR.search(s):
            return self._text(s, depth)
        t = _LONE_CR.sub("\n", s)
        refused = self.hits.get("budget", 0)
        red = self._text(t, depth)
        again = None
        # (a reading inside the first one that the budget refused replaced what it found there, which can leave the
        # text as it came unreadable to the second: a `diff -y` block cut into rows; the whole text fails closed)
        if red.count("\n") == t.count("\n") and self.hits.get("budget", 0) == refused:
            seps = [s[m.start()] for m in _LF.finditer(t)]
            parts = red.split("\n")
            out = parts[0] + "".join(a + b for a, b in zip(seps, parts[1:]))
            n = len(out) + READ_CALL
            split = self._split
            self._split = True
            try:
                if not self._sides and len(out) <= 2 * len(s) + READ_BASE:
                    again = self._text(out, depth)
                elif self._grant(n):
                    self._sides += 1
                    self._frames.append([n])
                    try:
                        again = self._text(out, depth)
                    except _BudgetSpent:
                        self.hits["budget"] = self.hits.get("budget", 0) + 1
                    finally:
                        self._sides -= 1
                        self._end_frame()
            finally:
                self._split = split
            if again is not None and again.count("\n") == out.count("\n"):
                return again
        return "\n".join(BUDGET_FAILED if ln else ln for ln in s.split("\n"))

    def _grant(self, n: int) -> bool:
        """Reserve n characters of the running call's budget for a side reading; False (and nothing reserved) when the
        side readings' charge so far, the reservations and n would pass READ_FACTOR times what the plain readings handed
        the rule pass so far plus READ_BASE. Outside a call (a test calling a reading directly) every one is granted.
        A reservation is used up by the main pass of the reading it was made for (_rules with main=True), so that
        pass is not counted twice: once reserved and once charged."""
        w = self._work
        if w is None or n <= 0:
            return True
        if w[0] + w[2] + n > READ_FACTOR * w[1] + READ_BASE:
            self.hits["budget"] = self.hits.get("budget", 0) + 1
            return False
        w[2] += n
        return True

    def _end_frame(self) -> None:
        """A granted reading ended: what its reservation has left is no longer reserved."""
        f = self._frames.pop()
        if self._work is not None:
            self._work[2] = max(0, self._work[2] - f[0])

    def _text(self, s: str, depth: int = 0) -> str:
        if depth < MAX_JSON_DEPTH and '"' in s:
            s = self._json_strings(s, depth)
        before = s
        if "\x1b" in s:
            # terminal escape sequences: the view passes see every line without them (the last one the whole text); the
            # rules never see the escapes, which would join a value to the text after it (`3600\x1b[m`)
            s = self._line_view(s)
        else:
            s = self._rules(self._line_view(s), _RULES, _ASSIGN_RULES, main=True)
        if self._sides < MAX_SIDE_LEVEL and "\n" in s:
            if _diff_hint(s):
                s = self._diff_sides(s, depth, before)
            if _SDIFF_GUTTER.search(before):
                s = self._columns(before, s, depth)
        for rx in self.user:
            s, n = _windowed_subn(rx, s, "[REDACTED:custom]", keep_lines=True)
            if n:
                self.hits["custom"] = self.hits.get("custom", 0) + n
        return s

    def _diff_sides(self, s: str, depth: int, before: str | None = None) -> str:
        """A diff read as the old and the new text of each hunk (_diff_hunks), each side redacted (one pass over all
        sides of the text) and mapped back line by line: a removed line from its old side, an added line from the new
        side, a context line from the side that changed it (two sides that changed it differently: DIFF_FAILED). A
        line-number decoration before the markers (`grep -n`, `grep -n -C`, `cat -n`, `nl`, the Read tool) stays; a
        coloured line that changes is written back without its escape sequences. Fail closed: a hunk whose sides come
        back with other line counts has its every line replaced by DIFF_FAILED (after its markers); a hunk over
        MAX_DIFF_HUNK_LINES, or after the first MAX_DIFF_HUNKS, has its changed lines replaced. The sides cost at most
        (columns + 1) times the hunk's text. A `--word-diff` hunk (no line markers) has two sides made of its lines
        with the `{+...+}` or the `[-...-]` parts left out; there a changed part within WORD_DIFF_REACH lines after a
        line a redaction changed (compared with `before`, the text before the other rules ran) is replaced too."""
        lines = s.split("\n")
        orig = before.split("\n") if before is not None else None
        if orig is not None and len(orig) != len(lines):
            orig = None
        plain = [_ANSI.sub("", ln) if "\x1b" in ln else ln for ln in lines]
        body = _undecorated(plain)
        # (the hunks are found in the text as it came: a rule that replaced a word-diff part or a marker with the value
        # around it leaves a line that no longer looks like part of its hunk)
        obody = _undecorated([_ANSI.sub("", ln) if "\x1b" in ln else ln for ln in orig]) if orig is not None else body
        hunks = _diff_hunks(obody, headerless=not self._sides)
        if not hunks:
            return s
        known: dict[str, str | None] = {} # a side's text -> its redaction (identical sides are read once)
        batch: dict[str, None] = {}     # the sides of the batch being gathered
        size = 0
        plans: list = []
        spent = False
        # a --word-diff line inside a part opened on a line above (`[-old` ... `-]`) is read with that part's opener
        # before it: wb(i) is the line as its word diff reads
        wbody: dict[int, str] = {}

        def wb(i: int) -> str:
            return wbody.get(i, body[i])

        for h, (a, b, cols) in enumerate(hunks):
            if h >= MAX_DIFF_HUNKS or b - a > MAX_DIFF_HUNK_LINES:
                plans.append(None)
                continue
            sides: list[list[str]] = [[] for _ in range(max(cols, 1) + 1)]  # old side of each parent, then the new
            member: list[list[tuple[int, int]]] = []
            carry = ""
            broken = False
            for i in range(a, b):
                bd = body[i]
                mem: list[tuple[int, int]] = []
                if not cols:        # a --word-diff line
                    if carry:
                        bd = wbody[i] = carry + bd
                    # (the line as it came too: the plain reading may have replaced a part with its opener)
                    carry = _word_open(bd) or (_word_open((carry if carry else "") + orig[i]) if orig is not None
                                               else "")
                    old, new, in_old, in_new, _marked = _word_sides(bd)
                    for k, (on, t) in enumerate(((in_old, old), (in_new, new))):
                        if on:
                            mem.append((k, len(sides[k])))
                            sides[k].append(t)
                elif not bd.startswith("\\"):
                    mk = bd[:cols] if bd else " " * cols
                    omk = obody[i][:cols] if obody[i] else " " * cols
                    if mk != omk:
                        # (a line the plain reading emptied, as it empties the lines of a value over lines, is an empty
                        # line of its old place; one whose markers it replaced otherwise cannot be mapped)
                        broken = broken or bool(bd)
                        mk, bd = omk, omk + bd
                    in_new = "-" not in mk
                    for k in range(cols + 1):
                        if (k == cols and in_new) or (k < cols and (mk[k] == "-" or (in_new and mk[k] != "+"))):
                            mem.append((k, len(sides[k])))
                            sides[k].append(bd[cols:])
                member.append(mem)
            if broken:
                plans.append(())        # (fails closed below: every line of the hunk replaced)
                continue
            texts = ["\n".join(x) for x in sides]
            fresh = [t for t in dict.fromkeys(texts) if t and t not in known and t not in batch]
            # (the budget: each new side's length, READ_CALL for a new reading and for each side read on its own because
            # it holds a NUL; a hunk the budget refuses fails closed below. Sides are read in batches, so the budget a
            # hunk is granted from has the further passes of the batches before it charged)
            cost = sum(len(t) + 1 + (READ_CALL if "\x00" in t else 0) for t in fresh) + (
                READ_CALL if fresh and not batch else 0)
            if spent or not self._grant(cost):
                spent = True        # (the budget refused a hunk: the rest of the diff is not read, and fails closed)
                plans.append(False)
                continue
            for t in fresh:
                batch[t] = None
            size += cost
            plans.append((texts, member, sides))
            if size >= self._batch:
                got = self._read_batch(list(batch), size, depth)
                spent = spent or None in got
                known.update(zip(batch, got))
                batch.clear()
                size = 0
        if batch:
            known.update(zip(batch, self._read_batch(list(batch), size, depth)))
        failed = 0
        hot_end = -2        # the line after the last word-diff hunk, when that hunk's last line was hot
        lost = False        # a hunk the budget refused or whose batch ran out of it: it and every hunk after it fail
        for (a, b, cols), plan in zip(hunks, plans):
            if plan is False or (plan and any(t in known and known[t] is None for t in plan[0])):
                lost = True
            # (every line of a hunk whose reading the budget refused, or whose sides could not be mapped; only the
            # changed lines of one over MAX_DIFF_HUNK_LINES / after MAX_DIFF_HUNKS, as before: those bounds are no
            # budget, and no ordinary diff reaches them)
            whole = lost or plan is not None
            if lost:
                plan = None
            if plan:
                texts, member, sides = plan
                got = [_or(known.get(t, t), t).split("\n") if sides[k] else [] for k, t in enumerate(texts)]
                if all(len(g) == len(x) for g, x in zip(got, sides)):
                    for i, mem in zip(range(a, b), member):
                        vals = {got[k][t] for k, t in mem if got[k][t] != sides[k][t]}
                        if not vals:
                            continue
                        lead = plain[i][:len(plain[i]) - len(body[i])]
                        if not cols and (i in wbody or _word_open(wb(i)) or "\r" in wb(i).rstrip("\r")):
                            # (a line inside a part open over lines, or one holding carriage returns that a second
                            # reading takes for line ends: its changed parts replaced, the part left open as it was,
                            # so a second redaction reads it the same way)
                            lines[i] = lead + _word_masked(wb(i))[len(wb(i)) - len(body[i]):]
                            continue
                        if not cols and _word_sides(wb(i))[4]:
                            # a --word-diff line with markers: written again from its two redacted sides
                            parts = {k: got[k][t] for k, t in mem}
                            lines[i] = lead + ("[-" + parts[0] + "-]" if 0 in parts else "") + (
                                "{+" + parts[1] + "+}" if 1 in parts else "")
                            continue
                        mk = body[i][:cols] if body[i] else " " * cols
                        new = vals.pop() if len(vals) == 1 else DIFF_FAILED
                        lines[i] = lead + mk + new
                    if not cols:
                        # a --word-diff hunk: git moves blanks and line breaks between words into and out of the
                        # markers, so a value's lines need not line up with its name. A changed part on a line that a
                        # redaction changed, or within WORD_DIFF_REACH lines after one, is replaced (fail closed)
                        # (a hunk right after one whose last line was hot goes on from it: the lines between the
                        # two were unchanged, and a block may run on over them)
                        hot = a - 1 if hot_end == a - 1 else -WORD_DIFF_REACH - 1
                        for i in range(a, b):
                            if lines[i] != (orig[i] if orig is not None else plain[i]) or "[REDACTED" in lines[i]:
                                hot = i
                            if i - hot <= WORD_DIFF_REACH and _word_sides(wb(i))[4]:
                                lead = plain[i][:len(plain[i]) - len(body[i])]
                                lines[i] = lead + (_word_masked(wb(i))[len(wb(i)) - len(body[i]):] if i in wbody
                                                   else _word_masked(lines[i][len(lead):]))
                                hot = i         # (a run of changed lines after a redaction is replaced to its end)
                        hot_end = b if hot == b - 1 else -2
                    continue
            # (fails closed: with `whole`, every line of the hunk, context lines too, since the reading would have read
            # them all; an empty line holds nothing)
            failed += 1
            for i in range(a, b):
                bd = body[i]
                mk = obody[i][:cols] if obody[i] else " " * cols
                if not obody[i]:
                    continue
                if not cols:
                    if whole or _word_sides(wb(i))[4]:
                        lines[i] = plain[i][:len(plain[i]) - len(bd)] + DIFF_FAILED
                elif not bd.startswith("\\") and (whole or mk.strip()):
                    lines[i] = plain[i][:len(plain[i]) - len(bd)] + mk + DIFF_FAILED
        if failed:
            self.hits["diff-unmapped"] = self.hits.get("diff-unmapped", 0) + failed
        return "\n".join(lines)

    def _columns(self, orig: str, s: str, depth: int) -> str:
        """`diff -y` output read as its two columns (_sdiff_runs, found in the text as it came, `orig`, before the
        other rules changed the width of a column): the left column's lines as one text, the right column's as another,
        each redacted (_side_texts) and mapped back by line. The columns are read as they came (a cell the plain reading
        changed can have lost what joins it to the next line: `NAME=V\\` became `NAME=[REDACTED:assignment]`). The
        reading only adds to what the plain reading did: the cells are found in the line as the plain reading left it
        (`s`, by the columns, or by its one gutter mark), and a cell is written back only where the column reading
        changed it (_added: where the plain reading changed it too, differently, it becomes DIFF_FAILED), between the
        line's own gutter (blanks, mark and the left column's `\\r` as they were); a line the column reading changed
        whose cells cannot be found in the plain reading's line becomes DIFF_FAILED, its gutter, DIFF_FAILED. Fail
        closed: a column that comes back with another line count, or a block the budget refuses (and every block after
        it), has every line of the block replaced that way."""
        lines = s.split("\n")
        olines = orig.split("\n")
        if len(olines) != len(lines):
            olines = lines
        runs = _sdiff_runs(olines)
        if not runs:
            return s
        pieces: list[str] = []
        done: list[str | None] = []     # the columns read so far, two per block in `blocks`
        size = 0
        blocks: list[list] = []
        spent = False
        for a, b, g, rs in runs:
            rows = []
            for i in range(a, b):
                o = olines[i]
                cr = "\r" if o.endswith("\r") else ""
                o = o[:len(o) - len(cr)]
                oc = _sdiff_cells(o, g, rs) if o.strip() else (0, 0, " ")
                end, start = (oc[0], oc[1]) if oc else (0, 0)
                gap = o[end:start]
                cur = lines[i][:len(lines[i]) - len(cr)] if lines[i].endswith(cr) else lines[i]
                at = None          # (left end, right start) in the plain reading's line
                if cur == o:
                    at = (end, start)
                elif cur.strip():
                    c = _sdiff_cells(cur, g, rs)
                    if c is not None and c[2] == (oc[2] if oc else " "):
                        at = (c[0], c[1])
                    else:
                        ms = list(_SDIFF_GUTTER.finditer(cur))
                        if len(ms) == 1 and oc and oc[2] != " ":
                            k = ms[0].start()
                            while k > 0 and cur[k - 1] in " \t\r":
                                k -= 1
                            at = (k, ms[0].end())
                src = cur if at is not None else o
                le, rs0 = at if at is not None else (end, start)
                # (the right column without the blanks and the `\r` after it: `diff -y` pads a CRLF file's right
                # column with tabs after its `\r`, and a value ends at its line's end)
                re_ = max(rs0, len(src.rstrip(" \t\r")))
                # (the columns are read as they came: a cell the plain reading changed can have lost what joins it to
                # the next line, `NAME=V\` becoming `NAME=[REDACTED:assignment]`)
                rows.append((i, src, le, rs0, re_, gap, cr, at is not None,
                             o[:end], o[start:max(start, len(o.rstrip(" \t\r")))]))
            texts = ["\n".join(r[8] for r in rows), "\n".join(r[9] for r in rows)]
            cost = len(texts[0]) + len(texts[1]) + 2 + READ_CALL
            # (a block the budget refuses, and every block after it, fails closed: every row replaced)
            spent = spent or not self._grant(cost)
            blocks.append([rows, None if spent else texts])
            if spent:
                continue
            pieces += texts
            size += cost
            if size >= self._batch:
                got = self._read_batch(pieces, size, depth)
                spent = None in got
                done += got
                pieces, size = [], 0
        if pieces:
            done += self._read_batch(pieces, size, depth)
        failed = 0
        lost = False
        k = 0
        for rows, texts in blocks:
            lt = rt = None
            if texts is not None:
                lt, rt = done[k], done[k + 1]
                k += 2
            lost = lost or lt is None or rt is None
            left, right = (lt.split("\n"), rt.split("\n")) if not lost and lt is not None and rt is not None else (
                [], [])
            if len(left) != len(rows) or len(right) != len(rows):
                failed += 1
                for i, _src, _le, _rs0, _re, gap, cr, *_rest in rows:
                    if olines[i].strip():        # (an empty line between rows stays empty)
                        lines[i] = DIFF_FAILED + gap + DIFF_FAILED + cr
                continue
            for (i, src, le, rs0, re_, gap, cr, ok, ol, orr), l1, r1 in zip(rows, left, right):
                # a cell as the column reading left it where it changed the cell as it came; the plain reading's
                # where it did not, or where both made the same change; both changed it otherwise: DIFF_FAILED
                nl, nr = _added(ol, src[:le], l1), _added(orr, src[rs0:re_], r1)
                if nl == src[:le] and nr == src[rs0:re_]:
                    continue
                lines[i] = (nl + src[le:rs0] + nr + src[re_:] if ok else DIFF_FAILED + gap + DIFF_FAILED) + cr
        if failed:
            self.hits["diff-unmapped"] = self.hits.get("diff-unmapped", 0) + failed
        return "\n".join(lines)

    def _side_texts(self, pieces: list[str], depth: int) -> list[str]:
        """The sides of a diff (or the columns of `diff -y`) redacted, one side reading deeper (MAX_SIDE_LEVEL): together
        in one text when that keeps each side's line count, else each on its own; a side that holds a NUL (which could
        be taken for the separator) is always read on its own."""
        self._sides += 1
        try:
            return self._joined_reading(pieces, depth, lines=True)
        finally:
            self._sides -= 1

    def _read_batch(self, pieces: list[str], reserved: int, depth: int) -> list[str | None]:
        """A batch of granted side readings (_side_texts), its reservation released when it ends; when a further pass
        inside it would go past the budget (_view_pass raises _BudgetSpent), every piece of the batch comes back None
        and fails closed in the caller."""
        self._frames.append([reserved])
        try:
            return list(self._side_texts(pieces, depth))
        except _BudgetSpent:
            self.hits["budget"] = self.hits.get("budget", 0) + 1
            return [None] * len(pieces)
        finally:
            self._end_frame()

    def _joined_reading(self, pieces: list[str], depth: int, lines: bool = False) -> list[str]:
        """Each piece redacted as a text of its own, those without a NUL together in one text (one pass of every
        rule, separated by _PIECE_SEP: no rule's run crosses a NUL line, and no diff, word diff or `diff -y` block is
        read across one, _diff_hunks) unless a match ran across the separator (or, with lines=True, changed a
        piece's line count): then each on its own."""
        out: list[str | None] = [None] * len(pieces)
        plain = [i for i, p in enumerate(pieces) if p and "\x00" not in p]
        if len(plain) > 1:
            parts = self._text(_PIECE_SEP.join(pieces[i] for i in plain), depth).split(_PIECE_SEP)
            if len(parts) == len(plain) and (not lines or all(
                    a.count("\n") == pieces[i].count("\n") for a, i in zip(parts, plain))):
                for i, a in zip(plain, parts):
                    out[i] = a
        return [o if o is not None else (self._text(p, depth) if p else p) for o, p in zip(out, pieces)]

    def _rules(self, s: str, rules, assign, main: bool = False) -> str:
        w = self._work
        if w is not None:      # (the work of the call: a side reading's is charged, the rest is the plain one)
            if self._sides:
                w[0] += len(s) + READ_PASS
                if main and self._frames:   # (the main pass of a granted reading uses up its reservation)
                    f = self._frames[-1]
                    d = min(f[0], len(s) + READ_PASS)
                    f[0] -= d
                    w[2] -= d
            else:
                w[1] += len(s) + READ_PASS
        low = None
        # markers already in the text (a text redacted before, a quoted marker) are held apart while the rules run, so
        # the token net (_CUT_TOKEN) only extends markers this pass produced (`# was [REDACTED:private-key]-MAC: ...`)
        held = "[REDACTED:" in s and _HELD_MARKER not in s    # (a text holding the variant itself is left as it is)
        if held:
            s = s.replace("[REDACTED:", _HELD_MARKER)
        found: dict[int, bool] = {}     # a word list -> whether the text holds one of its words (many rules share one)
        for kind, rx, repl in rules:
            need = _NEEDS.get(rx)
            if need:
                if low is None:
                    low = s.lower()      # (a replacement never adds one of these words: a stale copy only over-counts)
                ok = found.get(id(need))
                if ok is None:
                    ok = found[id(need)] = any(w in low for w in need)
                if not ok:
                    continue
            # a rule that changed the text runs again (at most 8 more times): `mysql -pa -pb` or `curl -u a:b -u c:d`
            # expose their next value only once the first is replaced, and a second redaction must change nothing.
            # A pass that changed nothing ends it (a callback may keep a match as it is: that is not a hit).
            for _round in range(9):
                new, n = rx.subn(repl, s)
                if new == s:
                    break
                self.hits[kind] = self.hits.get(kind, 0) + n
                s = new
        for _kind, rx, repl in assign:
            need = _NEEDS.get(rx)
            if need:
                if low is None:
                    low = s.lower()
                ok = found.get(id(need))
                if ok is None:
                    ok = found[id(need)] = any(w in low for w in need)
                if not ok:
                    continue
            new = rx.sub(repl, s)
            if new != s:
                self.hits["assignment"] = self.hits.get("assignment", 0) + 1
                s = new
        _forget_context()       # (the memo of _line_context holds the last text it read)
        if "[REDACTED:" in s:
            s = _CUT_TOKEN.sub(r"\g<m>", s)
        if held:
            s = s.replace(_HELD_MARKER, "[REDACTED:")
        return s

    def _json_strings(self, s: str, depth: int) -> str:
        """Every JSON string literal in `s` (8+ characters), redacted as the plain text it decodes to; consecutive string
        values with only JSON punctuation and keys between them (the items of `"lines": ["+-----BEGIN ...", "+MIIE...",
        ...]`, the values of `{"line1": ..., "line2": ...}`, the texts of content blocks) as ONE text, one value per line
        (_join_items: a multi-line secret a tool gives one line per string is seen whole). A key is never joined."""
        groups: list[list[tuple[int, int, str]]] = []      # literals redacted together: one, or a run of values
        run: list[tuple[int, int, str]] = []
        prev = -1           # end of the previous literal (after its closing quote)
        tag = False         # the previous literal was the key "type"

        def close() -> None:
            if len(run) > 1 or (run and len(run[0][2]) >= 8):
                groups.append(list(run))
            run.clear()

        for m in _JSON_STRING.finditer(s):
            lit = (m.start("body"), m.end("body"), m.group("body"))
            linked = prev >= 0 and _JSON_PUNCT.fullmatch(s, prev, m.start()) is not None
            prev = m.end()
            if not linked:
                close()
                tag = False
            if _KEY_AFTER.match(s, m.end()):
                if len(lit[2]) >= 8:
                    groups.append([lit])     # a key: redacted alone, as before; the run of values goes on past it
                tag = lit[2] == "type"
                continue
            if tag:     # the `"type": "text"` of a content block: a tag, not text; the run goes on past it too
                tag = False
                if len(lit[2]) >= 8:
                    groups.append([lit])
                continue
            run.append(lit)
        close()
        if not groups:
            return s
        decoded = [[_decode_literal(body) for _a, _b, body in g] for g in groups]
        texts, layouts = [], []
        for d in decoded:
            joined, layout = _join_items([t for t, _strict in d]) if len(d) > 1 else (d[0][0], None)
            texts.append(joined)
            layouts.append(layout)
        # a text this call read already (at this depth and side level or a shallower one: a reading at least as deep) is
        # not read again; the others are read together if the budget grants them, in order
        # (inside a side reading each new text is charged to the budget, read in batches: see READ_FACTOR)
        read: dict[str, str] = {}
        todo: dict[str, None] = {}
        refused: set[str] = set()
        size = 0
        charged = bool(self._sides)
        for t in texts:
            hit = self._memo.get(t)
            if hit is not None and hit[1] <= depth + 1 and hit[2] <= self._sides:
                read[t] = hit[0]
            elif t not in todo and t not in refused and t not in read:
                cost = len(t) + 1 + (READ_CALL if "\x00" in t else 0) + (READ_CALL if not todo else 0)
                if charged and not self._grant(cost):
                    refused.add(t)
                    continue
                todo[t] = None
                size += cost
                if charged and size >= self._batch:
                    self._frames.append([size])
                    try:
                        read.update(zip(todo, self._pieces(list(todo), depth + 1)))
                    finally:
                        self._end_frame()
                    todo.clear()
                    size = 0
        if todo:
            if charged:
                self._frames.append([size])
            try:
                read.update(zip(todo, self._pieces(list(todo), depth + 1)))
            finally:
                if charged:
                    self._end_frame()
        for t, r in read.items():
            if t not in self._memo or self._memo[t][1] > depth + 1 or self._memo[t][2] > self._sides:
                self._memo[t] = (r, depth + 1, self._sides)
        done = [read.get(t) for t in texts]
        changed = []
        for g, d, text, layout, new in zip(groups, decoded, texts, layouts, done):
            if new is None:
                # refused by the budget: the strings fail closed, unless it is one string of one line that decodes to
                # its own text (the plain reading of the text around it read exactly that)
                if "\n" in text or len(g) > 1 or text != g[0][2]:
                    changed += [(a, b, BUDGET_FAILED) for a, b, _body in g]
                continue
            if new == text:
                continue
            news = [new] if layout is None else _split_items(layout, text, new)[0]
            changed += [(a, b, _encode_literal(nt, strict)) for (a, b, _body), (old, strict), nt in zip(g, d, news)
                        if nt != old]
        if not changed:
            return s
        changed.sort()          # a run's values have keys between them, and a key is a group of its own
        out, last = [], 0
        for a, b, enc in changed:
            out += [s[last:a], enc]
            last = b
        out.append(s[last:])
        return "".join(out)

    def _pieces(self, pieces: list[str], depth: int) -> list[str]:
        """Each piece redacted as a text of its own (see _joined_reading); a piece with carriage returns inside its
        lines on its own (_cr_text)."""
        crs = {i for i, p in enumerate(pieces) if "\r" in p and _LONE_CR.search(p)} if not self._split else ()
        if not crs:
            return self._joined_reading(pieces, depth)
        rest = iter(self._joined_reading([p for i, p in enumerate(pieces) if i not in crs], depth))
        return [self._cr_text(p, depth) if i in crs else next(rest) for i, p in enumerate(pieces)]

    def _line_view(self, s: str) -> str:
        """Lines with a tool prefix or a trailing comment, redacted once more without them (every other line is
        empty in that view, so its cost follows the decorated lines). A line with terminal colour or other escape
        sequences (`git diff --color`, `grep --color=always`, `jq -C`) is seen without them; such a line keeps its
        escapes unless a rule changes it (then it is written back without them); a coloured line is seen once more as
        a whole (the full-text pass cannot see it: the escapes break its matches)."""
        coloured = "\x1b" in s
        s = self._view_pass(s, True)
        s = self._view_pass(s, True, _LINE_PREFIX_ONE)
        if _DASH_BLOCK.search(s):
            # `-  key: |` reads as a list item or as a removed diff line: both readings are redacted (the second view
            # takes the dash for a marker; a block's siblings can be emptied with it, which fails closed)
            s = self._view_pass(s, True, _LINE_PREFIX_DASH)
        return self._view_pass(s, False) if coloured else s

    def _view_pass(self, s: str, decorated: bool, prefix: re.Pattern | None = None) -> str:
        """One pass of _line_view: decorated=True, every line with a prefix or a trailing comment (or colours) without
        them; decorated=False, every line whole, coloured ones without their escape sequences."""
        lines = s.split("\n")
        bases = lines        # the lines the spans refer to (a coloured line without its escape sequences)
        view: list[str] = []
        spans: list[tuple[int, int] | None] = []
        seen = False
        numbered = _numbered_runs(lines) if decorated and prefix is None else {}
        for i, ln in enumerate(lines):
            coloured = "\x1b" in ln
            plain = _ANSI.sub("", ln) if coloured else ln
            if coloured:
                if bases is lines:
                    bases = list(lines)
                bases[i] = plain
            if not decorated:       # the whole text without its escape sequences (a key block can span coloured lines)
                a, b = 0, len(plain)
                coloured = True
            else:
                if i in numbered:
                    a = numbered[i]     # a line of numbered output: only the number goes, the rest is content
                else:
                    m = (prefix or _LINE_PREFIX).match(plain)
                    a = m.end() if m else 0
                c = _TRAILING_COMMENT.search(plain, a)
                if c and not plain[a:c.start()].strip():
                    c = None        # (a marker that starts the content is content, not a trailing comment)
                b = c.start() if c else len(plain)
                while c and b > a and plain[b - 1] in " \t":    # the blanks before the comment (each line's run once)
                    b -= 1
            if (a or b < len(plain) or coloured) and a < b:
                view.append(plain[a:b])
                spans.append((a, b))
                seen = True
            else:
                view.append("")
                spans.append(None)
        if not seen:
            return s
        v = "\n".join(view)
        w = self._work
        if self._sides and w is not None and w[0] + w[2] + len(v) > READ_FACTOR * w[1] + READ_BASE:
            raise _BudgetSpent      # (a side reading's further pass past the budget: its batch fails closed)
        red = self._rules(v, _RULES, _ASSIGN_RULES)
        if red == v:
            return s
        rl = red.split("\n")
        if len(rl) != len(view):
            return _splice(bases, spans, view, rl)
        for i, (old, new) in enumerate(zip(view, rl)):
            sp = spans[i]
            if old != new and sp:
                lines[i] = bases[i][:sp[0]] + new + bases[i][sp[1]:]
        return "\n".join(lines)

    def capped(self, s, limit: int, margin: int = CAP_MARGIN, tail: bool = False) -> tuple[str, bool]:
        """Redact `s` and keep at most `limit` characters of it: the first ones, or with tail=True the last ones.
        Returns (text, truncated). The cost is bounded by limit + margin whatever the input size.

        Only limit + margin characters are redacted. A secret that starts inside the kept part and is shorter
        than `margin` is therefore seen whole and replaced, and no character of the outermost `margin` region
        (where the pre-cut may have split a secret) is kept unless it lies inside a replacement."""
        red, cut, _lines_kept = self._capped(s, limit, margin, tail)
        return red, cut

    def _capped(self, s, limit: int, margin: int = CAP_MARGIN, tail: bool = False) -> tuple[str, bool, bool]:
        """capped, and whether the redaction kept the line count of what it redacted (then line i of the result is line
        i of `s`, for a cut at the end)."""
        if not isinstance(s, str):
            s = "" if s is None else str(s)
        if len(s) <= limit:
            red = self.text(s)
            return red, False, red.count("\n") == s.count("\n")
        if len(s) <= limit + margin:
            pre, cut_edge = s, False
        else:
            pre, cut_edge = (s[-(limit + margin):] if tail else s[:limit + margin]), True
        red = self.text(pre)
        kept = red.count("\n") == pre.count("\n")
        if cut_edge and self.enabled:
            # drop the unchanged characters at the cut edge (at most `margin`): they may hold half a secret
            k, n = 0, min(len(pre), len(red), margin)
            if tail:
                while k < n and pre[k] == red[k]:
                    k += 1
                red = red[k:]
            else:
                while k < n and pre[-1 - k] == red[-1 - k]:
                    k += 1
                red = red[:len(red) - k]
        out = red[-limit:] if tail else red[:limit]
        if cut_edge and self.enabled and not tail:
            out = _close_cut(out)
        return out, True, kept

    def obj(self, x) -> Any:
        """Redact every string in a JSON-like structure, dictionary keys included (the same as `record`)."""
        return self.record(x)

    def is_secret_file(self, path: str | None) -> bool:
        return is_secret_file(path, self.secret_patterns)

    def mentions_secret_file(self, command: str | None) -> bool:
        return mentions_secret_file(command, self.secret_patterns)

    def input_names_secret_file(self, inp, project: Path | None = None, why: list | None = None) -> bool:
        return input_names_secret_file(inp, project, self.secret_patterns, why)

    def omit_secret_file_lines(self, text: str) -> str:
        return omit_secret_file_lines(text, self.secret_patterns)

    def tool_reads_secret_file(self, name: str, inp, project: Path | None = None) -> bool:
        return tool_reads_secret_file(name, inp, project, self.secret_patterns)

    def filter_tool_output(self, name: str, inp, text: str, project: Path | None = None) -> tuple[str, bool]:
        return filter_tool_output(name, inp, text, project, self.secret_patterns)


def _config_enabled(store: Path | None, project: Path | None = None) -> bool:
    """Redaction is on unless the store's config.json says `"redact": false` AND the user allowed that outside the
    project (`devmem init --allow-unredacted` or `devmem setup --allow-unredacted`, devmem.consent). A config file
    can arrive with a `git pull` or a zip; it can never weaken redaction on its own. `project` defaults to the folder
    that holds the store."""
    if not store:
        return True
    try:
        from .store import read_store_text
        cfg = json.loads(read_store_text(Path(store) / "config.json", 1024 * 1024))
        from .sessions import config_bool
        if config_bool(cfg.get("redact"), True):
            return True
    except (OSError, ValueError, AttributeError):
        return True
    try:
        from .consent import allows_unredacted
        return not allows_unredacted(Path(project) if project else Path(store).parent)
    except Exception:  # noqa: BLE001 - when in doubt, redact
        return True


def config_asks_unredacted(store: Path | None) -> bool:
    """Does the store's config.json ask for `"redact": false` (whether or not it is allowed)?"""
    try:
        from .store import read_store_text
        cfg = json.loads(read_store_text(Path(store) / "config.json", 1024 * 1024))  # type: ignore[arg-type]
        from .sessions import config_bool
        return isinstance(cfg, dict) and not config_bool(cfg.get("redact"), True)
    except (OSError, ValueError, AttributeError, TypeError):
        return False


# names that the secret patterns stand for: a glob in a shell command (`cat .en?`) or an 8.3 short name (`ENV~1`) is
# tested against these, since it names no file by itself
_SECRET_EXAMPLES = (".env", ".env.local", ".env.production", "server.pem", "server.key", "id_rsa", "id_rsa.pub",
                    "id_dsa", "id_ecdsa", "id_ed25519", "my_secret.txt", "secrets.json", "credentials.json",
                    "my_credentials.json", "cert.p12", "cert.pfx", "app.keystore", "app.jks", ".netrc", "_netrc",
                    ".pgpass", ".pypirc", ".npmrc", "vault.kdbx", ".htpasswd", ".envrc", "deploy.ppk",
                    "terraform.tfstate", ".dev.vars", "local.settings.json", ".vault-token", ".my.cnf", ".s3cfg",
                    "kubeconfig", "AuthKey.p8", "prod.env", ".env-local", ".env_local", ".env~", "terraform.tfvars",
                    "prod.auto.tfvars", ".dockercfg", "wp-config.php")
# files that are secret by where they live, not by their name: a kubeconfig, docker's and gh's credential stores
_SECRET_PATH_SUFFIXES = (".kube/config", ".docker/config.json", ".config/gh/hosts.yml")
_GLOB = re.compile(r"[*?\[]")
_SHORT_NAME = re.compile(r"^([^~\\/]{1,8})~\d{1,6}(\.[^\\/.]{0,3})?$")


@functools.lru_cache(maxsize=32)
def _pattern_rx(pats: tuple[str, ...]) -> re.Pattern:
    """The secret-file patterns as one regex (fnmatch.translate of each, case already folded): one match per name
    instead of one fnmatch call per pattern."""
    return re.compile("|".join(f"(?:{fnmatch.translate(p.lower())})" for p in pats) or r"(?!)")


def _secret_file_part(part: str, pats: tuple[str, ...]) -> bool:
    """One name (a path component), lower case, matches a secret pattern; also its spelling without the trailing
    dots and spaces Windows ignores, a glob that could match a secret name, and an 8.3 short name whose stem is the
    start of one."""
    rx = _pattern_rx(pats)
    candidates = {part, part.rstrip(". "), part.split("::$", 1)[0], part.split(":", 1)[0] if ":" in part else part}
    if any(c and rx.match(c) for c in candidates):
        return True
    if _GLOB.search(part) and re.search(r"[a-z0-9_]", part):
        # a glob with at least one literal letter (`.en?`, `*.pe?`, `id_rs*`); like a shell, a leading wildcard does not
        # match a dot file. A bare `*` or `*.*` is not counted (it would hide every `grep x *`): best effort.
        rx = re.compile(fnmatch.translate(part.rstrip(". ")))
        if any(rx.match(ex) and (part.startswith(".") or not ex.startswith(".")) for ex in _SECRET_EXAMPLES):
            return True
    m = _SHORT_NAME.match(part)
    if m:
        stem = m.group(1).lower()
        if any(ex.lstrip("._").replace(".", "").replace("_", "").startswith(stem.replace("_", ""))
               for ex in _SECRET_EXAMPLES):
            return True
    return False


def is_secret_file(path: str | None, patterns: list[str] | None = None) -> bool:
    """True if the path, or any of its components, matches a secret-file pattern (.env, *.pem, id_rsa*,
    *secret*, ...), case-insensitively: the same rule devmem.store applies to its hard ignores. Other spellings of
    such a name count too: trailing dots or spaces, a `::$DATA` stream, a glob that could match one (`.en?`) and an
    8.3 short name whose stem begins one (`ENV~1`, `MY_CRE~1.JSO`)."""
    if not path:
        return False
    return _is_secret_file(str(path), tuple(patterns or SECRET_FILE_PATTERNS))


@functools.lru_cache(maxsize=8192)
def _is_secret_file(path: str, pats: tuple[str, ...]) -> bool:
    # cached: a tool output names the same few files on every line (`grep -rn` prints the path on each one)
    norm = path.replace(chr(92), "/").lower()
    parts = [p for p in norm.split("/") if p]
    if any(norm.endswith(sfx) and (len(norm) == len(sfx) or norm[-len(sfx) - 1] == "/") for sfx in _SECRET_PATH_SUFFIXES):
        return True
    return bool(_pattern_rx(pats).match(norm)) or any(_secret_file_part(x, pats) for x in parts)


_SHELL_WORD = re.compile(r"""(?:[^\s"'`;|&<>()\\]|\\.|\$'(?:[^'\\]|\\.)*'|'[^']*'|"(?:[^"\\]|\\.)*")+""", re.S)
_ANSI_C = re.compile(r"\\(x[0-9A-Fa-f]{1,2}|[0-7]{1,3}|.)", re.S)


def _ansi_c(body: str) -> str:
    """The text of a bash `$'...'` string (`\\x2e` -> `.`)."""
    def one(m: re.Match) -> str:
        e = m.group(1)
        if e[0] == "x":
            return chr(int(e[1:], 16))
        if e[0] in "01234567":
            return chr(int(e, 8) & 0xFF)
        return {"n": "\n", "t": "\t", "r": "\r", "0": "\0"}.get(e) or e
    return _ANSI_C.sub(one, body)


def _shell_words(command: str) -> list[str]:
    """The words of a command line as a POSIX shell would pass them on: quotes removed (`.e''nv`, `".env"`), backslash
    escapes resolved (`.\\env` -> `.env`), `$'\\x2eenv'` decoded. Best effort; nothing is expanded or run."""
    out = []
    for m in _SHELL_WORD.finditer(command):
        w, word, i = m.group(0), [], 0
        while i < len(w):
            c = w[i]
            if c == "\\" and i + 1 < len(w):
                word.append(w[i + 1])
                i += 2
            elif w.startswith("$'", i):
                j = i + 2
                while j < len(w) and w[j] != "'":
                    j += 2 if w[j] == "\\" else 1
                word.append(_ansi_c(w[i + 2:j]))
                i = j + 1
            elif c == "'":
                j = w.find("'", i + 1)
                j = len(w) if j < 0 else j
                word.append(w[i + 1:j])
                i = j + 1
            elif c == '"':
                j = i + 1
                while j < len(w) and w[j] != '"':
                    if w[j] == "\\" and j + 1 < len(w) and w[j + 1] in '"\\$`':
                        word.append(w[j + 1])
                        j += 2
                        continue
                    word.append(w[j])
                    j += 1
                i = j + 1
            else:
                word.append(c)
                i += 1
        out.append("".join(word))
    return out


# characters of a command that are checked: its first COMMAND_CHECK_HEAD and last COMMAND_CHECK_TAIL (a longer command
# is checked at both ends only: the cost per call stays bounded)
COMMAND_CHECK_HEAD = 20000
COMMAND_CHECK_TAIL = 4096


_BRACE = re.compile(r"\{([^{}\s]{0,64},[^{}\s]{0,64})\}")
MAX_BRACE_WORDS = 16


def _brace_words(tok: str) -> list[str]:
    """A shell word's brace expansions (`.e{n,}v` -> `.env`, `.ev`), at most MAX_BRACE_WORDS of them."""
    out = [tok]
    for _ in range(3):
        nxt = []
        for w in out:
            m = _BRACE.search(w)
            if not m:
                nxt.append(w)
                continue
            nxt += [w[:m.start()] + alt + w[m.end():] for alt in m.group(1).split(",")]
        out = nxt[:MAX_BRACE_WORDS]
    return out


def mentions_secret_file(command: str | None, patterns: list[str] | None = None) -> bool:
    """Does a shell command reference a secret file (e.g. `cat .env`, `cat .e''nv`, `cat .\\env`)? Then its output is
    not stored. The command's words are taken both as written (split at quotes and operators), as a shell would
    unquote them, with simple brace expansions (`.e{n,}v`) and without PowerShell's backtick or cmd's caret escapes
    (`.e`nv`, `.e^nv`)."""
    if not command:
        return False
    text = command if len(command) <= COMMAND_CHECK_HEAD + COMMAND_CHECK_TAIL else \
        command[:COMMAND_CHECK_HEAD] + "\n" + command[-COMMAND_CHECK_TAIL:]
    words = re.split(r"[\s\"'`;|&<>()=,]+", text)
    words += [x for w in _shell_words(text) for x in re.split(r"[\s;|&<>()=,]+", w)]
    # brace expansion (`cat .e{n,}v`), PowerShell's backtick and cmd's caret before a letter (`.e`nv`, `.e^nv`)
    words += [b for w in text.split() if "{" in w and "," in w for b in _brace_words(w.strip("\"'"))]
    if "`" in text or "^" in text:
        words += re.split(r"[\s\"';|&<>()=,]+", text.replace("`", "").replace("^", ""))
    for tok in words:
        if not tok or tok.startswith("-"):
            continue
        if len(tok) > MAX_PATH_CHARS:     # not a path: its last name only (each component costs time)
            tok = _last_name(tok)
        # `git show HEAD:.env`, `git show v1.2:config/.env`, `scp host:.env .`: the path after a colon
        pieces = [tok] + (tok.split(":") if ":" in tok else [])
        # `cat *.pem`, `cat .env*`: a glob that can only name secret files
        pieces += [re.sub(r"[*?\[\]{}]", "", p) for p in pieces if any(c in p for c in "*?[{")]
        if any(p and is_secret_file(p, patterns) for p in pieces):
            return True
    return False


# tool-input fields that name a file or folder the tool reads or writes (Claude Code's own tools and common MCP
# servers such as the filesystem server: path, paths, source, destination, ...)
_PATH_KEYS = ("file_path", "notebook_path", "path", "paths", "file", "files", "filename", "filepath",
              "source", "destination", "target", "dir", "directory", "uri")


# A tool-input value longer than this is not a path (PATH_MAX on Linux; Claude Code's tools never send one): it is never
# resolved or relativised, only its last name is checked. Path.resolve() of a long path that does not exist is
# superlinear on Windows (a 20 KB Grep `path` cost 9.8 s), and checking every component costs time per component.
MAX_PATH_CHARS = 4096
# per tool input: path values checked at all (more and the call counts as reading a secret file: its output is not
# kept), and characters of them checked component by component (beyond that, only each value's last name)
MAX_PATH_VALUES = 1000
PATH_CHECK_BUDGET = 16_384


@functools.lru_cache(maxsize=16)
def _project_bases(project: str) -> tuple[Path, ...]:
    bases = [Path(os.path.abspath(project))]
    try:
        r = Path(project).resolve()
        if r != bases[0]:
            bases.insert(0, r)
    except (OSError, RuntimeError):
        pass
    return tuple(bases)


def project_relative(value: str, project: Path | None) -> str | None:
    """`value` relative to the project as a POSIX path when it names something inside it, else None. Bounded cost:
    a value longer than MAX_PATH_CHARS (or holding NUL) is not a path, a network path is never touched, and
    Path.resolve() runs only on a path, or the parent of a path, that exists (a link in it is then followed, so a link
    out of the project counts as outside); anything else is normalised lexically."""
    return _locate(value, project)[0]


def _locate(value: str, project: Path | None) -> tuple[str | None, Path | None]:
    """(project_relative(value, project), the real path it names, links followed where it exists: None when it was
    not looked at). One bounded resolve() serves both."""
    from .store import is_remote_path
    if project is None or not value or len(value) > MAX_PATH_CHARS or chr(0) in value or is_remote_path(value):
        return None, None
    target = None
    try:
        a = os.path.abspath(value)
        bases = _project_bases(str(project))
        if os.path.lexists(a):
            target, bases = Path(a).resolve(), bases[:1]
        else:
            parent = os.path.dirname(a)
            if parent != a and os.path.lexists(parent):
                target, bases = Path(parent).resolve() / os.path.basename(a), bases[:1]
            else:
                target = Path(a)
        for base in bases:
            try:
                return target.relative_to(base).as_posix(), target
            except ValueError:
                continue
    except (ValueError, OSError, RuntimeError):
        pass
    return None, target


def _project_rel(value: str, project: Path | None) -> str:
    """What the secret-file rule checks for a path value: its project-relative form when it names something inside
    the project (so the project's own folder name never counts), else the real path it names, so a secret file reached
    through a link inside the project (`keys -> ~/.ssh`) is seen by its real name; `value` itself when it was not
    looked at (too long, a network path, no project)."""
    rel, target = _locate(value, project)
    return rel if rel is not None else value if target is None else target.as_posix()


def _last_name(value: str) -> str:
    v = value.rstrip("/\\")
    return v[max(v.rfind("/"), v.rfind("\\")) + 1:][-255:]


# A key whose name says its value is a path (any tool's naming: `file_path`, `relative_path` (Serena), `pathInProject`
# (JetBrains), `filePath`, `absolute_path`, `uri`, `glob`, `include`, ...): its whole value is checked as a path. A key
# holding a command or code (`command`, `cmd`, `script`, `code`) is checked as a shell command. Any other string is
# checked only when it looks like one path (one word with a `/`, `\\` or `.`): a path merely mentioned in prose does
# not hide the result.
_PATH_KEY = re.compile(r"(?i)path|file|uri|url|dir|folder|glob|include|^(?:source|src|destination|dest|dst|target|root|"
                       r"location)$")
# keys whose value is text or a search, never a file the tool reads
_NOT_PATH_KEY = re.compile(r"(?i)^(?:pattern|regex|query|q|search|text|content|contents|old_string|new_string|new_source|"
                           r"prompt|description|message|title|body|sql|exclude|excludes|ignore|ignores|comment|summary|"
                           r"reason|activeform)$")
_COMMAND_KEY = re.compile(r"(?i)^(?:command|cmd|commandline|command_line|script|shell|shell_command|code|bash|powershell)$")
# keys whose value is a command line or its words for terminal and process tools (`interact_with_process {"input": "cat
# .env"}`, `{"argv": ["sh", "-c", "cat .env"]}`, `{"args": "cat .env"}`): checked as a command, for a specific secret name
# only (`args: ["secrets.get"]` hides nothing)
_COMMAND_LIKE_KEY = re.compile(r"(?i)^(?:input|args|argv|arguments|line|stdin|cmdline|cmd_line)$")
_PATH_LIKE = re.compile(r"[^\s\"'`<>|;&()]{1,4096}")
MAX_INPUT_DEPTH = 32
# keys, values and items one input_names_secret_file call looks at: an input with more counts as naming a secret file
# (its output is not kept), so the check costs what 20,000 short strings cost whatever the input holds; a key is judged by
# its first MAX_KEY_CHECK characters
MAX_INPUT_NODES = 20_000
MAX_KEY_CHECK = 128


def _path_value(x: str) -> str:
    """A path value as written, with a `file://` URL decoded and anything from a NUL on dropped."""
    x = x.strip().split(chr(0), 1)[0]
    if x[:7].lower() == "file://":
        from urllib.parse import unquote
        x = unquote(x[7:])
        if x[:10].lower() == "localhost/":
            x = x[9:]
        if re.match(r"^/[A-Za-z]:[/\\]", x):
            x = x[1:]             # file:///C:/proj/.env
    return x


def input_names_secret_file(inp, project: Path | None = None, patterns: list[str] | None = None,
                            why: list | None = None) -> bool:
    """Does a tool's input name a secret file anywhere? Every string of the input is looked at, at any depth and under
    any key (an MCP `read_text_file {"path": ".env"}`, Serena `{"relative_path": ".env"}`, `{"args": {"path": ".env"}}`,
    a `file://` URL, Grep with `glob: ".env"`): see _PATH_KEY and _COMMAND_KEY. Paths inside the project are checked
    relative to it, so the project's own folder name never counts. Bounded: a value longer than MAX_PATH_CHARS, or past
    PATH_CHECK_BUDGET characters of path values in this input, is checked by its last name only; more than
    MAX_PATH_VALUES values, more than MAX_INPUT_NODES keys, values and items, or nesting deeper than MAX_INPUT_DEPTH,
    count as naming a secret file (the output is then not kept); then "too large" is appended to `why`, so the record
    can say which it was."""
    if not isinstance(inp, dict):
        return False
    state = {"budget": PATH_CHECK_BUDGET, "seen": 0, "nodes": 0, "deep": False}
    found = _input_walk(inp, project, patterns, state)
    if found and why is not None and (state["deep"] or state["nodes"] > MAX_INPUT_NODES or
                                      state["seen"] > MAX_PATH_VALUES):
        why.append("too large")
    return found


def _input_walk(inp: dict, project: Path | None, patterns: list[str] | None, state: dict) -> bool:

    def check_path(x: str) -> bool:
        state["seen"] += 1
        if state["seen"] > MAX_PATH_VALUES:
            return True
        x = _path_value(x)
        if not x:
            return False
        if len(x) > MAX_PATH_CHARS or len(x) > state["budget"]:
            return is_secret_file(_last_name(x), patterns)
        state["budget"] -= len(x)
        checked = _project_rel(x, project)
        if is_secret_file(checked, patterns):
            return True
        # outside the project: the name as written counts too (`.env` linked elsewhere)
        return checked != x and os.path.isabs(checked) and is_secret_file(x, patterns)

    def walk(v, key: str, depth: int) -> bool:
        state["nodes"] += 1
        if depth > MAX_INPUT_DEPTH or state["nodes"] > MAX_INPUT_NODES:
            state["deep"] = state["deep"] or depth > MAX_INPUT_DEPTH
            return True
        if isinstance(v, dict):
            return any(walk(x, k[:MAX_KEY_CHECK] if isinstance(k, str) else str(k)[:MAX_KEY_CHECK], depth + 1)
                       for k, x in v.items())
        if isinstance(v, list):
            return any(walk(x, key, depth + 1) for x in v)
        if not isinstance(v, str) or not v.strip():
            return False
        if _COMMAND_KEY.search(key):
            state["seen"] += 1
            return state["seen"] > MAX_PATH_VALUES or mentions_secret_file(v, patterns)
        if _COMMAND_LIKE_KEY.search(key):
            state["seen"] += 1
            if state["seen"] > MAX_PATH_VALUES or mentions_secret_file(v, _specific(patterns)):
                return True
        low = v[:7].lower()
        if low.startswith(("http://", "https:/", "ftp://", "sftp://")) or _NOT_PATH_KEY.search(key):
            return False          # a remote address names no local file; a search or a text is no path
        if _PATH_KEY.search(key):
            return check_path(v)
        w = v.strip()
        if not _PATH_LIKE.fullmatch(w):
            return False
        # a path-like word under any other key (`args: [".env"]`, `{"repo": "org/credentials-helper"}`): only a pattern
        # for a specific name counts (`.env`, `*.ppk`, `id_rsa*`), not one that matches any name holding a word
        # (`*secret*`: `secrets.get` or `fix/secret-scan` names no secret file)
        if not any(c in w for c in "./\\"):
            return False
        state["seen"] += 1
        return state["seen"] > MAX_PATH_VALUES or is_secret_file(_path_value(w)[-MAX_PATH_CHARS:], _specific(patterns))
    return walk(inp, "", 0)


def _specific(patterns: list[str] | None) -> list[str]:
    return [p for p in (patterns or SECRET_FILE_PATTERNS) if not (p.startswith("*") and p.endswith("*"))]


_OMITTED_LINE = "[OMITTED: line from a secret file]"


_OMITTED_DIFF = "[OMITTED: diff of a secret file]"
_OMITTED_FILE = "[OMITTED: content of a secret file]"
_HEAD_HEADER = re.compile(r"^==> (.{1,4096}) <==$")      # `head`/`tail` of several files
_HUNK_START = re.compile(r"^(?:@@|\d+(?:,\d+)?[acd]\d+(?:,\d+)?$|\*\*\*\*\*\*)")  # unified, normal, context diff
_DIFF_HEADER_KEEP = ("index ", "new file mode", "deleted file mode", "old mode", "new mode", "similarity index",
                     "dissimilarity index", "rename from", "rename to", "copy from", "copy to", "--- ", "+++ ",
                     "Binary files")


def _secret_line_head(line: str, patterns) -> int:
    """Length of a leading secret-file path in a `path:...` / `path-N-...` / `path-...` line (grep, ripgrep
    and the Grep tool, with or without -n and context), else 0. The path has no whitespace and contains one of
    `./\\`; at most 16 separators are tried per line, so the cost per line is bounded.

    The same answer as is_secret_file(head) for each candidate head, computed incrementally: the names before the
    head's last slash are judged once each as the line is walked, so a candidate costs one check of its last name and
    one regex match (a line of 512 characters is walked once, not once per candidate)."""
    pats = tuple(patterns or SECRET_FILE_PATTERNS)
    rx = _pattern_rx(pats)
    lim = min(len(line), 512)
    tried, i = 0, 2 if len(line) > 2 and line[1] == ":" and line[0].isalpha() else 0  # C:\... drive letter
    start, earlier_secret, dotted = 0, False, False
    while i < lim and tried < 16:
        c = line[i]
        if c.isspace():
            return 0
        if c in "/\\":
            part = line[start:i].lower()
            if part and _secret_part_cached(part, pats):
                earlier_secret = True
            start, dotted = i + 1, True
        elif c == ".":
            dotted = True
        elif c in ":-" and i:
            if dotted:
                tried += 1
                last = line[start:i].lower()
                if earlier_secret or (last and _secret_part_cached(last, pats)) or \
                        rx.match(line[:i].replace(chr(92), "/").lower()):
                    return i
        i += 1
    return 0


@functools.lru_cache(maxsize=8192)
def _secret_part_cached(part: str, pats: tuple[str, ...]) -> bool:
    return _secret_file_part(part, pats)


def _diff_header_paths(line: str) -> list[str]:
    """Paths named by a `diff ...` / `--- ...` / `+++ ...` header line (a/ b/ prefixes and timestamps removed)."""
    rest = line.split(" ", 1)[1] if " " in line else ""
    if line.startswith(("--- ", "+++ ")):
        rest = rest.split("\t", 1)[0]
        toks = [rest.strip()]
    else:
        toks = [t for t in rest.split() if not t.startswith("-")]
    out = []
    for t in toks:
        t = t.strip("\"'")
        out.append(t[2:] if len(t) > 2 and t[1] == "/" and t[0] in "abciwo" else t)
    return out


def omit_secret_file_lines(text: str, patterns: list[str] | None = None, keep_lines: bool = False) -> str:
    """Drop what a tool's output shows of a secret file, keeping the fact that it appeared:
      - `path:...` and `path-N-...` lines whose path is a secret file: grep, ripgrep and the Grep tool, with
        or without -n and context lines (`./.env:DB_PASS=...`, `./.env-1-KEY=...`, `config/.env:3:KEY=...`);
      - the hunks of a diff of a secret file: `git diff`, `git log -p`, `git show <commit>`, `git stash show -p`,
        `diff -u` / `diff -r` (the header lines stay, the hunks become one `[OMITTED: diff of a secret file]`).
    Used for every tool output by the hooks and by stream capture. keep_lines: the text keeps its line count (each
    omitted line becomes empty, the first of a section holds the marker): for sibling strings joined one per line
    (Redactor._run), so that each string keeps its own lines."""
    if not text or not (any(c in text for c in ":-") or "==> " in text):
        return text
    lines = text.split("\n")
    out, changed = [], False
    pending = None  # keep_lines: the marker the next omitted line becomes
    section = None  # None, or "head" / "hunk" while inside the diff of a secret file, "file" after `==> .env <==`
    for k, line in enumerate(lines):
        h = _HEAD_HEADER.match(line) if line.startswith("==> ") else None
        if h:
            out.append(line)
            section = None
            if is_secret_file(h.group(1).strip(), patterns):
                if keep_lines:
                    pending = _OMITTED_FILE
                else:
                    out.append(_OMITTED_FILE)
                section, changed = "file", True
            continue
        if section == "file":
            changed = True
            if keep_lines:
                out.append(pending or "")
                pending = None
            continue
        is_commit = line.startswith("commit ")
        # a `--- `/`+++ ` pair starts a file only outside a secret file's section (inside it, it is that file's
        # header, or content of its hunks)
        is_plain = section is None and line.startswith("--- ") and k + 1 < len(lines) \
            and lines[k + 1].startswith("+++ ")
        if is_commit or is_plain or line.startswith("diff "):
            paths = [] if is_commit else _diff_header_paths(line) + (
                _diff_header_paths(lines[k + 1]) if is_plain else [])
            secret = any(p and is_secret_file(p, patterns) for p in paths)
            section = "head" if secret else None
            out.append(line)
            if secret:
                if keep_lines:
                    pending = _OMITTED_DIFF
                else:
                    out.append(_OMITTED_DIFF)
                changed = True
            continue
        if section:
            if _HUNK_START.match(line):
                section = "hunk"
            if section == "head" and line.startswith(_DIFF_HEADER_KEEP):
                out.append(line)
            else:
                changed = True
                if keep_lines:
                    out.append(pending or "")
                    pending = None
            continue
        i = _secret_line_head(line, patterns)
        if i:
            out.append(f"{line[:i]}{line[i]} {_OMITTED_LINE}")
            changed = True
            continue
        out.append(line)
    return "\n".join(out) if changed else text


def tool_reads_secret_file(name: str, inp, project: Path | None = None, patterns: list[str] | None = None) -> bool:
    """Does this tool call read or write a secret file, so that its output (and input text) is never kept?
    Any path field of the input names one, or a shell command mentions one (`cat .env`, `git show HEAD:.env`,
    `Get-Content .env | Select-String KEY`)."""
    if not isinstance(inp, dict):
        return False
    return input_names_secret_file(inp, project, patterns)     # (a `command` value is checked as a shell command)


# ---- THE decision about a tool call and the stored form of its input, for every writer: stream capture
# (devmem.capture._Calls), the digest and the hooks (devmem.sessions.tool_entries), codex events (devmem.generic) ----

# What is stored of a call whose input the check could not read in full (call_decision "unchecked"), in place of its
# output: nothing of the input or the output is kept, and the record does not say that it named a secret file.
UNCHECKED_OUTPUT = ("[OMITTED: not stored: the call's input was too large to check for secret-file names, so neither "
                    "it nor the output is kept]")


def call_decision(name: str, inp, project: Path | None = None, patterns: list[str] | None = None) -> str:
    """"secret": the input names a secret file (tool_reads_secret_file); "unchecked": the check could not read the
    input in full (_unchecked_reason: a command over COMMAND_CHECK_HEAD + COMMAND_CHECK_TAIL characters or with a word
    over MAX_PATH_CHARS, a path over MAX_PATH_CHARS or path values past PATH_CHECK_BUDGET, a one-word value over
    MAX_PATH_CHARS under a key that may hold a path, nesting past MAX_INPUT_DEPTH, more than MAX_INPUT_NODES values or
    MAX_PATH_VALUES path values); "ok" otherwise. Both "secret" and "unchecked" keep nothing of the output; the first
    stores the input as secret_call_input gives it, the second as unchecked_input."""
    if not isinstance(inp, dict):
        return "ok"
    why: list = []
    if input_names_secret_file(inp, project, patterns, why):
        return "unchecked" if why else "secret"
    return "unchecked" if _unchecked_reason(inp) else "ok"


def call_names_secret_file(name: str, inp, project: Path | None = None, patterns: list[str] | None = None,
                           why: list | None = None) -> bool:
    """call_decision is not "ok": nothing of the call's output may be kept. `why` gets "too large" for "unchecked"."""
    d = call_decision(name, inp, project, patterns)
    if d == "unchecked" and why is not None:
        why.append("too large")
    return d != "ok"


def unchecked_input(inp) -> dict:
    """What is stored of the input of a call whose decision is "unchecked": the fact, the reason and its size."""
    try:
        size = len(json.dumps(inp, ensure_ascii=False))
    except (TypeError, ValueError, RecursionError):
        size = None
    reason = _unchecked_reason(inp) if isinstance(inp, dict) else None
    return {"input_not_checked": True, "reason": reason or "more values, path values or levels than the check reads",
            **({"chars": size} if size is not None else {})}


# a word of a command longer than MAX_PATH_CHARS (tried only where a word starts: linear in the command's length)
_LONG_WORD = re.compile(r"(?<![^\s\"'`;|&<>()=,])[^\s\"'`;|&<>()=,]{%d}" % (MAX_PATH_CHARS + 1))
_SPACE = re.compile(r"\s")


def _unchecked_reason(inp: dict) -> str | None:
    """Why input_names_secret_file does not read every string of this input in full, or None when it does."""
    stack: list = [(inp, "", 0)]
    nodes, path_chars = 0, 0
    while stack:
        v, key, depth = stack.pop()
        nodes += 1
        if depth > MAX_INPUT_DEPTH:
            return f"nested deeper than {MAX_INPUT_DEPTH} levels"
        if nodes > MAX_INPUT_NODES:
            return f"more than {MAX_INPUT_NODES:,} values"
        if isinstance(v, dict):
            stack.extend((x, (k if isinstance(k, str) else str(k))[:MAX_KEY_CHECK], depth + 1) for k, x in v.items())
        elif isinstance(v, list):
            stack.extend((x, key, depth + 1) for x in v)
        elif isinstance(v, str):
            if _COMMAND_KEY.search(key) or _COMMAND_LIKE_KEY.search(key):
                if len(v) > COMMAND_CHECK_HEAD + COMMAND_CHECK_TAIL:
                    return f"a command of {len(v):,} characters (over {COMMAND_CHECK_HEAD + COMMAND_CHECK_TAIL:,})"
                if _LONG_WORD.search(v):
                    return f"a command with a word of over {MAX_PATH_CHARS:,} characters"
            elif _NOT_PATH_KEY.search(key) or len(v) <= MAX_PATH_CHARS and not _PATH_KEY.search(key):
                continue
            elif _PATH_KEY.search(key):
                path_chars += len(v)
                if len(v) > MAX_PATH_CHARS:
                    return f"a path of {len(v):,} characters (over {MAX_PATH_CHARS:,})"
                if path_chars > PATH_CHECK_BUDGET:
                    return f"paths of over {PATH_CHECK_BUDGET:,} characters in all"
            elif not _SPACE.search(v.strip()):
                return f"one word of {len(v):,} characters (over {MAX_PATH_CHARS:,})"
    return None


def _read_in_full(inp: dict) -> bool:
    """Does input_names_secret_file read every string of this input in full (_unchecked_reason)?"""
    return _unchecked_reason(inp) is None


# keys of a secret call's input whose value is kept, when the value is shaped like a path (_PATH_VALUE): a path names
# the file and holds none of it; every other string of that input is replaced (secret_call_input)
_STORED_PATH_KEY = re.compile(r"(?i)^(?:file_path|filepath|filePath|notebook_path|path|paths|file|files|filename|source|"
                              r"destination|target|dir|directory|uri|glob|relative_path|absolute_path|root|cwd|workdir|"
                              r"working_directory|folder)$")
# a value kept as a path: one line of at most MAX_PATH_CHARS with a separator or a dot, no `=`, quote, shell operator,
# wildcard or variable (`NOTE=x` under `source` is no path; `C:/Users/A B/x.py` is one)
_PATH_VALUE = re.compile(r"(?=[^=]*[./\\])[^\t\n\r\f\v\"'`<>|;&()=*?$%]{1," + str(MAX_PATH_CHARS) + "}")
_PLAIN_KEY = re.compile(r"[A-Za-z0-9_$.:@-]{1,128}")
MAX_SECRET_NAMES = 8
# programs whose name a secret call's stored command keeps (any other first word is stored as "other")
KNOWN_PROGRAMS = frozenset(
    "cat tac echo printf tee sed awk gawk cp mv rm touch chmod chown ln grep egrep rg head tail less more nl cut sort "
    "uniq wc diff xxd od base64 strings dd jq yq type source . export env set unset vi vim nvim nano code python "
    "python3 py node deno bun ruby perl php bash sh zsh fish pwsh powershell cmd git docker npm npx pip curl wget ssh "
    "scp rsync tar zip unzip openssl gpg sops aws gcloud az kubectl terraform make findstr more.com get-content gc "
    "set-content sc add-content ac out-file copy-item cpi move-item mi remove-item ri new-item ni select-string sls "
    "get-item gi test-path write-output write-host".split())
# programs whose arguments name files (their operands are file names; for others only a redirect target is one)
_FILE_PROGRAMS = frozenset(
    "cat tac tee cp mv rm touch chmod chown ln head tail less more nl cut sort uniq wc diff xxd od base64 strings "
    "source . vi vim nvim nano code type get-content gc set-content sc add-content ac out-file copy-item cpi "
    "move-item mi remove-item ri new-item ni get-item gi test-path sed grep egrep rg findstr select-string sls".split())
_SEGMENT = re.compile(r"\|\||&&|[;|&\n]")
# a shell word; a quote that does not close runs to the end (no match fails part way: linear in the text)
_WORD = re.compile(r"""(?:[^\s'"]+|'[^']*(?:'|\Z)|"(?:[^"\\]|\\.?)*(?:"|\Z))+""", re.S)
_REDIRECT = re.compile(r"^\d*(?:>>?|<)(.*)$")


def _program(word: str) -> str:
    """The name of a command's first word as KNOWN_PROGRAMS knows it (`/usr/bin/cat`, `cat.exe` -> `cat`), else
    "other"."""
    base = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    base = base[:-4] if base.endswith(".exe") else base
    return base if base in KNOWN_PROGRAMS else "other"


def secret_command_marker(command: str, patterns: list[str] | None = None) -> str:
    """What is stored of a command (any command-like string) of a call that names a secret file: a fixed text that
    keeps at most the program (the first word, when KNOWN_PROGRAMS has it; else "other") and the names of the secret
    files the command's own syntax gives as files: a redirect target (`>> .env`), or an operand of a program that
    takes files (_FILE_PROGRAMS: `cat .env`, `Set-Content .env`), never a word with `=` in it (the value side of
    `NAME=value`); at most MAX_SECRET_NAMES names of 64 characters, matched by a specific secret-file pattern.
    `echo NOTE=X >> .env` -> program `echo`, file `.env`; `X > .env` -> program other, file `.env`."""
    command = str(command)
    text = command if len(command) <= COMMAND_CHECK_HEAD + COMMAND_CHECK_TAIL else \
        command[:COMMAND_CHECK_HEAD] + "\n" + command[-COMMAND_CHECK_TAIL:]
    names: list[str] = []
    patterns = _specific(patterns)     # a name a specific pattern gives (`.env`), never any word holding `secret`
    prog = None

    def add(word: str) -> None:
        w = word.strip("\"'")
        if not w or "=" in w or w.startswith("-") or len(w) > MAX_PATH_CHARS or len(names) >= MAX_SECRET_NAMES \
                or _SPACE.search(w):
            return
        name = _last_name(w)
        if name and len(name) <= 64 and name not in names and is_secret_file(name, patterns):
            names.append(name)
    for seg in _SEGMENT.split(text):
        words = _WORD.findall(seg)
        if not words:
            continue
        first = _program(words[0].strip("\"'"))
        prog = prog or first
        files = first in _FILE_PROGRAMS
        expect = False                         # the word after a bare `>` / `>>` / `<` is its target
        for w in words[1:]:
            m = _REDIRECT.match(w)
            if m:
                if m.group(1):
                    add(m.group(1))
                else:
                    expect = True
                continue
            if expect or files:
                add(w)
            expect = False
    return ("[OMITTED: a command that names a secret file" + (f"; program: {prog}" if prog else "")
            + (f"; secret files: {' '.join(names)}" if names else "") + "]")


MAX_STORED_NODES = 1_000


def _kept_path(key: str, v: str) -> bool:
    """A value kept in a secret call's stored form: shaped like a path, under a key the decision reads as a path."""
    path_key = bool(_STORED_PATH_KEY.match(key)) or bool(_PATH_KEY.search(key)) and not _NOT_PATH_KEY.search(key)
    return path_key and bool(_PATH_VALUE.fullmatch(v))


def secret_call_input(inp: dict, patterns: list[str] | None = None) -> dict:
    """The stored form of the input of a call that names a secret file (call_decision "secret"): a command-like string
    becomes secret_command_marker, a value shaped like a path (_PATH_VALUE) under a path key is kept, and every other
    string is OMITTED_SECRET_FILE whatever its key (an unknown key cannot carry the file's text); numbers, booleans and
    null are kept and a key that is not a plain word is replaced. An input of more than MAX_STORED_NODES values, or
    nested deeper than MAX_INPUT_DEPTH, keeps only its paths at the top level and `input_omitted` (so the stored form
    is never larger than a few markers per value of a small input)."""
    stack: list = [(inp, 0)]
    nodes = 0
    while stack:
        v, depth = stack.pop()
        nodes += 1
        if nodes > MAX_STORED_NODES or depth > MAX_INPUT_DEPTH:
            kept = {k: x for k, x in inp.items() if isinstance(k, str) and isinstance(x, str) and _kept_path(k, x)}
            return {**kept, "input_omitted": OMITTED_SECRET_FILE}
        if isinstance(v, dict):
            stack.extend((x, depth + 1) for x in v.values())
        elif isinstance(v, list):
            stack.extend((x, depth + 1) for x in v)

    def walk(v, key: str, depth: int) -> Any:
        if isinstance(v, dict):
            out = {}
            for i, (k, x) in enumerate(v.items()):
                kk = k if isinstance(k, str) and _PLAIN_KEY.fullmatch(k) else f"[devmem: key {i} omitted]"
                out[kk] = walk(x, kk[:MAX_KEY_CHECK], depth + 1)
            return out
        if isinstance(v, list):
            return [walk(x, key, depth + 1) for x in v]
        if isinstance(v, str):
            if _COMMAND_KEY.search(key) or _COMMAND_LIKE_KEY.search(key):
                return secret_command_marker(v, patterns)
            return v if _kept_path(key, v) else OMITTED_SECRET_FILE
        return v
    out = walk(inp, "", 0)
    return out if isinstance(out, dict) else {"input_omitted": OMITTED_SECRET_FILE}


# ---- a structured result kept on an "ok" decision: a part of it that names a secret file keeps its paths only ----

def _named_path(v: str, base: str | None) -> str:
    """A path value as the secret-file patterns check it: a `file://` URL decoded, relative to the project when it
    lies inside it (so the project's own folder name never counts), its last name only when it is longer than a path."""
    v = _path_value(v)
    if len(v) > MAX_PATH_CHARS:
        return _last_name(v)
    if base:
        low = v.replace("\\", "/")
        if low.lower().startswith(base + "/"):
            return low[len(base) + 1:]
    return v


# keys under which a tool result holds a file's text although their name says "file" (Claude Code's Edit/Write result
# `originalFile`): a value of more than one line there is that text, not a path (_file_text)
_FILE_TEXT_KEY = re.compile(r"(?i)^(?:originalfile|original_file|filecontent|file_content|filecontents|oldstring|"
                            r"newstring)$")


def _file_text(key: str, w: str) -> bool:
    """A value under a file-text key that is a file's text: more than one line once ONE trailing line break is
    removed. Any other value (one line under such a key, anything under another path key) is judged as a path."""
    if not _FILE_TEXT_KEY.match(key):
        return False
    for end in ("\r\n", "\n", "\r"):
        if w.endswith(end):
            w = w[:-len(end)]
            break
    return "\n" in w or "\r" in w


def _names_secret_file(d: dict, base: str | None, patterns: list[str] | None) -> bool:
    """Does this object name a secret file: a string (or a list of strings) under a path key, or a key shaped like a
    path (`{"C:/p/.env": [...]}`)? A file's multi-line text under a file-text key is not a path (_file_text)."""
    for k, v in d.items():
        if not isinstance(k, str):
            continue
        if any(c in k for c in "./\\") and is_secret_file(_named_path(k, base), _specific(patterns)):
            return True
        key = k[:MAX_KEY_CHECK]
        if not _PATH_KEY.search(key) or _NOT_PATH_KEY.search(key):
            continue
        vals = [v] if isinstance(v, str) else [w for w in v[:MAX_PATH_VALUES] if isinstance(w, str)] \
            if isinstance(v, list) else []
        if any(w and not w[:7].lower().startswith(("http://", "https:/")) and not _file_text(key, w) and
               is_secret_file(_named_path(w, base), patterns) for w in vals):
            return True
    return False


def scrub_secret_parts(x, project: Path | None = None, patterns: list[str] | None = None):
    """A structured tool result (or any field stored on an "ok" decision) with every object that names a secret file
    (_names_secret_file) stored as secret_call_input stores a secret call's input: its paths, and every other string
    under it at any depth OMITTED_SECRET_FILE; whatever the surrounding keys are called (`bashEditDiff.files[].hunks`,
    `structuredPatch`, a diagnostics map keyed by path). Anything else is returned as it is (no copy when nothing
    changes). Depth is bounded by the line's own (devmem.capture.MAX_EVENT_DEPTH)."""
    base = None
    if project is not None:
        try:
            base = os.path.abspath(str(project)).replace("\\", "/").rstrip("/").lower()
        except (OSError, ValueError):
            base = None

    def walk(v):
        if isinstance(v, dict):
            if _names_secret_file(v, base, patterns):
                return secret_call_input({str(k): w for k, w in v.items()}, patterns)
            new = {k: walk(w) for k, w in v.items()}
            return v if all(new[k] is w for k, w in v.items()) else new
        if isinstance(v, list):
            new = [walk(w) for w in v]
            return v if all(a is b for a, b in zip(new, v)) else new
        return v
    return walk(x)


def filter_tool_output(name: str, inp, text: str, project: Path | None = None,
                       patterns: list[str] | None = None) -> tuple[str, bool]:
    """THE one place that decides what of a tool's output may be kept (hooks and stream capture both call it):
    (OMITTED_SECRET_FILE, True) when the call reads a secret file; otherwise the text with secret-file lines
    and diff hunks removed (see omit_secret_file_lines), and False. Redaction of the remaining text is the
    caller's next step."""
    if tool_reads_secret_file(name, inp, project, patterns):
        return OMITTED_SECRET_FILE, True
    return omit_secret_file_lines(text, patterns) if isinstance(text, str) else text, False


def redact_text(text: str, store: Path | None = None) -> str:
    """Convenience wrapper: redact one string with built-in + `<store>/redact` patterns."""
    return Redactor(store).text(text)
