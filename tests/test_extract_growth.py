"""Extraction time grows linearly with a file's size, also when an opener is followed by a newline.

A negated class such as `[^)]*` crosses newlines, so the 4 KB line cap did not help: at 511 KB a `.rs` file of
`pub(\\n` took 42.6 s, a `.luau` file of `require(` + 290 spaces 32.6 s, a `.go` file of `func (\\n` 13.9 s and a `.js`
file of `a$a$...` lines 12.2 s (reviewer's ex_speed.py). Every representative file type is timed on each shape at
64, 128, 256 and 512 KB: the time may not grow faster than GROWTH per doubling (above a noise floor), and no
512 KB input may take longer than BOUND_S. Regex extraction reads at most MAX_EXTRACT_CHARS (256 KB) of a file.
"""
from __future__ import annotations

import gc
import math
import time

import pytest
from test_extract_speed import REPS

from devmem.extract import MAX_EXTRACT_CHARS, extract_file
from devmem.index import doc_terms, file_fields

# each pattern's opener with a newline inside it, and the reviewer's shapes
UNITS = {
    "paren-nl": "(\n", "angle-nl": "<\n", "brace-nl": "{\n", "bracket-nl": "[\n", "dquote-nl": '"\n', "squote-nl": "'\n",
    "backtick-nl": "`\n", "require-nl": "require(\n", "require-spaces": "require(" + " " * 290 + "\n",
    "go-func": "func (\n", "rust-pub": "pub(\n", "kotlin-fun": "fun <\n", "js-const": "const a = (\n",
    "ts-let": "let b=(x\n", "js-export": "export {\n", "js-import": "import a\n", "js-function": "function \n",
    "js-dollar": "a$" * 2040 + "\n", "luau-local": "local a:" + "b" * 190 + "\n", "luau-array": "{ 'a', 'b',\n",
    "block-comment": "/*\n", "lua-block": "--[[\n", "py-from": "from a\n", "py-def": "def (:\n", "shell-fn": "f()\n",
    "class": "class \n", "select": "SELECT a\n", "asset": "a = 'rbxassetid://\n", "member": "a.b \n",
    "lua-function": "function a.b.c\n", "kotlin-mods": "a b c d e f g h \n",
}
SIZES = (64_000, 128_000, 256_000, 512_000)
GROWTH = 2.5          # allowed time ratio per doubling (linear is about 2)
FLOOR_S = 0.1         # below this, ratios are noise (the old quadratic shapes took 0.5 to 8 s at 128 KB)
BOUND_S = 3.0         # any 512 KB input (measured worst about 0.2 s on Windows; Python's ast about 2 s in Docker)


def _time(rel: str, text: str, runs: int = 2) -> float:
    """Best of `runs`, with the garbage collector paused: its passes over the many objects Python's own `ast` makes
    grow faster than the input (measured in Docker: ast.parse alone 2.5x per doubling with it, about 2x without)."""
    best = 1e9
    gc.collect()
    gc.disable()
    try:
        for _ in range(runs):
            t = time.perf_counter()
            info, _facts = extract_file(rel, text)
            doc_terms(file_fields(rel, info, text))
            best = min(best, time.perf_counter() - t)
    finally:
        gc.enable()
    return best


def _check(rel: str, sizes: tuple[int, ...], runs: int) -> list[str]:
    problems = []
    for name, unit in UNITS.items():
        times = [_time(rel, (unit * (size // len(unit) + 1))[:size], runs) for size in sizes]
        bound = BOUND_S * sizes[-1] / 512_000          # linear share of the 512 KB bound
        if times[-1] > bound:
            problems.append(f"{name}: {times[-1]:.2f}s at {sizes[-1] // 1000} KB (bound {bound:.2f}s)")
        for a, b, s0, s1 in zip(times, times[1:], sizes, sizes[1:]):
            allowed = GROWTH ** math.log2(s1 / s0)      # per doubling
            if a >= FLOOR_S and b > allowed * a:      # a ratio only when the base time is well above noise
                problems.append(f"{name}: {a:.3f}s -> {b:.3f}s from {s0 // 1000} to {s1 // 1000} KB")
    return problems


@pytest.mark.parametrize("kind", sorted(REPS))
def test_extraction_grows_linearly_with_newlines_inside_openers(kind):
    """Every push: 32 KB then 128 KB (below the extraction cap), one run each (about 15 s in all)."""
    problems = _check(REPS[kind], (32_000, 128_000), runs=1)
    if problems:    # one sample can lose to a busy machine: every size again, best of three, before it counts
        problems = _check(REPS[kind], (32_000, 128_000), runs=3)
    assert not problems, f"{REPS[kind]}: " + "; ".join(problems)


@pytest.mark.stress
@pytest.mark.parametrize("kind", sorted(REPS))
def test_extraction_growth_full_sweep(kind):
    """64, 128, 256 and 512 KB, best of two runs, and the 512 KB bound (about 3.5 minutes in all)."""
    problems = _check(REPS[kind], SIZES, runs=3)
    assert not problems, f"{REPS[kind]}: " + "; ".join(problems)


def test_only_a_prefix_is_extracted_and_the_entry_says_so():
    head = "function early()\nend\n"
    text = head + ("-- filler\n" * (MAX_EXTRACT_CHARS // 10 + 10)) + "function late()\nend\n"
    info, _ = extract_file("src/a.js", text)
    assert info["symbols"] == ["early"] and info.get("extract_truncated") is True
    info, _ = extract_file("src/a.js", head)
    assert "extract_truncated" not in info
