"""Tool-agnostic capture: works with any AI coding tool (Codex, Cursor, Aider, Gemini CLI, ...).

DevMemory cannot read every tool's private transcript, so this adapter records what it can
observe directly: the user's prompt, the project's text files before and after the session
(the diff becomes the session's edits), the tool's visible output, and, where the tool
offers a documented export (e.g. `codex exec --json` / `-o`), its events and final message.
The record has the same shape as a Claude Code capture, so packs, retrieval and line-level
freshness work unchanged. Fields the tool did not expose are left empty, never guessed.

  devmem wrap --prompt "<task>" -- <command ...>   run any CLI agent; {prompt}/{pack_file} in its args
  devmem begin / devmem end --prompt "<task>"      bracket a session in an IDE tool (Cursor, Copilot)
  devmem codex "<task>"                            Codex CLI adapter (pack in, JSONL events captured)

Before/after state: in a git work tree only the files git reports as changed are read, and the "before"
text of a file that was clean comes from the commit that was HEAD at the start, so nothing is copied up
front. Outside git, the whole-project text snapshot is the fallback. Records are schema 2 (one turn) and
every text is redacted (devmem/redact.py) before it is written. What a begin and an end read, redact, run and write
is bounded (the constants above _Budget); past a bound the record lists the files it did not compare.

Nothing unredacted is written to the store at any point, including the pending begin (`pending/<id>.json`,
which waits on disk until `end`): for each file it keeps a hash of the text and a REDACTED copy, and
secret-pattern files (.env, *.pem, ...) are never read. `end` compares hashes to find the changed files and
diffs redacted text against redacted text (a clean file's "before" comes from git and is redacted the same
way). What the diff cannot show: a change that lies entirely inside a redacted span (a rotated API key in
settings.py); the file is still listed as edited, without an Edit entry for that change.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from . import proc
from . import sessions as S
from .redact import Redactor
from .store import (MAX_TEXT_BYTES, PRIVATE_FILE, STORE_FILE_CAP, ensure_store, find_program, git_run, hash_files, is_ignored, iter_files,
                    load_ignores, private_mkdir, project_path, read_regular, read_store_text, safe_rel, store_path)

NO_CAPTURE_ENV = "DEVMEM_NO_CAPTURE"  # devmem.hooks records nothing in a session devmem launches and records

MAX_SNIPPET = S.TEXT_CAP  # per old/new text, with `truncated` (design doc: 4,000 characters)


def refusal(project: Path, store: Path) -> str | None:
    """Why `store` is not this user's (devmem.consent.store_refusal), or None: then nothing is recorded there."""
    from .consent import store_refusal
    return store_refusal(Path(project), Path(store))


def _mine(project: Path, store: Path) -> Path:
    """The store a recording command writes, made with its .gitignore; StoreRefused (nothing created or written) when
    it is a store this user did not create or adopt."""
    from .store import StoreRefused, check_store_links
    check_store_links(Path(store))      # StoreLinkError first: the more specific reason
    why = refusal(project, store)
    if why:
        raise StoreRefused(f"nothing recorded: {why}")
    return ensure_store(Path(store))


def _pack(project: Path, store: Path, prompt: str, budget: int) -> str:
    """The pack `wrap` and `codex` give the agent. For a store that is not this user's, a pack of the project's files
    only, from a scan with a time limit (devmem.pack._project_only_pack): when the limit cut it short, one line says so
    on stderr (the agent still starts)."""
    from .pack import build_pack
    text, info = build_pack(project, store, prompt, budget=budget)
    if info.get("files_only_scan") in ("stopped", "incomplete", "busy", "failed", "skipped"):
        from . import term
        term.say(scan_cut_note(info), file=sys.stderr)
    return text


def scan_cut_note(info: dict) -> str:
    """The line for a files-only pack whose scan did not finish."""
    state = info.get("files_only_scan")
    if state in ("stopped", "incomplete"):
        return (f"devmem: the project scan for this pack stopped at its {info.get('files_only_seconds', '?'):g} s limit; "
                "the pack covers the files read so far, and the next run goes on from there")
    return f"devmem: the project scan for this pack did not run ({state}); the pack covers the files mapped before"


def refused_note(why: str) -> str:
    """The one line `wrap` and `codex` print before running the agent in a store that is not this user's."""
    return f"devmem: this run is not recorded and its context pack lists project files only. {why}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_rel(project: Path, rel: str) -> str | None:
    """The UTF-8 text of project file `rel`, or None. `rel` comes from git or a record, so it must be a plain path
    inside the project (store.project_path: no `..`, absolute, drive or UNC name, no linked folder on the way, not
    another spelling of a file), and the file a regular file of at most MAX_TEXT_BYTES (store.read_regular: never a
    link, FIFO or device)."""
    return _read_now(project, rel)[1]


def _read_now(project: Path, rel: str) -> tuple[str, str | None, int]:
    """(what project file `rel` is now: "absent", "text" or "other" (there, but not a regular UTF-8 text file of at most
    MAX_TEXT_BYTES, or behind a link), its text, the bytes read). Same rules as _read_rel."""
    p = project_path(project, rel)
    if p is None:
        return "other", None, 0
    data = read_regular(p, MAX_TEXT_BYTES)
    if data is None:
        try:
            os.lstat(p)
        except (FileNotFoundError, NotADirectoryError):
            return "absent", None, 0
        except (OSError, ValueError):
            return "other", None, 0
        return "other", None, 0
    try:
        return "text", data.decode("utf-8").replace("\r\n", "\n"), len(data)
    except UnicodeDecodeError:
        return "other", None, len(data)


def snapshot(project: Path) -> dict[str, str]:
    """Text content of every indexable file (same ignores as the scanner; secrets excluded). Not bounded: begin and
    end use the bounded walk in start_state / end_state instead."""
    snap = {}
    for rel in iter_files(project):
        t = _read_rel(project, rel)
        if t is not None:
            snap[rel] = t
    return snap


# ---- bounds: what one begin and one end read, redact, run and write is fixed, whatever the work tree holds ----
# (the hooks' bounds on git state are devmem.sessions.STORED_PATHS and its neighbours.) An ordinary session, a few to a
# few hundred changed files of ordinary size, stays under every bound and is recorded exactly as before. Past a bound
# nothing is guessed: a file whose start could not be kept, or whose change was not compared, is listed in the turn's
# `files_not_compared` (at most sessions.STORED_PATHS names; `files_not_compared_more` counts the rest, and
# `not_compared_why` says which bounds were met); it is never shown as written by the session. `begin`, `end` and the
# MCP tools say so in their result (begin_note, end_note). Each bound is read when a begin or an end starts.
MAX_COMPARED_FILES = 2_000            # files whose text one begin keeps; files one end compares (diffs)
MAX_COMPARED_NAME_CHARS = 1_000_000   # characters of the names of those files, together
MAX_REDACT_CHARS = 8_000_000          # characters of project text one begin, and one end, redacts (0.2-0.5 MB/s here)
MAX_READ_BYTES = 128 * 1024 * 1024    # bytes of project files one begin, and one end, reads (to hash or compare)
MAX_READ_FILES = 5_000                # project files one begin, and one end, opens (a fresh file: ~8 ms on Windows)
MAX_PENDING_BYTES = 32 * 1024 * 1024  # one pending begin file (under store.STORE_FILE_CAP, the most `end` reads)
MAX_UNKNOWN_IDS = 100_000             # files a begin keeps only as an 8-byte hash of the name (sessions.path_id)
MAX_UNLISTED = 1_000_000              # files past those a begin counts (`unlisted`); it stops listing there
GIT_STATUS_TIMEOUT_S = 30.0           # `git status` of one begin or end (a hook gives it 5 s: sessions.git_status)
GIT_SHOW_BUDGET_S = 30.0              # seconds one end spends in `git show` for the start text of clean files (no
GIT_SHOW_TIMEOUT_S = 10.0             # call starts past it); one `git show` (so at most budget + this in all)
MAX_DIFF_STEPS = 4_000_000            # steps of one file's line diff (_Counted: positions visited, a count, not a clock)
MAX_DIFF_STEPS_TOTAL = 16_000_000     # steps of all line diffs of one record
MAX_RECORD_EDITS = 2_000              # Edit/Write/Delete entries in one record (each old/new text capped: MAX_SNIPPET)
MAX_EDIT_CHARS = 2_000_000            # characters of old/new text and file names in them
MAX_UDIFF_CHARS = 4_000_000           # characters of the unified diff in the raw record
MAX_EVENTS_CHARS = 2_000_000          # characters of a tool's JSON events (codex --json) in the raw record
MAX_ERRORS = 50                       # errors kept from those events (each cut to 300 characters)
PROMPT_CAP = 100_000                  # characters of a prompt or summary kept (as devmem.hooks.PROMPT_CAP)

# why a file is listed in files_not_compared
WHY_START_NOT_KEPT = "its state at begin was not kept (a begin bound)"
WHY_START_TEXT_NOT_KEPT = "only a hash of its text at begin was kept (a begin bound)"
WHY_START_TOO_LARGE = "git listed more changed paths at begin than devmem keeps (sessions.MAX_STATUS_ENTRIES)"
WHY_END_TOO_LARGE = "git listed more changed paths at end than devmem compares; files changed only then are not listed"
WHY_END_STATUS_FAILED = ("git could not list the changed files at end (it failed or took longer than "
                         "GIT_STATUS_TIMEOUT_S); only the files changed at begin and in commits are compared")
