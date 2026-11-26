"""Redaction cost is linear and bounded: every pattern on adversarial inputs, and the cap-before-redact rule.

Before the fix, the quoted-assignment pattern was quadratic on a long run of word/dot/hyphen characters (a
13 KB SVG path took 27 s inside one hook); the YAML, env and URL patterns had the same problem on other runs.
The bounds below are generous (machines differ) but far below what a quadratic pattern needs at this size.
"""
from __future__ import annotations

import base64
import random
import string
import time

import pytest

from devmem import redact as R

N = 100_000


def _inputs(n: int) -> dict[str, str]:
    rnd = random.Random(7)
    al = string.ascii_letters + string.digits
    svg = "M" + "".join(f"{rnd.randint(0, 99)}.{rnd.randint(0, 9)}-{rnd.randint(0, 9)}.{rnd.randint(0, 9)}l"
                        for _ in range(n // 9))
    minjs = "".join(rnd.choice(["a.b=", "function(e){", "return ", "var t=", "e.token=", "'x'", ";", "}",
                                "n.password='", "c&&", "(", ")", ",", "x-y"]) for _ in range(n // 5))
    runs = {name: ch * n for name, ch in (("word", "a"), ("digit", "1"), ("underscore", "_"), ("dot", "."),
                                          ("hyphen", "-"), ("space", " "), ("newline", "\n"), ("colon", ":"),
                                          ("equals", "="), ("quote", '"'), ("slash", "/"))}
    return {
        **runs,
        "hyphen-joined": "-".join("ab" for _ in range(n // 3)),
        "dotted": ".".join("ab" for _ in range(n // 3)),
        "b64url": "".join(rnd.choice(al + "_") if i % 40 else "-" for i in range(n)),
        "base64": base64.b64encode(rnd.randbytes(n * 3 // 4)).decode(),
        "svg-path": svg,
        "minified-js": minjs,
        "PASSWORD-run": "PASSWORD_" * (n // 9),
        "password-yaml": "password: " * (n // 10),
        "token-quote": "token = '" * (n // 9),
        "sk-run": "sk-" * (n // 3),
        "xox-run": "xoxb-" * (n // 5),
        "scheme-run": "a://b:" * (n // 6),
        "jwt-ish": ("eyJ" + "a" * 8 + ".") * (n // 12),
        "env-dots": "TOKEN=abcdefg" + "." * n + "x",
        "env-chain": "A=:" * (n // 3),
        "quoted-open": 'a="' + "b" * n,
        "mysql-run": "mysql " * (n // 6),
        "yaml-indent": " " * n + "password: 1",
        "key-begin": "-----BEGIN RSA PRIVATE KEY-----" * (n // 31),
        "conn-chain": "password=a " * (n // 11),
        "conn-open": "Server=x;Password=" + "a b " * (n // 4),
        "curl-run": "curl -u " * (n // 8),
        "line-long": "db_password = " + "v" * n,
        "line-many": "k.k.k = vvvvvv\n" * (n // 15),
        "line-spaces": "password" + " " * n + "= x",
        "token-space": "--token " * (n // 8),
        # the rules added after the security review
        "pgp-begin": "-----BEGIN PGP PRIVATE KEY BLOCK-----" * (n // 37),
        "escaped-quotes": '\\"' * (n // 2),
        "escaped-assign": 'a\\": \\"' * (n // 8),
        "escaped-open": 'password\\": \\"' + "b" * n,
        "backslash-run": "\\" * n,
        "header-run": "X-API-Key: " * (n // 11),
        "header-open": "x-api-key:" + " " * n + "a1",
        "authorization-run": "Authorization: Token " * (n // 21),
        "authorization-space": "authorization:" + " " * n,
        "azure-run": "AccountKey=" * (n // 11),
        "npmrc-run": ":_authToken=" * (n // 12),
        "npmrc-auth-run": ":_auth=" * (n // 7),
        "auth-json-run": '"auth": "' * (n // 9),
        "auth-json-open": '"auth": "' + "QUJD" * (n // 4),
        "slack-hook-run": "hooks.slack.com/services/" * (n // 25),
        "sg-run": "SG." * (n // 3),
        "telegram-digits": "123456789:AA" * (n // 12),
        "digit-colon": "1234567890:" * (n // 11),
        "vault-run": "hvs." * (n // 4),
        "whsec-run": "whsec_" * (n // 6),
        "xapp-run": "xapp-1-" * (n // 7),
        "discord-run": "discord.com/api/webhooks/1" * (n // 26),
        "query-run": "?sig=&appid=" * (n // 12),
        "query-open": "?api_key=" + "a1" * (n // 2),
        "empty-user-url": "redis://:" * (n // 9),
        "mysql-flags": "mysql " + "-pa " * (n // 4),
        # the rules added in round 6 (name-driven rules, vendor table, command table)
        "pem-4dash-lower-run": "----begin rsa private key----" * (n // 29),
        "b64-pem-run": "LS0tLS1CRUdJTi" * (n // 14),
        "b64-pem-open": "LS0tLS1CRUdJTi" + "QUJD" * (n // 4),
        "ppk-open": "Private-Lines: 1\n" + "A" * n,
        "ppk-long-lines": "Private-Lines: 1\n" + ("A" * 300 + "\n") * (n // 301),
        "vendor-prefixes": "gsk_ xai- lsv2_pt_ secret_ ntn_ r8_ sntrys_ glsa_ NRAK- PMAK- dp.st. ATATT3 ATBB ya29. figd_ "
                           "sq0atp- pplx- pscale_tkn_ fo1_ pul- pat CFPAT- rubygems_ oy2 aio_ bkua_ pnu_ rdme_ tskey-auth- "
                           "ops_eyJ sk.eyJ sl. fio-u- CLOJARS_ tfp_ v1.0- EZAK re_ HRKU- nfp_ rnd_ CCIPAT_ AKCp 1/1 EAA "
                           "sk_ sbp_ napi_ xkeysib- api_org_ pcsk_ " * (n // 330),
        "vendor-long": "gsk_" + "a1" * (n // 2),
        "hex-us": ("0" * 31 + "-us") * (n // 34),
        "asana-digits": "1/" + "1" * n,
        "atlasv1-run": ".atlasv1." * (n // 9),
        "command-runs": "redis-cli sshpass sqlcmd az keytool ldapsearch mongosh zip gh secret set htpasswd openssl "
                        "vault login smbclient sqlplus aws configure set git config " * (n // 140),
        "redis-flags": "redis-cli" + " -a" * (n // 3),
        "header-dash-run": "-token: " * (n // 8),
        "header-name-run": "a-" * (n // 2) + "token: abcdef123456789",
        "cookie-run": "Cookie: " * (n // 8),
        "cookie-pairs": "Cookie: " + "a=b1c2d3e4f5;" * (n // 13),
        "curl-cookie-run": "curl -b " * (n // 8),
        "query-name-run": "?a=" * (n // 3),
        "query-password-run": ";password=" * (n // 10),
        "query-open-name": "&" + "a" * n,
        "xml-open-run": "<a>" * (n // 3),
        "xml-password-run": "<password>" * (n // 10),
        "xml-attrs-open": "<a " + "b" * n,
        "xml-value-open": "<password>" + "x" * n,
        "pair-run": "name: x\nvalue: " * (n // 15),
        "pair-xml-run": 'key="password" value="' * (n // 22),
        "cred-call-run": "auth(" * (n // 5),
        "cred-call-open": 'login("a", "' + "b" * n,
        "securestring-run": 'ConvertTo-SecureString "' * (n // 24),
        "quoted-pair-run": '"password", "' * (n // 14),
        "hcl-variable-run": 'variable "password" {' * (n // 21),
        "hcl-variable-open": 'variable "x" {' + "a" * n,
        "yaml-block-run": "password: |\n" * (n // 12),
        "yaml-block-lines": "password: |\n" + "  x\n" * (n // 4),
        "yaml-block-long": "  password: |\n" + ("   " + "x" * 5000 + "\n") * (n // 5004),
        "toml-multiline-run": "= '''" * (n // 5),
        "toml-multiline-open": "x = '''" + "a" * n,
        "passphrase-run": ': "a b' * (n // 6),
        "passphrase-open": '= "' + "a " * (n // 2),
        "md-table-run": "| password | " * (n // 13),
        "pipe-run": "|" * n,
        "dockerfile-env-run": "ENV a " * (n // 6),
        "dockerfile-env-open": "ENV PASSWORD " + "x" * n,
        "dotted-space-run": "a.b " * (n // 4),
        "dotted-space-open": "a.b.c.d " + "x" * n,
        "name-auth-run": "_auth=" * (n // 6),
        "name-auth-open": "x_auth: " + "QUJD" * (n // 4),
        "conn-brace-run": "pwd={" * (n // 5),
        "conn-brace-open": "Server=a;pwd={" + "x" * n,
        # the structure passes (Redactor._json_strings, Redactor._line_view)
        "path-no-colon": "a/" * (n // 2),
        "path-then-colon": "a/" * (n // 2) + ":",
        "grep-context-run": "a-1-" * (n // 4),
        "grep-number-run": "a:1" * (n // 3),
        "marker-run": "+-#>!<;%*" * (n // 9),
        "number-run": "1 " * (n // 2),
        "number-tab-lines": "1\t\n" * (n // 3),
        "trailing-hash-run": " #" * (n // 2),
        "comment-lines": "# a\n" * (n // 4),
        "literal-quotes": '"a' * (n // 2),
        "literal-escaped-quotes": '"\\"' * (n // 3),
        "literal-open-escapes": '"' + "\\n" * (n // 2),
        "literal-backslash-quote": '\\"' * (n // 2),
        "literal-secret-words": '"password key token", ' * (n // 22),
        # round 7 rules
        "sql-password-run": "PASSWORD '" * (n // 10),
        "identified-run": "IDENTIFIED BY " * (n // 14),
        "spaced-name-run": "storePassword " * (n // 14),
        "setter-run": ".setPassword(" * (n // 13),
        "setter-open": 'setPassword("' + "a1" * (n // 2),
        "header-directive-run": "proxy_set_header " * (n // 17),
        "pgpass-run": "a:1:b:c:" * (n // 8),
        "pgpass-lines": "a:1:b:c:Ab12Cd34\n" * (n // 17),
        "office-webhook-run": ".webhook.office.com/webhookb2/" * (n // 30),
        "roblox-run": "_|WARNING:-DO-NOT-SHARE-THIS." * (n // 29),
        "echo-pipe-run": "echo a | " * (n // 9),
        "echo-open": "echo " + "a" * n,
        "option-run": "-password " * (n // 10),
        "option-names": "-" + "a" * n,
        "slash-option-run": "/pass:" * (n // 6),
        "user-colon-run": "-u a:" * (n // 5),
        "7z-run": "7z -p" * (n // 5),
        "kv-run": "consul kv put " * (n // 14),
        "kv-name-value": "--name a " * (n // 9) + "--value x",
        "password-line-run": "password " * (n // 9),
        "postfix-run": "[a]:1 " * (n // 6),
        "acl-run": "ACL SETUSER a on " * (n // 17),
        "dsn-run": "a:b@tcp(" * (n // 8),
        "prose-run": "the password is " * (n // 16),
        "prose-gap": "password" + " x" * (n // 2),
        "comma-options": "-o a=b," * (n // 7),
        # runs of JSON string values redacted as one text (_json_strings), and their mapping back (_split_items)
        "array-of-short-strings": '"a", ' * (n // 5),
        "array-of-empties": '"", ' * (n // 4),
        "values-and-keys": '"k": "v", ' * (n // 10),
        "array-of-pem-lines": '"-----BEGIN RSA PRIVATE KEY-----", "MIIEowIBAAKCAQEA", ' * (n // 52),
        "array-unclosed": '["a", "b' * (n // 8),
        # terminal escape sequences (stripped for the line view), a two-column table
        "ansi-csi-run": "\x1b[" * (n // 2),
        "ansi-osc-open": "\x1b]" + "a" * n,
        "ansi-colour-run": "\x1b[31mpassword=\x1b[m" * (n // 18),
        "ansi-lines": "\x1b[32m+DB_PASSWORD=x\x1b[m\n" * (n // 24),
        "table-run": "DB_PASSWORD  " * (n // 13),
        "table-lines": "DB_PASSWORD   Ab12Cd34\n" * (n // 23),
        # round 8 low misses: terraform's `->`, numbers in JSON, a percent-encoded bearer token, pipes between env pairs
        "arrow-run": "a = b -> " * (n // 9),
        "arrow-open": 'password = "x" -> "' + "a" * n,
        "json-number-run": '"password": ' * (n // 12),
        "json-number-long": '"password": ' + "1" * n,
        "bearer-pct-run": "Bearer " + "%3D" * (n // 3),
        "pipe-env-run": "A=1|" * (n // 4),
        # round 9: PuTTY key lines that must end their line, multi-line XML values, a prose gap with quotes
        "ppk-mac-after-full-lines": ("Private-Lines: 1\n" + ("A" * 256 + "\n") * 255 + "Private-MAC: x\n") * (n // 65000 + 1),
        "ppk-lines-then-dash": "Private-Lines: 1\n" + ("A" * 255 + "-\n") * (n // 257),
        "xml-ml-open": "<password>\n" + " x\n" * (n // 3),
        "xml-ml-run": "<password>\n x\n</password>\n" * (n // 26),
        "prose-gap-quotes": "password" + '" x' * (n // 3),
        "prose-gap-escapes": "password" + "\\n x" * (n // 4),
        # typed declarations (_TYPED_ASSIGN) and the other string forms
        "typed-words": "password string " * (n // 16),
        "typed-colon-run": "password: " * (n // 10),
        "typed-dotted": "password: " + "a." * (n // 2),
        "typed-generic-open": "password: Foo<" + "a," * (n // 2),
        "typed-union-run": "token: str " + "| str " * (n // 6),
        "typed-lifetime-run": "token: &'a " * (n // 11),
        "typed-bracket-open": "password: Optional[" + "a" * n,
        "backtick-open": "password = `" + "a" * n,
        "backtick-dollars": "password = `" + "$" * n,
        "verbatim-open": 'password = @"' + 'a""' * (n // 3),
        "triple-open": 'password = """' + "a" * n,
        "raw-hash-open": 'password = r#"' + "a" * n,
        "prefixed-run": 'password = r"' * (n // 13),
        "yaml-props-run": "password: &a " * (n // 13),
        "yaml-tags-run": "password: !!str " * (n // 16),
        # round 9 low misses: ODBC `}}`, header values, `define` escapes, tab and pipe rows
        "odbc-doubled-braces": "Server=db;PWD={" + "}}" * (n // 2),
        "odbc-open": "Server=db;PWD={" + "a}}b" * (n // 4),
        "x-api-key-brackets": "X-API-Key: " + "a[" * (n // 2),
        "define-escapes": "define('DB_PASSWORD', '" + "\\'" * (n // 2),
        "define-other-quotes": "define('DB_PASSWORD', '" + '"' * n,
        "tab-rows": "DB_PASSWORD\tAb12Cd34\n" * (n // 21),
        "pipe-rows": "password | Ab12Cd34\n" * (n // 20),
        "pipe-row-run": "password | " * (n // 11),
        # round 10: types versus values (_type_kept, _line_context), typed forms and printed structures
        "letters-values": "password: CorrectHorse\n" * (n // 23),
        "letters-values-in-code": ("interface A {\n" + "  password: CorrectHorse;\n" * 50 + "}\n") * (n // 1300 + 1),
        "type-generic-run": "token: " + "Option<" * (n // 7),
        "type-index-run": "token: " + "a[" * (n // 2),
        "type-digits-run": "password: " + "u8" * (n // 2),
        "long-line-context": ("password: CorrectHorse\n" + "x" * 5000 + "\n") * (n // 5023 + 1),
        "c-array-run": "password[" * (n // 9),
        "c-array-open": 'password[] = "' + "a" * n,
        "property-open": "Password { get; " * (n // 16),
        "placeholder-run": "${DB_PASSWORD:-" * (n // 15),
        "printed-field-run": "{Password:" * (n // 10),
        "printed-field-spaces": "{" + " Password:x" * (n // 11),
        "print-r-run": "[password] => " * (n // 14),
        "env-fallback-run": "process.env.API_KEY || " * (n // 23),
        "env-fallback-open": 'os.getenv("API_KEY", "' + "a" * n,
        "env-lookup-brackets": "process.env['" * (n // 13),
        "swift-raw-open": 'password = #"' + "a" * n,
        "go-var-block": "\tapiSecret string = " * (n // 20),
        # round 11: flow mapping items, list flags, values to the end of their token (fail closed), XML values whole
        "flow-open-run": "{db_password: " * (n // 14),
        "flow-commas": "{a: b" + ", db_password: x" * (n // 16),
        "flow-long-value": "{db_password: " + "a" * n,
        "flow-colons": "{db_password: " + "a:" * (n // 2),
        "flow-newlines": "{\n" * (n // 2),
        "list-flag-run": '"--password", ' * (n // 14),
        "list-flag-open": '["redis-cli", "-a", "' + "a" * n,
        "list-flag-block": "- --password\n" * (n // 13),
        "prose-long-token": "the password is " + "a1" * (n // 2),
        "prose-many-long": ("token: " + "Ab3" * 60 + "\n") * (n // 188),
        "yaml-long-value": "db_password: " + "Ab3" * (n // 3),
        "quoted-long-open": 'password = "' + "a1" * (n // 2),
        "xml-long-value": "<password>" + "a" * n,
        "xml-secret-open-run": "<password>" * (n // 10),
        "marker-tail": "[REDACTED:assignment]" + "a" * n,
        "markers-run": "[REDACTED:x]a" * (n // 13),
        "block-scalar-open": "password: |\n" + "  x\n" * (n // 4),
        "block-scalars-run": "password: |\n  x\n" * (n // 17),
        "block-blank-lines": "password: |\n" + "\n" * n,
        "block-blank-then-line": ("password: |\n" + "\n" * 200 + "  x\n") * (n // 216),
        "toml-open-run": "api_key = '''" * (n // 13),
        "block-indented-blank-runs": ("password: |\n" + "    \n" * 100 + "x\n") * (n // 500),
        "block-long-blank-line": "password: |\n" + " " * n + "\n",
        "cdata-open-run": "<password><![CDATA[" * (n // 19),
        "cdata-brackets": "<password><![CDATA[" + "a]]" * (n // 3),
        "header-value-run": "Authorization: Token " + "a!" * (n // 2),
    }


INPUTS = _inputs(N)
PATTERNS = [(kind, rx, repl) for kind, rx, repl in R._RULES] + list(getattr(R, "_ASSIGN_RULES", None) or [
    (kind, getattr(R, name), "") for kind, name in  # older module layout, so this test can time it too
    (("quoted", "_QUOTED_ASSIGN"), ("env", "_ENV_ASSIGN"), ("yaml", "_YAML_ASSIGN"))])


@pytest.mark.parametrize("name", sorted(INPUTS))
def test_every_pattern_is_fast_on_adversarial_input(name):
    s = INPUTS[name]
    for kind, rx, repl in PATTERNS:
        t = time.perf_counter()
        rx.sub(repl, s)
        dt = time.perf_counter() - t
        assert dt < 2.0, f"{kind} took {dt:.2f}s on {len(s)} chars of {name}"


@pytest.mark.parametrize("name", sorted(INPUTS))
def test_the_structure_passes_are_fast_on_adversarial_input(name):
    s = INPUTS[name]
    red = R.Redactor(None)
    for label, f in (("line view", lambda: red._line_view(s)), ("json strings", lambda: red._json_strings(s, 0))):
        dt = 1e9
        for _ in range(3):
            t = time.perf_counter()
            f()
            dt = min(dt, time.perf_counter() - t)
            if dt < 2.0:
                break
        assert dt < 2.0, f"{label} took {dt:.2f}s on {len(s)} chars of {name}"


def test_whole_redactor_on_a_megabyte_is_bounded():
    red = R.Redactor(None)
    s = INPUTS["svg-path"] * 10
    t = time.perf_counter()
    red.text(s)
    assert time.perf_counter() - t < 10.0


def test_capped_cost_does_not_depend_on_input_size():
    red = R.Redactor(None)
    big = INPUTS["hyphen-joined"] * 120  # 12 MB
    t = time.perf_counter()
    out, trunc = red.capped(big, 2000)
    assert time.perf_counter() - t < 1.0 and trunc and len(out) <= 2000


KEY = "sk-proj-" + "q1W2e3R4t5Y6u7I8" * 2


@pytest.mark.parametrize("offset", [0, 1, 5, 20, 39, 40])
@pytest.mark.parametrize("tail", [False, True])
def test_secret_straddling_the_cap_is_still_redacted(offset, tail):
    red = R.Redactor(None)
    limit = 100
    filler = "x " * 400
    if not tail:  # the key starts `offset` chars before the cap
        s = "y" * (limit - offset - 1) + " " + KEY + " " + filler
    else:        # the key ends `offset` chars after the start of the kept tail
        s = filler + " " + KEY + " " + "z" * (limit - offset - 1)
    out, trunc = red.capped(s, limit, margin=60, tail=tail)
    assert trunc and len(out) <= limit
    for k in range(6, len(KEY) + 1):  # no piece of the key of 6+ characters survives
        assert KEY[:k] not in out and KEY[-k:] not in out, out


def test_secret_split_by_the_pre_cut_leaves_no_fragment():
    # long secrets before the cap shrink to short tags and pull text from beyond the cap into the window; the
    # piece of a key that the pre-cut (limit + margin) split off, too short to be recognised, must not be in it
    red = R.Redactor(None)
    limit, margin = 200, 100
    head = "sk-proj-" + "A" * 100 + " "  # 109 chars, replaced by 22
    s = head * 2
    s = s + "B" * (limit + margin - len(s) - 13) + " " + KEY + " tail"  # the pre-cut keeps "sk-proj-q1W2"
    out, _ = red.capped(s, limit, margin=margin)
    assert "q1W2" not in out and "sk-proj-q" not in out, out


def test_capped_short_text_is_plain_redaction():
    red = R.Redactor(None)
    assert red.capped(f"key {KEY} end", 4000) == ("key [REDACTED:openai-key] end", False)
    off = R.Redactor(None, enabled=False)
    assert off.capped("a" * 50, 10) == ("a" * 10, True)
