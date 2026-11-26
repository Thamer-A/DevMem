"""Past the budget nothing is kept that the refused reading would have read (review B11, blocker B5).

A reading other than the plain one may only add redaction to what the plain reading produced. When the call's budget
(Redactor._grant, READ_FACTOR) refuses one, what that reading would have read is replaced by a fixed marker: every
line of the refused hunk and of every hunk after it in the same diff (DIFF_FAILED; context lines too: in a diff of two
patch files they hold the inner patch's changed lines), every row of a refused `diff -y` block and of the blocks after
it, the whole text of a refused second reading of a text with returns inside its lines (BUDGET_FAILED), and every
JSON string a side reading's refused JSON reading would have decoded, unless it is one line that decodes to its own
text. On b35165e a refused hunk replaced only its changed lines and a refused second reading only the lines holding a
return, so a decoy earlier in the same text (an octopus merge's combined diff) let a later value through."""
from __future__ import annotations

import json
import random

import pytest

from devmem import redact as R
from devmem.redact import Redactor

from redact_view_helpers import leaks

B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
OLD = "Hq7RtW2xLm9KpB4nVc8sYd3F"
NEW = "Zx5NvB8mQw1ErT6yUi3oPa9S"
INNER = "@@ -1,4 +1,4 @@\n tls:\n   private_key: |\n-    {old}\n+    {new}\n   user: app\n"


def _octopus(kb: int, parents: int = 8, lines: int = 40) -> str:
    """A merge's combined diff in which each hunk has one line removed in each parent (old sides all differ)."""
    out, i = [], 0
    while sum(map(len, out)) < kb * 1000:
        out.append("@" * (parents + 1) + " " + " ".join(f"-{i},{lines}" for _ in range(parents)) + f" +{i},{lines} "
                   + "@" * (parents + 1))
        for k in range(lines):
            if k < parents:
                out.append("".join("-" if j == k else " " for j in range(parents)) + f"gone_{i}_{k} = old({k})")
            else:
                out.append(" " * parents + f"    keep_{i}_{k} = compute(a, b, {k})")
        i += 1
    return "\n".join(out) + "\n"


def _b64(rng: random.Random, n: int) -> str:
    while True:
        s = "".join(rng.choice(B64) for _ in range(n))
        if any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s):
            return s


def _pgp_patches() -> tuple[str, list[str]]:
    """`diff -u` of two versions of a patch that edits an OpenPGP private key block: the inner patch's lines are the
    outer hunk's context lines, and only the sides of the outer hunk (the inner patch) put them under their BEGIN."""
    rng = random.Random(7)
    body = [_b64(rng, 70 if i in (0, 3) else 64) for i in range(8)]
    lines = ["--- p2.diff\t2026-11-16 10:54:30 -0500", "+++ p.diff\t2026-11-16 10:54:30 -0500", "@@ -6,10 +6,10 @@",
             "  -----BEGIN PGP PRIVATE KEY BLOCK-----", "  Comment: synthetic", "  ",
             "--ZZZZZZ" + body[0][6:], "+-" + body[0], " -" + body[1], " -" + body[2], "-+ZZZZZZ" + body[3][6:],
             "++" + body[3], " +" + body[4], " +" + body[5], "  =Ab3x"]
    return "\n".join(lines) + "\n", body[:6]


# GNU `diff -y` of a YAML file whose lines end in CR LF and CR (the left column's return stands inside the row)
MIXED_SDIFF = ('tls:\t\t\t\t\t\t\t\ttls:\n  private_key: |\r\t\t\t\t\t\t\t\t  private_key: |\r\t\t\t\t\t\t\t\t\n'
               '    6RrzrPGA6jgdsuZxiIQ14aUq\t\t\t\t      |\t    dZjKuIsRHNFyp5pggxVb9WJr\n'
               '    qUa41QIixZusdgj6AGPrzrR6\r\t\t\t\t\t\t\t      |\t    rJW9bVxggp5pyFNHRsIuKjZd\r\t\t\t\t\t\t\t\t\n'
               '  user: app\t\t\t\t\t\t\t  user: app\n')
