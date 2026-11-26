"""The index reader's memory and work are bounded by the size of the file it reads, whatever the file holds, and a
process that ends inside the question does not make every later event ask again (the reviewer's q4 / q4e2e: a
version-4 index whose entries never strip their prefix made path i i bytes long, about n*n/2 bytes in all; 40,000
entries in a 2.6 MB file took 797 MB, and a shipped 9.8 MB index got every hook and the MCP server killed at 2 GB)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import consent, gitindex, store

HDR = struct.pack(">IIIIIIIIII", 0, 0, 0, 0, 0, 0, 0o100644, 0, 0, 0) + b"\x11" * 20


def _varint(value: int) -> bytes:
    buf = [value & 127]
    value >>= 7
    while value:
        value -= 1
        buf.append(128 | (value & 127))
        value >>= 7
    return bytes(reversed(buf))


def _v4(entries: list[tuple[int, bytes, int]]) -> bytes:
    """A version-4 index of (strip, suffix, name length in the flags) entries, with a valid checksum."""
    body = b"DIRC" + struct.pack(">II", 4, len(entries))
    body += b"".join(HDR + struct.pack(">H", min(n, 0xFFF)) + _varint(s) + suf + b"\0" for s, suf, n in entries)
    return body + hashlib.sha1(body).digest()


def growing(n: int) -> bytes:
    """The reviewer's shape: strip 0, one byte more each time (path i is i+1 bytes; all kept: they hold `~`)."""
    return _v4([(0, b"~", 1)] + [(0, b"a", i + 1) for i in range(1, n)])


def long_prefix(n: int) -> bytes:
    """Paths of about 4,000 bytes that differ in their last bytes: every entry strips 4 bytes and adds 4."""
    head = b"~" + b"p" * 3990
    out = [(0, head + b"0000", len(head) + 4)]
    for i in range(1, n):
        out.append((4, b"%04d" % (i % 10000), len(head) + 4))
    return _v4(out)


def _peak(path: Path, keep=lambda p: b"~" in p, cap: int = 250_000) -> tuple[int, str]:
    tracemalloc.start()
    try:
        try:
            got = gitindex.read_index(path, time.monotonic() + 60, keep=keep, cap=cap)
            what = f"returned {len(got)}"
            del got
        except gitindex.IndexUnknown as e:
            what = f"{type(e).__name__}: {e}"
        return tracemalloc.get_traced_memory()[1], what
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("shape", [growing, long_prefix])
@pytest.mark.parametrize("n", [2_000, 8_000, 32_000])
def test_the_readers_peak_is_a_small_multiple_of_the_file(tmp_path, shape, n):
    idx = tmp_path / "index"
    data = shape(n)
    idx.write_bytes(data)
    peak, what = _peak(idx)
    # the file once, kept paths (at most KEPT_FACTOR times it plus KEPT_SLACK) and one path at a time
    bound = (1 + gitindex.KEPT_FACTOR + 2) * len(data) + gitindex.KEPT_SLACK + 2 * gitindex.MAX_PATH_BYTES + 512 * 1024
    assert peak <= bound, (n, len(data), peak, what)
    assert "IndexLimit" in what or what.startswith("returned"), what


def test_the_kept_paths_and_all_paths_are_capped(tmp_path, monkeypatch):
    idx = tmp_path / "index"
    idx.write_bytes(growing(3_000))
    monkeypatch.setattr(gitindex, "KEPT_SLACK", 0)
    with pytest.raises(gitindex.IndexLimit, match="keeps"):
        gitindex.read_index(idx, time.monotonic() + 60, keep=lambda p: b"~" in p)
    monkeypatch.setattr(gitindex, "PATHS_SLACK", 0)
    monkeypatch.setattr(gitindex, "HONEST_PATH", 0)       # these paths are short enough for an honest index
    with pytest.raises(gitindex.IndexLimit, match="reads"):                 # nothing kept: all paths still count
        gitindex.read_index(idx, time.monotonic() + 60, keep=lambda p: False)
    monkeypatch.setattr(gitindex, "MAX_PATH_BYTES", 100)
    with pytest.raises(gitindex.IndexLimit, match="longer than 100"):
        gitindex.read_index(idx, time.monotonic() + 60, keep=lambda p: False)


def test_the_deadline_is_checked_by_bytes_as_well_as_by_entries(tmp_path, monkeypatch):
    idx = tmp_path / "index"
    idx.write_bytes(long_prefix(600))              # fewer than 4,096 entries, about 2.4 MB of paths
    calls = []
    real = time.monotonic

    def clock():
        calls.append(1)
        return real()
    monkeypatch.setattr(gitindex.time, "monotonic", clock)
    with pytest.raises(gitindex.IndexUnknown):
        gitindex.read_index(idx, real() + 60, keep=lambda p: b"~" in p)
    assert len(calls) >= 4, calls                   # every WORK_STEP bytes, not only every 4,096 entries