WHY_RANGE = "git could not list the files changed by the commits made since begin; those files are not listed"
WHY_NOT_TEXT_START = "not text at begin (binary, over MAX_TEXT_BYTES or not a plain file)"
WHY_NOT_TEXT_NOW = "not text now (binary, over MAX_TEXT_BYTES or not a plain file)"
WHY_FILES = "MAX_COMPARED_FILES or MAX_COMPARED_NAME_CHARS"
WHY_CHARS = "MAX_REDACT_CHARS"
WHY_READ = "MAX_READ_BYTES or MAX_READ_FILES"
WHY_SHOW = "GIT_SHOW_BUDGET_S"
WHY_SHOW_FAILED = "git could not show its text at begin"
WHY_NOT_SEEN = ("not seen at begin, and its modification time is not inside the session (ignored then?): it is not "
                "recorded as created")
MTIME_SLACK_S = 2.0            # a modification time this far past the end still counts as inside (coarse timestamps)
WHY_NOT_LISTED_NOW = "kept at begin but not listed now (ignored now?)"


class _Budget:
    """What one begin or one end may still spend (the bounds above, read now so that a test can lower them)."""

    def __init__(self) -> None:
        self.files, self.names = MAX_COMPARED_FILES, MAX_COMPARED_NAME_CHARS
        self.chars, self.read, self.show = MAX_REDACT_CHARS, MAX_READ_BYTES, GIT_SHOW_BUDGET_S
        self.opens = MAX_READ_FILES

    def take(self, rel: str, chars: int) -> str | None:
        """Spend one compared file named `rel` with `chars` characters to redact; None, or why it does not fit."""
        if self.files <= 0 or len(rel) > self.names:
            return WHY_FILES
        if chars > self.chars:
            return WHY_CHARS
        self.files, self.names, self.chars = self.files - 1, self.names - len(rel), self.chars - chars
        return None


# ---- before/after tracking: git when available, a bounded snapshot otherwise ----

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _entry(text: str | None, red: Redactor) -> dict | None:
    """What a begin keeps of one file: a hash of its text and a redacted copy (None: absent or not text)."""
    if text is None:
        return None
    return {"sha": _sha(text), "text": red.text(text)}


def _json_size(rel: str, entry) -> int:
    return len(json.dumps({rel: entry}, ensure_ascii=False).encode("utf-8", errors="surrogatepass"))


def _start_entries(project: Path, rels, red: Redactor, info: dict) -> dict:
    """The begin's entries for `rels` (sorted names), within the bounds: {"sha", "text"} (a hash of the text and a
    redacted copy) while MAX_COMPARED_FILES, MAX_REDACT_CHARS and MAX_PENDING_BYTES allow, then {"sha"} (a hash only),
    {"unread": True} for a file that is there but not text, None for an absent file; past MAX_BEGIN_FILES entries or
    MAX_READ_BYTES / MAX_READ_FILES read, the name's 8-byte hash in `unknown` (MAX_UNKNOWN_IDS of them; more are
    counted in `unlisted`).
    Returns {"entries", "unknown", "unlisted"}; `info` gets the counts (begin_note)."""
    b = _Budget()
    room = MAX_PENDING_BYTES - 16 * MAX_UNKNOWN_IDS - 64 * 1024      # what is left after the ids and the fixed fields
    entries: dict = {}
    ids: list[str] = []
    size, unlisted, hashed = 0, 0, 0

    def unknown(rel: str) -> None:
        nonlocal unlisted
        if len(ids) < MAX_UNKNOWN_IDS:
            ids.append(S.path_id(rel).hex())
        else:
            unlisted += 1

    for rel in rels:
        if unlisted >= MAX_UNLISTED:
            break                           # end treats every file it cannot place as unknown: the count is enough
        if len(entries) >= MAX_BEGIN_FILES or size >= room or b.read <= 0 or b.opens <= 0:
            unknown(rel)
            continue
        state, text, n = _read_now(project, rel)
        b.read, b.opens = b.read - n, b.opens - 1
        entry: dict | None = None
        if state == "other":
            entry = {"unread": True}
        elif state == "text" and text is not None:
            entry = {"sha": _sha(text)}
            if size + len(text) + len(rel) < room and b.take(rel, len(text)) is None:
                full = {"sha": entry["sha"], "text": red.text(text)}
                if size + _json_size(rel, full) <= room:
                    entry = full
            if "text" not in entry:
                hashed += 1
        n = _json_size(rel, entry)
        if size + n > room:
            unknown(rel)
            continue
        entries[rel] = entry
        size += n
    info.update(kept=len(entries) - hashed, hashed=hashed, unknown=len(ids) + unlisted)
    return {"entries": entries, "unknown": "".join(ids), "unlisted": unlisted}


def start_state(project: Path, red: Redactor | None = None, info: dict | None = None) -> dict:
    """What `end_state` needs to compute the session's changes, safe to write to disk and of a bounded size: in a git
    work tree, HEAD plus the entries (_start_entries) of the files that were already dirty (usually a handful);
    otherwise the same for every text file. Secret-pattern files are never read. A work tree with more changed paths
    than git_status keeps is recorded as such (`status_too_large`), not snapshotted. `info` gets what was left out."""
    project = Path(project)
    red = red or Redactor(None)
    info = info if info is not None else {}
    status, too_large = S.git_status_sized(project, untracked="all", timeout=GIT_STATUS_TIMEOUT_S)
    if too_large:
        info.update(kept=0, hashed=0, unknown=0, status_too_large=True)
        return {"kind": "git", "v": 2, "head": S.git_head(project), "dirty": {}, "status_too_large": True}
    if status is None:
        rels = (rel for rel in iter_files(project) if not red.is_secret_file(rel))
        got = _start_entries(project, rels, red, info)
        return {"kind": "snapshot", "v": 2, "snapshot": got["entries"], **_unknown_fields(got)}
    pats = load_ignores(project)
    rels = sorted(rel for rel in status
                  # also a folder (`dir/`) and any name outside the project
                  if safe_rel(rel) is not None and not is_ignored(rel, pats) and not red.is_secret_file(rel))
    got = _start_entries(project, rels, red, info)
    return {"kind": "git", "v": 2, "head": S.git_head(project), "dirty": got["entries"], **_unknown_fields(got)}


def _unknown_fields(got: dict) -> dict:
    """The begin's fields for files it did not keep: only present when there are some (an ordinary begin is as before)."""
    out: dict = {}
    if got["unknown"]:
        out["unknown"] = got["unknown"]
    if got["unlisted"]:
        out["unlisted"] = got["unlisted"]
    return out


def _start_entry(entry, red: Redactor) -> tuple[str | None, str | None]:
    """(redacted text, hash of the original text) of a begin's entry; a v1 begin stored the raw text."""
    if entry is None:
        return None, None
    if isinstance(entry, str):
        return red.text(entry), _sha(entry)
    if isinstance(entry, dict) and isinstance(entry.get("text"), str):
        return entry["text"], entry.get("sha")
    return None, None


def _git_show(project: Path, rev: str | None, rel: str, timeout: float = GIT_SHOW_TIMEOUT_S) -> tuple[str, str | None]:
    """(what `rel` was in commit `rev`: "absent", "text", "other" (there, but not UTF-8 text of at most
    MAX_TEXT_BYTES) or "error" (git failed or did not answer in `timeout` s), its text)."""
    if not rev or safe_rel(rel) is None:      # `rev:./../x` would read another part of the repository
        return "absent", None
    from .store import GitOutputLimit
    try:
        if not _HEAD.fullmatch(rev):          # a commit id only: never an option
            return "absent", None
        p = git_run(project, "show", "--no-textconv", f"{rev}:./{rel}", timeout=timeout, max_out=MAX_TEXT_BYTES)
    except GitOutputLimit:
        return "other", None
    except (OSError, subprocess.SubprocessError):
        return "error", None
    if p.returncode != 0:
        return "absent", None
    if len(p.stdout) > MAX_TEXT_BYTES:
        return "other", None
    try:
        return "text", p.stdout.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        return "other", None


def _ids(start: dict) -> set[bytes]:
    got = start.get("unknown")
    hexes = got if isinstance(got, str) else ""
    try:
        return {bytes.fromhex(hexes[i:i + 16]) for i in range(0, len(hexes), 16)}
    except ValueError:
        return set()


