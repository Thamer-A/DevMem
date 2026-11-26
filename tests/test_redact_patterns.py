"""Redaction: secret shapes are caught, ordinary code is left alone."""
from __future__ import annotations

import pytest

from devmem.redact import Redactor, is_secret_file, mentions_secret_file

R = Redactor(None)

# test values are assembled at runtime so the repository never contains a literal token
GH = "ghp_" + "a1B2c3D4e5F6" * 3
ANT = "sk-ant-api03-" + "Zx9_Yw8-Vu7" * 4
OAI = "sk-proj-" + "q1W2e3R4t5Y6u7I8" * 2
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
GOOGLE = "AIza" + "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY"
SLACK = "xoxb-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"
STRIPE = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
JWT = "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA7\nabcDEF==\n-----END RSA PRIVATE KEY-----"


@pytest.mark.parametrize("secret,kind", [
    (GH, "github-token"), (ANT, "anthropic-key"), (OAI, "openai-key"), (AWS, "aws-access-key-id"),
    (GOOGLE, "google-api-key"), (SLACK, "slack-token"), (STRIPE, "stripe-key"), (JWT, "jwt"),
])
def test_token_formats(secret, kind):
    out = R.text(f"token was {secret} in the log")
    assert secret not in out and f"[REDACTED:{kind}]" in out


def test_private_key_block_and_truncated_block():
    out = R.text(f"key:\n{PEM}\nafter")
    assert "MIIEow" not in out and out.endswith("after")
    cut = R.text("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA")  # cut off by a 4,000-char cap
    assert "b3Blbn" not in cut


