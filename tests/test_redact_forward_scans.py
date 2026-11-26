"""Growth of the rules of round 16 that read forward from a name or a line (review v16b: a block sequence whose items
hold long runs of blanks cost 20 s through the prompt hook, a017af9 3-4 s: the item was matched lazily up to a run of
blanks, and every position of the run started a scan to the line's end). Each rule is fed its adversarial repeats --
a name on every line, items that never end, blank lines and runs of blanks between and inside items, nesting, an
opener never closed -- plain and inside each diff decoration, and 5 times the text may cost at most GROWTH times as much
in CPU time (tests/test_redact_growth.linear_growth)."""
from __future__ import annotations

import base64
import json
import random

import pytest

from devmem import redact as R

from test_redact_growth import CEIL_S, linear_growth

_B64 = base64.b64encode(bytes(random.Random(4).getrandbits(8) for _ in range(48 * 400))).decode()

SHAPES = {
    # block sequence items (_block_seq_repl, _SEQ_ITEM)
    "seq: item with a run of blanks": lambda i: "api_keys:\n  - a" + str(i) + " " * 300 + "b",
    "seq: name on every line": lambda i: f"api_keys{i}:\n  - Ab12Cd34Ef{i}",
    "seq: items never ending": lambda i: f"  - Ab12Cd34Ef56{i}" + " " * 40,
    "seq: blank lines between items": lambda i: f"api_keys:\n  - Ab12Cd34Ef{i}\n\n  - x{i}\n",
    "seq: nested": lambda i: "  " * (i % 30) + f"- k{i}:",
    "seq: comment markers": lambda i: "secrets:\n  - Ab12 #" + " #" * 100,
    # flow sequences (_flow_seq_repl): brackets over lines, never closed, runs of blanks
    "flow: never closed": lambda i: f"api_keys: [a{i}" + " " * 200 + ",",
    "flow: blanks in items": lambda i: f"api_keys{i}: [a" + " " * 300 + "b, c" + " " * 100 + "d]",
    "flow: name on every line": lambda i: f"password: [Ab12Cd34Ef{i},",
    # a quoted value folded over lines (_spanning_repl, label check)
    "folded: never closing": lambda i: 'password: "word ' + "word " * 40,
    "folded: label words": lambda i: 'secret: "your must least\n  ' + "the " * 30 + '"',
    # Key Vault look-ahead and look-back (_key_vault_repl, _same_object)
    "kv: values after an id": lambda i: '{"id": "https://v.vault.azure.net/secrets/a/1", ' + '"value": "abcdefgh", ' * 20,
    "kv: values with brackets": lambda i: '"value": "abc{[", "x": [{' * 10 + '"id": "https://v.vault.azure.net/secrets/b"',
    # headless key runs (_key_run_repl): runs never matching a width, runs ending at END lines, armour everywhere
    # (a blank line every 400 lines: runs of at most KEY_RUN_MAX lines at every size, so both sizes are replaced the
    # same way; one run longer than that is kept and read on by every other rule, a step in the cost, not growth)
    "keyrun: 64-wide run": lambda i: _B64[(i * 64) % 19000:(i * 64) % 19000 + 64] + ("\n" if i % 400 == 399 else ""),
    "keyrun: 65-wide lines": lambda i: _B64[(i * 64) % 19000:(i * 64) % 19000 + 65] + " " * 60,
    "keyrun: END every 3 lines": lambda i: _B64[(i * 64) % 19000:(i * 64) % 19000 + 64] + (
        "\n-----END CERTIFICATE-----" if i % 3 == 0 else ""),
    "keyrun: carriage returns": lambda i: _B64[(i * 64) % 19000:(i * 64) % 19000 + 64] + "\r" + " " * 50,
}

DECORATIONS = {
    "plain": lambda lines: lines,
    "unified diff": lambda lines: ["@@ -1,999999 +1,999999 @@"] + [("+" if k % 2 else " ") + ln for k, ln in
                                                                   enumerate(lines)],
    "grep -n": lambda lines: [f"f.yaml:{k + 1}:{ln}" for k, ln in enumerate(lines)],
    "JSON strings": lambda lines: [json.dumps("\n".join(lines[k:k + 8])) + "," for k in range(0, len(lines), 8)],
}


def _text(shape: str, deco: str, size: int) -> str:
    lines: list[str] = []
    n, i = 0, 0
    while n < size:
        unit = SHAPES[shape](i)
        lines += unit.split("\n")
        n += len(unit) + 1
        i += 1
    return "\n".join(DECORATIONS[deco](lines)) + "\n"


LONG_LINES = {
    # (the line's own length is what a rescan multiplies: lines of 4,000 blanks, the rules' 4 KB line limit)
    "seq item": lambda i: f"api_keys:\n  - a{i}" + " " * 4000 + "b",
    "seq item in a diff": lambda i: f"+  - a{i}" + " " * 4000 + "b",
    "flow item": lambda i: f"api_keys{i}: [a" + " " * 4000 + "b]",
}


@pytest.mark.parametrize("shape", list(LONG_LINES))
def test_items_with_long_runs_of_blanks_cost_what_plain_lines_do(shape):
    """100 KB of such items costs at most 8 times 100 KB of the same lines with the blanks replaced by letters
    (b35165e: 13-26 s against about 0.4 s)."""
    import time
    head = "@@ -1,999 +1,999 @@\n api_keys:\n" if "diff" in shape else ""
    lines = [LONG_LINES[shape](i) for i in range(24)]
    trap = head + "\n".join(lines) + "\n"
    plain = trap.replace(" " * 4000, "x" * 4000)

    def cpu(t):
        best = 1e9
        for _ in range(2):
            a = time.process_time()
            R.Redactor(None).text(t)
            best = min(best, time.process_time() - a)
        return best
    for _attempt in range(2):
        t1, t2 = cpu(plain), cpu(trap)
        if t2 <= 8 * max(t1, 0.05):
            return
    raise AssertionError(f"{shape}: {t2:.2f} s against {t1:.2f} s")


