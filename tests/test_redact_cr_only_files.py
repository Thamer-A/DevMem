"""A file with old Mac line ends (CR only) in `git log -p` / `git show` (review B11, blocker B2).

Git sees such a file as one line, so its diff is one removed and one added line holding the whole file, each line
ended by a carriage return. The redactor reads a text with returns inside its lines twice (Redactor._cr_text): with
each such return taken for a line end, then that result as it is. On b35165e the second reading was charged as a side
reading and the JSON strings inside it read both ways again, so an ordinary file used up the call's budget and the
whole file was replaced ([REDACTED:diff] / [REDACTED:budget]: 2.4 KB of 2.7 KB at 30 lines, 100 KB at 2,500). Now the
second reading of a text the plain reading reads is a plain reading too, and a text is split at its returns once."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from devmem import redact as R
from devmem.redact import Redactor

CODE = Path(argparse.__file__).read_text(encoding="utf-8").split("\n")     # ordinary code, no secret in it


def _log(lines: list[str], ends: str) -> str:
    """`git log -p -1` of a commit editing one line of a file whose lines end as `ends` says (cr: every line CR only;
    mixed: every fifth line CR only, the others LF)."""
    def join(ls: list[str]) -> str:
        if ends == "cr":
            return "\r".join(ls) + "\r"
        return "".join(ln + ("\r" if i % 5 == 0 else "\n") for i, ln in enumerate(ls))
    new = list(lines)
    new[len(new) // 2] += "  # edited"
    head = ("commit 2b1f0c3d4e5f60718293a4b5c6d7e8f901234567\nAuthor: t <t@example.invalid>\n"
            "Date:   Mon Nov 16 10:00:00 2026 -0500\n\n    edit one line\n\n"
            "diff --git a/m.py b/m.py\nindex 1111111..2222222 100644\n--- a/m.py\n+++ b/m.py\n")
    if ends == "cr":
        return head + "@@ -1 +1 @@\n-" + join(lines) + "\n\\ No newline at end of file\n+" + join(new) + \
            "\n\\ No newline at end of file\n"
    # (mixed: git's lines end at the LFs; one hunk of the whole file is enough here)
    old_l, new_l = join(lines).split("\n"), join(new).split("\n")
    body = [" " + a if a == b else "-" + a + "\n+" + b for a, b in zip(old_l, new_l)]
    return head + f"@@ -1,{len(old_l)} +1,{len(new_l)} @@\n" + "\n".join(body) + "\n"


def test_the_file_with_lf_line_ends_is_not_changed():
    """(the control: the same code with ordinary line ends is not changed, so any change below is damage)"""
    text = "\n".join(CODE[:2500])
    assert Redactor(None).text(text) == text


@pytest.mark.parametrize("ends", ["cr", "mixed"])
@pytest.mark.parametrize("lines,cap", [(30, 100_000), (300, 4_000), (300, 100_000), (1000, 100_000),
                                       (2500, 100_000), (2500, 512_000)])
def test_a_cr_only_file_in_git_log_is_not_replaced(ends, lines, cap):
    """At the hook caps (4,000: a tool's text in a session record; 100,000: a prompt; 512,000: the store's text cap)."""
    text = _log(CODE[:lines], ends)[:cap]
    red = Redactor(None)
    out = red.text(text)
    assert out == text, [(a[:80], b[:80]) for a, b in zip(text.split("\n"), out.split("\n")) if a != b][:4]
    assert not red.hits.get("budget"), red.hits
    side, plain = red.last_work
    assert side <= R.READ_FACTOR * plain + R.READ_BASE, red.last_work


def test_a_value_in_a_cr_only_file_is_still_redacted():
    """(both directions: an edited value in such a file is replaced, the rest of the file kept)"""
    lines = CODE[:300]
    lines[150] = 'API_TOKEN = "Zx5NvB8mQw1ErT6yUi3oPa9S"'
    text = _log(lines, "cr")
    out = Redactor(None).text(text)
    assert "Zx5NvB8mQw1ErT6yUi3oPa9S" not in out and "[REDACTED:" in out
    assert out.count("\r") == text.count("\r")
    changed = [a for a, b in zip(text.split("\r"), out.split("\r")) if a != b]
    assert len(changed) == 2, changed            # (the value's line in the old and in the new file, nothing else)
