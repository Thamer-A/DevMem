"""Typed code (verifier v8b, items 1 and 4): typed fields, parameters and schemas under secret-sounding names are code
and stay as written, while a typed declaration of a string literal is redacted. The two pull in opposite directions,
so they are checked side by side, per language (TABLE), and over a benign corpus of typed code
(redact_typed_corpus.BENIGN, 300+ lines, none may change).

Before the fix a field line such as `  password: string;` lost its type to the YAML rule (`[REDACTED:assignment]`:
`string;` holds a non-letter), a zod schema `password: z.string().min(8),` became `[REDACTED:assignment]),`, a tuple
`TOKEN_TYPES = (TokenType.A, TokenType.B)` lost its first element and a keyword argument on its own line
(`password='...')`) lost its quotes and the closing parenthesis.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import redact_typed_corpus as corpus  # noqa: E402

from devmem.redact import Redactor  # noqa: E402

V = corpus.V

# (language, typed code that must be kept as written)
KEPT: list[tuple[str, str]] = [
    ("typescript", "  password: string;"),
    ("typescript", "  apiKey: string,"),
    ("typescript", "  token: Token;"),
    ("typescript", "  refreshToken: string;"),
    ("typescript", "  passwordHash: string;"),
    ("typescript", "  secretKey: Uint8Array;"),
    ("typescript", "  password: Array<string>;"),
    ("typescript", "function hash(password: string, salt: Uint8Array): Promise<ArrayBuffer>;"),
    ("rust", "    password: String,"),
    ("rust", "    token: Option<String>,"),
    ("rust", "    token: Arc<Mutex<Token>>,"),
    ("python", "    secret: bytes,"),
    ("python", "    salt: bytes,"),
    ("python", "    private_key: rsa.RSAPrivateKey,"),
    ("python", "TOKEN_TYPES = (TokenType.A, TokenType.B)"),
    ("python", "PASSWORD_VALIDATORS = [Validator.MIN, Validator.COMMON]"),
    ("python", "    token=args.token)"),
    ("go", "\tSecret   []byte"),
    ("scala", "  password: String,"),
    ("graphql", "  password: String!"),
    ("schemas", "  password: z.string().min(8),"),
    ("schemas", "  secret: z.string().regex(/^[a-f0-9]{64}$/),"),
    ("schemas", "  password: Joi.string().min(8).required(),"),
    ("schemas", '  passwordConfirm: yup.string().oneOf([yup.ref("password")]),'),
    ("schemas", "  password: PropTypes.string,"),
    ("schemas", "  token: PropTypes.string.isRequired,"),
]

# a quoted value followed by a closing bracket keeps the bracket (and its quotes)
BRACKETS: list[tuple[str, str]] = [
    ("    passwd='geheim$parole')", "    passwd='[REDACTED:assignment]')"),
    ("conn = connect(\n    password='" + V + "')", "conn = connect(\n    password='[REDACTED:assignment]')"),
    ('    password="' + V + '"],', '    password="[REDACTED:assignment]"],'),
]

# the same names with a value that is a credential: still redacted
STILL_REDACTED: list[str] = [
    f"password: {V}",
    f"password: {V},",
    f"  token: {V};",
    f"password: abc.{V}",
    f"API_KEY=({V}",
    f"DB_PASSWORD   {V}",
]


# per language, side by side: (typed declaration of a literal: redacted, the same declaration with a call, a reference
# or no value: kept)
TABLE: list[tuple[str, str, str]] = [
    ("python", f'password: str = "{V}"', "password: str = get_password()"),
    ("python", f'API_KEY: Final = "{V}"', 'API_KEY: Final = os.environ["API_KEY"]'),
    ("python", f'    token: Optional[str] = "{V}"', "    token: Optional[str] = None,"),
    ("python", f'    secret: bytes = b"{V}"', "    secret: bytes,"),
    ("python", f"password = r'{V}'", 'password = f"{prefix}{suffix}"'),
    ("typescript", f'const password: string = "{V}";', "const password: string = process.env.PASSWORD!;"),
    ("typescript", f'  private apiKey: string = "{V}";', "  private apiKey: string;"),
    ("typescript", f"const token = `{V}`;", "const token = `${prefix}-${suffix}`;"),
    ("typescript", f"  password: `{V}`,", "  password: z.string().min(8),"),
    ("rust", f'const PASSWORD: &str = "{V}";', 'const PASSWORD: &str = env!("PASSWORD");'),
    ("rust", f"static API_KEY: &'static str = \"{V}\";", "    api_key: &'a str,"),
    ("rust", f'let password = r#"{V}"#;', "let password: String = read_password()?;"),
    ("rust", f'let token: &str = "{V}";', "    token: Option<String>,"),
    ("kotlin", f'val dbPassword: String = "{V}"', "val dbPassword: String = BuildConfig.DB_PASSWORD"),
    ("kotlin", f'val password = """{V}"""', "    val password: String,"),
    ("swift", f'let password: String = "{V}"', "    let password: String"),
    ("swift", f'var apiKey: String? = "{V}"', "    var apiKey: String? = nil"),
    ("scala", f'val password: String = "{V}"', '  val secret: String = config.getString("secret")'),
    ("go", f'var password string = "{V}"', '\tvar password string = os.Getenv("DB_PASSWORD")'),
    ("go", f"password := `{V}`", "\tPassword string `json:\"password\"`"),
    ("csharp", f'var password = @"{V}";', '    var secret = configuration["Secret"];'),
    ("csharp", f'string password = """{V}""";', "    public string Password { get; set; }"),
    ("java", f'private static final String PASSWORD = "{V}";', '    private static final String API_KEY = System.getenv("API_KEY");'),
    ("yaml", f"password: &db_pw {V}", "password: &db_pw"),
    ("yaml", f"password: !!str {V}", "password: !vault |"),
    ("yaml", f'password: !!str "{V}"', "password: *db_pw"),
]


@pytest.mark.parametrize("lang,text,secret", corpus.LITERALS)
def test_a_typed_declaration_of_a_literal_is_redacted(lang, text, secret):
    out = Redactor(None, enabled=True).text(text)
    assert secret not in out and "[REDACTED:assignment]" in out


@pytest.mark.parametrize("lang,literal,code", TABLE)
def test_typed_literal_redacted_and_typed_code_kept_side_by_side(lang, literal, code):
    red = Redactor(None, enabled=True)
    out = red.text(literal)
    assert V not in out, (lang, literal, out)
    assert out.count("[REDACTED:assignment]") == 1
    # only the value is replaced: the quotes and everything around them stay
    head, tail = out.split("[REDACTED:assignment]")
    assert literal.startswith(head) and literal.endswith(tail)
    assert red.text(code) == code, (lang, code)


@pytest.mark.parametrize("lang,line", KEPT)
def test_typed_code_is_kept(lang, line):
    assert Redactor(None, enabled=True).text(line) == line


@pytest.mark.parametrize("line,want", BRACKETS)
def test_a_quoted_value_before_a_closing_bracket_keeps_the_bracket(line, want):
    assert Redactor(None, enabled=True).text(line) == want


@pytest.mark.parametrize("line", STILL_REDACTED)
def test_a_credential_under_the_same_names_is_still_redacted(line):
    assert V not in Redactor(None, enabled=True).text(line)


def test_the_typed_benign_corpus_is_unchanged():
    red = Redactor(None, enabled=True)
    lines = [ln for text in corpus.BENIGN.values() for ln in text.split("\n") if ln.strip()]
    assert len(lines) >= 300
    changed = [(ln, red.text(ln)) for ln in lines if red.text(ln) != ln]
    assert not changed, changed
    for lang, text in corpus.BENIGN.items():
        assert red.text(text) == text, lang