def _since(start: dict) -> float | None:
    """The begin's time as a POSIX time: its `since` (taken before its state), or for a begin written before that field
    existed its `started_at` (whole seconds); None if unknown."""
    t = start.get("since")
    if isinstance(t, (int, float)) and not isinstance(t, bool) and 0 <= t < 1e11:
        return float(t)
    try:
        return datetime.strptime(str(start.get("started_at")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, OverflowError):
        return None


def _mtime(project: Path, rel: str) -> float | None:
    p = project_path(project, rel)
    try:
        return os.lstat(p).st_mtime if p is not None else None
    except (OSError, ValueError):
        return None


def end_state(project: Path, start: dict, red: Redactor | None = None,
              since: float | None = None) -> tuple[dict[str, str], dict[str, str], dict]:
    """(before, after, git info) for the files that changed since `start`, both sides REDACTED, so the diff
    is redacted text against redacted text. Unchanged files (same hash) are left out. git info carries
    `redacted_only_changes`: files whose change is invisible after redaction (record_generic lists them), and, past a
    bound, `not_compared` (names), `not_compared_unnamed` (a count), `not_compared_why` and `deleted_unseen` (deleted
    files whose start text was not kept), which record_generic turns into bounded fields of the turn, and `hashes_now`
    (the hash store.file_hash gives each compared file, from the text read here: the record does not open them again).

    A file the begin did not see (ignored then and listed now, or absent from its state for another reason) counts as
    created only when its modification time lies inside the session: at or after `since` (the begin's time, default
    its `since` or started_at) and not after now plus MTIME_SLACK_S. Any other time (before the begin, or in the
    future) is no evidence: the file is listed as not compared (WHY_NOT_SEEN), never as written by the session. The
    time is the file's own, which a project, an archive or a tool sets: one that writes a file with an old or future
    time makes it "not seen", and one that sets a time inside the session on a file that was there makes it count
    as created."""
    project = Path(project)
    red = red or Redactor(None)
    since = _since(start) if since is None else since
    until = time.time()            # the session ends now: a later modification time is not evidence of it either
    hashes: dict[str, str] = {}
    b = _Budget()
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    hidden: list[str] = []
    skipped: list[str] = []
    gone: list[str] = []
    why: set[str] = set()
    ids = _ids(start)
    matched = matched_unlisted = 0
    got = start.get("unlisted")
    unlisted: int = got if isinstance(got, int) else 0

    def skip(rel: str, reason: str) -> None:
        skipped.append(rel)
        why.add(reason)

    def visit(rel: str, entry, clean: bool, listed: bool = True) -> None:
        """Compare one file with its start: `entry` from the begin (clean=False) or, for a file that was clean, its
        text in the start commit (clean=True). listed=False: the file is not there now (snapshot listing)."""
        nonlocal matched, matched_unlisted
        if red.is_secret_file(rel):
            return
        if ids and S.path_id(rel) in ids:
            matched += 1
            return skip(rel, WHY_START_NOT_KEPT)
        if clean and start.get("status_too_large"):
            return skip(rel, WHY_START_TOO_LARGE)
        if unlisted and (clean or entry is _NOT_IN_START):   # the begin could not list every file: it may be one
            matched_unlisted += 1
            return skip(rel, WHY_START_NOT_KEPT)
        if clean:                                   # its start text needs a `git show`: not when it could not be used
            if b.files <= 0 or len(rel) > b.names:
                return skip(rel, WHY_FILES)
            if b.show <= 0:
                return skip(rel, WHY_SHOW)
        if not listed:                              # a begin's file the listing does not show now: ignored now?
            if _mtime(project, rel) is not None:
                return skip(rel, WHY_NOT_LISTED_NOW)
            now, a_raw = "absent", None
        elif b.read <= 0 or b.opens <= 0:
            return skip(rel, WHY_READ)
        else:
            now, a_raw, n = _read_now(project, rel)
            b.read, b.opens = b.read - n, b.opens - 1
        raw = None                                  # the start text, not yet redacted (git, or a v1 begin)
        b_red = b_sha = None
        if clean:
            if a_raw is not None and len(a_raw) > b.chars:
                return skip(rel, WHY_CHARS)
            t0 = time.monotonic()
            # each call gets its full limit: a call cut short by the budget would put git in this project on its
            # one-minute pause (store._git_exec) for every devmem process; so the last call may end past the budget
            got, raw = _git_show(project, head0, rel, timeout=GIT_SHOW_TIMEOUT_S)
            b.show -= time.monotonic() - t0
            if got == "error":
                return skip(rel, WHY_SHOW_FAILED)
            kind = {"absent": "absent", "other": "unread"}.get(got, "text")
            b_sha = _sha(raw) if raw is not None else None
        elif entry is None or entry is _NOT_IN_START:
            kind = "absent"
        elif isinstance(entry, str):                # v1: raw text
            kind, raw, b_sha = "text", entry, _sha(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("text"), str):
            kind, b_red, b_sha = "text", entry["text"], entry.get("sha")
        elif isinstance(entry, dict) and isinstance(entry.get("sha"), str):
            kind, b_sha = "hash", entry["sha"]
        else:
            kind = "unread"
        if now == "text" and a_raw is not None and b_sha is not None and _sha(a_raw) == b_sha:
            return                                  # unchanged
        if kind == "unread":
            return skip(rel, WHY_NOT_TEXT_START) if now == "text" else None   # neither side text: as before, not listed
        if kind == "hash":
            if now == "absent":
                gone.append(rel)
                return None
            return skip(rel, WHY_START_TEXT_NOT_KEPT)
        if now == "other":
            return skip(rel, WHY_NOT_TEXT_NOW) if kind == "text" else None
        if kind == "absent" and now == "absent":
            return None
        if kind == "absent" and (clean or entry is _NOT_IN_START):   # the begin did not see it: created, or older?
            mt = _mtime(project, rel)
            if since is None or mt is None or not since <= mt <= until + MTIME_SLACK_S:
                return skip(rel, WHY_NOT_SEEN)
        cost = (len(raw) if raw is not None else 0) + (len(a_raw) if a_raw is not None else 0)
        refused = b.take(rel, cost)
        if refused:
            return skip(rel, refused)
        if raw is not None:
            b_red = red.text(raw)
        a_red = red.text(a_raw) if a_raw is not None else None
        if b_red is not None:
            before[rel] = b_red
        if a_red is not None and a_raw is not None:
            after[rel] = a_red
            hashes[rel] = hashlib.sha256(a_raw.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
        if b_red is not None and a_red is not None and a_red == b_red:
            hidden.append(rel)
        return None

    def info(git: dict) -> dict:
        git["hashes_now"] = hashes
        if hidden:
            git["redacted_only_changes"] = hidden
        # not kept at begin and not named now (a name seen now that may be an unlisted one is named above)
        unnamed = len(ids) - matched + max(0, unlisted - matched_unlisted)
        if unnamed > 0:
            why.add(WHY_START_NOT_KEPT)
        if skipped or why or unnamed > 0:
            git.update(not_compared=skipped, not_compared_unnamed=max(0, unnamed), not_compared_why=sorted(why))
        if gone:
            git["deleted_unseen"] = gone
        return git

    head0 = None
    if start.get("kind") != "git":
        snap0 = start.get("snapshot") or {}
        listed = {rel for rel in iter_files(project) if not red.is_secret_file(rel)}
        for rel in sorted(r for r in set(snap0) | listed if safe_rel(r) is not None):
            visit(rel, snap0.get(rel, _NOT_IN_START), clean=False, listed=rel in listed)
        return before, after, info({})
    status, too_large_end = S.git_status_sized(project, untracked="all", timeout=GIT_STATUS_TIMEOUT_S)
    if status is None and not too_large_end:
        why.add(WHY_END_STATUS_FAILED)
    status = status or {}
    head0, head1 = start.get("head"), S.git_head(project)
    # only commit ids ever reach git's argv (a begin file is data: `--output=<path>` would be an option)
    head0 = head0 if isinstance(head0, str) and _HEAD.fullmatch(head0) else None
    head1 = head1 if isinstance(head1, str) and _HEAD.fullmatch(head1) else None
    dirty0 = start.get("dirty") or {}
    candidates = set(dirty0) | set(status)
    if too_large_end:
        why.add(WHY_END_TOO_LARGE)
    if head0 and head1 and head0 != head1:
        out = S._git(project, "-c", "core.quotepath=off", "diff", "--no-ext-diff", "--no-textconv", "--name-only",
                     "--relative", head0, head1, "--", timeout=GIT_SHOW_TIMEOUT_S, max_out=S.MAX_STATUS_BYTES)
        names = {l.strip() for l in (out or "").split("\n") if l.strip()}
        if out is None or len(names) > S.MAX_STATUS_ENTRIES:
            why.add(WHY_RANGE)
        else:
            candidates |= names
    pats = load_ignores(project)
    for rel in sorted(candidates):
        if safe_rel(rel) is None or is_ignored(rel, pats) or red.is_secret_file(rel):
            continue          # a folder, or a name outside the project (a hand-written index can hold `../x`)
        visit(rel, dirty0.get(rel), clean=rel not in dirty0)
    git = {"head_start": head0, "head_end": head1,
           "dirty_start": bool(dirty0) or bool(ids) or bool(unlisted) or bool(start.get("status_too_large")),
           "dirty_end": bool(status) or too_large_end}
    if start.get("status_too_large"):
        git["status_too_large_start"] = True
    if too_large_end:
        git["status_too_large_end"] = True
    return before, after, info(git)


_NOT_IN_START = object()      # a snapshot name the begin did not list (created since, or one it could not list)


def _capped(entry: dict, **texts) -> dict:
    trunc = False
    for k, v in texts.items():
        entry[k], t = S.cap(v, MAX_SNIPPET)
        trunc = trunc or t
    entry["truncated"] = trunc
    return entry


class _DiffBudget(Exception):
    """A line diff ran out of its steps (_Counted)."""


class _Counted(difflib.SequenceMatcher):
    """difflib.SequenceMatcher whose work is counted and cut. find_longest_match is difflib's (unchanged from Python
    3.10 to 3.13) with one difference that changes no result: the positions of a line in b below `blo` are skipped
    with a bisection instead of one by one. Every position it then visits, and every line a match is extended by, is a
    step taken from `left` (a one-item list shared by the diffs of one record); past zero it raises _DiffBudget. Within
    the budget its opcodes, and so the Edit entries and the unified diff, are SequenceMatcher's exactly. Its work is
    therefore at most the budget plus difflib's linear set-up (indexing b, popular lines) and its bookkeeping per
    match found."""

    def __init__(self, a: list[str], b: list[str], autojunk: bool, left: list[int]):
        self._left = left
        super().__init__(None, a, b, autojunk=autojunk)

    def find_longest_match(self, alo=0, ahi=None, blo=0, bhi=None):
        from bisect import bisect_left
        # SequenceMatcher's own attributes (set by set_seqs / __chain_b; the type stubs do not declare them)
        a, b, b2j, bjunk = (getattr(self, k) for k in ("a", "b", "b2j", "bjunk"))
        isbjunk = bjunk.__contains__
        if ahi is None:
            ahi = len(a)
        if bhi is None:
            bhi = len(b)
        left = self._left
        besti, bestj, bestsize = alo, blo, 0
        j2len: dict[int, int] = {}
        nothing: list[int] = []
        for i in range(alo, ahi):
            js = b2j.get(a[i], nothing)
            lo = bisect_left(js, blo) if js and js[0] < blo else 0
            hi = bisect_left(js, bhi, lo)
            left[0] -= hi - lo + 1
            if left[0] < 0:
                raise _DiffBudget
            j2lenget = j2len.get
            newj2len = {}
            for j in js[lo:hi]:
                k = newj2len[j] = j2lenget(j - 1, 0) + 1
                if k > bestsize:
                    besti, bestj, bestsize = i - k + 1, j - k + 1, k
            j2len = newj2len
        start = bestsize
        while besti > alo and bestj > blo and not isbjunk(b[bestj - 1]) and a[besti - 1] == b[bestj - 1]:
            besti, bestj, bestsize = besti - 1, bestj - 1, bestsize + 1
        while besti + bestsize < ahi and bestj + bestsize < bhi and not isbjunk(b[bestj + bestsize]) and \
                a[besti + bestsize] == b[bestj + bestsize]:
            bestsize += 1
        while besti > alo and bestj > blo and isbjunk(b[bestj - 1]) and a[besti - 1] == b[bestj - 1]:
            besti, bestj, bestsize = besti - 1, bestj - 1, bestsize + 1
        while besti + bestsize < ahi and bestj + bestsize < bhi and isbjunk(b[bestj + bestsize]) and \
                a[besti + bestsize] == b[bestj + bestsize]:
            bestsize = bestsize + 1
        left[0] -= bestsize - start + 1
        if left[0] < 0:
            raise _DiffBudget
        return difflib.Match(besti, bestj, bestsize)


def _unified(a: list[str], b: list[str], fromfile: str, tofile: str, left: list[int], n: int = 3) -> list[str]:
    """difflib.unified_diff(a, b, fromfile, tofile, lineterm="") (its code, Python 3.10 to 3.13) on a _Counted matcher:
    the same lines, within the steps in `left`."""
    out: list[str] = []
    for group in _Counted(a, b, True, left).get_grouped_opcodes(n):
        if not out:
            out += [f"--- {fromfile}", f"+++ {tofile}"]
        first, last = group[0], group[-1]
        out.append(f"@@ -{difflib._format_range_unified(first[1], last[2])} "    # type: ignore[attr-defined]
                   f"+{difflib._format_range_unified(first[3], last[4])} @@")   # type: ignore[attr-defined]
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                out += [" " + line for line in a[i1:i2]]
                continue
            if tag in ("replace", "delete"):
                out += ["-" + line for line in a[i1:i2]]
            if tag in ("replace", "insert"):
                out += ["+" + line for line in b[j1:j2]]
    return out


def _ends(al: list[str], bl: list[str]) -> tuple[int, int]:
    """(common first lines, common last lines) of al and bl."""
    n = min(len(al), len(bl))
    pre = 0
    while pre < n and al[pre] == bl[pre]:
        pre += 1
    suf = 0
    while suf < n - pre and al[-1 - suf] == bl[-1 - suf]:
        suf += 1
    return pre, suf


def diff_edits(before: dict[str, str], after: dict[str, str], info: dict | None = None) -> tuple[list[dict], list[str], str]:
    """Per-hunk Edit entries (old/new text), changed files, and a unified diff for the raw record.
    A created file is a Write (its text in `new`), a removed file a Delete; texts are capped with `truncated`.
    Bounded: the line diff of one file may take MAX_DIFF_STEPS steps and those of one record MAX_DIFF_STEPS_TOTAL
    (_Counted: a count of the work, not a clock); a file whose diff runs out gets one Edit of the lines between its
    common first and last lines, marked `not_line_diffed`, and a note in the unified diff instead of hunks. Past
    MAX_RECORD_EDITS entries or MAX_EDIT_CHARS characters the remaining entries are left out, past MAX_UDIFF_CHARS the
    remaining files' diffs. `info` gets the counts left out, and `diff_steps` the steps taken."""
    edits, changed, udiff = [], [], []
    total, edit_chars, udiff_chars = MAX_DIFF_STEPS_TOTAL, MAX_EDIT_CHARS, MAX_UDIFF_CHARS
    left_out = udiff_left = coarse = 0
    for rel in sorted(set(before) | set(after)):
        a, b = before.get(rel), after.get(rel)
        if a == b:
            continue
        changed.append(rel)
        al, bl = (a or "").splitlines(), (b or "").splitlines()
        left = [min(MAX_DIFF_STEPS, max(0, total))]
        given = left[0]
        lines: list[str] = []
        try:
            if a is None or b is None:      # one side only: no matching to do, linear
                mine = [_capped({"tool": "Write", "file": rel, "op": "create"}, new=b or "") if a is None
                        else _capped({"tool": "Delete", "file": rel}, old=a)]
                if udiff_chars > 0:
                    lines = list(difflib.unified_diff(al, bl, f"a/{rel}", f"b/{rel}", lineterm=""))
            else:
                mine = [_capped({"tool": "Edit", "file": rel}, old="\n".join(al[i1:i2]), new="\n".join(bl[j1:j2]))
                        for op, i1, i2, j1, j2 in _Counted(al, bl, False, left).get_opcodes() if op != "equal"]
                if udiff_chars > 0:
                    lines = _unified(al, bl, f"a/{rel}", f"b/{rel}", left)
        except _DiffBudget:
            coarse += 1
            pre, suf = _ends(al, bl)
            mine = [_capped({"tool": "Edit", "file": rel, "not_line_diffed": True},
                            old="\n".join(al[pre:len(al) - suf]), new="\n".join(bl[pre:len(bl) - suf]))]
            lines = [f"--- a/{rel}", f"+++ b/{rel}",
                     f"# devmem: changed ({len(al)} -> {len(bl)} lines); not diffed line by line (MAX_DIFF_STEPS or "
                     "MAX_DIFF_STEPS_TOTAL)"]
        total -= given - max(0, left[0])
        for e in mine:
            n = len(e.get("old") or "") + len(e.get("new") or "") + len(rel)
            if len(edits) >= MAX_RECORD_EDITS or n > edit_chars:
                left_out += 1
                continue
            edit_chars -= n
            edits.append(e)
        n = sum(len(x) + 1 for x in lines)
        if lines and n <= udiff_chars:
            udiff += lines
            udiff_chars -= n
        elif lines or udiff_chars <= 0:
            udiff_left += 1
    if udiff_left:
        udiff.append(f"# devmem: the diffs of {udiff_left} more files are not kept here (MAX_UDIFF_CHARS)")
    if info is not None:
        info.update({k: v for k, v in (("edits_left_out", left_out), ("diff_left_out", udiff_left),
                                       ("not_line_diffed", coarse)) if v})
        info["diff_steps"] = MAX_DIFF_STEPS_TOTAL - total
    return edits, changed, "\n".join(udiff)


# The fields of a tool event's item (Codex `--json`: command_execution, file_change, mcp_tool_call, ...) kept as they
# are when the item names a secret file, when they hold a number or a plain word: what it was. Of the rest, a path is
# kept, a command keeps its program and the secret file names (redact.secret_call_input), and nothing else is.
_SECRET_ITEM_KEEPS = {"id", "type", "status", "exit_code", "server", "tool", "name"}
_PLAIN_FIELD = re.compile(r"[A-Za-z0-9_.:@/-]{1,128}")


def _strings_without_secret_lines(x, red: Redactor):
    if isinstance(x, str):
        return red.omit_secret_file_lines(x)
    if isinstance(x, list):
        return [_strings_without_secret_lines(v, red) for v in x]
    if isinstance(x, dict):
        return {k: _strings_without_secret_lines(v, red) for k, v in x.items()}
    return x


def _tool_event(e: dict, red: Redactor, project: Path) -> dict:
    """A tool's event as it may be stored, by the rule of stream capture (devmem.capture._Calls): an event whose item
    reads or writes a secret file (redact.call_names_secret_file: a path field names one, or its command does: `cat
    .env`, `echo X >> .env`) keeps what it was (_SECRET_ITEM_KEEPS), its paths, and of its command the program and the
    secret file names only (redact.secret_call_input); its output and any other text are OMITTED_SECRET_FILE. An
    event too large to check (redact.call_decision "unchecked") keeps what it was and the fact, the reason and
    the size (redact.unchecked_input), nothing of its input or output. In any other event, every part that names a
    secret file keeps its paths only (redact.scrub_secret_parts) and the lines of secret files are removed from every
    string (`grep -rn KEY .`). Redaction is the writer's next step."""
    from .redact import call_decision, scrub_secret_parts, secret_call_input, unchecked_input

    def kept(d: dict, decision: str) -> dict:
        out = unchecked_input(d) if decision == "unchecked" else secret_call_input(d, red.secret_patterns)
        for k in _SECRET_ITEM_KEEPS:
            v = d.get(k)
            if k in d and (not isinstance(v, (str, list, dict)) or isinstance(v, str) and _PLAIN_FIELD.fullmatch(v)):
                out[k] = v
        return out
    item = e.get("item")
    if isinstance(item, dict):
        d = call_decision("", item, project, red.secret_patterns)
        if d != "ok":
            return {**e, "item": kept(item, d)}
    d = call_decision("", e, project, red.secret_patterns)
    if d != "ok":
        return kept(e, d)
    out = _strings_without_secret_lines(scrub_secret_parts(e, project, red.secret_patterns), red)
    return out if isinstance(out, dict) else {}


def record_generic(project: Path, prompt: str, before: dict, after: dict, started_at: str, provider: str,
                   store: Path | None = None, result_text: str | None = None, output_text: str = "",
                   events: list[dict] | None = None, model: str | None = None, usage: dict | None = None,
                   command: list[str] | None = None, summary_source: str | None = None,
                   errors: list[str] | None = None, git: dict | None = None, extra: dict | None = None,
                   prompt_source: str = S.PROMPT_FROM_USER, info: dict | None = None,
                   output_cut: dict | None = None) -> Path:
    """Write a one-turn schema-2 record. `prompt_source` says who wrote `prompt`: "user" (the default: the
    CLI `end --prompt`, `wrap`, `codex`) or "reported-by-client" (MCP end_session: the AI client's text).
    Its size is bounded whatever the session changed (the bounds above diff_edits); `info` gets what was left out
    (end_note)."""
    project = Path(project)
    store = ensure_store(Path(store or store_path(project)))
    red = Redactor(store)
    sid = uuid.uuid4().hex
    base = S.session_base(store, started_at, sid)
    git = dict(git or {})
    hidden = [f for f in git.pop("redacted_only_changes", None) or [] if not red.is_secret_file(f)]
    hashes_now = git.pop("hashes_now", None) or {}
    skipped = [f for f in git.pop("not_compared", None) or [] if not red.is_secret_file(f)]
    unnamed = git.pop("not_compared_unnamed", 0)
    why = git.pop("not_compared_why", None) or []
    gone = [f for f in git.pop("deleted_unseen", None) or [] if not red.is_secret_file(f)]
    # secret files never reach the record, even if the caller's snapshot included them
    before = {k: v for k, v in before.items() if not red.is_secret_file(k)}
    after = {k: v for k, v in after.items() if not red.is_secret_file(k)}
    diffed: dict = {}
    edits, changed, udiff = diff_edits(before, after, diffed)
    # changed only inside redacted text: edited, but the redacted diff has nothing to show
    changed += [f for f in hidden if f not in changed and f in after]
    edits = [red.obj(e) for e in edits]
    prompt_r, prompt_cut = red.capped(prompt, PROMPT_CAP)
    result_r, result_cut = red.capped(result_text, PROMPT_CAP) if result_text else (result_text, False)
    raw: list[dict] = [{"type": "prompt", "text": prompt_r, "ts": started_at}]
    if command:
        raw.append({"type": "command", "argv": command})
    room, events_left = MAX_EVENTS_CHARS, 0
    for e in events or []:
        n = len(json.dumps(e, ensure_ascii=False))
        if n > room:
            events_left += 1
            continue
        room -= n
        raw.append({"type": "tool_event", "event": red.obj(_tool_event(e, red, project))})
    if events_left:
        raw.append({"type": "tool_events_left_out", "count": events_left, "limit": "MAX_EVENTS_CHARS"})
    if output_text:
        raw.append({"type": "output", "text": red.text(output_text), **({"left_out": output_cut} if output_cut else {})})
    if result_r:
        raw.append({"type": "final_message", "text": result_r, "source": summary_source or provider})
    ended = _now()
    changes = {"type": "file_changes", "files": changed, "unified_diff": red.text(udiff), "ts": ended}
    if diffed.get("diff_left_out"):
        changes["unified_diff_left_out"] = diffed["diff_left_out"]
    raw.append(changes)
    turn = S.new_turn(0, prompt_r, started_at)
    turn["prompt_source"] = prompt_source or S.PROMPT_FROM_USER
    deleted, deleted_more = S.bounded_paths(sorted(set(gone)))     # deleted, start text not kept: no Delete entry
    turn.update(
        tool_calls=edits,
        result_text=result_r,
        # who wrote result_text (tool output, assistant self-report, user); never left implicit
        result_source=(summary_source or provider) if result_text else "not available",
        assistant_text=[{"line": len(raw) - 2, "text": result_r, "source": summary_source or provider}]
        if result_text else [],
        files_edited=[c for c in changed if c in after],
        files_deleted=[c for c in changed if c not in after] + [f for f in deleted if f not in changed],
        errors=[{"error": red.capped(e, 2000)[0][:300]} for e in (errors or [])[:MAX_ERRORS]])
    # only when something was left out: an ordinary record keeps its shape
    if prompt_cut:
        turn["prompt_truncated"] = True
    if result_cut:
        turn["result_truncated"] = True
    if deleted_more:
        turn["files_deleted_more"] = deleted_more
    if skipped or unnamed or why:
        names, more = S.bounded_paths(sorted(set(skipped)))
        turn["files_not_compared"] = names
        if more + unnamed:
            turn["files_not_compared_more"] = more + unnamed
        turn["not_compared_why"] = list(why)
    for k in ("edits_left_out", "not_line_diffed"):
        if diffed.get(k):
            turn[k] = diffed[k]
    if len(errors or []) > MAX_ERRORS:
        turn["errors_left_out"] = len(errors or []) - MAX_ERRORS
    if events_left:
        turn["tool_events_left_out"] = events_left
    if output_cut:      # the middle of the program's output was not held (proc.CHILD_OUTPUT_HEAD / _TAIL)
        turn["output_left_out"] = dict(output_cut)
    rec = {
        "schema": S.SCHEMA,
        "session_id": sid,
        "started_at": started_at,
        "ended_at": ended,
        "provider": provider,
        "capture_method": "snapshot-diff" + (" + tool-json" if events else ""),
        "model": model,
        "git": git,
        "turns": [turn],
        "usage": usage or {},
        **(extra or {}),
    }
    S.derive_aggregates(rec, project, hash_now=False)
    rec["files_read"] = []  # not observable without the tool's transcript
    edited = [c for c in changed if c in after]
    # from the text end_state read (store.file_hash's value, without opening the file again); a caller that passed
    # its own before/after gets them hashed here
    rec["file_hashes_at_capture"] = {**hash_files(project, [c for c in edited if c not in hashes_now]),
                                     **{c: hashes_now[c] for c in edited if c in hashes_now}}
    rec["file_hashes_at_capture"] = dict(sorted(rec["file_hashes_at_capture"].items()))
    rec["result_source"] = turn["result_source"]
    if info is not None:
        info.update(edited=len(turn["files_edited"]), deleted=len(turn["files_deleted"]) + deleted_more,
                    not_compared=len(turn.get("files_not_compared") or []) + int(turn.get("files_not_compared_more")
                                                                                   or 0),
                    not_compared_why=list(why), **{k: turn[k] for k in ("edits_left_out", "not_line_diffed",
                                                                         "prompt_truncated", "result_truncated",
                                                                         "tool_events_left_out", "output_left_out")
                                                     if k in turn})
        if diffed.get("diff_left_out"):
            info["diff_left_out"] = diffed["diff_left_out"]
    lines = [json.dumps(r, ensure_ascii=False) for r in raw]
    return S.write_record(store, rec, raw_lines=lines, base=base, red=red)


def begin_note(info: dict) -> str | None:
    """The line `begin` (CLI and MCP) adds when its bounds left files out (start_state's `info`), else None."""
    if info.get("status_too_large"):
        return ("devmem: git lists more changed paths than devmem keeps (more than "
                f"{S.MAX_STATUS_ENTRIES:,}); the work tree at begin is not recorded, so `end` will list the changes "
                "it finds as not compared instead of attributing them to this session")
    hashed, unknown = info.get("hashed") or 0, info.get("unknown") or 0
    if not hashed and not unknown:
        return None
    return (f"devmem: {hashed + unknown:,} of the files changed at begin were not kept in full ({hashed:,} by a hash "
            f"only, {unknown:,} by name only: the begin bounds MAX_COMPARED_FILES, MAX_REDACT_CHARS, MAX_READ_BYTES, "
            "MAX_READ_FILES, MAX_PENDING_BYTES); if the session changes them, `end` lists them as not compared")


def end_note(info: dict) -> str | None:
    """The line `end`, `wrap` and `codex` (CLI) and end_session (MCP) add when the record left something out
    (record_generic's `info`), else None."""
    parts = []
    if info.get("not_compared"):
        parts.append(f"{info['not_compared']:,} changed files were not compared and are listed by name only, not as "
                     "edits (" + "; ".join(info.get("not_compared_why") or []) + ")")
    elif info.get("not_compared_why"):
        parts.append("; ".join(info["not_compared_why"]))
    if info.get("not_line_diffed"):
        parts.append(f"{info['not_line_diffed']:,} files were too large to diff line by line (one Edit each, marked "
                     "not_line_diffed)")
    if info.get("edits_left_out"):
        parts.append(f"{info['edits_left_out']:,} edits were left out (MAX_RECORD_EDITS / MAX_EDIT_CHARS)")
    if info.get("diff_left_out"):
        parts.append(f"the unified diff of {info['diff_left_out']:,} files was left out (MAX_UDIFF_CHARS)")
    if info.get("prompt_truncated") or info.get("result_truncated"):
        parts.append(f"the prompt or summary was cut to {PROMPT_CAP:,} characters")
    if info.get("tool_events_left_out"):
        parts.append(f"{info['tool_events_left_out']:,} tool events were left out (MAX_EVENTS_CHARS)")
    if info.get("output_left_out"):
        n = sum(info["output_left_out"].values())
        parts.append(f"{n:,} characters from the middle of the program's output were not kept "
                     "(proc.CHILD_OUTPUT_HEAD / CHILD_OUTPUT_TAIL)")
    return ("devmem: the record is bounded: " + "; ".join(parts)) if parts else None


# ---- begin / end: bracket a session in a tool DevMemory cannot launch (IDE agents) ----
# Each begin writes its own pending/<id>.json, so overlapping sessions never overwrite each other; end
# claims one atomically (os.replace), so two ends never consume the same begin. Overlapping sessions in the
# same working tree still see each other's edits: the diff is of the tree, not of one agent's actions.

def _pending_dir(store: Path) -> Path:
    return Path(store) / "pending"


def _pending(store: Path) -> Path:  # legacy single-slot file (v1); still honoured by end()
    return Path(store) / "pending.json"


_V2_HEAD = re.compile(rb'\{"id": "[A-Za-z0-9._-]{1,64}", "started_at": "[0-9TZ:.+\-]{1,40}", '
                      rb'"kind": "(?:git|snapshot)", "v": 2[,}]')
_V2_HEAD_BYTES = 256


def _is_current(f: Path) -> bool | None:
    """True when begin file `f` starts the way this version writes one (`{"id", "started_at", "kind", "v": 2`), read
    from its first bytes only, so that a large begin is not parsed by every begin, end and SessionStart; None when
    those bytes do not say (an older or a hand-made file: the caller parses it)."""
    from .store import _open_regular
    opened = _open_regular(f, None)
    if opened is None:
        return None
    fd, _size = opened
    try:
        head = os.read(fd, _V2_HEAD_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)
    return True if _V2_HEAD.match(head) else None


def secure_pending(store: Path, red: Redactor | None = None) -> list[str]:
    """Rewrite begin files written by older versions (which kept the raw text of files) in the current form,
    in place: a hash of each file's text and a redacted copy (within the begin bounds MAX_COMPARED_FILES and
    MAX_REDACT_CHARS; past them a hash only), secret-pattern files dropped. `end` works on them
    exactly as before (it compares hashes and diffs redacted text), and the file's modification time is kept,
    so `end` without an id still picks the most recent begin. The rewrite holds the same claim marker as
    `end`, so it never races one, and skips a begin an `end` is holding. Returns the ids rewritten.
    Called by `begin`, `end` and the SessionStart hook; `devmem status` can call it too. A begin this version wrote
    is recognised from its first bytes (_is_current) and not read further."""
    store = Path(store)
    d = _pending_dir(store)
    files = (sorted(d.glob("*.json")) if d.is_dir() else []) + [_pending(store)]
    done = []
    parsed, read_left = 0, SECURE_PENDING_BYTES
    for f in files:
        try:
            if not f.is_file() or _is_current(f):
                continue
            size = f.stat().st_size
            if parsed >= SECURE_PENDING_FILES or size > read_left:
                break                      # bounded per call (a hook calls this): the rest on a later call
            parsed, read_left = parsed + 1, read_left - 2 * size      # read here and again under the marker
            if json.loads(read_store_text(f)).get("v") == 2:
                continue
        except (OSError, ValueError, AttributeError, RecursionError):   # a nested-too-deep file: skipped
            continue
        marker = f.with_name(f.name + ".claim")
        if not _mark(marker):
            continue  # an `end` is using it right now
        try:
            red = red or Redactor(store)
            mtime = f.stat().st_mtime
            state = json.loads(read_store_text(f))  # re-read under the marker
            if not isinstance(state, dict) or state.get("v") == 2:
                continue
            key = "dirty" if state.get("kind") == "git" else "snapshot"
            state.setdefault("kind", "snapshot")
            found = state.get(key)
            entries = found if isinstance(found, dict) else {}
            b = _Budget()
            kept = {}
            for rel, v in entries.items():
                if red.is_secret_file(rel):
                    continue
                if isinstance(v, str):
                    kept[rel] = _entry(v, red) if b.take(rel, len(v)) is None else {"sha": _sha(v)}
                else:
                    kept[rel] = v if isinstance(v, dict) else None
            state[key] = kept
            state["v"] = 2
            first = {k: state.pop(k) for k in ("id", "started_at", "kind", "v") if k in state}
            S._atomic_write(f, json.dumps({**first, **state}, ensure_ascii=False))
            os.utime(f, (mtime, mtime))
            done.append(str(state.get("id") or first.get("id") or f.stem))
        except (OSError, ValueError, RecursionError):
            continue
        finally:
            _unmark(marker)
    return done


def begin(project: Path, store: Path | None = None, info: dict | None = None) -> Path:
    """Record the start state; returns the pending file (its stem is the id to pass to `end`). Its size is bounded
    (MAX_PENDING_BYTES, start_state); `info` gets what the bounds left out (begin_note)."""
    project = Path(project)
    store = _mine(project, Path(store or store_path(project)))
    secure_pending(store)
    d = _pending_dir(store)
    private_mkdir(d)
    pid = f"{S.stamp_of(_now())}_{uuid.uuid4().hex[:8]}"
    p = d / f"{pid}.json"
    t0 = time.time()     # before the state is taken: a file modified since then was written during the session
    state = {"id": pid, "started_at": _now(), **start_state(project, Redactor(store), info), "since": round(t0, 3)}
    S._atomic_write(p, json.dumps(state, ensure_ascii=False))
    return p


# Claiming a begin. Guarantee: each pending begin is consumed by at most one `end`, a begin is never
# deleted unless the `end` that holds it has written its record, and a begin that is free when an `end`
# starts looking is found by it. The claim is an exclusive create of `<begin>.claim` (O_CREAT|O_EXCL, i.e.
# CREATE_NEW on Windows), which is atomic on NTFS and POSIX file systems. A rename is NOT used as the
# claim: on Windows two concurrent renames of the same file can both succeed (each opens the source
# before either rename lands, then both rename that same file object), which handed one begin to two
# ends and made a loser delete it (reproduced under load before this change).
CLAIM_STALE_S = 3600  # a marker left by an `end` that crashed this long ago no longer blocks the begin


_T = TypeVar("_T")


def _retry(fn: Callable[[], _T], attempts: int = 40) -> _T:
    """Run fn, retrying transient Windows sharing violations (PermissionError) for up to about 2 s."""
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.01 + 0.002 * i)
    raise ValueError("attempts must be at least 1")


def _mark(marker: Path) -> bool:
    """Exclusive claim marker. True if this call created it."""
    for _ in range(2):
        try:
            fd = _retry(lambda: os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE))
        except FileExistsError:
            try:
                if time.time() - marker.stat().st_mtime > CLAIM_STALE_S:
                    marker.unlink()
                    continue
            except OSError:
                pass
            return False
        except OSError:
            return False
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        return True
    return False


