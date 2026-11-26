"""Incremental, ranked index over project files, sessions and turns (SQLite, stdlib only).

Stored at `<store>/index/index.db`. It is derived data: if it is damaged or from an older version it
is deleted and rebuilt (files come back on the next `sync`; sessions/turns must be upserted again by
the retrieval layer, which can tell from `Index.rebuilt` or by comparing `keys("session")`).

Why a postings table and not FTS5: `devmem.text.tokenize` must define what a term is (Arabic
stemming, CJK bigrams, camelCase splits). With FTS5 the terms would have to be re-tokenized by an
FTS tokenizer or squeezed through one, and the per-field weights (path x3, symbols x2) and
`matched` terms would need extra aux queries; FTS5 is also missing from some Python builds. A plain
`postings(kind, term, doc, tf)` table keeps the term definition in one place, works on every SQLite,
and BM25 over a handful of query terms is a few indexed range scans.

Scoring: BM25 (k1=1.2, b=0.6, the values `devmem.pack` used) with field-weighted term frequency:
each field's tokens count `FIELD_WEIGHTS[field]` times (path 3; module, symbols, defines, title, prompt 2; other 1),
which equals the v1 practice of repeating path tokens three times.

Concurrency: WAL journal, 10 s busy timeout, every write in `BEGIN IMMEDIATE` (so two writers queue
instead of failing on lock upgrade). Readers never block writers. If the lock cannot be taken, the
call reports `error: "locked"` (sync) or returns False (upsert/delete) instead of raising.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time

from pathlib import Path

from .extract import EXTRACTOR_VERSION, extract_file, is_text_candidate
from .store import (MAX_TEXT_BYTES, ListingTimeout, StoreRefused, ensure_store, list_files, normalized_hash,
                    private_mkdir, private_sqlite, project_path, read_regular)
from .text import TOKENIZER_VERSION, count_terms, tokenize

SCHEMA = 2
VERSION = f"schema{SCHEMA}-tok{TOKENIZER_VERSION}-ext{EXTRACTOR_VERSION}"
KINDS = {"file": 1, "session": 2, "turn": 3}
FIELD_WEIGHTS = {"path": 3.0, "module": 2.0, "symbols": 2.0, "defines": 2.0, "title": 2.0, "prompt": 2.0}
K1, B = 1.2, 0.6
RACY_NS = 3_000_000_000   # a file modified within 3 s of being indexed is re-hashed on the next sync
BATCH = 200               # files per write transaction during sync
FLUSH_S = 1.0             # and at least this often (seconds of work)
MAX_FIELD_CHARS = 600_000   # characters of one field that are tokenised for the index (a file is at most 512 KB)

_TABLES = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(rel TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
    indexed_ns INTEGER NOT NULL, hash TEXT NOT NULL, info TEXT NOT NULL, facts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY, kind INTEGER NOT NULL, key TEXT NOT NULL,
    len REAL NOT NULL, meta TEXT, terms TEXT, UNIQUE(kind, key));
CREATE TABLE IF NOT EXISTS postings(kind INTEGER NOT NULL, term TEXT NOT NULL, doc INTEGER NOT NULL,
    tf REAL NOT NULL, PRIMARY KEY(kind, term, doc)) WITHOUT ROWID;
"""
_REQUIRED = {"meta", "files", "docs", "postings"}
_UPSERT = sqlite3.sqlite_version_info >= (3, 24, 0)     # INSERT ... ON CONFLICT DO UPDATE


def _not_regular(p: str) -> bool:
    """Something other than a regular file or folder (a FIFO or device a repository shipped as index.db)."""
    import stat
    try:
        mode = os.lstat(p).st_mode
    except OSError:
        return False
    return not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))


_DAMAGE = ("malformed", "not a database", "no such table", "no such column", "file is encrypted")


def _is_lock_error(e: BaseException) -> bool:
    """A lock, or another error that says nothing about what the file holds (an I/O error or a sharing violation while
    another process or a virus scanner has it open, a full or read-only disk): the caller gives up for now and the index
    stays. Only damage (a corrupt file, not a database, missing tables) makes it be deleted and rebuilt: rebuilding on
    a passing "disk I/O error" emptied the index, and the pack built right then lost every file."""
    if not isinstance(e, sqlite3.OperationalError):
        return False
    msg = str(e).lower()
    return not any(d in msg for d in _DAMAGE)


def _kind_id(kind: str) -> int:
    try:
        return KINDS[kind]
    except KeyError:
        raise ValueError(f"unknown index kind {kind!r}; expected one of {sorted(KINDS)}") from None


def doc_terms(fields: dict[str, str], weights: dict[str, float] | None = None) -> tuple[dict[str, float], float]:
    """Field texts -> (weighted term frequencies, weighted length)."""
    tf: dict[str, float] = {}
    length = 0.0
    for name, text in fields.items():
        if not text:
            continue
        text = text[:MAX_FIELD_CHARS]   # a pasted multi-megabyte prompt or session text costs at most this much
        w = float((weights or {}).get(name, FIELD_WEIGHTS.get(name, 1.0)))
        if w <= 0:
            continue
        counts = count_terms(text)
        length += w * sum(counts.values())
        for t, c in counts.items():
            tf[t] = tf.get(t, 0.0) + w * c
    return tf, length


def file_fields(rel: str, info: dict, text: str) -> dict[str, str]:
    """The searchable fields of a project file (weights in FIELD_WEIGHTS)."""
    syms = " ".join(s.replace(":", ".").split(".")[-1] for s in info.get("symbols", []))
    names = " ".join(list(info.get("local_functions", [])) + list(info.get("config_keys", []))
                     + list(info.get("assets", [])) + list(info.get("remotes_used", {})))
    return {"path": rel, "module": info.get("module", ""), "symbols": syms,
            "defines": " ".join(info.get("remotes_defined", [])), "names": names, "body": text}


