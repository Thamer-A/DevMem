"""Round 7 (verifier round 6: 14 of 134 secrets of its corpus 6 missed as plain text, 1 false positive of 69 benign).
The rules are generalised by name or by command table: a quoted value after a secret name and a space (SQL `PASSWORD
'...'`, `IDENTIFIED BY`, a Groovy `storePassword '...'`), setters (`setPassword("...")`, `.withSecret("...")`), options
whose name ends in a secret word (`-Password X`, `--spring.datasource.password=X`), `name=value` options (`--set
auth.postgresPassword=`, `-Dgpg.passphrase=`, `-o user=u,password=X`), header-setting directives (nginx, Apache,
HAProxy), a password piped into a command reading stdin, the word for password in other languages, prose ("the password
is X"), session cookies in name/value pairs, .pgpass and postfix lines, Teams webhooks and Roblox's session cookie; plus
more commands in the command table. Corpus 7 (tests/redact_corpus7.py) was written before these rules; which of its
secrets stay missed is pinned below and listed as known gaps in the redact.py docstring.

The secrets are made up and put together at run time."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import redact_corpus6  # noqa: E402
import redact_corpus7  # noqa: E402
import redact_corpus8  # noqa: E402

from devmem import redact as R  # noqa: E402
from devmem.redact import Redactor  # noqa: E402

C6_MISSED = {"graphql_variables"}      # `"p": "..."`: a value under a one-letter name
C7_MISSED = {"pgbouncer_userlist", "php_pdo_positional", "conda_token_url", "algolia_positional", "jwk_private_d",
             "openvpn_askpass_cmd"}
# corpus 8 was written after the rules and run once: these 14 of 69 were missed then and are left as known gaps
C8_MISSED = {"rust_env_macro_fallback", "makefile_var", "cloud_init_chpasswd", "kickstart_rootpw", "preseed_passwd",
             "unattend_xml", "openldap_slapd_rootpw", "keepassxc_cli", "openssl_enc_k", "pdftk_user_pw", "qpdf_encrypt",
             "rsync_daemon_secrets", "japanese_prose", "chat_message_password"}


@pytest.mark.parametrize("corpus,known", [(redact_corpus6, C6_MISSED), (redact_corpus7, C7_MISSED),
                                          (redact_corpus8, C8_MISSED)])
def test_corpus_misses_are_the_known_ones(corpus, known):
    red = Redactor(None)
    missed = {n for n, t, s in corpus.S if s in red.text(t)}
    assert missed == known, (sorted(missed - known), sorted(known - missed))


@pytest.mark.parametrize("corpus", [redact_corpus6, redact_corpus7, redact_corpus8])
def test_corpus_benign_texts_are_kept(corpus):
    red = Redactor(None)
    changed = [(n, red.text(t)) for n, t in corpus.B if red.text(t) != t]
    assert not changed, changed


def j(*parts: str) -> str:
    return "".join(parts)


PW = j("Zq", "rt", "yu48", "21!xK")
CAUGHT = [
    f"CREATE ROLE app LOGIN PASSWORD '{PW}';",
    f"CREATE LOGIN app WITH PASSWORD = '{PW}';",
    f"SET PASSWORD FOR 'app'@'%' = PASSWORD('{PW}');",
    f"ALTER USER 'app'@'%' IDENTIFIED WITH mysql_native_password BY '{PW}';",
    f"CREATE USER app IDENTIFIED BY {PW};",
    f"keyPassword '{PW}'",
    f'cfg.setApiKey("{PW}");',
    f'Client.builder().withSecret("{PW}").build()',
    f"helm install db bitnami/redis --set auth.password={PW}",
    f"java -Djavax.net.ssl.keyStorePassword={PW} -jar app.jar",
    f"terraform plan -var 'db_password={PW}'",
    f"mount -o username=me,password={PW},uid=1000 //h/s /mnt",
    f'add_header X-Auth-Token "{PW}";',
    f"RequestHeader set X-Api-Key {PW}",
    f"fastcgi_param DB_PASSWORD {PW};",
    f"echo '{PW}' | docker login -u me --password-stdin registry.example.invalid",
    f"printf '%s' '{PW}' | passwd --stdin deploy",
    f"Passwort: {PW}",
    f"wachtwoord = {PW}",
    f"пароль: {PW}",
    f"the root password for the staging db is {PW}",
    f"Here is the API key: {PW} (rotate it monthly)",
    f"htpasswd -c -b /etc/nginx/.htpasswd admin {PW}",
    f"htpasswd -nb admin {PW}",
    f"New-LocalUser -Name svc -Password {PW}",
    f"cmdkey /add:srv /user:me /pass:{PW}",
    f"etcdctl --user root:{PW} get /",
    f"cqlsh -u cassandra -p {PW} db",
    f"mosquitto_sub -u me -P {PW} -t x",
    f"ssh-keygen -p -P old -N {PW} -f key",
    f"rabbitmqctl change_password deploy {PW}",
    f"az keyvault secret set --vault-name kv --name db-password --value {PW}",
    f"pulumi config set --secret apiToken {PW}",
    f"requirepass {PW}",
    f"[smtp.example.invalid]:587 me@example.invalid:{PW}",
    f"ACL SETUSER svc on >{PW} ~* +@read",
    f'dsn := "svc:{PW}@tcp(db:3306)/app"',
    f"db.internal:5432:*:app:{PW}",
    f'network={{\n  ssid="x"\n  psk="{PW}"\n}}',
    '{"cookies": [{"name": "JSESSIONID", "value": "' + "0A1B2C3D4E5F" * 3 + '"}]}',
    "https://x.webhook.office.com/webhookb2/" + "0a1b2c3d-" * 5 + "/IncomingWebhook/" + "ab12" * 8,
]


@pytest.mark.parametrize("text", CAUGHT)
def test_round7_shapes_are_caught(text):
    red = Redactor(None)
    out = red.text(text)
    secret = PW if PW in text else ("0A1B2C3D4E5F" if "JSESSIONID" in text else "ab12ab12")
    assert secret not in out, out
    assert red.text(out) == out


KEPT = [
    "ALTER ROLE app PASSWORD NULL;",
    "CREATE USER app IDENTIFIED BY VALUES 'S:0A1B2C';",
    "SELECT password_hash FROM users WHERE id = 1;",
    'field.setPasswordLabel("Password"); field.setPlaceholder("Enter your password");',
    "user.setPassword(passwordField.getText());",
    'auth.getPassword("prompt-1")',
    "helm upgrade api ./chart --set image.tag=v1.4.2 --set replicas=3",
    "java -Dspring.profiles.active=prod -Xmx512m -jar app.jar",
    "mount -t cifs //nas/share /mnt -o credentials=/root/.smbcred,vers=3.0",
    "proxy_set_header Host $host;\nproxy_set_header X-Real-IP $remote_addr;",
    "echo 'hello world' | tr a-z A-Z",
    "echo $PASSWORD | docker login -u me --password-stdin",
    "the password is required and must contain digits",
    "the token is stored in the keychain until logout",
    "Your password was changed on 2026-11-02 from 10.0.0.4",
    "the secret is v2-compatible since release 1.4.2",
    "def login(token: str, password: Optional[str] = None):",
    "docker run -u 1000:1000 -v $PWD:/src img",
    "msbuild /p:Configuration=Release /t:Build",
    "docker login --password-stdin",
    "gpg --passphrase-fd 0 --batch -d f.gpg",
    "--max-tokens 4096 --secret-name prod-db --token-file ~/.tok",
    "user www-data;\nworker_processes auto;",
    "password    required",
    "[database]\nhost = db\n",
    "src/config.py:12: timeout: 30\nsrc/config.py:13: retries: 5",
    "+ version: 1.2.3\n- version: 1.2.2",
    "  1. def login(user, password):\n  2.     return check(user, password)",
    "     1\timport os\n     2\tPASSWORD_MIN = 12",
    "# password: see the vault entry\n// token: rotated weekly",
    "db:\n  password: ''\n  token: <set me>\n  api_key: ${API_KEY}\n",
    "SECRET_KEY=your-secret-key-here",
    "2026-11-15 10:22:31 db:5432 connection ok",
    'consul kv get service/db/password',
    "a.b.c.d:5432:x",
]


@pytest.mark.parametrize("text", KEPT)
def test_round7_look_alikes_are_kept(text):
    assert Redactor(None).text(text) == text


def test_every_needs_entry_names_one_rule():
    assert not R._NEED_MISSES, R._NEED_MISSES


def test_a_rule_without_its_words_is_skipped_and_with_them_runs():
    red = Redactor(None)
    assert red.text(f"curl -u me:{PW} https://h") != f"curl -u me:{PW} https://h"
    assert red.text(f"CURL -u me:{PW} https://h") != f"CURL -u me:{PW} https://h"       # words match in any case
