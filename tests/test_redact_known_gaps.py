"""Views known to keep a value (review B11, B6 and the MEDIUM items), each pinned with real tool output: the test
asserts that a value IS kept, next to a control that keeps nothing. docs/PRIVACY.md lists each of them among the views
known to keep a value; a change that closes one fails its test until docs/PRIVACY.md and this file are corrected."""
from __future__ import annotations

import random
import string
import subprocess

import pytest

from devmem.redact import Redactor

from redact_view_helpers import git, leaks, run, tool_path

_RNG = random.Random(1106)


def _val(n: int = 22) -> str:
    a = string.ascii_letters + string.digits
    while True:
        v = "".join(_RNG.choice(a) for _ in range(n))
        if any(c.isdigit() for c in v) and any(c.isupper() for c in v) and any(c.islower() for c in v):
            return v


def _b64(n: int) -> str:
    a = string.ascii_letters + string.digits + "+/"
    while True:
        v = "".join(_RNG.choice(a) for _ in range(n))
        if any(c.isdigit() for c in v) and any(c.isupper() for c in v) and any(c.islower() for c in v):
            return v


def _commit_two(tmp, name: str, t1: str, t2: str):
    d = tmp / "r"
    d.mkdir()
    git(d, "init", "-q")
    (d / name).write_bytes(t1.encode())
    git(d, "add", "-A")
    git(d, "commit", "-qm", "c1")
    (d / name).write_bytes(t2.encode())
    git(d, "add", "-A")
    git(d, "commit", "-qm", "c2")
    return d


# ---- line ends that mix CR-only lines with LF or CRLF lines ----

ENDS = {"CRLF, CR and LF by turns": ("\r\n", "\r", "\n"), "CR and LF by turns": ("\r", "\n"),
        "CRLF and LF by turns": ("\r\n", "\n")}


def _mixed(lines: list[str], ends: tuple[str, ...]) -> str:
    return "".join(ln + ends[i % len(ends)] for i, ln in enumerate(lines))


def _list_block(v: str) -> list[str]:
    pad = [f"k{i}: v{i}" for i in range(6)]
    return pad + ["users:", "  - name: app", "    password: |", f"      {v}", f"      {v[::-1]}", "    role: x"] + pad


def _pem(v: str) -> list[str]:
    pad = [f"k{i}: v{i}" for i in range(6)]
    return pad + ["-----BEGIN RSA PRIVATE KEY-----", v + _b64(44)] + [_b64(64) for _ in range(3)] + [
        "-----END RSA PRIVATE KEY-----"] + pad


@pytest.mark.parametrize("shape,ends", [("list-item block", "CRLF, CR and LF by turns"), ("PEM key", "CR and LF by turns"),
                                        ("list-item block", "CRLF and LF by turns"), ("PEM key", "CRLF and LF by turns")])
def test_a_value_edited_in_a_file_with_cr_only_lines_among_others(tmp_path, shape, ends):
    old, new = _val(), _val()
    make = _list_block if shape == "list-item block" else _pem
    d = _commit_two(tmp_path, "c.yaml", _mixed(make(old), ENDS[ends]), _mixed(make(new), ENDS[ends]))
    out = Redactor(None).text(git(d, "diff", "HEAD~1", "HEAD"))
    kept = leaks(out, [old, new, old[::-1], new[::-1]])
    if ends == "CRLF and LF by turns":
        assert not kept, out                  # (the control: the mixture tests/test_redact_diff_edits.py covers)
    else:
        assert kept, "closed: update docs/PRIVACY.md's list of views known to keep a value and this test"


# ---- `git log -p --graph` of a history with a merge ----

