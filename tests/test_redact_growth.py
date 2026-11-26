"""Redaction of a whole text stays fast when one rule matches over and over: every rule's own match, repeated on one
line and on many lines, at 100 KB and 512 KB, through Redactor.text (the patterns, the replacement callbacks and the
repeat loop together).

Before the fix `"pwd=Ab12Cd34;"` repeated on one line cost 2.7 s at 10 KB and did not finish in 5 minutes at 100 KB:
the connection-string callback searched the whole line for every match, and the repeat loop ran 8 more rounds for a
rule whose callback changed nothing. tests/test_redact_speed.py times the bare patterns on other adversarial input.
"""
from __future__ import annotations

import json
import re
import time

import pytest

from devmem import redact as R

# one match per rule (in _RULES order, then the assignment rules); each seed is checked to match its rule below
SEEDS: dict[str, str] = {
    "private-key": "-----BEGIN RSA PRIVATE KEY-----ab-----END RSA PRIVATE KEY----- ",
    "anthropic-key": "sk-ant-" + "a1" * 10 + " ",
    "openai-key": "sk-" + "a1" * 12 + " ",
    "github-token": "ghp_" + "a1" * 15 + " ",
    "gitlab-token": "glpat-" + "a1" * 10 + " ",
    "mailgun-key": "key-" + "0a" * 16 + " ",
    "slack-token": "xoxb-" + "a1" * 5 + " ",
    "slack-webhook": "hooks.slack.com/services/" + "a1" * 10 + " ",
    "sendgrid-key": "SG." + "a" * 22 + "." + "b" * 43 + " ",
    "pypi-token": "pypi-AgE" + "a1" * 25 + " ",
    "google-oauth-secret": "GOCSPX-" + "a1" * 10 + " ",
    "telegram-bot-token": "123456789:AA" + "a" * 33 + " ",
    "stripe-webhook-secret": "whsec_" + "a1" * 12 + " ",
    "vault-token": "hvs." + "a1" * 12 + " ",
    "discord-webhook": "discord.com/api/webhooks/12345/" + "a1" * 10 + " ",
    "digitalocean-token": "dop_v1_" + "a1" * 32 + " ",
    "twilio-key": "SK" + "a1" * 16 + " ",
    "databricks-token": "dapi" + "a1" * 16 + " ",
    "shopify-token": "shpat_" + "a1" * 16 + " ",
    "aws-access-key-id": "AKIA" + "A1" * 8 + " ",
    "google-api-key": "AIza" + "a" * 35 + " ",
    "stripe-key": "sk_live_" + "a1" * 8 + " ",
    "docker-token": "dckr_pat_" + "a1" * 12 + " ",
    "linear-key": "lin_api_" + "a1" * 16 + " ",
    "age-secret-key": "AGE-SECRET-KEY-1" + "QZ" * 29 + " ",
    "fcm-server-key": "AAAAabc1234:APA91b" + "a1" * 12 + " ",
    "discord-bot-token": "MTE0NTY3ODkwMTIzNDU2Nzg5.GaBcDe." + "a1" * 15 + " ",
    "dsn-key": "https://" + "0a" * 16 + "@o1.ingest.sentry.io/2 ",
    "huggingface-token": "hf_" + "a1" * 15 + " ",
    "npm-token": "npm_" + "a1" * 18 + " ",
    "jwt": "eyJ" + "a" * 8 + ".eyJ" + "a" * 8 + "." + "a" * 8 + " ",
    "bearer": "Bearer " + "a1" * 8 + " ",
    "url-credentials": "a://u:pw1@h ",
    "argument": "mysql -pAb12Cd34 ",
    "argument#2": "--token=Ab12Cd34 ",
    "argument#3": "curl -u a:b1x ",
    "argument#4": "docker login -u me -p Ab12Cd34 ",
    "netrc": "machine h login me password Ab12Cd34 ",
    "header": "X-API-Key: Ab12Cd34Ef ",
    "header#2": "Authorization: Token Ab12Cd34Ef ",
    "query-secret": "?sig=Ab12Cd34& ",
    "connection-key": "AccountKey=Ab12Cd34Ef; ",
    "npmrc-token": ":_authToken=Ab12Cd34 ",
    "basic-auth": ":_auth=dXNlcjpwYXNz ",
    "basic-auth#2": '"auth": "dXNlcjpwYXNz" ',
    "assignment": "pwd=Ab12Cd34;",
    # round 6: base64 PEM, PuTTY, vendor table, command table, headers/cookies/query by name, config shapes
    "private-key#2": "LS0tLS1CRUdJTiBSU0EgUFJJVkFURSBLRVktLS0tLQ== ",
    "private-key#3": "Private-Lines: 1\nQUJDREVGR0g=\n",
    # round 16: key material without its BEGIN line (base64 lines right above an END line)
    "private-key#4": "QUJDREVGR0hJSktMTU5PUFFS\n-----END RSA PRIVATE KEY-----\n",
    "private-key#5": '"-----BEGIN RSA PRIVATE KEY-----\\n" +\n"QUJDREVGR0hJSktMTU5PUFFSU1RVVldY\\n" +\n'
                     '"-----END RSA PRIVATE KEY-----"\n',
    "vendor-token": "gsk_" + "a1" * 20 + " ",
    "terraform-token": "abcdefghijklmn.atlasv1." + "a1" * 30 + " ",
    "argument#5": "redis-cli -h h -a Ab12Cd34 ",
    "argument#6": "openssl pkcs12 -passout pass:Ab12Cd34 ",
    "argument#7": "vault login " + "a1" * 12 + " ",
    "argument#8": "smbclient //h/s -U me%Ab12Cd34 ",
    "argument#9": "sqlplus scott/Ab12Cd34@db ",
    "argument#10": "aws configure set aws_secret_access_key Ab12Cd34 ",
    "header#3": "X-Figma-Token: Ab12Cd34Ef56Gh ",
    "cookie": "Cookie: sid=Ab12Cd34Ef56\n",
    "cookie#2": 'curl -b "sid=Ab12Cd34Ef56" ',
    "query-secret#2": "?password=Ab12Cd34& ",
    "basic-auth#3": "NPM_CONFIG__AUTH=dXNlcjpwYXNz ",
    "assignment#2": "Server=db;Pwd={Ab12;Cd34}; ",
    "assignment#3": "<password>Ab12Cd34</password> ",
    "assignment#4": 'key="DbPassword" value="Ab12Cd34" ',
    "assignment#5": '"sensitive": true, "type": "string", "value": "Ab12Cd34"',   # (Terraform: _sensitive_repl)
    "assignment#6": '"value": "Ab12Cd34", "type": "string", "sensitive": true',   # (a state's order, round 16)
    "assignment#7": '"id": "https://kv.vault.azure.net/secrets/a/1", "value": "Ab12Cd34" ',   # (_key_vault_repl)
    "assignment#8": 'auth=("me", "Ab12Cd34") ',
    "argument#11": 'ConvertTo-SecureString "Ab12Cd34" ',
    "assignment#9": "define('DB_PASSWORD', 'Ab12Cd34'); ",
    "assignment#10": 'variable "db_password" { default = "Ab12Cd34" } ',
    "assignment#11": "password: |\n  Ab12Cd34\n",
    "assignment#12": "api_key = '''Ab12Cd34''' ",
    "assignment#13": "password: \"Ab12Cd34\n  Ef56Gh78\"\n",   # (a value over lines: _spanning_repl)
    "assignment#14": "API_KEY=Ab12Cd34\\\nEf56Gh78\n",   # (a value over lines: _spanning_repl)
    "assignment#15": 'passphrase = "Ab12 Cd34 Ef56" ',
    "assignment#16": "| API_TOKEN | Ab12Cd34Ef |",
    "assignment#17": "ENV DB_PASSWORD Ab12Cd34\n",
    "assignment#18": "mail.smtp.password Ab12Cd34\n",
    # round 7 (verifier corpus 6)
    "assignment#19": "CREATE USER app WITH PASSWORD 'Ab12Cd34'; ",
    "assignment#20": "ALTER USER scott IDENTIFIED BY Ab12Cd34; ",
    "assignment#21": 'ds.setPassword("Ab12Cd34"); ',
    "header#4": 'proxy_set_header X-Api-Key "Ab12Cd34Ef56"; ',
    "assignment#22": "db:5432:app:me:Ab12Cd34\n",
    "webhook": "x.webhook.office.com/webhookb2/" + "a1" * 25 + " ",
    "cookie#3": "_|WARNING:-DO-NOT-SHARE-THIS.x|_" + "A1" * 25 + " ",
    "argument#12": "echo Ab12Cd34 | sudo -S ls\n",
    "argument#13": "-Password Ab12Cd34 ",
    "argument#14": "/pass:Ab12Cd34 ",
    "argument#15": "-u me:Ab12Cd34 ",
    "argument#16": "7z a -pAb12Cd34 x ",
    "argument#17": "consul kv put db/password Ab12Cd34\n",
    "assignment#23": "password Ab12Cd34\n",
    "assignment#24": "[smtp.h]:587 me:Ab12Cd34\n",
    "argument#18": "ACL SETUSER me on >Ab12Cd34 ",
    "url-credentials#2": "me:Ab12Cd34@tcp(db) ",
    "assignment#25": "the password is Ab12Cd34Ef ",
    "assignment#26": "DB_PASSWORD                    Ab12Cd34Ef56\n",
    "assignment#27": '  ~ master_password = "old" -> "Ab12Cd34Ef56"\n',
    "assignment#28": '{"password": 84736251},\n',
    "assignment#29": "password | Ab12Cd34Ef56\n",
    # round 10 (verifier v9b item 4)
    "assignment#30": 'char password[] = "Ab12Cd34Ef56"; ',
    "assignment#31": 'public string Password { get; set; } = "Ab12Cd34Ef56"; ',
    "assignment#32": "${DB_PASSWORD:-Ab12Cd34Ef56} ",
    "assignment#33": "{Host:db Password:Ab12Cd34Ef56} ",
    "assignment#34": "  [password] => Ab12Cd34Ef56\n",
    "assignment#35": 'process.env.API_KEY || "Ab12Cd34Ef56"; ',
    # round 11 (verifier v10b): a YAML flow mapping item, a flag and its value as two list items (flow and block)
    "assignment#36": "{user: app, db_password: Ab12Cd34Ef56} ",
    "argument#19": '["--password", "Ab12Cd34Ef56"], ',
    "argument#20": "- --requirepass\n- Ab12Cd34Ef56\n",
    # round 16: a sequence under a secret name (flow, block)
    "assignment#37": "api_keys: [Ab12Cd34Ef56, Zy98Xw76Vu54]\n",
    "assignment#38": "api_keys:\n  - Ab12Cd34Ef56\n  - Zy98Xw76Vu54\n",
    "assignment#39": "<password><![CDATA[Ab12Cd34Ef56]]></password> ",
    # the assignment rules (_ASSIGN_RULES)
    "quoted-line": "DB_PASSWORD='Ab12'Cd34Ef56'\n",
    "quoted": '"password": "Ab12Cd34Ef56", ',
    "quoted#2": 'const password: string | undefined = "Ab12Cd34Ef56"; ',
    "quoted#3": '\\"password\\": \\"Ab12Cd34Ef56\\" ',
    "quoted#4": 'os.environ["X_API_KEY"] = "Ab12Cd34Ef56"; ',
    "env": "API_KEY=Ab12Cd34Ef56 ",
    "lower-env": '"db_password=Ab12Cd34Ef56" ',
    "yaml": "password: Ab12Cd34Ef56\n",
    "line": "db_password = Ab12Cd34Ef56\n",
}
# shapes that put a rule's context on the same line as many matches
EXTRA = {
    # round 10: a word of letters only, judged with the lines around it (_letters_value, _line_context), typed forms
    "letters-yaml": "password: CorrectHorse\n",
    "block-scalar-long": "password: |\n" + "  Ab12Cd34Ef56\n" * 200,     # (round 11 follow-up: no line limit)
    "letters-typed-field": "  password: CorrectHorse;\n",
    "letters-ini": "password = CorrectHorse\n",
    "letters-prose": "the password is CorrectHorse, ",
    "letters-table": "DB_PASSWORD    CorrectHorse\n",
    "typed-ts-optional": '  apiKey?: string = "Ab12Cd34Ef56"; ',
    "typed-rust-bytes": 'const API_KEY: &[u8] = br#"Ab12Cd34Ef56"#; ',
    "typed-go-block": '\tapiSecret string = "Ab12Cd34Ef56"\n',
    "swift-raw": 'let password = #"Ab12Cd34Ef56"#; ',
    # round 9: typed declarations and the other string forms of a quoted value, YAML anchors and tags
    "typed-rust-raw": "static API_KEY: &'static str = r#\"Ab12Cd34Ef56\"#; ",
    "typed-go": 'var password string = "Ab12Cd34Ef56"\n',
    "typed-python-optional": '    token: Optional[str] = "Ab12Cd34Ef56"\n',
    "csharp-verbatim": 'var password = @"Ab12Cd34Ef56"; ',
    "template-literal": "const password = `Ab12Cd34Ef56`; ",
    "python-prefixed": "password = rb'Ab12Cd34Ef56' ",
    "yaml-anchor": "password: &pw Ab12Cd34Ef56\n",
    "yaml-tag-quoted": 'password: !!str "Ab12Cd34Ef56"\n',
    "typed-field-lines": "  password: string;\n  token: Option<String>,\n",
    "conn-string-line": "Server=db;User Id=sa;" + "Password=Ab12Cd34;" * 1,
    "conn-many": "Server=db;Password=Ab12Cd34;",
    "mysql-chain": " -pAb1",
    "curl-chain": " -u a:b1x",
    "token-chain": " --token Ab12Cd34Ef",
    "env-in-string": '"API_KEY=Ab12Cd34Ef56",',
    "env-in-json-string": '\\"API_KEY=Ab12Cd34Ef56\\",',
    "env-before-quote-unclosed": "API_KEY=Ab12Cd34Ef56'",
    # JSON string literals, decoded and redacted as plain text (Redactor._json_strings)
    "json-literal": '"spring:\\n  password: Ab12Cd34Ef56\\n",',
    "json-literal-escaped-quotes": '"\\"',
    "json-literal-open": '"password \\n',
    "json-literal-nested": json.dumps(json.dumps({"a": "password: Ab12Cd34Ef56\n"})) + ",",
    "json-key-value": '{"password": "Ab12Cd34Ef56"},',
    # lines as tools print them, redacted once more without the prefix (Redactor._line_view)
    "grep-line": "config/app.yml:3:  password: Ab12Cd34Ef56\n",
    "grep-context-line": "config/app.yml-3-  password: Ab12Cd34Ef56\n",
    "diff-line": "+  password: Ab12Cd34Ef56\n",
    "diff-removed-env-line": "-DB_PASSWORD=Ab12Cd34Ef56\n",
    "cat-n-line": "     5\tpassword: Ab12Cd34Ef56\n",
    "read-tool-line": "     5→password: Ab12Cd34Ef56\n",
    "comment-line": "# password: Ab12Cd34Ef56\n",
    "quote-line": "> password: Ab12Cd34Ef56\n",
    "trailing-comment-line": "password: Ab12Cd34Ef56  # prod\n",
    # one string per line, redacted as one text (Redactor._json_strings runs, _join_items / _split_items)
    "json-line-array": '"password: |", "  Ab12Cd34Ef56", ',
    "json-patch-lines": '"+-----BEGIN RSA PRIVATE KEY-----", "+MIIEowIBAAKCAQEAx7Kq2Lm9Pz4Rt8Vw3Yb6", '
                        '"+-----END RSA PRIVATE KEY-----", ',
    "json-sibling-values": '"a": "password: |", "b": "  Ab12Cd34Ef56", ',
    "ansi-diff-line": "\x1b[32m+password: Ab12Cd34Ef56\x1b[m\n",
    "ansi-highlight-line": "DB_PASSWORD=\x1b[01;31m\x1b[KAb12Cd34Ef56\x1b[m\x1b[K\n",
    "json-content-blocks":'{"type": "text", "text": "password: |"}, {"type": "text", "text": "  Ab12Cd34Ef56"}, ',
}
# 512 KB of any one shape. What these tests prove is that the cost grows linearly: 512 KB may cost at most GROWTH times
# what 100 KB costs (5.1 times the text; a rule that is quadratic costs 26 times as much, one that is not linear takes
# minutes here), measured as CPU time of this process (time.process_time: other processes on a loaded machine or a
# shared CI runner do not count), the minimum of runs of each size taken in turns, and a failing shape is measured
# again after pauses before it fails (linear_growth). GROWTH is 10, not 8: ten pytest runs at once with three CPU
# burners on 16 cores (review B11) made a busy machine's caches slow the larger text more than its size, one or two
# linear shapes per run going past 8; a quadratic one still costs 26 times as much
# (test_the_growth_check_fails_on_a_quadratic_rule). CEIL_S is one generous absolute ceiling of CPU time at 512 KB
# (the slowest shapes, a command-line secret repeated 30,000 times on one line, take about 2 s alone; on a machine
# running two full suites CPU time rose to 7 s for a017af9 and this code alike: a busy core slows a process's own work
# too). BOUND_S is the older wall-clock bound other tests share.
BOUND_S = 5.0
CEIL_S = 15.0
GROWTH = 10.0
# a literal holding a literal is redacted three times (the text, each decoded level): a larger constant, still linear
SLOWER = {"json-literal-nested": 2.0, "json-literal": 1.5}


