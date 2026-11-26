"""User patterns have a runtime bound, not only a lint (verifier round 5: `(?:a*){40}b` (over 20 s on 30 characters),
`(?:a|b?){30}c`, `(a?){25}a{25}`, `\\w*\\w*\\w*\\w*\\w*!` and `(\\w+)\\1+!` all passed lint_pattern). Each approved pattern is
measured once in a child process killed at PROBE_TIMEOUT_S; a slow one is not used and `devmem status`/`doctor` list it
(user_patterns_state's problems). At run time a user pattern sees windows of USER_PATTERN_FRAGMENT characters only."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import consent
from devmem import redact as R

SLOW = [r"(?:a*){40}b", r"(?:a|b?){30}c", r"\w*\w*\w*\w*\w*!", r"(\w+)\1+!"]


@pytest.mark.parametrize("pat", SLOW)
def test_the_lint_passes_these_but_the_measurement_does_not(pat):
    assert R.lint_pattern(pat) is None            # the lint cannot see them
    t = time.perf_counter()
    why = R.probe_patterns([pat])[pat]
    assert why and "took more than" in why
    assert time.perf_counter() - t < R.PROBE_TIMEOUT_S + 8      # killed at the timeout (plus process start)


def test_a_slow_pattern_is_not_used_and_status_says_so(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    (store / "redact").write_text("(?:a*){40}b\nACME-[0-9]{6}\n", encoding="utf-8")
    consent.enable_init(project)                  # approval measures the patterns (consent.write_marker)
    cache = Path(os.environ["DEVMEM_USER_DATA"]) / R._PROBE_FILE
    assert cache.is_file()                        # verdicts kept outside the project
    t = time.perf_counter()
    pats, problems = R.user_patterns_state(store, project)
    assert time.perf_counter() - t < 1.0          # the hooks find the verdicts: no measurement on this path
    assert [p.pattern for p in pats] == ["ACME-[0-9]{6}"]
    assert any("(?:a*){40}b" in p and "not used" in p for p in problems), problems
    red = R.Redactor(store, project=project)
    t = time.perf_counter()
    assert red.text("a" * 30 + " ACME-123456") == "a" * 30 + " [REDACTED:custom]"
    assert time.perf_counter() - t < 1.0


def test_a_project_cannot_vouch_for_a_pattern(tmp_path):
    """The verdicts live in the per-user folder; a file in the store with the same name means nothing."""
    project = make_project(tmp_path)
    store = project / ".devmemory"
    (store / "redact").write_text("(?:a|b?){30}c\n", encoding="utf-8")
    (store / R._PROBE_FILE).write_text('{"version": 1}', encoding="utf-8")
    consent.enable_init(project)
    assert R.user_patterns_state(store, project)[0] == []


def test_windowed_matching_finds_every_match_in_a_long_text():
    rx = R.re.compile(r"ACME-[0-9]{6}")
    n = R.USER_PATTERN_FRAGMENT
    step = n - R._FRAGMENT_OVERLAP
    # matches at the start, across every window edge, inside overlaps, and at the very end
    positions = sorted({0, step - 5, step + 8, n - 4, 2 * step - 6, 2 * n - 3, 3 * step, 5 * n})   # 11+ apart
    s = list("x" * (5 * n + 20))
    for p in positions:
        s[p:p + 11] = "ACME-123456"
    text = "".join(s)
    out, k = R._windowed_subn(rx, text, "[R]")
    assert "ACME-" not in out and "123456" not in out
    assert out == rx.sub("[R]", text)              # the same result as an unwindowed run
    assert k == len(rx.findall(text))


def test_windowed_matching_on_lines_matches_plain_subn():
    rx = R.re.compile(r"(?m)^token: \w+$")
    text = "\n".join(f"token: v{i}" if i % 7 == 0 else f"line {i} " + "y" * (i % 300) for i in range(3000))
    assert R._windowed_subn(rx, text, "[R]")[0] == rx.sub("[R]", text)