SLOW_EXTRACT_S = 0.5      # a file whose extraction and tokenising took longer is remembered (by content hash) and
SLOW_KEPT = 500           # then indexed as text only: no extraction, and only its first SLOW_BODY_CHARS tokenised
SLOW_BODY_CHARS = 64_000


TEST_SWITCH_ENV = "DEVMEM_TESTING"              # both must be set: a test makes one file's extraction block
TEST_BLOCK_ENV = "DEVMEM_TEST_BLOCK_EXTRACT"     # "<text the file contains>|<seconds>[|plain]"
TEST_BLOCK_MAX_S = 60.0


def _test_block(text: str, level: str) -> None:
    """Test-only: block (sleep, at most TEST_BLOCK_MAX_S) while preparing a file that contains the marker text, to
    prove that a stuck step is bounded from outside (devmem.refresh) without relying on any algorithm being fast. It
    blocks full extraction only, or with `|plain` the text-only pass too. Inert unless both variables are set."""
    if os.environ.get(TEST_SWITCH_ENV) != "1":
        return
    spec = os.environ.get(TEST_BLOCK_ENV)
    if not spec:
        return
    parts = spec.split("|")
    if len(parts) < 2 or not parts[0] or parts[0] not in text:
        return
    if level != "full" and not (level == "plain" and parts[2:3] == ["plain"]):
        return
    try:
        secs = float(parts[1])
    except ValueError:
        return
    if secs == secs and secs > 0:       # not NaN
        time.sleep(min(secs, TEST_BLOCK_MAX_S))


def _prepare(project: str, rel: str, known_hash: str | None, slow: set | frozenset = frozenset(),
             skip: set | frozenset = frozenset(), before=None):
    """Read, hash and (if the content changed) extract one file. A content hash in `slow` is not extracted again
    (plain-text indexing only, of its first SLOW_BODY_CHARS); one in `skip` is indexed by its path only.
    `before(hash, level)` runs once the content is known and before any work on it (devmem.index marks it in progress
    there). The payload ends with the seconds extraction and tokenising took."""
    p = project_path(project, rel)
    data = read_regular(p, MAX_TEXT_BYTES) if p is not None else None
    if data is None:       # gone, grown past the cap, replaced by a link/FIFO, or not a plain project path
        return rel, "gone", None
    if b"\0" in data[:8192]:          # binary despite the extension
        return rel, "binary", None
    h = normalized_hash(data)
    if known_hash == h:
        return rel, "same", None
    text = data.decode("utf-8", errors="replace")
    level = "skip" if h in skip else "plain" if h in slow else "full"
    if before is not None:
        before(h, level)
    t = time.perf_counter()
    info, facts = extract_file(rel, text, plain=level != "full")
    info.update(hash=h, lines=text.count("\n") + 1)
    if level == "skip":
        info["content_skipped"] = True    # stopped devmem twice: found by its name only
        body = ""
    else:
        _test_block(text, level)       # stands for an extractor (or tokeniser) that does not return
        body = text if level == "full" else text[:SLOW_BODY_CHARS]
    # tokenising counts too: a text that is cheap to extract can still be slow to tokenise (it once was: NFKC)
    tf, length = doc_terms(file_fields(rel, info, body))
    took = time.perf_counter() - t
    return rel, "new", (h, info, facts, tf, length, took)


def _prepare_all(project: str, items: list[tuple[str, str | None]], slow: set | frozenset = frozenset(),
                 skip: set | frozenset = frozenset(), before=None):
    """Yield _prepare results in order, in this process. (Worker processes were tried and dropped:
    on Windows `spawn` re-imports the caller's main script, which is unsafe for unguarded callers,
    and on the 740-file stdlib tree it was slower than serial. A stuck file is bounded from outside instead: see
    devmem.refresh.)"""
    for rel, kh in items:
        yield _prepare(project, rel, kh, slow, skip, before)


MARK_PREFIX = "extracting."     # index/extracting.<pid>.<thread>: the content that thread is working on
STAMP_PREFIX = "stopped."       # index/stopped.<pid>: devmem.refresh killed that child at its budget
MARK_STALE_S = 600              # a mark this old counts as left by a dead process whatever its pid shows
MARK_CAP = 512                  # bytes of a mark that are read
MARKS_READ = 64                 # marks looked at per sync (more are left for the next one)
# The map build (devmem.scan) under a time budget is marked too, with these levels: "map0" for the full map, "map1" for
# the map without the Rojo mapping, import graph and conventions. A build that stopped a process demotes the next ones
# a level (the same rule as content: at once when devmem.refresh stamped the kill, else the second time); level 2 is
# file entries only. A demotion lasts MAP_DEMOTE_S, or until `devmem scan` (no budget) builds the full map.
MAP_MARK_LEVELS = ("map0", "map1")
MAP_MARK_HASH = "map"
MAP_DEMOTE_S = 24 * 3600


