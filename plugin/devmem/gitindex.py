"""Which paths git's index holds, read from the index file itself, for the question "which files of the store does
git track" (devmem.store.store_index_entries) when the git program cannot be run: it is not on the PATH devmem's
hooks or MCP server see (GitHub Desktop's or Visual Studio's own git, a git inside a conda, scoop or nix folder), devmem
does not run git in this project (devmem.store.git_argv), or git failed or did not answer in time.

Bounded and conservative: index versions 2, 3 and 4 (prefix-compressed paths), SHA-1 or SHA-256 object ids, every
stage; the file's checksum is verified as git verifies it. Anything this reader does not handle with certainty is
IndexUnknown ("cannot say"), never "nothing tracked": a split index (`link` extension), a sparse index (`sdir`, or a
directory entry), an unknown required extension or version, a file over MAX_INDEX_BYTES, a deadline that passes, a
git location in the environment, a configuration that names another work tree than the folder that holds `.git`
(core.worktree, for submodules only when it names that folder), an include, a bare repository, or a folder on the way
up that git would take for a repository itself. Paths are given as `git ls-files` prints them, relative to the folder
the question is asked in (`../` above it), so that the answer is the same as git's.
"""
from __future__ import annotations

import io
import os
import re
from array import array
import stat
import struct
import time
from pathlib import Path

MAX_INDEX_BYTES = 128 * 1024 * 1024      # a larger index is not read
MAX_INDEX_ENTRIES = 1_000_000            # nor one of more entries (reading them all takes longer than the question's 5 s)
# Whatever the file holds, the reader's memory and time stay within a small multiple of its size (bounded above):
MAX_PATH_BYTES = 32 * 1024     # a longer path: cannot say (git itself has no such limit; Linux's PATH_MAX is 4,096
                               # bytes, Windows' long paths 32,767 characters)
PATHS_FACTOR = 8               # all paths together (version 4 builds each from the one before) at most this many
PATHS_SLACK = 1024 * 1024      # times the file's size, plus this, or HONEST_PATH bytes per entry read so far,
HONEST_PATH = 4096             # whichever is more: an honest version-4 index of a deep tree (900-byte paths, a 70-byte
                               # entry each) passes; one of 32 KiB paths that share all but their last bytes does not
KEPT_FACTOR = 2                # the paths kept (the candidates) at most this many times the file's size, plus
KEPT_SLACK = 1024 * 1024       # this; and at most `cap` of them
WORK_STEP = 256 * 1024         # the deadline is checked after every this many bytes of paths (and 4,096 entries)
_CONFIG_CAP = 1024 * 1024
_ENV_PLACES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")
_OPTIONAL_EXTENSIONS_OK = True           # an extension whose name starts with A-Z is optional (git skips it too)


class IndexUnknown(OSError):
    """The index cannot be read with certainty; the text says why."""


class IndexLimit(IndexUnknown):
    """The index holds more than this reader looks through (a limit, not a damaged file): it does not pass by itself."""


def _read(path, cap: int) -> bytes:
    """The file's bytes, at most `cap` of them (more: IndexUnknown)."""
    return bytes(_read_buf(path, cap))


def _read_buf(path, cap: int) -> bytearray:
    """_read as a bytearray read in place, so that the file is never held twice."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise IndexUnknown(f"{path} is not a regular file")
        if st.st_size > cap:
            raise IndexUnknown(f"{path} is larger than {cap // (1024 * 1024)} MB")
        buf = bytearray(st.st_size)
        got = 0
        with memoryview(buf) as mv, io.FileIO(fd, "rb", closefd=False) as f:
            while got < len(buf):
                n = f.readinto(mv[got:got + 8 * 1024 * 1024])
                if not n:
                    break
                got += n
        del buf[got:]
        return buf
    finally:
        os.close(fd)


def _common_dir(gitdir: Path) -> Path:
    cd = gitdir / "commondir"
    if not os.path.lexists(cd):
        return gitdir
    c = Path(_read(cd, 4096).decode("utf-8", "replace").strip())
    return c if c.is_absolute() else gitdir / c


def _is_git_directory(d: Path) -> bool:
    """git's own test for a repository folder: HEAD, and objects/ and refs/ (in its commondir, if it has one)."""
    try:
        common = _common_dir(d)
        return os.path.isfile(d / "HEAD") and os.path.isdir(common / "objects") and os.path.isdir(common / "refs")
    except (OSError, ValueError):
        return False