@pytest.mark.parametrize("text,leak", [
    ('curl -H "Authorization: Bearer abcdef0123456789abcdef0123"', "abcdef0123456789abcdef0123"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZDEyMzQ1Njc4", "dXNlcjpwYXNzd29yZDEyMzQ1Njc4"),
    ("postgres://admin:S3cr3tPass@db.internal:5432/app", "S3cr3tPass"),
    ('api_key = "a8f5f167f44f4964e6c998dee827110c"', "a8f5f167f44f4964e6c998dee827110c"),
    ('{"password": "correct-horse-9"}', "correct-horse-9"),
    ("DB_PASSWORD=hunter2hunter2", "hunter2hunter2"),
    ("export GITHUB_TOKEN=abc123def456ghi", "abc123def456ghi"),
    ("client_secret: 9f8e7d6c5b4a3210", "9f8e7d6c5b4a3210"),
    ("password: 'Tr0ub4dor&3'", "Tr0ub4dor&3"),
    ('SECRET_KEY = "django-insecure-12345abcdef"', "django-insecure-12345abcdef"),
    # pasted into a prompt mid-sentence (missed before the end-to-end run caught it)
    ("(Fake test credential, ignore it: DB_PASSWORD=hunter2hunter2.) Reply", "hunter2hunter2"),
    ("run with `API_KEY=abc.def.123456` set", "abc.def.123456"),
])
def test_headers_urls_and_assignments(text, leak):
    out = R.text(text)
    assert leak not in out, out
    assert "[REDACTED" in out


@pytest.mark.parametrize("code", [
    "token = tokenize(text)",
    "password: str",
    "secret: SecretStr = Field(...)",
    'api_key = os.environ["API_KEY"]',
    'API_KEY = os.getenv("API_KEY")',
    'password = "<your-password>"',
    'token = "${GITHUB_TOKEN}"',
    "max_tokens: 4096",
    'max_tokens = "4096"',
    'token_type = "bearer"',
    'password_field: "Enter your password"',
    "def check_password(password, hashed):",
    "if not token:\n    raise AuthError('missing token')",
    "Bearer tokens are sent in the Authorization header.",
    'TOKENIZERS_PARALLELISM=false',
    "local SECRET_SAUCE = require(script.Parent.Config)",
    "AUTHOR=Sam",
    "https://example.com/docs/api#password-reset",
    "git commit -m 'fix login: password reset flow'",
    "for (const token of tokens) { emit(token) }",
    'return "hello " + name',
    "CREATE TABLE users (id INT, password_hash TEXT)",
])
def test_ordinary_code_untouched(code):
    assert R.text(code) == code


def test_user_patterns_and_disable(tmp_path):
    from devmem.consent import enable_init
    project = tmp_path / "project"
    store = project / ".devmemory"
    store.mkdir(parents=True)
    (store / "redact").write_text("# internal ids\nACME-[0-9]{6}\n(unclosed\n", encoding="utf-8")
    enable_init(project)                                 # the user sets the project up: this redact file is approved
    r = Redactor(store, project=project)
    assert r.text("ticket ACME-123456 done") == "ticket [REDACTED:custom] done"
    (store / "config.json").write_text('{"redact": false}', encoding="utf-8")
    assert Redactor(store, project=project).text(f"x {GH}") == "x [REDACTED:github-token]"   # not allowed
    enable_init(project, allow_unredacted=True)          # `devmem init --allow-unredacted`, outside the project
    assert Redactor(store, project=project).text(f"x {GH}") == f"x {GH}"
    enable_init(project)                                 # init again without the flag takes the permission back
    assert Redactor(store, project=project).text(f"x {GH}") != f"x {GH}"


def test_obj_redacts_nested_strings_only():
    data = {"a": [GH, 3, None, {"b": f"Bearer {'x' * 20}"}], "token_count": 5}
    out = R.obj(data)
    assert GH not in str(out) and out["token_count"] == 5 and out["a"][1] == 3


@pytest.mark.parametrize("path,secret", [
    (".env", True), ("config/.env.local", True), ("C:\\proj\\certs\\server.pem", True), ("id_rsa.pub", True),
    ("src/aws_credentials.json", True), ("secrets/db.yaml", True), ("SECRET.txt", True),
    ("src/app.py", False), ("README.md", False), ("src/environment.ts", False), ("keyboard.lua", False),
])
def test_secret_files(path, secret):
    assert is_secret_file(path) is secret


def test_commands_touching_secret_files():
    assert mentions_secret_file("cat .env")
    assert mentions_secret_file('type "config\\.env.production"')
    assert not mentions_secret_file("pytest -q tests/")
    assert not mentions_secret_file("git status")


# ---- short secret names and command-line passwords (DB_PASS=..., mysql -p...), both directions ----

@pytest.mark.parametrize("text,leak", [
    ("DB_PASS=hunter2hunter2", "hunter2hunter2"),
    ("./.env:DB_PASS=hunter2hunter2", "hunter2hunter2"),          # grep output of a .env line
    ("export PGPWD=s3cretvalue", "s3cretvalue"),
    ("MYSQL_PWD=abcdef12", "abcdef12"),
    ("set REDIS_PASSWD=abcdef12", "abcdef12"),
    ("STRIPE_KEY=4eC39HqLyjWDarjt", "4eC39HqLyjWDarjt"),
    ("MASTER_KEY=a1b2c3d4e5f6g7", "a1b2c3d4e5f6g7"),
    ('db_pass = "hunter22"', "hunter22"),
    ("pass = 'hunter22'", "hunter22"),
    ("  - password: abc12345", "abc12345"),
    ("AUTH_TOKEN: abc123def456", "abc123def456"),
    ("mysql -u root -phunter2 mydb", "hunter2"),
    ("mysqldump -h db -u x -p'hunter22' app", "hunter22"),
    ("mariadb -uroot -pS3cr3t! shop", "S3cr3t!"),
    ("psql --password=hunter22 app", "hunter22"),
    ("gh auth --token ghx123456789", "ghx123456789"),
    ("cli --api-key=abc123def456", "abc123def456"),
])
def test_short_secret_names_and_password_arguments(text, leak):
    out = R.text(text)
    assert leak not in out and "[REDACTED" in out, out


@pytest.mark.parametrize("code", [
    "password_field = models.CharField()",
    "max_tokens=100",
    "max_tokens=100000",
    "MAX_TOKENS=100000",
    "mysql -u root -p mydb",                      # bare -p: mysql prompts for the password
    "docker run -p8080:80 img",
    "ssh -p22 host",
    "pip install -pfoo",
    "--token $GITHUB_TOKEN",
    'bypass = "enabled1"',
    "pass_through = 'abcdefgh'",
    "COMPASS_HEADING=north12345",
    "CACHE_KEY=user_profile",
    "SORT_KEY=created_at1234",
    "PRIMARY_KEY=id_field_12345",
    'sort_key = "created_at"',
    "monkey = 'bananas1'",
    "PWD=/home/me/project",
    "OLDPWD=/home/me",
    "keyboard_layout: us-intl-2",
    "if args.password_file: load(args.password_file)",
    "for key, value in items.items():",
])
def test_ordinary_code_with_secret_like_words_untouched(code):
    assert R.text(code) == code


def test_tool_inputs_that_name_a_secret_file(tmp_path):
    from devmem.redact import input_names_secret_file, omit_secret_file_lines
    proj = tmp_path / "secret-project"  # the project's own folder name never counts
    assert input_names_secret_file({"pattern": "=", "path": str(proj / ".env")}, proj)
    assert input_names_secret_file({"path": ".env"}, proj)
    assert input_names_secret_file({"paths": ["src/a.py", "config/.env.local"]}, proj)
    assert input_names_secret_file({"source": "certs/server.pem", "destination": "/tmp/x"}, proj)
    assert input_names_secret_file({"notebook_path": "id_rsa"}, proj)
    assert not input_names_secret_file({"path": str(proj / "src")}, proj)
    assert not input_names_secret_file({"pattern": ".env", "path": str(proj)}, proj)  # searching FOR .env
    # a command value is checked as a shell command (mentions_secret_file), under any command-like key (round 6)
    assert input_names_secret_file({"command": "cat .env"}, proj)
    assert input_names_secret_file({"cmd": "cat .e''nv"}, proj)
    out = "src/app.py:3:x = 1\n./.env:DB_PASS=hunter2\nC:\\p\\.env:2:TOKEN=abc\nconfig/secrets.yaml:4:k: v\nplain: text"
    kept = omit_secret_file_lines(out)
    assert "hunter2" not in kept and "TOKEN=abc" not in kept and "k: v" not in kept
    assert "src/app.py:3:x = 1" in kept and "plain: text" in kept and "./.env:" in kept


# ---- the key decides, not the object path: `token.markup = "linkify"` is not a secret (round-2 review) ----

@pytest.mark.parametrize("code", [
    'token.markup = "linkify"',
    'token.info = "autolink"',
    'token.tag = "strong" if isStrong else "em"',
    'Token.String: "#AB0909",',
    'Token.Name.Builtin.ContextProperty: "#038B8B",',
    'Token:                      "#d8dee9",',              # a colour, however far it is aligned
    "'getpass': 'getpass#module-getpass',",
    'secret.kind = "symmetric"',
    'password.hint = "use8chars"',
    'git commit -m "rotate --token handling"',
    "mysql --password mydb",                               # --password with a space prompts in mysql
    "- command-line passwords: ``--password=...``, ``--token <value>``",
    "`--api-key=<value>`, `curl -u user:password`",
    "curl -u $USER:$PASS https://example.com",
    "    password = getpass.getpass()",
    "token = state.push('link_open', 'a', 1)",
    "api_key = settings.API_KEY",
    "password = pw",
    "secret = True",
    "x = password = generate(len);",
    "password=password;",
])
def test_dotted_paths_flags_and_code_stay_intact(code):
    assert R.text(code) == code


@pytest.mark.parametrize("text,leak", [
    ('self.api_key = "abcdef123456"', "abcdef123456"),
    ('config.db.password = "hunter22"', "hunter22"),
    ('"spring.datasource.password": "S3cretPass"', "S3cretPass"),
    ('x = {"password":                         "hunter22"}', "hunter22"),  # aligned with many spaces
    # INI / .properties / lowercase dotenv lines
    ("db_password=hunter22", "hunter22"),
    ("spring.datasource.password=S3cretPass", "S3cretPass"),
    ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI/K7MDENG"),
    ("  export db_pass=hunter22  # dev only", "hunter22"),
    # connection strings, curl, --password with a space
    ("Server=db;Database=app;User Id=sa;Password=p@ss w0rd;", "p@ss w0rd"),
    ('"Host=db;Username=app;Pwd=Tr0ub4dor;"', "Tr0ub4dor"),
    ("curl -u admin:hunter22 https://example.com", "hunter22"),
    ("curl --user 'admin:hunter22' https://example.com", "hunter22"),
    ("tool --password Hunter2024 --verbose", "Hunter2024"),
    ("deploy --token ghx123456789", "ghx123456789"),
])
def test_dotted_keys_ini_lines_connection_strings_and_curl(text, leak):
    out = R.text(text)
    assert leak not in out and "[REDACTED" in out, out


def test_secret_files_in_commands_grep_context_and_diffs():
    from devmem.redact import filter_tool_output, mentions_secret_file, omit_secret_file_lines
    for cmd in ("git show HEAD:.env", "git show v1.2:config/.env", "cat *.pem", "cat .env*",
                "Get-Content .env | Select-String KEY", "sort < .env | head", r"type C:\proj\.env",
                "python -c \"print(open('.env').read())\""):
        assert mentions_secret_file(cmd), cmd
    for cmd in ("git show HEAD:app.py", "ls *.py", "echo a:b", "grep -rn KEY ."):
        assert not mentions_secret_file(cmd), cmd
    grep = "./.env-1-OPENWEATHER=0f9a8b7c\n./.env:2:DB_HOST=prod\n./app-v2.py-3-x\n--\nsrc/my-app.py:4:x"
    kept = omit_secret_file_lines(grep)
    assert "0f9a8b7c" not in kept and "DB_HOST" not in kept
    assert "./app-v2.py-3-x" in kept and "src/my-app.py:4:x" in kept and kept.count("[OMITTED") == 2
    log = ("commit abc\nAuthor: me\n\n    rotate\n\ndiff --git a/.env b/.env\nindex 1..2 100644\n--- a/.env\n"
           "+++ b/.env\n@@ -1 +1 @@ ctx\n-OPENWEATHER=0f9a8b7c\n+OPENWEATHER=ffff0000\n--- x\n+++ y\n DB=1\n"
           "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\ncommit def\n")
    kept = omit_secret_file_lines(log)
    assert "0f9a8b7c" not in kept and "ffff0000" not in kept and "DB=1" not in kept
    assert "+x = 2" in kept and "commit def" in kept and "    rotate" in kept and "[OMITTED: diff" in kept
    assert "0f9a" not in omit_secret_file_lines("diff .env .env.example\n1c1\n< KEY=0f9a8b\n---\n> KEY=\n")
    assert filter_tool_output("Bash", {"command": "git show HEAD:.env"}, "K=0f9a")[1] is True
    assert filter_tool_output("mcp__git__git_diff", {"repo": "."}, log)[0] == kept


# ---- formats added after the security review (fake values, assembled at runtime) ----
B64 = "QWxhZGRpbjpvcGVuIHNlc2FtZQ" * 2
DOCKER_AUTH = "dXNlcjpwYXNzd29yZDEyMw=="            # base64 of "user:password123"
SECRETS = {
    "pgp": ("-----BEGIN PGP PRIVATE KEY BLOCK-----\n\nlQOYBGVfakeKEYdata0123\n=AbCd\n"
            "-----END PGP PRIVATE KEY BLOCK-----", "lQOYBGVfakeKEYdata0123"),
    "json-escaped": ('"{' + '\\"api_key\\": \\"' + "Zx9fake7Qw2fake4" + '\\"}"', "Zx9fake7Qw2fake4"),
    "json-double-escaped": ('{\\\\\\"password\\\\\\": \\\\\\"' + "Hu2nt3rFake99" + '\\\\\\"}', "Hu2nt3rFake99"),
    "azure-account-key": ("DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=" + B64
                          + "==;EndpointSuffix=core.windows.net", B64),
    "azure-sas-key": ("Endpoint=sb://ns.servicebus.windows.net/;SharedAccessKeyName=Root;SharedAccessKey="
                      + B64 + "=", B64),
    "slack-webhook": ("post to https://hooks.slack.com/services/T0FAKE1234/B0FAKE5678/" + "aBcDeFgHiJkLmNoPqRsTuVwX",
                      "aBcDeFgHiJkLmNoPqRsTuVwX"),
    "sendgrid": ("key SG." + "aBcDeFgHiJkLmNoPqRsTuV" + "." + "aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdefg",
                 "aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdefg"),
    "pypi": ("password = pypi-" + "AgEIcHlwaS5vcmcCJGFiY2RlZmdoLWlqa2wtbW5vcC1xcnN0LXV2d3h5ejAxMjM0NQ",
             "AgEIcHlwaS5vcmcCJGFiY2RlZmdo"),
    "pypi-bare": ("TWINE_PASS " + "pypi-" + "AgEIcHlwaS5vcmcCJGFiY2RlZmdoLWlqa2wtbW5vcC1xcnN0LXV2d3h5ejAxMjM0NQ",
                  "AgEIcHlwaS5vcmcCJGFiY2RlZmdo"),
    "google-oauth-secret": ('{"client_id": "x.apps.googleusercontent.com", "web": "GOCSPX-'
                            + "aBcDeFgHiJkLmNoPqRsTuVwXyZ01" + '"}', "aBcDeFgHiJkLmNoPqRsTuVwXyZ01"),
    "x-api-key": ("X-API-Key: " + "a1b2c3d4e5f6a7b8c9d0e1f2", "a1b2c3d4e5f6a7b8c9d0e1f2"),
    "curl-x-api-key": ("curl -H 'X-API-Key: " + "a1b2c3d4e5f6a7b8c9d0e1f2' https://api.example.com",
                       "a1b2c3d4e5f6a7b8c9d0e1f2"),
    "authorization-token": ("Authorization: Token " + "0123456789abcdef0123456789abcdef01234567",
                            "0123456789abcdef0123456789abcdef01234567"),
    "curl-authorization-token": ('curl -H "Authorization: Token ' + "9f8e7d6c5b4a39281706f5e4d3c2b1a0" + '" x',
                                 "9f8e7d6c5b4a39281706f5e4d3c2b1a0"),
    "npmrc": ("//registry.example.com/:_authToken=" + "0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d",
              "0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d"),
    "npmrc-auth": ("//registry.example.com/:_auth=" + DOCKER_AUTH, DOCKER_AUTH),
    "docker-config-auth": ('{"auths": {"https://index.docker.io/v1/": {"auth": "' + DOCKER_AUTH + '"}}}',
                           DOCKER_AUTH),
    "telegram": ("bot 123456789:" + "AAFakeTokenFakeTokenFakeToken12345_", "FakeTokenFakeTokenFakeToken12345_"),
    "stripe-webhook": ("whsec_" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345", "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
    "vault": ("VAULT hvs." + "CAESIFakeVaultTokenFakeVaultToken0123456789", "CAESIFakeVaultTokenFakeVault"),
    "slack-app-token": ("xapp-" + "1-A0FAKE1234-1234567890123-" + "abcdef0123456789abcdef0123456789",
                        "abcdef0123456789abcdef0123456789"),
}


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_more_secret_formats_are_redacted(name):
    text, leak = SECRETS[name]
    out = R.text(text)
    assert leak not in out and "[REDACTED" in out, out
    assert R.text(out) == out   # idempotent


@pytest.mark.parametrize("text", [
    "auth: true",
    '"auth": "basic"',
    '"auth": "required"',
    '{"auth": "oauth2-flow"}',
    "See SG.1 and SG.Appendix for details.",
    "python 3.10.12 and node v20.11.1-beta.4",
    "request 123e4567-e89b-12d3-a456-426614174000 failed",
    '"integrity": "sha512-z4PhNX7vuL3xVChQ1m2AB9Yg5AULVxXcg/SpIdNs6c5H0NE8XYXysP+DGNKHfuwvY7kxvUR/a6XhlHtzc+r8Ag=="',
    'hash = "sha256:6a1f9d1d7c2b8e4f5a3c0b9e8d7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f"',
    "Authorization: Token <your-token>",
    "Authorization header: Token based auth is used",
    "X-API-Key: ${API_KEY}",
    "X-API-Key: your-api-key-here",
    "AccountKey=<storage-key>;",
    "SharedAccessKeyName=RootManageSharedAccessKey",
    "//registry.npmjs.org/:_authToken=${NPM_TOKEN}",
    "meet at 10:30, port 8080:80, ratio 123456789:42",
    "the whsec_ prefix marks Stripe webhook secrets; hvs. marks Vault service tokens",
    "pip install pypi-server and GOCSPX- is the client secret prefix",
    "xapp- tokens are app-level tokens",
    "https://hooks.slack.com/services/",
])
def test_new_rules_leave_ordinary_text_alone(text):
    assert R.text(text) == text
