"""Corpus 7 (round 7, written before the round-7 rules and not tuned to them): 71 secrets in formats that neither the
verifier's corpus 6 nor the earlier rounds list (S: name, text, secret) and 44 benign look-alikes (B: name, text). All
values are made up, built at run time from a seeded RNG (no literal token in the source). tests/test_redact_corpus7.py
records which of them are caught."""
import base64
import random

R = random.Random(20261116)
UP = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LO = "abcdefghijklmnopqrstuvwxyz"
DG = "0123456789"
AN = UP + LO + DG


def g(alpha, n):
    return "".join(R.choice(alpha) for _ in range(n))


def pw():
    return g(UP, 2) + g(LO, 4) + g(DG, 3) + R.choice("!#%*@") + g(AN, 3)


S = []
B = []


def add(name, text, secret):
    assert secret in text, name
    S.append((name, text, secret))


p = pw(); add("kafka_jaas", f'sasl.jaas.config=org.apache.kafka.common.security.plain.PlainLoginModule required username="svc" password="{p}";', p)
p = pw(); add("redis_acl_setuser", f"ACL SETUSER alice on >{p} ~cache:* +get +set", p)
p = pw(); add("mongo_createuser", f'db.createUser({{user: "app", pwd: "{p}", roles: ["readWrite"]}})', p)
p = pw(); add("rabbitmqctl_add_user", f"rabbitmqctl add_user deploy {p}", p)
p = pw(); add("mysqladmin_password", f"mysqladmin -u root password {p}", p)
p = pw(); add("pgbouncer_userlist", f'"app" "{p}"\n"stats" "md5abc"', p)
p = pw(); add("chpasswd_pipe", f"echo 'root:{p}' | chpasswd", p)
p = pw(); add("gpg_passphrase_flag", f"gpg --batch --yes --passphrase '{p}' -d backup.tar.gpg", p)
p = pw(); add("7z_password", f"7z a -p{p} -mhe=on backup.7z ./data", p)
p = pw(); add("ssh_keygen_N", f"ssh-keygen -t ed25519 -N '{p}' -f ~/.ssh/deploy", p)
p = pw(); add("lftp_user_comma", f"lftp -u uploader,{p} ftp.example.invalid", p)
p = pw(); add("gradle_groovy_storepassword", f"signingConfigs {{\n  release {{\n    storePassword '{p}'\n    keyAlias 'up'\n  }}\n}}", p)
p = pw(); add("maven_D_gpg", f"mvn deploy -Dgpg.passphrase={p} -DskipTests", p)
p = pw(); add("ruby_hash_rocket", f"Mail.defaults do\n  delivery_method :smtp, :user_name => 'me', :password => '{p}'\nend", p)
p = pw(); add("php_pdo_positional", f"$db = new PDO('mysql:host=db;dbname=app', 'root', '{p}');", p)
p = pw(); add("pymysql_passwd_kw", f"conn = pymysql.connect(host='db', user='root', passwd='{p}', db='app')", p)
p = pw(); add("go_mysql_dsn", f'db, err := sql.Open("mysql", "app:{p}@tcp(db:3306)/shop?parseTime=true")', p)
p = pw(); add("kibana_yaml_quoted_dotted", f'elasticsearch.username: "kibana_system"\nelasticsearch.password: "{p}"', p)
p = pw(); add("grafana_ini", f"[security]\nadmin_user = admin\nadmin_password = {p}\n", p)
p = pw(); add("kubectl_from_literal", f"kubectl create secret generic db --from-literal=username=app --from-literal=password={p}", p)
p = pw(); add("docker_secret_create", f"printf '{p}' | docker secret create db_pw -", p)
p = pw(); add("compose_mysql_root", f"services:\n  db:\n    image: mysql:8\n    environment:\n      MYSQL_ROOT_PASSWORD: {p}\n", p)
p = g(AN, 20); add("gitlab_ci_variables", f"variables:\n  DEPLOY_TOKEN: {p}\n  STAGE: prod\n", p)
p = g(AN, 32); add("heroku_config_set", f"heroku config:set PAYMENT_API_KEY={p} -a shop", p)
p = pw(); add("fly_secrets_set", f"fly secrets set DATABASE_PASSWORD={p}", p)
p = pw(); add("aws_ssm_put", f"aws ssm put-parameter --name /prod/db/password --value '{p}' --type SecureString", p)
p = pw(); add("aws_secretsmanager", f"aws secretsmanager create-secret --name prod/db --secret-string '{p}'", p)
p = pw(); add("az_keyvault_set", f"az keyvault secret set --vault-name kv-prod --name db-password --value '{p}'", p)
p = pw(); add("terraform_cli_var", f"terraform apply -var 'db_password={p}' -auto-approve", p)
p = pw(); add("pulumi_config_secret", f"pulumi config set --secret dbPassword {p}", p)
p = pw(); add("vault_kv_put", f"vault kv put secret/prod/db username=app password={p}", p)
p = pw(); add("consul_kv_put", f"consul kv put service/db/password {p}", p)
p = pw(); add("etcdctl_user", f"etcdctl --user root:{p} get /config", p)
p = pw(); add("snmpwalk_v3", f"snmpwalk -v3 -l authPriv -u mon -a SHA -A {p} -x AES -X {p}x 10.0.0.1", p)
p = pw(); add("mount_cifs", f"mount -t cifs //nas/share /mnt/share -o username=backup,password={p},vers=3.0", p)
p = pw(); add("fstab_cifs", f"//nas/media  /mnt/media  cifs  username=media,password={p},uid=1000  0  0", p)
p = base64.b64encode(R.randbytes(32)).decode(); add("wireguard_privatekey", f"[Interface]\nAddress = 10.8.0.2/24\nPrivateKey = {p}\nDNS = 1.1.1.1\n", p)
p = pw(); add("ipsec_secrets_psk", f': PSK "{p}"', p)
p = pw(); add("radius_clients_secret", f"client office {{\n    ipaddr = 10.0.0.0/24\n    secret = {p}\n}}", p)
p = pw(); add("mosquitto_pub_P", f"mosquitto_pub -h broker -u sensor -P '{p}' -t temp -m 21", p)
p = pw(); add("postfix_sasl_passwd", f"[smtp.example.invalid]:587 mailer@example.invalid:{p}", p)
p = pw(); add("msmtprc_password", f"account default\nhost smtp.example.invalid\nuser me\npassword {p}\n", p)
p = pw(); add("git_credential_fill", f"protocol=https\nhost=git.example.invalid\nusername=ci\npassword={p}\n", p)
p = g(LO + DG, 2) + "-" + g(AN, 30); add("conda_token_url", f"channels:\n  - https://conda.example.invalid/t/{p}/private\n", p)
p = pw(); add("composer_auth_json", '{"http-basic": {"repo.example.invalid": {"username": "ci", "password": "' + p + '"}}}', p)
p = "cio" + g(AN, 32); add("cargo_credentials", f'[registry]\ntoken = "{p}"\n', p)
p = pw(); add("nuget_sources_add", f"nuget sources add -Name feed -Source https://pkgs.example.invalid/v3 -Username ci -Password {p}", p)
p = pw(); add("pwsh_env_assign", f'$env:DB_PASSWORD = "{p}"', p)
p = pw(); add("net_use", f"net use \\\\fileserver\\share {p} /user:CORP\\svc_backup", p)
p = pw(); add("cmdkey_pass", f"cmdkey /add:fileserver /user:CORP\\svc /pass:{p}", p)
p = pw(); add("schtasks_rp", f'schtasks /create /tn nightly /tr backup.cmd /sc daily /ru CORP\\svc /rp {p}', p)
p = g(LO + DG, 48); add("jupyter_token", f"c.NotebookApp.token = '{p}'", p)
p = pw(); add("airflow_conn_env", f"export AIRFLOW_CONN_WAREHOUSE='postgres://etl:{p}@warehouse:5432/dw'", p)
p = g("0123456789abcdef", 32); add("algolia_positional", f"const client = algoliasearch('ABCD1234EF', '{p}');", p)
p = g(AN, 32); add("keycloak_client_secret", '{"clientId": "web", "secret": "' + p + '", "publicClient": false}', p)
p = pw(); add("ldap_bind_credentials", f"ldap.url=ldaps://ldap.internal\nldap.bind.dn=cn=svc\nldap.bind.credentials={p}\n", p)
p = pw(); add("cqlsh_p", f"cqlsh -u cassandra -p '{p}' 10.0.0.9", p)
p = pw(); add("cypher_shell_p", f"cypher-shell -a bolt://db:7687 -u neo4j -p {p}", p)
p = pw(); add("hdbsql_p", f"hdbsql -n hana:30015 -u SYSTEM -p {p} \"select 1 from dummy\"", p)
p = base64.urlsafe_b64encode(R.randbytes(64)).decode().rstrip("="); add("jwk_private_d", '{"kty": "RSA", "e": "AQAB", "n": "' + g(AN, 40) + '", "d": "' + p + '"}', p)
p = "_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-items.|_" + g("0123456789ABCDEF", 80)
add("roblox_cookie", f".ROBLOSECURITY={p}", p[-80:])
p = pw(); add("steamcmd_login", f"steamcmd +login builder {p} +app_update 740 +quit", p)
p = pw(); add("influx_cli_password", f"influx -host db -username admin -password '{p}' -execute 'SHOW DATABASES'", p)
p = pw(); add("openvpn_askpass_cmd", f"openvpn --config corp.ovpn --askpass <(echo '{p}')", p)
p = pw(); add("x11vnc_passwd", f"x11vnc -display :0 -passwd {p} -forever", p)
p = pw(); add("unrar_p", f"unrar x -p{p} archive.rar", p)
p = pw(); add("sonar_password_props", f"sonar.host.url=https://sonar.internal\nsonar.password={p}\n", p)
p = pw(); add("spring_cli_flag", f"java -jar app.jar --spring.datasource.password={p}", p)
p = pw(); add("prose_french", f"Le mot de passe du serveur est {p} jusqu'à lundi", p)
p = pw(); add("prose_spanish", f"la contraseña de la base de datos es {p}", p)
p = pw(); add("prose_token_colon", f"Here is the deploy token: {p} (expires tomorrow)", p)

