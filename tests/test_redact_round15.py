"""Round 15 (verifier v14b, remaining items): parsed Terraform output at the write boundary, `az keyvault secret show`
JSON, a vendor token glued after `_`, and the keys allowed between a pair's name and its value."""
from __future__ import annotations

import json
import re

try:
    import re._parser as sre_parse      # Python 3.11+
    from re import _constants as sre_c
except ImportError:                     # pragma: no cover
    import sre_constants as sre_c       # type: ignore[no-redef]
    import sre_parse                    # type: ignore[no-redef]

import pytest

from devmem import redact as R
from devmem.redact import Redactor

V = "Xk4mLp9QrT2b" + "Wq8ZeR5t"


# ---- Terraform: `terraform output -json` and a state's `outputs` ----

TF = {
    "conn_str": {"sensitive": True, "type": "string", "value": V},          # a neutral name, marked sensitive
    "db_password": {"sensitive": False, "type": "string", "value": V[::-1]},  # a secret name, not marked
    "endpoints": {"sensitive": True, "type": ["list", "string"], "value": [V + "a", V + "b"]},
    "region": {"sensitive": False, "type": "string", "value": "eu-west-1"},
    "instance_count": {"sensitive": False, "type": "number", "value": 3},
}
KEPT = ["eu-west-1"]


@pytest.mark.parametrize("how", ["record", "tree", "record of a state", "text"])
def test_terraform_output_values_are_redacted_by_their_sensitive_flag_and_their_name(how):
    if how == "record":
        out = json.dumps(Redactor(None).record(TF))
    elif how == "tree":
        out = json.dumps(Redactor(None).tree(TF, 100_000)[0])
    elif how == "record of a state":
        out = json.dumps(Redactor(None).record({"version": 4, "outputs": TF, "resources": []}))
    else:
        out = Redactor(None).text(json.dumps(TF, indent=2))
    assert V[:8] not in out and V[::-1][:8] not in out, out
    assert all(k in out for k in KEPT) and '"instance_count"' in out, out


def test_a_value_marked_sensitive_false_under_an_ordinary_name_is_kept():
    x = {"region": {"sensitive": False, "type": "string", "value": "Xk4mLp9QrT2b"}}
    assert Redactor(None).record(x) == x


# ---- Azure Key Vault ----

KV = {"attributes": {"created": "2026-01-01T00:00:00+00:00", "enabled": True}, "contentType": None,
      "id": "https://kv-prod.vault.azure.net/secrets/Conn/0f1e2d3c", "kid": None, "managed": None, "name": "Conn",
      "tags": {"env": "prod"}, "value": V}


@pytest.mark.parametrize("how", ["record", "tree", "text", "text one line", "list"])
def test_an_az_keyvault_secret_value_is_redacted(how):
    red = Redactor(None)
    if how == "record":
        out = json.dumps(red.record(KV))
    elif how == "tree":
        out = json.dumps(red.tree(KV, 100_000)[0])
    elif how == "text":
        out = red.text(json.dumps(KV, indent=2))
    elif how == "text one line":
        out = red.text(json.dumps(KV))
    else:
        out = red.text(json.dumps([KV, {**KV, "value": V[::-1]}], indent=2))
    assert V[:8] not in out and V[::-1][:8] not in out, out
    assert "kv-prod.vault.azure.net/secrets/Conn" in out


def test_a_value_after_a_key_vault_object_that_ended_is_kept():
    text = json.dumps([{"id": "https://kv.vault.azure.net/secrets/a/1", "name": "a"}, {"name": "colour", "value": "teal-green-7"}],
                      indent=2)
    assert Redactor(None).text(text) == text


# ---- a token glued after `_` ----

ALPHABET = "aB3cD4eF5gH6jK7mN8pQ9rS2tU"


def _sample(rx: str) -> str:
    """A string the token regex matches (first branch of each alternation, the least repeat count; character classes
    filled from a mixed alphabet so lookaheads asking for a capital or a digit hold)."""
    out: list[str] = []
    k = [0]

    def pick(cls) -> str:
        for _ in range(64):
            c = ALPHABET[k[0] % len(ALPHABET)]
            k[0] += 1
            if re.fullmatch(cls, c):
                return c
        for c in map(chr, range(33, 127)):
            if re.fullmatch(cls, c):
                return c
        raise AssertionError(cls)

    def walk(items) -> None:
        for op, av in items:
            if op is sre_c.LITERAL:
                out.append(chr(av))
            elif op is sre_c.IN:
                out.append(pick(_cls(av)))
            elif op is sre_c.ANY:
                out.append("a")
            elif op in (sre_c.MAX_REPEAT, sre_c.MIN_REPEAT):
                lo, _hi, sub = av
                for _ in range(max(lo, 1) if lo else 0):
                    walk(sub)
            elif op is sre_c.SUBPATTERN:
                walk(av[-1])
            elif op is sre_c.BRANCH:
                walk(av[1][0])
            # (assertions and boundaries add nothing)

    walk(sre_parse.parse(rx))
    return "".join(out)


