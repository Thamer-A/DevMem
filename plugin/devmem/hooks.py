"""Claude Code hook entry point: interactive session capture and context delivery.

    python -m devmem.hooks <EventName> [--store DIR]      (Claude Code hook JSON on stdin)

Installed by `devmem.init.init_project`. Events handled (documented hook inputs only, verified against
Claude Code 2.1.323 payloads kept in tests/test_hooks_fixtures/):

  SessionStart        open/re-open the session's live log; git HEAD and dirty state at start; a resumed session's
                      turn left open by its earlier run is checked and closed (_close_open_turn)
  UserPromptSubmit    start a turn (the user's prompt, redacted) and print the context pack on stdout,
                      which Claude Code adds to the model's context (UTF-8, under the 10,000-char cap); a turn
                      still open (interrupted: Claude Code sends it no Stop) is checked and closed first
  PostToolUse         one tool call: tool, file, Edit/Write/MultiEdit text (capped, redacted), command
  PostToolUseFailure  a failed tool call and its error
  Stop                end of a turn: `last_assistant_message` (assistant-visible), deletions observed on
                      disk, and (BEST-EFFORT, undocumented format) visible text/model/usage read from the
                      transcript file; then the schema-2 record is rewritten so it is always usable
  PostCompact         the compaction summary, labelled as model-generated
  StopFailure         the API error that ended a turn; the turn is checked and closed as at a Stop
  SessionEnd          a turn still open is checked and closed; read what the Stops left of the transcript (one
                      event's budget; the rest is labelled in the record's `transcript_gaps`), finish the record and
                      move the log into sessions/

Never breaks the user's session: every error is caught and logged to `<store>/hook_errors.log` (or, when
that cannot be written, to `fallback_error_log()` in the temp folder), and the process always exits 0; the
installed command (devmem.init) also exits 0 when this file cannot even be imported. Nothing is written when
`<store>` does not exist (deleting `.devmemory` opts out). With `"capture": false` in `config.json` (also
0, "false", "off", "no"), or with DEVMEM_NO_CAPTURE=1 in the environment (set by devmem for a session it
launches and records itself), nothing is recorded; if delivery is on, its per-session state file
`live/<sid>.state.json` (what context was sent on which turn; no prompt or tool text) is still kept until
SessionEnd. `"deliver": false` turns off the context pack only.
When both `devmem init` handlers and the Claude Code plugin's handlers fire, `live/<sid>.route` (one word, "init" or
"plugin") says which of them records the session; see `session_route`.
Hook input is sanitised once (unpaired UTF-16 surrogates become U+FFFD). Tool text is cut before it is
redacted, so a huge or pathological tool output costs bounded time. Output of a call whose input names a
secret file (any path field, or a shell command mentioning one) is never stored.
Hidden reasoning is never stored: only prompts, assistant-visible text, tool calls/results and observed
file changes.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

if __name__ == "__main__" and not __package__:
    # Run as a file path (what init installs when `devmem` is not importable from the interpreter):
    # put the package's parent first on sys.path instead of the package directory itself.
    sys.path[0] = str(Path(__file__).resolve().parent.parent)
    from devmem.hooks import main as _main
    sys.exit(_main())

from . import sessions as S  # noqa: E402
from .redact import CAP_MARGIN, OMITTED_SECRET_FILE, Redactor  # noqa: E402
from .store import (_NO_LOCKS, _NOT_TRANSIENT, PRIVATE_FILE, OSLock, _os_lock, _same_file,  # noqa: E402,F401
                    is_remote_path, open_append_fd, private_fd, private_mkdir, read_store_text)

STORE_DIR = ".devmemory"
STDOUT_CAP = 9500           # Claude Code caps hook stdout at 10,000 characters (larger output goes to a file)
LOCK_TIMEOUT_S = 3.0
ABANDONED_AFTER_S = 12 * 3600
# finished per SessionStart (each costs a read and a rewrite of its log): the rest waits for later session starts
ABANDONED_PER_START = 8
ABANDONED_BYTES_PER_START = 32 * 1024 * 1024
DELIVERED_KEPT = 500        # entries of a session's delivery history kept in its state (read and written every prompt)
EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "StopFailure",
          "PostCompact", "SessionEnd")
# Set by devmem in the environment of a Claude Code session it launches and records itself (`devmem run`,
# `devmem wrap`): the hooks then deliver context as usual but record nothing, so the session is recorded once.
NO_CAPTURE_ENV = "DEVMEM_NO_CAPTURE"


# ---------------------------------------------------------------- small utilities

class _Lock(OSLock):
    """The session lock: devmem.store.OSLock waiting at most LOCK_TIMEOUT_S (read when the lock is taken), or
    `timeout` seconds."""

    def __init__(self, path: Path, timeout: float | None = None):
        super().__init__(path, timeout)

    def _timeout(self) -> float:
        return LOCK_TIMEOUT_S if self.timeout is None else self.timeout


def _config(store: Path) -> dict:
    try:
        c = json.loads(read_store_text(store / "config.json"))
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def _paths(store: Path, sid: str) -> dict[str, Path]:
    live = store / "live"
    return {"log": live / f"{sid}.jsonl", "state": live / f"{sid}.state.json", "lock": live / f"{sid}.lock",
            "edited": live / f"{sid}.edited", "route": live / f"{sid}.route", "seen": live / f"{sid}.gitseen",
            "turn": live / f"{sid}.turn", "open": live / f"{sid}.open", "edits": live / f"{sid}.edits",
            "prompt": live / f"{sid}.prompt", "end": live / f"{sid}.end"}


ROUTES = ("init", "plugin")
ROUTE_REDECIDE_S = 10.0     # a SessionStart that starts or resumes a session replaces an older decision


def _read_route(path: Path) -> tuple[str | None, float]:
    try:
        st = path.stat()
        value = read_store_text(path, 4096).strip()
    except (OSError, ValueError):
        return None, 0.0
    return (value if value in ROUTES else None), st.st_mtime


def session_route(store: Path, sid: str, propose, fresh_start: bool = False, persist: bool = True) -> str:
    """Which route records this session: "init" (`devmem init` handlers) or "plugin" (the Claude Code plugin).

    Claude Code may keep, or reload, its hook list while a session runs, so asking the settings files at every event
    loses or doubles events when `devmem init` / `devmem uninstall` run mid-session. Instead the route is decided once,
    at the session's first event, by whichever handler set gets there first (`propose()`; the two sets propose the
    same answer unless their view of the settings differs), and kept in `live/<sid>.route`; every later event of
    both sets obeys it. A SessionStart that starts or resumes the session (a new hook list may have been loaded)
    decides again when the stored decision is older than ROUTE_REDECIDE_S, so the sibling handler of that same
    SessionStart still finds the fresh one. The decision is taken under the session lock. When it cannot be written
    (read-only store) the proposal is used for this event only.

    persist=False (capture is off: nothing may be recorded, only the delivery state is kept) reads a stored decision
    but writes none; both handler sets then propose from the same settings files, so they still agree unless those
    change mid-session."""
    path = _paths(store, sid)["route"]
    if os.path.lexists(path) and not path.is_file() or path.is_symlink():
        return propose()       # a folder (or link) where the decision belongs: never wait on replacing it
    if not persist:
        value, _ = _read_route(path)
        return value or propose()
    if not fresh_start:
        value, _ = _read_route(path)
        if value:
            return value
    with _Lock(_paths(store, sid)["lock"]) as lk:
        value, mtime = _read_route(path)
        if value and (not lk.held or not fresh_start or time.time() - mtime < ROUTE_REDECIDE_S):
            return value
        value = propose()
        if not lk.held:
            return value if value in ROUTES else ROUTES[0]   # not decided for others without the lock
        if value not in ROUTES:
            value = ROUTES[0]
        try:
            S._atomic_write(path, value + "\n")
        except OSError:
            pass
        return value


def read_state(store: Path, sid: str, red: Redactor | None = None) -> dict:
    """Per-session state (`live/<sid>.state.json`): ids, record base name, transcript offset, and
    `delivered` (what context was injected on each turn, for the retrieval layer; entries get `cleared_at` when
    the conversation is compacted or cleared, and are then no longer counted as already sent). With `red`, its strings
    are remembered as redacted (_write_state wrote them through it), so writing the state back costs no second pass."""
    try:
        st = json.loads(read_store_text(_paths(store, sid)["state"]))
        st = valid_state(st) if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}
    if red is not None:
        red.mark_clean(st)
    return st


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _short_str(v) -> bool:
    return isinstance(v, str) and len(v) <= 4096


def _str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _spans(v) -> bool:
    return isinstance(v, list) and len(v) <= TURN_SPANS_KEPT and all(
        isinstance(x, list) and len(x) == 2 and all(S.state_time(t) is not None for t in x) for x in v)


# Every field of the live state a handler reads, and what it must be; a field that fails is treated as absent (removed
# when the state is read: valid_state). A field not listed is no handler's and is left as it is.
_STATE_FIELDS = {
    "session_id": _short_str, "started_at": _short_str, "project": _short_str, "transcript_path": _short_str,
    "base": _short_str,
    "turns": lambda v: _is_int(v) and 0 <= v < 2 ** 53,
    "transcript_offset": lambda v: _is_int(v) and 0 <= v < 2 ** 63,
    "delivered": lambda v: isinstance(v, list),
    "transcript_msg_ids": _str_list, "git_last_dirty": _str_list, "reported_deleted": _str_list,
    "git_start": lambda v: isinstance(v, dict),
    "record_size": lambda v: isinstance(v, list) and len(v) == 2 and all(_is_int(x) and x >= 0 for x in v),
    "ended": lambda v: isinstance(v, bool), "scanned": lambda v: isinstance(v, bool),
    "scan_incomplete": lambda v: isinstance(v, bool), "record_kept_noted": lambda v: isinstance(v, bool),
    "turn_open": lambda v: isinstance(v, bool),
    "turn_prompt_id": lambda v: isinstance(v, str) and len(v) <= 256,
    "closed_by": lambda v: v in CLOSED_BY,
    "git_checked_at": lambda v: S.state_time(v) is not None,
    "prompt_at": lambda v: S.state_time(v) is not None,
    "unchecked_from": lambda v: S.state_time(v) is not None,
    "unchecked_to": lambda v: S.state_time(v) is not None,
    "spans": _spans,
}
# how the session's last turn ended (`closed_by`): a Stop; a Stop whose hook did not get the lock (seen in the log by
# the next event); an API error (StopFailure); no Stop (the next prompt, SessionEnd, a resuming SessionStart); a lost
# prompt's turn closed unchecked (_absorb_prompt); a session finalised as abandoned
CLOSED_BY = ("Stop", "stop_unchecked", "StopFailure", "next_prompt", "session_end", "resume", "unchecked", "abandoned")
MAX_TURN_S = 7 * 86400          # a turn's start older than this (or than the session's own start) is unknown
PROMPT_AHEAD_S = 60.0           # and so is one more than this far in the future (a clock set back a little is allowed)


def valid_state(st: dict, now: float | None = None) -> dict:
    """The live state with every field a handler reads checked for type and range in one place (_STATE_FIELDS): a field
    of the wrong type (`turns` a list or a string, a time that is a string, NaN or 10**400) reads as absent. A turn's
    start (`prompt_at`) must also lie within MAX_TURN_S of now, not before the session's own start (`started_at`, to the
    second) and at most PROMPT_AHEAD_S ahead; else the turn's start is unknown."""
    out = {k: v for k, v in st.items() if k not in _STATE_FIELDS or _STATE_FIELDS[k](v)}
    if "delivered" in out:
        out["delivered"] = [d for d in out["delivered"] if isinstance(d, dict)]
    pa = S.state_time(out.get("prompt_at"))
    if pa is not None:
        now = time.time() if now is None else now
        began = S._when(out.get("started_at")) if isinstance(out.get("started_at"), str) else None
        if pa < now - MAX_TURN_S or pa > now + PROMPT_AHEAD_S or (began is not None and pa < began.timestamp() - 1):
            out.pop("prompt_at")
    return out


def _write_state(store: Path, sid: str, st: dict, red: Redactor | None = None) -> None:
    """Every string of the state is redacted on its way to disk (Redactor.record): it holds values from the hook
    payload (the transcript path, prompt ids)."""
    red = red or Redactor(store)
    S._atomic_write(_paths(store, sid)["state"], json.dumps(red.record(st), ensure_ascii=False, indent=1))


