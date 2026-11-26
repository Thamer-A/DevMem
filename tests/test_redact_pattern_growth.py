"""Every compiled pattern of devmem.redact, found automatically, on adversarial strings derived from its own source:
the literal words it mentions and the first member of each character class, repeated, joined with the separators
patterns use (`:`, `=`, `,`, blanks, quotes, line breaks), after the pattern's first word (its usual anchor). A pattern
that backtracks badly takes seconds on a few hundred characters (the name/value rule of e8c24da took 30 s on
`name: ` + `type:` * 48): each call must stay under a small bound, and doubling the repetitions of a body may at most
about double the time. A rule added later is covered without a hand-written seed."""
from __future__ import annotations

import re
import time

import pytest

from devmem import redact as R

SEPS = [":", ": ", "=", ",", ", ", " ", "\"", "'", "\n", ": \"", "{", "["]
SMALL = (24, 48)            # exponential backtracking shows at these sizes
LARGE = (400, 800)          # a quadratic pattern shows here
CALL_BOUND = 0.25           # seconds for one scan of a small string (they hold well under 1 KB)


def _patterns() -> list[tuple[str, re.Pattern]]:
    seen, out = set(), []
    for kind, rx, _repl in list(R._RULES) + list(R._ASSIGN_RULES):
        if id(rx) not in seen:
            seen.add(id(rx))
            out.append((f"{kind}:{rx.pattern[:40]}", rx))
    for name, v in sorted(vars(R).items()):
        if isinstance(v, re.Pattern) and id(v) not in seen:
            seen.add(id(v))
            out.append((name, v))
    return out


_GROUP = re.compile(r"\(\?P<\w+>|\(\?P=\w+\)|\(\?[imsx]+\)|\\[A-Za-z]|\{\d+(?:,\d*)?\}")
_WORD = re.compile(r"[A-Za-z][A-Za-z_\-]{1,30}")
_CLASS = re.compile(r"\[\^?(\\.|[^\\\]])")


def _tokens(rx: re.Pattern) -> list[str]:
    src = _GROUP.sub(" ", rx.pattern)
    words = list(dict.fromkeys(w for w in _WORD.findall(src) if w.lower() not in ("p", "a", "z")))[:8]
    members = []
    for m in _CLASS.finditer(rx.pattern):
        c = m.group(1)
        c = c[1] if c.startswith("\\") and c[1] not in "swdSWDnrt" else ("a" if c.startswith("\\") else c)
        members.append(c)
    return words + list(dict.fromkeys(members))[:6]


# patterns that production only calls with .match() at the start of one line (a line's tool prefix, a numbered
# line, a hunk header, a sequence item): scanned the same way here. A finditer over one long line would try them at
# every position, which production never does (review v15b: the ratio check of _LINE_PREFIX* failed on a loaded
# machine on both versions)
MATCH_ONLY = {"_LINE_PREFIX", "_LINE_PREFIX_DASH", "_LINE_PREFIX_ONE", "_NUM_PREFIX", "_CAT_PREFIX", "_GRAPH_GUTTER",
              "_DIFF_HEADER", "_BLOCK_ITEM", "_ITEM_MAPPING", "_SEQ_ITEM", "_CONFIG_KV", "_CONFIG_EQ",
              "_TYPED_LINE", "_INI_SECTION",
              # (production only fullmatches it against one value of a tool input: secret_call_input; a finditer over
              # a long string restarts its lookahead at every position, which no caller does)
              "_PATH_VALUE"}


def _scan(rx: re.Pattern, s: str, label: str = "") -> float:
    t = time.perf_counter()
    if label in MATCH_ONLY:
        rx.match(s)
    else:
        for _m in rx.finditer(s):
            pass
    return time.perf_counter() - t


def _grows(rx: re.Pattern, unit: str, label: str = "", sizes: tuple[int, int] = LARGE) -> tuple[float, float] | None:
    """None when doubling the repetitions of `unit` at most about triples the time, else (t1, t2): the minimum of
    five runs of each size taken in turns; a failing verdict is measured again up to three more times, each after a
    short pause (a burst of load on the machine passes), before it is reported. A quadratic pattern fails every one
    (test_the_growth_check_fails_on_a_quadratic_pattern)."""
    for attempt in range(4):
        if attempt:
            time.sleep(0.2 * attempt)
        t1 = t2 = 1e9
        for _ in range(5 if attempt == 0 else 9):
            t1 = min(t1, _scan(rx, unit * sizes[0], label))
            t2 = min(t2, _scan(rx, unit * sizes[1], label))
        if t2 <= 3.0 * t1 + 0.02:
            return None
    return round(t1, 4), round(t2, 4)


PATTERNS = _patterns()


@pytest.mark.parametrize("label,rx", PATTERNS, ids=[p[0][:60] for p in PATTERNS])
def test_pattern_has_no_bad_backtracking(label, rx):
    toks = _tokens(rx)
    if not toks:
        return
    anchor = toks[0]
    slow = []
    for body in toks:
        for sep in SEPS:
            unit = body + sep
            for pre in ("", anchor + ": ", anchor + "=", "\"" + anchor + "\": "):
                for tail in ("", " value", "\x00"):
                    for n in SMALL:
                        s = pre + unit * n + tail
                        dt = _scan(rx, s, label)
                        if dt > CALL_BOUND:
                            dt = min(dt, _scan(rx, s, label), _scan(rx, s, label))   # (retried: a busy machine)
                            assert dt <= CALL_BOUND, (label, repr(s[:60]), n, round(dt, 3))   # (stop at the first)
            grew = _grows(rx, unit, label)
            if grew:
                slow.append((repr(unit[:30]), "x2", *grew))
    assert not slow, (label, slow[:5])


def test_the_growth_check_fails_on_a_quadratic_pattern():
    """Self-test: a pattern that scans to the end from every start is reported."""
    # (sizes where a quadratic scan costs more than the 0.02 s of clock resolution the check allows)
    assert _grows(re.compile(r"a(?=[^\n]*b)"), "a", sizes=(12_000, 24_000)) is not None
    assert _grows(re.compile(r"a+b"), "a", sizes=(12_000, 24_000)) is not None


def test_the_whole_redactor_on_the_anchor_strings():
    """Redactor.text on each pattern's anchor + `:` + repeated words: bounded per call."""
    red = R.Redactor(None)
    slow = []
    for label, rx in PATTERNS:
        toks = _tokens(rx)
        if len(toks) < 2:
            continue
        for body in toks[1:4]:
            s = toks[0] + ": " + (body + ":") * 48 + " value"
            t = time.perf_counter()
            red.text(s)
            dt = time.perf_counter() - t
            if dt > 1.0:
                slow.append((label, s[:50], round(dt, 2)))
    assert not slow, slow[:5]