def _rules() -> list[tuple[str, re.Pattern]]:
    out, seen = [], {}
    for kind, rx, _repl in list(R._RULES) + list(R._ASSIGN_RULES):
        seen[kind] = seen.get(kind, 0) + 1
        out.append((kind if seen[kind] == 1 else f"{kind}#{seen[kind]}", rx))
    return out


def test_every_rule_has_a_seed_that_it_matches():
    for name, rx in _rules():
        assert name in SEEDS, f"add a seed for rule {name}"
        assert rx.search(SEEDS[name]), f"the seed for {name} does not match it"


def _line(seed: str, size: int, sep: str = "") -> str:
    unit = seed + sep
    return unit * (size // len(unit) + 1)


SHAPES = sorted(SEEDS) + sorted(EXTRA)


def _cpu(fn) -> float:
    t = time.process_time()
    fn()
    return time.process_time() - t


def _linear(t1: float, t2: float, bound: float) -> bool:
    """The verdict: the ratio, and the ceiling unless the small text was slow too (a machine so loaded that both sizes
    are slow together is judged on the ratio alone, up to three times the ceiling)."""
    if t2 > GROWTH * max(t1, 0.05):
        return False
    return t2 < bound or (t1 * GROWTH >= bound and t2 < 3 * bound)


def linear_growth(make, bound: float, small: int = 100_000, big: int = 512_000) -> tuple[float, float]:
    """(CPU s at `small`, at `big`) of make(size)() when the cost is linear: the big text costs at most GROWTH times
    the small one (and 0.05 s of clock resolution) and at most `bound` (_linear); else AssertionError. The two sizes
    are run in turns, three times each (the minimum counts); a failing verdict is measured again, after a pause that
    lets a burst of load on the machine pass, up to three more times with five runs each, before it fails. A quadratic
    cost fails every one of them (test_the_growth_check_fails_on_a_quadratic_rule)."""
    fs, fb = make(small), make(big)
    t1 = t2 = 1e9
    for attempt in range(4):
        if attempt:
            time.sleep(1.0 * attempt)
        for _ in range(3 if attempt == 0 else 5):
            t1 = min(t1, _cpu(fs))
            t2 = min(t2, _cpu(fb))
            if _linear(t1, t2, bound):
                return t1, t2
    raise AssertionError(f"{t1:.3f} s at {small}, {t2:.3f} s at {big} (bound {bound} s, growth {GROWTH})")


@pytest.mark.parametrize("sep", ["", "\n"], ids=["one line", "lines"])
@pytest.mark.parametrize("name", SHAPES)
def test_one_line_of_one_match_repeated(name, sep):
    seed = SEEDS.get(name) or EXTRA[name]
    bound = CEIL_S * SLOWER.get(name, 1.0)

    def make(size):
        red, s = R.Redactor(None), _line(seed.rstrip("\n"), size, sep)
        return lambda: red.text(s)
    try:
        linear_growth(make, bound)
    except AssertionError as e:
        raise AssertionError(f"{name}: {e}") from None


def test_the_growth_check_fails_on_a_quadratic_rule(monkeypatch):
    """Self-test: a rule that scans to the end of its line from every start (quadratic in the line) fails the check."""
    monkeypatch.setattr(R, "_RULES", [("assignment", re.compile(r"x(?=[^\n]*Q)"), "y")])
    monkeypatch.setattr(R, "_ASSIGN_RULES", [])

    def make(size):
        red, s = R.Redactor(None), "x" * (size // 8)
        return lambda: red._rules(s, R._RULES, R._ASSIGN_RULES)
    with pytest.raises(AssertionError):
        linear_growth(make, 60.0, 80_000, 400_000)


@pytest.mark.parametrize("levels", [1, 4, 8])
def test_one_large_literal_nested_deeply_is_bounded(levels):
    text = "spring:\n  password: Ab12Cd34Ef56\n  timeout: 30\n" * (512_000 // 46 // levels)
    for _ in range(levels):
        text = json.dumps({"result": text})
    red = R.Redactor(None)
    bound = BOUND_S * SLOWER["json-literal-nested"]   # each level is redacted again: about 3 s alone at 8 levels
    dt = 1e9
    for _ in range(3):
        dt = min(dt, _cpu(lambda: red.text(text)))       # (CPU time of this process, best of three)
        if dt < bound:
            break
    assert dt < bound, f"{levels} levels: {dt:.2f}s on {len(text)} characters"
    assert "Ab12Cd34Ef56" not in red.text(text)


def test_a_rule_that_changes_nothing_is_not_run_again(monkeypatch):
    calls = []
    rx = re.compile(r"pwd=\w+;")

    def unchanged(m):
        calls.append(1)
        return m.group(0)
    monkeypatch.setattr(R, "_RULES", [("assignment", rx, unchanged)])
    R.Redactor(None).text("pwd=abc; pwd=def;")
    assert len(calls) == 2, len(calls)        # one pass, not 9


def test_redaction_of_one_text_is_idempotent_on_the_seeds():
    red = R.Redactor(None)
    for seed in list(SEEDS.values()) + list(EXTRA.values()):
        once = red.text(seed * 3)
        assert red.text(once) == once, seed
