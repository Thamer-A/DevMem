"""Redaction cost does not depend on how the text is cut into lines: every rule's seed and the shapes whose verdict reads
the lines around a value (_line_context), on 100 KB texts whose line length is swept from 16 bytes to one single line,
with and without trailing blanks; each stays under the growth test's bound, and doubling the text at most doubles the
time (with a margin for a busy machine).

Before the fix, `token: AbCdEfGhIjKl ` repeated on lines of 1-4 KB took 5-7 s for 100 KB: the walk back to the previous
line, when that line was longer than its 1 KB window, read the whole text before it, once for every match. The old
speed test used 5,000-character lines, which take an early return, so that range was never exercised.

The default run takes the context-sensitive shapes (each repeated on its lines, and interleaved with filler lines of
the swept length) at 1 KB, 3.9 KB and one line; `-m stress` sweeps all nine lengths with and without trailing blanks,
for those shapes and for every rule's seed.
"""
from __future__ import annotations

import time

import pytest

from devmem import redact as R

from test_redact_growth import BOUND_S, EXTRA, SEEDS, SLOWER

LENGTHS = [16, 256, 1024, 2048, 3900, 4100, 8192, 65536, None]      # None: the whole text is one line
SIZE = 100_000
FILLER = "lorem ipsum dolor sit amet, consectetur adipiscing elit "

# shapes whose verdict reads the lines around the value, or which look outside their own match
CONTEXT = {
    "token-letters": "token: AbCdEfGhIjKl ",
    "prose-letters": "the password is CorrectHorse ",
    "prose-pwd": "pwd is Qwertyuiop ",
    "yaml-letters": "password: Abcdefgh ",
    "yaml-line": "password: CorrectHorse\n",
    "typed-field": "  password: CorrectHorse;\n",
    "ini-letters": "password = CorrectHorse\n",
    "table-letters": "DB_PASSWORD    CorrectHorse\n",
    "conn-many": "Server=db;Password=Ab12Cd34;",
    "printed-field": "{Host:db Password:Ab12Cd34Ef56} ",
    "env-quote-line": 'DB_PASSWORD=Xk4"Lm2Pq9\n',
    "header-name": "X-Api-Token: Ab12Cd34Ef56Gh ",
    "flow-map": "{user: admin, db_password: summer2024} ",
    "flow-letters": "{user: admin, password: CorrectHorse}\n",
}


def _text(seed: str, length: int | None, blanks: bool, size: int, interleave: bool = False) -> str:
    unit = seed.rstrip("\n")
    tail = "  \t" if blanks else ""
    if length is None:
        return (unit * (size // len(unit) + 1))[:size] + tail
    if interleave:      # one short line of the shape, then a filler line of the swept length
        block = unit + tail + "\n" + (FILLER * (length // len(FILLER) + 1))[:length] + tail + "\n"
    else:
        block = (unit * (length // len(unit) + 1))[:max(length, len(unit))] + tail + "\n"
    return (block * (size // len(block) + 1))[:size]


def _best(red: R.Redactor, s: str) -> float:
    t = time.perf_counter()
    red.text(s)
    return time.perf_counter() - t


def _check(name: str, seed: str, length: int | None, blanks: bool, interleave: bool = False) -> None:
    bound = BOUND_S * SLOWER.get(name, 1.0)
    red = R.Redactor(None)
    small, large = _text(seed, length, blanks, SIZE, interleave), _text(seed, length, blanks, 2 * SIZE, interleave)
    t1 = t2 = 1e9
    for _ in range(3):      # interleaved best of up to three: one sample can lose to a busy machine
        t1 = min(t1, _best(red, small))
        t2 = min(t2, _best(red, large))
        if t1 < bound and t2 <= 3.2 * t1 + 0.1:
            break
    where = f"{name} lines={length} blanks={blanks} interleave={interleave}"
    assert t1 < bound, f"{where}: {t1:.2f}s on {len(small)} characters"
    # linear: twice the text costs about twice the time (3.2x leaves a margin; a quadratic cost is 4x and far slower)
    assert t2 <= 3.2 * t1 + 0.1, f"{where}: {t1:.3f}s at {len(small)}, {t2:.3f}s at {len(large)}"


QUICK = [1024, 3900, None]      # the default run: the range the regression lived in, and one single line


@pytest.mark.parametrize("length", QUICK, ids=lambda v: f"L{v}")
@pytest.mark.parametrize("name", sorted(CONTEXT))
def test_context_shapes_cost_the_same_whatever_the_line_length(name, length):
    for interleave in ((False, True) if length else (False,)):
        _check(name, CONTEXT[name], length, False, interleave)


@pytest.mark.stress
@pytest.mark.parametrize("length", LENGTHS, ids=lambda v: f"L{v}")
@pytest.mark.parametrize("name", sorted(CONTEXT))
def test_context_shapes_full_sweep(name, length):
    for blanks in (False, True):
        for interleave in ((False, True) if length else (False,)):
            _check(name, CONTEXT[name], length, blanks, interleave)


@pytest.mark.stress
@pytest.mark.parametrize("length", LENGTHS, ids=lambda v: f"L{v}")
@pytest.mark.parametrize("name", sorted(SEEDS) + sorted(EXTRA))
def test_every_seed_costs_the_same_whatever_the_line_length(name, length):
    seed = SEEDS.get(name) or EXTRA[name]
    for blanks in (False, True):
        _check(name, seed, length, blanks)


def test_lines_are_classified_once_however_many_matches_ask(monkeypatch):
    """Each line's kind is computed at most once per text: thousands of matches on a few lines cost one pass."""
    calls = []
    real = R._line_kind

    def counting(t):
        calls.append(t)
        return real(t)
    monkeypatch.setattr(R, "_line_kind", counting)
    lines = 40
    text = ("password: Abcdefgh " * 150 + "\n") * lines     # 6,000 values of letters only, each asking for context
    R.Redactor(None).text(text)
    # (each pass of a rule over a changed text may classify the lines again: a handful of passes, never per match)
    assert len(calls) <= 8 * lines, len(calls)


def test_a_long_neighbour_is_no_evidence_and_is_not_read():
    """A neighbour longer than the window ends the walk: the verdict is the same as with no neighbour at all."""
    long = "x = 1;" * 400                                   # a 2,400-character line of code
    code = "interface A {\n  x: number;\n"
    s = code + long + "\n  password: CorrectHorse;\n"
    pos = s.index("password")
    assert R._line_context(s, pos) is None                  # the code lines are past the long line: not read
    R._forget_context()
    s2 = code + "  password: CorrectHorse;\n"
    assert R._line_context(s2, s2.index("password")) == "code"
    R._forget_context()
