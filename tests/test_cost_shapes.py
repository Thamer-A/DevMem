"""Extraction and tokenising cost on whole-file shapes that are not a repeated line: many distinct names, an opener
followed by a long whitespace run, text that grows under NFKC, a large Python file.

Each of these was quadratic or worse before (measured on Windows, Python 3.13): 256 KB of distinct
`local ab=require(conf)` lines 49-68 s (one whole-file search per name), `import` + 1999 x " \\n" in a .ts file 33 s,
500 KB of `f'{a}{b}...'` lines 69 s (Anaconda's `ast`), 500 KB of U+FDFA 6.5 s to tokenise (NFKC after the cut).
The default run times 32 and 128 KB once (and all sizes again, best of three, before a failure counts); the `stress`
run 64-512 KB, best of three.
"""
from __future__ import annotations

import gc
import itertools
import math
import string
import time

import pytest

from devmem.extract import extract_file
from devmem.index import doc_terms, file_fields

GROWTH = 2.5          # allowed time ratio per doubling (linear is about 2)
FLOOR_S = 0.1         # below this, ratios are noise
BOUND_S = 3.0         # any 512 KB input; the linear share of it for smaller ones (measured worst about 0.3 s)


def _names():
    for n in itertools.count(1):
        for t in itertools.product(string.ascii_letters, repeat=n):
            yield "".join(t)


def _lines(size: int, make) -> str:
    out, n = [], 0
    for i, nm in enumerate(_names()):
        line = make(i, nm)
        if n + len(line) > size:
            break
        out.append(line)
        n += len(line)
    return "".join(out)


