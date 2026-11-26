"""Round 16 (review v15b of a017af9): YAML block scalar headers and sequences under a secret name in a plain file, key
material without its BEGIN line (an encrypted or long PEM body in a default `git diff`), `git log --graph -p`, hunk
headers whose counts are too short, carriage returns inside lines, word-diff parts over lines, Terraform state text,
Key Vault ids after the value, and quoted labels folded over lines. Each rule is tested both ways."""
from __future__ import annotations

import base64
import json
import random
import subprocess

import pytest

from devmem import redact as R
from devmem.redact import Redactor

from redact_view_helpers import git, leaks

V = "Hq7RtW2xLm9KpB4nVc8sYd3F"
W = "Zx5NvB8mQw1ErT6yUi3oPa9S"


def red(text: str) -> str:
    return Redactor(None).text(text)


# ---- YAML (review yaml15.py): the block scalar header by the YAML grammar ----

@pytest.mark.parametrize("head", ["|", "|-", "|+", ">", ">-", "|2", "|2-", "|-2", ">2+", ">+2", "|9",
                                  "&k |", "!!binary |", "&k !!binary |", "!!binary &k |-", "!vault |2-",
                                  "| # rotated", "|2- # rotated"])
def test_a_block_scalar_under_a_secret_name_is_redacted_whatever_its_header(head):
    text = f"tls:\n  private_key: {head}\n      {V}\n      {W}\n  user: app\n"
    out = red(text)
    assert not leaks(out, [V, W]), out
    assert out.count("\n") == text.count("\n") and "  user: app" in out


@pytest.mark.parametrize("head", ["|2-", "&k !!binary |", ">2+"])
def test_a_block_scalar_under_an_ordinary_name_is_kept(head):
    text = f"docs:\n  description: {head}\n      Some text here\n      and more text\n  user: app\n"
    assert red(text) == text


# ---- YAML: a sequence directly under a secret name ----

@pytest.mark.parametrize("text", [
    f"api_keys: [{V}, {W}]\n",
    f"password: [{V}]\n",
    f'api_keys: ["{V}", "{W}"]\n',
    f"api_keys: [\n  {V},\n  {W}\n]\n",
    f"  - db_passwords: [{V}, {W}]  # rotated\n",
    f"api_keys:\n  - {V}\n  - {W}\n",
    f"api_keys:\n- {V}\n- \"{W}\"\n",
    f"secrets:\n  - {V}  # old\n  - {W}\n",
])
def test_the_items_of_a_sequence_under_a_secret_name_are_redacted(text):
    out = red(text)
    assert not leaks(out, [V, W]), out
    assert out.count("\n") == text.count("\n")


@pytest.mark.parametrize("text", [
    "tokens: [hello, world, again]\n",                       # `tokens` is no secret name (counts, NLP tokens)
    'tokens: ["the", "quick", "brown"]\n',
    "api_keys: [${API_KEY_1}, ${API_KEY_2}]\n",              # references
    "api_keys: [changeme, your-key-here]\n",                 # placeholders
    "passwords:\n  - name: admin\n    value_from: vault\n",  # items that are mappings
    "api_keys:\n  - os.environ['A']\n  - get_key()\n",        # code
    "secret_names: [db, cache]\n",                           # short words
    "password_rules: [1.2.3, 4.5.6]\n",                      # versions
])
def test_sequences_that_hold_no_credential_are_kept(text):
    assert red(text) == text


# ---- key material without its BEGIN line (review graph15.py / tab_rt15.py rows pem_proc) ----

def _b64_lines(seed: int, n: int, width: int = 64) -> list[str]:
    rnd = random.Random(seed)
    raw = bytes(rnd.getrandbits(8) for _ in range(width * n))
    b = base64.b64encode(raw).decode()
    while True:     # (every full line of a real key holds both cases and a digit; draw again until they do)
        lines = [b[i:i + width] for i in range(0, width * n, width)]
        if all(any(c.isupper() for c in ln) and any(c.islower() for c in ln) and any(c.isdigit() for c in ln)
               for ln in lines):
            return lines
        raw = bytes(rnd.getrandbits(8) for _ in range(width * n))
        b = base64.b64encode(raw).decode()