def _cls(av) -> str:
    parts = []
    neg = False
    for op, a in av:
        if op is sre_c.NEGATE:
            neg = True
        elif op is sre_c.LITERAL:
            parts.append(re.escape(chr(a)))
        elif op is sre_c.RANGE:
            parts.append(re.escape(chr(a[0])) + "-" + re.escape(chr(a[1])))
        elif op is sre_c.CATEGORY:
            parts.append({sre_c.CATEGORY_DIGIT: r"\d", sre_c.CATEGORY_WORD: r"\w", sre_c.CATEGORY_SPACE: r"\s"}.get(a, r"\w"))
    return "[" + ("^" if neg else "") + "".join(parts) + "]"


TOKEN_RULES = ["anthropic-key", "openai-key", "github-token", "gitlab-token", "mailgun-key", "slack-token",
               "sendgrid-key", "pypi-token", "google-oauth-secret", "stripe-webhook-secret", "vault-token",
               "digitalocean-token", "twilio-key", "databricks-token", "shopify-token", "aws-access-key-id",
               "google-api-key", "stripe-key", "docker-token", "linear-key", "age-secret-key", "fcm-server-key"]


def _token_samples() -> list:
    out = []
    for kind, rx, _repl in R._RULES:
        if kind in TOKEN_RULES:
            out.append(pytest.param(kind, _sample(rx.pattern), id=kind))
    for name, rx in R._VENDOR_TOKENS:
        for j, alt in enumerate(R._top_alternatives(rx)):
            out.append(pytest.param(name, _sample(alt), id=f"{name}-{j}"))
    return out


SAMPLES = _token_samples()


def test_every_token_rule_and_vendor_entry_has_a_sample():
    kinds = {p.values[0] for p in SAMPLES}
    assert set(TOKEN_RULES) <= kinds and {n for n, _ in R._VENDOR_TOKENS} <= kinds


# token rules whose prefix is short and also a common piece of an identifier (review v15b glued15.py: `cache_key-<md5>`,
# `TASK_SK<hex>`, `my_dapi<hex>`, `build_sk-<id>` were taken for tokens): a word boundary before them, `_` included;
# the vendor entries with a prefix of fewer than four characters are listed by redact.VENDOR_GLUE_NO
GLUE_GENERIC = {"mailgun-key", "twilio-key", "databricks-token"}
BARE_OPENAI = "sk-" + "aB3cD4eF5gH6jK7mN8pQ9rS2"


def _generic(kind: str) -> bool:
    return kind in GLUE_GENERIC or kind in R.VENDOR_GLUE_NO


@pytest.mark.parametrize("kind,token", SAMPLES + [pytest.param("openai-key-bare", BARE_OPENAI, id="openai-key-bare")])
@pytest.mark.parametrize("before", ["", " ", "notes_", "API_TOKEN_", "x_y_"])
def test_a_token_is_caught_after_an_underscore_unless_its_prefix_is_generic(kind, token, before):
    """Both directions for every token rule and vendor entry: after nothing or a blank every token is caught; after
    `_` a token with a distinctive prefix (`ghp_`, `glpat-`, `xoxb-`, `AKIA`, `sk-proj-`) is caught, one with a short
    generic prefix (`key-`, `SK`, `dapi`, a bare `sk-`, the vendor entries of VENDOR_GLUE_NO) is not: there it is
    the end of an identifier."""
    out = Redactor(None).text(before + token + " end")
    generic = _generic(kind) or kind == "openai-key-bare"
    if before.endswith("_") and generic:
        assert out == before + token + " end", (kind, token, out)
    else:
        assert token[-10:] not in out, (kind, token, out)
        assert out.startswith(before) and out.endswith(" end")


@pytest.mark.parametrize("text", ["user_key-7631bc07a1cc8fcd56e70fc6b2fb4a43", "evicted cache_key-7631bc07a1cc8fcd56e70fc6b2fb4a43",
                                  "TASK_SK7631bc07a1cc8fcd56e70fc6b2fb4a43", "/v1/my_dapi7631bc07a1cc8fcd56e70fc6b2fb4a43",
                                  "build_sk-abcdefghijklmnopqrstuvwx", "artifact_key-7631bc07a1cc8fcd56e70fc6b2fb4a43.tar.gz"])
def test_names_glued_to_a_generic_token_prefix_stay(text):
    assert Redactor(None).text(text) == text


@pytest.mark.parametrize("text", ["task_gsk_list = []", "my_sk-test", "re_AKIA_name", "foo_key-value", "a_dapi_b",
                                  "x_ghp_short", "SK_FLAG = 1"])
