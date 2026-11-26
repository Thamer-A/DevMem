"""build_pack reads only the files it cites (no full-project read, no BM25 rebuild), and its output is safe."""
from __future__ import annotations

import pathlib

from devmem.pack import build_pack
from devmem.scan import scan

from pack_helpers import FILES, make_project, standard_sessions


def test_reads_only_cited_files(tmp_path, monkeypatch):
    files = dict(FILES)
    for i in range(150):
        files[f"src/gen/module_{i}.py"] = f"def helper_{i}(x):\n    return x + {i}\n# generic filler text\n"
    project, store = make_project(tmp_path, files)
    standard_sessions(store)
    scan(project, store)
    build_pack(project, store, "warm up the shop")     # first call indexes the sessions
    reads: list[str] = []
    real_text, real_bytes = pathlib.Path.read_text, pathlib.Path.read_bytes

    def spy_text(self, *a, **k):
        if str(self).startswith(str(project / "src")):
            reads.append(str(self))
        return real_text(self, *a, **k)

    def spy_bytes(self, *a, **k):
        if str(self).startswith(str(project / "src")):
            reads.append(str(self))
        return real_bytes(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "read_text", spy_text)
    monkeypatch.setattr(pathlib.Path, "read_bytes", spy_bytes)
    text, info = build_pack(project, store, "make rerolling the shop offers cost more")
    assert info["files"]
    assert len(set(reads)) <= len(info["files"]) + 4, reads   # cited files + the shown sessions' edited files


def test_scaffolding_is_ascii(tmp_path):
    """Only user/project text can be non-ASCII; the pack's own words never are (any console can print them)."""
    project, store = make_project(tmp_path)
    standard_sessions(store)
    scan(project, store)
    text, _ = build_pack(project, store, "make rerolling the shop offers cost more")
    assert text and text.isascii()
    text.encode("cp1252")


def test_info_shape(tmp_path):
    project, store = make_project(tmp_path)
    standard_sessions(store)
    scan(project, store)
    text, info = build_pack(project, store, "make rerolling the shop offers cost more", budget=1500, mode="full")
    assert all(isinstance(f, str) for f in info["files"])
    assert all("session" in s for s in info["sessions"])
    assert info["tokens_est"] > 0 and info["mode"] == "full"
    for f in info["files"]:
        assert f"- `{f}`" in text
    for s in info["sessions"]:
        assert f"session {s['session'][:8]}" in text