def _config_problem(text: bytes, gitdir: Path, top: Path) -> str | None:
    """Why a configuration file makes the work tree uncertain, or None. Tolerant: anything unusual is a problem."""
    section = b""
    for raw in text.splitlines():
        line = raw.split(b"#", 1)[0].split(b";", 1)[0].strip()
        if not line:
            continue
        if line.startswith(b"["):
            section = line[1:].split(b"]", 1)[0].strip().lower()
            if section.startswith(b"include"):
                return "git's configuration includes other files"
            rest = line.split(b"]", 1)[1].strip() if b"]" in line else b""
            if not rest:
                continue
            line = rest
        key, eq, value = line.partition(b"=")
        key, value = key.strip().lower(), value.strip().strip(b'"')
        if section.startswith(b"core") and key == b"bare" and (not eq or value.lower() not in
                                                               (b"false", b"no", b"off", b"0")):
            return "git's configuration says the repository is bare"
        if section.startswith(b"core") and key == b"worktree":
            try:
                wt = Path(os.fsdecode(value))
                wt = wt if wt.is_absolute() else gitdir / wt
                if os.path.normcase(os.path.realpath(wt)) == os.path.normcase(os.path.realpath(top)):
                    continue                    # a submodule's gitdir names its own work tree
            except (OSError, ValueError):
                pass
            return "git's configuration names another work tree (core.worktree)"
        if section.startswith(b"extensions") and key == b"worktreeconfig" and value.lower() in (b"true", b"yes", b"on",
                                                                                               b"1", b""):
            continue                            # config.worktree is read below
    return None


def locate(where) -> tuple[Path, Path]:
    """(top of the work tree, index file) for the folder `where`, found the way git finds them (a `.git` folder or
    `gitdir:` file in `where` or above it, a linked worktree's commondir). Raises IndexUnknown when the layout is not
    certain (see the module docstring), FileNotFoundError when there is no repository."""
    if any(os.environ.get(k) for k in _ENV_PLACES):
        raise IndexUnknown("a git location is set in the environment")
    start = Path(os.path.realpath(where) if os.name != "nt" else os.path.abspath(where))
    for d in (start, *start.parents):
        g = d / ".git"
        try:
            st = os.lstat(g)
        except FileNotFoundError:
            if _is_git_directory(d):
                raise IndexUnknown(f"{d} is laid out as a git repository itself") from None
            continue
        except (OSError, ValueError) as e:
            raise IndexUnknown(f"{g} cannot be read ({e})") from None
        if stat.S_ISDIR(st.st_mode):
            gitdir = g
        elif stat.S_ISREG(st.st_mode):
            line = _read(g, 4096).decode("utf-8", "replace").strip()
            if not line.startswith("gitdir:"):
                raise IndexUnknown(f"{g} is not a gitdir file")
            gd = Path(line[len("gitdir:"):].strip())
            gitdir = gd if gd.is_absolute() else d / gd
        else:
            raise IndexUnknown(f"{g} is neither a folder nor a file")
        if not _is_git_directory(gitdir):
            raise IndexUnknown(f"{gitdir} is not a complete git repository")
        common = _common_dir(gitdir)
        for cfg in {common / "config", gitdir / "config.worktree", common / "config.worktree"}:
            try:
                text = _read(cfg, _CONFIG_CAP)
            except FileNotFoundError:
                continue
            problem = _config_problem(text, gitdir, d)
            if problem:
                raise IndexUnknown(problem)
        return d, gitdir / "index"
    raise FileNotFoundError("not in a git work tree")


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    """git's offset varint (varint.c)."""
    c = buf[pos]
    pos += 1
    val = c & 127
    while c & 128:
        val += 1
        if val > 1 << 32:
            raise IndexUnknown("an index entry's path prefix is malformed")
        c = buf[pos]
        pos += 1
        val = (val << 7) + (c & 127)
    return val, pos


