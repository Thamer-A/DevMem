"""Ordinary text that redaction is known to replace (review B12), each pinned: the test asserts that the ordinary text
IS changed, next to a control that is not. docs/PRIVACY.md lists each of them among the ordinary text redaction can
replace; a change that stops one fails its test until docs/PRIVACY.md and this file are corrected."""
from __future__ import annotations

from devmem import redact as R
from devmem.redact import Redactor

from test_redact_cr_only_files import _log


# ---- a file with CR-only line ends whose text holds `[-` or `{+` (B1) ----
# git shows such a file as one removed and one added line holding the whole file. Read with its returns as line ends,
# the file's lines stand bare inside the `@@ -1 +1 @@` hunk, and a hunk with a `[-` or `{+` in it and a bare line is
# read as a `git diff --word-diff` hunk: Python's `items[-1]` opens a removed part that never closes, and once a
# redaction changes a line of that hunk, every changed part after it is replaced (fail closed)

def _code(slice_line: bool, secret: bool) -> list[str]:
    lines = [f"def step_{i}(items):" if i % 3 == 0 else f"    total_{i} = compute(items, {i})" for i in range(60)]
    if secret:
        lines[10] = 'API_TOKEN = "Zx5NvB8mQw1ErT6yUi3oPa9S"'
    if slice_line:
        lines[30] = "    last = items[-1]"
    return lines


def _changed(text: str, out: str) -> list[int]:
    return [i for i, (a, b) in enumerate(zip(text.split("\r"), out.split("\r"))) if a != b]


def test_a_cr_only_file_with_a_slice_loses_its_lines_after_it_once_a_value_is_redacted():
    text = _log(_code(slice_line=True, secret=True), "cr")
    out = Redactor(None).text(text)
    assert "Zx5NvB8mQw1ErT6yUi3oPa9S" not in out
    changed = _changed(text, out)
    # (the old file's line 10 holds the value; from its line 30, the slice, every line to the end of the new file)
    assert changed == [10] + list(range(30, len(text.split("\r")))), changed
    assert R.DIFF_FAILED in out


def test_the_same_file_without_a_slice_keeps_every_line_but_the_value():
    """(the control: only the value's line, in the old and in the new file, changes)"""
    text = _log(_code(slice_line=False, secret=True), "cr")
    out = Redactor(None).text(text)
    assert "Zx5NvB8mQw1ErT6yUi3oPa9S" not in out and len(_changed(text, out)) == 2
    clean = _log(_code(slice_line=True, secret=False), "cr")       # (and the slice alone changes nothing)
    assert Redactor(None).text(clean) == clean


# ---- `openssl x509 -text` hex dumps ----
# a colon-separated hex line is read as a `name: value` pair from one of its bytes on (the rules on the text as it is
# replace the rest of `6d:72:38:5e:...`; the reading of decorated lines, taking `83:` for a line-number prefix, the
# rest of `83:c3:50:5c:06:...`)

X509_TEXT = ("        Subject Public Key Info:\n            Public Key Algorithm: rsaEncryption\n"
             "                Public-Key: (2048 bit)\n                Modulus:\n"
             "                    00:c3:5d:93:8d:30:67:aa:12:9f:0b:77:41:d8:3e:\n"
             "                    83:c3:50:5c:06:dc:4a:ac:5e:d8:74:91:be:0d:2e:\n"
             "                    6d:72:38:5e:4d:f9:0f:c0:f9:cc:36:c1:be:ea:58:\n")


def test_an_openssl_x509_text_hex_dump_has_lines_replaced():
    out = Redactor(None).text(X509_TEXT).split("\n")
    lines = X509_TEXT.split("\n")
    assert out[:5] == lines[:5]                      # (the headers and the first hex line stay)
    assert out[5] == "                    83:c3:50:5c:06:[REDACTED:assignment]", out[5]
    assert out[6] != lines[6] and "[REDACTED:assignment]" in out[6], out[6]