def test_git_log_graph_with_a_merge_keeps_an_edited_block_value(tmp_path):
    """Both branches edit the file, then merge: a commit drawn with an empty graph column has the prefix `|   `
    (a pipe and three blanks), which the graph decoration (`| `, `* `, `|/` units) does not read as a gutter."""
    def conf(pair: str, blk: str, li: str) -> str:
        return (f"db:\n  host: localhost\n  password: {pair}\n  user: app\ntls:\n  private_key: |\n    {blk}\n"
                f"    {blk[::-1]}\n  port: 1\nusers:\n  - name: app\n    secret: |\n      {li}\n    role: x\nLOG=1\n")
    d = tmp_path / "g"
    d.mkdir()
    git(d, "init", "-q", "-b", "main")
    p0, b0, l0 = _val(), _val(), _val()
    (d / "c.yaml").write_bytes(conf(p0, b0, l0).encode())
    git(d, "add", "-A")
    git(d, "commit", "-qm", "base")
    git(d, "checkout", "-q", "-b", "feature")
    p1, l1 = _val(), _val()
    (d / "c.yaml").write_bytes(conf(p1, b0, l1).encode())
    git(d, "commit", "-qam", "feature")
    git(d, "checkout", "-q", "main")
    b2 = _val()
    (d / "c.yaml").write_bytes(conf(p0, b2, l0).encode())
    git(d, "commit", "-qam", "main")
    git(d, "merge", "-q", "--no-edit", "feature")
    values = [p0, b0, l0, p1, l1, b2]
    plain = Redactor(None).text(git(d, "log", "-p"))
    assert not leaks(plain, values), plain              # (the control: the same history without the graph)
    out = Redactor(None).text(git(d, "log", "-p", "--graph"))
    assert leaks(out, values), "closed: update docs/PRIVACY.md's list of views known to keep a value and this test"


# ---- a key body edited in its middle, default `git diff` ----

def _key_lines() -> list[str]:
    return ["-----BEGIN RSA PRIVATE KEY-----"] + [_b64(64) for _ in range(24)] + [_b64(40),
                                                                                 "-----END RSA PRIVATE KEY-----"]


def test_the_key_line_git_puts_in_the_hunk_header_is_kept(tmp_path):
    """git puts the last line above the hunk that starts like a function (a body line of a PEM file) after the hunk
    header; the headless-body rule does not read a hunk header."""
    key = _key_lines()
    k2 = list(key)
    k2[12] = k2[12][::-1]
    d = _commit_two(tmp_path, "k.pem", "\n".join(key) + "\n", "\n".join(k2) + "\n")
    diff = git(d, "diff", "HEAD~1", "HEAD")
    head = [ln for ln in diff.split("\n") if ln.startswith("@@ ")][0]
    assert head.split("@@")[-1].strip() in key, head
    out = Redactor(None).text(diff)
    body = [ln for ln in diff.split("\n")[5:] if ln[:1] in " +-" and len(ln) > 60]
    assert not leaks(out, [ln[1:] for ln in body]), out        # (every body line of the hunk itself is replaced)
    assert leaks(out, [head.split("@@")[-1].strip()]), "closed: update docs/PRIVACY.md and this test"


def test_a_key_in_a_block_scalar_edited_in_its_middle_keeps_the_edited_lines(tmp_path):
    """The plain reading replaces the context runs (three body lines each) first, and the hunk's sides are built from
    its output, so each edited line stands alone on its side: fewer than three base64 lines."""
    key = _key_lines()
    k2 = list(key)
    k2[12] = k2[12][::-1]

    def secret(ls: list[str]) -> str:
        return "apiVersion: v1\nkind: Secret\nstringData:\n  tls.key: |\n" + "".join(f"    {x}\n" for x in ls) + \
            "  other: x\n"
    d = _commit_two(tmp_path, "s.yaml", secret(key), secret(k2))
    out = Redactor(None).text(git(d, "diff", "HEAD~1", "HEAD"))
    assert not leaks(out, [key[11], key[13]]), out                  # (the context lines are replaced)
    assert leaks(out, [key[12], k2[12]]), "closed: update docs/PRIVACY.md and this test"


# ---- a private key's body without its BEGIN line, next to public armour or under a data line (review B12, B2) ----
# The headless-body rule declines for a whole text whose armour lines all name public material (a certificate bundle
# read from its middle), and for a run under a line that says base64 data follows; a private key body edited in its
# middle (its BEGIN line out of view) shown in such a text is kept.

def _cert_lines() -> list[str]:
    return ["-----BEGIN CERTIFICATE-----"] + [_b64(64) for _ in range(18)] + [_b64(28), "-----END CERTIFICATE-----"]