_MODES = frozenset((0o100644, 0o100755, 0o120000, 0o160000))     # what git writes (create_ce_mode); 040000: sparse
_HFS_IGNORED = frozenset("\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u206a\u206b\u206c\u206d\u206e"
                         "\u206f\ufeff")


def _bad_path(path: bytes) -> str | None:
    """Why git's verify_path would refuse `path` (its rules for every platform: core.protectNTFS and protectHFS
    included), or None. git applies them when it adds an entry, so an index git wrote never holds such a name."""
    comps = path.replace(b"\\", b"/").split(b"/") if os.name == "nt" else path.split(b"/")
    for c in comps:
        if not c:
            return "an empty component"
        if c in (b".", b".."):
            return "a . or .. component"
        low = c.lower()
        if low.startswith(b".git") and (len(low) == 4 or low[4:].strip(b" .") == b"" or low[4:5] == b":"):
            return "a .git component"
        if low.startswith(b"git~") and low[4:].isdigit():
            return "a short name of .git"
        if b"\0" in c:
            return "a NUL byte"
        if not c.isascii():
            try:
                s = c.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if "".join(ch for ch in s if ch not in _HFS_IGNORED).lower() == ".git":
                return "a .git component with ignored characters"
    return None


def _entries(buf, hlen: int, deadline: float, keep, cap: int):
    """(object id, path) of every entry for which keep(path) holds (keep None: of none), and the offset after the last
    entry. Only those paths are kept, within the limits above (IndexLimit): at most `cap` of them and KEPT_FACTOR times
    the file's size in bytes; no path longer than MAX_PATH_BYTES; all paths together at most PATHS_FACTOR times the
    file's size. The deadline is checked by entries and by bytes of paths."""
    sig, version, count = struct.unpack_from(">4sII", buf, 0)
    if sig != b"DIRC" or version not in (2, 3, 4):
        raise IndexUnknown("git's index has an unknown format or version")
    pos, prev, out, end = 12, b"", [], len(buf) - hlen
    fixed = 40 + hlen + 2
    if count * (fixed + 2) > end - 12:
        raise IndexUnknown("git's index ends inside an entry")      # more entries than the file can hold
    if count > MAX_INDEX_ENTRIES:
        raise IndexLimit(f"git's index holds more than {MAX_INDEX_ENTRIES:,} entries, more than devmem reads in time")
    paths_cap = PATHS_FACTOR * len(buf) + PATHS_SLACK
    kept_cap = KEPT_FACTOR * len(buf) + KEPT_SLACK
    work = kept = 0
    check_at = WORK_STEP
    prev_stage = 0
    empty = False
    offsets = array("Q")                         # where each entry starts, and whether its path is built from no
    fresh = bytearray()                         # earlier one (for the IEOT check in _extensions)
    for i in range(count):
        if not i & 4095 or work >= check_at:
            if time.monotonic() > deadline:
                raise IndexUnknown("reading git's index took too long")
            check_at = work + WORK_STEP
        start = pos
        if pos + fixed > end:
            raise IndexUnknown("git's index ends inside an entry")
        mode = struct.unpack_from(">I", buf, pos + 24)[0]
        oid = buf[pos + 40:pos + 40 + hlen]
        flags = struct.unpack_from(">H", buf, pos + 40 + hlen)[0]
        pos += fixed
        if mode not in _MODES:
            if mode & 0o170000 == 0o040000:
                raise IndexUnknown("git's index is a sparse index (it holds folders)")
            raise IndexUnknown("git's index has an entry with a mode git does not write")
        if flags & 0x4000:
            if version < 3:
                raise IndexUnknown("git's index has an extended entry in version 2")
            if pos + 2 > end or struct.unpack_from(">H", buf, pos)[0] & ~0x6000:
                raise IndexUnknown("git's index has an entry with flags git does not know")
            pos += 2
        n = flags & 0xFFF
        stage = (flags >> 12) & 3
        if version == 4:
            strip, pos = _varint(buf, pos)
            if strip > len(prev):
                raise IndexUnknown("git's index has a malformed path")
            fresh.append(strip == len(prev))
            room = MAX_PATH_BYTES - (len(prev) - strip)
            nul = buf.find(b"\0", pos, min(end, pos + room + 1))
            if nul < 0:
                if pos + room + 1 <= end:
                    raise IndexLimit(f"git's index holds a path longer than {MAX_PATH_BYTES} bytes")
                raise IndexUnknown("git's index has a malformed path")
            path = prev[:len(prev) - strip] + buf[pos:nul]
            pos = nul + 1
            if len(path) != n and not (n == 0xFFF and len(path) >= 0xFFF):
                # git takes the length from the flags (it would read another name and another next entry)
                raise IndexUnknown("git's index has a path whose length is not the one its entry states")
        else:
            fresh.append(True)
            nul = buf.find(b"\0", pos, min(end, pos + (n if n < 0xFFF else MAX_PATH_BYTES) + 1))
            if nul < 0 and n == 0xFFF and pos + MAX_PATH_BYTES + 1 <= end:
                raise IndexLimit(f"git's index holds a path longer than {MAX_PATH_BYTES} bytes")
            if nul < 0 or (n < 0xFFF and nul != pos + n):
                raise IndexUnknown("git's index has a malformed path")
            path = bytes(buf[pos:nul])
            pos = start + ((nul - start) + 8) // 8 * 8
            if pos > end:
                raise IndexUnknown("git's index ends inside an entry")
            if any(buf[nul:pos]):
                raise IndexUnknown("git's index has an entry whose padding is not zero")
        work += len(path)
        if work > paths_cap and work > HONEST_PATH * (i + 1):
            raise IndexLimit("git's index holds more bytes of paths than devmem reads for an index of its size")
        if path.endswith(b"/"):
            raise IndexUnknown("git's index is a sparse index (it holds folders)")
        if not path:                            # a split index's entry (its name is in the shared index): the
            empty = True                        # `link` extension makes it "cannot say" below, by its own reason
            offsets.append(start)
            continue
        # git's own order (check_ce_order): by name, then by stage; a name at stage 0 only once and alone
        if i and (path < prev or (path == prev and (stage <= prev_stage or stage == 0 or prev_stage == 0))):
            raise IndexUnknown("git's index has entries out of order or twice")
        bad = _bad_path(path)
        if bad:
            raise IndexUnknown(f"git's index has a path git does not accept ({bad})")
        prev, prev_stage = path, stage
        offsets.append(start)
        if keep is not None and keep(path):
            out.append((oid.hex(), path))
            kept += len(path)
            if len(out) > cap:
                raise IndexLimit(f"more than {cap} entries of git's index could be in the store")
            if kept > kept_cap:
                raise IndexLimit("the entries of git's index that could be in the store hold more bytes of paths "
                                 "than devmem keeps for an index of its size")
    if empty:
        fresh.append(2)                         # read by _extensions: an empty path without a `link` extension
    return out, pos, offsets, fresh