def _append(path: Path, obj: dict, red: Redactor | None = None) -> None:
    """Append one event as one complete JSON line, atomically and without any lock.

    THE write boundary of hook capture: every string of the event (any field, any depth, keys too) goes through
    `red.record` here, so a field a handler adds later cannot reach the log unredacted; every record and index entry
    of a hook-recorded session is built from this log. Without `red`, the built-in patterns apply.

    Guarantee: concurrent appends from any number of threads/processes never overwrite or interleave each
    other; every event that reaches this function ends up in the log as its own intact line (order between
    concurrent writers is arbitrary; the record builder groups by prompt_id/tool_use_id, not by position).
    - Windows: the file is opened with FILE_APPEND_DATA access only (no FILE_WRITE_DATA), so the file
      system places every WriteFile at the current end of file atomically. Python's os.O_APPEND is not
      enough there: the C runtime emulates it with seek-then-write, and two processes can write at the
      same offset (this lost events under contention before; see tests/test_hooks_capture.py).
    - POSIX: O_APPEND with a single write() of the whole line; the kernel sets the offset and writes
      atomically with respect to other O_APPEND writers on a local file.
    A record is written as newline + JSON + newline: a write cut short (a full disk) leaves a torn line, and the
    next record then still starts a line of its own instead of being glued to it and lost with it (read_events
    skips blank and unparseable lines; `devmem doctor` reports torn ones).
    """
    private_mkdir(path.parent)
    obj = (red or Redactor(None)).record(obj)
    text = "\n" + json.dumps(obj, ensure_ascii=False) + "\n"
    try:
        line = text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate from a source other than the hook input: keep the event
        line = S.scrub_surrogates(text).encode("utf-8")
    _append_bytes(path, line)


def _append_bytes(path: Path, data: bytes) -> None:
    """`data` appended to `path` in one atomic write, without a lock (see _append); never through a link."""
    if os.name == "nt":
        _not_a_link(path)
        _win_append(str(path), data)
        return
    fd = open_append_fd(path)           # a regular file only: a FIFO planted as the log never blocks the hook
    try:
        if os.write(fd, data) != len(data):  # only on disk-full or similar; never silently half-written
            raise OSError(f"short write appending to {path}")
    finally:
        os.close(fd)


_K32 = None


def _win_append(path: str, data: bytes) -> None:
    global _K32
    import ctypes
    from ctypes import wintypes
    if _K32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k.WriteFile.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        _K32 = k
    file_append_data, synchronize = 0x0004, 0x00100000
    share_all = 0x1 | 0x2 | 0x4          # read | write | delete (the log may be renamed at SessionEnd)
    open_always, attr_normal = 4, 0x80
    h = _K32.CreateFileW(path, file_append_data | synchronize, share_all, None, open_always, attr_normal, None)
    if h is None or h == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        written = wintypes.DWORD(0)
        if not _K32.WriteFile(h, data, len(data), ctypes.byref(written), None) or written.value != len(data):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _K32.CloseHandle(h)


def _reset_delivered(st: dict, why: str) -> None:
    """Context sent earlier may no longer be in the model's context (compaction, /clear). Keep the history
    but mark every entry cleared, so `_active_delivered` no longer counts it and the items can be sent again."""
    now = S.now_iso()
    for d in st.get("delivered") or []:
        if isinstance(d, dict) and "cleared_at" not in d:
            d["cleared_at"], d["cleared_by"] = now, why


def _active_delivered(st: dict) -> list:
    return [d for d in st.get("delivered") or [] if not (isinstance(d, dict) and d.get("cleared_at"))]


def _ensure_session(store: Path, project: Path, sid: str, payload: dict, red: Redactor | None = None) -> dict:
    """Load or create the session state; re-open a finished session's log on resume."""
    p = _paths(store, sid)
    st = read_state(store, sid, red)
    if not st:
        st = {"session_id": sid, "started_at": S.now_iso(), "project": str(project),
              "transcript_offset": 0, "turns": 0, "delivered": []}
        st["base"] = S.existing_base(store, sid) or S.session_base(store, st["started_at"], sid).name
        if not (payload.get("hook_event_name") == "SessionStart" and payload.get("source") == "startup"):
            # first event seen mid-session: earlier transcript text is not ours to attribute
            st["transcript_offset"] = _transcript_size(payload)
    st["session_id"] = sid          # the file is this session's, whatever a damaged state says
    if not S.safe_id(st.get("base")):
        # the state file names the record: a value that is not a plain name (a hand-edited or planted state) is replaced
        st["base"] = S.session_base(store, str(st.get("started_at") or S.now_iso()), sid).name
    tp = payload.get("transcript_path")
    if _transcript_ok(tp):
        st["transcript_path"] = tp      # for finalising the session later without its SessionEnd
    if st.get("ended") and not p["log"].exists():
        raw = store / "sessions" / f"{st['base']}.jsonl"
        if raw.exists():
            S.move_file(raw, p["log"])
        st["ended"] = False
    return st


def _transcript_ok(tp) -> bool:
    """A transcript path kept in the session state for finalising the session without its SessionEnd (the only
    transcript read whose path does not come from Claude Code's own payload): a plain local path to a `.jsonl` file,
    the format of Claude Code's transcripts."""
    return isinstance(tp, str) and 0 < len(tp) <= 4096 and chr(0) not in tp and tp.lower().endswith(".jsonl") \
        and not is_remote_path(tp)


def _offset(st: dict) -> int:
    try:
        return max(0, int(st.get("transcript_offset") or 0))
    except (TypeError, ValueError):
        return 0


def _take_transcript(tp, st: dict, red: Redactor, final: bool = False) -> dict:
    """Event fields from the transcript, read from the session's offset within one event's budget
    (S.read_transcript_since: at most TRANSCRIPT_READ_CAP bytes and about TRANSCRIPT_TEXT_BUDGET characters). Every
    text is redacted (and cut to TEXT_CAP, marked `truncated`) before it goes anywhere. Message ids already counted
    are kept in the state, so usage is counted once per message across reads. With `final` (SessionEnd, a session
    finalised without one) what is still unread afterwards is labelled as an "unread" gap: no later event reads it."""
    if not isinstance(tp, str) or not tp or chr(0) in tp:
        return {}
    ids = [x for x in st.get("transcript_msg_ids") or [] if isinstance(x, str)]
    items, st["transcript_offset"] = S.read_transcript_since(tp, _offset(st), seen_msg=set(ids))
    texts, model, usage, gaps = [], None, {}, []
    for it in items:
        if it["kind"] == "text":
            text, cut = red.capped(it["text"], S.TEXT_CAP)
            entry = {"text": text, "ts": it.get("ts"), "source": "assistant-visible (transcript, best-effort)"}
            if cut:
                entry["truncated"] = True
            texts.append(entry)
        elif it["kind"] == "model":
            model = it["model"]
        elif it["kind"] == "usage":
            if isinstance(it.get("id"), str):
                ids.append(it["id"])
            for k, v in it["usage"].items():
                usage[k] = usage.get(k, 0) + (v or 0)
        elif it["kind"] == "gap":
            gaps.append({k: v for k, v in it.items() if k != "kind"})
    st["transcript_msg_ids"] = ids[-S.MSG_IDS_KEPT:]
    if final:
        left = S.transcript_backlog(tp, st["transcript_offset"])
        if left:
            gaps.append(S.make_gap("unread", bytes=left))
    return {"assistant_text": texts or None, "model": model, "usage": usage or None, "transcript_gaps": gaps or None}


def _transcript_size(payload: dict) -> int:
    """Size of the session's transcript, 0 when it is missing, not a plain path or on a network share."""
    tp = payload.get("transcript_path")
    if not isinstance(tp, str) or not tp or is_remote_path(tp):
        return 0
    try:
        return Path(tp).stat().st_size
    except (OSError, ValueError):
        return 0


def _event(payload: dict, name: str, **fields) -> dict:
    ev = {"event": name, "ts": S.now_iso(ms=True), "session_id": payload.get("session_id")}
    if payload.get("prompt_id"):
        ev["prompt_id"] = payload["prompt_id"]
    if payload.get("agent_id"):
        ev["agent_id"] = payload["agent_id"]
        ev["agent_type"] = payload.get("agent_type")
    ev.update({k: v for k, v in fields.items() if v is not None})
    return ev


def _write_record(store: Path, project: Path, st: dict, events: list[dict], final: bool,
                  keep_hashes: bool = False, red: Redactor | None = None, complete: bool = True) -> Path:
    """keep_hashes: keep `file_hashes_at_capture` from the record already on disk instead of hashing the
    files now (a session finalised hours later must not take in changes made since its last Stop).

    complete: the events are the whole log (devmem.sessions.read_events reads at most LIVE_READ_CAP bytes per event).
    An existing record is never replaced by one built from part of the log, or with fewer turns or tool calls than it
    held when this session last wrote it (`record_size` in the state): it is kept, the raw log keeps every event, and
    hook_errors.log says so once per session."""
    base = store / "sessions" / st["base"]
    meta_path = base.with_suffix(".meta.json")
    log = _paths(store, st["session_id"])["log"]
    red = red or Redactor(store, project=project)
    red.mark_clean(events)    # read back from the log, which _append wrote through red.record
    rec = S.build_from_events(events, project, hash_now=not keep_hashes)
    size = [len(rec.get("turns") or []), sum(len(t.get("tool_calls") or []) for t in rec.get("turns") or [])]
    held = st.get("record_size")
    smaller = (isinstance(held, list) and len(held) == 2 and all(isinstance(x, int) for x in held)
               and (size[0] < held[0] or size[1] < held[1]))
    if (not complete or smaller) and meta_path.exists():
        if not st.get("record_kept_noted"):
            st["record_kept_noted"] = True
            why = (f"more than {S.LIVE_READ_CAP:,} bytes long, more than devmem reads per event" if not complete else
                   f"read back with fewer turns or tool calls ({size}) than the record holds ({held})")
            _log_error(store, "Stop", OSError(f"the record {meta_path.name} was kept as it is: the session's event log "
                                              f"{log.name} is {why}; every event stays in the raw log"))
        if final:
            S.move_file(log, base.with_suffix(".jsonl"))
        return meta_path
    if keep_hashes:
        try:
            old = json.loads(read_store_text(base.with_suffix(".meta.json")))
            old_hashes = old.get("file_hashes_at_capture") if isinstance(old, dict) else None
        except (OSError, ValueError):
            old_hashes = None
        # no earlier record (never reached a Stop): hashes unknown rather than taken from today's files
        rec["file_hashes_at_capture"] = old_hashes if isinstance(old_hashes, dict) else {}
    rec["raw_record"] = base.with_suffix(".jsonl").name
    rec["capture_note"] = ("Captured by Claude Code hooks: prompts, tool calls and Stop-hook text are "
                           "documented hook fields; assistant text/model/usage from the transcript are best-effort.")
    meta = S.write_record(store, rec, base=base, red=red)
    st["record_size"] = size
    if final:
        S.move_file(log, base.with_suffix(".jsonl"))
    else:
        raw = base.with_suffix(".jsonl")
        tmp = raw.with_name(f".{raw.name}.{os.getpid()}.tmp")
        try:            # a copy in bounded memory, whatever the log's size; readers see the old copy or the new one
            S._copy_private(log, tmp)
            os.replace(tmp, raw)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
    return meta


# ---------------------------------------------------------------- handlers

def on_session_start(payload: dict, project: Path, store: Path, red: Redactor) -> str:
    sid = payload["session_id"]
    p = _paths(store, sid)
    try:
        p["end"].unlink()       # the session goes on (a resume): it is not one a later SessionStart finishes
    except OSError:
        pass
    with _Lock(p["lock"]) as lk:
        if not lk.held:
            _append(p["log"], _event(payload, "SessionStart", source=payload.get("source"), model=payload.get("model")), red)
            return ""
        st = _ensure_session(store, project, sid, payload, red)
        if payload.get("source") in ("resume", "fork", "compact", "clear"):
            # text already in the transcript belongs to earlier turns
            st["transcript_offset"] = max(st.get("transcript_offset", 0), _transcript_size(payload))
        if payload.get("source") in ("compact", "clear"):
            _reset_delivered(st, f"SessionStart:{payload['source']}")
        git = None
        if "git_start" not in st:
            git = _take_git_start(project, p, st)
        _absorb_prompt(p, st)
        if payload.get("source") == "resume":
            # a turn still open belongs to the run that ended (a compaction, source "compact", keeps the turn going)
            _close_open_turn(project, store, sid, st, p, red, "resume", time.time())
        _append(p["log"], _event(payload, "SessionStart", source=payload.get("source"), model=payload.get("model"),
                                 git=git or None), red)
        _write_state(store, sid, st, red)
    if not _RECORD_ONLY:    # while git cannot say, nothing but this session's own files is read from the store
        _finalize_abandoned(store, project, keep=sid, red=red)
        _secure_old_begins(store)
    return ""


def _secure_old_begins(store: Path) -> None:
    """A `devmem begin` written by an older version keeps raw file text in pending/: rewrite it redacted
    (devmem.generic.secure_pending). Only imports generic when there is a pending file at all."""
    pend = store / "pending"
    if not ((pend.is_dir() and any(pend.glob("*.json"))) or (store / "pending.json").exists()):
        return
    try:
        from .generic import secure_pending
        secure_pending(store)
    except Exception as exc:  # noqa: BLE001 - tidying must never fail the session start
        _log_error(store, "SessionStart:secure_pending", exc)


