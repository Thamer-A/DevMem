"""Secret-file omission sees a secret file named anywhere in a tool's input (verifier round 5: MCP tools whose path
argument is `relative_path` (Serena), `pathInProject` (JetBrains), `filePath`, `absolute_path`, a `file://` URL or a
nested `args.path` got `.env` content stored; `head *` output with `==> .env <==` was kept; `deploy.ppk`, `.envrc` and
kubeconfig were not secret files; `cat .e''nv` and `cat .\\env` were not flagged)."""
from __future__ import annotations

import json

import pytest
from hooks_helpers import make_project

from devmem import hooks
from devmem.redact import input_names_secret_file, is_secret_file, mentions_secret_file, omit_secret_file_lines


@pytest.fixture()
def proj(tmp_path):
    p = tmp_path / "secret-project"         # the project's own folder name never counts
    p.mkdir()
    (p / ".env").write_text("DB_PASSWORD=x\n", encoding="utf-8")
    (p / "src").mkdir()
    return p


def _inputs(p):
    env = str(p / ".env")
    url = "file:///" + env.replace("\\", "/").lstrip("/")
    return [
        {"relative_path": ".env"}, {"pathInProject": ".env"}, {"filePath": env}, {"absolute_path": env},
        {"file_paths": [env]}, {"uri": url}, {"url": url}, {"url": "file://localhost/" + env.replace("\\", "/").lstrip("/")},
        {"args": {"path": ".env"}}, {"input": {"file_path": ".env"}}, {"file_path": env + "\x00"},
        {"arguments": [{"name": "x", "target_file": ".env"}]}, {"args": [".env"]}, {"cmd": "cat .env"},
        {"script": "type .env"}, {"code": "print(open('.env').read())"}, {"pattern": "PASSWORD", "glob": ".env"},
        {"pattern": "x", "include": ".env"}, {"file": "%2Eenv".replace("%2E", ".")},
        {"path": "~/.kube/config"}, {"path": "/home/x/.docker/config.json"}, {"path": "C:/Users/x/.config/gh/hosts.yml"},
        {"path": "deploy.ppk"}, {"path": ".envrc"}, {"path": "infra/terraform.tfstate"}, {"path": "kubeconfig"},
        {"path": "ops/prod.kubeconfig"}, {"path": ".dev.vars"}, {"path": "local.settings.json"}, {"path": "AuthKey_AB12.p8"},
    ]


def test_a_secret_file_named_under_any_key_is_seen(proj):
    for inp in _inputs(proj):
        assert input_names_secret_file(inp, proj), inp


@pytest.mark.parametrize("inp", [
    {"pattern": "secrets.get", "path": "src"}, {"name": "secrets-manager"}, {"query": "select * from secrets"},
    {"url": "https://github.com/org/secrets"}, {"description": "read the .env loader", "command": "pytest -q"},
    {"old_string": "load('.env')", "new_string": "x", "file_path": "app.py"}, {"glob": "*.py", "pattern": "token"},
    {"repo": "org/credentials-helper"}, {"branch": "fix/secret-scan"}, {"exclude": ".env"},
    {"content": "cat .env\nDB_PASSWORD=1"}, {"prompt": "show me .env"}, {"pattern": ".env", "path": "."},
    {"todos": [{"content": "rotate .env values", "activeForm": "Rotating .env values"}]},
])
def test_a_secret_file_mentioned_in_text_or_a_search_does_not_hide_the_result(proj, inp):
    assert not input_names_secret_file(inp, proj), inp


@pytest.mark.parametrize("cmd", ["cat .e''nv", 'cat .e"n"v', "cat .\\env", "cat $'\\x2eenv'", "cat $'\\056env'",
                                 "cat '.e'nv", 'cat ".env"', "less .en\\v", "cat deploy.ppk", "source .envrc",
                                 "cat ~/.kube/config", "cat " + "x " * 15000 + ".env"])
def test_shell_quoting_does_not_hide_a_secret_file(cmd):
    assert mentions_secret_file(cmd), cmd


@pytest.mark.parametrize("cmd", ["cat notes.txt", "ls -la", "git status", "echo hello", "pytest -k env"])
def test_ordinary_commands_are_not_flagged(cmd):
    assert not mentions_secret_file(cmd), cmd


def test_new_secret_names_and_locations():
    for p in (".envrc", "deploy.ppk", "terraform.tfstate", "x.tfstate.backup", ".dev.vars", "local.settings.json",
              ".vault-token", ".my.cnf", ".s3cfg", "kubeconfig", "prod.kubeconfig", "AuthKey_X.p8",
              "service-account-prod.json", "/home/me/.kube/config", "C:\\Users\\me\\.docker\\config.json"):
        assert is_secret_file(p), p
    for p in ("config.py", "src/kube/config.yaml", "docker/config.json", "envrc.md", "tfstate.py"):
        assert not is_secret_file(p), p


def test_head_of_several_files_omits_the_secret_one():
    out = "==> a.py <==\nx = 1\n\n==> .env <==\nDB_PASSWORD=hunter22x\nB=2\n\n==> b.py <==\ny = 2\n"
    kept = omit_secret_file_lines(out)
    assert "hunter22x" not in kept and "B=2" not in kept
    assert "==> .env <==" in kept and "x = 1" in kept and "y = 2" in kept and "==> b.py <==" in kept


def test_end_to_end_an_mcp_read_with_an_unusual_path_key_stores_no_content(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    (project / ".env").write_text("DB_PASSWORD=Hunter2Unusual99\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    sid = "5e55107d-0000-4000-8000-0000000000f1"
    base = {"session_id": sid, "cwd": str(project), "transcript_path": str(tmp_path / "t.jsonl")}

    def send(event, **f):
        hooks.run([event], json.dumps({**base, "hook_event_name": event, **f}).encode("utf-8"))
    send("SessionStart", source="startup")
    send("UserPromptSubmit", prompt="look at the config", prompt_id="p1")
    for i, (tool, inp) in enumerate([("mcp__serena__read_file", {"relative_path": ".env"}),
                                     ("mcp__jetbrains__get_file_text_by_path", {"pathInProject": ".env"}),
                                     ("mcp__x__read", {"args": {"path": ".env"}}),
                                     ("mcp__fs__read", {"uri": "file:///" + str(project / ".env").replace("\\", "/").lstrip("/")}),
                                     ("Bash", {"command": "cat .e''nv"}),
                                     ("Bash", {"command": "head -n 5 *"})]):
        out = "SECRET=Hunter2Unusual99" if tool != "Bash" or "head" not in inp["command"] else \
            "==> .env <==\nDB_PASSWORD=Hunter2Unusual99\n\n==> greet.py <==\ndef greet(name):"
        send("PostToolUse", tool_name=tool, tool_use_id=f"t{i}", tool_input=inp,
             tool_response={"content": [{"type": "text", "text": out}]} if tool != "Bash" else {"stdout": out})
    send("Stop", last_assistant_message="done")
    send("SessionEnd", reason="other")
    store = project / ".devmemory"
    found = [p for p in store.rglob("*") if p.is_file() and b"Hunter2Unusual99" in p.read_bytes()]
    assert not found, found