def _ieot(buf, pos: int, size: int, offsets, fresh) -> None:
    """git loads the entries in blocks, each in a thread of its own and starting with no earlier path, where the IEOT
    extension lists them (and EOIE is valid): its blocks must be where the entries are, and in version 4 each block's
    first path must be built from no earlier one, or git's threaded and plain reads of the file differ."""
    if size < 4 or (size - 4) % 8 or struct.unpack_from(">I", buf, pos)[0] != 1:
        return                                  # git ignores an IEOT it cannot read (and reads the file plainly)
    at = 0
    for k in range((size - 4) // 8):
        off, nr = struct.unpack_from(">II", buf, pos + 4 + 8 * k)
        if at >= len(offsets) or offsets[at] != off or not fresh[at]:
            raise IndexUnknown("git's index lists entry blocks (IEOT) that do not match its entries")
        at += nr
    if at != len(offsets):
        raise IndexUnknown("git's index lists entry blocks (IEOT) that do not match its entries")


def _fsmonitor_ok(buf, pos: int, size: int, count: int) -> bool:
    """git's read_fsmonitor_extension: version 1 or 2, an EWAH bitmap of exactly the stated size, no more bits than
    entries (a malformed one is "index file corrupt")."""
    end = pos + size
    if size < 9:
        return False
    v = struct.unpack_from(">I", buf, pos)[0]
    p = pos + 4
    if v == 1:
        p += 8
    elif v == 2:
        nul = buf.find(b"\0", p, end)
        if nul < 0:
            return False
        p = nul + 1
    else:
        return False
    if p + 12 > end:
        return False
    ewah = struct.unpack_from(">I", buf, p)[0]
    bits, words = struct.unpack_from(">II", buf, p + 4)
    return ewah == 12 + 8 * words and p + 4 + ewah <= end and bits <= count


def _tree_ok(buf, pos: int, size: int, hlen: int) -> bool:
    """The cache-tree extension (TREE) as git's read_one() reads it, within its bounds: each node a NUL-terminated
    name, "<entries> <subtrees>" and a newline, the tree's id unless entries is -1, then its subtrees. git skips a TREE
    it cannot read, but some damaged ones make it crash (a fuzzed name byte: git ls-files ended with SIGSEGV), so devmem
    accepts only one that parses to its end exactly."""
    end, pending = pos + size, 1
    while pending:
        pending -= 1
        nul = buf.find(b"\0", pos, end)
        nl = buf.find(b"\n", nul + 1, end) if nul >= 0 else -1
        if nl < 0:
            return False
        fields = bytes(buf[nul + 1:nl]).split(b" ")
        if (len(fields) != 2 or not fields[1].isdigit() or not fields[0].lstrip(b"-").isdigit()
                or fields[0].count(b"-") > fields[0].startswith(b"-")):
            return False
        entries, subtrees = int(fields[0]), int(fields[1])
        pos = nl + 1 + (hlen if entries >= 0 else 0)
        if entries < -1 or pos > end:
            return False
        pending += subtrees
        if pending * 4 > end - pos:             # every node takes at least four bytes
            return False
    return pos == end


def _extensions(buf, pos: int, hlen: int, offsets, fresh) -> None:
    end = len(buf) - hlen
    entries_end = pos
    while pos < end:
        if pos + 8 > end:
            raise IndexUnknown("git's index ends inside an extension")
        sig = bytes(buf[pos:pos + 4])
        size = struct.unpack_from(">I", buf, pos + 4)[0]
        if pos + 8 + size > end:
            raise IndexUnknown("git's index has an extension larger than the file")
        if sig == b"link":
            raise IndexUnknown("git's index is split (core.splitIndex)")
        if sig == b"sdir":
            raise IndexUnknown("git's index is a sparse index")
        if not (65 <= sig[0] <= 90):
            raise IndexUnknown(f"git's index has an extension devmem does not know ({sig!r})")
        if sig == b"EOIE" and (size != 4 + hlen or pos + 8 + size != end
                               or struct.unpack_from(">I", buf, pos + 8)[0] != entries_end):
            raise IndexUnknown("git's index has an end-of-entries extension (EOIE) that does not match its entries")
        if sig == b"IEOT":
            _ieot(buf, pos + 8, size, offsets, fresh)
        # UNTR (the untracked cache: core.untrackedCache, feature.manyFiles) is skipped by its stated size, like any
        # optional extension: it caches which files are NOT in the index and changes no entry, every entry has been
        # read before the extensions start (the header gives their count), its size is checked to lie inside the file
        # above, and the checksum over the whole file is verified after this. A damaged one can make git itself crash
        # (a fuzzed name byte: SIGSEGV), not this reader, which never looks inside it.
        if sig == b"TREE" and not _tree_ok(buf, pos + 8, size, hlen):
            raise IndexUnknown("git's index has a malformed cache-tree extension")
        if sig == b"FSMN" and not _fsmonitor_ok(buf, pos + 8, size, len(offsets)):
            raise IndexUnknown("git's index has a malformed fsmonitor extension")
        pos += 8 + size
    if pos != end:
        raise IndexUnknown("git's index has a malformed extension")
    if len(fresh) > len(offsets):
        raise IndexUnknown("git's index has an entry with an empty path")


def _checksum_ok(buf, hlen: int) -> bool:
    trailer = bytes(buf[-hlen:])
    if trailer == bytes(hlen):
        return True                             # index.skipHash: git writes no checksum
    import hashlib
    with memoryview(buf) as mv, mv[:-hlen] as body:
        return (hashlib.sha1 if hlen == 20 else hashlib.sha256)(body).digest() == trailer


def read_index(path, deadline: float, keep=lambda p: True, cap: int = 10 ** 9) -> list[tuple[str, bytes]]:
    """(object id, path relative to the top of the work tree) of the entries of the index file `path` that keep()
    accepts. Raises IndexUnknown (see the module docstring), IndexLimit for a limit of this reader. Holds the file
    once, the entries kept and one path at a time."""
    buf = _read_buf(path, MAX_INDEX_BYTES)
    if len(buf) < 12 + 20:
        raise IndexUnknown("git's index is too short")
    found, why = None, ""
    for hlen in (20, 32):                       # SHA-1 or SHA-256 repository: only one length parses to the end
        try:                                    # (once one has, the other attempt keeps nothing: it only has to fail)
            got, pos, offsets, fresh = _entries(buf, hlen, deadline, keep if found is None else None, cap)
            _extensions(buf, pos, hlen, offsets, fresh)
            del offsets, fresh
        except IndexLimit:
            raise
        except (IndexUnknown, struct.error, IndexError, ValueError, OverflowError) as e:
            if isinstance(e, IndexUnknown) and any(w in str(e) for w in ("too long", "sparse", "split")):
                raise
            why = why or (str(e) if isinstance(e, IndexUnknown) else "")
            continue
        if not _checksum_ok(buf, hlen):
            why = why or "its checksum does not match"
        else:
            if found is not None:
                raise IndexUnknown("git's index is ambiguous")
            found = got
    if found is None:
        raise IndexUnknown("git's index could not be read" + (f" ({why.replace('git' + chr(39) + 's index ', '')})"
                                                               if why else ""))
    return found


def _is_sep(c: int) -> bool:
    return c == 47 or (c == 92 and os.name == "nt")


def relative(name: bytes, prefix: bytes) -> bytes:
    """`name` (relative to the top of the work tree) as `git ls-files` prints it from the folder `prefix` (relative to
    the top, ending with `/`, or empty): a port of git's relative_path() (path.c)."""
    if not prefix:
        return name
    i = j = 0
    pl, nl = len(prefix), len(name)
    prefix_off = in_off = 0
    while i < pl and j < nl and prefix[i] == name[j]:
        if _is_sep(prefix[i]):
            while i < pl and _is_sep(prefix[i]):
                i += 1
            while j < nl and _is_sep(name[j]):
                j += 1
            prefix_off, in_off = i, j
        else:
            i += 1
            j += 1
    if i >= pl and prefix_off < pl:
        if j >= nl:
            in_off = nl
        elif _is_sep(name[j]):
            while j < nl and _is_sep(name[j]):
                j += 1
            in_off = j
        else:
            i = prefix_off
    elif j >= nl and in_off < nl and j < nl and _is_sep(name[j]):
        pass
    elif j >= nl and i < pl and _is_sep(prefix[i]):
        while i < pl and _is_sep(prefix[i]):
            i += 1
        in_off = nl
    rest = name[in_off:]
    if i >= pl:
        return rest if rest else b"./"
    out = bytearray()
    while i < pl:
        if _is_sep(prefix[i]):
            out += b"../"
            while i < pl and _is_sep(prefix[i]):
                i += 1
            continue
        i += 1
    if not _is_sep(prefix[pl - 1]):
        out += b"../"
    return bytes(out) + rest


# The characters a case-insensitive file system can take for ASCII letters (Unicode case folding, NTFS's upcase table):
# sharp s and capital sharp s (ss), dotted capital I and dotless i (i), long s (s), the Kelvin sign (k) and the Latin
# ligatures U+FB00-FB06 (ff fi fl ffi ffl st st), in UTF-8. Only a path holding one of them can spell an ASCII store
# name with non-ASCII letters, and only a name holding f, i, k, l, s or t.
_FOLDS_TO_ASCII = (b"\xc3\x9f", b"\xe1\xba\x9e", b"\xc4\xb0", b"\xc4\xb1", b"\xc5\xbf", b"\xe2\x84\xaa",
                   *(bytes((0xEF, 0xAC, 0x80 + i)) for i in range(7)))
_FOLD_LETTERS = frozenset("fiklstFIKLST")
# Code points HFS+ leaves out when it compares names (U+200C-200F, U+202A-202E, U+206A-206F, U+FEFF, in UTF-8): a path
# holding one of them may spell any store name there.
_IGNORED_BY_HFS = re.compile(b"\xe2\x80[\x8c-\x8f\xaa-\xae]|\xe2\x81[\xaa-\xaf]|\xef\xbb\xbf")


def index_candidates(where, names, deadline: float, cap: int, tilde=None) -> list[tuple[str, bytes]]:
    """What devmem.store._candidates_raw gets from git, read from the index file: (object id, path as git prints it
    from `where`) of every entry that may land in a folder named one of `names`: one of the names in any ASCII letter
    case, or with a non-ASCII letter a file system takes for one of its letters (_FOLDS_TO_ASCII; every non-ASCII
    path when a name is not ASCII), or an 8.3 short name: a component holding `~` when `tilde` is None, else one that
    tilde.could_be (devmem.store._tilde_rule, a devmem.store._ShortNames). At most `cap` of them (more: IndexLimit)."""
    top, index = locate(where)
    start = os.path.realpath(where) if os.name != "nt" else os.path.abspath(where)
    rel = os.path.relpath(start, top)
    prefix = b"" if rel == "." else os.fsencode(rel.replace(os.sep, "/")) + b"/"
    keys = [os.fsencode(n).lower() for n in names if n and n.isascii()]
    wide = any(n and not n.isascii() for n in names)
    folds = any(_FOLD_LETTERS & set(n) for n in names if n)
    seps = "/\\" if os.name == "nt" else "/"

    def short(p: bytes) -> bool:
        s = p.decode("utf-8", "surrogateescape")
        for sep in seps[1:]:
            s = s.replace(sep, "/")
        return tilde is not None and any("~" in c and tilde.could_be(c) for c in s.split("/"))

    def keep(p: bytes) -> bool:
        if wide:
            return True
        if b"~" in p and (tilde is None or short(p)):
            return True
        if not p.isascii() and ((folds and any(f in p for f in _FOLDS_TO_ASCII)) or _IGNORED_BY_HFS.search(p)):
            return True
        low = p.lower()
        return any(k in low for k in keys)
    if not os.path.lexists(index):
        return []                               # no index yet (a new repository): git tracks nothing
    got = read_index(index, deadline, keep, cap)
    if len(got) > cap:
        raise IndexLimit(f"more than {cap} entries of git's index could be in the store")
    for i, (oid, p) in enumerate(got):          # in place: the list is not held twice
        got[i] = (oid, relative(p, prefix))
    return got