def on_user_prompt(payload: dict, project: Path, store: Path, red: Redactor) -> str:
    sid = payload["session_id"]
    p = _paths(store, sid)
    prompt = payload.get("prompt") or ""
    text, cut = red.capped(prompt if isinstance(prompt, str) else str(prompt), PROMPT_CAP)
    at = time.time()            # where this turn's interval starts (_dated): Claude acts only after this hook returns
    with _Lock(p["lock"]) as lk:
        git = None
        if lk.held:
            st = _ensure_session(store, project, sid, payload, red)
            if "git_start" not in st:  # hooks installed mid-session, or SessionStart not delivered
                git = _take_git_start(project, p, st)
            _absorb_prompt(p, st)
            _close_open_turn(project, store, sid, st, p, red, "next_prompt", at)   # no Stop came for the last one
            st["turns"] = st.get("turns", 0) + 1
            _set_open(p, st, at, payload.get("prompt_id"))
        else:       # the event, and the prompt's time for the next event that gets the lock (_absorb_prompt)
            st = read_state(store, sid, red)
            _mark_prompt(p, at, payload.get("prompt_id"))
        _append(p["log"], _event(payload, "UserPromptSubmit", prompt=text, prompt_truncated=True if cut else None,
                                 git=git or None), red)
        if lk.held:
            _write_state(store, sid, st, red)
    if not S.config_bool(_config(store).get("deliver"), True):
        return ""
    return _deliver(payload, project, store, sid, prompt, st, red)


# Seconds the refresh on a prompt may take (devmem.refresh: a child process killed at this budget): a new or large
# project, or a file that is slow to extract, then delays one prompt by about this much; the rest is indexed on later
# prompts. Then the pack: session records are indexed (devmem.pack.DOC_CHUNK documents per write, the deadline checked
# between writes) and embeddings used only within HOOK_PACK_BUDGET.
HOOK_SCAN_BUDGET = 8.0
HOOK_PACK_BUDGET = 4.0
# characters of a prompt or of the last assistant message that are stored (and used to find the pack); a longer one
# is cut, before redaction, and flagged `prompt_truncated` / `last_assistant_message_truncated`
PROMPT_CAP = 100_000
_RECORD_ONLY = False   # this event records but serves nothing: git cannot say, its last answer was clean (run)


def _deliver(payload: dict, project: Path, store: Path, sid: str, prompt: str, st: dict,
             red: Redactor | None = None) -> str:
    import inspect
    from .consent import recorded_refusal
    if recorded_refusal(project, store):   # not this user's store (run() asked store_refusal for this event already)
        return ""
    from .pack import PACK_MODES, build_pack
    p = _paths(store, sid)
    map_missing = not (store / "index" / "map.json").exists()
    if map_missing or not st.get("scanned") or st.get("scan_incomplete") or p["edited"].exists():
        from .refresh import refresh
        # once per session, and again after this session edited files or when the last refresh did not finish; in a
        # child process killed at the budget (devmem.refresh), so no file can stall the prompt
        r = refresh(project, store, HOOK_SCAN_BUDGET)
        try:
            p["edited"].unlink()
        except OSError:
            pass
        st["scanned"] = True
        st["scan_incomplete"] = r.get("state") not in ("fresh", "done")
    cfg = _config(store)
    budget = cfg.get("pack_budget", 1500)
    if not isinstance(budget, int) or isinstance(budget, bool):
        budget = 1500   # a config value of the wrong type falls back to the default
    budget = max(100, min(20000, budget))
    mode = cfg.get("pack_mode", "full")
    kwargs = {"budget": budget, "mode": mode if isinstance(mode, str) and mode in PACK_MODES else "full"}
    params = inspect.signature(build_pack).parameters
    # Retrieval layer hook-in: if build_pack accepts these, it can skip context already sent this session.
    if "session_id" in params:
        kwargs["session_id"] = sid
    if "delivered" in params:
        kwargs["delivered"] = _active_delivered(st)
    if "deadline" in params:
        kwargs["deadline"] = time.monotonic() + HOOK_PACK_BUDGET
    text, info = build_pack(project, store, prompt[:PROMPT_CAP], **kwargs)
    # `keys` names every item the pack carried (session:<id8>, file:<path>, req:<hash>, conventions), so the
    # next prompt of this session is not sent them again. An empty pack prints nothing at all.
    keys = [str(k) for k in info.get("keys") or []]
    if len(text) > STDOUT_CAP:
        cut = text.rfind("\n", 0, STDOUT_CAP - 80)
        text = text[: cut if cut > 0 else STDOUT_CAP - 80] + "\n[DevMemory: pack cut at the hook output limit]\n"
        keys = [k for k in keys if (k == "conventions" and "## Conventions" in text)
                or (k.startswith("session:") and f"session {k[8:]}" in text)
                or (k.startswith("file:") and f"`{k[5:]}`" in text)]   # what was cut off was not delivered
    files = [k[5:] for k in keys if k.startswith("file:")] if keys else list(info.get("files") or [])
    with _Lock(p["lock"]) as lk:
        if not lk.held:
            return text
        red = red or Redactor(store, project=project)
        cur = read_state(store, sid, red) or st
        cur["scanned"] = True
        cur["scan_incomplete"] = bool(st.get("scan_incomplete"))
        cur.setdefault("delivered", []).append({
            "turn": cur.get("turns", 1) - 1, "prompt_id": payload.get("prompt_id"), "ts": S.now_iso(),
            "files": files,
            "sessions": [x.get("session") for x in info.get("sessions") or [] if isinstance(x, dict)
                         and (not keys or f"session:{str(x.get('session'))[:8]}" in keys)],
            "keys": keys, "empty_reason": info.get("empty_reason"),
            "decisions": info.get("decisions", 0), "tokens_est": info.get("tokens_est"), "chars": len(text)})
        if len(cur["delivered"]) > DELIVERED_KEPT:     # the state is read and written on every prompt
            cur["delivered"] = cur["delivered"][-DELIVERED_KEPT:]
        _write_state(store, sid, cur, red)
    return text


def on_tool(payload: dict, project: Path, store: Path, red: Redactor, failed: bool = False) -> str:
    sid = payload["session_id"]
    p = _paths(store, sid)
    name = str(payload.get("tool_name") or "")
    raw_input = payload.get("tool_input")
    inp = raw_input if isinstance(raw_input, dict) else {}
    resp = payload.get("tool_response")
    calls = S.tool_entries(name, inp, project, red, ok=not failed, response=resp)
    # Secret: the input names a secret file in any path field (Read/Edit/Write, Grep over .env, an MCP
    # filesystem read, ...) or a shell command mentions one (`cat .env`, `git show HEAD:.env`). Then only the
    # fact that the call happened is kept, never its output. Otherwise lines and diff hunks of secret files are
    # dropped from the output (`grep -rn`, `git log -p`). Stream capture applies the same filter_tool_output.
    from .redact import UNCHECKED_OUTPUT, call_decision, scrub_secret_parts   # the one decision (tool_entries too)
    decision = "unchecked" if any(c.get("input_not_checked") for c in calls) else \
        "secret" if any(c.get("secret_file") for c in calls) else call_decision(name, inp, project, red.secret_patterns)
    secret = decision != "ok"

    def keep(text: str, limit: int) -> tuple[str, bool]:
        if secret:
            return (UNCHECKED_OUTPUT if decision == "unchecked" else OMITTED_SECRET_FILE), False
        # cut before filtering and redacting (bounded cost whatever the size), with margin for a secret on the cut
        n = len(text)
        text, _ = red.filter_tool_output(name, inp, text[:limit + 2 * CAP_MARGIN], project)
        text, trunc = red.capped(text, limit)
        return text, trunc or n > limit

    if isinstance(resp, (dict, list)):     # a path value naming something outside the project is not stored
        resp = S.omit_outside_paths(resp, project)
        resp = scrub_secret_parts(resp, project, red.secret_patterns)    # a part naming a secret file: its paths
    out, trunc = keep(str(payload.get("error") or "") if failed else S.output_text(resp, red), S.OUTPUT_CAP)
    ev = _event(payload, "PostToolUseFailure" if failed else "PostToolUse", tool=name,
                tool_use_id=payload.get("tool_use_id"), calls=calls, output=out or None,
                output_truncated=trunc or None, duration_ms=payload.get("duration_ms"))
    if failed:
        ev["error"] = keep(str(payload.get("error") or ""), 600)[0]
        ev["is_interrupt"] = bool(payload.get("is_interrupt"))
    if name in ("Bash", "PowerShell") and not failed and isinstance(resp, dict) and resp.get("interrupted"):
        ev["interrupted"] = True
    if not p["log"].exists():  # first event of the session, or a late event after SessionEnd
        with _Lock(p["lock"]) as lk:
            if lk.held and not p["log"].exists():
                st = _ensure_session(store, project, sid, payload, red)  # re-opens a finished log, never a fresh one
                _write_state(store, sid, st, red)
    _append(p["log"], ev, red)  # atomic without the lock; parallel tool calls never wait on each other
    if not failed:
        _record_edits(p, calls)     # for the checks of the project's other sessions (_other_sessions)
    if _is_own_work(payload, failed, ev):
        _mark_own_work(p)
    if (not failed and name in S.EDIT_TOOLS) or name in ("Bash", "PowerShell"):  # files may have changed
        try:
            private_mkdir(p["edited"].parent)
            p["edited"].touch(mode=PRIVATE_FILE)
        except OSError:
            pass
    return ""


# live/<sid>.gitseen: what a hook compares of git's state from one event to the next, as sorted 8-byte path hashes
# (devmem.sessions.path_id), never as text: b"DMS1", three little-endian uint32 counts, then the hashes of each list.
# Its size is 8 bytes per changed path (at most MAX_STATUS_ENTRIES of them per list); it is not session text, so it
# does not go through the redactor, and the session state keeps no path list at all.
_SEEN_MAGIC = b"DMS1"
_SEEN_KEYS = ("start_deleted", "last_dirty", "reported_deleted")


def _read_seen(path: Path) -> dict[str, set[bytes]] | None:
    """The hash lists of live/<sid>.gitseen, or None when it is missing or not in this format."""
    from .store import read_regular
    data = read_regular(path, 16 + 8 * 3 * S.MAX_STATUS_ENTRIES)
    if data is None or len(data) < 16 or data[:4] != _SEEN_MAGIC:
        return None
    counts = [int.from_bytes(data[4 + 4 * i: 8 + 4 * i], "little") for i in range(3)]
    if 16 + 8 * sum(counts) != len(data):
        return None
    out, at = {}, 16
    for k, n in zip(_SEEN_KEYS, counts):
        out[k] = {data[at + 8 * i: at + 8 * i + 8] for i in range(n)}
        at += 8 * n
    return out


def _write_seen(path: Path, seen: dict[str, set[bytes]]) -> None:
    lists = [sorted(seen.get(k) or ())[:S.MAX_STATUS_ENTRIES] for k in _SEEN_KEYS]   # a fixed upper bound
    data = _SEEN_MAGIC + b"".join(len(x).to_bytes(4, "little") for x in lists) + b"".join(b"".join(x) for x in lists)
    try:
        S._atomic_write_bytes(path, data)
    except OSError:
        pass


def _drop_seen(path: Path) -> None:
    """No live/<sid>.gitseen: the next Stop takes git's state as unknown (a regular file only is removed)."""
    try:
        if os.path.isfile(path) and not os.path.islink(path):
            os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------- turns: when each ran, and what changed in it
#
# A path that newly appears in git's list at a check is attributed to the session only with evidence of its own: a
# modification time inside the turn the check closes, from that turn's UserPromptSubmit (`prompt_at` in the live state)
# to its end plus OBSERVED_FUTURE_SLACK_S, while no other session of the project was active (_other_sessions). A turn
# ends at its Stop (or StopFailure). Claude Code sends no Stop for a turn the user interrupts (Esc), so the next event
# that finds the turn still open closes it first (_close_open_turn: the next UserPromptSubmit, a SessionEnd, a resuming
# SessionStart): such a turn ends at its last recorded event, which is the modification time of the session's event log
# (every event is appended to it, PostToolUse without the lock, so no event pays for a state write to keep it). A Stop
# that comes again without a prompt continues the turn when Claude Code says a Stop hook continued it
# (`stop_hook_active`); any other Stop without an open turn has an unknown start. Every other path is counted in the
# turn's `git_unattributed`, under the reason that holds (devmem.sessions.UNATTRIBUTED_WHY). A new folder git lists
# (`gen/`) is dated by its own time, or else by the newest of up to OBSERVED_ENTRIES_PER_FOLDER entries directly inside
# it. At most OBSERVED_STATS_PER_CHECK stats per check in all.
OBSERVED_FUTURE_SLACK_S = 2.0
OBSERVED_STATS_PER_CHECK = 2_000
OBSERVED_ENTRIES_PER_FOLDER = 64
TURN_SPANS_KEPT = 8             # closed turns a session keeps in live/<sid>.turn for the other sessions' checks
# Other sessions of the same project (_other_sessions): at most OTHER_SESSIONS_READ of them are read per check (each
# file at most OTHER_FILE_BYTES, and the last OTHER_EDITS_READ of its edit records), in a listing of at most
# OTHER_LIVE_ENTRIES names of live/; an open turn whose last recorded event is older than OTHER_TURN_STALE_S (a session
# that crashed mid-turn) counts as active until that event only.
OTHER_SESSIONS_READ = 32
OTHER_LIVE_ENTRIES = 20_000
OTHER_FILE_BYTES = 4096
OTHER_TURN_STALE_S = 3600.0
# live/<sid>.edits: one ASCII line per path, of a fixed size: the 8-byte path hash (devmem.sessions.path_id) in hex, a
# space, the time as %017.6f, a newline (text, so that every store file reads as UTF-8)
EDIT_RECORD = 35
OTHER_EDITS_READ = 1024
EDITS_KEPT = 1024               # the session's own Stop cuts its file back to this many records past twice as many