MIXED_VALUES = ["6RrzrPGA6jgdsuZxiIQ14aUq", "dZjKuIsRHNFyp5pggxVb9WJr", "qUa41QIixZusdgj6AGPrzrR6",
                "rJW9bVxggp5pyFNHRsIuKjZd"]


def test_a_diff_of_two_patches_after_an_octopus_merge_keeps_no_key_line():
    """The real budget: 10 KB of an 8-parent combined diff uses it up, and the diff of two patches after it is
    replaced whole instead of having only its changed lines replaced (b35165e kept 4 of the 6 key lines)."""
    view, body = _pgp_patches()
    alone = Redactor(None).text(view)
    assert not leaks(alone, body), alone             # (read on its own, the sides catch every key line)
    red = Redactor(None)
    out = red.text(_octopus(10) + view)
    assert not leaks(out, body), out[-1200:]
    assert red.hits.get("budget") and R.DIFF_FAILED in out.split("@@ -6,10 +6,10 @@", 1)[1]


def test_a_mixed_line_end_diff_y_after_a_merge_keeps_no_value():
    """The real budget after 3 KB of a 2-parent combined diff (b35165e kept 2 of the 4 values: its second reading of
    the text was refused and replaced only the lines holding a lone return)."""
    assert not leaks(Redactor(None).text(MIXED_SDIFF), MIXED_VALUES)
    for decoy in (_octopus(3, 2), _octopus(3, 8), _octopus(10, 8)):
        out = Redactor(None).text(decoy + MIXED_SDIFF)
        assert not leaks(out, MIXED_VALUES), out[-800:]


def _no_budget(monkeypatch):
    monkeypatch.setattr(R, "READ_FACTOR", 0)
    monkeypatch.setattr(R, "READ_BASE", 0)


@pytest.mark.parametrize("base", [0, 32, 64, 128, 256, 1024])
def test_a_mixed_line_end_diff_y_with_no_budget_left_keeps_no_value(monkeypatch, base):
    """With READ_BASE characters of budget and nothing more (the state a decoy before it leaves): b35165e kept 2 of
    the 4 values at 256 or less. A reading inside the first reading that the budget refuses replaces only the rows it
    found, which leaves the rows unreadable as columns to the second reading: the whole text fails closed."""
    monkeypatch.setattr(R, "READ_FACTOR", 0)
    monkeypatch.setattr(R, "READ_BASE", base)
    red = Redactor(None)
    out = red.text(MIXED_SDIFF)
    assert not leaks(out, MIXED_VALUES), out
    if red.hits.get("budget"):
        assert R.BUDGET_FAILED in out or R.DIFF_FAILED in out, out


def _outer(old: str, new: str) -> str:
    """A diff of two patch files: the inner patch's lines, values on its changed lines, are context lines."""
    return ("@@ -1,7 +1,7 @@\n-From 1111 Mon Sep 17 00:00:00 2001\n+From 2222 Mon Sep 17 00:00:00 2001\n"
            + "".join(" " + ln + "\n" for ln in INNER.format(old=old, new=new).rstrip("\n").split("\n")))


def test_a_refused_hunk_has_its_context_lines_replaced_too(monkeypatch):
    _no_budget(monkeypatch)
    red = Redactor(None)
    out = red.text(_outer(OLD, NEW))
    assert not leaks(out, [OLD, NEW]), out
    lines = out.split("\n")
    assert lines[0] == "@@ -1,7 +1,7 @@" and all(ln.endswith(R.DIFF_FAILED) for ln in lines[1:-1]), out
    assert red.hits.get("budget")


