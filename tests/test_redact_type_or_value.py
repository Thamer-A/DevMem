"""A type or a value (verifier v9b, items 1 and 3)? Round 9 kept a word on a `name: value` line as code when it looked
like a type (_type_field), and that kept everyday passwords: `Passw0rd!`, `Summer2025!`, `Welcome1?`, `Admin.Pass1`, any
`password: Winter560,` before a comma, in docker-compose, Kubernetes, Spring, Ansible, heroku output, PowerShell's
`Get-ChildItem env:` rows, YAML flow maps, log lines and sentences. A value is now a type only on evidence (type syntax,
a dotted chain of names without digits, a built-in type name, a class-like name; _type_kept), never in prose, and a
word of letters only under a password, secret, token or API-key name is a value when it looks like one and the lines
around it are no code (_letters_value, _line_context).

The other direction is held by the typed benign corpus (tests/test_redact_typed.py: 323 lines of typed code, none may
change) and by KEPT below."""
from __future__ import annotations

import random

import pytest

from devmem.redact import Redactor

# human-style passwords (made up, built from pieces)
PW = ["Passw0rd" + "!", "Summer" + "2025!", "Welcome" + "1?", "Admin." + "Pass1", "Winter" + "560", "Hunter2" + "pass"]

SHAPES = {
    "compose env map": "services:\n  db:\n    image: postgres:16\n    environment:\n      POSTGRES_PASSWORD: {v}\n"
                       "      POSTGRES_USER: app\n",
    "compose one line": "      MYSQL_ROOT_PASSWORD: {v}",
    "k8s stringData": "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db\nstringData:\n  password: {v}\n",
    "spring": "spring:\n  datasource:\n    url: jdbc:postgresql://db/app\n    password: {v}\n",
    "ansible": "ansible_become_password: {v}",
    "heroku config": "=== app Config Vars\nDATABASE_PASSWORD: {v}",
    "yaml comment": "password: {v}  # prod",
    "yaml list": "- db_password: {v}",
    "flow map": "db: {{password: {v}, user: admin}}",
    "flow map last": "credentials: {{username: admin, password: {v}}}",
    "log": "INFO connecting with user: admin, password: {v}, db: main",
    "comma": "  password: {v},",
    "semicolon": "  password: {v};",
    "prose comma": "the db password is {v}, can you connect?",
    "prose semicolon": "my password is {v}; please log in",
    "prose keep it": "The API key is {v}, keep it safe",
    "pwsh env row": "Name                           Value\n----                           -----\nDB_PASSWORD                    {v}",
    "tab row": "DB_PASSWORD\t{v}",
    "env colon": "DB_PASSWORD: {v}",
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("pw", PW)
def test_everyday_passwords_in_config_log_and_prose_shapes_are_redacted(shape, pw):
    text = SHAPES[shape].format(v=pw)
    out = Redactor(None, enabled=True).text(text)
    assert pw not in out, (shape, out)
    assert out.count("\n") == text.count("\n")


def test_a_comma_or_semicolon_after_the_value_stays():
    red = Redactor(None, enabled=True)
    assert red.text("  password: " + PW[4] + ",") == "  password: [REDACTED:assignment],"
    assert red.text("db: {password: " + PW[4] + ", user: admin}") == "db: {password: [REDACTED:assignment], user: admin}"


# item 3: a value of letters only under a password, secret, token or API-key name on a config-looking line
LETTERS = [
    "password: Correct" + "Horse",
    "password = Correct" + "Horse",
    "api_key: AbCdEfGh" + "IjKlMnOpQrSt",
    "  POSTGRES_PASSWORD: Galaxy" + "Silver",
    "services:\n  db:\n    environment:\n      POSTGRES_PASSWORD: post" + "gres\n      POSTGRES_USER: app",
    "[database]\nhost = db.internal\npassword = sun" + "shine\nport = 5432",
    "DB_PASSWORD    Correct" + "Horse",
    "the password is Correct" + "HorseBattery, use it",
    "the password is P@ss" + "word!",
    "the password is Qw3" + "rty!",
]


@pytest.mark.parametrize("text", LETTERS)
def test_a_value_of_letters_only_is_redacted_where_it_looks_like_one(text):
    out = Redactor(None, enabled=True).text(text)
    assert "[REDACTED:" in out, out
    for word in ("CorrectHorse", "AbCdEfGhIjKlMnOpQrSt", "GalaxySilver", "postgres\n", "sunshine", "P@ssword", "Qw3rty"):
        assert word not in out, (text, out)


# kept: settings, types and names, and the same words in code
KEPT = [
    "password: required",
    "password: true",
    "password: null",
    "password: string",
    "token: bearer",
    "secret: optional",
    "password: hidden",
    "secret_name: db-credentials",
    "password: Passwort",
    "  token: Token;",
    "  token: TokenStore,",
    "    secret: SecretString,",
    "    password: SecretStr",
    "  password: String!",
    "  token: Option<Vec<u8>>,",
    "  secret: [32]byte",
    "\tPassword *string",
    "\tPassword []byte",
    "  secretKey: Uint8Array;",
    "  privateKey: rsa.RSAPrivateKey,",
    "export interface Login {\n  password: CorrectHorse;\n  user: string;\n}",
    "@dataclass\nclass Config:\n    password: HorseStaple\n    user: str\n    port: int",
    "def connect():\n    password = CorrectHorse\n    return open_db(password)",
    "the token is JsonWebToken",
    "the password is required, see the docs",
    "the api key is Base64",
    "password = pw",
    "password = postgres",
]


@pytest.mark.parametrize("text", KEPT)
def test_settings_types_and_code_are_kept(text):
    assert Redactor(None, enabled=True).text(text) == text


WORDS = ["summer", "winter", "spring", "autumn", "welcome", "dragon", "monkey", "sunshine", "football", "shadow",
         "master", "letmein", "qwerty", "princess", "baseball", "hunter", "freedom", "charlie", "orange", "banana",
         "tiger", "falcon", "rocket", "galaxy", "matrix", "phoenix", "silver", "purple"]
ALNUM = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def _styles(rng: random.Random) -> dict[str, str]:
    w = rng.choice(WORDS)
    return {
        "word+digits": w.capitalize() + str(rng.randint(1, 9999)),
        "word+digits+symbol": w.capitalize() + str(rng.randint(1, 2025)) + rng.choice("!?.#$@*"),
        "CamelCase": "".join(rng.choice(WORDS).capitalize() for _ in range(rng.randint(2, 3))) +
                     rng.choice(["", str(rng.randint(1, 99))]),
        "random letters and digits": "".join(rng.choice(ALNUM) for _ in range(rng.randint(10, 20))),
    }


POSITIONS = ["  password: {v}", "  password: {v}  # prod", "  password: {v},", "  password: {v};", "- db_password: {v}",
             "the password is {v}", "the password is {v}, use it", "DB_PASSWORD\t{v}", "DB_PASSWORD    {v}",
             "password = {v}", "DB_PASSWORD={v}"]


@pytest.mark.parametrize("position", POSITIONS)
def test_generated_passwords_are_kept_at_most_rarely(position):
    """200 generated passwords per position (seeded: the same every run). Measured 2026-11-15: at most 4% kept per style
    (the CamelCase ones ending in a type word such as `...Secret`, and random letters whose case changes read as a
    camelCase name); before the fix up to 100% at `,`/`;` and in prose, and about half of the CamelCase ones everywhere."""
    red = Redactor(None, enabled=True)
    rng = random.Random("type-or-value:" + position)
    kept: dict[str, int] = {}
    for _ in range(200):
        for style, v in _styles(rng).items():
            if v in red.text(position.format(v=v)):
                kept[style] = kept.get(style, 0) + 1
    assert all(n <= 16 for n in kept.values()), kept       # 8% of 200: twice the measured worst


# one example per class that stays open after round 10 (documented as known gaps in the redact.py docstring and
# docs/PRIVACY.md): a fix of one of them updates the docs
KNOWN_GAPS = [
    "password: Winter" + "Secret",                 # CamelCase ending in a type word
    "password: Admin." + "Pass",                   # a dotted name without digits
    "password: Tig" + "3r",                        # fewer than six characters
    "password: Correct-" + "Horse",                # letters with a dash
    "key: Correct" + "HorseBattery",               # letters only under a name that is not a password/secret/token one
    "the password for the prod db, as agreed, is Correct" + "Horse",    # letters only after other words
    "pw = 'Xk4pQ9" + "Lm2zT8'",                    # a name of two letters
]


@pytest.mark.parametrize("text", KNOWN_GAPS)
def test_known_gaps_stay_documented(text):
    assert Redactor(None, enabled=True).text(text) == text


def test_a_value_that_goes_on_past_a_quote_is_taken_whole():
    """A sentence-like match cut at a quote inside a value (`password: )diZL@W?"RkJl...`) left the part after the quote
    when the prose rule redacted the part before it; the line rules take such a value whole (verifier v9b g1: printable
    passwords partly kept 70 -> 58 of 900)."""
    red = Redactor(None, enabled=True)
    for v in (')diZL@W?"RkJl' + "S-RS1N!", "^U#ZNIx*;.'WKd" + "Z^N*zm", 'S}B@cTh"}=N8E' + "Z%W1{<z"):
        for text in ("db:\n  password: " + v + "\n  host: db", "X-API-Key: " + v):
            out = red.text(text)
            assert not any(v[i:i + 8] in out for i in range(len(v) - 7)), (text, out)
    assert red.text('{"msg": "the password is Winter' + '2024"}') == '{"msg": "the password is [REDACTED:assignment]"}'