class _Others:
    """What the live files of the project's other sessions say about one check's turn (_other_sessions): the times
    another session was active, the hashes of the paths its file tools edited then, and whether there were more such
    sessions than one check reads."""

    def __init__(self) -> None:
        self.windows: list[tuple[float, float]] = []
        self.edited: set[bytes] = set()
        self.overflow = False

    def active_at(self, t: float) -> bool:
        return any(a <= t <= b for a, b in self.windows)


def _json_file(path: Path) -> dict:
    from .store import read_regular
    data = read_regular(path, OTHER_FILE_BYTES)
    try:
        v = json.loads(data.decode("utf-8")) if data else None
    except (ValueError, UnicodeDecodeError):
        return {}
    return v if isinstance(v, dict) else {}


def _read_edit_records(path: Path, limit: int) -> list[tuple[bytes, float]]:
    """The last `limit` records of a live/<sid>.edits file (a regular file only), oldest first."""
    from .store import _open_regular
    opened = _open_regular(path, None)
    if opened is None:
        return []
    fd, size = opened
    try:
        size -= size % EDIT_RECORD
        take = min(size, limit * EDIT_RECORD)
        os.lseek(fd, size - take, os.SEEK_SET)
        parts, n = [], 0
        while n < take:
            chunk = os.read(fd, take - n)
            if not chunk:
                break
            parts.append(chunk)
            n += len(chunk)
    except OSError:
        return []
    finally:
        os.close(fd)
    data = b"".join(parts)
    out = []
    for i in range(0, len(data) - len(data) % EDIT_RECORD, EDIT_RECORD):
        rec = _edit_record_parse(data[i:i + EDIT_RECORD])
        if rec is not None:
            out.append(rec)
    return out


def _edit_record(h: bytes, t: float) -> bytes:
    return f"{h.hex()} {t:017.6f}\n".encode("ascii")