# ---- the length of ONE line (review B11, B1): the tests above repeat lines of a fixed length, so a cost quadratic in
# the length of one line does not show as growth there. Here each rule's own seed (tests/test_redact_growth.SEEDS:
# one per rule, so every rule that scans forward from its match is started) stands before and after one run of a
# filler class on a single line, 4 KB and 64 KB long; sixteen lines of 4 KB are read in the time one line of 64 KB
# is, within LINE_GROWTH (a cost quadratic in the line's length takes about sixteen times as long), and a 4 KB line
# of the filler costs at most FILLER_COST times a 4 KB line of one letter (a rule that caps its scan at 4 KB but
# rescans within it, as b35165e's did over 4,000 blanks, costs far more there and does not grow past the cap).
FILLERS = {"blanks": " ", "dashes": "-", "base64": "Ab1+/"}
LINE_GROWTH = 4.0
FILLER_COST = 8.0


def _one_line(prefix: str, filler: str, size: int) -> str:
    return prefix + (filler * (size // len(filler) + 1))[:size] + " " + prefix.split("\n")[-1] + "\n"


def line_growth(fn_small, fn_big, factor: float = LINE_GROWTH, what: str = "16 x 4 KB, 1 x 64 KB") -> None:
    """fn_big costs at most `factor` times fn_small in CPU time (and 0.05 s of clock resolution): the minimum of runs
    taken in turns; a failing verdict is measured again after a pause, up to two more times."""
    import time

    def cpu(f) -> float:
        a = time.process_time()
        f()
        return time.process_time() - a
    ts = tb = 1e9
    for attempt in range(3):
        if attempt:
            time.sleep(1.0)
        for _ in range(2 if attempt == 0 else 4):
            ts, tb = min(ts, cpu(fn_small)), min(tb, cpu(fn_big))
            if tb <= factor * max(ts, 0.05):
                return
    raise AssertionError(f"{what}: {ts:.3f} s, {tb:.3f} s (at most {factor} times)")


def _seeds() -> list[str]:
    from test_redact_growth import SEEDS
    return sorted(SEEDS)


@pytest.mark.parametrize("filler", list(FILLERS))
@pytest.mark.parametrize("seed", _seeds())
def test_one_long_line_costs_what_its_length_does(seed, filler):
    from test_redact_growth import SEEDS
    prefix = SEEDS[seed].rstrip("\n")       # (a seed of several lines keeps them: a sequence item under its name)
    # (4,000: under the 4,096 characters some rules read of a line, so they read the short line whole)
    small, big = _one_line(prefix, FILLERS[filler], 4000), _one_line(prefix, FILLERS[filler], 64000)
    letters = _one_line(prefix, "x", 4000)
    red = R.Redactor(None)
    try:
        line_growth(lambda: [red.text(small) for _ in range(16)], lambda: red.text(big))
        line_growth(lambda: [red.text(letters) for _ in range(16)], lambda: [red.text(small) for _ in range(16)],
                    FILLER_COST, "16 x 4 KB of one letter, of the filler")
    except AssertionError as e:
        raise AssertionError(f"{seed} | {filler}: {e}") from None


@pytest.mark.parametrize("bounded", [False, True], ids=["to the line's end", "within 4 KB"])
def test_the_line_checks_fail_on_a_rule_quadratic_in_one_line(monkeypatch, bounded):
    """Self-test: a rule whose every match scans on to its line's end fails the growth check; one that rescans at most
    4 KB from every blank (as the sequence item regexes of b35165e did over a run of blanks) fails the filler check."""
    import re
    rx = re.compile(r" (?=[ ]{0,4096}Q)" if bounded else r" (?=[^\n]*Q)")
    monkeypatch.setattr(R, "_RULES", [("assignment", rx, " ")])
    monkeypatch.setattr(R, "_ASSIGN_RULES", [])
    red = R.Redactor(None)

    def rules(t):
        return lambda: red._rules(t, R._RULES, R._ASSIGN_RULES)
    with pytest.raises(AssertionError):
        if bounded:
            # (as many lines as make the rescans cost a second of CPU time: on a fast machine 64 lines stay under the
            # check's 0.05 s floor times FILLER_COST, and the check, rightly, does not fail)
            import time
            n = 64
            while n < 4096:
                t0 = time.process_time()
                rules(_one_line("x", " ", 4096) * n)()
                if time.process_time() - t0 >= 1.0:
                    break
                n *= 2
            line_growth(rules(_one_line("x", "x", 4096) * n), rules(_one_line("x", " ", 4096) * n), FILLER_COST)
        else:       # (2 KB and 32 KB lines here: the quadratic rule would take minutes on 64 KB)
            line_growth(rules(_one_line("x", " ", 2048) * 16), rules(_one_line("x", " ", 32768)))


@pytest.mark.parametrize("deco", list(DECORATIONS))
@pytest.mark.parametrize("shape", list(SHAPES))
def test_a_forward_scan_grows_linearly(shape, deco):
    def make(size):
        red, s = R.Redactor(None), _text(shape, deco, size)
        return lambda: red.text(s)
    try:
        linear_growth(make, CEIL_S, 20_000, 100_000)
    except AssertionError as e:
        raise AssertionError(f"{shape} | {deco}: {e}") from None