def test_identifiers_with_a_token_prefix_inside_stay(text):
    assert Redactor(None).text(text) == text


# ---- the keys allowed between a pair's name and its value ----

@pytest.mark.parametrize("meta", ['"Foo": "bar"', '"Owner": "team-a"', '"rotation": 30', '"Labels": []',
                                  '"Region": "eu-west-1", "Tier": "Standard"'])
def test_any_short_sibling_key_may_stand_between_a_pair_name_and_its_value(meta):
    text = '{"Name": "/prod/DB_PASSWORD", ' + meta + ', "Value": "' + V + '"}'
    out = Redactor(None).text(text)
    assert V not in out, out


@pytest.mark.parametrize("indent", [None, 2, 4])
@pytest.mark.parametrize("outer", ["Parameter", "Parameters"])
def test_a_pair_inside_a_named_object_is_still_read_by_its_own_name(outer, indent):
    """`"Parameter": {"Name": "/prod/DB_PASSWORD", ..., "Value": X}` (aws ssm get-parameter): the outer key opens a
    candidate pair too; the scan of its items stops at the inner `Name`, so the inner pair is read (a regression of the
    any-keys scan before it excluded `name` and `key`, found by the reviewer's b_rates/f14_e2e)."""
    p = {"Name": "/prod/db/DB_PASSWORD", "Type": "SecureString", "Value": V, "Version": 2}
    text = json.dumps({outer: [p] if outer.endswith("s") else p}, indent=indent)
    out = Redactor(None).text(text)
    assert V[:8] not in out, out


def test_a_pair_name_with_more_than_six_keys_before_the_value_is_not_read_as_a_pair():
    """The scan is bounded (six items): a name farther away is not the value's name (a stated limit)."""
    meta = ", ".join(f'"k{i}": "v{i}"' for i in range(7))
    text = '{"Name": "/prod/region", ' + meta + ', "Value": "eu-west-1"}'
    assert Redactor(None).text(text) == text


@pytest.mark.parametrize("value", [[V, V[::-1]], {"user": "app", "password": V}, 84736251])
def test_a_sensitive_terraform_list_map_or_number_printed_over_lines_is_redacted(value):
    text = json.dumps({"out": {"sensitive": True, "type": ["list", "string"], "value": value}}, indent=2)
    out = Redactor(None).text(text)
    assert V[:8] not in out and V[::-1][:8] not in out and "84736251" not in out, out
    assert out.count("\n") == text.count("\n")


@pytest.mark.xfail(strict=True, reason="a listed gap: a sensitive value nested two levels deep in text form")
def test_a_sensitive_terraform_value_nested_two_levels_in_text():
    text = json.dumps({"out": {"sensitive": True, "type": "x", "value": {"k": {"j": V}}}}, indent=2)
    assert V[:8] not in Redactor(None).text(text)


# ---- a value over lines (_spanning_repl): each decline tested from both sides ----

@pytest.mark.parametrize("text", [
    'db:\n  password: "' + V[:10] + '\n    ' + V[10:] + '"\n  user: app\n',
    "db:\n  - api_key: '" + V[:10] + "\n      " + V[10:] + "'\n",
    'API_TOKEN="' + V[:10] + '\n' + V[10:] + '"\n',
    "LOG=1\nAPI_KEY=" + V[:10] + "\\\n" + V[10:] + "\nPORT=1\n",
    "export DB_PASSWORD=\\\n  " + V + "\n",
])
def test_a_secret_value_over_lines_is_redacted_whole(text):
    out = Redactor(None).text(text)
    assert V[:8] not in out and V[10:18] not in out and V[-8:] not in out, out
    assert out.count("\n") == text.count("\n")


@pytest.mark.parametrize("text", [
    'db:\n  description: "the primary\n    database"\n',                  # not a secret name
    'x = "token=" + fmt(a,\n    b) + "end"\n',                              # the name does not start its line
    'password: "abc\n  def" + suffix\n',                                    # the line goes on after the quote
    "CFLAGS=-O2 \\\n  -Wall\n",                                              # not a secret name
    "password: \"\n\"\n",                                                    # nothing between the quotes
])
def test_a_value_over_lines_under_no_secret_name_or_in_code_is_kept(text):
    assert Redactor(None).text(text) == text


@pytest.mark.parametrize("nxt", ["DEBUG=0", "  - DEBUG=0", "user: app", "[db]", "+PORT=1", "export PATH=/usr/bin"])
def test_a_value_ending_in_a_backslash_leaves_the_next_setting_alone(nxt):
    """A generated password can end in `\\` (tests/test_redact_value_grammar.py): a next line that starts like another
    setting is no continuation, so it is kept (and the value itself is redacted)."""
    out = Redactor(None).text("DB_PASSWORD=" + V + "\\\n" + nxt + "\n")
    assert V[:8] not in out and nxt in out, out
