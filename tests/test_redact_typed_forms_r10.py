"""Typed-code forms and printed structures that verifier v9b found missed (item 4): each is redacted now, with the same
form holding code (a call, a reference, a type, an empty or placeholder default) kept beside it."""
from __future__ import annotations

import pytest

from devmem.redact import Redactor

V = "Xk4" + "pQ9" + "Lm2zT8"

# (redacted: a literal in this form, kept: the same form without one)
PAIRS = [
    ("ts optional field", f'  apiKey?: string = "{V}";', "  apiKey?: string = process.env.API_KEY;"),
    ("ts definite field", f'  apiKey!: string = "{V}";', "  apiKey!: string;"),
    ("c char array", f'char password[] = "{V}";', "char password[64];"),
    ("c sized const array", f'static const char API_KEY[32] = "{V}";', "static const char API_KEY[32];"),
    ("c# property initializer", f'    public string Password {{ get; set; }} = "{V}";',
     "    public string Password { get; set; } = string.Empty;"),
    ("rust byte slice", f'const API_KEY: &[u8] = b"{V}";', 'const API_KEY: &[u8] = env!("API_KEY").as_bytes();'),
    ("rust raw byte string", f'const API_KEY: &[u8] = br#"{V}"#;', "    api_key: &'a [u8],"),
    ("go var block", f'\tapiSecret string = "{V}"', '\tapiSecret string = os.Getenv("API_SECRET")'),
    ("swift raw string", f'let password = #"{V}"#', "let password = try keychain.read()"),
    ("spring default", f'@Value("${{db.password:{V}}}")', '@Value("${db.password}")'),
    ("shell default", f'PGPASSWORD="${{DB_PASSWORD:-{V}}}" psql', 'PGPASSWORD="${DB_PASSWORD:-}" psql'),
    ("compose default", f"      POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD:-{V}}}",
     "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set it}"),
    ("go %+v", f"config: {{Host:db User:admin Password:{V} Port:5432}}", "config: {Host:db User:admin Password:<nil> Port:5432}"),
    ("go %v map", f"map[password:{V} user:admin]", "map[password:<nil> user:admin]"),
    ("php print_r", f"    [password] => {V}", "    [password] => "),
    ("js env fallback", f'const apiKey = process.env.API_KEY || "{V}";', "const apiKey = process.env.API_KEY || '';"),
    ("ts env fallback ??", f"const token = process.env['AUTH_TOKEN'] ?? '{V}';", "const token = process.env['AUTH_TOKEN'] ?? null;"),
    ("php getenv ?:", f"$key = getenv('API_KEY') ?: '{V}';", "$key = getenv('API_KEY') ?: null;"),
    ("ruby ENV ||", f'pw = ENV["DB_PASSWORD"] || "{V}"', 'pw = ENV["DB_PASSWORD"] || raise("unset")'),
    ("ruby ENV.fetch", f'pw = ENV.fetch("DB_PASSWORD", "{V}")', 'pw = ENV.fetch("DB_PASSWORD")'),
    ("python getenv", f'password = os.getenv("DB_PASSWORD", "{V}")', 'password = os.getenv("DB_PASSWORD", "")'),
    ("python environ.get", f'token = os.environ.get("API_TOKEN", "{V}")', 'token = os.environ.get("API_TOKEN")'),
    ("laravel env", f"'password' => env('DB_PASSWORD', '{V}'),", "'password' => env('DB_PASSWORD'),"),
    ("c# env ??", f'var key = Environment.GetEnvironmentVariable("API_KEY") ?? "{V}";',
     'var key = Environment.GetEnvironmentVariable("API_KEY") ?? string.Empty;'),
]


@pytest.mark.parametrize("label,secret,code", PAIRS)
def test_typed_forms_redacted_and_code_kept(label, secret, code):
    red = Redactor(None, enabled=True)
    out = red.text(secret)
    assert V not in out and "[REDACTED:" in out, (label, out)
    assert red.text(code) == code, label


# false positives of v9b's corpora that are names, rules or types, not credentials (item 5c)
KEPT = [
    "export const ACCESS_TOKEN_KEY = 'accessToken';",
    '    public static final String PASSWORD_PARAM = "password";',
    'AUTH_TOKEN_COOKIE = "session_token"',
    'export const TOKEN_KEY = "auth_token";',
    "            'password' => 'required|string|min:8|confirmed',",
    "'password' => 'required|min:8',",
    "token\tTOKEN_TYPE_ACCESS",
    "\tPassword *string",
    "\tPassword []byte",
    '{"source": ["env:\\n", "    valueFrom:\\n", "      secretKeyRef:\\n", "        name: db\\n"]}',   # JSON text
]


@pytest.mark.parametrize("line", KEPT)
def test_names_rules_and_types_are_kept(line):
    assert Redactor(None, enabled=True).text(line) == line


STILL = [
    f'SECRET_KEY = "{V}"',
    'SECRET_KEY = "django-insecure-' + V + '"',
    f'API_KEY = "{V}"',
    f"'password' => '{V}',",
    f"token\t{V}",
]


@pytest.mark.parametrize("line", STILL)
def test_the_same_names_with_a_credential_are_still_redacted(line):
    assert V not in Redactor(None, enabled=True).text(line)
