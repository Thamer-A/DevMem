"""sessions.move_file: when the rename fails (another file system; on Windows a reader holding the target open), the
copy it makes instead is private from the start.

It used shutil.copyfile and then tighten(): with the usual umask 022 the copy of a session's raw log (prompts, tool
output) was world-readable until the chmod, and a link at the target was written through.
"""
from __future__ import annotations

import errno
import os
import stat

import pytest

from devmem import sessions, store

posix = pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
DATA = b'{"event": "secret prompt"}\n' * 5000


def _no_rename(monkeypatch, src):
    real = os.replace

    def replace(a, b, *args, **kw):
        if os.fspath(a) == os.fspath(src):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(a, b, *args, **kw)
    monkeypatch.setattr(os, "replace", replace)


@pytest.fixture
def umask022():
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


@posix
@pytest.mark.parametrize("existing", [False, True], ids=["new", "existing 0644"])
def test_the_copy_is_never_readable_by_others(tmp_path, monkeypatch, umask022, existing):
    src = tmp_path / "live" / "s.jsonl"
    src.parent.mkdir()
    src.write_bytes(DATA)
    dst = tmp_path / "sessions" / "x.jsonl"
    if existing:
        dst.parent.mkdir()
        dst.write_bytes(b"older\n")
        dst.chmod(0o644)
    _no_rename(monkeypatch, src)
    seen = []
    real_tighten = store.tighten
    monkeypatch.setattr(store, "tighten", lambda p: (seen.append((os.fspath(p), stat.S_IMODE(os.lstat(p).st_mode))),
                                                     real_tighten(p))[1])
    sessions.move_file(src, dst)
    assert dst.read_bytes() == DATA and not src.exists()
    # by the time anything narrows it, the copy is already private: it never had group or other bits
    assert [m for p, m in seen if p == os.fspath(dst)] == [0o600]


@posix
def test_the_copy_never_writes_through_a_link(tmp_path, monkeypatch):
    src = tmp_path / "s.jsonl"
    src.write_bytes(DATA)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"keep\n")
    dst = tmp_path / "sessions" / "x.jsonl"
    dst.parent.mkdir()
    dst.symlink_to(victim)
    _no_rename(monkeypatch, src)
    with pytest.raises(OSError):
        sessions.move_file(src, dst)
    assert victim.read_bytes() == b"keep\n" and src.read_bytes() == DATA


@posix
def test_a_fifo_at_the_target_fails_instead_of_blocking(tmp_path, monkeypatch):
    src = tmp_path / "s.jsonl"
    src.write_bytes(DATA)
    dst = tmp_path / "x.jsonl"
    os.mkfifo(dst)
    _no_rename(monkeypatch, src)
    with pytest.raises(OSError):
        sessions.move_file(src, dst)
    assert src.read_bytes() == DATA


def test_the_copy_keeps_the_bytes_everywhere(tmp_path, monkeypatch):
    src = tmp_path / "s.jsonl"
    src.write_bytes(DATA + "é\r\n".encode("utf-8"))
    dst = tmp_path / "sessions" / "x.jsonl"
    _no_rename(monkeypatch, src)
    sessions.move_file(src, dst)
    assert dst.read_bytes() == DATA + "é\r\n".encode("utf-8") and not src.exists()
