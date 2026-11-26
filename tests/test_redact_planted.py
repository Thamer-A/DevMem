"""Planted credential formats from the fuzz run (FUZZ/r2_redact_survivors.py): each must be gone after redaction,
redaction must be idempotent, and the ordinary text around them must stay. All values are fake."""
from __future__ import annotations

import pytest

from devmem.redact import Redactor

R = Redactor(None, enabled=True)

PLANTED = [
    ("telegram bot token", "TELEGRAM=1234567890:AAHfiqksKZ8WmR2zSjiQ7_v4TMAKdiHm9T0", "AAHfiqksKZ8WmR2zSjiQ7"),
    ("telegram bot token in code", 'bot = Bot("1234567890:AAHfiqksKZ8WmR2zSjiQ7_v4TMAKdiHm9T0")', "AAHfiqksKZ8WmR2"),
    ("slack webhook", "post to https://hooks.slack.com/" "services/T0ABCDEFG/B0ABCDEFGHI/abcdefghijklmnopqrstuvwx",
     "abcdefghijklmnopqrstuvwx"),
    ("discord webhook", "https://discord.com/api/webhooks/123456789012345678/abcDEF-ghiJKLmnoPQRstuVWXyz0123456789"
     "abcdefghijklmnopqrstuvwxyzABCDEF", "abcDEF-ghiJKLmnoPQRstuVWXyz"),
    ("pypi token", "twine upload -u __token__ -p pypi-AgEIcHlwaS5vcmcCJGFiY2RlZjEyLTM0NTYtNzg5MC1hYmNkLWVmMTIzNDU2Nzg5"
     "MAACKlszLCJ", "AgEIcHlwaS5vcmcCJGFiY2Rl"),
    ("sendgrid", 'sg = SendGridAPIClient("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")',
     "TwL2iGABf9DHoTf-09kqeF8tAmbihYzrnopKc"),
    ("google oauth client secret (code)", 'flow = Flow(secret="GOCSPX-abcdefghijklmnopqrstuvwx12")',
     "abcdefghijklmnopqrstuvwx12"),
    ("stripe webhook secret", 'stripe.Webhook.construct_event(p, sig, "whsec_abcdefghijklmnopqrstuvwxyz123456")',
     "abcdefghijklmnopqrstuvwxyz123456"),
    ("digitalocean", "doctl auth init -t dop_v1_" + "0123456789abcdef" * 4, "0123456789abcdef0123456789abcdef"),
    ("shopify", 'headers = {"X-Shopify-Access-Token": "shpat_0123456789abcdef0123456789abcdef"}',
     "0123456789abcdef0123456789abcdef"),
    ("vault", "vault login hvs.CAESIJ0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUV", "CAESIJ0123456789"),
    ("databricks", 'w = WorkspaceClient(host=h, token="dapi0123456789abcdef0123456789abcdef")',
     "dapi0123456789abcdef0123456789abcdef"),
    ("twilio api key", 'Client("SK0123456789abcdef0123456789abcdef", "x")', "SK0123456789abcdef0123456789abcdef"),
    ("azure storage", "DefaultEndpointsProtocol=https;AccountName=acct1;AccountKey=Zm9vYmFyYmF6cXV4Zm9vYmFyYmF6cXV4Zm9v"
     "YmFyYmF6cXV4==;EndpointSuffix=core.windows.net", "Zm9vYmFyYmF6cXV4Zm9v"),
    ("azure SAS url", "https://acct.blob.core.windows.net/c/f.txt?sv=2022-11-02&sp=r&se=2030-01-01T00:00:00Z"
     "&sig=AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdEFG%3D", "AbCdEfGhIjKlMnOpQrStUvWxYz"),
    ("pgp", "-----BEGIN PGP PRIVATE KEY BLOCK-----\n\nlQOYBGZabc0123456789abcdefghij\n=abcd\n"
     "-----END PGP PRIVATE KEY BLOCK-----", "lQOYBGZabc0123456789"),
    ("redis url, password only", "REDIS_URL used: redis://:s3cr3tPassw0rd123@cache.example.com:6379/0",
     "s3cr3tPassw0rd123"),
    ("x-api-key header in curl", 'curl -H "X-API-Key: 7b7cbb957b075f864fe5c8cc26c36bb8" https://api.example.com/v1',
     "7b7cbb957b075f864fe5c8cc26c36bb8"),
    ("api key query param", "GET https://api.openweathermap.org/data/2.5/weather?q=London&appid="
     "d4b29a866c667b8e7330d199b4485fc8", "d4b29a866c667b8e7330d199b4485fc8"),
    ("api_key query param", "https://example.com/v1/x?api_key=d4b29a866c667b8e7330d199b4485fc8&page=2",
     "d4b29a866c667b8e7330d199b4485fc8"),
    ("env api key in prose", "set OPENWEATHER_API_KEY=d4b29a866c667b8e7330d199b4485fc8 and run",
     "d4b29a866c667b8e7330d199b4485fc8"),
]


@pytest.mark.parametrize("label,text,leak", PLANTED, ids=[p[0] for p in PLANTED])
def test_planted_secret_is_redacted_and_idempotent(label, text, leak):
    out = R.text(text)
    assert leak not in out, out
    assert R.text(out) == out


@pytest.mark.parametrize("text", [
    "redis://localhost:6379/0", "https://discord.com/api/webhooks/", "see the SK-1 ticket and dop_v1 docs",
    "https://example.com/search?q=apple&page=2&key=value", "GET /weather?q=London&appid=YOUR_APP_ID",
    "the sig= parameter carries the signature", "dapi tokens start with dapi", "shpat_ tokens are Shopify's",
])
def test_ordinary_text_stays(text):
    assert R.text(text) == text


@pytest.mark.parametrize("text", ["mysql -ppasswordmysql -p-p", "mysql -pa -pb -pc", "mysqldump -pX1y2Z3w4 db",
                                  "mysql -p'secret12' mysql -p", "curl -u a:b -u c:d"])
def test_redaction_is_idempotent_on_repeated_flags(text):
    once = R.text(text)
    assert R.text(once) == once, (text, once)


def test_property_idempotent_and_bounded():
    hypothesis = pytest.importorskip("hypothesis")    # optional, test-only: skipped when it is not installed
    st = hypothesis.strategies

    @hypothesis.settings(max_examples=300, deadline=None)
    @hypothesis.given(st.text(alphabet="aA0_-.:=/@'\" \n-pmysqlcurlpasswordtokenkeyhvsSGx", max_size=200))
    def check(s):
        once = R.text(s)
        assert R.text(once) == once
        assert len(once) <= len(s) + 64 * (s.count("=") + s.count(":") + s.count("-") + s.count("_") + 4)
    check()