# benign look-alikes
B += [
    ("acl_list", "ACL LIST\nACL WHOAMI"),
    ("createuser_doc", 'db.createUser({user: "app", pwd: passwordPrompt(), roles: []})'),
    ("rabbit_list", "rabbitmqctl list_users"),
    ("gpg_doc", "gpg --batch --passphrase-fd 0 -d file.gpg"),
    ("7z_list", "7z l archive.7z -slt"),
    ("keygen_empty", "ssh-keygen -t ed25519 -N '' -f key"),
    ("gradle_env_ref", "storePassword System.getenv('STORE_PASSWORD')"),
    ("maven_D_ordinary", "mvn test -Dtest=UserServiceTest -DfailIfNoTests=false"),
    ("pdo_env", "$db = new PDO($dsn, getenv('DB_USER'), getenv('DB_PASS'));"),
    ("kibana_ref", 'elasticsearch.password: "${ELASTIC_PASSWORD}"'),
    ("grafana_empty", "[security]\nadmin_password =\n"),
    ("kubectl_get", "kubectl get secret db -o jsonpath='{.data.password}'"),
    ("compose_ref", "environment:\n  MYSQL_ROOT_PASSWORD_FILE: /run/secrets/db_root\n"),
    ("heroku_config", "heroku config:get DATABASE_URL -a shop"),
    ("ssm_get", "aws ssm get-parameter --name /prod/db/password --with-decryption"),
    ("tf_var_file", "terraform apply -var-file=prod.tfvars"),
    ("vault_kv_get", "vault kv get -field=password secret/prod/db"),
    ("mount_creds_file", "mount -t cifs //nas/share /mnt -o credentials=/root/.smbcred,vers=3.0"),
    ("wg_publickey", "[Peer]\nPublicKey = " + base64.b64encode(R.randbytes(32)).decode() + "\nAllowedIPs = 0.0.0.0/0\n"),
    ("git_credential_doc", "protocol=https\nhost=github.com\nusername=me\n"),
    ("cargo_doc", "cargo login --registry internal"),
    ("pwsh_env_ref", "$env:DB_PASSWORD = $null"),
    ("net_use_list", "net use"),
    ("jupyter_empty", "c.NotebookApp.token = ''"),
    ("sonar_url", "sonar.host.url=https://sonar.internal\nsonar.projectKey=shop\n"),
    ("diff_code", "@@ -1,3 +1,3 @@\n-    timeout = 30\n+    timeout = 60\n     retries = 3\n"),
    ("grep_timeout", "src/config.py:12: timeout: 30\nsrc/config.py:13: retries: 5\n"),
    ("diff_version", "+ version: 1.2.3\n- version: 1.2.2\n"),
    ("yaml_placeholders", "db:\n  password: ''\n  token: <set me>\n  api_key: ${API_KEY}\n"),
    ("comments", "# password: see the vault entry\n// token: rotated weekly\n"),
    ("numbered_code", "  1. def login(user, password):\n  2.     return check(user, password)\n"),
    ("cat_n_code", "     1\timport os\n     2\tPASSWORD_MIN = 12\n     3\tdef check(password): ...\n"),
    ("read_tool_code", "     1→def tokenize(text):\n     2→    return text.split()\n"),
    ("md_quote", "> The password must be at least 12 characters long.\n> Tokens expire after 1 hour.\n"),
    ("prose_password_rules", "the password is required and must contain digits"),
    ("prose_token_stored", "the token is stored in the keychain until logout"),
    ("prose_password_changed", "Your password was changed on 2026-11-02 from 10.0.0.4"),
    ("sql_grant", "GRANT SELECT ON ALL TABLES IN SCHEMA public TO reporting;"),
    ("sql_password_null", "ALTER ROLE app PASSWORD NULL;"),
    ("setter_label", 'field.setPasswordLabel("Password"); field.setPlaceholder("Enter your password");'),
    ("helm_set_ordinary", "helm upgrade api ./chart --set image.tag=v1.4.2 --set replicas=3"),
    ("nginx_header", 'proxy_set_header X-Request-Id $request_id;\nproxy_set_header Host $host;'),
    ("echo_pipe_ordinary", "echo 'hello world' | tr a-z A-Z"),
    ("pgpass_like_log", "2026-11-15 10:22:31 db:5432 connection ok"),
]
