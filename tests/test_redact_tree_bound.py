"""Redactor.tree costs what its budget allows, whatever the size of the structure.

Each string it redacts is a pass of every rule; it visited every key of an input, so a tool input with 60,000 keys made
one PostToolUse hook take 7 s (tests/test_hook_payload_sweep.py::many_keys went from 1 s to 7 s).
"""
from __future__ import annotations

import json

import pytest

from devmem import redact
from devmem.redact import Redactor


@pytest.fixture()
def counted(tmp_path, monkeypatch):
    red = Redactor(tmp_path)
    calls = []
    real = red._text

    def counting(s, depth=0):
        calls.append(len(s))
        return real(s, depth)
    monkeypatch.setattr(red, "_text", counting)
    return red, calls


@pytest.mark.parametrize("shape", ["many_keys", "many_values", "nested_lists", "many_empty", "many_lines", "key_lines",
                                   "many_blocks"])
def test_the_walk_stops_when_the_budget_is_used_up(counted, shape):
    red, calls = counted
    big = {
        "many_keys": {f"k{i}": f"v{i}" for i in range(60000)},
        "many_values": [str(i % 10) for i in range(200000)],
        "nested_lists": [[["ab"] * 50] * 50] * 50,
        "many_empty": {"a": [""] * 200000, "b": "kept"},
        "many_lines": {"lines": [f"+line {i}" for i in range(200000)]},          # one run, redacted as one text
        "key_lines": {"lines": ["-----BEGIN RSA PRIVATE KEY-----", "MIIEowIBAAKCAQEA" * 4] * 100000},
        "many_blocks": [{"type": "text", "text": f"block {i}"} for i in range(100000)],
    }[shape]
    out, cut = red.tree(big, 1000)
    assert len(calls) <= 2 * redact.TREE_LEAVES + 10, len(calls)
    if shape == "many_empty":
        assert not cut and out["b"] == "kept"
    else:
        assert cut
        assert len(json.dumps(out)) < 20000


def test_what_fits_the_budget_is_still_redacted_and_in_order():
    red = Redactor(None)
    token = "ghp_" + "a1B2c3D4e5" * 4
    out, cut = red.tree({"first": "plain", "password": "hunter2-Zq9", "note": f"token {token}", "rest": ["x"] * 5000}, 400)
    text = json.dumps(out)
    assert "hunter2-Zq9" not in text and token not in text
    assert list(out)[:3] == ["first", "password", "note"] and out["first"] == "plain"
    assert cut and len(out["rest"]) < 5000


def test_a_small_structure_is_unchanged_by_the_bound():
    red = Redactor(None)
    data = {"a": ["one", "two", {"b": "three", "n": 4, "z": None}], "c": "four"}
    out, cut = red.tree(data, 10000)
    assert out == data and not cut


def test_a_long_key_is_redacted_and_kept_only_in_part(counted):
    red, calls = counted
    key = "password: k9Lm2QzX\n" * 55000          # about 1 MB
    out, _cut = red.tree({key: "v"}, 100_000)
    (k,) = out
    assert max(calls) <= redact.MAX_KEY + redact.CAP_MARGIN + 16, max(calls)
    assert len(k) < redact.MAX_KEY + 100 and "k9Lm2Qz" not in k
    rec = red.record({key: "v"})
    assert len(next(iter(rec))) < redact.MAX_KEY + 100


class _Seen:
    def __init__(self, rx):
        self.rx, self.longest = rx, 0

    def search(self, s, *a):
        self.longest = max(self.longest, len(s))
        return self.rx.search(s, *a)


def test_the_secret_file_check_of_an_input_is_bounded(monkeypatch):
    seen = _Seen(redact._PATH_KEY)
    monkeypatch.setattr(redact, "_PATH_KEY", seen)
    assert not redact.input_names_secret_file({"k" * 5000 + str(i): "v" for i in range(50)})
    assert seen.longest <= redact.MAX_KEY_CHECK
    # more strings than it looks at: counted as naming a secret file (the output is then not kept)
    assert redact.input_names_secret_file({"a": [f"s{i}" for i in range(redact.MAX_INPUT_NODES + 10)]})
    assert not redact.input_names_secret_file({"a": [f"s{i}" for i in range(100)]})