def _unmark(marker: Path) -> None:
    try:
        _retry(lambda: marker.unlink(missing_ok=True))
    except OSError:
        pass


_HEAD = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_HEX = re.compile(r"[0-9a-f]*")
_STAMP = re.compile(r"[0-9TZ:.+\-]{1,40}")
MAX_BEGIN_FILES = 20_000       # entries in one begin file
MAX_CLAIM_FILES = 16           # begin files one `end` opens while looking for a usable one (newest first)
MAX_CLAIM_BYTES = 128 * 1024 * 1024   # bytes of begin files it reads doing so; past either it stops and says so
SECURE_PENDING_FILES = 4       # older-format begin files one secure_pending call parses (the rest: a later call)
SECURE_PENDING_BYTES = 64 * 1024 * 1024   # bytes it reads doing so
FUTURE_SLACK_S = 300           # a begin file dated further in the future than this is not taken as the newest


def validate_begin(state) -> str | None:
    """Why a begin file must not be used, or None. Everything in it reaches git or the record: the start commit is
    passed to `git diff`/`git show` (a value like `--output=<path>` would make git write a file), and the file names
    are opened. A begin file can come from a cloned repository or an archive, so every field is checked strictly."""
    if not isinstance(state, dict):
        return "not a JSON object"
    if state.get("kind", "snapshot") not in ("git", "snapshot"):
        return "unknown kind"
    if "id" in state and not S.safe_id(state["id"]):
        return "bad id"
    if not isinstance(state.get("started_at"), str) or not _STAMP.fullmatch(state["started_at"]):
        return "bad started_at"
    head = state.get("head")
    if head is not None and not (isinstance(head, str) and _HEAD.fullmatch(head)):
        return "the start commit is not a commit id"
    for field in ("dirty", "snapshot"):
        entries = state.get(field)
        if entries is None:
            continue
        if not isinstance(entries, dict) or len(entries) > MAX_BEGIN_FILES:
            return f"bad {field}"
        for rel, entry in entries.items():
            if safe_rel(rel) is None:
                return f"a file name in {field} is not a plain project path"
            if entry is None or (isinstance(entry, str) and len(entry) <= MAX_TEXT_BYTES):   # v1 kept raw text
                continue
            if entry == {"unread": True}:                       # there at begin, not text
                continue
            sha = entry.get("sha") if isinstance(entry, dict) else None
            if not (sha is None or (isinstance(sha, str) and _SHA256.fullmatch(sha))):
                return f"a bad entry in {field}"
            if isinstance(entry, dict) and set(entry) == {"sha"} and sha is not None:   # a hash only (a begin bound)
                continue
            if not (isinstance(entry, dict) and isinstance(entry.get("text"), str)
                    and len(entry["text"]) <= MAX_TEXT_BYTES * 2):
                return f"a bad entry in {field}"
    unknown = state.get("unknown")
    if unknown is not None and not (isinstance(unknown, str) and len(unknown) % 16 == 0
                                    and len(unknown) <= 16 * MAX_UNKNOWN_IDS and _HEX.fullmatch(unknown)):
        return "bad unknown"
    unlisted = state.get("unlisted")
    if unlisted is not None and not (type(unlisted) is int and 0 <= unlisted <= MAX_UNLISTED):
        return "bad unlisted"
    if state.get("status_too_large") not in (None, True):
        return "bad status_too_large"
    since = state.get("since")
    if since is not None and not (isinstance(since, (int, float)) and not isinstance(since, bool)
                                  and 0 <= since < 1e11):
        return "bad since"
    return None


