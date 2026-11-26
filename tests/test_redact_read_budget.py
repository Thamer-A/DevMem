"""The work of one redaction call is bounded (review v15b, blocker 1: each reading other than the plain one -- JSON
string literals, a diff's sides, combined-diff parents, `diff -y` columns, numbered prefixes, word diffs -- ran the
whole rule pass again on its rebuilt text, and readings nested, so a 106 KB prompt of diffs inside JSON strings inside
diffs took 55 s through the UserPromptSubmit hook, and 60 assistant texts of it 60 s at Stop, past its 30 s limit).

Now the side readings (a diff's sides, `diff -y` columns, a text with returns inside lines read again, and every
reading made inside them) of one call draw from one budget: READ_FACTOR times what the call's plain readings handed
the rule pass, plus READ_BASE (Redactor._read, _grant, _view_pass); a reading the budget refuses fails closed. The
texts come from tests/redact_cost_shapes.py: every pair (and, under the stress marker, every triple) of the
decorations tools give text, stacked, each line distinct."""
from __future__ import annotations

import itertools
import json
import time

import pytest

from devmem import redact as R
from devmem.redact import Redactor

from redact_cost_shapes import DECORATIONS, base_lines, stacked
from redact_view_helpers import leaks

SINGLES = [(n,) for n in DECORATIONS]
PAIRS = list(itertools.product(DECORATIONS, repeat=2))
TRIPLES = list(itertools.product(DECORATIONS, repeat=3))
SIZES = (6_000, 24_000)


def _ids(c) -> str:
    return "+".join(c)


