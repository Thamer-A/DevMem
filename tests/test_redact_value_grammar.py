"""Values that hold `( ) [ ] { }`, a backtick, a quote or a backslash, in everyday formats (verifier v7b: p6_symbols,
p7_charclass, p8_django, p9_connsym, p11_jsonvalid).

The value rules rejected such values: a whole-line `name = value` with a bracket or a quote was taken for code, a dotenv
value followed by one of them was kept whole, a quoted value ended at either quote character (so `"Xk4'Lm2"` was never
redacted, and `"Xk4\\"Lm2"` lost only its head and left a JSON document that no longer parsed). Now:
  - a quoted value ends at the matching quote of its own kind, with backslash escapes (and `''` / the shell's `'\\''`
    in single quotes); the replacement leaves a document that parses where it parsed before;
  - an unquoted value on a config line runs to the end of the line (minus a ` #` / ` ;` comment) whatever it holds;
    code and references on such a line are recognised by their shape (calls, indexes, attribute chains, `${VAR}`,
    `%(var)s`, `{{ var }}`, YAML tags) and kept;
  - key=value sequences end where their format ends a value (`;` in a connection string, `&` in a query or form body).
"""
from __future__ import annotations

import json
import random
import string

import pytest

from devmem.redact import Redactor

SYM = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
FULL = string.ascii_letters + string.digits + SYM


def _passwords(n: int, alpha: str = FULL, seed: int = 7, size: int = 20) -> list[str]:
    rng = random.Random(seed)
    out = []
    while len(out) < n:
        p = "".join(rng.choice(alpha) for _ in range(size))
        if any(c.isdigit() for c in p) and any(c.isalpha() for c in p):
            out.append(p)
    return out


def _survives(p: str, out: str) -> bool:
    """A run of 4+ characters of the password (not only digits) is still in the output."""
    return any(p[i:i + 4] in out and not p[i:i + 4].isdigit() for i in range(len(p) - 3))


def _sq_shell(p: str) -> str:
    return "'" + p.replace("'", "'\\''") + "'"


QUOTED = {
    "json": lambda p: json.dumps({"db": {"password": p}}),
    "json-pretty": lambda p: json.dumps({"password": p, "user": "app"}, indent=2),
    "yaml-dq": lambda p: "db:\n  password: " + json.dumps(p) + "\n  user: app",
    "yaml-sq": lambda p: "db:\n  password: '" + p.replace("'", "''") + "'\n  user: app",
    "toml-basic": lambda p: "[db]\npassword = " + json.dumps(p) + "\nuser = \"app\"",
    "python": lambda p: "PASSWORD = " + repr(p),
    "js": lambda p: "const password = " + json.dumps(p) + ";",
    "dotenv-dq": lambda p: 'DB_PASSWORD="' + p.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
                           .replace("`", "\\`") + '"',
    "dotenv-sq": lambda p: "DB_PASSWORD=" + _sq_shell(p),
    "export-sq": lambda p: "export PGPASSWORD=" + _sq_shell(p),
}
UNQUOTED = {
    "ini": lambda p: "[db]\npassword = " + p + "\nuser = app",
    "props": lambda p: "spring.datasource.password=" + p.replace("\\", "\\\\") + "\nspring.datasource.user=app",
    "dotenv": lambda p: "DB_PASSWORD=" + p + "\nDB_USER=app",
    "dotenv-lower": lambda p: "db_password=" + p + "\ndb_user=app",
    "yaml": lambda p: "db:\n  secret_key: " + p + "\n  user: app",
    "compose": lambda p: "    environment:\n      - SECRET_KEY=" + p + "\n      - DEBUG=0",
    "printenv": lambda p: "HOME=/root\nSECRET_KEY=" + p + "\nPATH=/usr/bin",
}


def _unquoted_ok(p: str) -> bool:
    """A value that can be written unquoted on a config line: no blank inside, not starting with a quote (it would be a
    quoted value) or a comment/YAML indicator."""
    return not p[0] in "\"'#!&*|>%@[{`$" and " #" not in p and " ;" not in p


@pytest.mark.parametrize("fmt", sorted(QUOTED))
def test_quoted_values_with_any_printable_character(fmt):
    red = Redactor(None)
    kept = []
    for p in _passwords(150):
        t = QUOTED[fmt](p)
        out = red.text(t)
        if _survives(p, out):
            kept.append((t, out))
    assert not kept, kept[:3]


@pytest.mark.parametrize("fmt", sorted(UNQUOTED))
def test_unquoted_values_on_config_lines_with_any_printable_character(fmt):
    red = Redactor(None)
    kept = []
    for p in [p for p in _passwords(300) if _unquoted_ok(p)][:150]:
        t = UNQUOTED[fmt](p)
        out = red.text(t)
        if _survives(p, out):
            kept.append((t, out))
        assert "app" in out or "0" in out or "/usr/bin" in out, out      # the next line is kept
    assert not kept, kept[:3]


@pytest.mark.parametrize("fmt", ["dotenv", "compose", "yaml", "printenv"])
def test_django_secret_keys(fmt):
    red = Redactor(None)
    alpha = "abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*(-_=+)"
    kept = [p for p in _passwords(100, alpha, seed=5, size=50) if _unquoted_ok(p)
            and (p[:12] in red.text(UNQUOTED[fmt](p)) or p[-12:] in red.text(UNQUOTED[fmt](p)))]
    assert not kept, kept[:3]


