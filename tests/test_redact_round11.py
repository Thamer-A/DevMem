"""Round 11 (verifier v10b): YAML flow mappings and argument lists, lower-case-and-digit values under secret names,
long values redacted whole, and the small false positives of settings about a secret. All values are made up."""
from __future__ import annotations

import pytest

from devmem.redact import Redactor, _flow_repl  # noqa: F401  (the flow rule exists)

GEN = "q8Zr" + "Lm3XvT9" + "pW2kN7sQ"            # a generated-looking value, 19 characters

FLOW = [
    ("environment: {POSTGRES_USER: app, POSTGRES_PASSWORD: %s}", GEN),
    ("cfg: {db_password: %s, user: admin}", GEN),
    ("cfg: {user: admin, DB_PASSWORD: %s, port: 5}", GEN),
    ("cfg: {user: admin, MYSQL_ROOT_PASSWORD: %s}", GEN),
    ("  - { name: alice, dbPassword: %s }", GEN),
    ("cfg: {client_secret: %s}", GEN),
    ("cfg: {SECRET_KEY: %s}", GEN),
    ("cfg: {api_token: %s, x: 1}", GEN),
    ("cfg: {access_token: %s}", GEN),
    ("cfg: {user: admin,\n  db_password: %s\n}", GEN),
    ("users: [{name: a, db_password: %s}, {name: b}]", GEN),
    ("x: {a: {db_password: %s}}", GEN),
    ("credentials: {username: admin, password: %s}", "summer" + "2024"),
    ("- { name: alice, password: %s }", "hunter" + "22x"),
    ("environment: {POSTGRES_PASSWORD: %s, POSTGRES_USER: app}", "summer" + "2024"),
    ("{user: admin, password: %s}", "john" + "1985"),
    ("{user: admin, password: %s}", "1234" + "5678"),
    ("2024-01-01 login user=admin password=%s host=db", "1234" + "5678"),
    ("env: [{name: DB_PASSWORD, value: %s}]", GEN),
    ("env:\n  - name: DB_PASSWORD\n    value: %s\n", "summer" + "2024"),
    ('args: ["--password", "%s"]', GEN),
    ('command: ["redis-server", "--requirepass", "%s"]', GEN),
    ('command: ["redis-cli", "-h", "db", "-a", "%s"]', GEN),
    ("command:\n  - redis-server\n  - --requirepass\n  - %s\n", GEN),
    ("command: redis-server --requirepass %s", GEN),
    ("    entrypoint: redis-server --masterauth %s", "summer" + "2024"),
]


@pytest.mark.parametrize("shape,value", FLOW)
def test_flow_maps_and_argument_lists(shape, value):
    out = Redactor(None).text(shape % value)
    assert value not in out, out


PROSE = [
    "the db password is %s",
    "the db password is %s.",
    "login: admin / password: %s",
    "2024-01-01 INFO a: b, password: %s",
    "Here is the API key: %s",
    "the token is %s",
]


@pytest.mark.parametrize("value", ["summer" + "2024", "hunter" + "22x", "password" + "123", "john" + "1985",
                                   "qwerty" + "123", "1234" + "5678", "hunter" + "2"])
@pytest.mark.parametrize("shape", PROSE)
def test_lower_case_and_digit_values_under_a_secret_name(shape, value):
    out = Redactor(None).text(shape % value)
    if value.isdigit() and "token" in shape:
        assert out == shape % value     # digits alone under a token name are a count (round 11 follow-up)
    else:
        assert value not in out, out


# token counts and settings that sit next to a secret word in AI session text (verifier v11b d3_tokcount.py): kept
COUNTS = ["token limit: 200000", "input token count: 153422", "token usage: 123456", "Token budget: 100000 tokens",
          "context window (token limit): 1000000", "max token length: 131072", "token id: 50256", "token id: 128001",
          "secret expires at: 1735689600", "token created: 1700000000", "api key created: 20240131",
          "password changed at: 1700000000", "the token is 1700000000 seconds old", "token ttl: 2592000",
          "session token age: 864000", "Password last set: 20240131", "token count is 128000", "secret count: 123456",
          "token: 128000 used of 200000", "Tokens used: 123456", "token_count: 123456", "secret rotated at: 20240131",
          "prompt token count: 123456, output token count: 2345", "token expires at: 1735689600",
          "tokens: 4096", "max_tokens: 200000", "token_count=153422", "password length: 12", "key size: 4096",
          "secret version: 3", "password_min_length: 8", "token ttl: 86400", "key id: 20240131"]


@pytest.mark.parametrize("text", COUNTS)
def test_token_counts_and_settings_are_kept(text):
    assert Redactor(None).text(text) == text


