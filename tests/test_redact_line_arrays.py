"""Text a tool gives one line per string is redacted as the text it is (verifier v7b, e2_patchlines / p2_split).

Claude Code's Edit and Write responses carry `structuredPatch[].lines`, one JSON string per diff line, and stream-json
has the same in `tool_use_result`; a notebook's `source` is a list of lines. Each string was redacted on its own, so a
private key block lost only its header line and a YAML block scalar (`password: |` with the value on the next line) or a
TOML multi-line string was stored as written. Sibling strings (a list's consecutive strings, a dictionary's consecutive
string values, the texts of consecutive content blocks) are now redacted as one text, one item per line, and cut back
item by item (Redactor._run, Redactor.joined, _json_strings for arrays inside JSON text).
"""
from __future__ import annotations

import json

import pytest
from hooks_helpers import make_project

from devmem import capture, hooks
from devmem import sessions as S
from devmem.redact import Redactor

BEGIN = "-----BEGIN RSA " + "PRIVATE KEY-----"
END = "-----END RSA " + "PRIVATE KEY-----"
BODY = ["MIIEowIBAAKCAQEA" + "x7Kq2Lm9Pz4Rt8Vw3Yb6Nc1" * 2,
        "Qd5Fg0Hj2Kl4Mn6Op8Qr0St2Uv4Wx6Yz8Ab0Cd2Ef4Gh6Ij8Kl0Mn2Op4Qr6St8",
        "Uv0Wx2Yz4Ab6Cd8Ef0Gh2Ij4Kl6Mn8Op0Qr2St4Uv6Wx8Yz0Ab2Cd4Ef6Gh8Ij0="]
PW = "Zx9Kq2Lm7Pw4Rt"
SECRETS = BODY[1:] + [PW]

LINES = {
    "pem": [BEGIN, *BODY, END],
    "yaml-block": ["db:", "  password: |", "    " + PW, "  host: db"],
    "yaml-folded": ["db:", "  password: >-", "    " + PW, "  host: db"],
    "toml-multiline": ["[db]", 'password = """', PW, '"""'],
}


def _patch(lines: list[str]) -> list[str]:
    return [" a"] + ["+" + x for x in lines] + [" b"]


def _leaks(text: str) -> list[str]:
    return [s for s in SECRETS if s in text]


@pytest.mark.parametrize("name", sorted(LINES))
@pytest.mark.parametrize("shape", ["lines", "patch", "notebook"])
def test_a_multi_line_secret_given_one_line_per_string_is_redacted(name, shape):
    red = Redactor(None)
    lines = {"lines": LINES[name], "patch": _patch(LINES[name]), "notebook": [x + "\n" for x in LINES[name]]}[shape]
    resp = {"filePath": "/p/src/config.ts", "structuredPatch": [{"oldStart": 1, "newStart": 1, "lines": lines}]}
    tree, cut = red.tree(resp, 100_000)
    assert not cut and not _leaks(json.dumps(tree))
    assert len(tree["structuredPatch"][0]["lines"]) == len(lines)     # the list keeps its length
    assert not _leaks(S.output_text(resp, red))
    assert not _leaks(red.text(json.dumps(resp)))                     # the same as JSON text (a command printing it)
    assert not _leaks(red.text(json.dumps(json.dumps(resp))))         # and as JSON inside JSON


def test_folded_lines_come_back_empty_and_the_others_unchanged():
    red = Redactor(None)
    out = red.tree({"lines": _patch(LINES["pem"])}, 100_000)[0]["lines"]
    assert out == [" a", "+[REDACTED:private-key]", "+", "+", "+", "+", " b"]      # a key block keeps its lines
    out = red.tree({"lines": ["password = '''", "Zx9Kq2Lm7Pw4Rt", "'''", "next"]}, 100_000)[0]["lines"]
    assert out[-1] == "next" and len(out) == 4 and not any(PW in x for x in out)


@pytest.mark.parametrize("shape", ["dict-siblings", "content-blocks", "stdout-stderr"])
def test_split_across_sibling_values_and_content_blocks(shape):
    red = Redactor(None)
    x = {
        "dict-siblings": {"line1": "db:", "line2": "  password: |", "line3": "    " + PW},
        "content-blocks": {"content": [{"type": "text", "text": "  password: |"}, {"type": "text", "text": "    " + PW}]},
        "stdout-stderr": {"stdout": "-----BEGIN RSA " + "PRIVATE KEY-----\n" + "\n".join(BODY[:2]),
                          "stderr": "\n".join(BODY[2:]) + "\n" + END},
    }[shape]
    assert not _leaks(json.dumps(red.tree(x, 100_000)[0]))
    assert not _leaks(red.text(json.dumps(x)))      # the same as JSON text