def _repeat(unit: str, size: int) -> str:
    return (unit * (size // len(unit) + 1))[:size]


SHAPES = {
    # one whole-file search per required config alias / created remote / HttpService alias
    "luau-config-aliases": ("x.luau", lambda s: _lines(s, lambda i, nm: f"local {nm}=require(conf)\n")),
    "luau-remote-vars": ("x.luau", lambda s: _lines(s, lambda i, nm: f"{nm}=Instance.new('RemoteEvent')\n")),
    "luau-http-aliases": ("x.luau", lambda s: _lines(s, lambda i, nm: f"{nm}=game:GetService('HttpService')\n")),
    # `import` then a whitespace run the clause could take any part of
    "ts-import-space-nl": ("x.ts", lambda s: _repeat("import" + " \n" * 1999, s)),
    "js-import-nl": ("x.js", lambda s: _repeat("import" + "\n" * 2000, s)),
    # CPython parses each f-string field from a copy of the rest of the input
    "py-fstrings": ("x.py", lambda s: _repeat("f'{a}{b}{c}{d}{e}{f}{g}'\n", s)),
    "py-fstrings-then-plain": ("x.py", lambda s: "f'{a}'\n" * 1000 + _repeat("x = 1\n", s - 7000)),
    # NFKC turns U+FDFA into 18 characters; it ran on the whole field after the cut
    "nfkc-expanding": ("x.md", lambda s: chr(0xFDFA) * (s // 3)),
    "nfkc-expanding-spaced": ("x.js", lambda s: (chr(0xFDFA) + " ") * (s // 4)),
    "py-fstrings-in-defs": ("x.py", lambda s: _repeat("def f():\n" + "    f'{a}{b}'\n" * 50, s)),
}


def _time(rel: str, text: str, runs: int) -> float:
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


def _check(name: str, sizes: tuple[int, ...], runs: int) -> list[str]:
    rel, make = SHAPES[name]
    times = [_time(rel, make(size), runs) for size in sizes]
    problems = []
    bound = BOUND_S * sizes[-1] / 512_000
    if times[-1] > bound:
        problems.append(f"{times[-1]:.2f}s at {sizes[-1] // 1000} KB (bound {bound:.2f}s)")
    for a, b, s0, s1 in zip(times, times[1:], sizes, sizes[1:]):
        if a >= FLOOR_S and b > GROWTH ** math.log2(s1 / s0) * a:
            problems.append(f"{a:.3f}s -> {b:.3f}s from {s0 // 1000} to {s1 // 1000} KB")
    return problems


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_whole_file_shapes_stay_linear(name):
    problems = _check(name, (32_000, 128_000), runs=1)
    if problems:    # one sample can lose to a busy machine: every size again, best of three, before it counts
        problems = _check(name, (32_000, 128_000), runs=3)
    assert not problems, f"{name}: " + "; ".join(problems)


@pytest.mark.stress
@pytest.mark.parametrize("name", sorted(SHAPES))
def test_whole_file_shapes_full_sweep(name):
    problems = _check(name, (64_000, 128_000, 256_000, 512_000), runs=3)
    assert not problems, f"{name}: " + "; ".join(problems)


def test_luau_one_pass_lookups_find_what_the_per_name_searches_found():
    code = ("local Config = require(game.ReplicatedStorage.Config)\n"
            "local Tun = require(script.Parent.Settings).Combat\n"
            "local Http = game:GetService('HttpService')\n"
            "local r = Instance.new('RemoteEvent')\n"
            "r.Name = 'Buy'\n"
            "other.Name = 'NotARemote'\n"
            "print(Config.Speed, a.Config.Jump, Tun.Damage)\n"
            "Http:GetAsync(url)\nHttpService:PostAsync(url, x)\nNotHttp:GetAsync(url)\n")
    info, facts = extract_file("src/Shop.server.luau", code)
    assert info["remotes_defined"] == ["Buy"]
    assert info["config_keys_used"] == ["Combat", "Combat.Damage", "Jump", "Speed"]
    assert facts["ops"]["network requests"] == 2


def test_js_import_clauses_are_read_as_before():
    code = ("import React, { useState as s, type X } from 'react'\n"
            "import type { T } from \"./types\"\n"
            "import * as path from 'node:path'\n"
            "import type from 'odd-default'\n"
            "import {\n  a,\n  b\n} from './multi'\n"
            "import broken from\nimport ok from './ok'\n"
            "const x = import('./dyn')\n")
    info, facts = extract_file("src/app.ts", code)
    assert info["deps"] == ["./dyn", "./multi", "./ok", "./types", "node:path", "odd-default", "react"]
    assert ["react", "useState"] in facts["uses"] and ["./multi", "b"] in facts["uses"]


PY_SAMPLE = '''"""Module doc with code at column 0:
def not_a_def():
    pass
"""
import os
from typing import (
    Any,
)


@decorator(
    arg=1)
# a comment between
def decorated(x):
    return f"{x!r} {os.sep}"


if os.sep == "/":
    A = 1
elif os.sep == "\\\\":
    A = 2
else:
    A = 3
try:
    import json
except ImportError:
    json = None
finally:
    pass
x = 1 + \
2
class Shop:
    def buy(self):
        return [
1, 2]
'''


def test_python_is_parsed_in_pieces_with_the_same_result():
    from devmem import extract_python as ep
    text = PY_SAMPLE * 40 + "if __name__ == '__main__':\n    decorated(1)\n"
    starts = ep.top_level_starts(text)
    assert starts and all(text[b - 1] == "\n" for b in starts)
    assert not any(text.startswith(("else", "elif", "except", "finally", "def decorated", "2\n", "1, 2]"), b)
                   for b in starts)
    whole = ep.PythonExtractor()._extract("m.py", text)
    old = ep.PARSE_CHUNK
    try:
        ep.PARSE_CHUNK = 10 ** 9
        assert ep.PythonExtractor()._extract("m.py", text) == whole
    finally:
        ep.PARSE_CHUNK = old
    info, facts = whole
    assert "parse_error" not in info and {"decorated", "Shop"} <= set(info["symbols"]) and facts["entry"]


def test_a_large_python_file_is_cut_at_a_statement_start_and_still_parsed():
    from devmem.extract import MAX_EXTRACT_CHARS
    text = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(20_000))
    assert len(text) > MAX_EXTRACT_CHARS
    info, _facts = extract_file("big.py", text)
    assert info.get("extract_truncated") and "parse_error" not in info
    assert "f0" in info["symbols"] and "f19999" not in info["symbols"]


def test_nfkc_output_is_bounded_and_normal_text_is_unchanged():
    from devmem.text import nfkc, tokenize
    assert nfkc("caf\u00e9 \ufb01le") == "caf\u00e9 file"
    assert len(nfkc("\ufdfa" * 10_000)) <= 10_000 + 4096
    prompt = "\ufdfa " * 50_000                          # a 100,000-character prompt
    t = time.perf_counter()
    tokenize(prompt)
    assert time.perf_counter() - t < 1.0                  # 2.6 s before (measured 0.08 s after)
