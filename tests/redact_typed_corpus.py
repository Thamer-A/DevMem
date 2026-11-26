"""Typed code with secret-sounding names (verifier v8b, g3_typed / g5_regress / g6_params): declarations whose right
side is a type, a call or a reference (BENIGN: none of these lines may change) and typed declarations of a string
literal (LITERALS: lang, text, secret; each must be redacted). Written by hand per language; the values are made up and
built at run time."""
from __future__ import annotations

V = "Xk4" + "pQ9" + "Lm2zT8"
HEX = "f3a9c1e7" + "b2d4a6f8" + "c0e2b4d6" + "a8f0c2e4"

BENIGN: dict[str, str] = {
    "typescript": """\
export interface Credentials {
  password: string;
  apiKey: string,
  token: Token;
  refreshToken: string;
  passwordHash: string;
  token?: string;
  readonly secret: string;
  secretKey: Uint8Array;
  privateKey: CryptoKey | null;
  accessToken?: string | undefined;
  tokens: string[];
  password: Array<string>;
  apiToken: Promise<string>;
  secret: Record<string, string>;
  credentials: Map<string, Secret>;
}
export class AuthService {
  private apiKey: string;
  private readonly password: string;
  protected token?: Token;
  public secret!: string;
  static secretKey: KeyObject;
  private token: string = process.env.TOKEN ?? "";
  private apiKey: string = config.apiKey;
  private password: string = getPassword();
  constructor(private readonly password: string, private token: TokenStore) {}
  async login(password: string, token?: string): Promise<Session> {
    const secret: string = await this.vault.read(path);
    const apiKey: string = this.config.get("API_KEY");
    let token: string | null = null;
    const password: string = req.body.password;
    return this.session(password, token);
  }
}
type Secrets = { password: string; apiKey: string; token: Token };
function sign(secret: Buffer, token: string): string {
  return hmac(secret, token);
}
const secretKey: Buffer = Buffer.from(env.SECRET_KEY, "base64");
const token: string = jwt.sign(payload, secretKey);
let password: string;
declare const API_KEY: string;
""",
    "rust": """\
pub struct Config {
    password: String,
    token: Option<String>,
    pub password: String,
    api_key: &'a str,
    pub secret_key: Vec<u8>,
    private_key: Box<dyn Signer>,
    token: Arc<Mutex<Token>>,
    password_hash: [u8; 32],
    secret: SecretString,
}
impl Config {
    pub fn new(password: String, token: Option<String>) -> Self {
        let secret: String = env::var("SECRET").unwrap_or_default();
        let api_key: &str = &self.api_key;
        let token: Token = Token::new(&secret);
        let password: String = read_password()?;
        Self { password, token: Some(token.into()), ..Default::default() }
    }
    fn sign(&self, secret: &[u8], token: &str) -> Result<Vec<u8>, Error> {
        let key: Hmac<Sha256> = Hmac::new_from_slice(secret)?;
        Ok(key.finalize().into_bytes().to_vec())
    }
}
static API_KEY: Lazy<String> = Lazy::new(|| env::var("API_KEY").unwrap());
const SECRET_LEN: usize = 32;
let password: &str = args.password.as_str();
""",
    "python": """\
@dataclass
class Settings:
    password: str
    api_key: str | None = None
    token: Optional[str] = None
    secret: bytes = field(default_factory=bytes)
    secret_key: SecretStr = Field(..., env="SECRET_KEY")
    private_key: rsa.RSAPrivateKey | None = None
    passphrase: Optional[bytes] = None
    tokens: list[str] = field(default_factory=list)
    token: str = os.environ.get("TOKEN", "")
    password: str = os.environ["DB_PASSWORD"]
    api_key: str = settings.API_KEY


def connect(
    host: str,
    password: str,
    token: Optional[str] = None,
    api_key: str | None = None,
    secret: bytes,
    salt: bytes,
    private_key: rsa.RSAPrivateKey,
    password_file: Path | None = None,
) -> Connection:
    secret_key: bytes = derive(password, salt)
    token: str = make_token(secret_key)
    return Connection(host, password=password, token=token)


class Credentials(BaseModel):
    password: SecretStr
    api_key: SecretStr = Field(alias="apiKey")
    token: str = Field(default_factory=new_token)


API_KEY: Final = os.environ["API_KEY"]
SECRET_KEY: Final[str] = get_secret("django")
TOKEN_TYPES = (TokenType.A, TokenType.B)
PASSWORD_VALIDATORS = [Validator.MIN, Validator.COMMON]
conn = connect(
    password=password,
    token=args.token)
""",
    "go": """\
type Config struct {
\tPassword string `json:"password"`
\tToken    string
\tAPIKey   string `env:"API_KEY"`
\tSecret   []byte
\tPrivateKey *rsa.PrivateKey
}
func NewConfig(password string, token string) *Config {
\tvar secret string = os.Getenv("SECRET")
\tvar apiKey string = cfg.APIKey
\tpassword := os.Getenv("DB_PASSWORD")
\ttoken := strings.TrimSpace(raw)
\treturn &Config{Password: password, Token: token, Secret: []byte(secret)}
}
var password string
var token = flag.String("token", "", "API token")
""",
    "kotlin": """\
data class Credentials(
    val password: String,
    var token: String?,
    val apiKey: String = BuildConfig.API_KEY,
    private val secret: ByteArray,
    val secretKey: SecretKey? = null,
)
class AuthRepository(private val password: String, private val token: TokenProvider) {
    private val apiKey: String = System.getenv("API_KEY")
    val secret: String by lazy { vault.read("secret") }
    lateinit var token: String
    fun login(password: String, token: String?): Session = api.login(password, token)
}
const val TOKEN_HEADER: String = HEADER_NAME
""",
    "swift": """\
struct Credentials: Codable {
    let password: String
    var token: String?
    let apiKey: String
    var secret: Data
}
final class AuthClient {
    private let apiKey: String
    private var token: String? = nil
    let password: String = ProcessInfo.processInfo.environment["PASSWORD"] ?? ""
    func login(password: String, token: String?) async throws -> Session {
        let secret: Data = try keychain.read(account)
        return try await api.login(password: password, token: token)
    }
}
""",
    "scala": """\
case class Credentials(
  password: String,
  token: Option[String],
  apiKey: String = sys.env("API_KEY"),
  secret: Array[Byte]
)
class Auth(password: String, token: Token) {
  val secret: String = config.getString("secret")
  private val apiKey: String = sys.env.getOrElse("API_KEY", "")
  def login(password: String, token: Option[String]): Future[Session] = api.login(password, token)
}
""",
    "csharp": """\
public class Credentials
{
    public string Password { get; set; }
    public string? Token { get; init; }
    public string ApiKey { get; set; } = string.Empty;
    private readonly string _password;
    private readonly SecureString secret;
    public byte[] SecretKey { get; private set; }
    public Credentials(string password, string token)
    {
        string apiKey = Environment.GetEnvironmentVariable("API_KEY");
        var secret = configuration["Secret"];
        _password = password;
    }
    public async Task<string> GetTokenAsync(string password, CancellationToken token) => await api.Login(password, token);
}
""",
    "java": """\
public class Credentials {
    private String password;
    private final String token;
    private char[] secret;
    private static final String API_KEY = System.getenv("API_KEY");
    private SecretKey secretKey;
    public Credentials(String password, String token) {
        this.password = password;
        this.token = token;
        String apiKey = props.getProperty("api.key");
    }
    public String getPassword() { return password; }
    public void setToken(String token) { this.token = token; }
}
""",
    "schemas": """\
const schema = z.object({
  password: z.string().min(8),
  apiKey: z.string().uuid(),
  token: z.string().optional(),
  secret: z.string().regex(/^[a-f0-9]{64}$/),
});
const joi = Joi.object({
  password: Joi.string().min(8).required(),
  token: Joi.string().required(),
  apiKey: Joi.string().guid(),
});
const yupSchema = yup.object({
  password: yup.string().required("Required"),
  passwordConfirm: yup.string().oneOf([yup.ref("password")]),
});
Login.propTypes = {
  password: PropTypes.string,
  token: PropTypes.string.isRequired,
  apiKey: PropTypes.string,
  onSecret: PropTypes.func,
};
const userSchema = new mongoose.Schema({
  password: { type: String, required: true, select: false },
  token: String,
  apiKey: { type: String, index: true },
});
""",
    "graphql": """\
type User {
  id: ID!
  password: String!
  token: String
  apiKey: String @deprecated(reason: "use tokens")
  tokens: [Token!]!
}
input LoginInput {
  password: String!
  token: String
  secret: Secret
}
type Mutation {
  login(password: String!, token: String): Session!
  rotateApiKey(token: ID!): ApiKey
}
export type Credentials = {
  password: Scalars['String'];
  token?: Maybe<Scalars['String']>;
  apiKey: Scalars['String'];
};
""",
    "protobuf": """\
message Credentials {
  string password = 1;
  optional string token = 2;
  bytes secret = 3;
  string api_key = 4 [json_name = "apiKey"];
  repeated string tokens = 5;
  map<string, string> secrets = 6;
}
service Auth {
  rpc Login(LoginRequest) returns (Session);
}
""",
    "sql": """\
CREATE TABLE users (
    id BIGSERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    password VARCHAR(255) NOT NULL,
    password_hash TEXT NOT NULL,
    api_key CHAR(64) UNIQUE,
    token UUID DEFAULT gen_random_uuid(),
    secret BYTEA,
    reset_token VARCHAR(128) NULL,
    token_expires_at TIMESTAMPTZ
);
ALTER TABLE users ADD COLUMN secret_key VARCHAR(64) NOT NULL DEFAULT '';
CREATE INDEX idx_users_token ON users (token);
SELECT id, password_hash FROM users WHERE email = $1;
UPDATE users SET password = crypt($2, gen_salt('bf')) WHERE id = $1;
""",
    "more": """\
class Creds(TypedDict):
    password: str
    token: NotRequired[str]
    api_key: Required[str]
def handler(event: dict[str, Any], secret: Secret | None = None) -> None: ...
async def refresh(token: Token, *, secret_key: bytes = DEFAULT_KEY) -> Token: ...
password_field: forms.CharField = forms.CharField(widget=forms.PasswordInput)
token_store: dict[str, Token] = {}
class User {
    private string $password;
    private ?string $token = null;
    public function __construct(private readonly string $apiKey) {}
}
class Account {
  final String password;
  String? token;
  late final String apiKey;
}
struct Credentials {
    std::string password;
    std::optional<std::string> token;
    std::vector<uint8_t> secret;
};
sig { params(password: String, token: T.nilable(String)).void }
@type t :: %{password: String.t(), token: String.t() | nil}
components:
  schemas:
    Login:
      type: object
      properties:
        password:
          type: string
          format: password
        token:
          type: string
          writeOnly: true
{"password": {"type": "string", "minLength": 8}, "token": {"type": ["string", "null"]}}
export type LoginFn = (password: string, token?: string) => Promise<void>;
export const useAuth = (): { token: string | null; login: (password: string) => void } => ctx;
const [password, setPassword] = useState<string>("");
const [token, setToken] = useState<string | null>(null);
function hash(password: string, salt: Uint8Array): Promise<ArrayBuffer>;
password: Password = Depends(get_password),
token: Annotated[str, Depends(oauth2_scheme)],
secret: SecretStr = Body(...),
api_key: str = Security(api_key_header),
let secret_key: [u8; 32] = rand::random();
let token: Option<&str> = headers.get("token").map(|v| v.to_str().unwrap());
fn verify(password: &str, hash: &PasswordHash<'_>) -> bool {
pub secret: Secret<String>,
var password: String by Delegates.notNull()
val token: StateFlow<String?> = _token.asStateFlow()
@Value("${app.secret}") private String secret;
@Column(name = "password", nullable = false) private String password;
public required string Password { get; init; }
password: string = '';
token: string | null = null;
""",
}

