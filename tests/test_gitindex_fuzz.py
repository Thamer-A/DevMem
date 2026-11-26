"""The index reader is at least as strict as git (the reviewer's fuzz_idx.py, kept as a test with fixed seeds): real
index files written by git (versions 2, 3 and 4, SHA-1 and SHA-256, with the extensions git writes), mutated. For every
mutated file the reader either declines (IndexUnknown) or lists exactly the entries `git ls-files -s` lists for it;
"git refuses the file and the reader answers" and "git lists an entry the reader does not" never happen. On the code
before this test, 3,000 cases had about 113 files git refuses accepted, and one version-4 file that both read differently
(git takes a version-4 name's length from the entry's flags)."""
from __future__ import annotations

import hashlib
import os
import random
import shutil
import struct
import subprocess
import time

import pytest

from devmem import gitindex

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
GENV = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
BASES = [("v2", {}), ("v3", {"extras": ("skipworktree",)}), ("v4", {"version": 4}),
         ("v4sw", {"version": 4, "extras": ("skipworktree",)}), ("v2tree", {"extras": ("tree", "untracked")}),
         ("v2eoie", {"extras": ("eoie",)}), ("v2conf", {"extras": ("conflict",)}),
         ("v2reuc", {"extras": ("conflict", "reuc")}), ("v4conf", {"version": 4, "extras": ("conflict",)}),
         ("s256v2", {"sha256": True}), ("s256v4", {"sha256": True, "version": 4, "extras": ("skipworktree",)}),
         ("split", {"extras": ("split",)}), ("sparse", {"extras": ("sparse",)}),
         ("skiphash", {"extras": ("skiphash",)}), ("s256skip", {"sha256": True, "extras": ("skiphash",)})]
SEEDS = [1, 2, 3]
CASES = 250


def _g(repo, *a, check=True, env=None, inp=None):
    r = subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", *a], capture_output=True,
                       env=env or GENV, input=inp)
    if check and r.returncode:
        raise RuntimeError(f"{a}: {r.stderr.decode()}")
    return r