def _key_edit_repo(tmp, cert_commit: str):
    """A key file whose body is edited in its middle (default three lines of context: no BEGIN line in the hunk), with
    a certificate added in an earlier commit ("before"), in the same commit ("same") or not at all ("none")."""
    key = _key_lines()
    k2 = list(key)
    k2[12] = k2[12][::-1]
    d = tmp / "k"
    d.mkdir()
    git(d, "init", "-q")
    (d / "k.pem").write_bytes(("\n".join(key) + "\n").encode())
    git(d, "add", "-A")
    git(d, "commit", "-qm", "key")
    if cert_commit == "before":
        (d / "c.pem").write_bytes(("\n".join(_cert_lines()) + "\n").encode())
        git(d, "add", "-A")
        git(d, "commit", "-qm", "cert")
    (d / "k.pem").write_bytes(("\n".join(k2) + "\n").encode())
    if cert_commit == "same":
        (d / "c.pem").write_bytes(("\n".join(_cert_lines()) + "\n").encode())
    git(d, "add", "-A")
    git(d, "commit", "-qm", "edit key")
    return d, [key[i] for i in range(9, 16)] + [k2[12]]


@pytest.mark.parametrize("cert_commit,argv", [("before", ["log", "-p", "-2"]), ("same", ["show", "HEAD"])],
                         ids=["git log -p -2: certificate in the commit before", "git show: certificate in the commit"])
def test_a_key_body_edited_in_its_middle_beside_a_certificate_is_kept(tmp_path, cert_commit, argv):
    d, body = _key_edit_repo(tmp_path, cert_commit)
    alone = Redactor(None).text(git(d, "show", "HEAD", "--", "k.pem"))
    assert not leaks(alone, body), alone                  # (the control: the key's hunk alone is replaced)
    out = Redactor(None).text(git(d, *argv))
    assert leaks(out, body), "closed: update docs/PRIVACY.md's list of views known to keep a value and this test"


@pytest.mark.parametrize("head", ["Content-Transfer-Encoding: base64\n\n", "  key: data:application/octet-stream;base64,\n"],
                         ids=["MIME data line", "data URI"])
def test_a_key_body_under_a_line_that_says_base64_data_follows_is_kept(head):
    body = _key_lines()[3:15]
    alone = Redactor(None).text("\n".join(body) + "\n")
    assert not leaks(alone, body), alone                  # (the control: the same lines alone are replaced)
    out = Redactor(None).text(head + "\n".join(body) + "\n")
    assert leaks(out, body), "closed: update docs/PRIVACY.md's list of views known to keep a value and this test"


# ---- `diff -y` options ----

@pytest.mark.parametrize("opt", ["-t", "--left-column", "--suppress-common-lines"])
def test_diff_y_options_that_change_its_columns_keep_a_block_value(tmp_path, opt):
    tool_path("diff")
    old, new = _val(), _val()

    def block(v: str) -> str:
        return f"tls:\n  cert: x.crt\n  private_key: |\n    {v}\n    {v[::-1]}\n  user: app\n"
    (tmp_path / "a.yaml").write_bytes(block(old).encode())
    (tmp_path / "b.yaml").write_bytes(block(new).encode())
    plain = Redactor(None).text(run(tmp_path, ["diff", "-y", "a.yaml", "b.yaml"], ok=(1,)))
    assert not leaks(plain, [old, new]), plain                       # (the control: `diff -y` itself)
    out = Redactor(None).text(run(tmp_path, ["diff", "-y", opt, "a.yaml", "b.yaml"], ok=(1,)))
    assert leaks(out, [old, new, old[::-1], new[::-1]]), "closed: update docs/PRIVACY.md and this test"


# ---- YAML forms the block and sequence rules do not read ----

@pytest.mark.parametrize("text", [
    '"password": |\n  {v}\n',                      # a quoted key
    "'password': |\n  {v}\n",
    "? password\n: {v}\n",                         # a complex key
    "secrets:\n  - |\n    {v}\n",                   # a block scalar as a sequence item
    "api_keys:\n  # rotated\n  - {v}\n",            # a comment line before the items
], ids=["double-quoted key", "single-quoted key", "complex key", "block scalar item", "comment before items"])
def test_yaml_forms_not_read_keep_their_value(text):
    v = _val()
    out = Redactor(None).text(text.format(v=v))
    assert leaks(out, [v]), "closed: update docs/PRIVACY.md and this test"


def test_subprocess_is_available():
    assert subprocess.run(["git", "--version"], capture_output=True).returncode == 0
