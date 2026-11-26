"""Item 6 of verifier round v8b: an input over the secret-file check's bounds is still dropped (fail closed) but says
so truthfully, and finding the closing brackets a value does not open is one pass."""
from __future__ import annotations

import time

from devmem import sessions as S
from devmem.redact import MAX_INPUT_NODES, Redactor, _unbalanced_closers


def test_an_input_too_large_to_check_is_dropped_with_a_truthful_label(tmp_path):
    red = Redactor(None, enabled=True)
    big = {"rows": [{"a": "x", "b": "y", "c": "z"} for _ in range(MAX_INPUT_NODES // 3 + 10)]}
    e = S.tool_entries("mcp__db__insert", big, tmp_path, red)[0]
    # still fail-closed (nothing of it is kept), and labelled as what it is (sixth review): not a secret file's call
    assert e["input_not_checked"] is True and "secret_file" not in e and "input_summary" not in e
    assert "values" in e["reason"] and e["chars"] > 0
    e = S.tool_entries("mcp__fs__write_file", {"path": ".env", "content": "A=1"}, tmp_path, red)[0]
    assert e["input_summary"] == "[OMITTED: input names a secret file]"
    deep: dict = {}
    node = deep
    for _ in range(400):
        node["x"] = {}
        node = node["x"]
    e = S.tool_entries("mcp__x__y", deep, tmp_path, red)[0]
    assert e["input_not_checked"] is True and "input_summary" not in e
    e = S.tool_entries("mcp__x__y", {"query": "select 1"}, tmp_path, red)[0]
    assert "OMITTED" not in e["input_summary"]


def test_unbalanced_closers_is_one_pass():
    assert _unbalanced_closers("abc)") == ")"
    assert _unbalanced_closers("k3j(x)") == ""
    assert _unbalanced_closers("a(b)])}") == "])}"
    assert _unbalanced_closers("") == "" and _unbalanced_closers("abc") == ""
    value = "abc123" + ")" * 4090
    t = time.perf_counter()
    for _ in range(200):
        assert len(_unbalanced_closers(value)) == 4090
    assert time.perf_counter() - t < 1.0     # recounting for every closer took about 2 s here