def _claim(store: Path, pending_id: str | None, project: Path | None = None) -> tuple[Path, Path, dict]:
    """Returns (begin file, claim marker, begin state). Without an id, the newest begin is taken, but never one that
    git tracks (a repository can ship begin files) or one dated more than FUTURE_SLACK_S in the future (it would
    always be "newest"); a begin file that fails validate_begin is refused."""
    d = _pending_dir(store)
    if pending_id:
        if not S.safe_id(pending_id):   # from the MCP client or the command line: never a path
            raise RuntimeError(f"{str(pending_id)[:40]!r} is not a pending id (see `devmem begin`)")
        cands = [d / f"{pending_id}.json"]
    refused = []
    if not pending_id:  # most recent begin first, then the legacy single-slot file
        dated = []
        limit = time.time() + FUTURE_SLACK_S
        for q in (d.glob("*.json") if d.exists() else []):
            try:
                mt = q.stat().st_mtime
            except OSError:  # finished and removed by a concurrent end since glob()
                continue
            if mt <= limit:
                dated.append((mt, q))
            else:
                refused.append(f"{q.name}: dated in the future")
        cands = [q for _, q in sorted(dated, key=lambda x: x[0], reverse=True)]
        cands.append(_pending(store))
    tracked = set()
    if project is not None:
        from .store import tracked_in_store
        tracked = {os.path.normcase(os.path.basename(t)) for t in tracked_in_store(project, store)}
    opened, read_left, stopped = 0, MAX_CLAIM_BYTES, False
    for c in cands:
        if not c.exists():
            continue
        if os.path.normcase(c.name) in tracked:
            refused.append(f"{c.name}: tracked by git (shipped with the repository)")
            continue
        try:
            size = c.stat().st_size
        except OSError:
            continue
        if not pending_id and (opened >= MAX_CLAIM_FILES or size > read_left):
            stopped = True
            break
        marker = c.with_name(c.name + ".claim")
        if not _mark(marker):
            continue  # held by a concurrent end
        opened, read_left = opened + 1, read_left - size
        try:
            state = json.loads(_retry(lambda: read_store_text(c)))
        except FileNotFoundError:  # another end finished it, or not a plain file of at most STORE_FILE_CAP
            _unmark(marker)
            if pending_id and c.exists():
                raise RuntimeError(f"begin file {c.name} cannot be used: it is not a plain file of at most "
                                   f"{STORE_FILE_CAP // (1024 * 1024)} MB") from None
            continue
        except (OSError, ValueError, RecursionError) as e:
            _unmark(marker)  # unreadable now: leave the begin in place for a later end, never delete it
            if pending_id:
                what = "is not valid JSON" if isinstance(e, (ValueError, RecursionError)) else "could not be read"
                raise RuntimeError(f"begin file {c.name} cannot be used: it {what}") from None
            if isinstance(e, (ValueError, RecursionError)):
                refused.append(f"{c.name}: not valid JSON" + (" (nested too deep)" if isinstance(e, RecursionError)
                                                              else ""))
            continue
        why = validate_begin(state)
        if why:
            _unmark(marker)
            if pending_id:
                raise RuntimeError(f"begin file {c.name} cannot be used: {why}")
            refused.append(f"{c.name}: {why}")
            continue
        if "kind" not in state:  # v1 pending.json: {"started_at", "snapshot"}
            state["kind"] = "snapshot"
        return c, marker, state
    if stopped:
        raise RuntimeError(f"no usable begin among the {opened} newest begin files devmem read (it reads at most "
                           f"{MAX_CLAIM_FILES} files and {MAX_CLAIM_BYTES // (1024 * 1024)} MB looking for one)"
                           + (": refused " + "; ".join(refused[:3]) if refused else "")
                           + f". Pass the id `devmem begin` printed (`devmem end --id <id>`, MCP pending_id), or remove "
                           f"begin files you no longer need from {d}")
    if refused:
        raise RuntimeError("no usable begin: refused " + "; ".join(refused[:3]) + " (remove those files from "
                           f"{d}, then run `devmem begin` again)")
    if pending_id:
        raise RuntimeError(f"no begin with id {pending_id} in {d} (an `end` may have recorded it already, or another "
                           "`end` is recording it now)")
    raise RuntimeError("no session in progress: run `devmem begin` first")


