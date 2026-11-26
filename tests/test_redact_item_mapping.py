"""Sibling strings redacted as one text and cut back item by item (verifier v8b: f1_join_fuzz, f2_folds, f3_shift,
f5_cache, f6_bounds, f8_record_raw).

Three properties: the write boundary (Redactor.record) joins a run of raw strings even when some of its items are
strings the redactor has seen before (a short item such as "", " " or "}" is remembered from any earlier run); a
redaction that spans lines keeps the text's line count, so a run is cut back by position; and where a line count still
changes (a user pattern), no text moves from one item into another.
"""
from __future__ import annotations

import json

import pytest

from devmem.redact import Redactor

BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
END = "-----END " + "PRIVATE KEY-----"
BODY = ["MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC" + "7Zq8rT2vLmN4xK7pW",
        "Qd5Fg0Hj2Kl4Mn6Op8Qr0St2Uv4Wx6Yz8Ab0Cd2Ef4Gh6Ij8Kl0Mn2Op4Qr6St8",
        "Uv0Wx2Yz4Ab6Cd8Ef0Gh2Ij4Kl6Mn8Op0Qr2St4Uv6Wx8Yz0Ab2Cd4Ef6Gh8Ij0="]
PW = "Xk4" + "pQ9" + "Lm2zT8"
PEM = [BEGIN, *BODY, END]
YAML = ["db:", "  password: |", "    " + PW, ""]


def _leaks(x) -> list[str]:
    d = json.dumps(x)
    return [s for s in BODY + [PW] if s in d]


# ---- item 3: the write boundary joins a run unless every item of it is known clean


def test_record_joins_a_run_although_an_item_is_remembered():
    for first in (None, ["a", "", "b"], ["x", "}"], "db:", "}", " "):
        red = Redactor(None, enabled=True)
        if first is not None:
            red.record(first)
        assert not _leaks(red.record(PEM + ["}"])), first
        assert not _leaks(red.record({"v": YAML})), first
        assert not _leaks(red.record([" ", *("+" + x for x in PEM), " "])), first


def test_record_of_a_multi_hunk_patch_joins_every_hunk():
    """One record() call: hunk 1's context line " " is remembered when hunk 2 is redacted."""
    red = Redactor(None, enabled=True)
    patch = {"structuredPatch": [{"lines": [" ", "-a", "+b", " "]},
                                 {"lines": [" ", *("+" + x for x in PEM), " ", "+  password: |", "+    " + PW, " "]}]}
    out = red.record(patch)
    assert not _leaks(out)
    assert [len(h["lines"]) for h in out["structuredPatch"]] == [4, 10]


def test_write_record_raw_line_after_an_earlier_text():
    from devmem import sessions as S
    red = Redactor(None, enabled=True)
    red.text("}")
    line = json.dumps({"type": "user", "tool_use_result": {"structuredPatch": [{"lines": ["+" + x for x in PEM] + ["}"]}]}})
    assert not _leaks(S._redact_line(line, red))


def test_a_run_of_known_clean_items_is_looked_up():
    red = Redactor(None, enabled=True)
    out, _cut = red.tree({"lines": ["+" + x for x in PEM] + [" ok"]}, 100_000)
    calls = []
    orig = red.text
    red.text = lambda s: calls.append(s) or orig(s)      # type: ignore[method-assign]
    assert red.record(out) == out and not calls


# ---- item 2: a redaction that spans lines keeps the line count; items never take another item's text

PPK = ["PuTTY-User-Key-File-3: ssh-rsa", "Encryption: none", "Comment: k", "Public-Lines: 1", "AAAAB3NzaC1yc2EAAAADAQAB",
       "Private-Lines: 3", BODY[0], BODY[1], BODY[2], "Private-MAC: " + "ab" * 32]
FOLDS = {
    "xml": ["<cfg>", "  <password>", "    " + PW, "  </password>", "</cfg>"],
    "xml-value-then-close": ["<cfg>", "  <password>" + PW, "  </password>", "</cfg>"],
    "ppk": PPK,
    "toml": ["[db]", 'password = """', PW, '"""', 'host = "db"'],
    "toml-literal": ["[db]", "password = '''", PW, "'''", "host = 'db'"],
    "yaml-block": ["db:", "  password: |", "    " + PW, "    " + PW, "  host: db"],
    "pem": PEM,
}


def _multi_line_corpus() -> list[tuple[str, str]]:
    import test_redact_shapes_matrix as matrix
    import redact_join_fuzz as fuzz
    import random
    out = [(n, t) for n, t, _s in matrix._cases() if "\n" in t]
    out += [(f"fold:{n}", "\n".join(v)) for n, v in FOLDS.items()]
    rng = random.Random(5)
    out += [(f"fuzz:{k}", "\n".join(fuzz.construct(rng, k)[0])) for k in fuzz.KINDS]
    return out


def test_every_multi_line_secret_keeps_the_line_count():
    red = Redactor(None, enabled=True)
    cases = _multi_line_corpus()
    assert len(cases) > 30
    bad = []
    for name, text in cases:
        for deco in ("", "+", " ", "-", "> "):
            t = "\n".join(deco + ln for ln in text.split("\n"))
            r = red.text(t)
            if r.count("\n") != t.count("\n"):
                bad.append((name, deco, t.count("\n") + 1, r.count("\n") + 1))
    assert not bad, bad


def test_ppk_lines_are_emptied_and_the_mac_line_stays():
    out = Redactor(None, enabled=True).text("\n".join(PPK))
    lines = out.split("\n")
    assert len(lines) == len(PPK) and lines[5] == "Private-Lines: 3"
    assert lines[6] == "[REDACTED:private-key]" and lines[7:9] == ["", ""]
    assert lines[9] == PPK[9]                       # was `[REDACTED:private-key]-MAC: ...`
    assert not any(b in out for b in BODY)