def _key(kind: str, seed: int) -> str:
    if kind == "encrypted PEM":
        return ("-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0A1B2C3D4E5F6071\n\n"
                + "\n".join(_b64_lines(seed, 12)) + "\nq7Rt==\n-----END RSA PRIVATE KEY-----\n")
    if kind == "long PEM":
        return "-----BEGIN PRIVATE KEY-----\n" + "\n".join(_b64_lines(seed, 26)) + "\nAB==\n-----END PRIVATE KEY-----\n"
    if kind == "OpenSSH":
        return ("-----BEGIN OPENSSH PRIVATE KEY-----\n" + "\n".join(_b64_lines(seed, 6, 70)) + "\nAAAE=\n"
                "-----END OPENSSH PRIVATE KEY-----\n")
    return ("-----BEGIN PGP PRIVATE KEY BLOCK-----\nComment: test\n\n" + "\n".join(_b64_lines(seed, 14)) + "\n=Ab1c\n"
            "-----END PGP PRIVATE KEY BLOCK-----\n")


KEYS = ["encrypted PEM", "long PEM", "OpenSSH", "PGP"]


@pytest.mark.parametrize("kind", KEYS)
@pytest.mark.parametrize("view", ["git diff", "git log -p", "git show", "diff -u", "git diff -U1"])
def test_a_key_body_replaced_in_a_diff_whose_context_hides_the_begin_line(tmp_path, kind, view):
    """The whole body changes, so the hunk starts below BEGIN (three lines of context, or one): old and new body lines
    are key material without a header; also the lines of the body that did not change."""
    old, new = _key(kind, 1), _key(kind, 2)
    d = tmp_path / "r"
    d.mkdir()
    git(d, "init", "-q")
    (d / "k.txt").write_bytes(old.encode())
    git(d, "add", "-A")
    git(d, "commit", "-qm", "a")
    (d / "k.txt").write_bytes(new.encode())
    git(d, "commit", "-qam", "b")
    if view == "diff -u":
        (d / "o.txt").write_bytes(old.encode())
        out = subprocess.run(["diff", "-u", "o.txt", "k.txt"], cwd=d, capture_output=True).stdout.decode()
    else:
        out = git(d, *{"git diff": ["diff", "HEAD~1"], "git log -p": ["log", "-p"], "git show": ["show", "HEAD"],
                       "git diff -U1": ["diff", "-U1", "HEAD~1"]}[view])
    body = [ln for ln in (old + new).split("\n") if len(ln) >= 60]
    assert any(b in out for b in body)
    res = red(out)
    assert not [b for b in body if b[:24] in res], res


@pytest.mark.parametrize("text", [
    "\n".join("%064x" % (i * 7919) for i in range(1, 9)) + "\n",                          # hex digests
    "\n".join(_b64_lines(3, 2)) + "\n",                                                     # two lines only
    "\n".join(ln[:63] + "\n" + ln[:65] for ln in _b64_lines(4, 3)) + "\n",                  # lengths differ
    "\n".join(_b64_lines(5, 6, 76)) + "\n",                                                 # MIME (76 wide)
    "\n".join(("abcdefghijklmnopqrstuvwxyz" * 3)[:64] for _ in range(5)) + "\n",            # letters only
    "-----BEGIN CERTIFICATE-----\n" + "\n".join(_b64_lines(6, 8)) + "\n-----END CERTIFICATE-----\n",
    "-----BEGIN PUBLIC KEY-----\n" + "\n".join(_b64_lines(7, 6)) + "\n-----END PUBLIC KEY-----\n",
    "-----BEGIN PGP SIGNATURE-----\n\n" + "\n".join(_b64_lines(8, 4)) + "\n=Ab1c\n-----END PGP SIGNATURE-----\n",
    "+" + "\n+".join(_b64_lines(9, 2)) + "\n",                                              # two added lines
])
def test_base64_that_is_no_headless_key_body_is_kept(text):
    assert red(text) == text


def test_a_headless_run_needs_key_run_lines_lines():
    assert R.KEY_RUN_LINES == 3
    three = "\n".join(_b64_lines(10, 3)) + "\n"
    assert red(three) != three and not leaks(red(three), [three.split("\n")[0]])


