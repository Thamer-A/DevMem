"""R5: placeholders match exactly (`mypassword123` is a value, not a placeholder). R6: `secret_id` and a password
that starts like a version are redacted again; the earlier false-positive fixes stay (verify3/redact/t3, t4)."""
from __future__ import annotations

import pytest

from devmem.redact import Redactor

CAUGHT = ['password = "mypassword123"', "mysql -pMyS3cretPw db", "VAULT_SECRET_ID=123e4567-e89b-12d3-a456-426614174000",
          "PASSWORD=1.5Xk9fake44pass", 'db_password: "thepassword9"']
KEPT = ['api_key = "your_api_key"', 'password = "your-password-here"', "TOKEN=my_token", '{"tokenId": "1234567890"}',
        "session_token_id = 123e4567-e89b-12d3-a456-426614174000", "SECRET_SERVICE_VERSION=2.14.0b1",
        '"@aws-sdk/credential-providers": "^3.645.0",']


@pytest.mark.parametrize("text", CAUGHT)
def test_caught(text):
    assert "REDACTED" in Redactor(None).text(text), text


@pytest.mark.parametrize("text", KEPT)
def test_kept(text):
    assert Redactor(None).text(text) == text
