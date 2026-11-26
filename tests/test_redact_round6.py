"""Round 6 (verifier round 5: 44 of 97 fresh secrets were missed). Coverage is generalised by names rather than one
regex per sample: XML elements and key/value attributes, query parameters, headers, cookies and command flags are
judged by the secret-ness of their name; vendor tokens come from one prefix table; command-line passwords from one
command table. A second, fresh corpus of formats not in the verifier's list (TOML, YAML block scalars, HCL,
.properties, .npmrc/.pypirc/.netrc, Dockerfile, compose, CI files, connection strings, signed URLs, JWTs, keys in JSON)
was written before the rules and is checked here too, with benign look-alikes for every new rule.

Token-shaped test values are put together at run time (no literal token in the source)."""
from __future__ import annotations

import pytest

from devmem.redact import Redactor

A = "Xq8abcDEFghiJKLmnoPQRstuVWXyz0123456789"      # a generated-looking body
HEX = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"
PEM = "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"
B64_PEM = "LS0tLS1CRUdJTiBSU0EgUFJJVkFURSBLRVktLS0tLQpNSUlFb3dJQkFBS0NBUUVBdTFTVTFMZlZMUEhDb3pNeEgyTW80bGdPRWVQek5tMHRS"


def j(*parts: str) -> str:
    return "".join(parts)