def test_items_after_a_secret_keep_their_positions():
    """f3_shift: two changed lines in a row before a multi-line element moved the next items down two slots."""
    gh = "ghp_" + "Zq8rT2vLmN4xK7pW1sY9bC3dF6gH0jA5eR2u"
    lines = [" <config>", f"+  <apiKey>{gh}</apiKey>", "+  <token>sk_" + "live_51Hq7RzK2mT9vL4xW8pN3bC6dF1gH5jY0sA2eU7</token>",
             "+  <host>db.internal</host>", "+  <password>", "+    " + PW, "+  </password>", "+  <port>5432</port>", " </config>"]
    red = Redactor(None, enabled=True)
    for out in (red.tree({"lines": lines}, 100_000)[0]["lines"], Redactor(None, enabled=True).record(list(lines)),
                json.loads(Redactor(None, enabled=True).text(json.dumps({"lines": lines})))["lines"]):
        assert len(out) == len(lines)
        assert out[0] == lines[0] and out[3] == lines[3] and out[7:] == lines[7:]
        assert out[1] == "+  <apiKey>[REDACTED:github-token]</apiKey>"
        assert out[2] == "+  <token>[REDACTED:stripe-key]</token>"
        assert out[4] == "+  <password>[REDACTED:assignment]" and out[5] in ("", "+") and out[6] == "+  </password>"
        assert PW not in json.dumps(out)


def test_a_user_pattern_that_folds_lines_moves_no_text_across_items():
    """A user pattern can span lines: its replacement keeps the line breaks it replaced (verifier v9b item 2), so the
    items it ran through become the marker and empty strings, and the others keep their text and their positions."""
    red = Redactor(None, enabled=True, extra=[r"BEGIN-X\n[^\n]*\nEND-X"])
    items = ["a", "  password: " + PW, "b", "BEGIN-X", "middle", "END-X", "c", "d"]
    for out in (red.record(list(items)), red.joined(list(items)), red.tree({"v": list(items)}, 100_000)[0]["v"]):
        assert len(out) == len(items)
        assert out[0] == "a" and out[2] == "b" and out[6:] == ["c", "d"]
        assert out[3:6] == ["[REDACTED:custom]", "", ""]
        assert PW not in json.dumps(out) and "middle" not in json.dumps(out) and "[REDACTED:lines]" not in out


def test_a_cut_after_a_fold_keeps_only_marked_or_positional_items():
    red = Redactor(None, enabled=True, extra=[r"BEGIN-X\n[^\n]*\nEND-X"])
    items = ["BEGIN-X", "middle", "END-X"] + [f" line {i}" for i in range(200)]
    for budget in range(40, 900, 23):
        out, cut = red.tree({"v": list(items)}, budget)
        got = out.get("v", [])
        assert "middle" not in json.dumps(got)
        for i, o in enumerate(got[3:], 3):        # a kept line is its own item's text (or a cut-off start of it)
            assert o == "[REDACTED:lines]" or items[i].startswith(o), (budget, i, o)


def test_split_items_by_position_when_the_count_is_kept():
    from devmem.redact import _join_items, _split_items
    items = ["a", "b\nc", "d\n", "e"]
    joined, layout = _join_items(items)
    assert _split_items(layout, joined, joined.replace("c", "[R]"))[0] == ["a", "b\n[R]", "d\n", "e"]
    # a cut red text whose redaction kept the count: by position, the items after the cut get nothing
    assert _split_items(layout, joined, "a\nb\n[R", cut=True, kept=True) == (["a", "b\n[R", "", ""], 2)
    # count changed and cut: the changed run is marked, nothing after `red` is taken
    out, used = _split_items(layout, joined, "a\nX", cut=True, kept=False)
    assert out == ["a", "[REDACTED:lines]", "", ""] and used == 2


@pytest.mark.parametrize("seed", [11, 12])
def test_join_fuzz_no_leak_and_no_benign_item_changed(seed):
    """The verifier's f1_join_fuzz (round v8b) with fixed seeds: before the fix, seed 12 changed a benign item in 4% of
    the cases in every shape."""
    import redact_join_fuzz as fuzz
    stats, examples = fuzz.run(250, seed)
    assert stats == {}, (stats, [repr(e)[:600] for e in examples[:3]])


def test_multi_line_secrets_next_to_the_tree_limits():
    """The verifier's f6_bounds: a key block or a YAML block scalar planted next to TREE_LEAVES and across character
    budgets; nothing of it survives, and every kept benign item is its own text (or the cut-off start of it)."""
    from devmem.redact import TREE_LEAVES
    yaml = ["+db:", "+  password: |", "+    " + PW, "+  host: db"]
    pem = ["+" + x for x in PEM]
    for block in (pem, yaml):
        for pos in (TREE_LEAVES - 6, TREE_LEAVES - 1, TREE_LEAVES, 0, 500):
            lines = [f" line {i}" for i in range(1100)]
            lines[pos:pos] = block
            for budget in (10_000, 15_000, 50_000, 10_000_000):
                tree, _cut = Redactor(None, enabled=True).tree({"structuredPatch": [{"lines": lines}]}, budget)
                out = tree["structuredPatch"][0]["lines"] if tree.get("structuredPatch") else []
                assert not _leaks(tree), (pos, budget)
                for i, (a, b) in enumerate(zip(lines, out)):
                    if a.startswith(" line"):
                        assert b == a or (i == len(out) - 1 and a.startswith(b)), (pos, budget, i, a, b)
