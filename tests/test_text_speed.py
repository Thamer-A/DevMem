"""The tokenizer is linear on every input shape built from its patterns' own alphabets.

Fuzz finding HIGH-1 (FUZZ/r9_camel_redos.py, r10_poisoned_prompt.py): `([A-Z]+)([A-Z][a-z])` made tokenize("A"*n)
quadratic (16,000 capitals 4.9 s, 32,000 22 s; base64 of zero bytes is "AAAA..."), and a pasted blob stored in a
prompt was re-tokenised when sessions were indexed, so it slowed every later session. Bounds are generous: the
linear code takes well under a second on each 200,000-character input here.
"""
from __future__ import annotations

import time

import pytest

from devmem import text as T
from devmem.index import MAX_FIELD_CHARS, doc_terms

N = 200_000
SHAPES = {
    "capitals": "A",
    "capital-pairs": "AB",
    "camel": "aB",
    "mixed-run": "AbC",
    "upper-lower": "AAa",
    "digits": "1",
    "letter-digit": "a1",
    "underscores": "_",
    "snake": "a_",
    "spaces": " ",
    "cjk": "中",
    "hangul": "한",
    "arabic": "ال",
    "marks": "é",
    "base64-zero": "AAAA",
}


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_tokenize_and_count_terms_are_linear(name):
    s = (SHAPES[name] * (N // len(SHAPES[name]) + 1))[:N]
    t = time.perf_counter()
    T.tokenize(s)
    T.count_terms(s)
    assert time.perf_counter() - t < 10.0, name


def test_capital_run_reported_by_the_fuzz_run():
    t = time.perf_counter()
    T.tokenize("A" * 50_000)
    assert time.perf_counter() - t < 2.0   # was about 50 s


def test_camel_split_is_unchanged():
    assert T.tokenize("HTTPServer parseJSONData XMLHttpRequest") == T.tokenize("HTTP Server parse JSON Data XML Http "
                                                                               "Request")


def test_index_fields_are_capped():
    big = "word " * (MAX_FIELD_CHARS // 2)
    t = time.perf_counter()
    tf, length = doc_terms({"prompt": big})
    assert time.perf_counter() - t < 10.0 and length <= MAX_FIELD_CHARS * 2