def _edit_record_parse(b: bytes) -> tuple[bytes, float] | None:
    try:
        if b[16:17] != b" " or b[-1:] != b"\n":
            return None
        return bytes.fromhex(b[:16].decode("ascii")), float(b[17:-1].decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None


def _other_sessions(store: Path, sid: str, lo: float, now: float) -> _Others:
    """The project's other sessions as their live files describe them, for a turn that began at `lo` (bounded: see
    OTHER_SESSIONS_READ). A session is active during each of its recorded turns (live/<sid>.turn), from the start of an
    open one (live/<sid>.open, or a prompt waiting in live/<sid>.prompt) until now, and after its last turn while its
    event log is still being written (tool calls of work it left running). Only sessions recorded by a devmem that
    writes these files are seen."""
    o = _Others()
    slack = OBSERVED_FUTURE_SLACK_S
    found: dict[str, dict[str, float]] = {}
    try:
        with os.scandir(store / "live") as it:
            for n, e in enumerate(it):
                if n >= OTHER_LIVE_ENTRIES:
                    o.overflow = True               # more names than one check lists: whatever they say is unknown
                    break
                stem, dot, suffix = e.name.rpartition(".")
                if suffix not in ("turn", "open", "prompt", "edits", "jsonl") or stem == sid or not S.safe_id(stem):
                    continue
                try:
                    mt = e.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                found.setdefault(stem, {})[suffix] = mt
    except OSError:
        return o
    if o.overflow:
        return o
    live = store / "live"
    relevant = []
    for other, files in found.items():
        # a session this version recorded (its turn files); an open turn may have begun long ago, anything else matters
        # only when written (its event log included) since this turn began
        if not files.keys() - {"jsonl"}:
            continue
        # by what can overlap this turn, before the cap: its last recorded event since this turn began, or an open turn
        # that is not stale (one whose last event is older than OTHER_TURN_STALE_S ended there, before this turn)
        last = max(files.values())
        if last >= lo - slack or (("open" in files or "prompt" in files) and last >= now - OTHER_TURN_STALE_S):
            relevant.append(other)
    if len(relevant) > OTHER_SESSIONS_READ:
        o.overflow = True
        return o
    for other in relevant:
        files = found[other]
        log_mt = files.get("jsonl")
        last = max(files.values())
        spans = _json_file(live / f"{other}.turn").get("spans") if "turn" in files else None
        span_end = None
        if isinstance(spans, list) and _spans(spans):
            for a, b in spans:
                o.windows.append((float(a), float(b) + slack))
                span_end = max(span_end or 0.0, float(b))
        marks = [_json_file(live / f"{other}.{k}") for k in ("open", "prompt") if k in files]
        starts = [t for t in (S.state_time(m.get(k)) for m in marks for k in ("at", "first")) if t is not None]
        if starts:                                  # a turn open now
            end = now + slack if last >= now - OTHER_TURN_STALE_S else last + slack
            o.windows.append((min(starts), end))
        elif log_mt is not None and log_mt > (span_end or 0.0) + slack:
            o.windows.append(((span_end if span_end is not None else lo), log_mt + slack))
        if "edits" in files and files["edits"] >= lo - slack:
            o.edited.update(h for h, t in _read_edit_records(live / f"{other}.edits", OTHER_EDITS_READ)
                            if t == t and t >= lo - slack)
    return o


def _dated(project: Path, paths: list[str], since: float | None, win: dict,
           others=None) -> tuple[list[str], dict[str, int]]:
    """(the paths attributed to the turn the check closes, {reason: count} of the others; see the comment above).
    `since`: the previous known check (None: unknown). `win`: the turn's "start" (None: unknown) and "end"; for a turn
    whose end was noticed later, "after" (the time it was noticed); "unchecked": [from, to) of an earlier turn whose
    end was not checked. `others()`: the other sessions (_Others), asked once, only when a path falls inside the turn."""
    import stat as _stat
    from .store import project_path
    counts: dict[str, int] = {}

    def count(k: str, n: int = 1) -> None:
        if n:
            counts[k] = counts.get(k, 0) + n
    start = win.get("start")
    if start is None:
        count("changed_turn_start_unknown", len(paths))
        return [], counts
    end, after, unchecked = win["end"], win.get("after"), win.get("unchecked")
    future = time.time() + OBSERVED_FUTURE_SLACK_S
    lo = since if since is not None and since <= start else None
    budget = OBSERVED_STATS_PER_CHECK
    inside, dirs = [], {}
    asked: list = []

    def other_why(f: str, mt: float) -> str | None:
        if others is None:
            return None
        if not asked:
            asked.append(others())
        o = asked[0]
        if o.overflow:
            return "changed_other_sessions_not_read"
        if S.path_id(f.rstrip("/")) in o.edited:
            return "changed_by_other_session_tool"
        return "changed_while_other_session_active" if o.active_at(mt) else None

    def bucket(mt: float) -> str | None:
        if start <= mt <= min(end, future):
            return None                         # inside the turn
        if after is not None and end < mt <= after + OBSERVED_FUTURE_SLACK_S:
            return "changed_after_last_activity"
        if unchecked and unchecked[0] <= mt < unchecked[1]:
            return "changed_turn_end_unknown"
        if lo is not None and lo <= mt < start:
            return "changed_between_turns"
        return "changed_not_in_interval"
    for n, f in enumerate(paths):
        if budget <= 0:
            count("changed_not_looked_at", len(paths) - n)
            break
        folder = f.endswith("/")                # git lists a new untracked folder once, as `name/`
        budget -= 1
        q = project_path(project, f.rstrip("/") if folder else f, dirs)
        try:
            st = os.lstat(q) if q is not None else None
        except (OSError, ValueError):
            st = None
        if st is None:
            count("changed_not_dated")
            continue
        mt = st.st_mtime
        why = bucket(mt)
        if why is not None and _stat.S_ISDIR(st.st_mode):
            # a folder's own time changes when entries are added to or removed from it; a folder made earlier whose
            # files changed in the turn is dated by the newest of a few of its entries
            newest = st.st_mtime
            try:
                with os.scandir(q) as it:
                    for k, e in enumerate(it):
                        if k >= OBSERVED_ENTRIES_PER_FOLDER or budget <= 0:
                            break
                        budget -= 1
                        try:
                            newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                        except OSError:
                            pass
            except OSError:
                pass
            mt, why = newest, bucket(newest)
            if why == "changed_not_in_interval":
                why = "folder_not_in_interval"
        if why is None:
            why = other_why(f, mt)
        if why is None:
            inside.append(f)
        else:
            count(why)
    return inside, counts


def _state_time(st: dict, key: str) -> float | None:
    """A time the live state holds (a number of seconds), or None when it is anything else: not a finite number of a
    sane size (devmem.sessions.state_time)."""
    return S.state_time(st.get(key))


def _checked_since(st: dict, p: dict[str, Path]) -> float | None:
    """When git's list was last known (the start of the interval a new path must have changed in), or None."""
    if "git_checked_at" in st:
        return _state_time(st, "git_checked_at")
    try:                        # a session an older devmem started: the file was written at its last known check
        return os.stat(p["seen"]).st_mtime
    except OSError:
        return None


def _take_git_start(project: Path, p: dict[str, Path], st: dict) -> dict:
    """git's state at the start of the session: the snapshot of a fixed size (devmem.sessions.git_snapshot) goes into
    the session state and the event; what is dirty and deleted goes into live/<sid>.gitseen as hashes."""
    git, status = S.git_observe(project)
    if not git and _in_git_repo(project):
        git = {"status_unknown": True}      # git failed or timed out in a work tree: unknown, not "no git" (_observe)
    st["git_start"] = git
    for k in ("git_last_dirty", "reported_deleted"):    # path lists a devmem before 0.3.1 kept in the state
        st.pop(k, None)
    if status is None:          # git could not describe the work tree (too large, failed): the start is unknown
        _drop_seen(p["seen"])
        return git
    st["git_checked_at"] = time.time()
    _write_seen(p["seen"], {"start_deleted": {S.path_id(f) for f, c in status.items() if "D" in c},
                            "last_dirty": {S.path_id(f) for f in status}, "reported_deleted": set()})
    return git


def _in_git_repo(project: Path) -> bool:
    """Is `project` (or a folder above it) a git repository, by the files git finds it by (devmem.gitindex.locate, no git
    process)? True also when the layout is one devmem does not read itself: only "no repository" is False."""
    from .gitindex import locate
    try:
        locate(project)
    except FileNotFoundError:
        return False
    except Exception:  # noqa: BLE001 - IndexUnknown and the like: there is a repository devmem cannot read
        return True
    return True


def _seen_of(p: dict[str, Path], st: dict) -> dict[str, set[bytes]] | None:
    """live/<sid>.gitseen, or the same lists from a state an older devmem wrote (path lists in the state itself;
    they are taken out of it here, so the state shrinks to its fixed size at the next write). None when git's state
    at the last check is unknown: no such file (git could not describe the work tree at the start), or one that is
    missing, damaged, too large or not a regular file; never "nothing was dirty"."""
    seen = _read_seen(p["seen"])
    gs = st.get("git_start")
    gs = gs if isinstance(gs, dict) else {}
    old = {"start_deleted": gs.pop("deleted", None), "last_dirty": st.pop("git_last_dirty", None),
           "reported_deleted": st.pop("reported_deleted", None)}
    gs.pop("dirty_paths", None)
    if seen is not None:
        return seen
    if gs.get("status_too_large") or not isinstance(old["last_dirty"], list):
        return None
    return {k: {S.path_id(f) for f in v if isinstance(f, str)} if isinstance(v, list) else set()
            for k, v in old.items()}


def _observe_files(project: Path, st: dict, events: list[dict], p: dict[str, Path], win: dict,
                   others=None) -> tuple[list[str], list[str], str | None, dict | None]:
    """Files deleted since they were last seen, and files changed outside the session's file tools (whole lists, in
    memory: the caller stores them through devmem.sessions.bounded_paths), HEAD, and what git showed that could not be
    attributed. What was dirty and what was already reported is compared as hashes (live/<sid>.gitseen, rewritten here
    when it changed).

    When git's state at the last check is unknown (_seen_of: git could not describe the work tree at the start, or the
    file is missing or damaged), nothing git shows now is attributed to the session: a deletion or a change cannot be
    dated. This check becomes the known one: what changes after it is attributed at a later Stop. What was left out
    is counted ({"deleted": n, "changed": n}). Deletions of files the session's own tool calls named still count.
    `win` and `others`: the turn this check closes and the project's other sessions, for dating new paths (_dated)."""
    seen = _seen_of(p, st)
    unknown = seen is None
    since = _checked_since(st, p)
    if seen is None:
        seen = {k: set() for k in _SEEN_KEYS}
    before = {k: set(v) for k, v in seen.items()}
    unattributed = None
    known, edited_this_turn = [], set()
    last_prompt = max((i for i, e in enumerate(events) if e.get("event") == "UserPromptSubmit"), default=-1)
    for i, e in enumerate(events):
        for c in e.get("calls") or []:
            f = c.get("file")
            # a path is evidence of a deletion only when Claude Code's own file tools read or wrote it successfully
            # (it existed in the project then); a path another tool names (an MCP call, a search) is not
            if f and S._is_inside(f) and c.get("ok", True) and c.get("tool") in S.FILE_TOOLS:
                known.append(f)
                if i > last_prompt and c.get("tool") in S.EDIT_TOOLS:
                    edited_this_turn.add(f)
    pid = S.path_id
    reported = seen["reported_deleted"]
    known_ids = {f: pid(f) for f in set(known)}
    deleted = sorted({f for f, h in known_ids.items() if h not in reported and not (project / f).exists()})
    changed, head = [], None
    git_start = st.get("git_start") or {}
    if git_start:
        status = S.git_status(project)
        if status is not None:
            head = S.git_head(project)
            ids = {f: pid(f) for f in status}
            if unknown:             # this check becomes the known one; nothing git shows now is dated
                gone = set(deleted)
                unattributed = {"deleted": sum(1 for f, c in status.items() if "D" in c and f not in gone),
                                "changed": sum(1 for f, c in status.items() if "D" not in c and f not in gone)}
                seen["start_deleted"] = {h for f, h in ids.items() if "D" in status[f]}
            else:
                pre_deleted = seen["start_deleted"]
                deleted = sorted(set(deleted) | {f for f, c in status.items() if "D" in c and ids[f] not in pre_deleted
                                                  and ids[f] not in reported})
                last_dirty = seen["last_dirty"]
                gone = set(deleted)
                new = sorted(f for f, h in ids.items() if h not in last_dirty and f not in edited_this_turn
                             and f not in gone)
                changed, counts = _dated(project, new, since, win, others)
                if counts:
                    unattributed = counts
            seen["last_dirty"] = set(ids.values())
            st["git_checked_at"] = time.time()
            st.pop("unchecked_from", None)      # an earlier turn's unchecked span is counted at this check
            st.pop("unchecked_to", None)
            # a reported deletion only needs remembering while git or a tool call could report it again: the set
            # stays within this status and this log, whatever the session did before
            keep = {h for f, h in ids.items() if "D" in status[f]} | set(known_ids.values())
            reported = (reported & keep) | {pid(f) for f in deleted}
        else:
            reported = reported | {pid(f) for f in deleted}
    else:
        reported = reported | {pid(f) for f in deleted}
    seen["reported_deleted"] = reported
    if unknown and unattributed is None:
        return deleted, changed, head, None      # still unknown (git cannot describe it now either): nothing written
    if seen != before or unknown or not p["seen"].exists():
        if os.path.lexists(p["seen"]) and not os.path.isfile(p["seen"]):
            pass                    # not a regular file (a folder): left as it is, and the state stays unknown
        else:
            _write_seen(p["seen"], seen)
    return deleted, changed, head, unattributed


def _turn_open(st: dict) -> bool:
    """Is a turn of this session open (prompted, not yet ended)? A state an older devmem wrote has no `turn_open`: it
    removed `prompt_at` at every Stop, so a `prompt_at` it kept from after its last check is the open turn's."""
    if "turn_open" in st:
        return st["turn_open"] is True
    pa, since = S.state_time(st.get("prompt_at")), S.state_time(st.get("git_checked_at"))
    return pa is not None and (since is None or pa >= since)


def _unchecked(st: dict) -> list[float] | None:
    a, b = S.state_time(st.get("unchecked_from")), S.state_time(st.get("unchecked_to"))
    return [a, b] if a is not None and b is not None and a < b else None


def _set_open(p: dict[str, Path], st: dict, at: float, prompt_id) -> None:
    """A turn starts at `at` (its UserPromptSubmit); live/<sid>.open tells the project's other sessions."""
    st["prompt_at"], st["turn_open"] = at, True
    st.pop("closed_by", None)
    if isinstance(prompt_id, str) and prompt_id:
        st["turn_prompt_id"] = prompt_id
    else:
        st.pop("turn_prompt_id", None)
    _write_small(p["open"], {"at": at})
    try:                        # its time is the turn's last own event so far (_own_last): the prompt
        os.utime(p["open"], (at, at))
    except OSError:
        pass


def _set_closed(p: dict[str, Path], st: dict, end: float, by: str, continued: bool = False) -> None:
    """The open turn (or, `continued`, the turn a Stop hook continued) ended at `end`; live/<sid>.turn keeps the last
    TURN_SPANS_KEPT turns' times for the project's other sessions."""
    spans: list = list(st.get("spans") or []) if _spans(st.get("spans")) else []
    pa = S.state_time(st.get("prompt_at"))
    if continued and spans and pa is not None and spans[-1][0] == pa:
        spans[-1] = [pa, max(end, spans[-1][1])]
    elif (_turn_open(st) or continued) and pa is not None and pa <= end:
        spans.append([pa, end])
    st["spans"] = spans[-TURN_SPANS_KEPT:]
    st["turn_open"], st["closed_by"] = False, by
    _write_small(p["turn"], {"v": 1, "spans": st["spans"]})
    _drop_seen(p["open"])


def _write_small(path: Path, obj: dict) -> None:
    try:
        S._atomic_write(path, json.dumps(obj))
    except OSError:
        pass


def _absorb_prompt(p: dict[str, Path], st: dict) -> None:
    """A UserPromptSubmit that did not get the session lock left its time in live/<sid>.prompt (_mark_prompt): the next
    handler that holds the lock applies it. With no turn open, the turn starts at that time. With one open (no Stop came
    for it), that earlier turn ends unchecked: what is dated inside it is counted at the next check
    (`changed_turn_end_unknown`), not attributed. Prompts that piled up the same way end their turns unchecked too."""
    path = p["prompt"]
    if not os.path.lexists(path):
        return
    m = _json_file(path)
    _drop_seen(path)
    at, first = S.state_time(m.get("at")), S.state_time(m.get("first"))
    if at is None:
        return
    first = at if first is None or first > at else first
    pa = S.state_time(st.get("prompt_at")) if _turn_open(st) else None
    if _turn_open(st) and (pa is None or pa < at):
        # its start unknown: from the previous check (or the first lost prompt, when that is earlier or unknown)
        frm = pa if pa is not None else (S.state_time(st.get("git_checked_at")) or first)
        st["unchecked_from"], st["unchecked_to"] = min(frm, first), at
        _set_closed(p, st, at, "unchecked")
    elif _turn_open(st):
        return                                  # the open turn began after it: nothing to apply
    elif first < at:
        st["unchecked_from"], st["unchecked_to"] = first, at
    st["turns"] = st.get("turns", 0) + 1
    _set_open(p, st, at, m.get("prompt_id"))


def _mark_prompt(p: dict[str, Path], at: float, prompt_id) -> None:
    """The prompt's time for _absorb_prompt, written atomically without the lock (one small file; a second prompt that
    also misses the lock keeps the first one's time as `first`)."""
    old = _json_file(p["prompt"]) if os.path.lexists(p["prompt"]) else {}
    first = S.state_time(old.get("first")) or S.state_time(old.get("at")) or at
    rec: dict = {"at": at, "first": min(first, at)}
    if isinstance(prompt_id, str) and len(prompt_id) <= 256:
        rec["prompt_id"] = prompt_id
    try:
        private_mkdir(p["prompt"].parent)
    except OSError:
        return
    _write_small(p["prompt"], rec)


# Which events are the open turn's own work, and so move the end of a turn that gets no Stop (_own_last):
# - its UserPromptSubmit (the turn's start) and the main agent's PostToolUse and PostToolUseFailure (no `agent_id`);
# - not a tool call reported as interrupted (PostToolUseFailure with `is_interrupt`, a Bash or PowerShell result with
#   `interrupted`): it is reported when the turn stops, possibly after an edit you made since; what it wrote is counted;
# - not a sub-agent's or a background agent's tool call (`agent_id`): it may run after the turn ended (the main agent's
#   own call that started a foreground sub-agent counts when it returns);
# - not a compaction (PreCompact/PostCompact, SessionStart with source compact), a SessionStart of any source, a
#   SubagentStop, nor a notification: none is work of the turn. A Stop or StopFailure ends the turn at its own time.
# A tool event of the main agent that arrives after you pressed Esc (Ctrl+Enter lets a running tool finish) counts as
# the turn's: it is the turn's tool call ending.


def _is_own_work(payload: dict, failed: bool, ev: dict) -> bool:
    if payload.get("agent_id"):
        return False
    if failed and payload.get("is_interrupt"):
        return False
    return not ev.get("interrupted")


def _mark_own_work(p: dict[str, Path]) -> None:
    """The open turn's own work happened now: the modification time of live/<sid>.open (one call, no lock; nothing when
    no turn is open, as the file is then absent)."""
    try:
        if os.path.isfile(p["open"]) and not os.path.islink(p["open"]):
            os.utime(p["open"])
    except OSError:
        pass


def _own_last(p: dict[str, Path], pa: float | None) -> float | None:
    """When the open turn's own work last happened (see the list above): the time of live/<sid>.open, never before the
    turn's prompt; the prompt's time when that file is missing."""
    try:
        t = os.lstat(p["open"]).st_mtime
    except OSError:
        return pa
    return max(t, pa) if pa is not None else t


def _check_fields(project: Path, store: Path, sid: str, st: dict, events: list[dict], p: dict[str, Path],
                  win: dict) -> dict:
    """One check (_observe_files) as the fields of the event that ends the turn; the paths stored are bounded
    (devmem.sessions.bounded_paths), the rest counted."""
    lo = win.get("start")
    others = (lambda: _other_sessions(store, sid, lo, time.time())) if lo is not None else None
    if "git_start" not in st:
        # the live state was lost (or no event before set it): git is still compared with the last check the session's
        # live/<sid>.gitseen holds, and what it shows is counted under the reason that holds, never dropped unsaid
        st["git_start"] = {"state_lost": True}
    elif not st.get("git_start") and _in_git_repo(project):
        st["git_start"] = {"status_unknown": True}     # git failed at the start (an older devmem took that as no git)
    checked_before = st.get("git_checked_at")
    deleted, changed, head, unattributed = _observe_files(project, st, events, p, win, others)
    start = win.get("start")
    if st.get("git_start") and st.get("git_checked_at") == checked_before and start is not None:
        # git could not be asked at this turn's end (it failed, timed out, or devmem is waiting after a timeout): what
        # changed inside the turn is counted at the next check as such (`changed_turn_end_unknown`), never taken for a
        # change made between turns
        span, end = _unchecked(st), max(win["end"], win.get("after") or 0.0)   # up to when the end was noticed
        st["unchecked_from"] = min(span[0], start) if span else start
        st["unchecked_to"] = max(span[1], end) if span else end
    deleted, deleted_more = S.bounded_paths(deleted)
    changed, changed_more = S.bounded_paths(changed)
    return {"deleted": deleted or None, "deleted_more": deleted_more or None,
            "deleted_source": "observed on disk at end of turn" if deleted else None,
            "changed_observed": changed or None, "changed_observed_more": changed_more or None, "git_head": head,
            "git_unattributed": unattributed}


def _close_open_turn(project: Path, store: Path, sid: str, st: dict, p: dict[str, Path], red: Redactor,
                     by: str, now: float) -> bool:
    """A turn still open when the next prompt, a resuming SessionStart or the finishing of an ended session comes ended
    without a Stop (Claude Code sends none for an interrupted turn), or with a Stop whose hook did not get the lock (its
    event is in the log): check it now. It ends at that Stop, or else at its own last event (_own_last: its prompt or a
    tool call of the main agent, not any other event the log received since); what is dated between then and now is
    counted (`changed_after_last_activity`). A TurnEnd event carries the result."""
    if not _turn_open(st):
        return False
    events = S.read_events(p["log"])
    pa = S.state_time(st.get("prompt_at"))
    last_prompt = max((i for i, e in enumerate(events) if e.get("event") == "UserPromptSubmit"), default=-1)
    stop_at = None
    for e in events[last_prompt + 1:]:
        if e.get("event") == "Stop" and not e.get("agent_id"):
            w = S._when(e.get("ts"))
            stop_at = w.timestamp() if w is not None else stop_at
    if stop_at is not None:
        end, by = stop_at, "stop_unchecked"
    else:
        end = _own_last(p, pa) or now
    end = min(max(end, pa if pa is not None else end), now)
    win = {"start": pa, "end": end + OBSERVED_FUTURE_SLACK_S, "after": now, "unchecked": _unchecked(st)}
    fields = _check_fields(project, store, sid, st, events, p, win)
    ev = {"event": "TurnEnd", "ts": S.now_iso(ms=True), "session_id": sid, "ended_by": by}
    if isinstance(st.get("turn_prompt_id"), str):
        ev["prompt_id"] = st["turn_prompt_id"]
    ev.update({k: v for k, v in fields.items() if v is not None})
    _append(p["log"], ev, red)
    _set_closed(p, st, end, by)
    return True


def _trim_edits(p: dict[str, Path]) -> None:
    """The session's own edit records (live/<sid>.edits, appended by PostToolUse without the lock) are cut back to the
    last EDITS_KEPT once there are twice as many; an append that races the rewrite may be lost (the other sessions'
    checks then count that path as changed while this session was active, instead of as edited by it)."""
    try:
        size = os.lstat(p["edits"]).st_size
    except OSError:
        return
    if size <= 2 * EDITS_KEPT * EDIT_RECORD:
        return
    recs = _read_edit_records(p["edits"], EDITS_KEPT)
    try:
        S._atomic_write_bytes(p["edits"], b"".join(_edit_record(h, t) for h, t in recs))
    except OSError:
        pass


def _record_edits(p: dict[str, Path], calls: list[dict]) -> None:
    """The paths this tool call edited with a file tool, as hashes with the time, for the other sessions' checks."""
    files = {c["file"] for c in calls if c.get("tool") in S.EDIT_TOOLS and c.get("ok", True)
             and isinstance(c.get("file"), str) and S._is_inside(c["file"])}
    if not files:
        return
    now = time.time()
    try:
        _append_bytes(p["edits"], b"".join(_edit_record(S.path_id(f), now) for f in sorted(files)))
    except OSError:
        pass


def on_stop(payload: dict, project: Path, store: Path, red: Redactor) -> str:
    sid = payload["session_id"]
    if payload.get("hook_event_name") == "SubagentStop" or payload.get("agent_id"):
        return ""       # a subagent (or one of Claude Code's own internal agents) finished: the main turn goes on
    p = _paths(store, sid)
    last = payload.get("last_assistant_message")
    last_text, last_cut = red.capped(last, PROMPT_CAP) if isinstance(last, str) else (None, False)
    with _Lock(p["lock"]) as lk:
        if not lk.held:     # the event only; the transcript text is read by the next Stop (its offset did not move)
            _append(p["log"], _event(payload, "Stop", last_assistant_message=last_text,
                                     last_assistant_message_truncated=True if last_cut else None), red)
            return ""
        st = _ensure_session(store, project, sid, payload, red)
        _absorb_prompt(p, st)
        now = time.time()
        continued = False
        win: dict
        if _turn_open(st):
            win = {"start": S.state_time(st.get("prompt_at")), "unchecked": _unchecked(st)}
        elif st.get("closed_by") in ("Stop", "stop_unchecked") and payload.get("stop_hook_active") is True:
            # a Stop hook kept the turn going (Claude Code says so): the same turn, from its own prompt
            win, continued = {"start": S.state_time(st.get("prompt_at"))}, True
        else:       # no open turn and not a continued one (the prompt's hook did not run, or the state was lost)
            win = {"start": None}
        win["end"] = now + OBSERVED_FUTURE_SLACK_S
        read: dict = {}
        events = S.read_events(p["log"], read)
        fields = _check_fields(project, store, sid, st, events, p, win)
        if S.config_bool(_config(store).get("read_transcript"), True):
            # a backlog larger than one event's budget continues at the next Stop (or at SessionEnd)
            fields.update(_take_transcript(payload.get("transcript_path"), st, red))
        ev = _event(payload, "Stop", last_assistant_message=last_text,
                    last_assistant_message_truncated=True if last_cut else None, **fields)
        _append(p["log"], ev, red)
        _write_record(store, project, st, events + [ev], final=False, red=red, complete=read["complete"])
        if _turn_open(st) or continued:
            _set_closed(p, st, time.time(), "Stop", continued=continued)
        _trim_edits(p)
        _write_state(store, sid, st, red)
    return ""


def on_stop_failure(payload: dict, project: Path, store: Path, red: Redactor) -> str:
    """StopFailure: an API error ended the turn (Claude Code sends it instead of Stop). The turn is checked and closed
    like at a Stop, without reading the transcript."""
    sid = payload["session_id"]
    p = _paths(store, sid)
    fields = {"error": payload.get("error"),
              "error_details": red.capped(str(payload.get("error_details") or ""), 300)[0]}
    with _Lock(p["lock"]) as lk:
        if not lk.held:
            _append(p["log"], _event(payload, "StopFailure", **fields), red)
            return ""
        st = _ensure_session(store, project, sid, payload, red)
        _absorb_prompt(p, st)
        if _turn_open(st):
            win = {"start": S.state_time(st.get("prompt_at")), "unchecked": _unchecked(st),
                   "end": time.time() + OBSERVED_FUTURE_SLACK_S}
            fields.update(_check_fields(project, store, sid, st, S.read_events(p["log"]), p, win))
            _set_closed(p, st, time.time(), "StopFailure")
        _append(p["log"], _event(payload, "StopFailure", **fields), red)
        _write_state(store, sid, st, red)
    return ""


def on_simple(payload: dict, project: Path, store: Path, red: Redactor, name: str) -> str:
    sid = payload["session_id"]
    p = _paths(store, sid)
    fields = {}
    if name == "PostCompact":
        fields = {"trigger": payload.get("trigger"),
                  "compact_summary": red.capped(str(payload.get("compact_summary") or ""), S.TEXT_CAP)[0]}
    with _Lock(p["lock"]) as lk:
        if not lk.held:
            _append(p["log"], _event(payload, name, **fields), red)
            return ""
        st = _ensure_session(store, project, sid, payload, red)
        if name == "PostCompact":
            _reset_delivered(st, "PostCompact")
        _append(p["log"], _event(payload, name, **fields), red)
        _write_state(store, sid, st, red)
    return ""


def on_session_end(payload: dict, project: Path, store: Path, red: Redactor) -> str:
    """With a turn still open (no Stop came: the session was closed mid-turn), only the fact that the session ended is
    recorded, one atomic append and no lock: the turn's check (git status and dating), the transcript's rest and the
    record are left to the next SessionStart in the project (_finalize_sessions), whose budget is not SessionEnd's
    1.5 s. Otherwise the record is finished here as before; cut off at any point, what is on disk stays true (the record
    is replaced atomically, the log moves last) and a session whose SessionEnd is in its log (live/<sid>.end, _mark_ended)
    is finished later the same way."""
    sid = payload["session_id"]
    p = _paths(store, sid)
    if _turn_open(read_state(store, sid)) or os.path.lexists(p["prompt"]):
        _append(p["log"], _event(payload, "SessionEnd", reason=payload.get("reason")), red)
        _mark_ended(p)
        return ""
    with _Lock(p["lock"]) as lk:
        if not lk.held:     # the event only; the log stays in live/ and is finished later (_finalize_abandoned)
            _append(p["log"], _event(payload, "SessionEnd", reason=payload.get("reason")), red)
            _mark_ended(p)
            return ""
        st = _ensure_session(store, project, sid, payload, red)
        _absorb_prompt(p, st)
        _close_open_turn(project, store, sid, st, p, red, "session_end", time.time())
        fields = {}
        if S.config_bool(_config(store).get("read_transcript"), True):
            # what the Stops left unread (a last turn larger than one Stop's budget), within one event's budget of
            # its own (about 1 s for 8 MB of new transcript; SessionEnd's timeout is 15 s); the rest is labelled
            fields = _take_transcript(payload.get("transcript_path") or st.get("transcript_path"), st, red,
                                      final=True)
        git: dict | None = None
        if st.get("git_start"):
            git = {"head": S.git_head(project)}
            status = S.git_status(project)
            if status is not None:
                git["dirty"] = bool(status)
        ev = _event(payload, "SessionEnd", reason=payload.get("reason"), git=git, **fields)
        _append(p["log"], ev, red)
        _mark_ended(p)          # cut off from here on: a later SessionStart finishes it
        read: dict = {}
        events = S.read_events(p["log"], read)
        _write_record(store, project, st, events, final=True, red=red, complete=read["complete"])
        st["ended"] = True
        _write_state(store, sid, st, red)
        for f in (p["edited"], p["end"]):
            try:
                f.unlink()
            except OSError:
                pass
    return ""


def _unmark_ended(p: dict[str, Path], marked: bool = True) -> None:
    if marked:
        try:
            p["end"].unlink()
        except OSError:
            pass


def _mark_ended(p: dict[str, Path]) -> None:
    """live/<sid>.end: this session's SessionEnd is in its log and the session is not finished yet. The next
    SessionStart in the project finishes it (_finalize_abandoned), whatever events arrived after the SessionEnd (a late
    tool call); a SessionStart of the session itself (a resume) removes the mark. One lstat per log tells it, so a
    SessionStart opens no log it does not finish."""
    try:
        fd = os.open(str(p["end"]), os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0), PRIVATE_FILE)
        os.close(fd)
    except OSError:
        pass


