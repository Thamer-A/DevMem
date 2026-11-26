"""The write boundary's shortcut for runs it already redacted is keyed by the run (verifier v9b item 5a). It used to skip a
run when every item was a string this redactor had produced, and text() remembers what it returns: a key block whose
lines had each been redacted alone (the header alone is a mention, a body line alone is nothing) then arrived joined in
one list and was stored. Now a run is skipped only when its joined text is one this redactor produced (a run redacted
as one text, or the log read back through mark_clean)."""
from __future__ import annotations

from devmem.redact import Redactor

BODY = ["MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC" + "7Zq8rT2vLmN4xK7pW",
        "Qz7xR2mT9vL4kW8pN3bC6dF1gH5jY0sA2eU7iO4rTqWzXcVbN3" + "vB7xQ2mR8tL5kW1p"]
PEM = ["-----BEGIN " + "PRIVATE KEY-----"] + BODY + ["-----END " + "PRIVATE KEY-----"]


def test_a_key_block_seen_line_by_line_first_is_caught_when_it_arrives_joined():
    red = Redactor(None, enabled=True)
    for line in ["context"] + PEM + ["more"]:
        assert red.text(line) == line          # each line alone: nothing to redact
    out = red.record({"lines": ["context"] + PEM + ["more"]})
    assert not any(b[:20] in "\n".join(out["lines"]) for b in BODY)
    assert len(out["lines"]) == len(PEM) + 2


def test_a_run_redacted_as_one_text_is_looked_up_the_second_time():
    red = Redactor(None, enabled=True)
    tree, _cut = red.tree({"lines": ["a"] + PEM + ["b"]}, 1_000_000)
    calls = []
    orig = red.text
    red.text = lambda s: calls.append(s) or orig(s)
    assert red.record(tree) == tree
    assert not any("\n" in c for c in calls)   # the run was not joined and redacted again


def test_runs_read_back_from_the_log_are_known():
    writer = Redactor(None, enabled=True)
    stored = writer.record({"lines": ["a"] + PEM + ["b"], "x": "first", "y": "second"})
    reader = Redactor(None, enabled=True)
    reader.mark_clean(stored)
    calls = []
    orig = reader.text
    reader.text = lambda s: calls.append(s) or orig(s)
    assert reader.record(stored) == stored
    assert not any("\n" in c for c in calls)
