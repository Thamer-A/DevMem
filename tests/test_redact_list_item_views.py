"""A list item whose first key is a secret block scalar, under tool decorations (verifier v13b dbg_li2.py: 24 of 24
cells leaked through `cat -n`, `> ` quoting, `git diff` and `grep -n`): the block is redacted, its sibling keys stay."""
from __future__ import annotations

import pytest

from devmem.redact import Redactor

T = "H5cKErdAat6Q" + "dWc429hrMAgG" + "amKbsqmpBadc"


def _views(t: str) -> dict:
    lines = t.split("\n")
    return {
        "plain": t,
        "cat -n": "\n".join(f"{n + 1:6}\t{ln}" for n, ln in enumerate(lines)),
        "quote": "\n".join("> " + ln for ln in lines),
        "diff +": "\n".join("+" + ln if ln else ln for ln in lines),
        "diff -": "\n".join("-" + ln if ln else ln for ln in lines),
        "grep -n": "\n".join(f"users.yaml:{n + 1}:{ln}" for n, ln in enumerate(lines)),
    }


@pytest.mark.parametrize("name", ["password", "token", "api_key"])
@pytest.mark.parametrize("sib", [0, 1, 2, 3, 5, 10])
@pytest.mark.parametrize("body", [1, 2, 3])
def test_a_list_item_block_under_every_view(body, sib, name):
    t = "users:\n  - " + name + ": |\n" + "".join("      " + T + "\n" for _ in range(body)) + \
        "".join(f"    k{i}: v{i}\n" for i in range(sib))
    for view, text in _views(t).items():
        out = Redactor(None).text(text)
        assert T not in out, (view, out)
        assert all(f"k{i}: v{i}" in out for i in range(sib)), (view, out)


PEM = ["-----BEGIN RSA PRIVATE KEY-----", "MIIEpAIBAAKCAQEA1x" + "Zq8rT2vLmN4xK7pW1sY9bC3dF6gH0jA5eR2u",
       "9Lm2Qp9Rt7Zy8Wq3Np5Vb2" + "Xk4Lm2Qp9Rt7Zy8Wq3Np5Vb2Xk4Lm2Qp", "0aB3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0hJ3",
       "-----END RSA PRIVATE KEY-----"]


@pytest.mark.parametrize("sep", [":", "-"])
def test_numbered_output_whatever_the_content_starts_with(sep):
    """`grep -n` (`N:`) and its context lines (`N-`): a run of numbers going up by one is numbered output, also when
    a line starts with a digit (v14b dbg_digit.py, pem_grepA.txt)."""
    text = "\n".join(f"{i + 10}{sep}{ln}" for i, ln in enumerate(["x = 1"] + PEM + ["y = 2"]))
    out = Redactor(None).text(text)
    assert all(p[8:24] not in out for p in PEM[1:4]), out


@pytest.mark.parametrize("mark", ["- ", "+ ", "! ", "# ", "* ", "> ", "; ", "% ", "< ", "// ", "-- ", ":: "])
@pytest.mark.parametrize("view", ["cat -n", "quote", "diff +", "grep -n"])
def test_a_block_body_line_that_starts_like_a_marker_is_content(mark, view):
    """Inside a block scalar seen through a view, a body line starting like a marker stays part of the block
    (v14b dbg_marker.py)."""
    t = "password: |\n  " + mark + "Qw8e" + "Zx1cVb7n" + "\n  more9Kq\nuser: a\n"
    out = Redactor(None).text(_views(t)[view])
    assert "Qw8eZx1c" not in out, out
