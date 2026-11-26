"""Shapes from the second security review's redaction lists (all values made up).

Caught now: a Telegram token inside a Bot API URL and one with a longer tail, versioned Discord webhook paths, GitLab
pipeline-trigger and deploy tokens, Mailgun `key-<32 hex>`, a 12-character Stripe live key, and an assignment inside
JSON escaped with eight backslashes (three levels; tests/test_redact_round4.py checks eight). No longer redacted: dependency versions under secret-sounding package names, `tokenId`
and `tokenAddress` values, a `class SK<32 HEX>` name, `session_token_id = <uuid>`, a `*_VERSION=` setting and a short
lowercase `?sig=` word.
"""
from __future__ import annotations

import pytest

from devmem.redact import Redactor

H32 = "0123456789abcdef0123456789abcdef"
CAUGHT = {
    "telegram in bot URL": ("curl https://api.telegram.org/bot7123456789:AAHfakefakefakefakefakefakefake12345/sendMessage",
                            "AAHfakefake"),
    "telegram plain": ("TELEGRAM 7123456789:AAHfakefakefakefakefakefakefake12345 ok", "AAHfakefake"),
    "discord v10": ("https://discord.com/api/v10/webhooks/123456789012345678/AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
                    "AbCdEfGhIjKl"),
    "gitlab trigger": ("glptt-0123456789abcdef0123456789abcdef01234567", "0123456789abcdef0123"),
    "gitlab deploy": ("gldt-AbCdEfGh12345678IjKl", "AbCdEfGh1234"),
    "mailgun": ("MAILGUN key-" + H32, H32),
    "stripe short": ("sk_live_" + "Ab12" * 3, "Ab12Ab12Ab12"),
    "escaped 8 levels": ("\\" * 8 + '"password' + "\\" * 8 + '": ' + "\\" * 8 + '"Xk9fakeP4ssw0rdQz' + "\\" * 8 + '"',
                         "Xk9fakeP4ssw0rdQz"),
}
KEPT = [
    '"@aws-sdk/credential-providers": "^3.645.0",',
    '"@google-cloud/secret-manager": "^5.6.0",',
    '{"tokenId": "1234567890", "owner": "alice"}',
    '{"tokenAddress": "0x6B175474E89094C44Da98b954EedeAC495271d0F"}',
    "class SK" + H32.upper() + " {}",
    "session_token_id = 123e4567-e89b-12d3-a456-426614174000",
    "SECRET_SERVICE_VERSION=2.14.0b1",
    "https://example.com/blog?sig=newsletter2024&page=2",
]


@pytest.mark.parametrize("name", sorted(CAUGHT))
def test_caught(name):
    text, secret = CAUGHT[name]
    out = Redactor(None).text(text)
    assert secret not in out and "REDACTED" in out, out


@pytest.mark.parametrize("text", KEPT)
def test_kept(text):
    assert Redactor(None).text(text) == text


def test_real_secrets_next_to_those_shapes_are_still_caught():
    red = Redactor(None)
    assert "REDACTED" in red.text('"password": "Xk9fakeP4ssw0rdQz"')
    assert "REDACTED" in red.text("https://acct.blob.core.windows.net/c?sv=1&sig=AbC%2Bfake%2Ffake1234%3D")
    assert "REDACTED" in red.text("twilio SK" + H32)
    assert "REDACTED" in red.text("STRIPE_KEY=4eC39HqLyjWDarjtT1zdp7dc")
