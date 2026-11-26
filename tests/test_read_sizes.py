"""Reading a small store file or hook payload never asks for the whole cap at once: BufferedReader.read(n) allocates n
bytes up front, and read(64 MB + 1) for every config.json, state file and record cost about 20 ms each on Windows (a
5,000-record store took 100 s to load)."""
from __future__ import annotations

import io
import os

from devmem import hooks, store


class _Spy(io.BufferedReader):
    sizes: list[int] = []

    def read(self, n=-1):
        _Spy.sizes.append(n)
        return super().read(n)


def test_read_regular_asks_for_the_file_size_not_the_cap(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    p.write_bytes(b'{"a": 1}')
    real = os.fdopen
    monkeypatch.setattr(store.os, "fdopen", lambda fd, mode="r", *a, **k: _Spy(real(fd, "rb", buffering=0)))
    _Spy.sizes = []
    assert store.read_regular(p, store.STORE_FILE_CAP) == b'{"a": 1}'
    assert _Spy.sizes and max(_Spy.sizes) <= 8 + 1


def test_read_regular_still_reads_a_file_that_grew_and_refuses_past_the_limit(tmp_path, monkeypatch):
    p = tmp_path / "grows.txt"
    p.write_bytes(b"x" * 10)
    real_open = store._open_regular

    def opened_then_grown(path, max_bytes, follow=False):
        got = real_open(path, max_bytes, follow)
        with open(p, "ab") as f:
            f.write(b"y" * 3_000_000)          # grows after the size was taken
        return got
    monkeypatch.setattr(store, "_open_regular", opened_then_grown)
    assert store.read_regular(p, 4_000_000) == b"x" * 10 + b"y" * 3_000_000
    assert store.read_regular(p, 4_000_000) is None       # now 6 MB: past the limit


def test_hook_stdin_is_read_in_bounded_steps():
    _Spy.sizes = []
    raw = io.BytesIO(b"{}" + b" " * 3_000_000)
    assert hooks.read_stdin(_Spy(raw), limit=hooks.MAX_STDIN_BYTES).startswith(b"{}")
    assert max(_Spy.sizes) <= 1 << 20
    _Spy.sizes = []
    data = hooks.read_stdin(_Spy(io.BytesIO(b"z" * 5000)), limit=100)
    assert data == b"z" * 101 and max(_Spy.sizes) <= 1 << 20      # cut past the limit (then rejected as JSON)


def test_the_session_lock_reads_its_timeout_when_it_is_taken(tmp_path, monkeypatch):
    """hooks._Lock is devmem.store.OSLock (which devmem.refresh imports without the hooks); the hooks' LOCK_TIMEOUT_S is
    read when the lock is taken, not frozen as a default argument."""
    import time
    lock = tmp_path / "x.lock"
    monkeypatch.setattr(hooks, "LOCK_TIMEOUT_S", 0.2)
    with hooks._Lock(lock) as a:
        assert a.held
        t = time.perf_counter()
        with hooks._Lock(lock) as b:
            assert not b.held
        assert time.perf_counter() - t < 2.0       # not the 3 s default
    with store.OSLock(lock, timeout=0) as c:
        assert c.held
