"""Secrets inside JSON-serialised tool data (verifier round 6). Claude Code's Edit response carries `originalFile` (the
whole file), Write's carries `content`, WebFetch's `result`, an MCP tool's a structured result; sessions.output_text
wrote such a response as `json.dumps(response)` and tool_entries the input of MCP/Task/WebFetch calls the same way, so
the file text reached redaction with its quotes and newlines escaped, where the line rules (YAML `key: value`, TOML
`name = "value"`, .properties, HCL, PowerShell) never matched: the password of `config/application.yml` was stored.

Now every string of a tool input or response is redacted as the plain text it is before it is serialised
(Redactor.tree, used by output_text and tool_entries), Redactor.text decodes each JSON string literal of a text
(_json_strings, up to eight levels of JSON in JSON, also a literal cut off at the end), and the write boundary judges a
dictionary item together with its key (`{"password": "..."}`). These tests replay the recorded Edit and Write payloads
(cc-2.1.323) and the shapes of MultiEdit, WebFetch and MCP calls with secrets inside, then search every byte of the
store. The secrets are put together at run time."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from hooks_helpers import load_payload, make_project

from devmem import hooks
from devmem import sessions as S
from devmem.redact import Redactor


def j(*parts: str) -> str:
    return "".join(parts)


PW_YAML = j("Yml", "pw", "7391", "Tzq")
PW_TOML = j("Tml", "pw", "4410", "Kxa")
PW_PROPS = j("Prp", "pw", "8823", "Lmo")
PW_HCL = j("Hcl", "pw", "6604", "Vbn")
PW_PS = j("Pws", "pw", "1199", "Qrt")
PW_K8S = j("Kbs", "pw", "5530", "Lqx")
SECRETS = [PW_YAML, PW_TOML, PW_PROPS, PW_HCL, PW_PS, PW_K8S]

YAML = f"spring:\n  datasource:\n    url: jdbc:mysql://db:3306/app\n    username: app\n    password: {PW_YAML}\n"
TOML = f'[database]\nhost = "db"\ndb_password = "{PW_TOML}"\n'
PROPS = f"spring.datasource.url=jdbc:mysql://db/app\nspring.datasource.password={PW_PROPS}\n"
HCL = f'resource "aws_db_instance" "db" {{\n  username = "app"\n  password = "{PW_HCL}"\n}}\n'
PS = f'$pw = ConvertTo-SecureString "{PW_PS}" -AsPlainText -Force\n'
K8S = f"apiVersion: v1\nkind: Secret\nstringData:\n  DB_PASSWORD: {PW_K8S}\n"
FILES = {"yaml": YAML, "toml": TOML, "props": PROPS, "hcl": HCL, "ps": PS, "k8s": K8S}
ALL = YAML + TOML + PROPS + HCL + PS + K8S


def _forms(secret: str) -> list[bytes]:
    """The secret as it could sit in a file: plain, JSON-escaped once or twice (the same bytes for these values)."""
    return [secret.encode("utf-8"), json.dumps(secret)[1:-1].encode("utf-8")]


def _found(dirs: list[Path]) -> list[str]:
    out = []
    for d in dirs:
        for p in d.rglob("*"):
            if p.is_file():
                data = p.read_bytes()
                out += [f"{s} in {p.name}" for s in SECRETS if any(f in data for f in _forms(s))]
    return out


@pytest.mark.parametrize("name", sorted(FILES))
@pytest.mark.parametrize("levels", [0, 1, 2, 3, 8])
def test_each_config_shape_is_redacted_inside_json_up_to_eight_levels(name, levels):
    text = FILES[name]
    for _ in range(levels):
        text = json.dumps({"result": text, "status": 200})
    out = Redactor(None).text(text)
    assert not [s for s in SECRETS if s in out], out


@pytest.mark.parametrize("name", sorted(FILES))
def test_a_literal_cut_off_at_the_end_is_decoded_too(name):
    dump = json.dumps({"originalFile": FILES[name] + "tail: 1\n" * 3})
    cut = dump[:dump.index("tail")]          # what a cap before redaction leaves: the closing quote is gone
    out = Redactor(None).text(cut)
    assert not [s for s in SECRETS if s in out], out


def test_unknown_response_shapes_hold_redacted_strings_before_they_are_serialised():
    red = Redactor(None)
    for resp in ({"filePath": "/p/a.yml", "originalFile": ALL, "structuredPatch": []},
                 {"bytes": 1, "code": 200, "result": ALL, "url": "https://example.invalid/"},
                 {"content": [{"type": "text", "text": "rows"}, {"type": "resource", "resource": {"text": ALL}}]},
                 [{"row": {"config": ALL}}]):
        out = S.output_text(resp, red, 100_000)
        assert not [s for s in SECRETS if s in out], out
        json.loads(out)                         # still JSON
    # without a redactor the text is unredacted JSON (the store's redaction decodes its literals)
    raw = S.output_text({"originalFile": ALL})
    assert PW_YAML in raw and PW_YAML not in red.text(raw)


def test_tree_is_bounded_by_its_budget_whatever_the_response_size():
    red = Redactor(None)
    big = {"filePath": "/p/a.yml", "originalFile": ("x: 1\n" * 1_000_000) + YAML, "other": ["a" * 100_000] * 50}
    best = 1e9
    for _ in range(3):
        t = time.perf_counter()
        out, trunc = red.tree(big, 5000)
        best = min(best, time.perf_counter() - t)
    assert trunc and best < 1.0, best
    assert sum(len(v) for v in out.get("other", [])) + len(out["originalFile"]) <= 5000   # what did not fit is left out


def test_a_dictionary_item_is_judged_with_its_key_at_the_write_boundary():
    red = Redactor(None)
    out = red.record({"input": {"password": PW_YAML, "db_password": PW_TOML, "token_count": "12345678"},
                      "env": [{"name": "DB_PASSWORD", "value": PW_K8S}, {"name": "LOG_LEVEL", "value": "debug"}],
                      "note": "a word", "secret_file": True, "password_hint": "the dog's name"})
    s = json.dumps(out)
    assert not [x for x in SECRETS if x in s], s
    assert out["input"]["token_count"] == "12345678" and out["env"][1]["value"] == "debug"
    assert out["password_hint"] == "the dog's name" and out["secret_file"] is True
    # a raw record line (a transcript line) parsed and redacted value by value
    line = json.dumps({"type": "tool_use", "input": {"password": PW_YAML, "sql": f"-- x\nPASSWORD: {PW_TOML}\n"}})
    red_line = S._redact_line(line, red)
    assert PW_YAML not in red_line and PW_TOML not in red_line, red_line


def test_ordinary_json_keeps_its_exact_text():
    red = Redactor(None)
    for obj in ({"filePath": "/p/greet.py", "originalFile": 'def greet(name):\n    return "hello " + name\n'},
                {"content": "password: str\ntoken = tokenize(text)\nmax_tokens: 100\n", "url": "a\\b"},
                {"cmd": "grep -n 'key' src/*.py", "note": "the \u00e9t\u00e9 key\tis kept", "path": "C:\\Users\\me"},
                {"rows": [{"name": "timeout", "value": "30"}, {"name": "api_key_header", "value": "X-Api-Key"}]}):
        for text in (json.dumps(obj), json.dumps(obj, ensure_ascii=False), json.dumps(json.dumps(obj))):
            assert red.text(text) == text, text
        assert red.record(obj) == obj


def _send(event: str, payload: dict) -> None:
    hooks.run([event], json.dumps(payload).encode("utf-8"))


def test_recorded_edit_write_and_other_tool_shapes_store_no_secret(tmp_path, monkeypatch):
    project = make_project(tmp_path / "p")
    store = project / ".devmemory"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    for name, text in FILES.items():
        (project / f"conf_{name}.txt").write_text(text, encoding="utf-8")
    start = load_payload("000_SessionStart", project, transcript)
    _send("SessionStart", start)
    _send("UserPromptSubmit", load_payload("002_UserPromptSubmit", project, transcript))
    base = {k: start[k] for k in ("session_id", "cwd", "transcript_path")}
    # the recorded Edit and Write payloads, their file text replaced by config files with secrets in them
    edit = load_payload("008_PostToolUse", project, transcript)
    edit["tool_response"]["originalFile"] = ALL
    edit["tool_response"]["structuredPatch"] = [{"oldStart": 1, "lines": [f"-    password: {PW_YAML}",
                                                                          f"+    password: {PW_YAML}x"]}]
    _send("PostToolUse", edit)
    write = load_payload("010_PostToolUse", project, transcript)
    write["tool_input"]["content"] = ALL
    write["tool_response"]["content"] = ALL
    write["tool_response"]["originalFile"] = ALL
    _send("PostToolUse", write)
    f = str(project / "conf_yaml.txt")
    shapes = [
        ("MultiEdit", {"file_path": f, "edits": [{"old_string": "db:3306", "new_string": "db:3307"}]},
         {"filePath": f, "edits": [{"old_string": "db:3306", "new_string": "db:3307"}], "originalFile": ALL,
          "structuredPatch": [], "userModified": False}),
        ("WebFetch", {"url": "https://example.invalid/docs", "prompt": "the config"},
         {"bytes": 100, "code": 200, "codeText": "OK", "result": "Example config:\n" + ALL, "durationMs": 5,
          "url": "https://example.invalid/docs"}),
        ("mcp__k8s__apply", {"manifest": K8S + HCL, "namespace": "prod"},
         {"content": [{"type": "text", "text": "applied"}], "structuredContent": {"applied": ALL}}),
        ("mcp__db__query", {"sql": f"-- seed\nPASSWORD: {PW_TOML}\n", "params": {"password": PW_PS}},
         [{"type": "text", "text": "ok"}, {"type": "image", "data": "AAAA", "mimeType": "image/png", "alt": ALL}]),
        ("Task", {"description": "check config", "prompt": "Use this config:\n" + ALL},
         {"content": [{"type": "text", "text": "done"}], "totalTokens": 10}),
    ]
    for i, (tool, inp, resp) in enumerate(shapes):
        _send("PostToolUse", {**base, "hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": inp,
                              "tool_response": resp, "tool_use_id": f"toolu_{i:04d}"})
    _send("Stop", {**base, "hook_event_name": "Stop", "last_assistant_message": "done"})
    _send("SessionEnd", {**base, "hook_event_name": "SessionEnd", "reason": "other"})
    assert list((store / "sessions").glob("*.meta.json")), "no record written"
    assert not _found([store]), _found([store])