# (review v16b certoff16.py: certifi's bundle read with the Read tool from 231 offsets lost 2,079 lines of public
# certificate bodies: a window that starts inside a certificate shows its END line below the run, not its BEGIN line)
def _cert(seed: int, n: int = 20) -> str:
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(_b64_lines(seed, n)) + "\nAB==\n-----END CERTIFICATE-----"


def _numbered(text: str, start: int = 1) -> str:
    return "\n".join(f"{start + i:6}→{x}" for i, x in enumerate(text.split("\n")))


@pytest.mark.parametrize("view", ["plain", "Read", "cat -n", "diff +"])
@pytest.mark.parametrize("cut", ["window starts inside a certificate", "middle of a bundle, no armour of its own",
                                 "SAML X509Certificate"])
def test_public_certificate_bodies_without_their_begin_line_are_kept(view, cut):
    if cut == "window starts inside a certificate":
        text = "\n".join(_cert(11).split("\n")[5:]) + "\n" + _cert(12) + "\n"
    elif cut == "middle of a bundle, no armour of its own":
        text = _cert(13) + "\n" + "\n".join(_cert(14, 30).split("\n")[3:26]) + "\n" + _cert(15) + "\n"
    else:
        text = ("<md:KeyDescriptor>\n  <ds:X509Data>\n    <ds:X509Certificate>\n"
                + "\n".join("      " + ln for ln in _b64_lines(16, 12)) + "\n    </ds:X509Certificate>\n")
    if view == "Read":
        text = _numbered(text, 40)
    elif view == "cat -n":
        text = "\n".join(f"{i + 40:6}\t{x}" for i, x in enumerate(text.split("\n")))
    elif view == "diff +":
        text = "@@ -1,0 +1,99 @@\n" + "\n".join("+" + x for x in text.split("\n"))
    assert red(text) == text


@pytest.mark.parametrize("text", [
    "\n".join(_b64_lines(17, 6)) + "\n",                                            # no armour in view at all
    _cert(18) + "\n" + "\n".join(_b64_lines(19, 6)) + "\n-----END RSA PRIVATE KEY-----\n",   # a private END below
    "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----\n" + _cert(20) + "\n"
    + "\n".join(_b64_lines(21, 6)) + "\n",                                          # a private key's armour in the text
])
def test_a_headless_run_with_no_public_evidence_is_still_replaced(text):
    out = red(text)
    body = [ln for ln in text.split("\n") if len(ln) == 64 and ln not in _cert(18) + _cert(20)]
    assert body and not [b for b in body if b in out], out


# ---- git log --graph -p (review graph15.py) ----