def _finalize_abandoned(store: Path, project: Path, keep: str, red: Redactor | None = None) -> None:
    """Live logs of sessions whose SessionEnd did not finish them (live/<sid>.end: on_session_end left a turn's check
    to this), and live logs untouched for ABANDONED_AFTER_S (crash, killed terminal): close an open turn (an ended
    session's is checked as at its next prompt: _close_open_turn), write their record and move the log into sessions/.
    Their record was already usable (written at every Stop); this tidies live/.

    What their Stops left unread of the transcript (its path is kept in the session state) is read first, one
    event's budget for one such session per call: another abandoned session with unread transcript waits for a later
    SessionStart (its log stays in live/), and what one read does not reach is labelled in the record."""
    now = time.time()
    reads_left = 1
    left, bytes_left = ABANDONED_PER_START, ABANDONED_BYTES_PER_START
    tries = 2 * ABANDONED_PER_START                 # sessions whose files are opened per call, finished or not
    read_ok = red is not None and S.config_bool(_config(store).get("read_transcript"), True)
    for log in (store / "live").glob("*.jsonl"):
        sid = log.stem
        if sid == keep:
            continue
        try:
            st_log = log.stat()
        except OSError:
            continue
        # ended: its SessionEnd left it to this (live/<sid>.end), whatever came after it in the log; one lstat, no open
        ended = os.path.lexists(_paths(store, sid)["end"])
        if not ended and now - st_log.st_mtime < ABANDONED_AFTER_S:
            continue                                # still live (or resumed after its SessionEnd)
        p = _paths(store, sid)
        if not os.path.lexists(p["state"]):        # nothing can finish it (no state: no record name); one lstat
            _unmark_ended(p, ended)                 # it no longer waits here: the abandoned path, as any other log
            continue
        if not S.safe_id(read_state(store, sid).get("base")):
            continue        # no record name, a damaged state or one unreadable now: one small read, no try, mark kept
        if tries <= 0 or left <= 0 or (bytes_left <= 0 and left < ABANDONED_PER_START):
            break                                   # bounded per SessionStart: the rest at a later one
        tries -= 1                                  # tries go only to logs that can be finished
        with _Lock(p["lock"]) as lk:
            if not lk.held:
                continue
            wred = red or Redactor(store, project=project)
            st = read_state(store, sid, wred)
            if not S.safe_id(st.get("base")):      # changed since the read above
                continue
            fields: dict = {}
            tp = st.get("transcript_path")
            if read_ok and red is not None and _transcript_ok(tp) and S.transcript_backlog(tp, _offset(st)):
                if not reads_left:
                    continue                        # its turn comes at a later SessionStart
                reads_left -= 1
                fields = {k: v for k, v in _take_transcript(tp, st, red, final=True).items() if v is not None}
            left, bytes_left = left - 1, bytes_left - st_log.st_size
            if ended:
                _absorb_prompt(p, st)
                _close_open_turn(project, store, sid, st, p, wred, "session_end", time.time())
                git: dict | None = None
                if st.get("git_start"):
                    git = {"head": S.git_head(project)}
                    status = S.git_status(project)
                    if status is not None:
                        git["dirty"] = bool(status)
                _append(log, {"event": "Finalized", "ts": S.now_iso(), "session_id": sid, "git": git, **fields}, wred)
            else:
                _append(log, {"event": "Abandoned", "ts": S.now_iso(), "session_id": sid, **fields}, wred)
            read: dict = {}
            events = S.read_events(log, read)
            _write_record(store, project, st, events, final=True, keep_hashes=True, red=wred, complete=read["complete"])
            if _turn_open(st):      # it ended mid-turn, at its last recorded event; nothing of that turn is checked
                _set_closed(p, st, st_log.st_mtime, "abandoned")
            st["ended"] = True
            _write_state(store, sid, st, wred)
            try:
                p["end"].unlink()
            except OSError:
                pass