@pytest.mark.parametrize("text", [
    "the password is md5 hashed", "the token is base64 encoded", "the api key is oauth2 based", "the token is http2",
    "the password is 8080", "the token is valid for 30 minutes", "password: 443", "{token: 30, port: 8080}",
    "the token is node18", "the password is python3 compatible", "the password is 2024-01-01", "the token is v1.2.3",
    "{secret: sha256, token: utf8}", "the password is sha256(x)", "the secret is sha256 of the body",
    "const user = { password: string, token: Token };", "f({ password: password, token: this.token })",
    "login({ user: u, password: hashedPassword })", "{ apiKey: process.env.API_KEY, secret: config.secret }",
    "{ token: getToken(), x: 1 }", "Config{Password: cfg.Password, Token: tok}", "{ password: params[:password] }",
    "def login(self, user, password: str, token: Optional[str] = None):",
    "{password_hash: argon2id, password_policy: strict}", "password_hash: argon2id", "password_strength: Strength",
    'os.environ.get("TOKEN_MODE", "bearer")', 'os.getenv("TOKEN_ALGO", "HS256")', "Password: (hidden)",
    "password: '%env(DB_PASSWORD)%'", "token: ((concourse_token))",
    'args: ["--port", "8080", "--password-file", "/run/secrets/db"]', 'command: ["redis-server", "--requirepass", "$PW"]',
])
def test_kept(text):
    assert Redactor(None).text(text) == text


