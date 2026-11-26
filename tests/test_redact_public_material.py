"""Public material and data shown as base64 lines without a private key's BEGIN line (review B11, blocker B3).

The headless key-body rule (redact._key_run_repl) replaces a run of three or more equal-width (64 or 70) base64 lines
as key material when no BEGIN line is in view. b35165e also replaced an RFC 4716 public key under its visible
`---- BEGIN SSH2 PUBLIC KEY ----` line, a MIME attachment and an HTML data URI wrapped at 64, and runs of any length.
Now the rule declines under RFC 4716's BEGIN line, under a line that says the run is base64 data (`Content-Transfer-
Encoding: base64`, `;base64,`), and for a run longer than KEY_RUN_MAX lines; base64 of binary data with none of that in
view is still replaced (it cannot be told from a key body: docs/PRIVACY.md states it as a trade-off)."""
from __future__ import annotations

import base64
import random

import pytest

from devmem import redact as R
from devmem.redact import Redactor

RUN_MAX = getattr(R, "KEY_RUN_MAX", 512)     # (the module constant; b35165e had none)
B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _lines(n: int, width: int, seed: int = 4716) -> list[str]:
    rng = random.Random(seed)
    out = []
    while len(out) < n:
        s = "".join(rng.choice(B64) for _ in range(width))
        if any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s):
            out.append(s)
    return out


def _blob_b64(size: int, width: int) -> list[str]:
    rng = random.Random(size)
    b = base64.b64encode(bytes(rng.getrandbits(8) for _ in range(size))).decode()
    return [b[i:i + width] for i in range(0, len(b), width)]


def test_an_rfc4716_public_key_is_kept():
    body = _lines(8, 70) + ["Q7xY2"]
    text = "\n".join(["---- BEGIN SSH2 PUBLIC KEY ----", 'Comment: "3072-bit RSA, converted by t@h from OpenSSH"',
                      *body, "---- END SSH2 PUBLIC KEY ----", ""])
    assert Redactor(None).text(text) == text
    # (read from its middle, the END line below is the evidence)
    tail = "\n".join(body[2:] + ["---- END SSH2 PUBLIC KEY ----", ""])
    assert Redactor(None).text(tail) == tail


@pytest.mark.parametrize("head", ["Content-Type: application/pdf\nContent-Transfer-Encoding: base64\n\n",
                                  "Content-Transfer-Encoding: BASE64\n",
                                  '<img src="data:image/png;base64,\n',
                                  "  logo: data:image/svg+xml;base64,\\\n"])
def test_base64_data_under_a_line_that_says_so_is_kept(head):
    text = head + "\n".join(_blob_b64(3000, 64)) + "\n"
    assert Redactor(None).text(text) == text


@pytest.mark.parametrize("width", [64, 70])
def test_a_run_longer_than_any_key_body_is_kept(width):
    text = "\n".join(_blob_b64(width * 3 // 4 * (RUN_MAX + 40), width)) + "\n"
    assert text.count("\n") > RUN_MAX
    assert Redactor(None).text(text) == text


@pytest.mark.parametrize("n", [3, 40, 200, RUN_MAX])
def test_a_headless_run_up_to_the_largest_key_body_is_still_replaced(n):
    """(the other direction: a key body of up to KEY_RUN_MAX lines with no BEGIN line and nothing else in view)"""
    body = _lines(n, 64, seed=n)
    out = Redactor(None).text("\n".join(body) + "\n")
    assert "[REDACTED:private-key]" in out and not any(ln in out for ln in body)


@pytest.mark.parametrize("width", [64, 70])
def test_base64_of_binary_with_nothing_else_in_view_is_replaced(width):
    """Pins the documented trade-off: `openssl base64` / `base64 -w 64` output of a file, alone, is replaced (about 60
    lines for 3,000 bytes: fp16.py in the review measured 63, 63 and 57 lines)."""
    lines = _blob_b64(3000, width)
    out = Redactor(None).text("\n".join(lines) + "\n")
    assert "[REDACTED:private-key]" in out and not any(ln in out for ln in lines[:-1])


def test_a_key_body_under_a_data_line_with_its_private_end_line_is_replaced():
    """(the data evidence does not outweigh a private key's END line right below)"""
    body = _lines(6, 64)
    text = "Content-Transfer-Encoding: base64\n\n" + "\n".join(body) + "\n-----END RSA PRIVATE KEY-----\n"
    out = Redactor(None).text(text)
    assert not any(ln in out for ln in body), out
