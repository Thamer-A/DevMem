"""V9: an event whose text holds U+2028, U+2029 or U+0085 is kept.

Events are written with ensure_ascii=False, so these characters appear raw inside JSON strings, and str.splitlines()
splits at them: read_events (and the stream readers) cut such a line in two and dropped the event. One such character
in a prompt, a reply or a fetched page removed that event from the record.
"""
from __future__ import annotations

import json

import pytest

from devmem import capture, claude_cli, hooks
from devmem.sessions import read_events


@pytest.mark.parametrize("ch", [" ", " ", "\u0085", "\x1c", "\x1d", "\x1e", "\x0b", "\x0c"])
def test_events_with_unicode_line_separators_are_kept(tmp_path, ch):
    log = tmp_path / "live" / "s.jsonl"
    hooks._append(log, {"event": "UserPromptSubmit", "prompt": f"before{ch}after"})
    hooks._append(log, {"event": "Stop", "n": 2})
    events = read_events(log)
    assert [e["event"] for e in events] == ["UserPromptSubmit", "Stop"]
    assert events[0]["prompt"] == f"before{ch}after"


def test_stream_readers_keep_such_lines(tmp_path):
    stream = tmp_path / "s.jsonl"
    rows = [{"type": "system", "subtype": "init", "session_id": "x"},
            {"type": "result", "result": "a b", "session_id": "x"}]
    stream.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    assert len(capture._load_lines(stream)) == 2
    assert claude_cli.parse_stream(stream).get("result_text") == "a b"