def _make(W, name, version=2, sha256=False, extras=()):
    repo = W / name
    repo.mkdir()
    _g(repo, "init", "-q", *(["--object-format=sha256"] if sha256 else []))
    _g(repo, "config", "index.version", str(version))
    files = ["a.py", "src/b.py", "src/deep/c.txt", ".devmemory/sessions/r1.md", ".DevMemory/x.json", "DEVMEM~1/y",
             "d~2/z", "ünï/cöde.py", "sp ace/f g.txt"]
    for f in files:
        fp = repo / f
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(f"{f}\n")
    _g(repo, "add", "-A")
    blob = _g(repo, "rev-parse", ":a.py").stdout.decode().strip()
    for odd in ("x" * 5000 + "/long.txt", "q/" + "y" * 4094, ".devmemory/sessions/" + "z" * 4090 + ".md"):
        _g(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{odd}")
    _g(repo, "commit", "-qm", "c")
    if "skipworktree" in extras:
        _g(repo, "update-index", "--skip-worktree", "src/b.py")
        (repo / "new.py").write_text("n\n")
        _g(repo, "add", "-N", "new.py")
    if "tree" in extras:
        _g(repo, "read-tree", "HEAD")
    if "untracked" in extras:
        _g(repo, "config", "core.untrackedCache", "true")
        (repo / "u.txt").write_text("u")
        _g(repo, "update-index", "--untracked-cache")
        _g(repo, "status")
    if "eoie" in extras:
        _g(repo, "config", "index.recordEndOfIndexEntries", "true")
        _g(repo, "config", "index.recordOffsetTable", "true")
        _g(repo, "config", "index.threads", "2")
        _g(repo, "update-index", "--force-write-index" if False else "--really-refresh", check=False)
        (repo / "a.py").write_text("changed\n")
        _g(repo, "add", "a.py")
    if "conflict" in extras:
        _g(repo, "checkout", "-qb", "o")
        (repo / "a.py").write_text("o\n")
        _g(repo, "commit", "-qam", "o")
        _g(repo, "checkout", "-q", "-")
        (repo / "a.py").write_text("m\n")
        _g(repo, "commit", "-qam", "m")
        _g(repo, "merge", "o", check=False)
    if "reuc" in extras:
        (repo / "a.py").write_text("resolved\n")
        _g(repo, "add", "a.py")
    if "split" in extras:
        _g(repo, "update-index", "--split-index")
    if "sparse" in extras:
        _g(repo, "sparse-checkout", "init", "--cone", "--sparse-index")
        _g(repo, "sparse-checkout", "set", "src")
    if "skiphash" in extras:
        _g(repo, "config", "index.skipHash", "true")
        (repo / "a.py").write_text("sh\n")
        _g(repo, "add", "a.py")
    return repo


def _git_view(repo, idx_path):
    r = subprocess.run(["git", "-C", str(repo), "ls-files", "-s", "-z"], capture_output=True,
                       env=dict(GENV, GIT_INDEX_FILE=str(idx_path)))
    if r.returncode:
        return None
    out = set()
    for rec in r.stdout.split(b"\0"):
        if not rec:
            continue
        head, _, path = rec.partition(b"\t")
        out.add((head.split()[1].decode(), path))
    return out


def _git_view_without_untr(repo, idx_path, data: bytes, hlen: int):
    """git's view of the entries of an index git itself crashed or failed on, when the only thing in the way is its
    untracked cache: the same file with the UNTR extension taken out (and the checksum made again). The reader skips
    UNTR (it changes no entry), so its answer is to equal git's view of the entries; a fuzzed UNTR can make git crash
    (SIGSEGV) after it has read them. None when there is no UNTR or git refuses the file without it too."""
    try:
        _got, pos, _o, _f = gitindex._entries(bytearray(data), hlen, time.monotonic() + 30, None, 10 ** 9)
    except (gitindex.IndexUnknown, struct.error, IndexError, ValueError):
        return None
    end, out, had = len(data) - hlen, bytearray(data[:pos]), False
    while pos + 8 <= end:
        size = struct.unpack_from(">I", data, pos + 4)[0]
        if data[pos:pos + 4] == b"UNTR":
            had = True
        else:
            out += data[pos:pos + 8 + size]
        pos += 8 + size
    if not had or pos != end:
        return None
    stripped = idx_path.with_name(idx_path.name + ".nountr")
    stripped.write_bytes(_rehash(bytes(out) + bytes(hlen), hlen))
    return _git_view(repo, stripped)


def _boundaries(buf, version, hlen):
    """Offsets of the header, each entry and each extension (from a clean parse)."""
    out = [0, 4, 8, 12]
    pos = 12
    count = struct.unpack_from(">I", buf, 8)[0]
    try:
        for _ in range(count):
            out.append(pos)
            flags = struct.unpack_from(">H", buf, pos + 40 + hlen)[0]
            q = pos + 40 + hlen + 2 + (2 if flags & 0x4000 else 0)
            if version == 4:
                _s, q = gitindex._varint(buf, q)
                q = buf.index(b"\0", q) + 1
            else:
                n = flags & 0xFFF
                q = pos + ((40 + hlen + 2 + (2 if flags & 0x4000 else 0) + n) + 8) // 8 * 8
            pos = q
        while pos < len(buf) - hlen:
            out.append(pos)
            out.append(pos + 4)
            pos += 8 + struct.unpack_from(">I", buf, pos + 4)[0]
        out.append(pos)
    except Exception:  # noqa: BLE001
        pass
    return sorted(set(o for o in out if 0 <= o <= len(buf)))


def _rehash(buf, hlen):
    if len(buf) < hlen + 12:
        return buf
    h = hashlib.sha1 if hlen == 20 else hashlib.sha256
    return buf[:-hlen] + h(buf[:-hlen]).digest()


def _mutate(rng, buf, hlen, bnds):
    b = bytearray(buf)
    k = rng.randrange(14)
    fix = rng.random() < 0.7
    if k == 0:                                    # truncate at a structure boundary (+-2)
        cut = max(0, rng.choice(bnds) + rng.randint(-2, 2))
        return bytes(b[:cut]), "trunc-bound", False
    if k == 1:
        return bytes(b[:rng.randrange(len(b))]), "trunc-rand", False
    if k == 2:                                    # byte flips
        for _ in range(rng.randint(1, 8)):
            i = rng.randrange(len(b))
            b[i] = rng.randrange(256)
        how = "flip"
    elif k == 3:                                  # entry count
        struct.pack_into(">I", b, 8, rng.choice([0, 1, 2, 9, 11, 12, 0xFFFF, 0x7FFFFFFF, 0xFFFFFFFF]))
        how = "count"
    elif k == 4:                                  # version / signature
        if rng.random() < 0.5:
            struct.pack_into(">I", b, 4, rng.choice([0, 1, 2, 3, 4, 5, 0xFFFFFFFF]))
        else:
            b[0:4] = rng.choice([b"DIRX", b"dirc", b"\0\0\0\0"])
        how = "version"
    elif k == 5:                                  # flags of an entry (name length, extended, stage)
        e = rng.choice(bnds[4:] or [12])
        if e + 40 + hlen + 2 <= len(b):
            struct.pack_into(">H", b, e + 40 + hlen, rng.choice([0, 1, 0xFFF, 0x0FFE, 0x4000 | 5, 0x8FFF, 0x3FFF,
                                                                  rng.randrange(65536)]))
        how = "flags"
    elif k == 6:                                  # a varint / first name byte becomes large
        e = rng.choice(bnds[4:] or [12])
        q = e + 40 + hlen + 2
        if q + 6 <= len(b):
            b[q:q + 5] = bytes(rng.choice([[0xFF, 0xFF, 0xFF, 0xFF, 0x7F], [0x80, 0x80, 0x80, 0x80, 0x00],
                                           [0x7F, 0, 0, 0, 0], [0x81, 0x00, 0, 0, 0], [0xC0, 0x01, 0, 0, 0]]))
        how = "varint"
    elif k == 7:                                  # extension with a bad size / unknown name
        tail = len(b) - hlen
        ext = rng.choice([b"TREE", b"REUC", b"link", b"sdir", b"abcd", b"\0\0\0\0", b"ZZZZ", b"UNTR"])
        size = rng.choice([0, 1, 7, 0xFFFFFFFF, 0x7FFFFFFF, 100])
        b[tail:tail] = ext + struct.pack(">I", size) + bytes(min(size, 16))
        how = "ext"
    elif k == 8:                                  # duplicate a span (entries) inside the entries
        i, j = sorted(rng.sample(bnds[4:] or [12, 13], 2)) if len(bnds) > 5 else (12, 12)
        b[j:j] = b[i:j]
        how = "dup"
    elif k == 9:                                  # delete a span
        i, j = sorted(rng.sample(bnds[4:] or [12, 13], 2)) if len(bnds) > 5 else (12, 12)
        del b[i:j]
        how = "del"
    elif k == 10:                                 # zero trailer (skipHash)
        b[-hlen:] = bytes(hlen)
        fix = False
        how = "zerotrail"
    elif k == 11:                                 # non-UTF-8 / NUL in a name
        e = rng.choice(bnds[4:] or [12])
        q = e + 40 + hlen + 2 + rng.randint(0, 6)
        if q < len(b) - hlen:
            b[q] = rng.choice([0x00, 0xFF, 0xC3, 0x2F, 0x5C, 0x7E, 0x80])
        how = "namebyte"
    elif k == 12:                                 # mode -> directory / gitlink / symlink
        e = rng.choice(bnds[4:] or [12])
        if e + 28 <= len(b):
            struct.pack_into(">I", b, e + 24, rng.choice([0o040000, 0o160000, 0o120000, 0o100755, 0, 0xFFFFFFFF]))
        how = "mode"
    else:                                         # append garbage, or grow to near the cap
        b += bytes(rng.randrange(256) for _ in range(rng.randint(1, 64)))
        how = "append"
    return bytes(_rehash(bytes(b), hlen) if fix else bytes(b)), how + ("+fix" if fix else ""), fix


@pytest.fixture(scope="module")
def bases(tmp_path_factory):
    W = tmp_path_factory.mktemp("fuzzbases")
    out = []
    for nm, kw in BASES:
        try:
            repo = _make(W, nm, **kw)
        except Exception:  # noqa: BLE001 - a feature this git does not have (sparse index, SHA-256)
            continue
        out.append((nm, repo, (repo / ".git" / "index").read_bytes(), 32 if kw.get("sha256") else 20))
    assert len(out) >= 8
    return out


def test_every_unmutated_base_the_reader_reads_equals_git(bases, tmp_path):
    p = tmp_path / "case.idx"
    read = 0
    for nm, repo, buf, _hlen in bases:
        p.write_bytes(buf)
        try:
            mine = set(gitindex.read_index(p, time.monotonic() + 30))
        except gitindex.IndexUnknown:
            assert nm in ("split", "sparse"), nm      # split and sparse indexes: declined (an untracked cache is skipped)
            continue
        read += 1
        assert mine == _git_view(repo, p), nm
    assert read >= 8


@pytest.mark.parametrize("seed", SEEDS)
def test_a_mutated_index_is_read_as_git_reads_it_or_declined(bases, tmp_path, seed):
    rng = random.Random(seed)
    p = tmp_path / "case.idx"
    stats = {"declined": 0, "same": 0}
    for _ in range(CASES):
        nm, repo, buf, hlen = rng.choice(bases)
        version = struct.unpack_from(">I", buf, 4)[0]
        data, how, _fixed = _mutate(rng, buf, hlen, _boundaries(buf, version, hlen))
        p.write_bytes(data)
        try:
            got = gitindex.read_index(p, time.monotonic() + 30)
        except gitindex.IndexUnknown:
            stats["declined"] += 1
            continue
        if data == buf:
            continue
        gv = _git_view(repo, p)
        if gv is None:
            gv = _git_view_without_untr(repo, p, data, hlen)
        assert gv is not None, f"seed {seed}: the reader answers for a file git refuses ({nm}, {how})"
        assert set(got) == gv, f"seed {seed}: the reader and git list different entries ({nm}, {how})"
        stats["same"] += 1
    print(f"seed {seed}: {stats}")
