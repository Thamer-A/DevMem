"""Damage on ordinary documents (verifier v12b): a Markdown guide that mentions `api_key = \"\"\"` inline and a
cloud-init file whose list items start with a `password: |` block. Redaction may empty the block's own line; it may not
remove prose or sibling keys (before the fix: 304 lines of the guide, 104 of the 114 cloud-init lines)."""
from __future__ import annotations

from pathlib import Path

import pytest

from devmem.redact import Redactor

DOCS = Path(__file__).resolve().parent / "redact_benign_docs"


def _changed(name: str) -> list[tuple[int, str, str]]:
    text = (DOCS / name).read_text(encoding="utf-8")
    out = Redactor(None).text(text)
    a, b = text.split("\n"), out.split("\n")
    assert len(a) == len(b)
    return [(i, x, y) for i, (x, y) in enumerate(zip(a, b)) if x != y]


def test_the_guide_is_unchanged():
    assert _changed("guide.md") == []


def test_cloud_init_keeps_every_sibling_key():
    changed = _changed("cloud-init-users.yaml")
    assert changed and all(x.strip().startswith("$6$") for _i, x, _y in changed), changed[:3]


LONGCODE = "def load(path):\n    with open(path) as f:\n        return f.read()\n\nclass Config:\n    pass\n" * 80


@pytest.mark.parametrize("text", [
    'TOKEN_HELP = """\nUse a token from the settings page.\n"""\n',
    'TOKEN_HELP = """\n' + LONGCODE + '"""\n',
    'PASSWORD_PROMPT = """\nPlease enter your password\n"""\n',
    'SECRET_KEY_DOC = """\nDescribes the secret key setting\n"""\n',
    'password = """\n' + LONGCODE + 'def g():\n    """Doc"""\n',
    "Set `api_key = '''` in the file.\n\n" + "Plain documentation line without any quote marks here\n" * 120
    + "```\nx = '''y'''\n```\n",
    "<token>This is a note about tokens</token>",
    "<token><![CDATA[This is a note about tokens]]></token>",
])
def test_kept(text):
    assert Redactor(None).text(text) == text


def test_a_list_item_block_ends_at_its_siblings():
    text = "items:\n  - secret: |\n      Xk4Lm2Qp9Rt7\n" + "".join(f"    key{i}: value{i}\n" for i in range(100)) + "  - name: b\n"
    out = Redactor(None).text(text)
    assert "Xk4Lm2Qp9Rt7" not in out
    assert all(f"    key{i}: value{i}\n" in out for i in range(100))


def test_code_under_a_password_triple_quote_within_4_kb_is_redacted():
    # the triple-quote rule declines only a clearly prose body (three or more lines of four or more words, no code
    # punctuation): code under `password = """` closed within 4 KB is redacted, an accepted and documented cost
    text = 'password = """\n' + LONGCODE[:1000] + 'def g():\n    """Doc"""\n'
    assert "def load" not in Redactor(None).text(text)
    prose = ('password = """\nThis is the text that the help page shows\nto every user who opens the settings\n'
             'and it has no secret in it at all\n"""\n')
    assert Redactor(None).text(prose) == prose
    two = 'password = """\ncorrect horse battery staple\nsecond line of it\n"""\n'
    assert "horse" not in Redactor(None).text(two)


@pytest.mark.parametrize("name", ["guide.md", "cloud-init-users.yaml"])
def test_capped_views_of_the_documents_lose_nothing_benign(name):
    """Every cap from 250 characters to the file's length, every 250: the capped text equals the text's own redaction
    cut to the same length (the largest loss, in characters, is 0)."""
    text = (DOCS / name).read_text(encoding="utf-8")
    red = Redactor(None)
    whole = red.text(text)
    worst = 0
    for cap in range(250, len(text) + 250, 250):
        out, _cut = red.capped(text, cap)
        ref = whole[:len(out)]
        worst = max(worst, sum(1 for a, b in zip(out, ref) if a != b))
    assert worst == 0, worst


CORPUS2 = sorted((DOCS / "corpus2").iterdir())


@pytest.mark.parametrize("path", CORPUS2, ids=lambda p: p.name)
def test_ordinary_project_files_are_unchanged(path):
    """Verifier v13b corpus2: an event log with `"name": "password_reset", ..., "value": "done"` lines, CloudFormation
    and SSM listings, OpenAPI, Helm, Terraform, CLI help (before the fix: 622 changed lines per 10,000)."""
    text = path.read_text(encoding="utf-8")
    assert Redactor(None).text(text) == text
