"""The other readers that asked for their whole cap up front (the pattern tests/test_read_sizes.py covers for
store.read_regular and the hook stdin): the transcript read (2 MB per event), a project settings file
(init._read_settings) and the plugin hook's stdin (32 MB on every event). Each now asks for the size there is, then
reads on a megabyte at a time."""
from __future__ import annotations

import io
import json
import os

from devmem import init, plugin, store
from devmem import sessions as S


class _Spy(io.BufferedReader):
    sizes: list[int] = []

    def read(self, n=-1):
        _Spy.sizes.append(n)
        return super().read(n)


def _spy_fdopen(monkeypatch):
    real = os.fdopen
    monkeypatch.setattr(os, "fdopen", lambda fd, mode="r", *a, **k: _Spy(real(fd, "rb", buffering=0)))
    _Spy.sizes = []


def test_read_bounded_reads_on_when_the_file_grew_and_stops_at_the_cap():
    assert store.read_bounded(io.BytesIO(b"x" * 10), 10, 100) == b"x" * 10
    assert store.read_bounded(io.BytesIO(b"x" * 3_000_000), 10, 4_000_000) == b"x" * 3_000_000   # grew after fstat
    assert store.read_bounded(io.BytesIO(b"x" * 3_000_000), 10, 2_500_000) == b"x" * 2_500_000
    assert store.read_bounded(io.BytesIO(b""), 0, 5) == b""


def test_the_transcript_read_asks_for_what_is_there(tmp_path, monkeypatch):
    tp = tmp_path / "t.jsonl"
    line = json.dumps({"type": "assistant", "message": {"id": "m", "content": [{"type": "text", "text": "hi"}]}})
    tp.write_bytes((line + "\n").encode("utf-8"))
    _spy_fdopen(monkeypatch)
    items, offset = S.read_transcript_since(str(tp), 0)
    assert [it["text"] for it in items if it["kind"] == "text"] == ["hi"] and offset == tp.stat().st_size
    assert _Spy.sizes and max(_Spy.sizes) <= len(line) + 2


def test_the_transcript_read_still_takes_a_full_budget_of_a_big_file(tmp_path):
    tp = tmp_path / "t.jsonl"
    line = json.dumps({"type": "assistant", "message": {"id": "m", "content": [{"type": "text", "text": "x" * 100}]}})
    tp.write_bytes(((line + "\n") * 40_000).encode("utf-8"))      # about 5 MB
    items, offset = S.read_transcript_since(str(tp), 0, text_budget=10**9)
    assert offset > 2 * 1024 * 1024 - len(line) - 2 and offset <= 2 * 1024 * 1024


def test_a_settings_file_is_read_by_its_size(tmp_path, monkeypatch):
    p = tmp_path / ".claude" / "settings.local.json"
    p.parent.mkdir()
    p.write_bytes(b'{"hooks": {}}')
    _spy_fdopen(monkeypatch)
    assert init._read_settings(p, tmp_path) == b'{"hooks": {}}'      # the `devmem init` path (project given)
    assert _Spy.sizes and max(_Spy.sizes) <= 13 + 1


def test_the_plugin_hook_reads_its_stdin_in_bounded_steps():
    _Spy.sizes = []
    assert plugin._read_payload(_Spy(io.BytesIO(b"{}" + b" " * 3_000_000))).startswith(b"{}")
    assert max(_Spy.sizes) <= 1 << 20
    data = plugin._read_payload(_Spy(io.BytesIO(b"z" * 5000)), limit=100)
    assert data == b"z" * 101
    assert plugin._read_payload(None) == b""
