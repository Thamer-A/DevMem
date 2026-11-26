"""Coloured output and two-column name/value tables (verifier v7b: e3_channels part d, p4_more).

`git -c color.ui=always diff`, `grep --color=always`, `jq -C`, `yq -C`, bat and delta put terminal escape sequences
around and inside the lines, so no rule matched them (5 and 6 values stored end to end). PowerShell's
`Get-ChildItem env:` prints a `Name  Value` table (`DB_PASSWORD   <value>`), which no rule read.
"""
from __future__ import annotations

import pytest

from devmem.redact import Redactor

PW = "Xk4pQ9Lm2zTw"
ESC = "\x1b"


@pytest.mark.parametrize("text", [
    f"{ESC}[1mdiff --git a/.env.example b/.env.example{ESC}[m\n{ESC}[32m+DB_PASSWORD={PW}{ESC}[m\n",
    f"{ESC}[32m+{ESC}[m{ESC}[32m  password: {PW}{ESC}[m",
    f"{ESC}[35mconfig/app.yml{ESC}[m{ESC}[36m:{ESC}[m{ESC}[32m3{ESC}[m{ESC}[36m:{ESC}[m  "
    f"{ESC}[01;31m{ESC}[Kpassword{ESC}[m{ESC}[K: {PW}",
    f"DB_PASSWORD={ESC}[01;31m{ESC}[K{PW}{ESC}[m{ESC}[K",
    f'{ESC}[1;39m{{\n  {ESC}[0m{ESC}[34;1m"password"{ESC}[0m{ESC}[1;39m:{ESC}[0;32m"{PW}"{ESC}[0m\n{ESC}[1;39m}}{ESC}[0m',
    f"{ESC}[34mpassword{ESC}[0m: {ESC}[32m{PW}{ESC}[0m",
    f"{ESC}]8;;file:///p/.env{ESC}\\link{ESC}]8;;{ESC}\\ API_KEY={PW}",
])
def test_coloured_output(text):
    out = Redactor(None).text(text)
    assert PW not in out, repr(out)


def test_coloured_lines_that_hold_no_secret_keep_their_colours():
    text = f"{ESC}[32m+PASSWORD_RESET_TIMEOUT = 3600{ESC}[m\n{ESC}[31m-x = 1{ESC}[m"
    assert Redactor(None).text(text) == text


TABLE = f"""
Name                           Value
----                           -----
ALLUSERSPROFILE                C:\\ProgramData
DB_PASSWORD                    {PW}
API_KEY                        sk9Lm2QzXw7Pq4Tz
GITHUB_TOKEN                   gh0Lm2QzXw7Pq4Tz9
PATH                           C:\\Windows\\system32;C:\\Windows
PASSWORD_MIN_LENGTH            12
"""


def test_powershell_environment_table():
    out = Redactor(None).text(TABLE)
    for v in (PW, "sk9Lm2QzXw7Pq4Tz", "gh0Lm2QzXw7Pq4Tz9"):
        assert v not in out
    assert "C:\\ProgramData" in out and "C:\\Windows\\system32" in out and "PASSWORD_MIN_LENGTH            12" in out


@pytest.mark.parametrize("text", [
    "password_reset.py  1.2KB",
    "password_reset.py    4096",
    "secret              Opaque              2      5d",
    "token_count         12345678",
    "api_key_header      X-API-Key",
    "secret_key          The Django secret key",
    "SECRET_KEY          ${SECRET_KEY}",
])
def test_tables_that_hold_no_secret_are_kept(text):
    assert Redactor(None).text(text) == text