def end(project: Path, prompt: str, provider: str = "manual", store: Path | None = None,
        summary: str | None = None, summary_source: str | None = None, model: str | None = None,
        pending_id: str | None = None, prompt_source: str = S.PROMPT_FROM_USER, info: dict | None = None) -> Path:
    """Record the session since a begin (`pending_id`, default the newest one); `info` gets what the record's bounds
    left out (end_note)."""
    project = Path(project)
    store = _mine(project, Path(store or store_path(project)))
    secure_pending(store)  # the begin this end consumes, and any other one an older version wrote
    pending, marker, state = _claim(store, pending_id, project)
    try:
        before, after, git = end_state(project, state, Redactor(store))
        meta = record_generic(project, prompt, before, after, state["started_at"], provider, store=store,
                              result_text=summary, summary_source=summary_source, model=model, git=git,
                              extra={"begin_id": state.get("id")} if state.get("id") else None,
                              prompt_source=prompt_source, info=info)
    except BaseException:
        _unmark(marker)  # release: the begin stays, so the user can retry `end`
        raise
    # consume: the begin first, then the marker (a begin without a marker would be claimable again)
    try:
        _retry(lambda: pending.unlink(missing_ok=True))
    finally:
        _unmark(marker)
    return meta


# ---- wrap: any CLI agent ----