class _Mark:
    """The content this thread is extracting, in `index/extracting.<pid>.<thread>`: written before the work starts,
    emptied when it ends, removed when the sync ends. A plain write to an open file, no fsync and no SQLite commit:
    it survives the process being killed (all it is for; the data is in the OS's cache), and costs microseconds where
    a committed SQLite row per file made the first scan of the 740-file stdlib tree about 25% slower. The next sync
    (`Index._reap_marks`) finds a mark left by a process that is gone and does not try that content the same way
    again. Never raises: when the mark cannot be written the work goes on unmarked."""

    def __init__(self, folder: Path):
        import threading
        self.path = Path(folder) / f"{MARK_PREFIX}{os.getpid()}.{threading.get_ident()}"
        self.fd: int | None = None

    def set(self, h: str, level: str) -> None:
        data = json.dumps({"h": h, "level": level, "t": time.time()}).encode("utf-8")
        try:
            if self.fd is None:
                if os.path.lexists(self.path):     # a leftover of a dead process with this id: never opened
                    os.unlink(self.path)
                self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                                  | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.lseek(self.fd, 0, os.SEEK_SET)
            os.write(self.fd, data)
            os.ftruncate(self.fd, len(data))
        except OSError:
            pass

    def clear(self) -> None:
        if self.fd is not None:
            try:
                os.ftruncate(self.fd, 0)
            except OSError:
                pass

    def close(self) -> None:
        if self.fd is None:
            return
        try:
            os.close(self.fd)
            os.unlink(self.path)
        except OSError:
            pass
        self.fd = None


def _read_mark(path: Path) -> tuple[int, int, dict | None] | None:
    """(pid, thread, mark or None when empty) of a mark file; None when its name or content is not a mark's."""
    parts = path.name[len(MARK_PREFIX):].split(".")
    if len(parts) != 2 or not all(p.isdigit() and len(p) < 24 for p in parts):
        return None
    data = read_regular(path, MARK_CAP)
    if data is None:
        return None
    if not data.strip():
        return int(parts[0]), int(parts[1]), None
    try:
        mark = json.loads(data.decode("utf-8"))
        mark = {"h": str(mark["h"])[:64], "level": str(mark["level"]), "t": float(mark["t"])}
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeDecodeError):
        return None
    return int(parts[0]), int(parts[1]), mark


