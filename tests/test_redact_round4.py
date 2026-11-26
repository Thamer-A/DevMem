"""Survivors from the third security review's redaction lists (verify3/redact/t3_correct.py; all values made up).

Caught now: `docker login -p`/`--password`, Docker Hub `dckr_pat_` tokens, subscript assignments
(`os.environ["X_API_KEY"] = "..."`), Discord bot tokens, Firebase FCM server keys, Linear `lin_api_` keys, age
secret keys, netrc passwords, `*_PAT=` names and the key part of a Sentry-style DSN. No longer redacted: Stripe
publishable keys (`pk_test_`/`pk_live_`), which are public by design. JSON escaped inside JSON is caught to eight
levels (255 backslashes before a quote); deeper is a known gap (R7: the old comment said six, a test said eight).
"""
from __future__ import annotations

import json

import pytest

from devmem.redact import Redactor

B62 = "Xk9fAkeQz7Lm2Np4Rs6Tv8Wx0Yb3Cd5Ef1Gh"
H = "0123456789abcdef"
CAUGHT = {
    "docker login -p": ("docker login -u me -p S3cr3tPassw0rd registry.example.com", "S3cr3tPassw0rd"),
    "docker login --password": ("docker login --username me --password S3cr3tPassw0rd reg", "S3cr3tPassw0rd"),
    "docker login --password=": ("podman login --password=S3cr3tPassw0rd quay.io", "S3cr3tPassw0rd"),
    "docker hub pat": ("echo dckr_pat_" + B62[:27] + " | docker login -u me --password-stdin", "dckr_pat_"),
    "os.environ subscript": ('os.environ["OPENAI_API_KEY"] = "' + B62 + '"', B62),
    "process.env subscript": ("process.env['X_TOKEN'] = '" + B62 + "';", B62),
    "config subscript": ('config[\'password\'] = "Pa55wordValue"', "Pa55wordValue"),
    "discord bot token": ("client.login('MTE0NTY3ODkwMTIzNDU2Nzg5.GaBcDe.Xk9fAkeQz7Lm2Np4Rs6Tv8Wx0Yb3Cd5Ef1')",
                          "MTE0NTY3"),
    "fcm server key": ("serverKey: 'AAAA" + B62 + ":APA91b" + B62 + "'", "APA91b"),
    "linear key": ("Authorization header value lin_api_" + B62, "lin_api_"),
    "age secret key": ("AGE-SECRET-KEY-1QYQSZQGPQYQSZQGPQYQSZQGPQYQSZQGPQYQSZQGPQYQSZQGPQYQS", "QYQSZQGPQYQSZ"),
    "netrc one line": ("machine api.example.com login me password N3trcPassw0rd", "N3trcPassw0rd"),
    "netrc lines": ("machine api.example.com\n  login me\n  password N3trcPassw0rd\n", "N3trcPassw0rd"),
    "azure devops pat": ("AZURE_DEVOPS_PAT=" + "a" * 20 + "1" * 32, "aaaaaaaaaaaaaaaaaaaa1111"),
    "github pat name": ("GITHUB_PAT=Ab12Cd34Ef56Gh78", "Ab12Cd34Ef56Gh78"),
    "sentry dsn": ("SENTRY_DSN=https://" + H * 2 + "@o123.ingest.sentry.io/456", H * 2),
    "mysql -p": ("mysql -u root -pMyS3cretPw app", "MyS3cretPw"),
    "mysql --password=": ("mysql --password=MyS3cretPw app", "MyS3cretPw"),
    "PGPASSWORD": ("PGPASSWORD=PgPassw0rd99 psql -h db", "PgPassw0rd99"),
    "vault secret_id": ("VAULT_SECRET_ID=4f8e2a1c-9b7d-4e3f-a2c1-0d9e8f7a6b5c", "4f8e2a1c-9b7d"),
}
KEPT = [
    "pk_test_ placeholder: pk_test_TYooMQauvdEDq54NiTphI7jx",
    "const stripe = Stripe('pk_live_51H" + B62 + "');",
    "docker login -u me --password-stdin registry.example.com",
    "docker login -u me -p $DOCKER_PASSWORD reg",
    'os.environ["API_KEY"]',
    'if config["password"] == "x":',
    'headers["Accept"] = "application/json"',
    'os.environ["PATH_PREFIX"] = "/usr/local/bin"',
    "the machine learning password manager",
    "MyVeryLongClassNameHere12.method.someVeryLongPropertyNameHere12345",
    "PAT=Ab12Cd34Ef56Gh78",                  # a bare PAT without a qualifier is too generic
    "pattern_PAT_COUNT=Ab12",
    "https://user@example.com/x",
]


@pytest.mark.parametrize("name", sorted(CAUGHT))
def test_caught(name):
    text, secret = CAUGHT[name]
    out = Redactor(None, enabled=True).text(text)
    assert secret not in out, out


@pytest.mark.parametrize("text", KEPT)
def test_kept(text):
    assert Redactor(None, enabled=True).text(text) == text


@pytest.mark.parametrize("levels", range(1, 9))
def test_json_in_json_up_to_eight_levels(levels):
    s = '{"password": "Xk9fakeP4ssw0rdQz", "api_key": "Zq8fakeK3yValue77"}'
    for _ in range(levels):
        s = json.dumps(s)
    out = Redactor(None, enabled=True).text(s)
    assert "Xk9fakeP4ssw0rdQz" not in out and "Zq8fakeK3yValue77" not in out
