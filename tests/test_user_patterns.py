"""R2: the regexes of `<store>/redact` run only when this user approved that exact file, and only after a lint.

The file's patterns ran unbounded, and a tracked `.devmemory/redact` arriving with `git pull` after setup was used
without approval: `(a+)+$` against 28 `a`s made one hook take 23 s (verify3/redact/). The switch outside the project
now records the file's SHA-256 when the user runs `devmem init` or /devmem:setup; a different file is not run, and
`devmem doctor` says how to approve it. Approved patterns with a repeat inside an unbounded repeat are refused.
"""
from __future__ import annotations

import time

import pytest
from cli_helpers import run_cli
from hooks_helpers import make_project

from devmem import consent
from devmem.redact import Redactor, lint_pattern, user_patterns_state


def test_a_redact_file_that_arrives_after_setup_is_not_run(tmp_path):
    project = make_project(tmp_path)                      # switched on (devmem init) with no redact file
    store = project / ".devmemory"
    (store / "redact").write_text("(a+)+$\nCUSTOM-[0-9]+\n", encoding="utf-8")     # as if a git pull brought it
    red = Redactor(store, project=project)
    assert red.user == []
    t = time.perf_counter()
    assert red.text("a" * 28 + "!") == "a" * 28 + "!"
    assert time.perf_counter() - t < 1
    assert "CUSTOM-12" in red.text("CUSTOM-12")
    out = run_cli(["doctor"], cwd=project).stdout.decode("utf-8", "replace")
    assert "not approved" in out and "devmem init" in out, out


def test_after_approval_good_patterns_run_and_nested_repeats_are_refused(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    (store / "redact").write_text("(a+)+$\nCUSTOM-[0-9]+\n", encoding="utf-8")
    consent.enable_init(project)                           # the user looks at it and sets the project up again
    pats, problems = user_patterns_state(store, project)
    assert [p.pattern for p in pats] == ["CUSTOM-[0-9]+"]
    assert any("(a+)+$" in p and "refused" in p for p in problems)
    red = Redactor(store, project=project)
    assert "CUSTOM-12" not in red.text("key CUSTOM-12")
    (store / "redact").write_text("CUSTOM-[0-9]+\nOTHER\n", encoding="utf-8")   # changed again: not approved
    assert Redactor(store, project=project).user == []


def test_an_old_switch_without_the_field_approves_nothing(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    (store / "redact").write_text("CUSTOM-[0-9]+\n", encoding="utf-8")
    m = consent.init_marker(project)
    m.write_text('{"project": "x", "allow_unredacted": false}', encoding="utf-8")   # written by an older devmem
    assert Redactor(store, project=project).user == []


@pytest.mark.parametrize("pat", ["(a+)+$", "(a|aa)*b", "(?:x*)*", "(a*b?)+", "((ab)+)+", "(?=(a+)+)", "x{1,3}(y+)*"])
def test_nested_repeats_are_refused(pat):
    assert lint_pattern(pat)


@pytest.mark.parametrize("pat", ["CUSTOM-[0-9]+", r"\bsk_[A-Za-z0-9]{20,}", "(?:abc|def){2}", "a{2,5}b+", "x" * 10])
def test_ordinary_patterns_pass(pat):
    assert lint_pattern(pat) is None


def test_long_patterns_and_too_many_are_refused():
    assert lint_pattern("a" * 501)
