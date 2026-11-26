"""Low misses of verifier round v8b that generalise (item 5): a generated value that only starts like a path, ODBC
`}}` inside braces, API-key header values with brackets, PHP `define` with escaped quotes, tab-separated and
`name | value` rows, and two look-alikes (a value equal to its name, Maven `@...@`). The misses that stay open are
listed as known gaps in the redact.py docstring and docs/PRIVACY.md; KNOWN_GAPS pins one example of each, so that a fix
updates the docs."""
from __future__ import annotations

import pytest

from devmem.redact import Redactor

V = "Xk4" + "pQ9" + "Lm2zT8"

CAUGHT = [
    f"DB_PASSWORD=.{V}",
    f'{{"password": "~{V}"}}',
    f'password = "/{V}"',
    f"password: ~{V}",
    "Driver={ODBC Driver 18};Server=db;UID=sa;PWD={ab}}" + V + "};",
    f"X-API-Key: WBXrAL4mnt+q[e){V}",
    f"curl -H 'X-API-Key: {V}[x]' https://api.example.invalid",
    f"define('DB_PASSWORD', 'ab\\'{V}');",
    f"define('DB_PASSWORD', 'ab\"{V}');",
    f"DB_PASSWORD\t{V}",
    f"password | {V}",
    f"-[ RECORD 1 ]-------\nusername | admin\npassword | {V}",
]

KEPT = [
    "PASSWORD_FILE=/run/secrets/db",
    "password: /run/secrets/db",
    "DB_PASSWORD=~/.pgpass",
    "secret: .env",
    "password = './secrets/db.txt'",
    "SSL_KEY=/etc/ssl/private/server.key",
    "PASSWORD = 'password'   # field name",
    "password=@db.password@",
    "\tToken\tstring",
    "\tSecret\t[]byte",
    "password | required",
    "token | string",
    "cat secret.txt | base64",
    "echo $PASSWORD | docker login --password-stdin",
]

# one example per class that stays open (documented as known gaps)
KNOWN_GAPS = [
    "db:\n  password: |\n    {" + V + "}",                         # a block scalar value starting with `{`
    "jdbc:postgresql://db/app?user=u&password=%7B" + V,               # a percent-encoded `{` value
    f"docker run -e POSTGRES_PASSWORD='ab'\"'\"'{V}' postgres",       # shlex.quote's '"'"' inside a value
    f"DB_PASSWORD=$ecret{V}",                                         # a value that starts like a reference
    f"postgres://user:ab\"{V}@db/app",                                # a URL password holding a quote
    f"NAME          PASSWORD\napp-db        {V}",                     # a column named in the header row
    f"username |   password\n----------+--------------\n admin    | {V}",   # psql rows
]


@pytest.mark.parametrize("text", CAUGHT)
def test_caught(text):
    assert V not in Redactor(None, enabled=True).text(text)


@pytest.mark.parametrize("text", KEPT)
def test_kept(text):
    assert Redactor(None, enabled=True).text(text) == text


@pytest.mark.parametrize("text", KNOWN_GAPS)
def test_known_gaps_stay_documented(text):
    assert V in Redactor(None, enabled=True).text(text)