HANDLERS = {
    "SessionStart": on_session_start,
    "UserPromptSubmit": on_user_prompt,
    "PostToolUse": on_tool,
    "PostToolUseFailure": lambda pl, pr, st, r: on_tool(pl, pr, st, r, failed=True),
    "Stop": on_stop,
    "StopFailure": on_stop_failure,
    "PostCompact": lambda pl, pr, st, r: on_simple(pl, pr, st, r, "PostCompact"),
    "SessionEnd": on_session_end,
}


# ---------------------------------------------------------------- entry point

def capture_enabled(cfg: dict) -> bool:
    """Recording is on unless config.json says `"capture": false` (or 0, "false", "off", ...) or devmem itself
    launched this Claude Code session and records it from its own output (NO_CAPTURE_ENV)."""
    if S.config_bool(os.environ.get(NO_CAPTURE_ENV), False):
        return False
    return S.config_bool(cfg.get("capture"), True)


def fallback_error_log() -> Path:
    """Where hook errors go when `<store>/hook_errors.log` cannot be written (or there is no store): a per-user
    file in the system temp folder. `devmem doctor` should show it when it exists."""
    import tempfile
    uid = getattr(os, "getuid", None)
    name = f"devmem_hook_errors_{uid()}.log" if uid else "devmem_hook_errors.log"
    return Path(tempfile.gettempdir()) / name


def _open_temp_log(path: Path):
    """The temp folder's error log for appending: never through a link (O_NOFOLLOW), created 0600, and only when it
    is a regular file this user owns (its name is predictable and /tmp is shared on Linux and macOS). The plugin and
    `devmem init` bootstraps open it the same way (devmem.plugin.ERROR_LOG_LINES)."""
    import stat
    # O_NONBLOCK: a FIFO planted at the name fails at once (no reader) or is refused below, never blocks the hook
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0), 0o600)
    try:
        st = os.fstat(fd)
        getuid = getattr(os, "getuid", None)        # POSIX only
        if not stat.S_ISREG(st.st_mode) or (getuid is not None and st.st_uid != getuid()):
            raise OSError(f"{path} is not a regular file of this user; not written")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "a", encoding="utf-8", errors="replace")


def _log_error(store: Path | None, event: str, exc: BaseException, about: Path | None = None) -> None:
    """Log a hook failure into `<store>/hook_errors.log`, or, with store None, into the temp folder's log
    (fallback_error_log). `about`: the store the failure concerns when it is not written into (a store not known to be
    this user's: nothing is written there), named in the entry's header so `devmem doctor` can match it."""
    try:
        import traceback
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-3000:]
        entry = f"--- {S.now_iso()} {event} pid={os.getpid()} store={store or about}\n{tb}\n"
        # an error message can quote the payload (a refused session id): the built-in patterns and the store's approved
        # user patterns, whatever the store's config says (the temp-folder fallback log is outside the store)
        try:
            red = Redactor(store, enabled=True, project=store.parent) if store else Redactor(None)
        except Exception:  # noqa: BLE001 - an unreadable patterns file still leaves the built-in patterns
            red = Redactor(None)
        entry = red.text(entry)
    except Exception:  # noqa: BLE001 - logging must never raise
        return
    targets = ([store / "hook_errors.log"] if store and store.is_dir() else []) + [fallback_error_log()]
    for target in targets:  # the store's log first; if it cannot be written (read-only store, a link), the temp one
        try:
            if target == fallback_error_log():
                with _open_temp_log(target) as f:
                    f.write(entry)
            else:
                from .store import open_append
                with open_append(target) as f:
                    f.write(entry)
            return
        except Exception:  # noqa: BLE001 - logging must never raise
            continue


def _write_stdout(text: str) -> None:
    """UTF-8 regardless of the console/pipe code page (cp1252 on Windows would raise on U+2212)."""
    if not text:
        return
    data = text.encode("utf-8", errors="replace")
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except (AttributeError, ValueError):
        sys.stdout.write(data.decode("utf-8"))
        sys.stdout.flush()


def _init_switched_on(project, event: str = "") -> bool:
    """The `devmem init` switch. An unusable per-user folder (a relative DEVMEM_USER_DATA, one inside the project)
    counts as "not switched on"; it is logged once per session, at SessionStart, in the temp folder's log."""
    try:
        from .consent import init_marker
        return init_marker(project).is_file()
    except ValueError as exc:
        if event == "SessionStart":
            _log_error(None, event, exc)
        return False
    except Exception:  # noqa: BLE001 - no switch can be read: not switched on
        return False


# Where the hooks write (or SQLite opens) in a store: checked with one lstat each on every event, instead of walking
# the store. A link in any of them makes the hook write nothing (store_links_now).
LINK_CHECKED = ("", "live", "sessions", "index", "pending", "hook_errors.log", "mcp_calls.jsonl", "hook_timing.log",
                "config.json", "PROJECT.md", "index/index.db", "index/index.db-wal", "index/index.db-shm",
                "index/vectors.db", "index/map.json")


def store_links_now(store: Path, sid: str) -> list[str]:
    """The paths among LINK_CHECKED and this session's live files that are symbolic links or Windows junctions (lstat
    only; devmem.store._is_link)."""
    from .store import _is_link
    names = list(LINK_CHECKED) + [f"live/{sid}{s}" for s in S.LIVE_SUFFIXES]
    found = []
    for n in names:
        p = os.path.join(store, n) if n else str(store)
        if _is_link(p):
            found.append(p)
    return found


def _not_a_link(path) -> None:
    from .store import _is_link
    if _is_link(path):
        raise OSError(f"{path} is a symbolic link or junction; devmem never writes through a link")


# ---------------------------------------------------------------- events not recorded, counted
# An event the hooks would have recorded but did not, because the store was refused (devmem.consent.store_refusal: git
# could not say which of its files it tracks and its last answer was not clean, not this user's store, files a pull
# brought) or holds a link where devmem writes, is counted outside the store, in devmem's per-user folder:
# `<user data>/stores/<store key>.unrecorded/<hash of the session id>`, one line `<time> <kind> <event>` per event
# (consent.refusal_kind), appended atomically without a lock (as _append), so any number of hook processes and threads
# count at once. When that session next records, the lines not yet taken become ONE `NotRecorded` event in its log
# (count, kinds, events, first and last time), which the record turns into its `not_recorded` field; a line
# `<time> taken <offset>` then marks what was taken (never deleted while the session may record again, so a count
# appended meanwhile is taken next time). Bounds: a file stops growing at UNRECORDED_FILE_CAP (the count then says "at
# least"); at most UNRECORDED_FILES sessions per store (a fully taken file, or one older than UNRECORDED_KEEP_S, makes
# room; past that a new session's events are not counted); files untouched for UNRECORDED_KEEP_S are deleted. A count
# whose session never records again is listed by `devmem status` (unrecorded_left) until it is deleted.
UNRECORDED_FILE_CAP = 256 * 1024
UNRECORDED_FILES = 64
UNRECORDED_KEEP_S = 30 * 86400
_UNRECORDED_DIRS: dict[str, Path] = {}


def _unrecorded_dir(store: Path) -> Path:
    """The store's folder of counts (ValueError when the per-user folder is unusable for it: consent.store_note)."""
    key = os.environ.get("DEVMEM_USER_DATA", "") + "|" + os.path.normcase(os.path.abspath(store))
    got = _UNRECORDED_DIRS.get(key)
    if got is None:
        from .consent import store_note
        note = store_note(store)
        got = _UNRECORDED_DIRS[key] = note.with_name(note.name + ".unrecorded")
    return got