def test_django_secret_keys_inline():
    red = Redactor(None)
    alpha = "abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*(-_=+)"
    keys = _passwords(500, alpha, seed=9, size=50)
    kept = [k for k in keys if k[:12] in red.text(f"docker run -e SECRET_KEY={k} img")
            or k[-12:] in red.text(f"docker run -e SECRET_KEY={k} img")]
    assert len(kept) <= 5, kept[:5]        # a value shaped like a call (`abc(...`) is taken for code: about 0.4%
    assert red.text("see API_KEY=#configuration in the docs") == "see API_KEY=#configuration in the docs"


def test_redacted_json_and_toml_still_parse():
    red = Redactor(None)
    broken = []
    for p in _passwords(300, seed=11):
        for doc in (json.dumps({"db": {"password": p, "user": "app"}}, indent=2),
                    json.dumps({"password": p}), json.dumps({"cfg": json.dumps({"password": p})})):
            out = red.text(doc)
            try:
                json.loads(out)
            except ValueError:
                broken.append(out)
            assert p not in out
    assert not broken, broken[:3]
    tomllib = pytest.importorskip("tomllib")
    for p in _passwords(150, seed=12):
        doc = "[db]\npassword = " + json.dumps(p) + "\nuser = 'app'\n"
        out = red.text(doc)
        assert tomllib.loads(out)["db"]["user"] == "app", out


@pytest.mark.parametrize("tpl", [
    "Server=tcp:sql1.example.net,1433;Initial Catalog=app;User ID=app;Password={};Encrypt=True;",
    "Host=db;Port=5432;Database=app;Username=app;Password={};SSL Mode=Require",
    "jdbc:sqlserver://db:1433;databaseName=app;user=app;password={};encrypt=true",
])
def test_connection_string_passwords_with_symbols(tpl):
    red = Redactor(None)
    for c in "!\"#$%&'()*+,-./:<=>?@[\\]^_`{|}~":
        p = "Ab3xY9" + c + "kLm2Qz"
        out = red.text(tpl.format(p))
        assert "Ab3xY9" not in out and "kLm2Qz" not in out, out
        assert "Encrypt=True" in out or "SSL Mode" in out or "encrypt=true" in out, out
        doc = json.dumps({"ConnectionStrings": {"Default": tpl.format(p)}})
        out = red.text(doc)
        assert "Ab3xY9" not in out and "kLm2Qz" not in out, out
        json.loads(out)


@pytest.mark.parametrize("text, secret", [
    ("username=app&password=Xk4pQ9#Lm2zT&remember=1", "Lm2zT"),
    ("curl -d 'user=app&password=Xk4pQ9#Lm2zT' https://example.test/login", "Lm2zT"),
    ("POST /login\n\nuser=app&password=Xk4(pQ9)Lm2zT", "Lm2zT"),
])
def test_form_bodies_keep_no_tail(text, secret):
    out = Redactor(None).text(text)
    assert secret not in out and "Xk4" not in out, out


# code and references on a config-looking line: kept
CODE = [
    "password = get_password()",
    "password = getpass.getpass()",
    "token = cfg[\"token\"]",
    "token = cfg['token']",
    "key = os.environ[\"K\"]",
    "SECRET_KEY = os.environ['SECRET_KEY']",
    "SECRET_KEY = os.getenv(\"SECRET_KEY\")",
    "SECRET_KEY = env(\"SECRET_KEY\")",
    "PASSWORD = config('PASSWORD',default='')",
    "password = request.POST['password']",
    "api_key = settings.API_KEY;",
    "secret = sys.argv[1]",
    "password = getPassword();",
    "password = self.password",
    "password = None",
    "token = tokens[0]",
    "secret: ${{ secrets.DEPLOY_KEY }}",
    "password: !vault |",
    "password: ${DB_PASSWORD}",
    "password = %(db_password)s",
    "password = {{ db_password }}",
    "password={{db_password}}",
    "DB_PASSWORD=${DB_PASSWORD:-}",
    "DB_PASSWORD=$DB_PASSWORD",
    "PASSWORD=$(cat /run/secrets/db)",
    "password = [\"a\", \"b\"]",
    "secret = {\"a\": 1}",
    "secrets = {}",
    "password = (",
    "password = Password(",
    "API_KEY=os.getenv(\"API_KEY\")",
    "token = f\"{prefix}{suffix}\"",
    "password = <%= ENV['DB_PASSWORD'] %>",
    "password = #{db_password}",
    "secret_key: {}",
    "secret_key: []",
    "  password: \"{{ vault_db_password }}\"",
    "password = getattr(settings,'DB_PASSWORD',None)",
    "token = response.json()['access_token']",
    "secret = this.config.get('secret')",
    '_non_token_end_matcher = re.compile(r"[^{}]+".format(',
    'HEADER_TOKEN_RE = re.compile(r"^\\s*([^=\\s;,]+)")',
    "a `;password=getpw()`: code, not a URL",
]


@pytest.mark.parametrize("line", [
    "[db]\npassword = uGi$(aot+J\"\"7\\py(}h(",
    "spring.datasource.password=VERG(};N`\\\\6U8+{|BDd:",
    "password = getX(k3j)9!pQ2",
])
def test_generated_values_that_start_like_a_call_are_redacted(line):
    out = Redactor(None).text(line)
    assert "[REDACTED" in out and line.split("=", 1)[1].strip()[-6:] not in out, out


@pytest.mark.parametrize("line", CODE)
def test_code_and_references_are_kept(line):
    assert Redactor(None).text(line) == line