def _shipped_with_filters(tmp_path: Path, index: bytes) -> tuple[Path, Path]:
    """The reviewer's q4e2e: a project whose `.git` came in an archive, with more filter drivers than devmem switches
    off (git is not run there: the reader answers), and a hand-made index."""
    project = make_project(tmp_path)
    st = project / ".devmemory"
    with open(project / ".git" / "config", "a", encoding="utf-8") as f:
        for i in range(store._MAX_FILTERS + 1):
            f.write(f'[filter "f{i}"]\n\tclean = cat\n')
    (project / ".git" / "index").write_bytes(index)
    return project, st


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_shipped_hostile_index_is_a_limit_in_bounded_memory(tmp_path, monkeypatch):
    project, st = _shipped_with_filters(tmp_path, growing(6_000))
    monkeypatch.setattr(store, "MAX_STORE_CANDIDATES", 1_000)      # a low cap, as the real one is for 250,000
    monkeypatch.setattr(store, "_tilde_rule", lambda *a: None)     # where 8.3 names cannot be ruled out
    store._FILTERS.clear()
    consent.forget_decisions()
    tracemalloc.start()
    try:
        why = consent.store_refusal(project, st)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert why and "more than 1000 entries" in why, why
    assert peak < 8 * 1024 * 1024, peak
    assert consent.is_limit_failure(st)


_KILLED = """
import os, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from devmem import consent, store
def die(*a, **k):
    os._exit(9)          # as the kernel's out-of-memory killer would end it: nothing after this runs
store.store_index_entries = die
consent._tracked(Path(sys.argv[2]), Path(sys.argv[3]), cached=True)
"""


def _kill_while_asking(project, st) -> None:
    env = {**os.environ, "DEVMEM_USER_DATA": os.environ["DEVMEM_USER_DATA"]}
    r = subprocess.run([sys.executable, "-c", _KILLED, str(Path(store.__file__).parent.parent), str(project), str(st)],
                       env=env, capture_output=True, timeout=120)
    assert r.returncode == 9, r.stderr
    mark = json.loads(consent._checked_path(st).read_text(encoding="utf-8"))
    assert mark["asking"]["stamp"] == consent._git_index_stamp(project)