def test_after_a_refused_hunk_the_rest_of_the_diff_fails_closed(monkeypatch):
    """A budget for about one hunk: the first is read on its sides; the later ones are replaced line by line, the
    diff of two patches among them (its values only on context lines) included, and the text after the diff stays."""
    first = INNER.format(old=OLD, new=NEW)
    later = "".join(INNER.format(old=f"Qa{i}Ws{i}Ed{i}Rf{i}Tg{i}Yh", new=f"Pl{i}Ok{i}Ij{i}Uh{i}Yg{i}Tf") for i in range(30))
    o2, n2 = OLD[::-1], NEW[::-1]
    text = first + later + _outer(o2, n2) + "the end of the log\n"
    monkeypatch.setattr(R, "READ_FACTOR", 0)
    monkeypatch.setattr(R, "READ_BASE", 400)
    monkeypatch.setattr(R, "READ_BATCH", 1)
    red = Redactor(None)
    out = red.text(text)
    assert not leaks(out, [OLD, NEW, o2, n2]), out[-900:]
    lines = out.split("\n")
    assert lines[3] == "-    [REDACTED:assignment]" and lines[1] == " tls:", out[:300]
    cut = next(i for i, ln in enumerate(lines) if ln.endswith(R.DIFF_FAILED))
    assert lines[cut - 1].startswith("@@ ") and cut < len(lines) - 20, out[:900]     # (refused at a hunk's start)
    rest = [ln for ln in lines[cut:-2] if not ln.startswith("@@ ")]
    assert all(ln.endswith(R.DIFF_FAILED) for ln in rest), out[-900:]
    assert lines[-2] == "the end of the log"


def test_the_rest_includes_the_hunks_of_later_commits_in_the_same_text():
    """(review B12) "the rest of the diff" is every later hunk of the same TEXT: in a `git log -p` whose first commit
    is a merge's combined diff that uses the budget up, the hunks of the commits after it are replaced too; their
    commit headers and messages are not hunk lines and stay."""
    later = "".join(f"commit {i:040x}\nAuthor: t <t@example.invalid>\n\n    change {i}\n\ndiff --git a/f{i}.py "
                     f"b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n@@ -1,3 +1,3 @@\n def f{i}():\n-    return {i}\n"
                     f"+    return {i + 1}\n     # done\n" for i in range(4))
    alone = Redactor(None).text(later)
    assert alone == later                                  # (read on their own nothing changes)
    head = "commit " + "a" * 40 + "\nMerge: 1 2 3 4 5 6 7 8\n\n    octopus\n\ndiff --cc m.py\n"
    red = Redactor(None)
    out = red.text(head + _octopus(10) + later).split("\n")[-len(later.split("\n")):]
    assert red.hits.get("budget")
    for i in range(4):
        k = out.index(f"commit {i:040x}")
        assert out[k + 3] == f"    change {i}" and out[k + 9:k + 13] == [
            " " + R.DIFF_FAILED, "-" + R.DIFF_FAILED, "+" + R.DIFF_FAILED, " " + R.DIFF_FAILED], out[k:k + 13]


# a merge's combined diff in the shape of _octopus (one line removed in each parent per hunk, the old sides all
# differ): the parents and sizes at which the budget refuses it, the numbers docs/PRIVACY.md states
@pytest.mark.parametrize("parents,kb,refused", [(2, 100, False), (3, 50, False), (3, 100, True), (4, 5, False),
                                                (4, 10, True), (5, 3, True), (8, 1, False), (8, 2, True)])
def test_where_a_combined_diff_reaches_the_budget(parents, kb, refused):
    red = Redactor(None)
    red.text(_octopus(kb, parents))
    assert bool(red.hits.get("budget")) is refused, red.last_work


def test_a_refused_json_reading_replaces_a_string_that_decodes_to_other_text(monkeypatch):
    """Inside a side reading, a JSON string the budget refuses is replaced unless it is one line that decodes to its
    own text (the plain reading read exactly that); one with escapes was read by no one (b35165e kept it)."""
    red = Redactor(None)
    monkeypatch.setattr(red, "_grant", lambda n: False)
    red._sides = 1
    hidden = json.dumps("password=" + NEW).replace("p", "\\u0070", 1)        # `"password=..."`
    plain = json.dumps("a plain string of words")
    out = red._json_strings('{"a": ' + hidden + ', "b": 1, "c": ' + plain + '}', 0)
    assert NEW not in out and '"' + R.BUDGET_FAILED + '"' in out, out
    assert plain in out, out