# (name, text, the secret that must not survive)
CAUGHT = [
    # the verifier's round-5 misses
    ("xml element", "<server>\n  <id>nexus</id>\n  <password>N3xusDepl0y!</password>\n</server>", "N3xusDepl0y!"),
    ("web.config key/value", j('<add key="StripeApiKey" value="rk_prod_', A[:20], '" />'), A[:20]),
    ("nuget key/value", '<add key="ClearTextPassword" value="NuG3tFeedP4ss" />', "NuG3tFeedP4ss"),
    ("jdbc query", "jdbc:postgresql://db:5432/app?user=app&password=JdbcS3cret42&ssl=true", "JdbcS3cret42"),
    ("jdbc in properties", "spring.datasource.url=jdbc:mysql://db:3306/app?user=root&password=JdbcS3cret42", "JdbcS3cret42"),
    ("kubeconfig key data", j("users:\n- name: admin\n  user:\n    client-key-data: ", B64_PEM), B64_PEM[20:]),
    ("powershell securestring", '$pw = ConvertTo-SecureString "Tr0ub4dor&3xq" -AsPlainText -Force', "Tr0ub4dor&3xq"),
    ("powershell credential", "$c = New-Object PSCredential('admin', (ConvertTo-SecureString 'Wint3r2024!x' -AsPlainText))",
     "Wint3r2024!x"),
    ("cookie header", j("Cookie: session=eyJ1c2VyIjoiYm9iIn0.ZxYw.", A[:24]), A[:24]),
    ("set-cookie", j("Set-Cookie: auth_session=", HEX, "; HttpOnly; Secure"), HEX),
    ("curl -b", j('curl -b "sessionid=', HEX, '" https://app.internal/api'), HEX),
    ("redis-cli -a", "redis-cli -h cache.internal -a R3disPassw0rd99 ping", "R3disPassw0rd99"),
    ("sshpass -p", "sshpass -p 'Sup3rS3cret9' ssh deploy@10.0.0.5", "Sup3rS3cret9"),
    ("sqlcmd -P", "sqlcmd -S db.internal -U sa -P 'Sql$erv3rPwd1' -Q \"select 1\"", "Sql$erv3rPwd1"),
    ("az -p", j("az login --service-principal -u 0a1b2c3d-1111-2222-3333-444455556666 -p ", A, " --tenant t"), A),
    ("openssl pass:", "openssl pkcs12 -export -in c.crt -inkey k.key -passout pass:Exp0rtPassw0rd -out c.p12",
     "Exp0rtPassw0rd"),
    ("keytool -storepass", "keytool -list -keystore app.jks -storepass St0reP4ss2024", "St0reP4ss2024"),
    ("gh secret --body", "gh secret set DB_PASSWORD --body 'GhB0dyS3cret77'", "GhB0dyS3cret77"),
    ("aws configure set", j("aws configure set aws_secret_access_key wJalrXUtnFEMI/K7MDENG/", A[:20]), A[:20]),
    ("x-amz-security-token", j("https://b.s3.amazonaws.com/k?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Security-Token=",
                               "IQoJb3JpZ2luX2VjEHcaCXVzLWVhc3QtMSJHMEUCIQDf8k2&X-Amz-Signature=9f8e7d"),
     "IQoJb3JpZ2luX2VjEHcaCXVzLWVhc3QtMSJHMEUCIQDf8k2"),
    ("okta ssws", j("Authorization: SS", "WS 00QCjAl4MlV-WPXM", A[:16]), A[:16]),
    ("apim subscription key", j('curl -H "Ocp-Apim-Subscription-Key: ', HEX, '" https://westus.api.cognitive.microsoft.com/'),
     HEX),
    ("x-figma-token header", j('curl -H "X-Figma-Token: fig', "d_", A, '" https://api.figma.com/v1/me'), A),
    ("groq", j("my key is gs", "k_", A, "ABCDEFGHijk"), A),
    ("xai", j("use xa", "i-", A, A[:30]), A),
    ("langsmith", j("ls", "v2_pt_", HEX, "_0123456789"), HEX),
    ("notion", j("notion sec", "ret_", A, "ABCDE"), A),
    ("replicate", j("r", "8_", A[:37]), A[:37]),
    ("sentry", j("sntr", "ys_eyJpYXQiOjE2ODQ5NjAwMDAuMCwidXJsIjoiaHR0cHM6Ly9zZW50cnkuaW8ifQ_", A[:28]), A[:28]),
    ("grafana", j("gl", "sa_", A[:32], "_0a1b2c3d"), A[:32]),
    ("newrelic", j("NR", "AK-0A1B2C3D4E5F6G7H8I9J0K1L2M3"), "0A1B2C3D4E5F6G7H8I9J0K1L2M3"),
    ("postman", j("PM", "AK-64f1c2a3b4d5e6f7a8b9c0d1-0a1b2c3d4e5f60718293a4b5c6d7e8f901"), "0a1b2c3d4e5f60718293a4b5c6d7e8f901"),
    ("doppler", j("dp", ".st.prd.", A, "ABCD"), A),
    ("atlassian", j("ATA", "TT3xFfGF0", A, "-ABCDEFGH=1A2B3C4D"), A),
    ("google oauth access", j("access token ya", "29.a0AfH6SMBx", A), A),
    ("lowercase pem header", "-----begin rsa private key-----\n" + PEM + "\n-----end rsa private key-----", PEM),
    ("four-dash pem header", "----BEGIN RSA PRIVATE KEY----\n" + PEM, PEM),
    ("laravel app key with /", "APP_KEY=base64:Zm9vYmFyYmF6cXV4/MTIzNDU2Nzg5MGFiY2RlZg==", "Zm9vYmFyYmF6cXV4/MTIzNDU2Nzg5MGFiY2RlZg=="),
    ("signing salt", "SIGNING_SALT=BEsalt8f14e45fceea", "BEsalt8f14e45fceea"),
    ("quoted passphrase", 'passphrase = "correct horse battery staple"', "correct horse battery staple"),
    ("auth tuple", "requests.get(u, auth=('admin', 'TupleBE99x'))", "TupleBE99x"),
    ("putty ppk", "PuTTY-User-Key-File-3: ssh-rsa\nPrivate-Lines: 2\nQUJDREVGR0hJSktMTU5PUA==\nUVJTVFVWV1hZWg==\nPrivate-MAC: ab",
     "QUJDREVGR0hJSktMTU5PUA=="),
    # the fresh corpus (formats not in the verifier's list)
    ("toml inline table", 'db = { user = "app", password = "T0mlInl1neP4ss" }', "T0mlInl1neP4ss"),
    ("toml multi-line literal", "[auth]\napi_key = '''\nk8Qw2Zr5Tn7Lp3Xv9Bm4\n'''", "k8Qw2Zr5Tn7Lp3Xv9Bm4"),
    ("yaml block literal", "secrets:\n  db_password: |\n    Bl0ckSc4larPass\n  other: 1\n", "Bl0ckSc4larPass"),
    ("yaml block folded", "service:\n  api_token: >-\n    f0ldedT0kenValue8Rk3mWq\n", "f0ldedT0kenValue8Rk3mWq"),
    ("k8s env name/value", "env:\n  - name: DB_PASSWORD\n    value: K8sEnvV4lue77\n", "K8sEnvV4lue77"),
    ("json name/value", '[{"name": "DB_PASSWORD", "value": "AppS3ttingPw9", "slotSetting": false}]', "AppS3ttingPw9"),
    ("hcl variable default", 'variable "db_password" {\n  default = "HclD3faultPwd"\n}', "HclD3faultPwd"),
    ("properties colon", "jdbc.password : Pr0pColonPw", "Pr0pColonPw"),
    ("properties space", "mail.smtp.password Pr0pSpacePw9", "Pr0pSpacePw9"),
    ("npm config env auth", "NPM_CONFIG__AUTH=ZGVwbG95OkRlcGwweVAxNHNz", "ZGVwbG95OkRlcGwweVAxNHNz"),
    ("netrc default", "default login anonymous password N3trcDefaultPw", "N3trcDefaultPw"),
    ("rubygems", j(":rubygems_api_key: rubyg", "ems_", HEX, HEX[:16]), HEX),
    ("dockerfile env space", "ENV DB_PASSWORD D0ckerEnvSp4ce", "D0ckerEnvSp4ce"),
    ("odbc braces", "Driver={ODBC Driver 18};Server=tcp:db,1433;Uid=sa;PWD={0dbcBr4ce;Pw};", "0dbcBr4ce"),
    ("oracle sqlplus", "sqlplus scott/Or4cleTiger9@//db.internal:1521/ORCLPDB1", "Or4cleTiger9"),
    ("gcs signed url", j("https://storage.googleapis.com/b/o?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Signature=", HEX, "a1b2"),
     HEX),
    ("square", j("sq0", "atp-", A[:22]), A[:22]),
    ("pulumi", j("pu", "l-", HEX, "0a1b2c3d"), HEX),
    ("planetscale", j("pscale", "_tkn_", A), A),
    ("contentful", j("CFP", "AT-", A, "ABCDE"), A),
    ("perplexity", j("ppl", "x-", A, "ABCDEFGHijk"), A),
    ("tailscale", j("tskey", "-auth-kXq8abcDEF1CNTRL-", A[:30]), A[:30]),
    ("heroku", j("HRK", "U-AA", A, "_ABCDEFGHijklmnopqrstuv"), A),
    ("mapbox secret", j("s", "k.eyJ1IjoiYWNtZSIsImEiOiJja3h5ejEyMzQifQ.", A[:22]), A[:22]),
    ("bitbucket app password", j("app password AT", "BB", A[:32], "A2B3C4D"), A[:32]),
    ("asana", j("2/1204567890123456/1205678901234567:", HEX), HEX),
    ("cloudflare header", j('curl -H "X-Auth-Key: ', HEX, 'a0b1c" https://api.cloudflare.com'), HEX),
    ("datadog header", j('curl -H "DD-API-KEY: ', HEX, '" https://api.datadoghq.com'), HEX),
    ("php define", "define( 'DB_PASSWORD', 'WpD3fineP4ss' );", "WpD3fineP4ss"),
    ("wordpress salt", "define('AUTH_SALT', 'x8#Kp2!vQ9@zL5$wR3^mN7&tB1*cF6');", "x8#Kp2!vQ9@zL5$wR3^mN7&tB1*cF6"),
    ("python setdefault", 'os.environ.setdefault("OPENAI_API_KEY", "a7Fh2kLm9Qz3Xc8Vb1Nm4Pq")', "a7Fh2kLm9Qz3Xc8Vb1Nm4Pq"),
    ("java credentials", 'new UsernamePasswordCredentials("deploy", "J4vaCr3dPw")', "J4vaCr3dPw"),
    ("c# network credential", 'var c = new NetworkCredential("svc", "CsN3tCredPw");', "CsN3tCredPw"),
    ("smtp login", 'server.login("me@example.com", "SmtpL0ginPw9")', "SmtpL0ginPw9"),
    ("helm --set", "helm install app ./chart --set postgresql.auth.password=H3lmS3tPw99", "H3lmS3tPw99"),
    ("git config token", j("git config --global github.token ", HEX, "0a1b2c3d"), HEX),
    ("npm config set", j("npm config set //registry.npmjs.org/:_authToken np", "m_", A[:36]), A[:36]),
    ("wget http password", "wget --http-user=ci --http-password=WgetHttpPw9 https://files.internal/a.tgz", "WgetHttpPw9"),
    ("ldapsearch -w", 'ldapsearch -x -D "cn=admin,dc=example,dc=com" -w LdapB1ndPw -b dc=example,dc=com', "LdapB1ndPw"),
    ("smbclient -U user%pw", "smbclient //fs/share -U backup%SmbB4ckupPw", "SmbB4ckupPw"),
    ("zip -P", "zip -P Z1pArch1vePw backup.zip data.db", "Z1pArch1vePw"),
    ("markdown table", "| API_TOKEN | Tk9fake8Rk3mWq |", "Tk9fake8Rk3mWq"),
]