def _age_kept_failure(st, seconds: float) -> None:
    path = consent._checked_path(st)
    kept = json.loads(path.read_text(encoding="utf-8"))
    kept["failed"]["at"] -= seconds
    path.write_text(json.dumps(kept), encoding="utf-8")


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_question_that_ended_its_process_is_asked_again_after_a_minute_by_the_hooks(tmp_path, monkeypatch):
    """A hook killed while asking (Claude Code's time limit) is not a limit of devmem: the hooks wait a minute, and an
    hour only when it happens again within half an hour; the reason says so, and nothing about adopting."""
    project = make_project(tmp_path)
    st = project / ".devmemory"
    _kill_while_asking(project, st)
    real = store.store_index_entries

    def asked(*a, **k):
        raise AssertionError("a hook asked again within the hold")
    monkeypatch.setattr(store, "store_index_entries", asked)
    for _ in range(2):
        got, why = consent._tracked(project, st, cached=True)
        assert got is None and why == consent.DIED
        assert not consent.is_limit_failure(st)
    kept = json.loads(consent._checked_path(st).read_text(encoding="utf-8"))
    assert "asking" not in kept and kept["failed"]["died"] == 1 and "limit" not in kept["failed"]
    consent.forget_decisions()
    why = consent.store_refusal(project, st, check_git="cached")
    assert consent.is_git_unknown(why) and not consent.is_git_limit(why), why
    assert "asks again in a minute" in why and "adopt" not in why and "report" not in why, why
    notice = consent.paused_notice(project, st, why, recording=True)
    assert "It asks again in a minute" in notice and "does not clear by itself" not in notice, notice
    assert "adopt" not in notice and "report" not in notice, notice

    monkeypatch.setattr(store, "store_index_entries", real)
    _age_kept_failure(st, consent.DIED_RETRY_SECONDS + 1)        # a minute later the next hook asks: killed again
    _kill_while_asking(project, st)
    monkeypatch.setattr(store, "store_index_entries", asked)
    got, why = consent._tracked(project, st, cached=True)
    assert got is None and why == consent.DIED_AGAIN
    kept = json.loads(consent._checked_path(st).read_text(encoding="utf-8"))
    assert kept["failed"]["died"] == 2
    _age_kept_failure(st, consent.DIED_RETRY_SECONDS + 1)        # now held for an hour
    assert consent._tracked(project, st, cached=True) == (None, consent.DIED_AGAIN)
    consent.forget_decisions()
    why = consent.store_refusal(project, st, check_git="cached")
    assert "in an hour" in why and "report" in why, why

    monkeypatch.setattr(store, "store_index_entries", real)
    (project / "new.txt").write_text("n\n", encoding="utf-8")       # the index changes: asked again
    subprocess.run(["git", "-C", str(project), "add", "new.txt"], check=True, capture_output=True)
    assert consent._tracked(project, st, cached=True) == ([], "")


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_command_asks_again_at_once_after_a_question_that_ended_its_process(tmp_path, monkeypatch):
    """`devmem status` or `doctor` has no hook time limit to protect: it asks, and its answer replaces the mark (the
    hooks then reuse that answer)."""
    project = make_project(tmp_path)
    st = project / ".devmemory"
    _kill_while_asking(project, st)
    calls = {"n": 0}
    real = store.store_index_entries

    def counted(*a, **k):
        calls["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(store, "store_index_entries", counted)
    assert consent._tracked(project, st, cached=False) == ([], "")
    assert calls["n"] == 1
    kept = json.loads(consent._checked_path(st).read_text(encoding="utf-8"))
    assert "asking" not in kept and "failed" not in kept and kept["ok"]["entries"] == []
    assert consent._tracked(project, st, cached=True) == ([], "") and calls["n"] == 1     # the hooks reuse it


def test_an_answer_replaces_the_asking_mark(tmp_path):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    assert consent._tracked(project, st, cached=False) == ([], "")
    assert "asking" not in json.loads(consent._checked_path(st).read_text(encoding="utf-8"))


def test_gits_output_is_capped_while_it_is_read(tmp_path, monkeypatch):
    """The git route: `ls-files` output past MAX_CANDIDATE_OUTPUT stops git (a limit), and is never held whole."""
    project = make_project(tmp_path)
    st = project / ".devmemory"
    g = ["git", "-C", str(project), "-c", "core.protectNTFS=false"]
    blob = subprocess.run([*g, "hash-object", "-w", "--stdin"], input=b"x\n", capture_output=True,
                          check=True).stdout.decode().strip()
    names = "".join(f"100644 {blob}\td~{i}/{'x' * 100}\n" for i in range(3_000))
    subprocess.run([*g, "update-index", "--index-info"], input=names.encode(), check=True, capture_output=True)
    monkeypatch.setattr(store, "MAX_CANDIDATE_OUTPUT", 64 * 1024)
    monkeypatch.setattr(store, "_tilde_rule", lambda *a: None)     # where 8.3 names cannot be ruled out
    store._FILTERS.clear()
    with pytest.raises(store.GitLimit, match="longer than"):
        store.store_index_entries(project, st)
    monkeypatch.setattr(store, "MAX_CANDIDATE_OUTPUT", 64 * 1024 * 1024)
    assert store.store_index_entries(project, st) == []


def test_an_honest_version_4_index_of_long_shared_paths_is_read(tmp_path):
    """Item 8: an honest version-4 index whose paths are much longer than its entries (a deep tree: 900-byte paths
    that share their folders, about 70 bytes per entry) passed PATHS_FACTOR times the file's size; HONEST_PATH bytes
    per entry are allowed whatever the file's size."""
    head = b"/".join(b"module_%02d_" % k + b"x" * 40 for k in range(17))
    out = [(0, head + b"/f000000.java", len(head) + 13)]
    for i in range(1, 20_000):
        out.append((11, b"%06d.java" % i, len(head) + 13))
    idx = tmp_path / "index"
    idx.write_bytes(_v4(out))
    assert sum(len(head) + 13 for _ in out) > gitindex.PATHS_FACTOR * idx.stat().st_size + gitindex.PATHS_SLACK
    assert gitindex.read_index(idx, time.monotonic() + 60, keep=lambda p: False) == []


def test_an_index_of_more_entries_than_can_be_read_in_time_is_a_limit_at_once(tmp_path, monkeypatch):
    """The reviewer's `many` shape (1.66 million short entries): once `~` entries are no longer kept where they cannot
    be the store, the reader went through all of them and stopped at its 5-second deadline on every question. The
    header's entry count decides it first; a count the file cannot hold is a malformed index, not a limit."""
    idx = tmp_path / "index"
    idx.write_bytes(growing(3_000))
    monkeypatch.setattr(gitindex, "MAX_INDEX_ENTRIES", 2_999)
    calls = []
    real = gitindex._varint
    monkeypatch.setattr(gitindex, "_varint", lambda *a: calls.append(1) or real(*a))
    with pytest.raises(gitindex.IndexLimit, match="more than 2,999 entries"):
        gitindex.read_index(idx, time.monotonic() + 60, keep=lambda p: False)
    assert calls == []                                  # decided from the header
    data = bytearray(growing(10))
    struct.pack_into(">I", data, 8, 10 ** 6)            # a count far beyond what the file holds
    idx.write_bytes(bytes(data))
    with pytest.raises(gitindex.IndexUnknown) as e:
        gitindex.read_index(idx, time.monotonic() + 60, keep=lambda p: False)
    assert not isinstance(e.value, gitindex.IndexLimit)