def wrap(project: Path, prompt: str, command: list[str], provider: str = "cli", store: Path | None = None,
         budget: int = 1500, with_pack: bool = True, info: dict | None = None) -> Path | None:
    """Run `command` in the project. `{prompt}` in its args becomes pack + task, `{pack_file}` a file path. Returns the
    record, or None for a store that is not this user's (devmem.consent.store_refusal): then the pack holds project
    files only, nothing is written into the store and the run is not recorded (refused_note says so on stderr)."""
    from .scan import scan
    project = Path(project).resolve()
    prog = command[0] if command else ""
    if prog and os.path.basename(prog) == prog:
        # a bare program name comes from PATH only: never a copy the project (or the current folder) ships
        exe = find_program(prog, project)
        if not exe:
            raise FileNotFoundError(f"{prog} was not found on PATH (devmem does not run programs from the project or "
                                    "the current folder; give its full path)")
        command = [exe, *command[1:]]
    code = proc.code_arguments(command)
    if command and any("{prompt}" in a and (code is None or i in code) for i, a in enumerate(command[1:], 1)):
        # cmd.exe parses a batch file's arguments again (`&`, `|` and `%VAR%` in the pack or task would run as
        # commands or expand, and the argument ends at the first newline); PowerShell, wsl, `sh -c` and `python -c`
        # run their text as code. The pack quotes project files, so it must never become code.
        what = ("a batch file or shell that reads its whole command line as commands" if code is None
                else "the program reads that argument as code")
        raise proc.UnsafeCommand(f"{os.path.basename(command[0])}: {what}, so the prompt ({{prompt}}) cannot go "
                                 "there. Pass {pack_file} instead (a path to the pack; the task is in --prompt), or "
                                 "read the file named by the DEVMEM_PACK_FILE variable")
    proc.check(command, project)          # the template alone: refuse before scanning when cmd.exe would misread it
    store = Path(store or store_path(project))
    why = refusal(project, store)
    if why:
        from . import term
        term.say(refused_note(why), file=sys.stderr)
    else:
        store = ensure_store(store)
        scan(project, store)
    red = Redactor(None if why else store)
    pack = _pack(project, store, prompt, budget) if with_pack else ""   # project files only if refused
    tmp = Path(tempfile.mkdtemp(prefix="devmem-wrap-"))
    try:                                  # the folder holds the full pack: removed however the command ends
        pack_file = tmp / "context_pack.md"
        pack_file.write_text(pack, encoding="utf-8")
        full = f"{pack}\n\n---\n\n{prompt}" if pack else prompt
        argv = [a.replace("{prompt}", full).replace("{pack_file}", str(pack_file)) for a in command]
        t0 = time.time()
        start, started = (None if why else start_state(project, red)), _now()   # not recorded: nothing to compare
        # NO_CAPTURE_ENV: when the wrapped command is Claude Code in a project with `devmem init` hooks, the
        # hooks still deliver context but do not record a second copy of this session
        # its output is held within proc.CHILD_OUTPUT_HEAD + _TAIL per stream; only the tail is recorded
        r = proc.run_capped(argv, project=project, cwd=project,
                            env={**os.environ, "DEVMEM_PACK_FILE": str(pack_file), NO_CAPTURE_ENV: "1"})
        if why or start is None:
            return None
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        before, after, git = end_state(project, start, red, since=t0)
        return record_generic(project, prompt, before, after, started, provider, store=store,
                              output_text=red.capped(out, 20000, tail=True)[0], command=[prog or command[0], "..."],
                              result_text=red.capped((r.stdout or "").strip(), 2000, tail=True)[0] or None,
                              summary_source="tool stdout", git=git, info=info, output_cut=_output_cut(r))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- Codex CLI adapter (documented flags: exec --json, -o/--output-last-message, -C, --sandbox) ----