KEPT = [
    "<password></password>", "<password/>", "<password>${env.DB_PASSWORD}</password>", "<password>{{ db_password }}</password>",
    '<add key="Password" value="" />', '<add key="ApiBaseUrl" value="https://api.example.com" />',
    '<add key="Timeout" value="30" />', "<token>EOF</token>", "<secret>false</secret>",
    "<passwordPolicy>strong</passwordPolicy>", '<string name="password_hint">Enter your password</string>',
    "Set-Cookie: theme=dark; Path=/; Max-Age=3600; SameSite=Lax", "Cookie: lang=en-US; theme=dark",
    "curl -b cookies.txt -c cookies.txt https://example.com", "Content-Type: application/json",
    "X-Request-Id: 4f9c2b7e-1a8d-3f6c-5e0b-9a7d2c4e6f8a", "Idempotency-Key: 8e03978e-40d5-43e8-bc93-6894a57f9324",
    "https://example.com/search?q=password+reset&page=2",
    "https://api.example.com/items?pageToken=CAESBggDEJ4BGgYIARCeAQ&limit=50",
    "https://example.com/reset?token={token}", "https://example.com/?password_reset=true",
    "for(i=0;password=getpw();i++)", "password = None", "password: Optional[str] = None",
    "  --password TEXT  The password to use (prompted if omitted)", "usage: redis-cli [-a <password>] [-h host] [-p port]",
    "redis-cli -h cache.internal -p 6379 ping", "redis-cli -a $REDIS_PASSWORD ping", 'sshpass -p "$SSHPASS" ssh host',
    "sqlcmd -S db -U sa -P $SA_PASSWORD", "keytool -list -keystore app.jks -storepass:env STOREPASS",
    "openssl pkcs12 -in c.p12 -passin env:P12_PASSWORD -nodes", "openssl rsa -in key.pem -passin file:pw.txt",
    "gh secret set NPM_TOKEN --body-file token.txt", "aws configure set region us-east-1",
    'git config --global user.name "Jane Doe"', "npm config set registry https://registry.npmjs.org/",
    "docker login --password-stdin", "--token-file ./token.txt",
    'password: "Enter your password"', '"password": "Password must be at least 8 characters."',
    "password_label: Password", "secret: true", "token_type: bearer", "tokens: 1500", "salt_rounds: 12",
    "SALT_ROUNDS=12", "auth=(username, password)", 'login("user", "Log in")', 'translations("password", "Password")',
    'setAuthType("basic", "digest")', "ConvertTo-SecureString $plain -AsPlainText -Force",
    "kubectl create secret generic db --from-file=./password.txt", "helm install app ./chart --set image.tag=1.2.3",
    "Private-Lines: see the PuTTY documentation", "client-key-data: REDACTED",
    "certificate-authority-data: LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSUM1ekNDQWMrZ0F3SUJBZ0lCQURBTkJna3Fo",
    "- name: DB_PASSWORD\n  valueFrom:\n    secretKeyRef:\n      name: db\n      key: password\n",
    "| password | string | the user's password |", "| Name | Value |", "ENV NODE_ENV production",
    "spring.profiles.active production", "task_gsk_list = []", "secret_santa_assignments_for_the_2026_holiday_party = {}",
    "re_compile = re.compile", "r8_dataset_v2", "release-v1.0-0a1b2c3d",
    j("p", "k.eyJ1IjoiYWNtZSIsImEiOiJja3h5ejEyMzQifQ.publicMapboxToken"),
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
    '"integrity": "sha512-Ws1Sc7UZ3Vjz1jOw0H4kkiaT6kXaF6d2Y1FaPr5ytRlh0K6n2UQ0nkmZJ0vBf0nLjVJdS0BbEXAMPLE2z1q7Kg=="',
    "commit 1f0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e", "id: 3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "background: #1e293b; color: #F8FAFC;", "C:\\Users\\me\\projects\\app\\config\\settings.py",
    "secret_key_for_signing_the_session_cookies_in_production = settings.SECRET",
    "def test_password_reset_token_is_invalidated_after_use(self):",
]


@pytest.mark.parametrize("name,text,secret", CAUGHT, ids=[c[0] for c in CAUGHT])
def test_caught(name, text, secret):
    out = Redactor(None).text(text)
    assert secret not in out, out
    assert Redactor(None).text(out) == out          # a second pass changes nothing


@pytest.mark.parametrize("text", KEPT)
def test_kept(text):
    assert Redactor(None).text(text) == text
