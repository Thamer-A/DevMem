"""Extraction cost is linear: every extractor entry point on adversarial input, for every supported file type.

A scan runs on the first prompt of every session and on MCP get_context_pack, and it reads every project file up to
MAX_TEXT_BYTES (512 KB). Before the fix, one Luau pattern was exponential (a 150-byte file stalled the hook: 4x the
time per two more list elements) and a dozen others were quadratic (16 KB of blank lines took 5-11 s in the rust,
ruby, shell, kotlin, JS and Luau line-start patterns; a 16 KB hex token 6 s in the asset pattern; a run of capitals
6 s in the tokenizer). The corpus below holds long runs of each character class those patterns repeat, blank lines,
one huge line, one huge token and unterminated openers repeated many times; each 512 KB input must finish far below
what a quadratic pattern needs (hours at this size) and well above what the linear code takes (measured: under 1 s).
"""
from __future__ import annotations

import time

import pytest

from devmem.extract import DATA_EXT, LANG_BY_EXT, extract_file, language_of
from devmem.index import doc_terms, file_fields

N = 512_000
BOUND = 15.0     # seconds per 512 KB input, extraction and tokenizing together (measured worst well under 1.5 s)

# a remote-defining prefix, so the Luau extractor also runs its remote-name heuristics on every shape
PREFIX = 'local r = Instance.new("RemoteEvent")\nlocal names = {'


def _rep(unit: str, n: int = N) -> str:
    return (unit * (n // len(unit) + 1))[:n]


SHAPES = {
    "blank-lines": "\n",
    "space-lines": " \n",
    "tab-lines": "\t\n",
    "crlf-lines": "\r\n",
    "one-token": "a",
    "capitals": "A",
    "hex": "0f",
    "dotted": "a.",
    "words": "a ",
    "colons": ":a",
    "quotes": "'",
    "escaped-quotes": "\\'",
    "dquote-open": '"a',
    "backticks": "`",
    "block-open": "/*",
    "lua-block-open": "--[[",
    "lua-long-string": "[[",
    "braces": "{",
    "parens": "(",
    "string-array": "'a' ",
    "array-open": "{'a' ",
    "require-open": "require(",
    "require-local": "local a: ",
    "import-open": "import ",
    "import-brace": "import {",
    "export-brace": "export {",
    "const-brace": "const {",
    "select": "SELECT a ",
    "function": "function ",
    "def": "def ",
    "fun": "fun ",
    "asset-name": "a = 'rbxassetid://",
    "assignment-lines": "a =\n",
    "minified": "a.b(c,d);e=f?g:h;",
}
INPUTS = {name: PREFIX + _rep(unit, N - len(PREFIX)) for name, unit in SHAPES.items()}


def _representatives() -> dict[str, str]:
    """One file name per extraction path: each language, and prose versus data for plain text."""
    reps: dict[str, str] = {}
    for ext in sorted(set(LANG_BY_EXT) | DATA_EXT):
        rel = f"src/sample{ext}"
        lang = language_of(rel)
        key = lang if lang != "text" else ("text-prose" if ext in (".md", ".rst", ".txt") else "text-data")
        reps.setdefault(key, rel)
    reps["luau-server"] = "src/Remotes.server.luau"
    return reps


REPS = _representatives()


def test_every_extension_maps_to_a_representative():
    langs = {language_of(f"x{e}") for e in LANG_BY_EXT}
    assert langs <= set(REPS) and {"text-prose", "text-data"} <= set(REPS)


@pytest.mark.parametrize("kind", sorted(REPS))
def test_extraction_is_fast_on_adversarial_input(kind):
    rel = REPS[kind]
    worst, worst_name = 0.0, ""
    for name, text in INPUTS.items():
        t = time.perf_counter()
        info, _facts = extract_file(rel, text)
        doc_terms(file_fields(rel, info, text))
        dt = time.perf_counter() - t
        if dt > worst:
            worst, worst_name = dt, name
        assert dt < BOUND, f"{rel}: {dt:.1f}s on 512 KB of {name}"
    print(f"{rel}: worst {worst:.2f}s ({worst_name})")


@pytest.mark.parametrize("m", [16, 24, 40, 400])
def test_unclosed_string_array_in_a_remote_module_is_linear(m):
    """The reported reproduction: m=24 took 2.3 s before (4x per two more elements); m=40 would take hours."""
    src = 'local r = Instance.new("RemoteEvent")\nlocal names = {' + "'a' " * m
    t = time.perf_counter()
    extract_file("src/Remotes.luau", src)
    assert time.perf_counter() - t < 0.5


def test_closed_string_array_still_names_remotes():
    src = ('local r = Instance.new("RemoteEvent")\nlocal names = { "Buy", \'Sell\'; "Trade" }\n'
           "for _, n in names do end\n")
    info, _ = extract_file("src/Remotes.luau", src)
    assert info["remotes_defined"] == ["Buy", "Sell", "Trade"]