def test_ordinary_lists_and_dicts_are_unchanged():
    red = Redactor(None)
    data = {"lines": [" import os", "-x = 1", "+x = 2", "", "", "+y = 3"], "args": ["--password", "--help"],
            "content": [{"type": "text", "text": "key: value"}, {"type": "text", "text": "  nested: 1"}],
            "a": "password:", "b": "required", "files": ["src/a.py", "src/b.py"], "n": [1, 2, "x"]}
    out, cut = red.tree(data, 100_000)
    assert out == data and not cut
    assert red.record(data) == data
    text = json.dumps(data)
    assert red.text(text) == text


def test_the_write_boundary_joins_raw_line_lists_too():
    red = Redactor(None)
    out = red.record({"tool_use_result": {"structuredPatch": [{"lines": _patch(LINES["pem"])}]}})
    assert not _leaks(json.dumps(out))


def test_a_budget_cut_inside_a_run_keeps_only_redacted_text():
    red = Redactor(None)
    lines = ["x" * 50] * 10 + _patch(LINES["pem"]) + ["y" * 50] * 10
    for budget in range(100, 1400, 37):
        out, cut = red.tree({"lines": lines}, budget)
        assert not _leaks(json.dumps(out)), budget
        assert all(o == i or "[REDACTED" in o or len(o) < len(i) for o, i in zip(out["lines"], lines)), budget


def test_stream_import_redacts_tool_use_result_lines(tmp_path):
    red = Redactor(None)
    tid = "toolu_1"
    events = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": "Edit", "input": {
            "file_path": str(tmp_path / "src" / "config.ts"), "old_string": "a", "new_string": "b"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]},
         "tool_use_result": {"filePath": str(tmp_path / "src" / "config.ts"),
                             "structuredPatch": [{"lines": _patch(LINES[n])} for n in sorted(LINES)]}},
    ]
    out = capture.redact_stream([json.dumps(e) for e in events], tmp_path, red)
    assert len(out) == 2 and not _leaks("\n".join(out))
    assert all(isinstance(json.loads(x), dict) for x in out)


def test_a_hook_edit_response_with_patch_lines_stores_no_key(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    f = str(project / "src" / "config.ts")
    new = "\n".join(LINES["pem"] + LINES["yaml-block"])
    payload = {"session_id": "lines-session", "cwd": str(project), "hook_event_name": "PostToolUse",
               "tool_name": "Edit", "tool_use_id": "t1",
               "tool_input": {"file_path": f, "old_string": "x", "new_string": new},
               "tool_response": {"filePath": f, "oldString": "x", "newString": new, "originalFile": "x\n",
                                 "structuredPatch": [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 9,
                                                      "lines": ["-x"] + ["+" + ln for ln in new.split("\n")]}]}}
    assert hooks.run(["PostToolUse"], json.dumps(payload).encode("utf-8"))[0] == 0
    stored = "".join(p.read_text(encoding="utf-8") for p in (project / ".devmemory").rglob("*") if p.is_file())
    assert "structuredPatch" in stored or "REDACTED" in stored
    assert not _leaks(stored)


# ---- the same problem one level up: pieces of one text that reach devmem separately


def _post(project, tid: str, stdout: str) -> None:
    payload = {"session_id": "split-session", "cwd": str(project), "hook_event_name": "PostToolUse",
               "tool_name": "Bash", "tool_use_id": tid, "tool_input": {"command": "cat part"},
               "tool_response": {"stdout": stdout, "stderr": ""}}
    assert hooks.run(["PostToolUse"], json.dumps(payload).encode("utf-8"))[0] == 0


def test_a_key_printed_by_two_tool_calls_is_caught_in_both_halves(tmp_path, monkeypatch):
    """Each hook event is redacted on its own: the first half of a key (header and body lines) is caught as a cut-off
    block, the second half (body lines right above the END line, no header) as key material without its header
    (the headless-body rule)."""
    project = make_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    _post(project, "t1", "\n".join([BEGIN, BODY[0], BODY[1]]))
    _post(project, "t2", "\n".join([BODY[2], END]))
    log = (project / ".devmemory" / "live" / "split-session.jsonl").read_text(encoding="utf-8")
    assert BODY[1] not in log
    assert BODY[2] not in log


def test_a_stream_text_over_its_cap_keeps_no_part_of_a_key_on_the_cut(tmp_path):
    """capture.STREAM_TEXT_CAP cuts one text before it is redacted (with margin): a key that straddles the cut is not
    stored in part (a cut-off block is redacted up to the cut; the margin is redacted with it)."""
    red = Redactor(None)
    for offset in (-100, 50, 900, 1900, 2050):
        filler = "x" * (capture.STREAM_TEXT_CAP + offset)
        text = filler + "\n" + "\n".join([BEGIN, *BODY, END]) + "\n" + "tail" * 1000
        ev = {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}
        out = capture.redact_stream([json.dumps(ev)], tmp_path, red)[0]
        assert not _leaks(out), offset
