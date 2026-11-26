"""Secret files, round 7 (verifier round 6, r21): names that were read and indexed (`prod.env`, `.env-local`, `.env~`,
`terraform.tfvars`, `*.auto.tfvars`, `.dockercfg`, `wp-config.php`), shell spellings that reached one unnoticed
(brace expansion `cat .e{n,}v`, PowerShell's backtick `.e`nv`, cmd's caret `.e^nv`) and commands under keys other than
`command` (`interact_with_process {"input": ...}`, `{"argv": [...]}`, `{"args": ...}`). Ordinary names keep their
output."""
from __future__ import annotations

import pytest

from devmem.redact import Redactor, is_secret_file
from devmem.store import HARD_IGNORES

SECRET_NAMES = ["prod.env", "web-variables.env", ".env-local", ".env_local", ".env~", "terraform.tfvars",
                "terraform.tfvars.json", "prod.auto.tfvars", "infra/prod.auto.tfvars.json", ".dockercfg", "wp-config.php"]
ORDINARY = ["environment.md", "token_utils.ts", "keys.py", "keyboard.py", "env.ts", "environment.prod.ts",
            "Makefile.envsubst", "variables.tf", "main.tf", "docker-compose.yml", "config.php", "env.local"]


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_more_secret_file_names(name):
    assert is_secret_file(name)


@pytest.mark.parametrize("name", ORDINARY)
def test_ordinary_names_are_not_secret_files(name):
    assert not is_secret_file(name)


def test_the_scanner_skips_them_too():
    import fnmatch
    for name in ("prod.env", ".env-local", ".env~", "terraform.tfvars", "x.auto.tfvars", ".dockercfg", "wp-config.php"):
        assert any(fnmatch.fnmatch(name.lower(), p) for p in HARD_IGNORES), name


READS = [
    ("Bash", {"command": "cat .e{n,}v"}),
    ("Bash", {"command": "cat .{env,x}"}),
    ("Bash", {"command": "head -n 5 config/{.env,app.yml}"}),
    ("PowerShell", {"command": "Get-Content .e`nv"}),
    ("Bash", {"command": "type .e^nv"}),
    ("Bash", {"command": "cat prod.env"}),
    ("Bash", {"command": "docker compose --env-file prod.env config"}),
    ("Bash", {"command": "cat terraform.tfvars"}),
    ("mcp__desktop-commander__interact_with_process", {"pid": 4242, "input": "cat .env"}),
    ("mcp__terminal__run", {"argv": ["sh", "-c", "cat .env"]}),
    ("mcp__terminal__run", {"argv": ["cat", ".env"]}),
    ("mcp__terminal__run", {"args": "cat .env"}),
    ("mcp__x__exec", {"line": "cat .env"}),
    ("Grep", {"pattern": "KEY", "glob": "*.env"}),
]
KEPT = [
    ("Read", {"file_path": "docs/environment.md"}),
    ("Read", {"file_path": "src/token_utils.ts"}),
    ("Read", {"file_path": "src/keys.py"}),
    ("Bash", {"command": "grep -rn 'load_dotenv' src/"}),
    ("Bash", {"command": "echo {a,b}.txt && ls src/{x,y}.py"}),
    ("PowerShell", {"command": "Write-Host `n done"}),
    ("Bash", {"command": "grep -n '^import' src/app.py"}),
    ("mcp__terminal__run", {"args": ["secrets.get", "--help"]}),
    ("mcp__desktop-commander__interact_with_process", {"pid": 1, "input": "print('environment ok')"}),
    ("mcp__x__search", {"query": "how to load .env in node"}),
]


@pytest.mark.parametrize("name,inp", READS, ids=[str(i) for i in range(len(READS))])
def test_reads_of_secret_files_are_recognised(name, inp, tmp_path):
    assert Redactor(None).tool_reads_secret_file(name, inp, tmp_path)


@pytest.mark.parametrize("name,inp", KEPT, ids=[str(i) for i in range(len(KEPT))])
def test_ordinary_calls_keep_their_output(name, inp, tmp_path):
    assert not Redactor(None).tool_reads_secret_file(name, inp, tmp_path)


@pytest.mark.parametrize("command", ["{a,b}{c,d}{e,f} " * 1500, "{" + "a," * 20000 + "}", "x`y^z " * 4000,
                                     "a{b,c}" * 5000])
def test_the_new_spellings_cost_little_on_long_commands(command):
    import time
    from devmem.redact import mentions_secret_file
    best = 1e9
    for _ in range(3):
        t = time.perf_counter()
        mentions_secret_file(command)
        best = min(best, time.perf_counter() - t)
    assert best < 1.0, best