def test_git_log_graph_p_is_read_through_its_gutter(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    git(d, "init", "-q")
    for v in ("Base0Base0Base0Base0", V, W):
        (d / "c.ini").write_bytes(f"[db]\nhost = x\npassword = {v}\nuser = app\n".encode())
        (d / "t.yaml").write_bytes(f"tls:\n  private_key: |\n    {v}\n  user: app\n".encode())
        git(d, "add", "-A")
        git(d, "commit", "-qm", "c")
    for argv in (["log", "-p", "--graph"], ["log", "-p", "--graph", "--oneline"]):
        out = git(d, *argv)
        assert "| -password = " in out or "|  " in out
        assert not leaks(red(out), [V, W]), red(out)


def test_graph_columns_are_no_decoration_without_a_hunk_in_the_run():
    text = "| a | b |\n|---|---|\n| -x | y |\n"
    assert red(text) == text


# ---- hunk headers, carriage returns, word diffs over lines (review fc15.py) ----

A, B, C, D = "Xk4mLp9QrT2bQw8eRt5yUi3o", "Zx1cVb9nMa8sDf7gHj6kLq5w", "Pa7sDf6gHj2kLm4nBv8cXz1q", "Qr2sT5uV8wX1yZ4aB7cD0eF"


@pytest.mark.parametrize("text,values", [
    ("@@ -1,3 +1,3 @@\n tls:\n   private_key: |\n-    " + A + "\n-    " + C + "\n+    " + B + "\n+    " + D
     + "\n   user: x\n", [A, B, C, D]),                                    # counts too short: read on to the run's end
    ("@@ -1,3 +1,3 @@\r tls:\r   private_key: |\r-    " + A + "\r+    " + B + "\r   user: x\r", [A, B]),  # CR only
    ("@@ -1,4 +1,4 @@\n tls:\r\n   private_key: |\n-    " + A + "\r\n+    " + B + "\n   user: x\n", [A, B]),  # mixed
    ("@@ -1,3 +1,3 @@\npassword: [-" + A + "\n" + C + "-]{+" + B + "+}\n", [A, B, C]),    # a part over lines
    ("@@ -1,2 +1,2 @@\nprivate_key: |\n  [-" + C + "-]{+" + D + "+}\n@@ -3,2 +3,2 @@\n  [-" + A + "-]{+" + B + "+}\n",
     [A, B, C, D]),                                                         # a word-diff hunk right after a hot one
])
def test_diff_shapes_the_side_reading_failed_on(text, values):
    out = red(text)
    assert not leaks(out, values), out
    assert out.count("\n") == text.count("\n") and out.count("\r") == text.count("\r")


def test_counts_too_short_take_no_line_after_the_marked_run():
    text = "@@ -1,1 +1,1 @@\n-a = 1\n+a = 2\nplain prose after the diff\n"
    assert red(text) == text


def test_a_return_before_blanks_only_is_no_line_end():
    """`diff -y` pads the right column of a CRLF file after its `\\r`: no line end there (an ordinary column such as
    `pass | return.` is not read as a table row)."""
    text = "        pass\t\t\t\t\t\t\t\t      |\t        return.\r\t\t\t\t\t\t\t\t\t\t\n"
    assert red(text) == text


# ---- Terraform state as text (value, type, sensitive) and Key Vault ids after the value (review tf15.py) ----

def test_a_terraform_state_v4_output_marked_sensitive_is_redacted_as_text():
    state = {"version": 4, "terraform_version": "1.9.5", "serial": 3, "lineage": "0f7c",
             "outputs": {"admin_conn": {"value": V, "type": "string", "sensitive": True},
                         "conns": {"value": [V[::-1], "b"], "type": ["list", "string"], "sensitive": True},
                         "region": {"value": "eu-west-1", "type": "string"}},
             "resources": []}
    for text in (json.dumps(state, indent=2), json.dumps(state)):
        out = red(text)
        assert not leaks(out, [V, V[::-1]]), out
        assert "eu-west-1" in out


def test_a_terraform_value_not_marked_sensitive_is_kept_whatever_the_order():
    text = json.dumps({"outputs": {"region": {"value": "eu-west-1-x9", "type": "string", "sensitive": False}}}, indent=2)
    assert red(text) == text


KV = "https://vlt1.vault.azure.net/secrets/db/0a1b2c3d4e5f"


@pytest.mark.parametrize("obj", [{"value": V, "id": KV, "name": "db"}, {"id": KV, "name": "db", "value": V},
                                 {"attributes": {"enabled": True}, "value": V, "tags": {"a": "b"}, "id": KV}])
def test_a_key_vault_value_is_redacted_with_its_id_before_or_after_it(obj):
    for text in (json.dumps(obj, indent=2), json.dumps(obj)):
        assert V not in red(text)


@pytest.mark.parametrize("obj", [
    {"id": KV, "tags": {"value": "owner-team"}, "name": "db"},                  # a nested object's value
    {"value": "owner-team-x", "items": [{"id": KV}]},                           # the id inside a nested list
    [{"id": KV, "name": "a"}, {"name": "colour", "value": "blue-green-x"}],     # another object of a list
])
def test_a_value_that_is_no_sibling_of_a_key_vault_id_is_kept(obj):
    text = json.dumps(obj, indent=2)
    assert red(text) == text


# ---- quoted labels folded over lines (review span15.py) ----

@pytest.mark.parametrize("text", [
    'password: "Your password must be\n  at least 12 characters"\n',
    'secret: "This value is read from the vault\n  at startup and never logged"\nport: 1\n',
])
def test_a_quoted_label_over_lines_is_kept_as_on_one_line(text):
    assert red(text) == text
    assert red(text.replace("\n  ", " ", 1)) == text.replace("\n  ", " ", 1)


@pytest.mark.parametrize("text", [f'password: "{V[:12]}\n  {V[12:]}"\n', f"password: '{V[:12]}\n  {V[12:]}'\n"])
def test_a_quoted_value_over_lines_is_still_redacted(text):
    assert not leaks(red(text), [V])
