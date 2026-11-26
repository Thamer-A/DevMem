"""An env-style assignment (`NAME=value`) that sits inside a string: the value ends at the quote that closes it.

Found by the redaction fuzzer (h6b_env_contexts.py): `OPENWEATHER_API_KEY=<hex>` was redacted bare, in prose, after
`export`, dotenv-quoted and when a space followed, but kept whole when a closing quote came straight after the value
(`Client("API_KEY=...")`, `{"value": "API_KEY=..."}`, `curl -H "API_KEY=..."`, a JSON body in a shell argument, and
the same escaped inside a JSON string). All values are made up.
"""
from __future__ import annotations

import json

import pytest

from devmem.redact import Redactor

V = "d4b29a866c667b8e7330d199b4485fc8"
S = "OPENWEATHER_API_KEY=" + V

CAUGHT = {
    "python call": 'client = Client("' + S + '")',
    "json value": '{"value": "' + S + '"}',
    "curl header": 'curl -H "' + S + '" https://api.example.com',
    "shell json body": "curl -sS https://api.example.com/v1 -d '{\"k\": \"" + S + "\"}'",
    "single quotes": "x = '" + S + "'",
    "single quotes then comma": "args = ['" + S + "', 'b']",
    "brace": "${" + S + "}",
    "password double quoted": 'env("DB_PASSWORD=Pa55wordValue9")',
    "token in a list": '["GITHUB_TOKEN=Ab12Cd34Ef56Gh78Ij90"]',
    "sentence punctuation then quote": '"' + S + '."',
}


@pytest.mark.parametrize("name", sorted(CAUGHT))
def test_value_before_a_closing_quote_is_redacted(name):
    r = Redactor(None, enabled=True)
    text = CAUGHT[name]
    out = r.text(text)
    for secret in (V, "Pa55wordValue9", "Ab12Cd34Ef56Gh78Ij90"):
        assert secret not in out, out
    assert "[REDACTED:assignment]" in out
    assert r.text(out) == out           # idempotent


@pytest.mark.parametrize("levels", range(1, 9))
def test_value_inside_json_in_json_is_redacted(levels):
    s = json.dumps({"v": S, "w": "client.run('" + S + "')"})
    for _ in range(levels - 1):
        s = json.dumps(s)
    r = Redactor(None, enabled=True)
    out = r.text(s)
    assert V not in out, out
    assert r.text(out) == out
    # the escaping around the value is kept, so the text still decodes to the same shape
    for _ in range(levels):
        out = json.loads(out)
    assert out["v"] == "OPENWEATHER_API_KEY=[REDACTED:assignment]"


KEPT = [
    'API_KEY=os.getenv("API_KEY")',              # an expression
    'TOKEN=os.environ["TOKEN"]',
    '"MAX_TOKENS=4096"',                         # a count, not a secret
    '"API_KEY=your_api_key_here"',               # placeholders
    '"API_KEY=YOUR_API_KEY"',
    "'SECRET_KEY=$SECRET_KEY'",
    '"SECRET_KEY=${SECRET_KEY}"',
    '"PASSWORD=" + password',
    '"SORT_KEY=created_at_2024"',                # not a secret name
    '"CACHE_KEY=user_profile_v2"',
    '"COMMIT_SHA=0123456789abcdef0123456789abcdef01234567"',
    '"BUILD_ID=6f1c2b3a-9d8e-4f7a-b6c5-d4e3f2a1b0c9"',
    '"TOKEN_VERSION=1.2.3"',
    '"PASSWORD_FILE=/run/secrets/db"',           # a path
    '"AUTH_TOKEN_URL=https://example.com/token"',
    '"STRIPE_KEY=short"',                        # FOO_KEY needs a generated-looking value
    '"TOKEN=changeme"',
    'print("ACCESS_TOKEN=%s" % token)',
    'f"{API_KEY=}"',
]


@pytest.mark.parametrize("text", KEPT)
def test_ordinary_text_with_a_quoted_assignment_stays(text):
    assert Redactor(None, enabled=True).text(text) == text


# The coordinator's second rerun of the old reproducers, and the same closers with a lowercase snake_case name (which
# no rule took mid-line before: only a whole `db_password=...` line was redacted).
SECOND = [
    'docker run -e "DB_PASSWORD={v}" img', '"GITHUB_TOKEN={v}"', 'env=["API_TOKEN={v}"]', '"password={v}"',
    "'password={v}'", "`password={v}`", "[password={v}]", "(password={v})", "{{password={v}}}",
    "password={v}, x", "password={v}; x", 'x = "db_password={v}"', '"api_key={v}"', "run -e password={v}",
    "API_TOKEN={v}]", "API_TOKEN={v})", "API_TOKEN={v}}}", "API_TOKEN={v},", "API_TOKEN={v};", "`API_TOKEN={v}`",
]


@pytest.mark.parametrize("template", SECOND)
def test_every_closer_after_the_value_and_lowercase_names(template):
    r = Redactor(None, enabled=True)
    out = r.text(template.format(v=V))
    assert V not in out and "[REDACTED:assignment]" in out, out
    assert r.text(out) == out


LOWER_KEPT = [
    "connect(password=pw)", "f(token=args.token2)", "login(password=password)", "x(api_key=api_key)",
    "f(secret=SECRET_V2)", "sort_key=created_at1 x", "cache_key=user_profile_v2 x", "token=tokenizer.encode(x)",
    "session_token_id=abc123def456 x", "max_tokens=4096 x", '"password=changeme"', "--password=$DB_PASSWORD x",
]


@pytest.mark.parametrize("text", LOWER_KEPT)
def test_code_with_a_lowercase_secret_name_stays(text):
    assert Redactor(None, enabled=True).text(text) == text