# (language, text, secret): a typed declaration of a string literal, in every string form the language has
LITERALS: list[tuple[str, str, str]] = [
    ("python", f'password: str = "{V}"', V),
    ("python", f'    db_password: str = "{V}"', V),
    ("python", f'API_KEY: Final = "{HEX}"', HEX),
    ("python", f'SECRET_KEY: Final[str] = "{V}"', V),
    ("python", f'    token: Optional[str] = "{V}"', V),
    ("python", f'    api_key: str | None = "{HEX}"', HEX),
    ("python", f"password = r'{V}'", V),
    ("python", f'secret = b"{V}"', V),
    ("python", f'password = rb"{V}"', V),
    ("python", f'password = """{V}"""', V),
    ("typescript", f'const password: string = "{V}";', V),
    ("typescript", f'  private apiKey: string = "{HEX}";', HEX),
    ("typescript", f'  readonly dbPassword: string = "{V}";', V),
    ("typescript", f"let secret: string = '{V}';", V),
    ("typescript", f'const token: string | undefined = "{V}";', V),
    ("typescript", f"const password = `{V}`;", V),
    ("typescript", f"  password: `{V}`,", V),
    ("rust", f'const PASSWORD: &str = "{V}";', V),
    ("rust", f'static API_KEY: &str = "{HEX}";', HEX),
    ("rust", f"static API_KEY: &'static str = \"{HEX}\";", HEX),
    ("rust", f'let password: &str = "{V}";', V),
    ("rust", f'let password = r"{V}";', V),
    ("rust", f'let password = r#"{V}"#;', V),
    ("kotlin", f'val dbPassword: String = "{V}"', V),
    ("kotlin", f'const val API_SECRET: String = "{V}"', V),
    ("kotlin", f'val password = """{V}"""', V),
    ("swift", f'let password: String = "{V}"', V),
    ("swift", f'var apiKey: String? = "{HEX}"', HEX),
    ("scala", f'val password: String = "{V}"', V),
    ("go", f'var password string = "{V}"', V),
    ("go", f"password := `{V}`", V),
    ("go", f"var apiKey = `{HEX}`", HEX),
    ("csharp", f'var password = @"{V}";', V),
    ("csharp", f'string password = @"{V}";', V),
    ("csharp", f'var password = """{V}""";', V),
    ("java", f'private static final String PASSWORD = "{V}";', V),
    ("yaml", f"password: &db_pw {V}", V),
    ("yaml", f"password: !!str {V}", V),
    ("yaml", f'password: !!str "{V}"', V),
    ("yaml", f"  secret_key: &key {V}", V),
]
