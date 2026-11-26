"""The grid the documentation's "redacted whole" sentences stand on: for each context below, a value made of letters,
digits and one separator character, at each length, is redacted with nothing of it left (no 8 characters of it, checked
every 4 characters along the value). docs/PRIVACY.md and the redact.py docstring may claim this grid and nothing wider; values outside it
(other characters, other forms, longer values) are best effort. All values are made up."""
from __future__ import annotations

import json

import pytest

from devmem.redact import Redactor

LENGTHS = [20, 300, 3000]
SEPARATORS = list("!#$%*()+,-./:=?@[]^_{|}~")


def _quoted(v: str) -> str:
    return json.dumps(v)


CONTEXTS = {
    "yaml line": (lambda v: f"db_password: {v}\n", ":#{}[],&*!|>'\"%@`"),
    "env line": (lambda v: f"DB_PASSWORD={v}\n", "#"),
    "ini line": (lambda v: f"[db]\npassword = {v}\n", "#;"),
    "properties": (lambda v: f"spring.datasource.password={v}\n", "#"),
    "json quoted": (lambda v: '{"password": %s}\n' % _quoted(v), ""),
    "json name/value": (lambda v: '{"name": "DB_PASSWORD", "value": %s}\n' % _quoted(v), ""),
    "ssm name/value": (lambda v: '{"Name": "/p/DB_PASSWORD", "Type": "SecureString", "Value": %s}\n' % _quoted(v), ""),
    "k8s name/value": (lambda v: f"- name: DB_PASSWORD\n  value: {v}\n", "#"),
    "xml element": (lambda v: f"<password>{v}</password>\n", "<>&"),
    "xml add": (lambda v: f'<add key="DbPassword" value="{v}"/>\n', "<>&\""),
    "flow map": (lambda v: f"environment: {{POSTGRES_USER: app, POSTGRES_PASSWORD: {v}}}\n", ",{}[]#:&*!|>'\"%@`"),
    "authorization token": (lambda v: f"Authorization: Token {v}\n", ","),
    "x-api-key": (lambda v: f"X-API-Key: {v}\n", ""),
    "block scalar": (lambda v: f"password: |\n  {v}\nuser: a\n", ""),
}


def _value(n: int, sep: str) -> str:
    unit = "Xk4mL" + sep + "p9QrT2"
    return (unit * (n // len(unit) + 1))[:n - 1] + "9"


@pytest.mark.parametrize("ctx", sorted(CONTEXTS))
def test_the_value_is_redacted_whole_in_the_documented_grid(ctx):
    make, excluded = CONTEXTS[ctx]
    red = Redactor(None)
    left = []
    for n in LENGTHS:
        if ctx.startswith("xml add") and n > 3000:
            continue
        for sep in SEPARATORS:
            if sep in excluded:
                continue
            v = _value(n, sep)
            out = red.text(make(v))
            if any(v[k:k + 8] in out for k in range(0, len(v) - 7, 4)):   # (no 8 characters of it anywhere)
                left.append((n, sep))
    assert not left, left
