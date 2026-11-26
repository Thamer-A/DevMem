"""Low-severity misses the verifier listed (v7b p3_shapes, p4_more, notes) that generalise cheaply; the rest are known
gaps in the redact.py docstring and docs/PRIVACY.md (CSV doubled quotes, HTML entities, zero-width and no-break spaces,
Python repr with a literal \\n before `-p`, a mongodb+srv password with a raw `@`)."""
from __future__ import annotations

import json

import pytest

from devmem.redact import Redactor

KEY_LETTERS = "QwErTyUiOpAsDfGhJkLzXcVbNmQwErTy"
TAIL = "EUifiRBkKG5E2XzMDj" + "Rfl76ZC9Ub0wnz4XsNiRVBChTYbJcE3F"     # made up, built from pieces


@pytest.mark.parametrize("text, secret", [
    (f"X-API-Key: {KEY_LETTERS}", KEY_LETTERS),
    (f"curl -H 'x-api-key: {KEY_LETTERS}' https://api.example.test", KEY_LETTERS),
    ("Authorization: Bearer " + "A" * 21 + "MLhe" + "A" * 7 + "0%2BuSeid%2BULvsea4JtiGRiSDSJSI%3D" + TAIL, TAIL),
    ('{"password": 84736251}', "84736251"),
    ('{\n  "db": {"db_password": 120934,\n  "user": "app"}\n}', "120934"),
    ("X=1|DB_PASSWORD=Xk4pQ9#Lm2zT|Y=2", "Lm2zT"),
    ('      ~ master_password = "old-Pw-1234" -> "Xk4pQ9#Lm2zT"', "Lm2zT"),
    ("      ~ master_password = (sensitive value) -> \"Xk4pQ9#Lm2zT\"", "Lm2zT"),
])
def test_low_misses_are_caught(text, secret):
    assert secret not in Redactor(None).text(text)


def test_a_number_under_a_password_key_in_a_structure():
    red = Redactor(None)
    out = red.tree({"password": 84736251, "port": 5432, "retries": 3, "password_length": 12345678}, 10_000)[0]
    assert out["password"] == "[REDACTED:assignment]" and out["port"] == 5432 and out["password_length"] == 12345678
    assert "84736251" not in json.dumps(red.record({"db": {"password": 84736251}}))


@pytest.mark.parametrize("text", [
    "X-API-Key: your-api-key-here",
    "X-API-Key: ExampleApiKeyGoesHereForDocs",
    '{"port": 5432123, "max_tokens": 4096000}',
    '{"token_count": 1234567}',
    "count = a -> b",
    'name = "a" -> "b"',
    "A=1|B=2|C=3",
])
def test_low_miss_look_alikes_are_kept(text):
    assert Redactor(None).text(text) == text
