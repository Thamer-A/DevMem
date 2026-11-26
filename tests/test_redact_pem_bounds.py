"""A key block takes only what is key material (verifier v7b: p10_splice, d1_damage).

The rule for a block cut off before its END line took everything after the header to the end of the text, so a header
mentioned in prose or code erased the rest (redact.py's own source went from 2,397 lines to 236; a cut-off key in a diff
swallowed the decorated lines after it). Now a block is the header, lines of key material (base64, `Proc-Type:`-style
headers, a blank line) and the END line; without an END line it stops at the first line that is not key material, and a
header with no key material after it is left alone. The replacement keeps the block's line breaks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from devmem import redact
from devmem.redact import Redactor

BEGIN = "-----BEGIN RSA " + "PRIVATE KEY-----"
END = "-----END RSA " + "PRIVATE KEY-----"
BODY = ["MIIEowIBAAKCAQEA" + "x7Kq2Lm9Pz4Rt8Vw3Yb6Nc1" * 2,
        "Qd5Fg0Hj2Kl4Mn6Op8Qr0St2Uv4Wx6Yz8Ab0Cd2Ef4Gh6Ij8Kl0Mn2Op4Qr6St8",
        "Uv0Wx2Yz4Ab6Cd8Ef0Gh2Ij4Kl6Mn8Op0Qr2St4Uv6Wx8Yz0Ab2Cd4Ef6Gh8Ij0="]
PROSE = ["The file starts with " + BEGIN + " and then the base64 body.",
         "", "Check that:", "- the permissions are 0600", "- the owner is the service user",
         "1. run the script", "2. restart the service", "", "Done when the health check passes.",
         "Questions go to the platform channel.", "Thanks!", "-- ops"]


def _leaks(text: str) -> list[str]:
    return [b for b in BODY[1:] if b in text]


def test_a_header_mentioned_in_prose_changes_nothing():
    text = "\n".join(PROSE)
    assert Redactor(None).text(text) == text


def test_redact_py_keeps_its_lines():
    src = Path(redact.__file__).read_text(encoding="utf-8")
    out = Redactor(None).text(src)
    assert out.count("\n") == src.count("\n")


@pytest.mark.parametrize("prefix", ["", "+", "-", " ", "    ", "> ", "# "])
def test_a_block_keeps_its_line_count_and_the_lines_around_it(prefix):
    lines = ["before"] + [BEGIN, *BODY, END] + ["after one", "after two"]
    text = "\n".join(prefix + ln for ln in lines)
    out = Redactor(None).text(text)
    assert not _leaks(out)
    assert out.count("\n") == text.count("\n")
    assert out.split("\n")[0] == prefix + "before" and out.split("\n")[-1] == prefix + "after two"


@pytest.mark.parametrize("prefix", ["", "+", "     12\t"])
def test_a_cut_off_block_stops_at_the_first_line_that_is_not_key_material(prefix):
    after = ["  host: db", "  port: 5432", "user = app", "# done", "x = 1", "y = 2"]
    text = "\n".join(prefix + ln for ln in [BEGIN, *BODY] + after)
    out = Redactor(None).text(text)
    assert not _leaks(out)
    assert out.split("\n")[-len(after):] == [prefix + ln for ln in after]


def test_a_block_cut_off_at_the_end_of_the_text_is_redacted():
    text = "key:\n" + BEGIN + "\n" + "\n".join(BODY)[:150]
    out = Redactor(None).text(text)
    assert BODY[0][20:40] not in out and "[REDACTED:private-key]" in out


@pytest.mark.parametrize("shape", ["env-escaped", "json", "spaces", "js-concat", "py-list", "yaml-block"])
def test_other_shapes_of_a_key(shape):
    pem = "\n".join([BEGIN, *BODY, END])
    text = {
        "env-escaped": "PRIVATE_KEY=" + pem.replace("\n", "\\n"),
        "json": json.dumps({"private_key": pem + "\n", "client_email": "svc@example.test"}),
        "spaces": "KEY=" + pem.replace("\n", " "),
        "js-concat": "const key = " + " +\n  ".join(json.dumps(ln + "\n") for ln in pem.split("\n")) + ";",
        "py-list": "KEY = [\n" + "".join(f"    {ln!r},\n" for ln in pem.split("\n")) + "]",
        "yaml-block": "tls:\n  key: |\n" + "\n".join("    " + ln for ln in pem.split("\n")) + "\n  cert: x",
    }[shape]
    out = Redactor(None).text(text)
    assert not _leaks(out), out
    assert "svc@example.test" in out or shape != "json"
