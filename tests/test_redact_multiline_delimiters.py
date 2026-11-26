"""A multi-line string under a secret name keeps both delimiters (verifier v9b item 5b). The TOML/Python rule emptied
the value in place, then the whole-line rule took the opening line `password = \"\"\"[REDACTED:assignment]` for a bare
value and replaced it again, so the opening delimiter was lost and `tomllib` could not parse the result; a one-line
triple-quoted value lost two quotes on each side (the quoted-line rule read it as a value holding its own quote)."""
from __future__ import annotations

import pytest

from devmem.redact import Redactor

V = "Xk4" + "pQ9" + "Lm2zT8"


@pytest.mark.parametrize("q", ['"""', "'''"])
def test_a_toml_multiline_string_keeps_its_delimiters_and_parses(q):
    text = f'[db]\npassword = {q}\n{V}\n{q}\nhost = "db"\n'
    out = Redactor(None, enabled=True).text(text)
    assert V not in out and out.count("\n") == text.count("\n")
    assert out.startswith(f"[db]\npassword = {q}[REDACTED:assignment]\n")
    tomllib = pytest.importorskip("tomllib")       # (Python 3.11+)
    assert tomllib.loads(out)["db"]["host"] == "db"


@pytest.mark.parametrize("text,want", [
    ('PASSWORD = """\n' + V + '\n"""\nx = 1', 'PASSWORD = """[REDACTED:assignment]\n\n"""\nx = 1'),
    ('password = """\n  ' + V + '\n  """', 'password = """[REDACTED:assignment]\n\n  """'),
    ('password = """' + V + '"""', 'password = """[REDACTED:assignment]"""'),
    ("db_password = '''~6QJH#ENo5;jEP'HyS6n'''", "db_password = '''[REDACTED:assignment]'''"),   # a quote inside
])
def test_python_triple_quoted_values_keep_their_delimiters(text, want):
    assert Redactor(None, enabled=True).text(text) == want
