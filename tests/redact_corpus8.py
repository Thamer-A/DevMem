"""Corpus 8 (round 7): 69 secrets in formats that corpus 6, corpus 7 and the earlier rounds do not list (S: name, text,
secret) and 30 benign look-alikes (B: name, text), written after the round-7 rules and run once without tuning the rules
to it: tests/test_redact_round7.py pins which are missed. All values are made up, built at run time from a seeded RNG."""
import base64
import random

R = random.Random(20261117)
UP = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LO = "abcdefghijklmnopqrstuvwxyz"
DG = "0123456789"
AN = UP + LO + DG
HX = "0123456789abcdef"


def g(a, n):
    return "".join(R.choice(a) for _ in range(n))


def pw():
    return g(UP, 2) + g(LO, 4) + g(DG, 3) + R.choice("!#%*@^") + g(AN, 3)


S, B = [], []


def add(n, t, s):
    assert s in t, n
    S.append((n, t, s))


p = pw(); add("hasura_admin_secret_header", f"curl -H 'x-hasura-admin-secret: {p}' https://gql.example.invalid/v1/graphql", p)
p = g(AN, 40); add("supabase_cli_db_password", f"supabase db push --db-url postgresql://postgres:{p}@db.example.invalid:5432/postgres", p)
p = pw(); add("prisma_env_url", f'DATABASE_URL="mysql://root:{p}@localhost:3306/shop"', p)
p = pw(); add("sequelize_config_json", '{"production": {"username": "app", "password": "' + p + '", "dialect": "postgres"}}', p)
p = pw(); add("knexfile_js", f"module.exports = {{ client: 'pg', connection: {{ host: 'db', user: 'app', password: '{p}' }} }};", p)
p = pw(); add("typeorm_ts", f'TypeOrmModule.forRoot({{ type: "mysql", username: "root", password: "{p}" }})', p)
p = pw(); add("django_databases", f"DATABASES = {{'default': {{'ENGINE': 'django.db.backends.postgresql', 'PASSWORD': '{p}'}}}}", p)
p = pw(); add("flask_config_py", f"SQLALCHEMY_DATABASE_URI = 'postgresql://app:{p}@db/app'", p)
p = g(AN, 50); add("rails_credentials_yml", f"aws:\n  access_key_id: AKIA{g(UP+DG,16)}\n  secret_access_key: {p}\n", p)
p = pw(); add("phoenix_config_exs", f'config :app, App.Repo,\n  username: "postgres",\n  password: "{p}",\n  hostname: "db"', p)
p = pw(); add("laravel_database_php", f"'mysql' => [\n    'username' => 'forge',\n    'password' => '{p}',\n],", p)
p = pw(); add("spring_boot_properties_colon_space", f"spring.mail.password : {p}", p)
p = pw(); add("quarkus_props", f"quarkus.datasource.password={p}", p)
p = pw(); add("micronaut_yaml_env", f"datasources:\n  default:\n    password: '{p}'\n", p)
p = pw(); add("dotnet_user_secrets_json", '{"ConnectionStrings:Default": "Server=db;Database=app;User Id=sa;Password=' + p + ';"}', p)
p = pw(); add("go_viper_yaml", f"db:\n  dsn: \"postgres://svc:{p}@db:5432/app?sslmode=disable\"\n", p)
p = pw(); add("rust_env_macro_fallback", f'let pw = std::env::var("DB_PASSWORD").unwrap_or("{p}".to_string());', p)
p = pw(); add("kotlin_gradle_kts", f'signingConfigs {{ create("release") {{ storePassword = "{p}" }} }}', p)
p = pw(); add("swift_keychain_literal", f'let password = "{p}"\nKeychain.save(password, for: "api")', p)
p = pw(); add("csharp_appsettings_secret", '{"Jwt": {"Key": "' + g(AN, 40) + '", "Issuer": "app"}, "Smtp": {"Password": "' + p + '"}}', p)
p = pw(); add("powershell_splat", f"$cred = @{{ UserName = 'svc'; Password = '{p}' }}", p)
p = pw(); add("bash_heredoc_mysql", f"mysql -u root <<EOF\nALTER USER 'root'@'localhost' IDENTIFIED BY '{p}';\nEOF", p)
p = pw(); add("ansible_vars_inline", f"ansible-playbook site.yml -e \"db_password={p} env=prod\"", p)
p = pw(); add("saltstack_pillar", f"mysql:\n  server:\n    root_password: {p}\n", p)
p = pw(); add("chef_attributes_rb", f"default['app']['db_password'] = '{p}'", p)
p = pw(); add("puppet_hiera", f"profile::db::password: '{p}'", p)
p = pw(); add("nomad_template_env", f'env {{\n  DB_PASSWORD = "{p}"\n}}', p)
p = pw(); add("systemd_environment", f'[Service]\nEnvironment="DB_PASSWORD={p}"\n', p)
p = pw(); add("supervisor_environment", f"[program:app]\nenvironment=DB_USER=\"app\",DB_PASSWORD=\"{p}\"\n", p)
p = pw(); add("crontab_inline_env", f"0 3 * * * PGPASSWORD={p} pg_dump -h db app > /backup/app.sql", p)
p = pw(); add("makefile_var", f"DEPLOY_PASSWORD := {p}\ndeploy:\n\t./deploy.sh", p)
p = pw(); add("justfile_export", f'export REGISTRY_PASSWORD := "{p}"', p)
p = pw(); add("tox_setenv", f"[testenv]\nsetenv =\n    DB_PASSWORD = {p}\n", p)
p = pw(); add("pytest_ini_env", f"[pytest]\nenv =\n    API_SECRET={p}\n", p)
p = pw(); add("jenkinsfile_env", f"environment {{\n    NEXUS_PASSWORD = '{p}'\n}}", p)
p = pw(); add("gitlab_ci_script_login", f"script:\n  - docker login -u ci -p {p} registry.example.invalid", p)
p = pw(); add("azure_pipelines_vars", f"variables:\n  sqlPassword: '{p}'\n", p)
p = pw(); add("bitbucket_pipelines_curl", f"- curl --user ci:{p} https://repo.example.invalid/upload", p)
p = pw(); add("circleci_env_yaml", f"environment:\n  POSTGRES_PASSWORD: {p}\n", p)
p = pw(); add("vagrantfile_ruby", f'config.vm.provision "shell", env: {{ "DB_PASS" => "{p}" }}', p)
p = pw(); add("packer_hcl_var", f'variable "winrm_password" {{\n  default = "{p}"\n}}', p)
p = pw(); add("cloud_init_chpasswd", f"chpasswd:\n  list: |\n    ubuntu:{p}\n  expire: false\n", p)
p = pw(); add("kickstart_rootpw", f"rootpw --plaintext {p}", p)
p = pw(); add("preseed_passwd", f"d-i passwd/root-password password {p}", p)
p = pw(); add("unattend_xml", f"<AdministratorPassword>\n  <Value>{p}</Value>\n  <PlainText>true</PlainText>\n</AdministratorPassword>", p)
p = pw(); add("ldif_userpassword", f"dn: uid=svc,ou=people,dc=example,dc=invalid\nuserPassword: {p}\n", p)
p = pw(); add("openldap_slapd_rootpw", f"rootpw          {p}", p)
p = pw(); add("pgbackrest_conf", f"[global]\nrepo1-cipher-pass={p}\n", p)
p = pw(); add("borg_passphrase_env", f"export BORG_PASSPHRASE='{p}'", p)
p = pw(); add("restic_password_flag_file", f"RESTIC_PASSWORD={p} restic -r s3:bucket backup /data", p)
p = pw(); add("rclone_conf_pass", f"[remote]\ntype = sftp\nhost = h\nuser = u\npass = {p}\n", p)
p = pw(); add("duplicity_env", f"PASSPHRASE={p} duplicity /home sftp://u@h/backup", p)
p = pw(); add("keepassxc_cli", f"echo '{p}' | keepassxc-cli ls vault.kdbx", p)
p = pw(); add("openssl_enc_k", f"openssl enc -aes-256-cbc -k {p} -in a -out a.enc", p)
p = pw(); add("zip_encrypt_long", f"zip --password {p} out.zip file.txt", p)
p = pw(); add("pdftk_user_pw", f"pdftk in.pdf output out.pdf user_pw {p}", p)
p = pw(); add("qpdf_encrypt", f"qpdf --encrypt {p} {p}x 256 -- in.pdf out.pdf", p)
p = pw(); add("wget_ftp_password", f"wget --ftp-user=me --ftp-password={p} ftp://h/f", p)
p = pw(); add("rsync_daemon_secrets", f"backup:{p}\n", p)
p = pw(); add("ftp_url_in_log", f"[INFO] uploading to ftp://deploy:{p}@ftp.example.invalid/www", p)
p = g(HX, 40); add("bitly_token_query", f"https://api-ssl.example.invalid/v3/shorten?access_token={p}&longUrl=x", p)
p = g(AN, 32); add("x_auth_token_lower_json", '{"headers": {"x-auth-token": "' + p + '"}}', p)
p = g(AN, 24); add("api_key_query_typo_casing", f"GET /v1/items?ApiKey={p}&page=2", p)
p = pw(); add("russian_prose", f"пароль от сервера: {p}", p)
p = pw(); add("japanese_prose", f"サーバーのパスワードは {p} です", p)
p = pw(); add("portuguese_prose", f"a senha do banco é {p}", p)
p = pw(); add("chat_message_password", f"ok I set the wifi password to {p} for the guests", p)
p = base64.b64encode(f"admin:{pw()}".encode()).decode(); add("basic_auth_header_lower", f"authorization: basic {p}", p)
p = pw(); add("smb_url_creds", f"smb://WORKGROUP;svc:{p}@nas/share", p)