def _output_cut(r) -> dict | None:
    """What run_capped left out of a program's output (characters per stream), or None when it held it whole."""
    cut = {k: n for k, n in (("stdout_chars", r.stdout_dropped), ("stderr_chars", r.stderr_dropped)) if n}
    return cut or None


def _find_usage(events: list[dict]) -> dict:
    """Sum any token-usage objects present in the JSONL events (field names vary by version)."""
    tot: dict = {}
    def walk(x):
        if isinstance(x, dict):
            if "input_tokens" in x and isinstance(x.get("input_tokens"), int):
                for k in ("input_tokens", "cached_input_tokens", "output_tokens"):
                    if isinstance(x.get(k), int):
                        tot[k] = tot.get(k, 0) + x[k]
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    for e in events:
        if e.get("type") in ("turn.completed", "token_count") or "usage" in e:
            walk(e)
    return tot


def codex_program(exe: str, project=None) -> list[str]:
    """How to start codex. npm installs it on Windows as a `codex.cmd` shim that cmd.exe runs (and re-parses); the
    shim only starts `node <its folder>\\node_modules\\@openai\\codex\\bin\\codex.js`, so devmem runs that directly:
    the shim folder's own node.exe (as the shim does), else node found on PATH (never in the project or the current
    folder; a `node.cmd` is no better than the shim). Anything else: the shim itself, which proc.run guards."""
    if not proc.runs_through_cmd(exe):
        return [exe]
    folder = Path(exe).parent
    js = folder / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    try:
        if not js.is_file() or (project and _inside(js.resolve(), Path(project).resolve())):
            return [exe]
        node = str(folder / "node.exe") if (folder / "node.exe").is_file() else find_program("node", project)
    except OSError:
        return [exe]
    if not node or proc.runs_through_cmd(node):
        return [exe]
    return [node, str(js)]


def _inside(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(child), os.path.normcase(parent)]) == os.path.normcase(parent)
    except ValueError:
        return False


def codex(project: Path, prompt: str, model: str | None = None, store: Path | None = None,
          budget: int = 1500, with_pack: bool = True, sandbox: str = "workspace-write", mcp: bool = False,
          isolated: bool = False, info: dict | None = None) -> Path | None:
    """Run `codex exec` on the task. with_pack: pack prepended to the prompt. mcp: instead expose DevMemory as an
    MCP server for this run only (-c overrides; the user's Codex config is not modified). isolated: ignore the
    user's Codex config (no personal plugins/MCP servers such as computer use), as in our benchmark runs. Returns the
    record, or None for a store that is not this user's (see wrap: project files only, nothing written there)."""
    from .scan import scan
    project = Path(project).resolve()
    store = Path(store or store_path(project))
    why = refusal(project, store)
    if why:
        from . import term
        term.say(refused_note(why), file=sys.stderr)
    else:
        store = ensure_store(store)
        scan(project, store)
    red = Redactor(None if why else store)
    pack = _pack(project, store, prompt, budget) if with_pack else ""   # project files only if refused
    full = f"{pack}\n\n---\n\n{prompt}" if pack else prompt
    exe = find_program("codex", project)
    if not exe:
        raise FileNotFoundError("codex was not found on PATH")
    tmp = Path(tempfile.mkdtemp(prefix="devmem-codex-"))
    try:                                  # removed however codex ends (its last message may quote the pack)
        return _run_codex(project, prompt, full, exe, tmp, store, red, model, sandbox, mcp, isolated, record=not why,
                          info=info)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_codex(project: Path, prompt: str, full: str, exe: str, tmp: Path, store: Path, red: Redactor,
               model: str | None, sandbox: str, mcp: bool, isolated: bool, record: bool = True,
               info: dict | None = None) -> Path | None:
    last = tmp / "last.txt"
    # the project is codex's working folder (cwd), never an argument: on Windows an npm `codex.cmd` goes through
    # cmd.exe, which would run `&` or expand `%VAR%` in a folder name (codex_program avoids the shim when it can,
    # and proc.run refuses what cmd.exe would misread)
    cmd = [*codex_program(exe, project), "exec", "--json", "--skip-git-repo-check", "--sandbox", sandbox,
           "-o", str(last)]
    if model:
        cmd += ["-m", model]
    if isolated:  # keep only the Windows sandbox mode, without which the workspace is read-only on Windows
        cmd += ["--ignore-user-config"] + (["-c", 'windows.sandbox="elevated"'] if os.name == "nt" else [])
    if mcp:
        repo = Path(__file__).resolve().parent.parent.as_posix()
        args = ["-m", "devmem.cli", "mcp", "--project", project.as_posix(), "--store", store.as_posix()]
        def toml(v) -> str:   # a TOML value: JSON strings and arrays are valid TOML (no \ud800 escapes)
            return json.dumps(v, ensure_ascii=False)
        cmd += ["-c", "mcp_servers.devmemory.command=" + toml(Path(sys.executable).as_posix()),
                "-c", "mcp_servers.devmemory.args=" + toml(args),
                # NO_CAPTURE_ENV: this run is recorded here, so the server's begin/end create nothing
                "-c", f"mcp_servers.devmemory.env={{PYTHONPATH={toml(repo)}, {NO_CAPTURE_ENV}=\"1\"}}"]
        for tool in ("get_context_pack", "list_sessions", "read_session", "begin_session", "end_session"):
            # headless exec cannot ask the user, so DevMemory's own tools are pre-approved for this run
            cmd += ["-c", f'mcp_servers.devmemory.tools.{tool}.approval_mode="approve"']
    cmd = proc.check(cmd + ["-"], project)   # refused before anything runs
    t0 = time.time()
    start, started = (start_state(project, red) if record else None), _now()   # not recorded: nothing to compare
    r = proc.run_capped(cmd, project=project, input=full, cwd=project, env={**os.environ, NO_CAPTURE_ENV: "1"})
    if not record or start is None:
        return None
    events = []
    if r.stdout_dropped:     # the events of the first and the last part; a line cut at either edge is not one
        lines = r.stdout_head.split("\n")[:-1] + r.stdout_tail.split("\n")[1:]
    else:
        lines = (r.stdout or "").split("\n")            # not splitlines(): U+2028 inside a JSON string
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):                        # an event is an object; any other JSON line is not one
            events.append(e)
    # codex writes this file in devmem's private temp folder; read it only as a plain file (a FIFO or a link put
    # there by the run must not block devmem or make it record another file), at most 4 MB
    from .store import read_regular
    raw_last = read_regular(last, 4 * 1024 * 1024)
    result = raw_last.decode("utf-8", errors="replace").strip() if raw_last is not None else None
    def sub(e: dict, k: str) -> dict:
        return e[k] if isinstance(e.get(k), dict) else {}
    errs = [str(e.get("message") or sub(e, "error").get("message") or sub(e, "item").get("message"))
            for e in events if e.get("type") in ("error", "turn.failed") or sub(e, "item").get("type") == "error"]
    before, after, git = end_state(project, start, red, since=t0)
    meta = record_generic(project, prompt, before, after, started, "codex-cli" + (" + devmemory-mcp" if mcp else ""), store=store, git=git,
                          result_text=result, summary_source="codex final message (-o)", events=events,
                          output_text=(r.stderr or "")[-4000:], model=model, usage=_find_usage(events),
                          command=["codex", "exec", "--json"], errors=errs, info=info, output_cut=_output_cut(r))
    return meta
