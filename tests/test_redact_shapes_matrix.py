"""Every secret of the redaction corpora, in every shape a tool puts text in (verifier round 6: a `password: X` line was
stored when it came as Grep `-n` output, a `git diff` line, `cat -n` output, after a comment marker, with a trailing
comment, or JSON-escaped). Redactor.text normalises a line's tool prefix and trailing comment once (_line_view) and
decodes JSON string literals (_json_strings), so every rule sees each shape; this matrix checks that per shape for every
secret the plain text is caught in. KNOWN lists the misses that remain, per shape: the documentation (docs/PRIVACY.md,
SECURITY.md, the redact.py docstring) claims only what this matrix shows.

Run `python tests/test_redact_shapes_matrix.py` for the table of misses per shape."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import redact_corpus6  # noqa: E402
import redact_corpus7  # noqa: E402
import redact_corpus8  # noqa: E402
import test_redact_patterns as patterns  # noqa: E402
import test_redact_review_lists as review_lists  # noqa: E402
import test_redact_round4 as round4  # noqa: E402
import test_redact_round6 as round6  # noqa: E402

from devmem.redact import Redactor  # noqa: E402


def _cases() -> list[tuple[str, str, str]]:
    out = [(f"r6:{n}", t, s) for n, t, s in round6.CAUGHT]
    out += [(f"r4:{n}", t, s) for n, (t, s) in round4.CAUGHT.items()]
    out += [(f"lists:{n}", t, s) for n, (t, s) in review_lists.CAUGHT.items()]
    out += [(f"patterns:{n}", t, s) for n, (t, s) in patterns.SECRETS.items()]
    out += [(f"c6:{n}", t, s) for n, t, s in redact_corpus6.S]
    out += [(f"c7:{n}", t, s) for n, t, s in redact_corpus7.S]
    out += [(f"c8:{n}", t, s) for n, t, s in redact_corpus8.S]
    return out


def _lines(text: str, f) -> str:
    return "\n".join(f(i, ln) if ln.strip() else ln for i, ln in enumerate(text.split("\n"), 1))


SHAPES = {
    "plain": lambda t: t,
    "json-once": lambda t: json.dumps({"result": t, "status": 200}),
    "json-twice": lambda t: json.dumps({"result": json.dumps({"stdout": t})}),
    "diff-added": lambda t: _lines(t, lambda i, ln: "+" + ln),
    "diff-removed": lambda t: _lines(t, lambda i, ln: "-" + ln),
    "diff-context": lambda t: _lines(t, lambda i, ln: " " + ln),
    "grep-n": lambda t: _lines(t, lambda i, ln: f"config/app.yml:{i}:{ln}"),
    "cat-n": lambda t: _lines(t, lambda i, ln: f"{i:6d}\t{ln}"),
    "read-tool": lambda t: _lines(t, lambda i, ln: f"{i:6d}→{ln}"),
    "commented": lambda t: _lines(t, lambda i, ln: "# " + ln),
    "trailing-comment": lambda t: _lines(t, lambda i, ln: ln + "  # prod"),
    "md-code-block": lambda t: _lines(t, lambda i, ln: "    " + ln),
    "md-blockquote": lambda t: _lines(t, lambda i, ln: "> " + ln),
    # one string per line (Edit/Write `structuredPatch[].lines`, a notebook's `source`, MCP content blocks): as JSON text
    # and as the structure a hook or stream capture walks (Redactor.tree, checked as the JSON it writes)
    "lines-json": lambda t: json.dumps({"lines": t.split("\n")}),
    "patch-lines-json": lambda t: json.dumps({"structuredPatch": [{"lines": [" x"] + ["+" + ln for ln in t.split("\n")]}]}),
    "notebook-source-json": lambda t: json.dumps({"source": [ln + "\n" for ln in t.split("\n")]}),
    "content-blocks-json": lambda t: json.dumps({"content": [{"type": "text", "text": ln} for ln in t.split("\n")]}),
    # terminal colours: `git diff --color`, `grep --color=always` (a coloured prefix, a highlighted match mid-line)
    "ansi-diff": lambda t: _lines(t, lambda i, ln: "\x1b[32m+" + ln + "\x1b[m"),
    "ansi-grep": lambda t: _lines(t, lambda i, ln: f"\x1b[35mconfig/app.yml\x1b[m\x1b[36m:\x1b[m\x1b[32m{i}\x1b[m"
                                                   f"\x1b[36m:\x1b[m{ln[:len(ln) // 2]}\x1b[01;31m\x1b[K"
                                                   f"{ln[len(ln) // 2:]}\x1b[m\x1b[K"),
    "ansi-midline": lambda t: _lines(t, lambda i, ln: ln[:len(ln) // 3] + "\x1b[1;33m" + ln[len(ln) // 3:] + "\x1b[0m"),
    "lines-tree": lambda t: {"lines": t.split("\n")},
    "patch-lines-tree": lambda t: {"structuredPatch": [{"lines": [" x"] + ["+" + ln for ln in t.split("\n")]}]},
    "content-blocks-tree": lambda t: {"content": [{"type": "text", "text": ln} for ln in t.split("\n")]},
}


def _red(red: Redactor, x) -> str:
    """The text as stored: Redactor.text of a text, the JSON of Redactor.tree of a structure."""
    return red.text(x) if isinstance(x, str) else json.dumps(red.tree(x, 10_000_000)[0])

# misses that remain, per shape (name of the corpus case)
KNOWN: dict[str, set[str]] = {shape: set() for shape in SHAPES}


def _missed(red: Redactor, text: str, secret: str) -> bool:
    forms = {secret, json.dumps(secret)[1:-1], json.dumps(json.dumps(secret)[1:-1])[1:-1]}
    return any(f in text for f in forms if len(f) >= 4)


def matrix() -> tuple[dict[str, list[str]], int]:
    red = Redactor(None)
    cases = [c for c in _cases() if not _missed(red, red.text(c[1]), c[2])]   # caught as plain text
    out = {}
    for shape, f in SHAPES.items():
        out[shape] = [n for n, t, s in cases if _missed(red, _red(red, f(t)), s)]
    return out, len(cases)


_RESULT: dict = {}


def _result():
    if not _RESULT:
        _RESULT["m"], _RESULT["n"] = matrix()
    return _RESULT["m"]


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_every_secret_caught_as_plain_text_is_caught_in_every_shape(shape):
    missed = set(_result()[shape])
    assert missed <= KNOWN[shape], sorted(missed - KNOWN[shape])
    assert not KNOWN[shape] - missed, f"now caught, remove from KNOWN: {sorted(KNOWN[shape] - missed)}"


# the corpus secrets as the value of a secret name in the wrappers config tools use (round 11): a YAML flow mapping (a
# compose `environment: {...}`, an Ansible `- { ... }` item, a list of maps) and a name/value pair (Kubernetes `env`, flow
# and block). A value a plain scalar cannot hold (blanks, `,[]{}#`, a leading quote or indicator) is single-quoted, as
# YAML writes it; values shorter than six characters or on several lines are not such values.
def _yaml_scalar(v: str) -> str:
    plain = not any(c in v for c in " \t,[]{}#'\"") and v[0] not in "!&*|>%@`-?:"
    return v if plain else "'" + v.replace("'", "''") + "'"


VALUE_SHAPES = {
    "flow-compose": lambda v: "environment: {POSTGRES_USER: app, POSTGRES_PASSWORD: %s}" % _yaml_scalar(v),
    "flow-ansible": lambda v: "  - { name: alice, db_password: %s, groups: wheel }" % _yaml_scalar(v),
    "flow-list": lambda v: "users: [{name: a, api_token: %s}, {name: b}]" % _yaml_scalar(v),
    "flow-multiline": lambda v: "cfg: {user: admin,\n  client_secret: %s,\n  port: 5}" % _yaml_scalar(v),
    "pair-flow": lambda v: "env: [{name: DB_PASSWORD, value: %s}]" % _yaml_scalar(v),
    "pair-block": lambda v: "env:\n  - name: DB_PASSWORD\n    value: %s\n" % _yaml_scalar(v),
    # a compose healthcheck: the command after `CMD` in an exec list, and a shell string after `CMD-SHELL`
    "healthcheck-cmd": lambda v: "    healthcheck:\n      test: [\"CMD\", \"redis-cli\", \"-a\", %s, \"ping\"]\n" % json.dumps(v),
    "healthcheck-shell": lambda v: "    healthcheck:\n      test: [\"CMD-SHELL\", %s]\n" % json.dumps(
        "redis-cli -a '" + v.replace("'", "'\\''") + "' ping"),
}
# misses per value shape: three corpus "secrets" are fragments that are no value alone (`dckr_pat_` and `lin_api_` are
# token prefixes; the age key's checked part is all capitals, a documented gap of letters-only values), and a quoted
# phrase with blanks is a value only under a password, passphrase or secret name (not `api_token`) and not in a
# name/value pair (documented: a value with blanks)
FRAGMENTS = {"r4:docker hub pat", "r4:linear key", "r4:age secret key"}
VALUE_KNOWN: dict[str, set[str]] = {
    "flow-compose": FRAGMENTS, "flow-ansible": FRAGMENTS, "flow-multiline": FRAGMENTS,
    "flow-list": FRAGMENTS | {"r6:quoted passphrase"},
    "pair-flow": set(), "pair-block": set(),
    "healthcheck-cmd": set(), "healthcheck-shell": set(),
}


def value_matrix() -> tuple[dict[str, list[str]], int]:
    red = Redactor(None)
    cases = [(n, s) for n, t, s in _cases() if len(s) >= 6 and "\n" not in s and s == s.strip()
             and not _missed(red, red.text(t), s)]
    return {shape: [n for n, s in cases if _missed(red, red.text(f(s)), s)] for shape, f in VALUE_SHAPES.items()}, \
        len(cases)


@pytest.mark.parametrize("shape", sorted(VALUE_SHAPES))
def test_every_secret_value_is_caught_under_a_secret_name_in_flow_maps_and_pairs(shape):
    if "v" not in _RESULT:
        _RESULT["v"], _RESULT["vn"] = value_matrix()
    missed = set(_RESULT["v"][shape])
    assert missed <= VALUE_KNOWN[shape], sorted(missed - VALUE_KNOWN[shape])
    assert not VALUE_KNOWN[shape] - missed, f"now caught, remove from VALUE_KNOWN: {sorted(VALUE_KNOWN[shape] - missed)}"


def test_the_corpora_are_large():
    _result()
    assert _RESULT["n"] >= 250, _RESULT["n"]


def test_benign_text_stays_intact_in_every_shape():
    red = Redactor(None)
    benign = [t for c in (redact_corpus6, redact_corpus7, redact_corpus8) for _n, t in c.B if red.text(t) == t]
    benign += [t for t in round6.KEPT if red.text(t) == t]
    changed = []
    for shape, f in SHAPES.items():
        for t in benign:
            s = f(t)
            if _red(red, s) != (s if isinstance(s, str) else json.dumps(s)):
                changed.append(f"{shape}: {t[:60]!r}")
    assert not changed, changed


if __name__ == "__main__":
    m, n = matrix()
    print(f"{n} secrets caught as plain text; misses per shape:")
    for shape, names in m.items():
        print(f"  {shape:16s} {len(names):3d} {names}")
