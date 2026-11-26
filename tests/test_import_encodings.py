"""A stream file in another encoding than UTF-8 (Windows PowerShell 5.1's `>` writes UTF-16 with a BOM) is decoded
as what it is, so the secret-file rule and redaction see its events; a line that still is not a JSON object is a
counted stub, never stored as text (fourth review: every UTF-16 line was stored as text with NULs between its
characters, .env text included)."""
from __future__ import annotations

import json
import re

import pytest
from hooks_helpers import make_project

from devmem import capture as C

SECRET = "QX7SECRETPLAIN"
ENV_TEXT = f"SERVICE_HOST=internal-db-zq81.corp.example\nNOTE={SECRET}\n"
SID = "11111111-2222-3333-4444-555555555555"


def _lines(project, n_pad: int = 0):
    env = str(project / ".env")
    ev = [{"type": "system", "subtype": "init", "session_id": SID, "model": "m"}]
    ev += [{"type": "assistant", "message": {"content": [{"type": "text", "text": f"pad {i} " + "w" * 200}]}}
           for i in range(n_pad)]
    ev += [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "A", "name": "Read",
                                                          "input": {"file_path": env}}]}},
           {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "A", "content": ENV_TEXT}]},
            "tool_use_result": {"file": {"content": ENV_TEXT}}},
           {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "B", "name": "Bash",
                                                          "input": {"command": "cat .env"}}]}},
           {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "B", "content": ENV_TEXT,
                                                     "is_error": True}]}, "tool_use_result": {"stdout": ENV_TEXT}},
           {"type": "result", "result": "done", "session_id": SID}]
    return [json.dumps(e) for e in ev]


def _encode(case: str, lines: list[str]) -> bytes:
    text = "\n".join(lines) + "\n"
    return {
        "utf16_bom_crlf": text.replace("\n", "\r\n").encode("utf-16"),          # PowerShell 5.1 `>`
        "utf16_le_no_bom": text.encode("utf-16-le"),
        "utf16_be_bom": b"\xfe\xff" + text.encode("utf-16-be"),
        "utf32_le_bom": b"\xff\xfe\x00\x00" + text.encode("utf-32-le"),
        "utf32_be_bom": b"\x00\x00\xfe\xff" + text.encode("utf-32-be"),
        "utf8_bom": b"\xef\xbb\xbf" + text.encode(),
        "bom_each_line": "".join("﻿" + x + "\n" for x in lines).encode(),
        "nul_bytes": text.replace("\n", "\x00\n").encode(),
        "latin1": text.replace("internal", "int\xe9rnal").encode("latin-1"),
    }[case]


def _secret_anywhere(store) -> list[str]:
    hits = []
    for f in store.rglob("*"):
        if not f.is_file():
            continue
        data = f.read_bytes()
        for text in (data.decode("utf-8", "replace"), data.decode("utf-16-le", "replace"),
                     data.decode("utf-16-be", "replace")):
            flat = re.sub(r"(\\u0000|\x00|\\ufffd|�)", "", text)
            if SECRET in flat or "zq81" in flat:
                hits.append(str(f.relative_to(store)))
    return hits


DECODED = ["utf16_bom_crlf", "utf16_le_no_bom", "utf16_be_bom", "utf32_le_bom", "utf32_be_bom", "utf8_bom",
           "bom_each_line", "latin1"]


@pytest.mark.parametrize("case", DECODED + ["nul_bytes"])
def test_a_stream_in_another_encoding_is_read_as_events_and_keeps_no_secret(tmp_path, case):
    project = make_project(tmp_path)
    (project / ".env").write_text(ENV_TEXT, encoding="utf-8")
    path = tmp_path / "s.jsonl"
    path.write_bytes(_encode(case, _lines(project)))
    info: dict = {}
    meta = C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory", info=info)
    assert _secret_anywhere(project / ".devmemory") == []
    rec = json.loads(meta.read_text(encoding="utf-8"))
    if case in DECODED:
        assert rec["session_id"] == SID and rec["result_text"] == "done"
        assert [c["tool"] for c in rec["tool_calls"]] == ["Read", "Bash"]
        assert rec["turns"][0]["errors"] == [{"line": 4, "error": C.OMITTED_SECRET_FILE}]
    else:                      # NUL before each newline: no line is JSON; each is a counted stub, none is text
        assert rec["session_id"] is None and info["stream_lines_not_read"] == 6


@pytest.mark.parametrize("case", ["utf16_bom_crlf", "utf16_le_no_bom", "utf32_le_bom"])
@pytest.mark.parametrize("odd", [0, 1, 3])
def test_a_large_utf16_or_utf32_file_is_read_as_its_head_and_tail_on_whole_characters(tmp_path, monkeypatch, case,
                                                                                         odd):
    monkeypatch.setattr(C, "STREAM_READ_HEAD", 4_000 + odd)
    monkeypatch.setattr(C, "STREAM_READ_TAIL", 6_000 + odd)
    project = make_project(tmp_path)
    (project / ".env").write_text(ENV_TEXT, encoding="utf-8")
    path = tmp_path / "s.jsonl"
    lines = _lines(project, n_pad=200)
    path.write_bytes(_encode(case, lines[:1] + lines[1:-5] + lines[-5:]))
    info: dict = {}
    meta = C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory", info=info)
    rec = json.loads(meta.read_text(encoding="utf-8"))
    assert rec["session_id"] == SID and rec["result_text"] == "done"          # the head and the tail decoded
    assert info["stream_left_out"]["bytes"] > 0 and _secret_anywhere(project / ".devmemory") == []
    raw = meta.with_name(meta.name.replace(".meta.json", ".jsonl")).read_text(encoding="utf-8")
    assert "\x00" not in raw and "�" not in raw


def test_the_encoding_is_decided_from_the_first_bytes():
    assert C._encoding(b"\xff\xfe{\x00") == ("utf-16-le", 2)
    assert C._encoding(b"\xff\xfe\x00\x00{\x00\x00\x00") == ("utf-32-le", 4)
    assert C._encoding("{\"type\": 1}".encode("utf-16-be")) == ("utf-16-be", 0)
    assert C._encoding(b'{"type": "a\x00b"}') == ("utf-8", 0)                # a few NULs: not UTF-16
    assert C._encoding(b"") == ("utf-8", 0)