B += [
    ("prisma_env_ref", 'DATABASE_URL="${DATABASE_URL}"'),
    ("django_env", "DATABASES = {'default': {'PASSWORD': os.environ['DB_PASSWORD']}}"),
    ("knex_env", "connection: { host: process.env.DB_HOST, password: process.env.DB_PASSWORD }"),
    ("laravel_env", "'password' => env('DB_PASSWORD', ''),"),
    ("spring_ref", "spring.datasource.password=${DB_PASSWORD}"),
    ("systemd_envfile", "[Service]\nEnvironmentFile=/etc/app/env\n"),
    ("cron_plain", "0 3 * * * /usr/local/bin/backup.sh >> /var/log/backup.log 2>&1"),
    ("makefile_plain", "VERSION := 1.4.2\nbuild:\n\tgo build ./..."),
    ("jenkins_cred_ref", "environment {\n    NEXUS = credentials('nexus-creds')\n}"),
    ("ci_secret_ref", "variables:\n  sqlPassword: $(SQL_PASSWORD)\n"),
    ("packer_sensitive", 'variable "winrm_password" {\n  sensitive = true\n}'),
    ("kickstart_iscrypted", "rootpw --iscrypted $6$saltsalt$abcdefghijklmnopqrstuv"),
    ("ldif_plain", "dn: uid=svc,ou=people,dc=example,dc=invalid\ncn: Service Account\n"),
    ("rclone_plain", "[remote]\ntype = s3\nprovider = AWS\nregion = eu-west-1\n"),
    ("openssl_pass_env", "openssl enc -aes-256-cbc -pass env:ENC_PASS -in a -out a.enc"),
    ("zip_plain", "zip -r out.zip src/"),
    ("wget_plain", "wget --tries=3 https://example.invalid/file.tar.gz"),
    ("ftp_url_nouser", "ftp://ftp.example.invalid/pub/file.txt"),
    ("query_page", "GET /v1/items?page=2&limit=50&sort=created_at"),
    ("prose_wifi", "the wifi password is on the fridge"),
    ("prose_ru_plain", "пароль должен содержать не менее 12 символов"),
    ("prose_pt_plain", "a senha deve ter pelo menos 8 caracteres"),
    ("basic_scheme_doc", "Authorization: Basic <base64 of user:pass>"),
    ("python_kwarg_var", "connect(host=host, user=user, password=password)"),
    ("ts_interface", "interface Config { password: string; token?: string }"),
    ("go_struct_tag", 'Password string `json:"password" yaml:"password"`'),
    ("sql_column_def", "password_hash VARCHAR(255) NOT NULL,"),
    ("diff_code_plain", "@@ -10,4 +10,4 @@\n-    retries = 3\n+    retries = 5\n     timeout = 30\n"),
    ("grep_code_plain", "src/auth.py:42:    if not check_password(user, password):\n"),
    ("log_line", "2026-11-15T10:22:31Z INFO user=alice action=login status=ok latency=12ms"),
]