def _pid_gone(pid: int) -> bool:
    """True when no process with this id is running (False when it cannot be told)."""
    if pid <= 0 or pid >= 2 ** 31:
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k.OpenProcess(0x1000, False, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 87            # ERROR_INVALID_PARAMETER: no such process
        try:
            code = wintypes.DWORD()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value != 259   # not STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def stamp_stopped(store_index: Path, pid: int) -> None:
    """devmem.refresh, just before it kills refresh child `pid` at its time budget: the content that child was working
    on (its mark) is then demoted at once (see Index._reap_marks). Never raises."""
    try:
        p = Path(store_index) / f"{STAMP_PREFIX}{int(pid)}"
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
    except (OSError, ValueError):
        pass


class Index:
    def __init__(self, store: Path, timeout: float = 10.0):
        self.store = Path(store)
        self.timeout = float(timeout)  # seconds to wait for a lock held by another process
        self.path = self.store / "index" / "index.db"
        self.rebuilt = False      # True if the db was (re)created by this instance
        self.degraded = False     # True if running on an in-memory db (file unusable and not removable)
        self.last_error: str | None = None
        self.map_stopped = 0      # the map level a stopped map build demoted to during this instance's sync
        self._conn: sqlite3.Connection | None = None
        try:
            ensure_store(self.store)
        except StoreRefused as e:
            # never let SQLite write through a linked store (or index/): an empty in-memory index, as for a linked file
            self.last_error = f"{e}; using an in-memory index"
            self._conn = self._connect(":memory:")
            self._init_schema()
            self.degraded = self.rebuilt = True
            return
        self._open()

    @property
    def conn(self) -> sqlite3.Connection:
        """The open connection. After close() there is none: using the index then raises, as before."""
        conn = self._conn
        if conn is None:
            raise AttributeError("the index is closed")
        return conn

    # ---- lifecycle --------------------------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def scrub(self, timeout: float = 3.0) -> bool:
        """For `devmem forget` only: make deleted documents unrecoverable from every file under index/. A
        one-time VACUUM for an index written before secure_delete was on (its free pages may still hold
        deleted text), then a truncating checkpoint so the write-ahead log holds no deleted or overwritten
        content. Waits at most `timeout` seconds for other connections (a reader holding a snapshot blocks the
        checkpoint); returns False if it could not complete, so the caller can say so. Never used on the
        hook path."""
        self._dirty = False
        if self._conn is None or self.degraded:
            return False
        try:
            self.conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            if self.meta_get("secure_delete") != "1":
                self.conn.execute("VACUUM")
                self.meta_set("secure_delete", "1")
            busy, _log, _done = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            return not busy
        except sqlite3.Error:
            return False
        finally:
            try:
                self.conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
            except sqlite3.Error:
                pass

    def close(self) -> None:
        if getattr(self, "_dirty", False) and self._conn is not None:
            # routine delete (re-indexing on a prompt): a non-blocking checkpoint only, never a wait. Old text
            # can stay in the -wal file until the next checkpoint or until the last connection closes.
            try:
                self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def _connect(self, target: str) -> sqlite3.Connection:
        if target != ":memory:" and any(os.path.islink(p) or _not_regular(p) for p in (
                os.path.dirname(target), target, target + "-wal", target + "-shm", target + "-journal")):
            # never let SQLite write through a link (a pulled-in index.db -> elsewhere): an empty in-memory index
            self.last_error = f"{target} (or its folder or journal) is a symbolic link; using an in-memory index"
            target = ":memory:"
        if target != ":memory:":
            private_sqlite(target)        # 0600 on POSIX, and so are the -wal/-shm files SQLite makes from it
        conn = sqlite3.connect(target, timeout=self.timeout, isolation_level=None, check_same_thread=False)
        conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        conn.execute("PRAGMA secure_delete=ON")   # deleted text is overwritten, not left in free pages
        return conn

    def _open(self) -> None:
        try:
            private_mkdir(self.path.parent)
            self._conn = self._connect(str(self.path))
            self._init_schema()
        except sqlite3.DatabaseError as e:
            if _is_lock_error(e) and self._conn is not None:
                self.last_error = f"locked while opening: {e}"
                return
            if _is_lock_error(e):       # could not even connect, for now: an empty in-memory index, nothing deleted
                self.last_error = f"could not open the index: {e}; using an in-memory index"
                self._conn = self._connect(":memory:")
                self._init_schema()
                self.degraded = True
                return
            self._rebuild(e)
        except OSError as e:
            self._rebuild(e)

    def _init_schema(self) -> None:
        conn = self.conn
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if _REQUIRED <= tables:
            row = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
            if row and row[0] == VERSION:
                self._pragmas()
                return
        self._pragmas()
        conn.execute("BEGIN IMMEDIATE")
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            row = (conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
                   if "meta" in tables else None)
            if not (row and row[0] == VERSION and _REQUIRED <= tables):  # another process may have won
                for t in ("postings", "docs", "files", "meta"):
                    conn.execute(f"DROP TABLE IF EXISTS {t}")
                for stmt in _TABLES.strip().split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('version', ?)", (VERSION,))
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('secure_delete', '1')")
                self.rebuilt = True
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def _pragmas(self) -> None:
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass  # e.g. a filesystem without shared memory: keep the default journal
        self.conn.execute("PRAGMA synchronous=NORMAL")

    def _rebuild(self, err: BaseException) -> None:
        """The db is unreadable or damaged: delete it and start empty (never raise)."""
        self.last_error = f"rebuilt index after: {err!r}"
        self.close()
        removed = True
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                removed = False
        if not removed:
            try:
                os.replace(self.path, self.path.with_name(f"index.db.damaged-{int(time.time())}"))
                removed = True
            except OSError:
                pass
        try:
            if not removed:
                raise sqlite3.OperationalError("cannot replace damaged index file")
            private_mkdir(self.path.parent)
            self._conn = self._connect(str(self.path))
            self._init_schema()
        except (sqlite3.DatabaseError, OSError) as e2:
            self.close()
            self._conn = self._connect(":memory:")
            self._init_schema()
            self.degraded = True
            self.last_error += f"; using an in-memory index: {e2!r}"
        self.rebuilt = True

    def _guard(self, fn, *args, default=None):
        """Run a read/write; on lock return `default`, on damage rebuild and retry once."""
        try:
            return fn(*args)
        except sqlite3.DatabaseError as e:
            if _is_lock_error(e):
                self.last_error = f"locked: {e}"
                return default
            self._rebuild(e)
            try:
                return fn(*args)
            except sqlite3.DatabaseError as e2:
                self.last_error = f"{self.last_error}; retry failed: {e2!r}"
                return default

    def _tx(self, body) -> None:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            body(conn)
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    # ---- documents --------------------------------------------------------------------------
    @staticmethod
    def _write_doc(conn, kid: int, key: str, tf: dict, length: float, meta: dict | None) -> None:
        mj = json.dumps(meta, sort_keys=True) if meta is not None else None
        terms = "\n".join(tf)
        row = conn.execute("SELECT id, terms FROM docs WHERE kind=? AND key=?", (kid, key)).fetchone()
        if row:
            did = row[0]
            if _UPSERT:
                # a replaced document (a session record that grew by a turn) keeps most of its terms: only terms it
                # lost are deleted and a posting is written only where its frequency changed (deleting and inserting
                # every posting of every turn of a growing live log took 26 s per prompt)
                old = row[1].split("\n") if row[1] else []
                conn.executemany("DELETE FROM postings WHERE kind=? AND term=? AND doc=?",
                                 [(kid, t, did) for t in old if t not in tf])
                conn.executemany("INSERT INTO postings(kind, term, doc, tf) VALUES (?,?,?,?) "
                                 "ON CONFLICT(kind, term, doc) DO UPDATE SET tf=excluded.tf WHERE tf != excluded.tf",
                                 [(kid, t, did, v) for t, v in tf.items()])
                conn.execute("UPDATE docs SET len=?, meta=?, terms=? WHERE id=?", (length, mj, terms, did))
                return
            Index._drop_postings(conn, kid, did, row[1])
            conn.execute("UPDATE docs SET len=?, meta=?, terms=? WHERE id=?", (length, mj, terms, did))
        else:
            did = conn.execute("INSERT INTO docs(kind, key, len, meta, terms) VALUES (?,?,?,?,?)",
                               (kid, key, length, mj, terms)).lastrowid
        conn.executemany("INSERT INTO postings(kind, term, doc, tf) VALUES (?,?,?,?)",
                         [(kid, t, did, v) for t, v in tf.items()])

    def doc_metas(self, kind: str, prefix: str) -> dict[str, dict] | None:
        """{key: meta} of the `kind` documents whose key starts with `prefix` (one range scan on the (kind, key)
        index); None when the index is locked."""
        kid = _kind_id(kind)
        if not prefix:
            return None
        end = prefix[:-1] + chr(ord(prefix[-1]) + 1)

        def run():
            out = {}
            for key, mj in self.conn.execute("SELECT key, meta FROM docs WHERE kind=? AND key>=? AND key<?",
                                             (kid, prefix, end)):
                try:
                    m = json.loads(mj) if mj else {}
                except (ValueError, RecursionError):
                    m = {}
                out[key] = m if isinstance(m, dict) else {}
            return out
        return self._guard(run, default=None)

    @staticmethod
    def _drop_postings(conn, kid: int, did: int, terms: str | None) -> None:
        # postings are keyed (kind, term, doc); the doc's own term list makes this a set of point deletes
        if terms:
            conn.executemany("DELETE FROM postings WHERE kind=? AND term=? AND doc=?",
                             [(kid, t, did) for t in terms.split("\n")])

    @staticmethod
    def _remove_doc(conn, kid: int, key: str) -> None:
        row = conn.execute("SELECT id, terms FROM docs WHERE kind=? AND key=?", (kid, key)).fetchone()
        if row:
            Index._drop_postings(conn, kid, row[0], row[1])
            conn.execute("DELETE FROM docs WHERE id=?", (row[0],))

    def upsert_doc(self, kind: str, key: str, fields: dict[str, str], meta: dict | None = None,
                   weights: dict[str, float] | None = None) -> bool:
        """Index (or replace) one document. `fields` maps a field name to its text; the field name
        selects the weight (FIELD_WEIGHTS, default 1; override with `weights`). Returns False if the
        db stayed locked."""
        kid = _kind_id(kind)
        tf, length = doc_terms(fields, weights)
        ok = self._guard(lambda: self._tx(lambda c: self._write_doc(c, kid, key, tf, length, meta)) or True,
                         default=False)
        return bool(ok)

    def upsert_docs(self, kind: str, docs: list[tuple[str, dict[str, str], dict | None]],
                    weights: dict[str, float] | None = None) -> bool:
        """Several (key, fields, meta) in one transaction (much faster than one upsert_doc each)."""
        kid = _kind_id(kind)
        prepared = [(key, *doc_terms(fields, weights), meta) for key, fields, meta in docs]

        def body(c):
            for key, tf, length, meta in prepared:
                self._write_doc(c, kid, key, tf, length, meta)
        return bool(self._guard(lambda: self._tx(body) or True, default=False))

    def delete_doc(self, kind: str, key: str) -> bool:
        kid = _kind_id(kind)
        self._dirty = True   # close() runs a passive checkpoint once
        return bool(self._guard(lambda: self._tx(lambda c: self._remove_doc(c, kid, key)) or True, default=False))

    def delete_docs(self, kind: str, keys: list[str]) -> bool:
        """Several delete_doc in one transaction."""
        if not keys:
            return True
        kid = _kind_id(kind)
        self._dirty = True

        def body(c):
            for key in keys:
                self._remove_doc(c, kid, key)
        return bool(self._guard(lambda: self._tx(body) or True, default=False))

    def keys(self, kind: str, strict: bool = False):
        """The keys of every `kind` document; an empty set when the index is locked, or None with `strict` (so a
        caller can tell "no documents" from "could not read": embed.rank_for_pack drops cached vectors by it)."""
        kid = _kind_id(kind)
        return self._guard(lambda: {r[0] for r in self.conn.execute("SELECT key FROM docs WHERE kind=?", (kid,))},
                           default=None if strict else set())

    def count(self, kind: str) -> int:
        kid = _kind_id(kind)
        return self._guard(lambda: self.conn.execute("SELECT COUNT(*) FROM docs WHERE kind=?", (kid,)).fetchone()[0],
                           default=0)

    # ---- ranking ----------------------------------------------------------------------------
    def _stats(self, kids: tuple[int, ...]) -> tuple[int, float]:
        q = ",".join("?" * len(kids))
        n, total = self.conn.execute(f"SELECT COUNT(*), COALESCE(SUM(len), 0) FROM docs WHERE kind IN ({q})",  # noqa: S608 - only a run of '?' placeholders is formatted in
                                     kids).fetchone()
        return n, (total / n if n else 1.0) or 1.0

    def search(self, kind: str, query: str, limit: int = 20) -> list[dict]:
        """BM25 over `kind` documents for the terms of `query` (tokenized with devmem.text).
        Returns [{key, score, matched: [terms], meta}] best first; ties broken by key.
        `limit <= 0` returns every document that matches at least one term."""
        kid = _kind_id(kind)
        terms = list(dict.fromkeys(tokenize(query)))
        if not terms:
            return []
        return self._guard(self._search, kid, terms, limit, default=[])

    def search_terms(self, kind: str, terms: list[str], limit: int = 20) -> list[dict]:
        """Like search() but with terms that are already tokenize() output."""
        kid = _kind_id(kind)
        terms = list(dict.fromkeys(t for t in terms if t))
        return self._guard(self._search, kid, terms, limit, default=[]) if terms else []

    def _search(self, kid: int, terms: list[str], limit: int) -> list[dict]:
        """Postings, statistics and metadata are read in one read transaction (one snapshot): read one by one, a
        document replaced by another process in between came back with no metadata (`{}`)."""
        conn = self.conn
        if conn.in_transaction:
            return self._search_rows(kid, terms, limit)
        conn.execute("BEGIN")
        try:
            return self._search_rows(kid, terms, limit)
        finally:
            try:
                conn.execute("COMMIT")      # read only: this just ends the snapshot
            except sqlite3.Error:
                pass

    def _search_rows(self, kid: int, terms: list[str], limit: int) -> list[dict]:
        n, avg = self._stats((kid,))
        if not n:
            return []
        scores: dict[int, float] = {}
        matched: dict[int, list[str]] = {}
        keys: dict[int, str] = {}
        for t in terms:
            rows = self.conn.execute(
                "SELECT p.doc, p.tf, d.len, d.key FROM postings p JOIN docs d ON d.id = p.doc "
                "WHERE p.kind=? AND p.term=?", (kid, t)).fetchall()
            if not rows:
                continue
            df = len(rows)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for doc, tf, dl, key in rows:
                s = idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * dl / avg))
                scores[doc] = scores.get(doc, 0.0) + s
                matched.setdefault(doc, []).append(t)
                keys[doc] = key
        ranked = sorted(scores, key=lambda d: (-scores[d], keys[d]))
        if limit and limit > 0:
            ranked = ranked[:limit]
        metas = {}
        for i in range(0, len(ranked), 500):
            chunk = ranked[i:i + 500]
            for did, mj in self.conn.execute(
                    f"SELECT id, meta FROM docs WHERE id IN ({','.join('?' * len(chunk))})", chunk):  # noqa: S608 - only a run of '?' placeholders is formatted in
                metas[did] = json.loads(mj) if mj else {}
        return [{"key": keys[d], "score": scores[d], "matched": sorted(matched[d]), "meta": metas.get(d, {})}
                for d in ranked]

    def idf(self, kind, term: str) -> float:
        """BM25 idf of a term in one kind (or several: pass a list/tuple of kinds). `term` should be a
        tokenize() term; a raw word that is not indexed as-is is tokenized first."""
        kinds = (kind,) if isinstance(kind, str) else tuple(kind)
        kids = tuple(_kind_id(k) for k in kinds)

        def run():
            n, _avg = self._stats(kids)
            if not n:
                return 0.0
            q = ",".join("?" * len(kids))
            df = self.conn.execute(f"SELECT COUNT(*) FROM postings WHERE kind IN ({q}) AND term=?",  # noqa: S608 - only a run of '?' placeholders is formatted in
                                   (*kids, term)).fetchone()[0]
            if not df:
                toks = tokenize(term)
                if len(toks) == 1 and toks[0] != term:
                    df = self.conn.execute(f"SELECT COUNT(*) FROM postings WHERE kind IN ({q}) AND term=?",  # noqa: S608 - only a run of '?' placeholders is formatted in
                                           (*kids, toks[0])).fetchone()[0]
            return math.log(1 + (n - df + 0.5) / (df + 0.5))
        return self._guard(run, default=0.0)

    # ---- files ------------------------------------------------------------------------------
    def files(self) -> dict[str, dict]:
        """rel -> info, the same shape as map.json "files" (sorted by path)."""
        return self._guard(lambda: {rel: json.loads(info) for rel, info in
                                    self.conn.execute("SELECT rel, info FROM files ORDER BY rel")}, default={})

    def facts(self) -> dict[str, dict]:
        """rel -> internal extraction facts (ops, uses, entry; see devmem.extract)."""
        return self._guard(lambda: {rel: json.loads(f) for rel, f in
                                    self.conn.execute("SELECT rel, facts FROM files ORDER BY rel")}, default={})

    def file_rows(self, with_facts: bool = True):
        """(rel, info JSON, facts JSON or None) per indexed file in path order, read lazily from one cursor: devmem.scan
        keeps only as much of the map as its caps allow, so the whole table is never held in memory. A lock or damage
        mid-way ends the rows (last_error says so)."""
        sql = "SELECT rel, info, facts FROM files ORDER BY rel" if with_facts else \
            "SELECT rel, info, NULL FROM files ORDER BY rel"
        try:
            yield from self.conn.execute(sql)
        except sqlite3.DatabaseError as e:
            self.last_error = f"reading files: {e}"

    def map_level(self) -> int:
        """The level the next map build under a time budget uses: 0 (full) unless a stopped build demoted it in the last
        MAP_DEMOTE_S seconds (see MAP_MARK_LEVELS)."""
        raw = self.meta_get("map_level")
        if not raw or len(raw) > 200:
            return 0
        try:
            d = json.loads(raw)
            level, at = int(d["level"]), float(d["t"])
        except (ValueError, TypeError, KeyError, RecursionError):
            return 0
        if not 0 < level <= len(MAP_MARK_LEVELS) or not -60 < time.time() - at < MAP_DEMOTE_S:
            return 0
        return level

    def clear_map_level(self) -> None:
        if self.meta_get("map_level"):
            self.meta_set("map_level", "")

    def meta_get(self, key: str) -> str | None:
        row = self._guard(lambda: self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone())
        return row[0] if row else None

    def meta_set(self, key: str, value: str) -> bool:
        return bool(self._guard(lambda: self._tx(lambda c: c.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))) or True, default=False))

    def _hash_list(self, key: str) -> list[str]:
        """A list of content hashes kept in meta (the slow-file memo), read tolerantly: anything but a JSON list of
        strings counts as empty, and at most SLOW_KEPT entries are read (the writer keeps no more)."""
        raw = self.meta_get(key)
        if not raw or len(raw) > SLOW_KEPT * 80:
            return []
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            return []
        if not isinstance(value, list):
            return []
        return [h for h in value[-SLOW_KEPT:] if isinstance(h, str) and len(h) <= 64]

    def sync(self, project: Path, deadline: float | None = None) -> dict:
        """Bring the file index in line with the project. Unchanged files are skipped by
        (size, mtime_ns); a stat change (or a recent mtime) is confirmed by content hash before
        anything is re-extracted. Returns {added, changed, removed, unchanged, seconds, files,
        hashed, listing[, error]}. With `deadline` (a time.perf_counter() value) the files still to be read when it
        passes are left for the next sync: what was indexed is kept and `incomplete`/`pending` say so."""
        t0 = time.perf_counter()
        project = Path(project).resolve()
        try:
            out = self._sync(project, deadline)
        except sqlite3.DatabaseError as e:
            if _is_lock_error(e):
                self.last_error = f"locked: {e}"
                out = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0, "error": "locked"}
            else:
                self._rebuild(e)
                try:
                    out = self._sync(project, deadline)
                except sqlite3.DatabaseError as e2:
                    self.last_error = f"{self.last_error}; sync after rebuild failed: {e2!r}"
                    out = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0, "error": repr(e2)}
        out["seconds"] = round(time.perf_counter() - t0, 4)
        if self.rebuilt:
            out["rebuilt"] = True
        return out

    def _plan(self, project: Path, deadline: float | None = None, git_by: float | None = None):
        """List the project and compare with the files table: (cands, known, check, removed, counts). With `deadline`
        the listing raises store.ListingTimeout once it passes; `git_by` bounds only git (store.list_files)."""
        exclude = ()
        try:
            exclude = (self.store.resolve().relative_to(project).as_posix(),)
        except ValueError:
            pass
        listing: dict = {}
        entries, method = list_files(project, store=self.store, exclude_prefixes=exclude, stats=listing,
                                     deadline=deadline, git_by=git_by)
        cands = {rel: (size, mt) for rel, size, mt in entries if size <= MAX_TEXT_BYTES and is_text_candidate(rel)}
        known = {r[0]: r[1:] for r in self.conn.execute("SELECT rel, size, mtime_ns, indexed_ns, hash FROM files")}
        counts = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0, "hashed": 0, "listing": method, **listing}
        check = []
        for rel, (size, mt) in cands.items():
            k = known.get(rel)
            if k and k[0] == size and k[1] == mt and mt < k[2] - RACY_NS:
                counts["unchanged"] += 1
            else:
                check.append(rel)
        removed = sorted(set(known) - set(cands))
        return cands, known, check, removed, counts

    def check(self, project: Path, deadline: float) -> dict:
        """The question devmem.refresh asks in the hook's (or MCP server's) own process before it starts a refresh: does
        the file index need a sync? Bounded by `deadline` (a time.perf_counter() value) and by construction: it lists
        the project, confirms a changed stat by the content hash, never extracts or tokenises anything, and records the
        new stat of files whose content is the same. {"needed": bool, "why": str, **the listing's counts}; anything it
        cannot settle in time (or a locked or damaged index) counts as needed."""
        def run():
            try:
                cands, known, check, removed, counts = self._plan(Path(project).resolve(), deadline)
            except ListingTimeout:
                return {"needed": True, "why": "time"}
            if removed:
                return {"needed": True, "why": "removed", **counts}
            touched = []
            for rel in check:
                if time.perf_counter() > deadline:
                    return {"needed": True, "why": "time", **counts}
                k = known.get(rel)
                p = project_path(str(project), rel)
                data = read_regular(p, MAX_TEXT_BYTES) if p is not None else None
                if data is None or b"\0" in data[:8192]:      # gone or binary: a sync drops a known one
                    if k:
                        return {"needed": True, "why": "removed", **counts}
                    continue
                if k is None or normalized_hash(data) != k[3]:
                    return {"needed": True, "why": "changed", **counts}
                size, mt = cands[rel]
                touched.append((size, mt, time.time_ns(), rel))
            if touched:
                self._tx(lambda c: c.executemany("UPDATE files SET size=?, mtime_ns=?, indexed_ns=? WHERE rel=?",
                                                 touched))
            return {"needed": False, "why": "", **counts}
        return self._guard(run, default=None) or {"needed": True, "why": "index"}

    def file_count(self) -> int | None:
        return self._guard(lambda: self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], default=None)

    def _reap_marks(self) -> int:
        """Content a process was working on when it died (its in-progress mark is still there). When devmem.refresh
        killed that process at its time budget (a stamp `stopped.<pid>` next to the mark), or the same content was
        found marked once before, it is demoted: extracted content is from now on indexed as text only (the slow-file
        memo), content that stopped a process even as text only is indexed by its path only. A process that died for
        another reason (its parent was killed, the user cancelled) only makes the content a suspect, tried again the
        same way once. Marks of live processes (and of this process's other threads) are left alone; a mark file that
        is not a mark is removed, and so is a stamp without a mark. Returns how many contents were demoted."""
        import threading
        if self.degraded:       # an in-memory index (the store's index/ is unusable or a link): no marks
            return 0
        folder = self.path.parent
        try:
            names, stamps = [], set()
            with os.scandir(folder) as it:
                for e in it:
                    if e.name.startswith(MARK_PREFIX) and len(names) < MARKS_READ:
                        names.append(e.name)
                    elif e.name.startswith(STAMP_PREFIX) and len(stamps) < MARKS_READ:
                        stamps.add(e.name)
        except OSError:
            return 0
        if not names and not stamps:
            return 0
        own = {t.ident for t in threading.enumerate()}
        dead: list[tuple[Path, str | None, str | None, bool]] = []
        marked_pids = set()
        for name in names:
            p = folder / name
            got = _read_mark(p)
            if got is None:
                dead.append((p, None, None, False))
                continue
            pid, tid, mark = got
            marked_pids.add(pid)
            at = mark["t"] if mark else 0.0
            if pid == os.getpid():
                gone = tid not in own
            else:
                gone = _pid_gone(pid)
            if gone or (mark is not None and time.time() - at > MARK_STALE_S):
                dead.append((p, mark["h"] if mark else None, mark["level"] if mark else None,
                             f"{STAMP_PREFIX}{pid}" in stamps))
        suspects = self._hash_list("suspect_extract")
        tagged = [(f"{level}:{h}", h, level, killed) for _p, h, level, killed in dead
                  if h and level in ("full", "plain", *MAP_MARK_LEVELS)]
        demoted = {tag for tag, _h, _l, killed in tagged if killed or tag in suspects}
        demote = list(dict.fromkeys((h, level) for tag, h, level, _k in tagged
                                    if tag in demoted and level not in MAP_MARK_LEVELS))
        # the map build (devmem.scan) stopped a process: the next maps are built at a lower level (see map_level)
        map_to = max((MAP_MARK_LEVELS.index(level) + 1 for tag, _h, level, _k in tagged
                      if tag in demoted and level in MAP_MARK_LEVELS), default=0)
        if map_to > self.map_level():
            self.meta_set("map_level", json.dumps({"level": map_to, "t": time.time()}))
            self.map_stopped = map_to
        new_suspects = [x for x in suspects if x not in demoted]
        for tag, _h, _l, _k in tagged:
            if tag not in demoted and tag not in new_suspects:
                new_suspects.append(tag)
        if demote or new_suspects != suspects:
            slow_list, skip_list = self._hash_list("slow_extract"), self._hash_list("skip_extract")
            for h, level in demote:
                target = slow_list if level == "full" else skip_list
                if h not in target:
                    target.append(h)

            def body(c):
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('slow_extract', ?)",
                          (json.dumps(slow_list[-SLOW_KEPT:]),))
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('skip_extract', ?)",
                          (json.dumps(skip_list[-SLOW_KEPT:]),))
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('suspect_extract', ?)",
                          (json.dumps(new_suspects[-SLOW_KEPT:]),))
            self._tx(body)          # recorded before the marks go: a kill in between only repeats this
        reaped = {p.name for p, _h, _l, _k in dead}
        live_pids = marked_pids - {int(n[len(MARK_PREFIX):].split(".")[0]) for n in reaped
                                   if n[len(MARK_PREFIX):].split(".")[0].isdigit()}
        for p in [p for p, _h, _l, _k in dead] + [folder / s for s in stamps
                                                 if not s[len(STAMP_PREFIX):].isdigit()
                                                 or int(s[len(STAMP_PREFIX):]) not in live_pids]:
            try:
                os.unlink(p)
            except OSError:
                pass
        return len(demote)

    def _sync(self, project: Path, deadline: float | None = None) -> dict:
        cands, known, check, removed, counts = self._plan(project, git_by=deadline)   # a git that hangs ends in time
        reaped = self._reap_marks()
        if reaped:
            counts["stopped_files"] = reaped
        if self.map_stopped:
            counts["stopped_map"] = self.map_stopped
        touched, writes = [], []
        mark = None if self.degraded else _Mark(self.path.parent)
        last_flush = time.perf_counter()

        def flush():
            nonlocal last_flush
            last_flush = time.perf_counter()
            if not (removed or touched or writes):
                return

            def body(c):
                if removed or writes:   # the map no longer matches: a process killed before it rebuilt the map
                    c.execute("DELETE FROM meta WHERE key='map_state'")    # must not leave the old one "current"
                for rel in removed:
                    c.execute("DELETE FROM files WHERE rel=?", (rel,))
                    self._remove_doc(c, KINDS["file"], rel)
                c.executemany("UPDATE files SET size=?, mtime_ns=?, indexed_ns=? WHERE rel=?", touched)
                for rel, size, mt, h, info, facts, tf, length, meta in writes:
                    c.execute("INSERT OR REPLACE INTO files(rel, size, mtime_ns, indexed_ns, hash, info, facts) "
                              "VALUES (?,?,?,?,?,?,?)", (rel, size, mt, time.time_ns(), h,
                                                         json.dumps(info, sort_keys=True),
                                                         json.dumps(facts, sort_keys=True)))
                    self._write_doc(c, KINDS["file"], rel, tf, length, meta)
            self._tx(body)
            counts["removed"] += len(removed)
            removed.clear()
            touched.clear()
            writes.clear()

        items = [(rel, known[rel][3] if rel in known else None) for rel in check]
        slow_list = self._hash_list("slow_extract")
        slow = set(slow_list)
        skip = set(self._hash_list("skip_extract"))
        try:
            for n, (rel, status, payload) in enumerate(_prepare_all(str(project), items, slow, skip,
                                                                    mark.set if mark else None)):
                if mark and status == "new":
                    mark.clear()            # the work on that content ended
                size, mt = cands[rel]
                k = known.get(rel)
                if status != "gone":
                    counts["hashed"] += 1
                if status in ("gone", "binary"):
                    if k:
                        removed.append(rel)
                    continue
                if status == "same":
                    touched.append((size, mt, time.time_ns(), rel))
                    counts["unchanged"] += 1
                    continue
                assert payload is not None  # only "gone" and "binary" carry no payload, and they continued above
                h, info, facts, tf, length, took = payload
                info["bytes"] = size
                writes.append((rel, size, mt, h, info, facts, tf, length,
                               {"lang": info["lang"], "lines": info["lines"]}))
                counts["changed" if k else "added"] += 1
                if took > SLOW_EXTRACT_S and h not in slow:
                    # remembered by content: the next sync (or a rebuild) indexes this file as text only
                    slow.add(h)
                    slow_list = (slow_list + [h])[-SLOW_KEPT:]
                    self.meta_set("slow_extract", json.dumps(slow_list))
                    counts["slow_files"] = counts.get("slow_files", 0) + 1
                past = deadline is not None and time.perf_counter() > deadline
                # committed every BATCH files, and at least every FLUSH_S seconds (a process killed at its time
                # budget loses at most that much work), file by file past the deadline or after a slow file
                if len(writes) >= BATCH or past or took > SLOW_EXTRACT_S or \
                        time.perf_counter() - last_flush > FLUSH_S:
                    flush()
                if past and n + 1 < len(items):
                    counts["incomplete"] = True     # out of time (a hook or the MCP server): the rest waits
                    counts["pending"] = len(items) - n - 1
                    break
            flush()
        finally:
            if mark:
                mark.close()        # an error or an interrupt is not a stuck file: no mark is left behind
        counts["files"] = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        return counts