def _bound(text: str, plain: int) -> int:
    """The proven bound on what the side readings of one call hand the rule pass: the budget, plus at most one
    batch's main pass read again piece by piece (Redactor._joined_reading) and one READ_CALL per piece read alone."""
    return R.READ_FACTOR * plain + R.READ_BASE + 2 * max(R.READ_BATCH, len(text) // 4)


def _check_work(names, size):
    text = stacked(names, size)
    red = Redactor(None)
    red.text(text)
    side, plain = red.last_work
    assert plain >= len(text), (side, plain)
    assert side <= _bound(text, plain), f"{_ids(names)} at {len(text)}: side {side}, plain {plain}"


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("names", SINGLES + PAIRS, ids=_ids)
def test_the_side_readings_stay_within_the_budget(names, size):
    _check_work(names, size)


@pytest.mark.stress
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("names", TRIPLES, ids=_ids)
def test_the_side_readings_stay_within_the_budget_for_triples(names, size):
    _check_work(names, size)


# ---- CPU time against the plain rule pass ----

# the whole call costs at most this many times one pass of the rules over the text and its decorated lines
# (Redactor._rules(_line_view(text))), measured as CPU time, for stacks of up to three decorations: the plain reading
# (about one pass), the JSON readings (each level of strings read again, as before) and the side readings (at most
# READ_FACTOR times the plain readings). Measured on one Windows machine: at most 7.7 for every pair at 20 KB (a017af9:
# 13.7), and 33 to 130 on a017af9 for diffs inside JSON strings inside diffs five levels deep (review v15b)
CPU_BOUND = 16.0


def _cpu(fn, reps: int = 3) -> float:
    best = 1e9
    for _ in range(reps):
        a = time.process_time()
        fn()
        best = min(best, time.process_time() - a)
    return best


def _cpu_ratio(text: str) -> float:
    """CPU time of Redactor.text over that of the plain rule pass, each the minimum of three runs (a loaded machine
    slows both alike; process time leaves out the time other processes take)."""
    whole = _cpu(lambda: Redactor(None).text(text))
    r = Redactor(None)
    plain = _cpu(lambda: r._rules(r._line_view(text), R._RULES, R._ASSIGN_RULES))
    return whole / max(plain, 0.02)


def _check_cpu(names, size):
    text = stacked(names, size)
    for _attempt in range(3):           # (a measurement disturbed by the machine is taken again before it fails)
        ratio = _cpu_ratio(text)
        if ratio <= CPU_BOUND:
            return
    raise AssertionError(f"{_ids(names)} at {len(text)} characters: {ratio:.1f} times the plain rule pass")


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("names", SINGLES + [("json", "unified"), ("unified", "json", "unified"),
                                             ("combined8", "json", "combined8"), ("json", "json", "unified"),
                                             ("combined3", "json", "word"), ("unified", "json", "sdiff"),
                                             ("numbered", "combined8"), ("json", "headerless")], ids=_ids)
def test_a_call_costs_a_bounded_multiple_of_the_plain_rule_pass(names, size):
    _check_cpu(names, size)


@pytest.mark.stress
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("names", PAIRS + TRIPLES, ids=_ids)
def test_a_call_costs_a_bounded_multiple_of_the_plain_rule_pass_stacked(names, size):
    _check_cpu(names, size)


def test_the_cpu_check_fails_on_a_multiplied_reading(monkeypatch):
    """Self-test of the measurement: a side reading that runs the rule pass 30 times over (what stacked readings did
    before the budget) is caught by the check."""
    orig = Redactor._side_texts

    def thirty(self, pieces, depth):
        for _ in range(29):
            orig(self, pieces, depth)
        return orig(self, pieces, depth)

    monkeypatch.setattr(Redactor, "_side_texts", thirty)
    monkeypatch.setattr(R, "READ_FACTOR", 10_000)
    monkeypatch.setattr(R, "READ_BASE", 10 ** 9)
    with pytest.raises(AssertionError):
        _check_cpu(("unified",), 24_000)


# ---- what the budget does when it runs out: fail closed ----

OLD = "Hq7RtW2xLm9KpB4nVc8sYd3F"
NEW = "Zx5NvB8mQw1ErT6yUi3oPa9S"
HUNK = "@@ -1,4 +1,4 @@\n tls:\n   private_key: |\n-    {old}\n+    {new}\n   user: app\n"


def _no_budget(monkeypatch):
    monkeypatch.setattr(R, "READ_FACTOR", 0)
    monkeypatch.setattr(R, "READ_BASE", 0)


def test_with_the_budget_a_value_on_a_changed_line_is_read_on_its_side():
    out = Redactor(None).text(HUNK.format(old=OLD, new=NEW))
    assert not leaks(out, [OLD, NEW]) and "[REDACTED:assignment]" in out and R.DIFF_FAILED not in out


def test_a_hunk_past_the_budget_has_every_line_replaced(monkeypatch):
    """(review B11, B5: the context lines of a refused hunk were kept, and they can hold what only its sides show)"""
    _no_budget(monkeypatch)
    red = Redactor(None)
    out = red.text(HUNK.format(old=OLD, new=NEW))
    assert not leaks(out, [OLD, NEW]), out
    lines = out.split("\n")
    assert lines[3] == "-" + R.DIFF_FAILED and lines[4] == "+" + R.DIFF_FAILED, out
    assert lines[1] == " " + R.DIFF_FAILED and lines[5] == " " + R.DIFF_FAILED, out
    assert lines[0] == "@@ -1,4 +1,4 @@" and lines[6] == ""        # (the header and the empty last line stay)
    assert red.hits.get("budget")


def test_hunks_are_read_in_order_until_the_budget_runs_out(monkeypatch):
    """A budget for about one hunk: the first is read on its sides, a later one fails closed."""
    one = HUNK.format(old=OLD, new=NEW)
    text = one + "".join(HUNK.format(old=OLD[::-1] + str(i), new=NEW[::-1] + str(i)) for i in range(40))
    monkeypatch.setattr(R, "READ_FACTOR", 0)
    monkeypatch.setattr(R, "READ_BASE", 400)
    monkeypatch.setattr(R, "READ_BATCH", 1)
    red = Redactor(None)
    out = red.text(text)
    assert not leaks(out, [OLD, NEW] + [OLD[::-1], NEW[::-1]]), out
    lines = out.split("\n")
    assert lines[3] == "-    [REDACTED:assignment]" and R.DIFF_FAILED in out, out[:600]


def test_a_diff_y_block_past_the_budget_has_every_row_replaced(monkeypatch):
    _no_budget(monkeypatch)

    def row(left: str, right: str, mark: str = " ") -> str:     # (GNU `diff -y` at its default width)
        return (left.ljust(62) + mark + "\t" + right) if mark != " " else left.ljust(64) + right

    text = "\n".join([row("tls:", "tls:"), row("  private_key: |", "  private_key: |"),
                      row("    " + OLD, "    " + NEW, "|"), row("  user: app", "  user: app")]) + "\n"
    out = Redactor(None).text(text)
    assert not leaks(out, [OLD, NEW]), out
    assert out.count(R.DIFF_FAILED) == 8, out


def test_a_json_string_of_lines_past_the_budget_is_replaced(monkeypatch):
    """Inside a side reading, a JSON string holding more than one line that the budget refuses is replaced whole; one
    of a single line is left to the plain reading of the text around it."""
    inner = HUNK.format(old=OLD, new=NEW)
    text = "@@ -1,3 +1,3 @@\n data:\n-  x: " + json.dumps(inner) + "\n+  x: " + json.dumps(inner[::-1]) + "\n"
    monkeypatch.setattr(R, "READ_FACTOR", 0)
    monkeypatch.setattr(R, "READ_BASE", 2 * len(text) + 200)     # the outer sides, not the strings inside them
    out = Redactor(None).text(text)
    assert not leaks(out, [OLD, NEW, OLD[::-1], NEW[::-1]]), out


def test_a_text_with_lone_returns_is_read_twice_as_plain_readings(monkeypatch):
    """The second reading of a text with returns inside its lines is a plain reading (review B11, B2: charged as a side
    reading it used up the budget on an ordinary file with old Mac line ends); with no budget at all it still runs."""
    _no_budget(monkeypatch)
    text = "tls:\r  private_key: |\r    " + OLD + "\r  user: app\n"
    red = Redactor(None)
    out = red.text(text)
    assert not leaks(out, [OLD]) and R.BUDGET_FAILED not in out and "[REDACTED:assignment]" in out, out
    side, plain = red.last_work
    assert side == 0 and plain >= 2 * len(text), red.last_work


def test_a_string_with_lone_returns_in_a_side_reading_past_the_budget_is_replaced_whole(monkeypatch):
    """Inside a side reading the second reading of a JSON string with returns inside its lines is charged; refused,
    every line it would have read is replaced (it was only the lines holding a return)."""
    red = Redactor(None)
    monkeypatch.setattr(red, "_grant", lambda n: False)
    red._sides = 1
    s = "tls:\r  private_key: |\r    " + OLD + "\nuser: app\npassword_hint: none"
    assert red._cr_text(s, 1) == "\n".join([R.BUDGET_FAILED] * 3)


def test_identical_sides_and_literals_are_read_once():
    """A combined diff whose parents' old sides are the same text, and a JSON string on a context line (in both sides
    of its hunk), cost one reading each: the side readings' charge is about two of the diff's texts, not nine."""
    lines = base_lines(400)
    par = 8
    body = [" " * par + ln for ln in lines]
    body[5] = "-" * par + lines[5]
    body.insert(6, "+" * par + lines[5] + "x")
    text = "@" * (par + 1) + " " + " ".join(["-1,999999"] * par) + " +1,999999 " + "@" * (par + 1) + "\n" + \
        "\n".join(body) + "\n"
    red = Redactor(None)
    red.text(text)
    side, plain = red.last_work
    assert side < 5 * len(text), (side, plain, len(text))      # (nine sides would be 9 times the text at least)


# ---- the write boundary: a structuredPatch of thousands of hunks ----

def _patch(n: int) -> dict:
    return {"filePath": "c.yaml", "oldString": "a", "newString": "b",
            "structuredPatch": [{"oldStart": i, "oldLines": 3, "newStart": i, "newLines": 3,
                                 "lines": [" private_key: |", f"-    {OLD}{i}", f"+    {NEW}{i}"]} for i in range(n)]}


def test_a_structured_patch_of_5000_equal_hunks_is_read_once():
    """review v15b wb15.py: record() of a raw list of 5,000 equal hunks took 10 s on a017af9 (0.03 s on e8c24da, which
    left their values in place, so each run was its own redaction and was looked up): a run redacted before is
    looked up (Redactor._run), so the cost is that of one hunk plus the walk."""
    hunks = [{"oldStart": i, "lines": [" private_key: |", "-    " + OLD, "+    " + NEW]} for i in range(5000)]
    best = _cpu(lambda: Redactor(None).record(hunks), reps=2)
    assert best < 2.0, best
    assert not leaks(json.dumps(Redactor(None).record(hunks)), [OLD, NEW])


@pytest.mark.parametrize("call", ["record", "tree"])
def test_a_structured_patch_of_thousands_of_hunks_costs_what_its_text_costs(call):
    """Distinct hunks: each hunk's lines are one run (one call): 4,000 hunks cost at most about eight times what 500
    do, and every value is redacted (tree stops at its budget of TREE_LEAVES strings)."""
    def run(n):
        red = Redactor(None)
        x = _patch(n)
        return (lambda: red.record(x)) if call == "record" else (lambda: red.tree(x, 10 ** 7))
    for _attempt in range(3):
        t1, t2 = _cpu(run(500), reps=2), _cpu(run(4_000), reps=2)
        if t2 <= 12 * max(t1, 0.05) and t2 < 20:
            break
    else:
        raise AssertionError(f"{call}: {t1:.2f} s for 500 hunks, {t2:.2f} s for 4,000")
    red = Redactor(None)
    out = red.record(_patch(300)) if call == "record" else red.tree(_patch(300), 10 ** 7)[0]
    assert not leaks(json.dumps(out), [OLD, NEW])
