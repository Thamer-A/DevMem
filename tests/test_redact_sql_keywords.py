"""`SET PASSWORD` and `IDENTIFIED BY` need the real construct (verifier v7b, d1_damage).

The rule for a quoted value after a name and a blank took any word starting with "set" or "identified" as the SQL
keyword, so `SETTINGS_FILE = "config/settings.yaml"`, every `set "DM_X=..."` line of devmem.cmd, `setup = '_setup()'`
and docstrings such as `setting 'name'` were redacted; the unquoted Oracle form caught prose ("identified by INDEX").
"""
from __future__ import annotations

import pytest

from devmem.redact import Redactor

KEPT = [
    'SETTINGS_FILE = "config/settings.yaml"',
    'SETTINGS_LOCAL = "settings.local.json"',
    'set "DM_PYTHON=%~dp0python.exe"',
    "setup = '_setup()'",
    "The setting 'max_retries' controls how often a request is retried.",
    "setx PATH 'C:/tools/bin'",
    "Each row is identified by INDEX in the table.",
    "the user is identified by UID and the group by GID",
    "Users are identified by four attributes.",
    "set -- 'positional-args'",
    "settings 'pathname_of_repo'",
]
CAUGHT = [
    ("SET PASSWORD FOR 'app'@'%' = 'Xk4pQ9Lm2zT';", "Xk4pQ9Lm2zT"),
    ("SET PASSWORD = PASSWORD('Xk4pQ9Lm2zT');", "Xk4pQ9Lm2zT"),
    ("ALTER USER app IDENTIFIED BY 'Xk4pQ9Lm2zT';", "Xk4pQ9Lm2zT"),
    ("CREATE USER app IDENTIFIED WITH mysql_native_password BY 'Xk4pQ9Lm2zT';", "Xk4pQ9Lm2zT"),
    ("CREATE USER scott IDENTIFIED BY Xk4pQ9Lm2zT;", "Xk4pQ9Lm2zT"),
    ("ALTER USER scott IDENTIFIED BY tiger42x ACCOUNT UNLOCK;", "tiger42x"),
    ("storePassword 'Xk4pQ9Lm2zT'", "Xk4pQ9Lm2zT"),
    ('client.setPassword("Xk4pQ9Lm2zT");', "Xk4pQ9Lm2zT"),
    ("cfg.set_api_key('Xk4pQ9Lm2zT9aB')", "Xk4pQ9Lm2zT9aB"),
]


@pytest.mark.parametrize("text", KEPT)
def test_words_that_only_start_like_a_keyword_are_kept(text):
    # a text that also holds a secret stem, so the rules are not skipped by _NEEDS
    full = text + "\n# the token path"
    assert Redactor(None).text(full) == full


@pytest.mark.parametrize("text, secret", CAUGHT)
def test_the_real_constructs_are_still_caught(text, secret):
    assert secret not in Redactor(None).text(text)