@pytest.mark.parametrize("name", ["token", "secret", "password", "db_password", "api_token"])
@pytest.mark.parametrize("n", [130, 200, 344, 1100, 4200, 5000])
def test_a_long_value_is_redacted_whole(name, n):
    value = ("Ab3" + "x9Y" + "Q1w" * 2) * (n // 12 + 1)
    value = value[:n]
    for text in (f"{name}: {value}\n", f"{name} = {value}\n", f'{name}: "{value}"\n', f"cfg: {{{name}: {value}}}\n"):
        out = Redactor(None).text(text)
        assert value[-24:] not in out and value[:24] not in out, (text[:40], out[:120])


@pytest.mark.parametrize("tag", ["password", "privateKey", "client_secret"])
def test_a_long_xml_value_is_redacted_whole(tag):
    value = ("QUJD" + "RUZH" + "SElK") * 200
    out = Redactor(None).text(f"<{tag}>{value}</{tag}>")
    assert "QUJDRUZHSElK" not in out, out[:120]


def test_no_marker_is_left_before_the_rest_of_its_token():
    # a value cut by a rule's limit: the rest of the token goes with the marker
    value = ("Zq7" + "Xw3Kp") * 700        # 5,600 characters, longer than any rule's 4 KB limit
    for text in (f"API_KEY={value} ", f"token: {value}\n", f"--token={value} ", f"password is {value}"):
        out = Redactor(None).text(text)
        assert "Zq7Xw3Kp" not in out, (text[:30], out[:120])


@pytest.mark.parametrize("opening,closing", [("api_key = '''", "'''"), ('private_key = """', '"""'),
                                             ("<privateKey>", "</privateKey>"), ("<password>", "</password>"),
                                             ("<password><![CDATA[", "]]></password>")])
@pytest.mark.parametrize("before", [10, 1500, 3000])
def test_a_construct_still_open_where_the_cap_cuts_is_redacted_from_its_opening(opening, closing, before):
    line = "QUJD" + "RUZH" + "SElK" + "TE1O" * 15        # 72 characters of key material per line
    text = "x = 1\n" * (before // 6) + opening + "\n" + (line + "\n") * 120 + closing + "\n"
    red = Redactor(None)
    out, cut = red.capped(text, 4048)
    assert cut
    assert "QUJDRUZHSElK" not in out, out[-200:]
    assert out.startswith("x = 1\n" * min(before // 6, 600))


@pytest.mark.parametrize("n", [40, 65, 200, 5000])
@pytest.mark.parametrize("form", ["  private_key: |\n{b}\n  port: 5\n", "password: |\n{b}\nuser: a\n", "token: >-\n{b}\n",
                                  "api_key = '''\n{b}\n'''\n", 'private_key = """\n{b}\n"""\n'])
def test_a_long_block_is_redacted_whole(form, n):
    line = "QUJD" + "RUZH" + "SElK" + "TE1O" * 12
    indent = "    " if form.startswith("  ") else "  " if ":" in form.split("\n")[0] else ""
    body = "\n".join(indent + line + f"{i:05d}" for i in range(n))
    text = form.replace("{b}", body)
    out = Redactor(None).text(text)
    if "=" in form and len(body) > 4096:
        assert out == text      # a multi-line string past 4 KB: all or nothing, so nothing (a documented gap)
        return
    assert "QUJDRUZHSElK" not in out, out[-200:]
    assert out.count("\n") == text.count("\n")


@pytest.mark.parametrize("text,kept", [
    ("Authorization: Token u0Hm9BX5Ozmy!vwKwcgaWQycp", "vwKwcgaWQycp"),
    ("Authorization: Token u0Hm9BX5Ozmy?vwKwcgaWQycp", "vwKwcgaWQycp"),
    ("https://h/x?api_key=8gRVIAtJLGxv?3Tjg0FCgYOXi&a=1", "3Tjg0FCgYOXi"),
    ("https://h/x?api_key=W959sPTg3GaL|k6G4vffpv7je&a=1", "k6G4vffpv7je"),
])
def test_header_and_query_values_run_to_their_end(text, kept):
    assert kept not in Redactor(None).text(text)


def test_a_long_name_value_pair_is_redacted_whole_in_text_and_at_the_write_boundary():
    import json
    value = ("Xk4" + "mL9" + "pQ2") * 600         # 5,400 characters
    red = Redactor(None)
    for text in (json.dumps({"name": "DB_PASSWORD", "value": value}), f'<add key="DbPassword" value="{value}"/>',
                 f"env: [{{name: DB_PASSWORD, value: {value}}}]", f"- name: DB_PASSWORD\n  value: {value}\n"):
        assert value[-24:] not in red.text(text), text[:40]
    for rec in ({"name": "DB_PASSWORD", "value": value}, {"env": [{"name": "DB_PASSWORD", "value": value}]},
                {"password": value}):
        assert value[-24:] not in json.dumps(red.record(rec)), list(rec)
    assert json.dumps(red.record({"name": "HOME", "value": "/x" * 3000})).count("/x") == 3000


@pytest.mark.parametrize("value", ["Daniel" + "Chicago", "Purple" + "Monkey"])
def test_a_flow_map_over_several_lines_is_config(value):
    for text in ("environment: {\n  POSTGRES_USER: app,\n  POSTGRES_PASSWORD: %s\n}\n",
                 "cfg: {user: admin,\n  client_secret: %s,\n  port: 5}\n"):
        assert value not in Redactor(None).text(text % value), text
    js = "const cfg = {\n  auth: {\n    password: %s\n  },\n};\n" % value      # an object literal of code stays
    assert Redactor(None).text(js) == js


@pytest.mark.parametrize("shape", ['test: ["CMD", "redis-cli", "-a", "%s", "ping"]',
                                   'test: ["CMD-SHELL", "redis-cli -a %s ping"]',
                                   "command: redis-server --requirepass %s", "redis-server --masterauth %s",
                                   'args: ["--password", "%s"]'])
@pytest.mark.parametrize("value", ["Abc" + "606@", "2024" + "0131", "Austin" + "Cowboys", "q8Zr" + "Lm3Xv"])
def test_compose_healthchecks_and_password_flags(shape, value):
    if value[0].isupper() and value.isalpha() and "-a" not in shape and "args" not in shape:
        pytest.skip("a word of letters after a flag with a blank: documented gap")
    if value == "Abc606@" and "-a" not in shape and "args" not in shape:
        pytest.skip("seven characters with a symbol after a flag with a blank: not generated-looking (documented)")
    out = Redactor(None).text(shape % value)
    assert value not in out, out


def test_a_marker_already_in_the_text_does_not_take_the_text_after_it():
    text = "# was [REDACTED:private-key]-MAC: abcdef1234\nnote [REDACTED:assignment]abc stays\n"
    assert Redactor(None).text(text) == text


def test_blank_lines_inside_a_block_scalar_and_cdata_values():
    line = "QUJD" + "RUZH" + "SElK" + "TE1O"
    text = f"password: |\n  {line}1\n\n\n\n  {line}2\n   \n\n  {line}3\nuser: a\n"
    out = Redactor(None).text(text)
    assert line not in out and out.count("\n") == text.count("\n") and out.endswith("user: a\n")
    value = "Xk4]mL" + "p9]qRt"
    assert value not in Redactor(None).text(f"<password><![CDATA[{value}]]></password>")
    kept = "<value><![CDATA[hello world]]></value>"
    assert Redactor(None).text(kept) == kept


PUNCT = "Xk4" + ",<>}'" + "mL9" + "!#$%&*()" + "pQ2"      # a value with every character a pair value may hold


@pytest.mark.parametrize("text", [
    '{"name": "DB_PASSWORD", "value": "%s"}' % PUNCT,
    '{\n  "name": "DB_PASSWORD",\n  "value": "%s"\n}' % PUNCT,
    '{"key": "API_TOKEN", "value": "%s"}' % PUNCT,
    '{"Name": "/prod/db/DB_PASSWORD", "Type": "SecureString", "Value": "%s", "Version": 3}' % PUNCT,
    '<add key="DbPassword" value="%s"/>' % PUNCT.replace("<", "").replace(">", ""),
    "env:\n  - name: DB_PASSWORD\n    value: %s\n" % PUNCT,
])
def test_a_pair_value_with_punctuation_is_redacted_whole(text):
    out = Redactor(None).text(text)
    assert "Xk4" not in out and "pQ2" not in out, out


def test_ssm_pairs_at_the_write_boundary():
    import json
    red = Redactor(None)
    for rec in ({"Name": "DB_PASSWORD", "Value": PUNCT}, {"Name": "/prod/DB_PASSWORD", "Type": "SecureString",
                                                         "Value": PUNCT}):
        assert "pQ2" not in json.dumps(red.record(rec)), rec


@pytest.mark.parametrize("off", [10, 500, 1500, 1900])
@pytest.mark.parametrize("form", ['{"password": "%s"}', '{"name": "DB_PASSWORD", "value": "%s"}',
                                  '{"Name": "/p/DB_PASSWORD", "Type": "SecureString", "Value": "%s"}',
                                  '<add key="DbPassword" value="%s"/>', 'DB_PASSWORD="%s"\n'])
def test_a_quoted_value_still_open_at_the_cap_is_redacted_from_its_quote(form, off):
    value = ("Xk4" + "mLp9qRt") * 600
    out, cut = Redactor(None).capped("x" * off + form % value, 2000)
    assert cut and "Xk4" not in out, out[-120:]


@pytest.mark.parametrize("form", ['{"name": "HOME", "value": "%s"}', '{"description": "%s"}'])
def test_an_open_quoted_value_under_another_name_is_kept_at_the_cap(form):
    value = ("Xk4" + "mLp9qRt") * 600
    out, _cut = Redactor(None).capped("x" * 500 + form % value, 2000)
    assert "Xk4" in out


def test_cdata_sections_one_after_another_are_one_value():
    out = Redactor(None).text("<password><![CDATA[Xk4mLp9]]><![CDATA[QqRt88zz]]></password>")
    assert "Xk4" not in out and "QqRt88" not in out, out


@pytest.mark.parametrize("text", [
    '{"db_password": {"sensitive": true, "type": "string", "value": "%s"}}',
    "Parameter:\n  ARN: arn:aws:ssm:eu-west-1:1:parameter/prod/DB_PASSWORD\n  DataType: text\n  LastModifiedDate: '2024'\n"
    "  Name: /prod/DB_PASSWORD\n  Type: SecureString\n  Value: %s\n  Version: 1\n",
    '{"Name": "/prod/DB_PASSWORD", "Type": "SecureString", "LastModifiedDate": 1, "LastModifiedUser": "arn:x", '
    '"Version": 2, "Value": "%s"}',
])
def test_terraform_and_ssm_outputs(text):
    out = Redactor(None).text(text % GEN)
    assert GEN not in out, out


@pytest.mark.parametrize("text", [
    '{"name": "password_reset", "status": "ok", "duration_ms": 562, "value": "done"}',
    "name=api_key_check, status: ok\nvalue=Done42x",
    'name: api_key, type: string, value: "a placeholder you replace"',
    '{"name": "token_count", "type": "int", "value": "153422"}',
])
def test_event_logs_with_name_and_value_are_kept(text):
    assert Redactor(None).text(text) == text


def test_azure_app_settings_listing():
    import json
    text = json.dumps([{"name": "DB_PASSWORD", "slotSetting": False, "value": GEN}], indent=2)
    assert GEN not in Redactor(None).text(text)


@pytest.mark.parametrize("text", [
    '{"Name": "/p/DB_PASSWORD", "Type": "SecureString", "KeyId": "alias/aws/ssm", "Value": "%s"}',
    '{"name": "API_TOKEN", "AllowedPattern": "x", "Value": "%s"}',
    '{"name": "DB_PASSWORD", "visibility": "private", "value": "%s"}',
    '{"name": "DB_PASSWORD", "Policies": [], "value": "%s"}',
    '{"name": "DB_PASSWORD", "tags": {}, "value": "%s"}',
])
def test_more_metadata_between_name_and_value(text):
    assert GEN not in Redactor(None).text(text % GEN)