def _unrecorded_file(store: Path, sid: str) -> Path:
    import hashlib
    return _unrecorded_dir(store) / hashlib.sha256(("session:" + sid).encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _count_unrecorded(store: Path, sid: str, event: str, why: str) -> None:
    """One event of session `sid` not recorded, for the reason `why` (a refusal, or "link"): counted. Never raises."""
    try:
        from .consent import refusal_kind
        path = _unrecorded_file(store, sid)
        try:
            size: int | None = os.lstat(path).st_size
        except FileNotFoundError:
            size = None
        if size is None:
            private_mkdir(path.parent)
            if len(os.listdir(path.parent)) >= UNRECORDED_FILES:
                _sweep_unrecorded(path.parent, full=True)
                if len(os.listdir(path.parent)) >= UNRECORDED_FILES:
                    return
        elif size >= UNRECORDED_FILE_CAP:
            return
        _append_bytes(path, f"{time.time():.3f} {refusal_kind(why)} {event}\n".encode("ascii"))
    except Exception:  # noqa: BLE001 - counting must never fail or slow the hook
        pass


def _untaken(data: bytes) -> tuple[dict, int]:
    """What a count file holds that is not taken yet: ({"count", "kinds", "events", "first", "last"}, the offset just
    past its last complete line). Only lines devmem writes count: a time that is a plain finite number in a sane range,
    a kind of consent.REFUSAL_KINDS, an event of EVENTS, and a `taken` mark whose offset (at most 12 digits) lies
    before the mark itself; any other line (a damaged or tampered file) is skipped."""
    from .consent import REFUSAL_KINDS
    taken = 0
    lines, pos = [], 0
    for raw in data.split(b"\n")[:-1]:      # complete lines only (an append is one whole line)
        lines.append((pos, raw.split()))
        pos += len(raw) + 1
    for p, parts in lines:
        if len(parts) == 3 and parts[1] == b"taken" and parts[2].isdigit() and len(parts[2]) <= 12 \
                and int(parts[2]) <= p and _count_time(parts[0]) is not None:
            taken = max(taken, int(parts[2]))
    got: dict = {"count": 0, "kinds": {}, "events": {}, "first": None, "last": None}
    for p, parts in lines:
        if p < taken or len(parts) != 3 or parts[1] == b"taken":
            continue
        t = _count_time(parts[0])
        kind, ev = parts[1].decode("ascii", "replace"), parts[2].decode("ascii", "replace")
        if t is None or kind not in REFUSAL_KINDS or ev not in EVENTS:
            continue
        got["count"] += 1
        got["kinds"][kind] = got["kinds"].get(kind, 0) + 1
        got["events"][ev] = got["events"].get(ev, 0) + 1
        got["first"] = t if got["first"] is None else min(got["first"], t)
        got["last"] = t if got["last"] is None else max(got["last"], t)
    return got, pos


def _count_time(b: bytes) -> float | None:
    """The time of a count line: digits, one point, at most 3 decimals, between 2000 and 2200; else None."""
    import re
    if not re.fullmatch(rb"\d{9,11}(?:\.\d{1,3})?", b):
        return None
    t = float(b)
    return t if 946684800.0 <= t < 7258118400.0 else None


def _iso(t: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fold_unrecorded(store: Path, sid: str, red: Redactor) -> None:
    """Before this session's event is recorded: its count of events not recorded (if any is not taken yet) becomes one
    `NotRecorded` event in its log, under the session lock (one taker at a time). Costs one lstat when there is no
    count. Never raises."""
    try:
        path = _unrecorded_file(store, sid)
        os.lstat(path)
    except (OSError, ValueError):
        return
    try:
        from .store import read_regular
        p = _paths(store, sid)
        with _Lock(p["lock"], timeout=0.5) as lk:
            if not lk.held:
                return                      # another event of this session holds it: a later event takes the count
            data = read_regular(path, UNRECORDED_FILE_CAP + 64 * 1024)
            got, end = _untaken(data or b"")
            if not got["count"]:
                return
            ev = {"event": "NotRecorded", "ts": S.now_iso(ms=True), "session_id": sid, "count": got["count"],
                  "kinds": got["kinds"], "events": got["events"], "first": _iso(got["first"]),
                  "last": _iso(got["last"])}
            mark = f"{time.time():.3f} taken {end}\n".encode("ascii")
            if len(data or b"") + len(mark) >= UNRECORDED_FILE_CAP:
                ev["at_least"] = True       # the file is at its bound: events after this one are not counted
            # the mark first: when it cannot be written, these lines are not taken now (a later event tries again),
            # so they are never carried into the log twice
            _append_bytes(path, mark)
            _append(p["log"], ev, red)
        _sweep_unrecorded(path.parent)
    except Exception as exc:  # noqa: BLE001 - a hook must never break the session
        _log_error(store, "NotRecorded", exc)


def _sweep_unrecorded(folder: Path, full: bool = False) -> None:
    """Delete the counts untouched for UNRECORDED_KEEP_S; with `full` (no room for another session) also the fully
    taken ones (their last line is a `taken` mark)."""
    try:
        now = time.time()
        for e in os.scandir(folder):
            try:
                st = e.stat(follow_symlinks=False)
                if now - st.st_mtime > UNRECORDED_KEEP_S:
                    os.unlink(e.path)
                elif full and e.is_file(follow_symlinks=False):
                    with open(e.path, "rb") as f:
                        f.seek(max(0, st.st_size - 64))
                        tail = f.read(64)
                    if tail.rstrip(b"\n").rsplit(b"\n", 1)[-1].split()[1:2] == [b"taken"]:
                        os.unlink(e.path)
            except OSError:
                continue
    except OSError:
        pass


def unrecorded_left(store: Path) -> list[dict]:
    """The counts of the store's sessions not taken yet (sessions that have not recorded since), for `devmem status`:
    one dict per session file ({"count", "kinds", "events", "first", "last"}, times as ISO text; "at_least" when its
    file reached its bound), counts older than UNRECORDED_KEEP_S deleted first. [] when there are none or the per-user
    folder is unusable."""
    from .store import read_regular
    try:
        folder = _unrecorded_dir(Path(store))
    except (OSError, ValueError):
        return []
    _sweep_unrecorded(folder)
    out = []
    try:
        names = sorted(e.name for e in os.scandir(folder) if e.is_file(follow_symlinks=False))
    except OSError:
        return []
    for name in names[:UNRECORDED_FILES * 2]:
        try:
            data = read_regular(folder / name, UNRECORDED_FILE_CAP + 64 * 1024) or b""
            got, _end = _untaken(data)
        except (OSError, ValueError):
            continue
        if got["count"]:
            got["first"], got["last"] = _iso(got["first"]), _iso(got["last"])
            if len(data) >= UNRECORDED_FILE_CAP:
                got["at_least"] = True
            out.append(got)
    return out


def _not_recorded(store: Path, project: Path, route: str, sid: str, event: str, why: str) -> None:
    """An event of a switched-on project that is not recorded because the store was refused or holds a link: counted
    (_count_unrecorded), by one route only, as _paused_message speaks (the init route when its switch is on too); not
    for a session devmem launches and records itself (NO_CAPTURE_ENV)."""
    try:
        if S.config_bool(os.environ.get(NO_CAPTURE_ENV), False):
            return
        if route == "plugin" and _init_switched_on(project):
            return
    except Exception:  # noqa: BLE001 - counting must never fail the hook
        return
    _count_unrecorded(store, sid, event, why)


def run(argv: list[str], stdin_bytes: bytes, route: str = "init", propose=None) -> tuple[int, str]:
    """Testable core: returns (exit code, stdout text). Always exit code 0.

    `route` names the handler set this process belongs to: "init" for the handlers `devmem init` installs, "plugin"
    for the Claude Code plugin's (devmem.plugin.hook_main). The event is handled only when `session_route` says
    this route records the session; `propose` (default: this route) is asked when the session has no route yet.

    The init route does nothing at all (no record, no pack, nothing written, not even an error log in the store)
    unless this user switched the project on with `devmem init` (devmem.consent.init_enabled: one hash, one stat);
    the plugin's entry point checks its own switch before calling this. Neither route writes into a store that holds
    a link where devmem writes (store_links_now), or into one that is not this user's (devmem.consent.store_refusal:
    a store a repository shipped, or one where git tracks files a pull brought); for those it reads nothing either."""
    event, store = (argv[0] if argv and not argv[0].startswith("-") else ""), None
    trusted = False                      # store_refusal said the store is this user's: errors may be logged there
    t0 = time.perf_counter()
    try:
        store_arg = argv[argv.index("--store") + 1] if "--store" in argv else None
        env_project = os.environ.get("CLAUDE_PROJECT_DIR")
        if is_remote_path(env_project) or is_remote_path(store_arg):
            return 0, ""   # a network folder is never stat-ed, resolved or written (as the CLI's _local_arg)
        if route == "init" and env_project and not _init_switched_on(env_project, event):
            return 0, ""
        if store_arg or env_project:  # known before parsing, so even unreadable input is logged in the store
            store = Path(store_arg) if store_arg else Path(str(env_project)) / STORE_DIR
        payload = json.loads(stdin_bytes.decode("utf-8-sig", errors="replace")) if stdin_bytes.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError(f"hook input is not a JSON object: {type(payload).__name__}")
        payload = clean_payload(S.scrub_surrogates(payload))  # once, at the boundary: types checked, encodable
        event = event or str(payload.get("hook_event_name") or "")
        if not env_project and is_remote_path(payload.get("cwd")):
            return 0, ""
        project = Path(env_project or payload.get("cwd") or os.getcwd())
        if route == "init" and not env_project and not _init_switched_on(project, event):
            store = None
            return 0, ""
        store = Path(store_arg) if store_arg else project / STORE_DIR
        if not store.is_dir():
            return 0, ""  # memory not initialised (or deleted): stay out of the way
        if event not in HANDLERS:
            return 0, ""
        if not payload.get("session_id"):
            raise ValueError(f"{event} payload has no session_id")
        if not S.safe_id(payload["session_id"]):   # it names files in live/ and sessions/: never a path
            raise ValueError(f"{event}: session_id is not a plain id ({str(payload['session_id'])[:40]!r}); "
                             "nothing recorded")
        red = Redactor(store, project=project)
        if red.text(payload["session_id"]) != payload["session_id"]:   # nor a secret: it names files
            raise ValueError(f"{event}: session_id looks like a secret; nothing recorded")
        links = store_links_now(store, str(payload["session_id"]))
        if links:   # e.g. a `git pull` brought .devmemory/hook_errors.log -> elsewhere: write nothing this session
            if event == "SessionStart":
                _log_error(None, event, OSError(f"the memory store holds a link where devmem writes ({links[0]}); "
                                                "nothing is recorded until it is removed"))
            _not_recorded(store, project, route, str(payload["session_id"]), event, "a symbolic link or junction")
            store = None
            return 0, ""
        from .consent import failure_is_fresh, is_git_unknown, records_while_unknown, store_refusal
        why = store_refusal(project, store, check_git="cached")  # git runs only after its index changed
        global _RECORD_ONLY
        _RECORD_ONLY = bool(why) and records_while_unknown(store, why)
        # tell the user on SessionStart, and mid-session on the prompt whose event found that git could not say
        notice = bool(why) and (event == "SessionStart" or (event == "UserPromptSubmit" and is_git_unknown(why)
                                                            and failure_is_fresh(store)))
        if why and not _RECORD_ONLY:   # not this user's store (shipped, or a pull brought tracked files), or git
            out = ""                   # cannot say and its last answer was not clean: nothing read or written there
            if event == "SessionStart":
                _log_error(None, event, OSError(f"nothing is recorded or added to prompts in this project: {why}"))
            if notice:
                out = _paused_message(project, store, route, why)
            _not_recorded(store, project, route, str(payload["session_id"]), event, why)
            store = None
            return 0, out
        trusted = True
        cfg = _config(store)
        fresh = event == "SessionStart" and payload.get("source") in ("startup", "resume")
        decided = session_route(store, str(payload["session_id"]), propose or (lambda: route), fresh,
                                persist=capture_enabled(cfg))
        if decided == "init" and route == "plugin" and not _init_switched_on(project):
            decided = "plugin"   # `devmem uninstall` mid-session: the init hooks still fire but record nothing now
        if decided != route:
            return 0, ""  # the other handler set records this session (devmem init hooks and the plugin both fire)
        if not capture_enabled(cfg):  # capture off: at most deliver the pack, record nothing
            # (delivery keeps its per-session state file, live/<sid>.state.json: which context was sent on
            # which turn, so it is not sent twice; it holds no prompt or tool text. It is removed at SessionEnd
            # unless the session was captured earlier, while capture was on.)
            if event == "SessionEnd":
                st_path = _paths(store, payload["session_id"])["state"]
                if st_path.exists() and not read_state(store, payload["session_id"]).get("base"):
                    try:
                        st_path.unlink()
                    except OSError:
                        pass
            if event != "UserPromptSubmit" or not S.config_bool(cfg.get("deliver"), True):
                return 0, ""
            out = _deliver(payload, project, store, payload["session_id"], payload.get("prompt") or "",
                           read_state(store, payload["session_id"]))
        else:
            _fold_unrecorded(store, str(payload["session_id"]), red)   # events of it not recorded earlier: their count
            out = HANDLERS[event](payload, project, store, red) or ""
        if _RECORD_ONLY:       # recorded; nothing served (the pack asks recorded_refusal itself and got the refusal)
            out = _paused_message(project, store, route, why, recording=True) if notice else ""
        if os.environ.get("DEVMEM_HOOK_TIMING"):
            from .store import open_append
            with open_append(store / "hook_timing.log") as f:
                f.write(f"{event}\t{(time.perf_counter() - t0) * 1000:.1f}ms\n")
        return 0, out
    except BaseException as exc:  # noqa: BLE001 - a hook must never break the session
        # nothing is written into a store before it is known to be this user's: an event that failed before the trust
        # check (an unreadable payload, a session id that is not a plain id) asks it now, else logs in the temp folder
        mine = store is not None and (trusted or _store_is_users(store, os.environ.get("CLAUDE_PROJECT_DIR")))
        _log_error(store if mine else None, event or "unknown", exc, about=store)
        return 0, ""


def _paused_message(project: Path, store: Path, route: str, why: str | None = None, recording: bool = False) -> str:
    """SessionStart in a project this user switched on (the init switch, or the plugin's: the caller checked it) whose
    store is refused: one sentence for the user (devmem.consent.paused_notice), as hook JSON whose `systemMessage`
    Claude Code shows to the user. It is not added to Claude's context (the sentence names a command that adopts the
    store, which is the user's decision). Printed by one route only: the plugin stays quiet when the init switch is on
    too."""
    try:
        if route == "plugin" and _init_switched_on(project):
            return ""
        from .consent import paused_notice
        from .term import clean
        return json.dumps({"systemMessage": clean(paused_notice(project, store, why, recording))})
    except Exception:  # noqa: BLE001 - a notice must never break the session
        return ""


def _store_is_users(store: Path, project) -> bool:
    """For the error path of `run`: is `store` this user's (devmem.consent.store_refusal, asked as the hooks ask it)?"""
    try:
        if not store.is_dir() or store_links_now(store, "x"):
            return False
        from .consent import store_refusal
        return store_refusal(Path(project) if project else store.parent, store, check_git="cached") is None
    except Exception:  # noqa: BLE001 - when it cannot be told, nothing is written into the store
        return False


_ID_FIELDS = ("session_id", "prompt_id", "tool_use_id", "agent_id", "agent_type", "hook_event_name", "source",
              "reason", "tool_name", "permission_mode", "trigger", "stop_hook_active")
_PATH_FIELDS = ("cwd", "transcript_path")
_TEXT_FIELDS = ("prompt", "last_assistant_message", "custom_instructions", "error")


def clean_payload(payload: dict) -> dict:
    """Field types checked once, at the hook boundary, so one odd event cannot poison the store: ids and names must
    be short strings (a number becomes its text; a list, object or anything over 256 characters is dropped), paths
    strings of at most 4096 characters, texts strings. A dropped field reads as missing (the session id is then
    refused as usual)."""
    out = dict(payload)
    for k in _ID_FIELDS:
        v = out.get(k)
        if v is None or isinstance(v, bool):
            continue
        if isinstance(v, int) and not isinstance(v, bool):
            v = str(v)
        if not isinstance(v, str) or len(v) > 256:
            out.pop(k, None)
        else:
            out[k] = v
    for k in _PATH_FIELDS:
        v = out.get(k)
        if v is not None and (not isinstance(v, str) or len(v) > 4096 or "\0" in v):
            out.pop(k, None)
    for k in _TEXT_FIELDS:
        v = out.get(k)
        if v is not None and not isinstance(v, str):
            out.pop(k, None)
    if "tool_input" in out and not isinstance(out["tool_input"], dict):
        out["tool_input"] = {}
    return out


MAX_STDIN_BYTES = 32 * 1024 * 1024   # a larger hook payload is not parsed (the event is logged as unreadable)


def read_stdin(stream=None, limit: int = MAX_STDIN_BYTES) -> bytes:
    """The hook payload: at most `limit` bytes are kept; the rest is drained and discarded (so the writer never
    blocks), and an oversized payload comes back cut, which the JSON parser then rejects (logged, nothing stored)."""
    stream = stream if stream is not None else (sys.stdin.buffer if sys.stdin is not None else None)
    if stream is None:
        return b""
    parts, n = [], 0
    while n <= limit:       # a megabyte at a time: read(limit + 1) allocated 32 MB up front (about 10 ms)
        chunk = stream.read(min(1 << 20, limit + 1 - n))
        if not chunk:
            break
        parts.append(chunk)
        n += len(chunk)
    data = b"".join(parts)
    if len(data) > limit:
        while stream.read(1 << 20):
            pass
    return data or b""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        data = read_stdin()
    except Exception:  # noqa: BLE001
        data = b""
    _, out = run(argv, data)
    try:
        _write_stdout(out)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
