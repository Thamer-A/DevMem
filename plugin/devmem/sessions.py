"""Session records: schema 2 (turns), the single loader, the writer, and memory deletion.

Schema 2 (see docs/DESIGN-v2.md): a session has `turns`, one per user prompt. The v1 aggregate fields
(`prompt`, `result_text`, `tool_calls`, `files_read`, `files_edited`, `assistant_text`, `errors`, ...)
stay present and are derived from the turns, so v1 readers (pack.py, ui_server.py) keep working.

Who wrote each text is labelled: `prompt` is the user's words; `assistant_text[].source` and
`result_source` say whether text is assistant-visible output, tool output, or a user-provided summary.
Hidden model reasoning is never stored (thinking blocks are skipped wherever they appear).

Live sessions (captured through Claude Code hooks) are an append-only event log under
`<store>/live/<session_id>.jsonl`. `build_from_events` turns that log into a schema-2 record; the hooks
write the record after every turn, so a crash or a closed terminal still leaves a usable record, and
`load_sessions` also materialises a live log that has no (or an older) record.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .store import read_store_text   # every store file is read as a plain, bounded regular file

# Not imported at run time: `typing` is not loaded on the hook path, and annotations are strings here.
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

SCHEMA = 2
TEXT_CAP = 4000          # per old/new text in a tool call (design doc)
COMMAND_CAP = 1000
OUTPUT_CAP = 2000        # tool output kept in the raw record
MAX_EDITS = 50           # MultiEdit edits kept per event (each is redacted: 1000 edits of 4.9 KB took 18.7 s)
EDITS_TEXT_BUDGET = 100_000   # characters of old/new text kept per MultiEdit event; later edits are left out
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "NotebookRead"}
SEARCH_TOOLS = {"Grep", "Glob"}
FILE_TOOLS = EDIT_TOOLS | READ_TOOLS     # Claude Code's own tools that read or write one named file
# turn["prompt_source"]: who wrote the prompt text. "user": the user's own words (hooks, `devmem run`, the CLI
# `end --prompt` / `wrap` / `codex`); "reported-by-client": text an AI client supplied (MCP end_session).
PROMPT_FROM_USER = "user"
PROMPT_FROM_CLIENT = "reported-by-client"
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "cost_usd_list",
              "num_turns", "duration_ms")


_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")   # 64: longer ids made Windows paths too long


_WIN_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}


def safe_id(value) -> bool:
    """Can `value` be used as a file name component? Session ids and pending ids name files in the store, so one that
    holds a separator, a drive or `..` must never reach a path (Claude Code's ids are UUIDs). Also refused: names
    Windows treats specially (a device name such as NUL or COM1 before the first dot, a leading or trailing dot)."""
    return (isinstance(value, str) and bool(_SAFE_ID.fullmatch(value)) and not value.startswith(".")
            and not value.endswith(".") and value.split(".", 1)[0].upper() not in _WIN_DEVICES)


def now_iso(ms: bool = False) -> str:
    d = datetime.now(timezone.utc)
    if ms:
        return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def scrub_surrogates(x: Any) -> Any:
    """Replace unpaired UTF-16 surrogates (e.g. `\\ud83d` from JavaScript output cut mid-emoji, which
    json.loads accepts) with U+FFFD in every string of a JSON-like value, so nothing downstream raises when
    encoding to UTF-8. Valid pairs never reach Python as surrogates (json.loads combines them), so all other
    text is unchanged."""
    if isinstance(x, str):
        return _LONE_SURROGATE.sub("�", x) if _LONE_SURROGATE.search(x) else x
    if isinstance(x, list):
        return [scrub_surrogates(v) for v in x]
    if isinstance(x, dict):
        return {scrub_surrogates(k) if isinstance(k, str) else k: scrub_surrogates(v) for k, v in x.items()}
    return x


def config_bool(value, default: bool = True) -> bool:
    """A config.json switch read the way a person writes it: false/0/"false"/"no"/"off" turn it off,
    true/1/"true"/"yes"/"on" turn it on; a missing or unreadable value keeps the default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("0", "false", "no", "off", "n", "f", "disabled", "none", ""):
            return False
        if v in ("1", "true", "yes", "on", "y", "t", "enabled"):
            return True
    return default


def stamp_of(started_at: str) -> str:
    return (started_at or now_iso()).replace(":", "").replace("-", "")[:15]


def cap(text, limit: int = TEXT_CAP) -> tuple[str, bool]:
    s = "" if text is None else str(text)
    return (s[:limit], True) if len(s) > limit else (s, False)


def rel_path(path: str | None, project: Path | None) -> str | None:
    """Project-relative POSIX path; paths outside the project stay absolute (with forward slashes)."""
    if not path:
        return None
    p = str(path)
    from .redact import MAX_PATH_CHARS, project_relative
    if len(p) > MAX_PATH_CHARS:
        return None          # not a path (devmem.redact.MAX_PATH_CHARS); the caller records that it was left out
    rel = project_relative(p, project)   # bounded: never resolves a long missing path or a network share
    return rel if rel is not None else p.replace("\\", "/")


# What a record keeps instead of a path a tool names that is not a path inside the project (devmem.capture keeps the
# same text for OUTSIDE_PROJECT). Each says why: outside the project; not a local file path (a URL or another scheme
# than file:, which devmem does not take as a project path); a path with a variable or relative to a drive's current
# folder, which devmem cannot take literally; a name inside the project that is not a plain file name.
OUTSIDE_PROJECT = "outside the project: the path is not stored"
NOT_LOCAL_PATH = "not a local file path (a URL or another scheme): not stored"
NOT_LITERAL_PATH = "a path devmem cannot take literally (a variable, or relative to a drive's current folder): not stored"
NOT_PLAIN_NAME = "inside the project but not a plain file name (an alternate data stream, a device name): not stored"
PATH_REASONS = (OUTSIDE_PROJECT, NOT_LOCAL_PATH, NOT_LITERAL_PATH, NOT_PLAIN_NAME)
# Git for Windows (`/c/x`, `/cygdrive/c/x`) and WSL (`/mnt/c/x`) spellings of a drive path: one leading separator, then
# a single drive letter as the whole first part
_POSIX_DRIVE = re.compile(r"(?i)^[\\/](?![\\/])(?:mnt[\\/]|cygdrive[\\/])?([a-z])(?:[\\/](.*))?$", re.S)
# a URL scheme: two or more characters (one letter and a colon is a Windows drive), followed by `//`, or one of the
# schemes editors use without it; `name.ext:stream` (an alternate data stream) and `a:b` (a POSIX file name) are not
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]+):(?=//)|^(file|untitled|mailto|data|urn|about|javascript|vscode"
                     r"(?:-[a-z]+)*|git|output|inmemory|chat):", re.I)
# a shell or Windows variable devmem would have to expand: $NAME, ${NAME}, $env:NAME, %NAME%; not the stream type of an
# NTFS name (`app.py::$DATA` is app.py itself)
_VARIABLE = re.compile(r"(?<!:)\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\$env:|%[A-Za-z_][A-Za-z0-9_()]*%", re.I)


def decode_path(value: str) -> tuple[str | None, str | None]:
    """The local path a tool's path value names, as text, or (None, why) when it names none devmem can take literally:
    a `file:` URL is decoded (`file:///C:/x`, `file://localhost/x`, `file:/x`, percent escapes; `file://host/share/x`
    becomes `//host/share/x`, a network path); any other scheme (`https:`, `vscode-remote:`, `untitled:`) is
    NOT_LOCAL_PATH; a path holding a variable (`$HOME/x`, `%USERPROFILE%\\x`), one in another user's home (`~user/x`;
    `~/x` is the user's own) or, on Windows, a drive-relative path (`C:x`) is NOT_LITERAL_PATH. Text only: nothing is
    looked up."""
    s = value
    m = _SCHEME.match(s)
    if m and (m.group(1) or m.group(2)).lower() == "file":
        from urllib.parse import unquote, urlsplit
        try:
            u = urlsplit(s)
        except ValueError:
            return None, NOT_LOCAL_PATH
        host, path = u.netloc, unquote(u.path)
        if host and host.lower() != "localhost":
            return "//" + host + path, None             # a network path: outside (devmem.store.is_remote_path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        s = path
        if not s:
            return None, NOT_LOCAL_PATH
    elif m:
        return None, NOT_LOCAL_PATH
    if _VARIABLE.search(s):
        return None, NOT_LITERAL_PATH
    if s.startswith("~") and len(s) > 1 and s[1] not in "\\/":
        return None, NOT_LITERAL_PATH                   # `~user/x`: another user's home, which only a lookup could give
    if os.name == "nt" and len(s) >= 2 and s[1] == ":" and s[0].isalpha() and s[2:3] not in ("\\", "/"):
        return None, NOT_LITERAL_PATH                   # `C:x`: relative to the current folder of drive C
    return s, None


def respell_path(value: str, project: Path | None = None) -> str:
    """One spelling of a decoded path (decode_path), before it is compared with the project: `\\\\?\\C:\\x` and other
    local device forms become `C:\\x` (devmem.store.local_path); on Windows `/c/x`, `/cygdrive/c/x` and `/mnt/c/x`
    become `C:\\x`; `~` is the user's home; a relative path is taken relative to the project (the folder the session
    runs in), not to the folder the hook process happens to run in. Text only: nothing is looked up."""
    from .store import local_path
    s = value
    if os.name == "nt":
        s = local_path(s)
        m = _POSIX_DRIVE.match(s)
        if m:
            s = m.group(1).upper() + ":\\" + (m.group(2) or "")
    if s.startswith("~"):
        s = os.path.expanduser(s)
    if project is not None and not os.path.isabs(s) and not (len(s) > 1 and s[1] == ":"):
        s = os.path.join(str(project), s)
    return s


def project_file(value, project: Path | None) -> tuple[str | None, str | None]:
    """The one rule for storing a path a tool names (the hooks' tool entries and the import's digest, through
    tool_entries; omit_outside_paths for the rest of a tool's input and result): (its project-relative POSIX form, None)
    when it names a plain file name inside the project in any spelling (decode_path, respell_path; 8.3 short names,
    letter case on a case-insensitive volume and links inside the project that resolve inside are settled by
    devmem.redact.project_relative), else (None, why), one of PATH_REASONS, or "N characters: not a path" for a value
    longer than devmem.redact.MAX_PATH_CHARS. The path itself is then not stored. The project folder itself is "."."""
    from .redact import MAX_PATH_CHARS, project_relative
    if not isinstance(value, str) or not value:
        return None, None
    if len(value) > MAX_PATH_CHARS:
        return None, f"{len(value)} characters: not a path"
    if project is None:
        return value.replace("\\", "/"), None
    from .store import is_remote_path, safe_rel
    s, why = decode_path(value)
    if s is None:
        return None, why
    if chr(0) in s or is_remote_path(s):
        return None, OUTSIDE_PROJECT
    s = respell_path(s, project)
    rel = project_relative(s, project) if len(s) <= MAX_PATH_CHARS else None
    if rel is None:
        return None, OUTSIDE_PROJECT
    if rel == "." or safe_rel(rel) is not None:
        return rel, None
    return None, NOT_PLAIN_NAME


# keys of a tool input or result whose string value is a path (any tool's naming, as devmem.redact reads them)
_PATH_KEY = re.compile(r"(?i)path|file|uri|dir|folder|^(?:source|src|destination|dest|dst|target|root|location|"
                       r"filenames)$")
_LOOKS_ABSOLUTE = re.compile(r"(?i)^(?:[a-z]:[\\/]|[\\/]|~[\\/]|file:)")
OUTSIDE_PATHS_LOOKED_AT = 200     # path values of one tool input or result compared with the project (each may resolve)
OUTSIDE_NODES_LOOKED_AT = 20_000  # values of one tool input or result walked (what is stored of it is cut far sooner)
OUTSIDE_DEPTH = 32


def _kept_path(v: str, project: Path, left: list, pathy: bool) -> str:
    """`v` itself, or the reason it is not stored (omit_outside_paths): `v` is a path value when it sits under a
    path-like key (`pathy`) or is one absolute path or `file:` URL (no white space)."""
    if not v or len(v) > 4096 or any(c in v for c in "\n\r\t"):
        return v
    if not (pathy or (_LOOKS_ABSOLUTE.match(v) and not any(c.isspace() for c in v))):
        return v
    if left[0] > 0:
        left[0] -= 1
        rel, why = project_file(v, project)
        if why == NOT_LOCAL_PATH and any(c.isspace() for c in v):
            return v                            # prose with a colon ("Note: ..."), not a URL
        return v if rel is not None or why not in PATH_REASONS or why == NOT_PLAIN_NAME else why
    s, why = decode_path(v)                     # past the compared ones: by text, against the project's own path
    if s is None:
        return why or v
    base = os.path.normcase(os.path.abspath(str(project))).rstrip("\\/")
    q = os.path.normcase(respell_path(s, project))
    return v if q == base or q.startswith(base + os.sep) else OUTSIDE_PROJECT


def omit_outside_paths(obj, project: Path | None, _left: list | None = None, _key: str = "", _depth: int = 0):
    """`obj` (a tool input or a tool result) with every path that is not one inside the project replaced by the reason
    (project_file: OUTSIDE_PROJECT, NOT_LOCAL_PATH, NOT_LITERAL_PATH): a string under a path-like key (`file_path`,
    `filePath`, `path`, `notebook_path`, `uri`, `filenames`, ...), any string that is one absolute path or `file:` URL
    (no white space), and a dictionary KEY that is one (`{"C:/Users/x/o.py": [...]}`; two such keys become the reason
    followed by " #2", " #3", ...). Bounded: at most OUTSIDE_PATHS_LOOKED_AT such values are compared with project_file;
    past that, an absolute path value is replaced unless its text starts with the project's own path. The walk stops
    after OUTSIDE_NODES_LOOKED_AT values or OUTSIDE_DEPTH levels, in the order the value is serialised: the hooks store
    at most a few thousand characters of a tool input or result (TEXT_CAP, OUTPUT_CAP), all of which lie within that
    walk. Free text (a shell command, a tool's printed output) is not taken apart. Returns a copy; `obj` is not
    changed."""
    if project is None:
        return obj
    left = _left if _left is not None else [OUTSIDE_PATHS_LOOKED_AT, OUTSIDE_NODES_LOOKED_AT]
    if left[1] <= 0:
        return obj
    left[1] -= 1
    if isinstance(obj, list):
        if _depth >= OUTSIDE_DEPTH:
            return obj
        done = []
        for v in obj:
            if left[1] <= 0:
                break
            done.append(omit_outside_paths(v, project, left, _key, _depth + 1))
        return done + obj[len(done):]       # the rest as it is (past what is stored of it)
    if isinstance(obj, dict):
        if _depth >= OUTSIDE_DEPTH:
            return obj
        out: dict = {}
        items = list(obj.items())
        for n, (k, v) in enumerate(items):
            if left[1] <= 0:
                out.update(items[n:])
                break
            # a key is a path when it looks like one: a separator, a scheme or a variable in it
            looks = isinstance(k, str) and bool("/" in k or "\\" in k or _SCHEME.match(k) or _VARIABLE.search(k))
            key = _kept_path(k, project, left, looks) if isinstance(k, str) else k
            if key != k:
                i = 2
                while key in out or key in obj:
                    key, i = f"{key.split(' #')[0]} #{i}", i + 1
            out[key] = omit_outside_paths(v, project, left, str(k), _depth + 1)
        return out
    if not isinstance(obj, str):
        return obj
    return _kept_path(obj, project, left, bool(_PATH_KEY.search(_key)))


def _is_inside(rel: str | None) -> bool:
    """A recorded path that may be opened in the project (devmem.store.safe_rel)."""
    from .store import safe_rel
    return safe_rel(rel) is not None


# ---------------------------------------------------------------- git

def _git(project: Path, *args: str, timeout: float = 5.0, max_out: int | None = None) -> str | None:
    try:
        from .store import git_run   # lazy: hooks that never call git start faster
        p = git_run(project, *args, timeout=timeout, max_out=max_out)
    except Exception:  # noqa: BLE001 - OSError, TimeoutExpired, ...: git is best-effort
        return None
    if p.returncode != 0:
        return None
    return p.stdout.decode("utf-8", errors="replace")


def is_git_worktree(project: Path) -> bool:
    out = _git(project, "rev-parse", "--is-inside-work-tree")
    return bool(out and out.strip() == "true")


def git_head(project: Path) -> str | None:
    out = _git(project, "rev-parse", "--verify", "-q", "HEAD")
    return out.strip() if out and out.strip() else None


MAX_STATUS_ENTRIES = 40_000    # a work tree with more changed paths than this is treated as one git cannot describe
MAX_STATUS_BYTES = 32 * 1024 * 1024   # bytes of `git status` read (more: git is stopped; treated the same way)

# What a hook STORES of git's state is small and fixed, whatever the project holds: a list of paths in an event or a
# record keeps at most STORED_PATHS paths, each cut to STORED_PATH_CHARS characters, STORED_PATHS_BYTES in all (the
# rest is counted). The full lists a hook compares from one event to the next (what was dirty, what was deleted) are
# kept as 8-byte hashes per path in `live/<sid>.gitseen` (devmem.hooks), never as text: nothing of their size reaches
# the redactor or the session state.
STORED_PATHS = 200
STORED_PATH_CHARS = 300
STORED_PATHS_BYTES = 32 * 1024
CUT_MARK = "…"             # ends a path cut to STORED_PATH_CHARS


def bounded_paths(paths) -> tuple[list[str], int]:
    """(at most STORED_PATHS of `paths`, each cut to STORED_PATH_CHARS and ending in CUT_MARK when cut, within
    STORED_PATHS_BYTES of UTF-8 in all; how many paths were left out)."""
    out: list[str] = []
    total = left = 0
    for p in paths:
        if left or len(out) >= STORED_PATHS:
            left += 1
            continue
        p = p if len(p) <= STORED_PATH_CHARS else p[:STORED_PATH_CHARS] + CUT_MARK
        n = len(p.encode("utf-8", errors="surrogatepass"))
        if total + n > STORED_PATHS_BYTES:
            left += 1
            continue
        out.append(p)
        total += n
    return out, left


def path_id(path: str) -> bytes:
    """The 8-byte hash a hook keeps of a path it only needs to compare (devmem.hooks, `live/<sid>.gitseen`)."""
    import hashlib
    return hashlib.blake2b(path.encode("utf-8", errors="surrogatepass"), digest_size=8).digest()


def git_status(project: Path, untracked: str = "normal") -> dict[str, str] | None:
    """{path relative to the project: two-letter porcelain code} or None when not a git work tree, or when git lists
    more than MAX_STATUS_ENTRIES changed paths or MAX_STATUS_BYTES of output (a hand-made index of a million entries
    that are not on disk). The dict stays in memory: what a hook stores of it is bounded (git_observe, bounded_paths).
    Paths are relative to the project directory even when the repo root is above it."""
    return git_status_sized(project, untracked)[0]


def git_status_sized(project: Path, untracked: str = "normal",
                     timeout: float = 5.0) -> tuple[dict[str, str] | None, bool]:
    """(git_status, whether it is None because git listed more than devmem keeps: MAX_STATUS_ENTRIES changed paths or
    MAX_STATUS_BYTES of output). `timeout`: seconds for git (a hook keeps the default)."""
    try:
        from .store import GitOutputLimit, git_run   # lazy: hooks that never call git start faster
        p = git_run(project, "-c", "core.quotepath=off", "status", "--porcelain=v1", "-z",
                    f"--untracked-files={untracked}", "--", ".", ":(exclude).devmemory", timeout=timeout,
                    max_out=MAX_STATUS_BYTES)
    except GitOutputLimit:
        return None, True
    except Exception:  # noqa: BLE001 - OSError, TimeoutExpired, ...: git is best-effort
        return None, False
    if p.returncode != 0:
        return None, False
    out = p.stdout.decode("utf-8", errors="replace")
    if out.count("\0") > 2 * MAX_STATUS_ENTRIES:
        return None, True
    root = _git(project, "rev-parse", "--show-toplevel")
    prefix = ""
    if root:
        try:
            prefix = Path(project).resolve().relative_to(Path(root.strip()).resolve()).as_posix()
        except (ValueError, OSError):
            prefix = ""
        prefix = "" if prefix == "." else prefix + "/" if prefix else ""
    res: dict[str, str] = {}
    parts = out.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":  # rename/copy: the next field is the source path
            i += 1
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        res[path] = code
    if len(res) > MAX_STATUS_ENTRIES:   # entries, not NUL bytes: a rename or copy entry holds two
        return None, True
    return res, False


def git_observe(project: Path) -> tuple[dict, dict[str, str] | None]:
    """(git_snapshot, the full git_status it was made from, for the caller to compare in memory; None when there is
    none)."""
    st, too_large = git_status_sized(project)
    if st is None:
        return ({"head": git_head(project), "status_too_large": True} if too_large else {}), None
    return {"head": git_head(project), "dirty": bool(st), "changed_count": len(st),
            "deleted_count": sum(1 for c in st.values() if "D" in c),
            "untracked_count": sum(1 for c in st.values() if c == "??")}, st


def git_snapshot(project: Path) -> dict:
    """What a record keeps of git's state, of a fixed size whatever the work tree holds: HEAD, whether the work tree
    is dirty, and how many paths are changed, deleted and untracked (no path list: what changed during a session is
    computed by the hooks from hashes, devmem.hooks._observe_files). {} when the project is not a git work tree;
    {"head", "status_too_large": True} when git listed more changed paths than devmem compares (git_status_sized), so
    that the record says why the rest is missing."""
    return git_observe(project)[0]


# ---------------------------------------------------------------- tool calls

def tool_entries(name: str, inp: dict, project: Path | None, redactor=None, ok: bool = True,
                 response=None, secret_call: bool | str | None = None) -> list[dict]:
    """Normalise one tool call into schema-2 `tool_calls` entries (MultiEdit -> one entry per edit).
    Text is capped at TEXT_CAP with `truncated` (cut before it is redacted, so the cost is bounded); a secret
    file's content is never kept. A call whose input names a secret file in any path field (`file_path`,
    `notebook_path`, `path`, ... e.g. Grep over `.env` or an MCP filesystem read) is marked `secret_file`."""
    inp = inp if isinstance(inp, dict) else {}

    def rc(s, limit: int) -> tuple[str, bool]:  # redact + cap, bounded by limit + margin
        return redactor.capped(s, limit) if redactor else cap(s, limit)

    fp = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") if name not in SEARCH_TOOLS \
        else inp.get("file_path")
    rel = rel_path(fp, project) if isinstance(fp, str) and fp else None    # for the secret-file check only
    from .redact import CAP_MARGIN, MAX_PATH_CHARS
    stored, omitted = project_file(fp, project) if isinstance(fp, str) and fp else (None, None)
    from .redact import call_decision, secret_call_input, unchecked_input   # the one decision, the one stored form
    decision = "ok" if not redactor else "secret" if rel and redactor.is_secret_file(rel) else \
        secret_call if isinstance(secret_call, str) else ("secret" if secret_call else "ok") \
        if secret_call is not None else call_decision(name, inp, project, redactor.secret_patterns)
    if decision == "unchecked":     # too large to check: nothing of the input is stored, only that and why
        entry = {"tool": name, **unchecked_input(inp)}
        return [dict(entry, ok=False) if not ok else entry]
    secret = decision == "secret"
    if secret:      # the stored form (its command: program and names); a too long path stays, to be left out below
        form, cut = secret_call_input(inp, getattr(redactor, "secret_patterns", None)), MAX_PATH_CHARS
        inp = {k: (v if k == "edits" or form.get(k) == v or k in ("file_path", "notebook_path", "path")
                   and isinstance(v, str) and len(v) > cut else form.get(k, "[OMITTED: secret file]"))
               for k, v in inp.items()}
    base: dict = {"tool": name}
    if stored:
        base["file"] = stored
    elif omitted:
        base["path_omitted"] = omitted
    if not ok:
        base["ok"] = False
    if secret:
        base["secret_file"] = True

    def text_pair(entry: dict, old, new) -> dict:
        trunc = False
        for key, val in (("old", old), ("new", new)):
            if val is None:
                continue
            if secret:
                entry[key] = "[OMITTED: secret file]"
                continue
            entry[key], t = rc(str(val), TEXT_CAP)
            trunc = trunc or t
        entry["truncated"] = trunc
        if secret:
            entry["secret_file"] = True
        return entry

    if name == "Edit":
        e = text_pair(dict(base), inp.get("old_string", ""), inp.get("new_string", ""))
        if inp.get("replace_all"):
            e["replace_all"] = True
        return [e]
    if name == "Write":
        e = text_pair(dict(base), None, inp.get("content", ""))
        if isinstance(response, dict) and response.get("type") in ("create", "update"):
            e["op"] = response["type"]
        return [e]
    if name == "MultiEdit":
        edits = inp.get("edits")
        edits = edits if isinstance(edits, list) else []
        out, used = [], 0
        for i, ed in enumerate(edits[:MAX_EDITS]):
            if used >= EDITS_TEXT_BUDGET:
                break
            ed = ed if isinstance(ed, dict) else {}
            e = text_pair(dict(base, edit_index=i), ed.get("old_string", ""), ed.get("new_string", ""))
            used += len(e.get("old") or "") + len(e.get("new") or "")
            if ed.get("replace_all"):
                e["replace_all"] = True
            out.append(e)
        if len(out) < len(edits):    # the rest is left out (bounded hook time), and the event says so
            out[-1]["truncated"] = True
            out[-1]["edits_omitted"] = len(edits) - len(out)
        return out or [dict(base)]
    if name == "NotebookEdit":
        e = text_pair(dict(base), None, inp.get("new_source", ""))
        if inp.get("edit_mode"):
            e["op"] = inp.get("edit_mode")
        return [e]
    if name == "Bash" or name == "PowerShell":
        e = dict(base)
        e["command"] = rc(str(inp.get("command", "")), COMMAND_CAP)[0]
        return [e]
    if name in SEARCH_TOOLS:
        e = dict(base)
        e["pattern"] = rc(str(inp.get("pattern", "")), 200)[0]
        if isinstance(inp.get("path"), str) and inp.get("path"):
            stored, omitted = project_file(inp["path"], project)
            if stored:
                e["path"] = stored
            elif omitted:
                e["path_omitted"] = omitted
        return [e]
    if name in READ_TOOLS:
        return [dict(base)]
    # anything else (Task/Agent, WebFetch, MCP tools ...): a short, redacted summary of the input
    e = dict(base)
    if inp:
        if secret:  # e.g. an MCP write_file to .env: its content is in the input
            e["input_summary"] = "[OMITTED: input names a secret file]"
        else:
            # every string of the input is redacted as the plain text it is before it is serialised: a YAML manifest
            # or a SQL statement in an MCP input keeps its lines there (in the JSON text its newlines are escapes)
            leaves = omit_outside_paths(inp, project)     # a path outside the project is not stored here either
            leaves = redactor.tree(leaves, 300 + 2 * CAP_MARGIN)[0] if redactor else leaves
            try:
                summary = json.dumps(leaves, ensure_ascii=False)
            except (TypeError, ValueError):
                summary = str(leaves)
            e["input_summary"] = rc(summary, 300)[0]
    return [e]


def output_text(response, redactor=None, budget: int | None = None) -> str:
    """Best-effort readable text of a tool response (for the raw record only). A response of another shape (Claude
    Code's Edit response with `originalFile`, a WebFetch result, an MCP structured result) is written as JSON; with a
    `redactor`, every string in it is first redacted as the plain text it is (Redactor.tree: at most `budget`
    characters, by default OUTPUT_CAP + 2 * CAP_MARGIN, lines of secret files dropped), so that JSON holds redacted
    strings. Without one, Redactor.text still decodes each JSON string literal of the result (_json_strings)."""
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        if "stdout" in response or "stderr" in response:
            return (str(response.get("stdout") or "") + ("\n[stderr]\n" + str(response["stderr"])
                                                       if response.get("stderr") else ""))
        f = response.get("file")
        if isinstance(f, dict) and "content" in f:
            return str(f.get("content") or "")
        if response.get("mode") == "content" and isinstance(response.get("content"), str):
            return response["content"]  # Grep in content mode: `path:line` text, one match per line
        if isinstance(response.get("content"), list):  # an MCP result object {"content": [blocks], ...}
            response = response["content"]
    if isinstance(response, list) and response and all(
            isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) for b in response):
        return "\n".join(b["text"] for b in response)  # MCP text blocks: their text, line structure intact
    if redactor is not None:
        from .redact import CAP_MARGIN
        response = redactor.tree(response, OUTPUT_CAP + 2 * CAP_MARGIN if budget is None else budget, omit=True)[0]
    try:
        return json.dumps(response, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(response)


# ---------------------------------------------------------------- schema 2 helpers

def new_turn(index: int, prompt: str | None, started_at: str | None, prompt_id: str | None = None) -> dict:
    t = {"index": index, "started_at": started_at, "prompt": prompt or "",
         "assistant_text": [], "result_text": None, "result_source": None,
         "tool_calls": [], "files_read": [], "files_edited": [], "files_deleted": [], "errors": []}
    if prompt_id:
        t["prompt_id"] = prompt_id
    return t


def _uniq(xs) -> list:
    seen, out = set(), []
    for x in xs:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def finalize_turn(t: dict) -> dict:
    """Derive per-turn file lists from its tool calls (order of first appearance)."""
    calls = t.get("tool_calls") or []
    t["files_read"] = _uniq(list(t.get("files_read") or []) +
                            [c.get("file") for c in calls if c.get("tool") in READ_TOOLS and c.get("ok", True)])
    t["files_edited"] = _uniq(list(t.get("files_edited") or []) +
                              [c.get("file") for c in calls if c.get("tool") in EDIT_TOOLS and c.get("ok", True)])
    t["files_deleted"] = _uniq(list(t.get("files_deleted") or []) +
                               [c.get("file") for c in calls if c.get("tool") == "Delete"])
    return t


# What one record's `file_hashes_at_capture` costs to make (derive_aggregates: `devmem import`/`run`, the hooks' Stop):
# at most this many files hashed and this many bytes read for them (each file at most store.MAX_HASH_BYTES); the
# files past either bound get no hash, and the record counts them in `file_hashes_left_out`
CAPTURE_HASH_FILES = 2_000
CAPTURE_HASH_BYTES = 256 * 1024 * 1024


def _hash_touched(project: Path, rels) -> tuple[dict[str, str | None], int]:
    """(store.hash_files of `rels` in the same order, within CAPTURE_HASH_FILES and CAPTURE_HASH_BYTES; how many were
    left out)."""
    from .store import MAX_HASH_BYTES, file_hash, project_path
    out: dict[str, str | None] = {}
    names = sorted(set(rels))
    spent = 0
    for n, r in enumerate(names):
        if n >= CAPTURE_HASH_FILES:
            return out, len(names) - n
        p = project_path(project, r)
        if p is None:
            out[r] = None
            continue
        try:
            size = os.lstat(p).st_size
        except (OSError, ValueError):
            size = 0
        cost = size if size <= MAX_HASH_BYTES else 0        # a larger file is identified by its size, not read
        if spent + cost > CAPTURE_HASH_BYTES:
            return out, len(names) - n
        spent += cost
        out[r] = file_hash(p)
    return out, 0


def derive_aggregates(rec: dict, project: Path | None = None, hash_now: bool = True) -> dict:
    """Fill the v1 aggregate fields from `turns` (in place) and return the record."""
    turns = [finalize_turn(t) for t in rec.get("turns") or []]
    rec["turns"] = turns
    rec["schema"] = SCHEMA
    first = turns[0] if turns else {}
    last_with_result = next((t for t in reversed(turns) if t.get("result_text")), None)
    rec["prompt"] = first.get("prompt", "")
    if first.get("prompt_source"):
        rec["prompt_source"] = first["prompt_source"]
    rec["result_text"] = last_with_result.get("result_text") if last_with_result else None
    rec["result_source"] = (last_with_result.get("result_source") if last_with_result else None) or \
        rec.get("result_source") or ("not available" if not last_with_result else None)
    rec["tool_calls"] = [c for t in turns for c in t.get("tool_calls") or []]
    rec["assistant_text"] = [a for t in turns for a in t.get("assistant_text") or []]
    rec["errors"] = [e for t in turns for e in t.get("errors") or []]
    gaps = [dict(g, turn=t.get("index")) for t in turns for g in t.get("transcript_gaps") or []]
    if gaps:      # only when something was left out: other records keep their old shape
        rec["transcript_gaps"] = gaps
    deleted = _uniq(f for t in turns for f in t.get("files_deleted") or [])
    edited = _uniq(f for t in turns for f in t.get("files_edited") or [])
    rec["files_read"] = sorted(set(f for t in turns for f in t.get("files_read") or []))
    rec["files_edited"] = sorted(edited)
    rec["files_deleted"] = sorted(deleted)
    if project is not None and hash_now:
        touched = {f for f in set(rec["files_read"]) | set(rec["files_edited"]) if _is_inside(f)}
        rec["file_hashes_at_capture"], left = _hash_touched(project, touched)
        if left:
            rec["file_hashes_left_out"] = left
        else:
            rec.pop("file_hashes_left_out", None)
    rec.setdefault("file_hashes_at_capture", {})
    rec.setdefault("usage", {})
    rec.setdefault("git", {})
    rec.setdefault("ended_at", None)
    rec.setdefault("model", None)
    return rec


def upgrade(meta: dict) -> dict:
    """Schema-1 record -> schema 2 in memory (one turn). Never touches the file."""
    if meta.get("schema") == SCHEMA and isinstance(meta.get("turns"), list):
        for k in ("files_deleted", "assistant_text", "errors", "tool_calls", "files_read", "files_edited"):
            meta.setdefault(k, [])
        meta.setdefault("git", {})
        meta.setdefault("ended_at", None)
        return meta
    m = dict(meta)
    calls = list(m.get("tool_calls") or [])
    turn = new_turn(0, m.get("prompt", ""), m.get("started_at"))
    turn.update({"assistant_text": [dict(a, source=a.get("source", "assistant-visible")) for a in
                                    (m.get("assistant_text") or []) if isinstance(a, dict)],
                 "result_text": m.get("result_text"), "result_source": m.get("result_source"),
                 "tool_calls": calls, "files_read": list(m.get("files_read") or []),
                 "files_edited": list(m.get("files_edited") or []),
                 "files_deleted": list(m.get("files_deleted") or []), "errors": list(m.get("errors") or [])})
    m["turns"] = [turn]
    m["schema"] = SCHEMA
    m["upgraded_from"] = meta.get("schema", 1)
    for k in ("files_deleted",):
        m.setdefault(k, [])
    m.setdefault("git", {})
    m.setdefault("ended_at", None)
    return m


# ---------------------------------------------------------------- writing

def _atomic_write(path: Path, text: str) -> None:
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate that slipped past the input boundary: never lose the write
        data = scrub_surrogates(text).encode("utf-8")
    _atomic_write_bytes(path, data)  # same bytes as write_text(newline="\n"): no newline translation


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    from .store import private_mkdir, write_private
    private_mkdir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    write_private(tmp, data)  # 0600 on POSIX
    for attempt in range(20):  # Windows: a reader holding the file open makes replace fail briefly
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if os.path.lexists(path) and not os.path.isfile(path):
                break           # a folder (or another non-file) there: waiting does not help
            time.sleep(0.02 * (attempt + 1))
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def id_tag(session_id) -> str:
    """`<first 8 characters>_<8 hex of a hash of the whole id>`: unique per id, also on a case-insensitive file system
    (two ids that share 8 leading characters, or differ only in case, get different names)."""
    import hashlib
    sid = str(session_id)
    return f"{sid[:8]}_{hashlib.sha256(sid.encode('utf-8', 'surrogatepass')).hexdigest()[:8]}"


def session_base(store: Path, started_at: str, session_id: str | None) -> Path:
    """`sessions/<start second>_<id tag>`. Records written before 2026-11 are named `<start>_<first 8 characters>`; the
    loader reads both (the record's own session_id field identifies it, not its name)."""
    tag = id_tag(session_id) if session_id else "unknown"
    return Path(store) / "sessions" / f"{stamp_of(started_at)}_{tag}"


def existing_base(store: Path, session_id) -> str | None:
    """The base name of a record this session already has in sessions/ (current name form), or None. A session whose
    state file was lost keeps writing that one record instead of starting a second one."""
    tag = id_tag(session_id)
    try:
        names = sorted(e.name for e in os.scandir(Path(store) / "sessions")
                       if e.name.endswith(f"_{tag}.meta.json") and e.is_file(follow_symlinks=False))
    except OSError:
        return None
    return names[0][: -len(".meta.json")] if names else None


def is_session_name(base: str, session_id) -> bool:
    """Is the record name `base` (without .meta.json) one of this session's, in the current or the older form?"""
    sid = str(session_id)
    return base.endswith("_" + id_tag(sid)) or base.endswith("_" + sid[:8])


# What a record's `provider` and times may hold: they are printed by `devmem sessions` and `status`, and an MCP
# client (a model) chooses the provider, so nothing that could carry a terminal control sequence is stored there.
PROVIDER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}")
WHEN_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?")


def plain_fields(rec: dict) -> dict:
    """`rec` with a provider that is not a short plain name replaced by "unknown", a started_at that is not an ISO
    time by now, and such an ended_at dropped (devmem writes all three itself; this is the last check before a write)."""
    p = rec.get("provider")
    if p is not None and not (isinstance(p, str) and PROVIDER_RE.fullmatch(p)):
        rec["provider"] = "unknown"
    if not (isinstance(rec.get("started_at"), str) and WHEN_RE.fullmatch(rec["started_at"])):
        rec["started_at"] = now_iso()
    if rec.get("ended_at") is not None and not (isinstance(rec["ended_at"], str) and WHEN_RE.fullmatch(rec["ended_at"])):
        rec["ended_at"] = None
    return rec


def write_record(store: Path, rec: dict, raw_lines: list[str] | None = None, base: Path | None = None,
                 red=None) -> Path:
    """Write `<base>.meta.json` (and `<base>.jsonl` from raw_lines if given). THE write boundary of a session record:
    every string of `rec` (any field, keys too) and every raw line is redacted here with `red` (default: a Redactor for
    this store), whatever the caller did before, so a field added later cannot reach the store unredacted (text the
    caller already redacted with the same Redactor costs a lookup). A raw line that is JSON is redacted value by value
    and written back as JSON."""
    from .redact import Redactor
    from .store import ensure_store
    ensure_store(store)  # the store's .gitignore exists before the first record does
    red = red or Redactor(store)
    rec = red.record(rec)
    plain_fields(rec)
    if raw_lines is not None:
        raw_lines = [_redact_line(line, red) for line in raw_lines]
    base = base or session_base(store, rec.get("started_at") or now_iso(), rec.get("session_id"))
    if raw_lines is not None:
        _atomic_write(base.with_suffix(".jsonl"), "".join(l if l.endswith("\n") else l + "\n" for l in raw_lines))
    rec["raw_record"] = rec.get("raw_record") or base.with_suffix(".jsonl").name
    out = {k: v for k, v in rec.items() if not k.startswith("_")}
    meta = base.with_suffix(".meta.json")
    _atomic_write(meta, json.dumps(out, indent=1, ensure_ascii=False))
    return meta


def _redact_line(line: str, red) -> str:
    try:
        obj = json.loads(line)
    except ValueError:
        return red.text(line)
    if isinstance(obj, (dict, list)):
        return json.dumps(red.record(obj), ensure_ascii=False)
    return red.text(line)


# ---------------------------------------------------------------- hook event log -> record

def torn_lines(path: Path, limit: int = 64 * 1024 * 1024) -> int:
    """How many non-blank lines of an event log do not parse (a record cut short by a full disk or a killed hook)."""
    from .store import read_regular
    data = read_regular(path, limit)
    if data is None:
        return 0
    bad = 0
    for line in data.decode("utf-8", errors="replace").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
        except ValueError:
            bad += 1
    return bad


LIVE_READ_CAP = 128 * 1024 * 1024   # bytes of a live event log read per call (one hook event, one listing)
LIVE_LINE_CAP = 16 * 1024 * 1024    # a longer line is skipped (an event devmem writes is far shorter: every text is capped)


def read_events(path: Path, info: dict | None = None) -> list[dict]:
    """The events of a hook event log, read as a stream: memory and time are bounded by LIVE_READ_CAP bytes per call
    and LIVE_LINE_CAP per line, not by the size of the whole file (a long session's log can be larger than any
    whole-file read devmem makes). A longer line is skipped, as is a torn or unparseable one (a crash mid-write); a
    log that is a FIFO, a link or a folder reads as no events. info (a dict, filled in): "complete" False when the
    log goes on past LIVE_READ_CAP, "skipped" the overlong lines."""
    from .store import _open_regular
    out: list[dict] = []
    done = {"complete": True, "skipped": 0}
    if info is not None:
        info.update(done)
    opened = _open_regular(path, None)
    if opened is None:
        return out
    fd, _size = opened
    read = 0
    with os.fdopen(fd, "rb") as f:
        while True:
            if read >= LIVE_READ_CAP:
                done["complete"] = not f.read(1)
                break
            raw = f.readline(LIVE_LINE_CAP + 1)
            if not raw:
                break
            read += len(raw)
            if len(raw) > LIVE_LINE_CAP and not raw.endswith(b"\n"):
                done["skipped"] += 1          # the rest of that line is passed over, within the same byte bound
                while read < LIVE_READ_CAP:
                    more = f.readline(1024 * 1024)
                    read += len(more)
                    if not more or more.endswith(b"\n"):
                        break
                continue
            # lines end at "\n" only: JSON written with ensure_ascii=False may hold U+2028, U+2029 or U+0085 inside a
            # string, and splitlines() cut the line there (the event was lost)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue  # a torn final line after a crash is skipped, earlier lines still count
            if isinstance(e, dict):
                out.append(e)
    if info is not None:
        info.update(done)
    return out


# A record's `not_recorded`: hook events of the session that were not recorded, counted outside the store
# (devmem.hooks._count_unrecorded) and taken into its log when the session next recorded; `kinds` by reason
# (devmem.consent.REFUSAL_KINDS), `first`/`last` the times of the first and last of them, `at_least` when the count
# reached its bound
NOT_RECORDED_NOTE = ("devmem's own count: hook events of this session that were not recorded (the store was not used "
                     "then: git could not say which of its files it tracks, it was not known to be yours, or it held a "
                     "link); what they held is not in this record")


def build_from_events(events: list[dict], project: Path | None = None, hash_now: bool = True) -> dict:
    """Schema-2 record from a hook event log (written by devmem.hooks). Pure function of the log."""
    rec: dict = {"schema": SCHEMA, "session_id": None, "provider": "claude-code", "capture_method": "hooks",
                 "started_at": None, "ended_at": None, "model": None, "git": {}, "turns": [],
                 "usage": {}, "starts": []}
    turns: list[dict] = rec["turns"]
    by_prompt: dict[str, dict] = {}
    usage_tot: dict = {}
    models: list[str] = []
    seen_tool_ids: set[str] = set()

    def turn_for(e: dict) -> dict:
        pid = e.get("prompt_id")
        if pid and pid in by_prompt:
            return by_prompt[pid]
        if not turns:  # tool activity before any captured prompt (hooks installed mid-session)
            t = new_turn(0, "", e.get("ts"))
            t["prompt_source"] = "not captured (activity before the first captured prompt)"
            turns.append(t)
        return turns[-1]

    def take_transcript(e: dict, t: dict | None = None) -> None:
        """What a Stop, SessionEnd or Abandoned event read from the transcript: text, model, usage, gaps."""
        if not (e.get("assistant_text") or e.get("transcript_gaps") or e.get("model") or e.get("usage")):
            return
        if e.get("assistant_text") or e.get("transcript_gaps"):
            t = t or turn_for(e)
            # each event carries only the text that is new since the last one (a long backlog arrives over
            # several Stops: read_transcript_since), so two Stops in one turn add up
            t["assistant_text"].extend(e.get("assistant_text") or [])
            if e.get("transcript_gaps"):
                t.setdefault("transcript_gaps", []).extend(e["transcript_gaps"])
        if e.get("model"):
            models.append(e["model"])
        for k, v in (e.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                usage_tot[k] = usage_tot.get(k, 0) + v

    def take_files(e: dict, t: dict) -> None:
        """What a check at a turn's end saw on disk and in git (a Stop, a StopFailure, or the TurnEnd the hooks write for
        a turn that ended without either). A turn a Stop hook continued has a check per Stop: they add up."""
        for f in e.get("deleted") or []:
            if f not in t["files_deleted"]:
                t["files_deleted"].append(f)
                t["tool_calls"].append({"tool": "Delete", "file": f, "source": e.get("deleted_source", "observed")})
        if isinstance(e.get("deleted_more"), int) and e["deleted_more"] > 0:
            # the event kept STORED_PATHS of them (bounded_paths): the turn says how many more there were
            t["files_deleted_more"] = int(t.get("files_deleted_more") or 0) + e["deleted_more"]
        if e.get("changed_observed"):
            obs = t.setdefault("files_changed_observed", {"files": [], "source": "git status"})
            obs["files"] = _uniq(list(obs.get("files") or []) + list(e["changed_observed"]))
            if isinstance(e.get("changed_observed_more"), int) and e["changed_observed_more"] > 0:
                obs["more"] = int(obs.get("more") or 0) + e["changed_observed_more"]
        if e.get("git_head"):
            rec["git"]["head_end"] = e["git_head"]
        if isinstance(e.get("git_unattributed"), dict):
            # what git showed at this check that was not attributed to the session, counted
            un = {k: int(v) for k, v in e["git_unattributed"].items() if k in UNATTRIBUTED_WHY
                  and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
            old = {k: v for k, v in (t.get("git_unattributed") or {}).items() if k in UNATTRIBUTED_WHY}
            for k, v in un.items():
                old[k] = old.get(k, 0) + v
            if old:
                t["git_unattributed"] = {**old, "source": "git status; " + "; ".join(UNATTRIBUTED_WHY[k] for k in old)}
            if "deleted" in un or "changed" in un:
                rec["git"]["start_unknown"] = True

    for e in events:
        ev = e.get("event")
        rec["session_id"] = rec["session_id"] or e.get("session_id")
        rec["started_at"] = rec["started_at"] or e.get("ts")
        if ev == "SessionStart":
            rec["starts"].append({"ts": e.get("ts"), "source": e.get("source")})
            if e.get("model"):
                models.append(e["model"])
            if e.get("git") and not rec["git"].get("head_start") and "head_start" not in rec["git"]:
                g = e["git"]
                rec["git"].update({"head_start": g.get("head"), "dirty_start": g.get("dirty")})
                if g.get("status_too_large"):
                    rec["git"]["status_too_large"] = True
        elif ev == "UserPromptSubmit":
            t = new_turn(len(turns), e.get("prompt", ""), e.get("ts"), e.get("prompt_id"))
            t["prompt_source"] = PROMPT_FROM_USER  # typed by the user; delivered verbatim by Claude Code
            turns.append(t)
            if e.get("prompt_id"):
                by_prompt[e["prompt_id"]] = t
            if e.get("git") and "head_start" not in rec["git"]:
                rec["git"].update({"head_start": e["git"].get("head"), "dirty_start": e["git"].get("dirty")})
        elif ev in ("PostToolUse", "PostToolUseFailure"):
            tid = e.get("tool_use_id")
            if tid and tid in seen_tool_ids:
                continue
            if tid:
                seen_tool_ids.add(tid)
            t = turn_for(e)
            for c in e.get("calls") or []:
                c = dict(c)
                if e.get("agent_id"):
                    c["agent_id"] = e["agent_id"]
                t["tool_calls"].append(c)
            if ev == "PostToolUseFailure" and not e.get("is_interrupt"):
                t["errors"].append({"tool": e.get("tool"), "error": str(e.get("error") or "")[:300]})
        elif ev in ("Stop", "StopFailure", "TurnEnd"):
            t = turn_for(e)
            if ev == "Stop":
                if e.get("last_assistant_message") is not None:
                    t["result_text"] = e.get("last_assistant_message")
                    t["result_source"] = "assistant-visible (Stop hook last_assistant_message)"
                take_transcript(e, t)
            elif ev == "StopFailure":
                t["errors"].append({"error": f"turn ended by API error: {e.get('error')}", "source": "StopFailure hook"})
            elif e.get("ended_by") in TURN_END_WHY:
                t["turn_end"] = TURN_END_WHY[e["ended_by"]]
            t["ended_at"] = e.get("ts")
            take_files(e, t)
        elif ev == "PostCompact":
            rec.setdefault("compactions", []).append(
                {"ts": e.get("ts"), "trigger": e.get("trigger"), "summary": e.get("compact_summary"),
                 "source": "model-generated compaction summary (PostCompact hook)"})
        elif ev == "SessionEnd":
            take_transcript(e)
            rec["ended_at"] = e.get("ts")
            rec["ended_reason"] = e.get("reason")
            if e.get("git"):
                rec["git"]["head_end"] = e["git"].get("head")
                rec["git"]["dirty_end"] = e["git"].get("dirty")
        elif ev == "Abandoned":
            take_transcript(e)
            rec["ended_at"] = rec["ended_at"] or e.get("ts")
            if not rec.get("ended_reason"):         # a SessionEnd in the log (cut off before it finished) says why
                rec["ended_reason"] = "no SessionEnd seen (finalised later)"
        elif ev == "NotRecorded":                   # events of this session the hooks counted but did not record
            n = e.get("count")
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                nr = rec.setdefault("not_recorded", {"events": 0, "kinds": {}, "first": None, "last": None,
                                                     "source": NOT_RECORDED_NOTE})
                nr["events"] += n
                kinds = e.get("kinds")
                for k, v in (kinds.items() if isinstance(kinds, dict) else ()):
                    if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v > 0:
                        nr["kinds"][k[:40]] = nr["kinds"].get(k[:40], 0) + v
                for key, pick in (("first", min), ("last", max)):
                    t = e.get(key)
                    if isinstance(t, str) and WHEN_RE.fullmatch(t):
                        nr[key] = t if nr[key] is None else pick(nr[key], t)
                if e.get("at_least") is True:
                    nr["at_least"] = True
        elif ev == "Finalized":                     # an ended session finished later (devmem.hooks._finalize_abandoned)
            take_transcript(e)
            if isinstance(e.get("git"), dict):
                rec["git"]["head_end"] = e["git"].get("head")
                if "dirty" in e["git"]:
                    rec["git"]["dirty_end"] = e["git"]["dirty"]
    _assign_texts_by_time(turns)
    # a turn that saw tool activity resumed after SessionEnd re-opens the session
    if rec["ended_at"] and events and events[-1].get("event") not in ("SessionEnd", "Abandoned", "Finalized"):
        rec["ended_at"] = None
        rec.pop("ended_reason", None)
    rec["model"] = models[-1] if models else None
    if usage_tot:
        rec["usage"] = {k: usage_tot.get(k) for k in USAGE_KEYS if k in usage_tot}
        rec["usage"]["num_turns"] = len(turns)
        rec["usage_source"] = "Claude Code transcript (best-effort; format undocumented)"
    if not rec["starts"]:
        rec.pop("starts")
    return derive_aggregates(rec, project, hash_now=hash_now)


def _when(ts) -> datetime | None:
    try:
        return _parse_when(ts) if ts else None
    except ValueError:
        return None


def _assign_texts_by_time(turns: list[dict]) -> None:
    """The transcript is written asynchronously, so text read at one Stop can belong to the previous turn.
    Re-home each timestamped text to the turn whose time window contains it."""
    starts = [_when(t.get("started_at")) for t in turns]
    if len(turns) < 2 or any(s is None for s in starts):
        return
    pool = [(a, i) for i, t in enumerate(turns) for a in t.get("assistant_text") or []]
    for t in turns:
        t["assistant_text"] = []
    for a, i in pool:
        w = _when(a.get("ts"))
        if w is not None:
            i = max([k for k, s in enumerate(starts) if s is not None and s <= w], default=i)
        turns[i]["assistant_text"].append(a)


# ---------------------------------------------------------------- loading

def _live_dir(store: Path) -> Path:
    return Path(store) / "live"


# the files a hook keeps in live/ for one session (devmem.hooks._paths)
# (.turn: the times of the session's last turns; .open: the start of its open turn; .edits: hashes of the paths its
# file tools edited; .prompt: a prompt whose hook did not get the session lock. Each is small and of a bounded size,
# and they are read by the hooks of the project's other sessions: devmem.hooks._other_sessions)
# (.end: empty, the session's SessionEnd left finishing it to a later SessionStart: devmem.hooks._finalize_abandoned)
LIVE_SUFFIXES = (".jsonl", ".state.json", ".lock", ".edited", ".route", ".gitseen", ".turn", ".open", ".edits",
                 ".prompt", ".end")


# records the last load_sessions call skipped, as "file: reason" (devmem status and doctor report them)
LOAD_PROBLEMS: list[str] = []
LOAD_REFUSED: list[str] = []     # why the last load_sessions call loaded nothing (devmem.consent.recorded_refusal)

_LIST_OF_STR = ("files_read", "files_edited", "files_deleted")
# the counts a turn's `git_unattributed` may hold (devmem.hooks._observe_files), and what each means
UNATTRIBUTED_WHY = {
    "deleted": "deletions git showed while the state before was unknown",
    "changed": "changes git showed while the state before was unknown",
    "changed_not_in_interval": ("paths that newly appeared in git's list with a modification time from before the "
                                "previous check (before the turn's prompt when that check is unknown) or in the future "
                                "(un-ignored, restored or unpacked with an old time)"),
    "changed_between_turns": ("paths that newly appeared in git's list with a time after the previous check but before "
                              "this turn's prompt, while no turn of this session was open: changed outside its turns (by "
                              "you, another program or session, or work this session left running in the "
                              "background)"),
    "folder_not_in_interval": ("new folders in git's list whose own time and the times of the entries looked at inside "
                               "them are from before the previous check or in the future"),
    "changed_not_dated": ("paths that newly appeared in git's list and could not be looked at (not a plain path inside "
                          "the project, reached through a link, or gone)"),
    "changed_turn_start_unknown": ("paths that newly appeared in git's list at the end of a turn whose start is unknown "
                                   "(its prompt was not recorded; the live state was lost or damaged; the start it "
                                   "holds is more than 7 days old, before the session's start or more than a minute "
                                   "ahead; or a Stop came again without a prompt and without saying that a Stop hook "
                                   "continued the turn)"),
    "changed_not_looked_at": "paths that newly appeared in git's list past the number looked at per check",
    "changed_after_last_activity": ("paths dated after the end of a turn that was noticed only later (no Stop came "
                                    "before the next prompt, a SessionEnd or a resumed session's start; or its Stop did "
                                    "not get the session's lock), which is its own last event (its prompt or a tool "
                                    "call of the main agent) or that Stop, and before it was noticed: a tool call of "
                                    "that turn still running then, or an edit made after it; which one cannot be "
                                    "told"),
    "changed_turn_end_unknown": ("paths dated inside an earlier turn whose end could not be checked (it ended without "
                                 "a Stop and the next prompt's hook did not get the session's lock, or git could not "
                                 "be asked when it ended): not attributed"),
    "changed_while_other_session_active": ("paths dated inside this turn while another session of this project had a "
                                           "turn open or was recording tool calls (its live files say so): not "
                                           "attributed to either session"),
    "changed_by_other_session_tool": ("paths dated inside this turn that another session of this project recorded "
                                      "editing with its file tools (Write, Edit, MultiEdit, NotebookEdit) since this "
                                      "turn began: not attributed to this session"),
    "changed_other_sessions_not_read": ("paths dated inside this turn while more sessions of this project could "
                                        "overlap it (an event since it began, or an open turn that is not stale) than "
                                        "one check reads: not attributed"),
}
# turn["turn_end"]: why a turn has no Stop (the TurnEnd event devmem.hooks writes when it notices)
TURN_END_WHY = {
    "next_prompt": ("no Stop: the next prompt came while this turn was open (interrupted, as with Esc, or a prompt "
                    "sent while it ran)"),
    "session_end": "no Stop: the session ended while this turn was open",
    "resume": "no Stop: the session was resumed while this turn was open (the earlier run ended mid-turn)",
    "stop_unchecked": ("its Stop came while another hook of this session held the session's lock: the turn was checked "
                       "when the next event noticed it"),
}
STATE_TIME_MIN = 946_684_800          # 2000-01-01: a time a live state or a per-user note may hold, at the least
STATE_TIME_AHEAD_S = 86_400           # and at most this far ahead of now


def state_time(v) -> float | None:
    """`v` as a time in seconds when it is a finite number between STATE_TIME_MIN and now + STATE_TIME_AHEAD_S, else
    None (a string, a list, a bool, NaN, an infinity, an integer of 400 digits: a stored value is data, never trusted to
    be a time)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, int) and not (STATE_TIME_MIN <= v <= time.time() + STATE_TIME_AHEAD_S):
        return None                       # compared as an integer: float() of a huge one raises OverflowError
    f = float(v)
    if f != f or not (STATE_TIME_MIN <= f <= time.time() + STATE_TIME_AHEAD_S):
        return None
    return f
_STR_FIELDS = ("session_id", "started_at", "ended_at", "prompt", "result_text", "result_source", "provider",
               "capture_method", "raw_record", "model", "prompt_source")


def _str_or_none(v):
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return None


def _normalize_part(d: dict) -> dict:
    for k in _STR_FIELDS:
        if k in d:
            d[k] = _str_or_none(d[k])
    for k in _LIST_OF_STR:
        if k in d:
            v = d[k]
            d[k] = [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    if "tool_calls" in d:
        v = d["tool_calls"]
        d["tool_calls"] = [c for c in v if isinstance(c, dict)] if isinstance(v, list) else []
        for c in d["tool_calls"]:
            for k in ("tool", "file", "command", "old", "new", "pattern"):
                if k in c:
                    c[k] = _str_or_none(c[k])
    for k in ("assistant_text", "errors"):
        if k not in d:
            continue
        v = d[k]
        d[k] = [x for x in v if isinstance(x, (dict, str))] if isinstance(v, list) else []
    for k in ("usage", "git"):
        if k in d and not isinstance(d[k], dict):
            d[k] = {}
    if "transcript_gaps" in d:      # optional (records without it are left as they are): dicts with a text note
        v = d["transcript_gaps"]
        d["transcript_gaps"] = [g for g in v if isinstance(g, dict)] if isinstance(v, list) else []
        for g in d["transcript_gaps"]:
            g["note"] = _str_or_none(g.get("note")) or gap_note(g)
    return d


def normalize_record(rec: dict) -> dict:
    """Coerce a loaded record's fields to the types the readers expect (a hand-edited, truncated or foreign
    meta.json can hold a number, list or object anywhere): strings stay strings or become None, lists keep only
    items of the right type, `file_hashes_at_capture` keeps str -> str pairs. Never raises on a dict."""
    _normalize_part(rec)
    h = rec.get("file_hashes_at_capture")
    rec["file_hashes_at_capture"] = ({k: v for k, v in h.items() if isinstance(k, str) and isinstance(v, str)}
                                     if isinstance(h, dict) else {})
    turns = rec.get("turns")
    rec["turns"] = [_normalize_part(t) for t in turns if isinstance(t, dict)] if isinstance(turns, list) else []
    return rec


def load_sessions(store: Path, include_live: bool = True, project: Path | None = None) -> list[dict]:
    """All session records in `store`, upgraded to schema 2, chronological by file name; [] for a store whose
    recordings are not this user's (devmem.consent.recorded_refusal, asked here so that no caller can skip it:
    `LOAD_REFUSED` then holds the reason).

    Every record carries `_meta` (its meta file name, or `live/<id>.jsonl` for a live session that has no
    record yet). Live sessions whose event log is newer than their record are rebuilt from the log in memory
    and marked `in_progress: true`. Files are never rewritten here."""
    from .consent import recorded_refusal
    LOAD_PROBLEMS.clear()
    LOAD_REFUSED.clear()
    why = recorded_refusal(project, store)
    if why:
        LOAD_REFUSED.append(why)
        return []
    return _load_all(store, include_live, project)


def _load_all(store: Path, include_live: bool = True, project: Path | None = None) -> list[dict]:
    """load_sessions without the trust decision: for deleting (forget_*) only, which shows no recorded text."""
    store = Path(store)
    out: list[dict] = []
    by_sid: dict[str, int] = {}
    LOAD_PROBLEMS.clear()
    for p in sorted((store / "sessions").glob("*.meta.json")):
        try:
            m = json.loads(read_store_text(p))
        except (OSError, ValueError, RecursionError):
            LOAD_PROBLEMS.append(f"{p.name}: unreadable")
            continue
        if not isinstance(m, dict):
            LOAD_PROBLEMS.append(f"{p.name}: not a JSON object")
            continue
        try:
            m = normalize_record(upgrade(m))
        except Exception as e:  # noqa: BLE001 - one bad record is skipped (and reported), never the whole store
            LOAD_PROBLEMS.append(f"{p.name}: {type(e).__name__}")
            continue
        m["_meta"] = p.name
        m["_mtime"] = _mtime(p)
        if m.get("session_id"):
            by_sid[str(m["session_id"])] = len(out)
        out.append(m)
    if include_live:
        for log in sorted(_live_dir(store).glob("*.jsonl")):
            sid = log.stem
            idx = by_sid.get(sid)
            if idx is not None and out[idx]["_mtime"] >= _mtime(log):
                out[idx]["in_progress"] = True
                continue
            events = read_events(log)
            if not events:
                continue
            try:
                rec = normalize_record(build_from_events(events, project, hash_now=False))
            except Exception as e:  # noqa: BLE001 - a live log with a malformed event: skip this session only
                LOAD_PROBLEMS.append(f"live/{log.name}: {type(e).__name__}")
                continue
            rec["in_progress"] = True
            rec["raw_record"] = f"../live/{log.name}"
            if idx is not None:
                rec["_meta"] = out[idx]["_meta"]
                out[idx] = rec
            else:
                rec["_meta"] = f"live/{log.name}"
                out.append(rec)
    for m in out:
        m.pop("_mtime", None)
    return out


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------- deleting memory

def _record_files(store: Path, rec: dict) -> list[Path]:
    """Every file that belongs to one session: its record and raw record, and for a session captured by the
    hooks also the live log, state, lock and marker files and the record/raw copy named in its state (an
    in-progress session is loaded from its live log, but its last Stop already wrote sessions/<base>.*);
    for a `devmem begin`/`end` session its pending begin, if one is left. Temp files of interrupted atomic
    writes are included."""
    import glob as _glob
    store = Path(store)
    sessions_dir, live = store / "sessions", _live_dir(store)
    files: list[Path] = []
    bases: set[str] = set()
    # every name below comes from a record, which a repository or an older version may have written: only bare file
    # names that pass safe_id are used (never a path, a drive or `..`), and _remove checks the result again
    meta = rec.get("_meta") or ""
    if isinstance(meta, str) and safe_id(meta):
        files.append(sessions_dir / meta)
        if meta.endswith(".meta.json"):
            bases.add(meta[:-len(".meta.json")])
    raw = rec.get("raw_record")
    if isinstance(raw, str) and safe_id(raw):
        files.append(sessions_dir / raw)
    sid = str(rec.get("session_id") or "")
    if safe_id(sid):
        for suffix in LIVE_SUFFIXES:
            files.append(live / f"{sid}{suffix}")
        files += live.glob(f".{_glob.escape(sid)}.*")
        try:
            st = json.loads(read_store_text(live / f"{sid}.state.json"))
            if isinstance(st, dict) and isinstance(st.get("base"), str):
                bases.add(st["base"])
        except (OSError, ValueError):
            pass
    for b in bases:
        if safe_id(b):
            files += [sessions_dir / f"{b}.meta.json", sessions_dir / f"{b}.jsonl"]
            files += sessions_dir.glob(f".{_glob.escape(b)}.*")
    begin = rec.get("begin_id")
    if isinstance(begin, str) and safe_id(begin):
        files += [store / "pending" / f"{begin}.json", store / "pending" / f"{begin}.json.claim"]
    return [f for f in dict.fromkeys(files) if f.is_file()]


def _in_store(store: Path, p: Path) -> bool:
    """`p` is an entry of the store or of one of its folders, reached without a link: its folder's real path is the
    folder itself, inside the store's real path. A link `p` itself is fine (unlinking removes the link only)."""
    real_store = os.path.realpath(store)
    parent = os.path.abspath(p.parent)
    rel = os.path.relpath(parent, os.path.abspath(store))
    if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        return False
    expected = os.path.normpath(os.path.join(real_store, rel))
    return os.path.normcase(os.path.realpath(parent)) == os.path.normcase(expected)


def _remove(paths: list[Path], store: Path | None = None) -> list[str]:
    removed = []
    for p in paths:
        if store is not None and not _in_store(Path(store), Path(p)):
            continue   # outside the store, or reached through a linked folder: never deleted
        try:
            p.unlink()
            removed.append(str(p))
        except FileNotFoundError:
            pass
    return removed


_SID_IN_TEXT = re.compile(r'"session_id"\s*:\s*"([^"\\]{1,256})"')
_UNLOADED_HEAD = 64 * 1024     # bytes of an unloadable record read to find its session id


def _unloaded_sessions(store: Path, loaded: list[dict]) -> dict[str, float]:
    """Sessions with files in the store that load_sessions could not load: a record whose .meta.json is torn or
    missing (an orphan raw .jsonl), or a state file without a log. {session id: start time (epoch) or 0}. The id
    is read from the file (the first `"session_id": "..."` in its first 64 KB) or, in live/, from the file name."""
    known = {str(s.get("_meta")) for s in loaded} | {str(s.get("raw_record")) for s in loaded}
    out: dict[str, float] = {}
    sessions_dir = Path(store) / "sessions"
    try:
        entries = sorted(os.scandir(sessions_dir), key=lambda e: e.name)
    except OSError:
        entries = []
    for e in entries:
        if e.name in known or e.name.startswith(".") or not e.name.endswith((".meta.json", ".jsonl")):
            continue
        try:
            head = read_store_text(Path(e.path))[:_UNLOADED_HEAD]
        except (OSError, ValueError):
            continue
        m = _SID_IN_TEXT.search(head)
        if m and safe_id(m.group(1)) and is_session_name(e.name.split(".", 1)[0], m.group(1)):
            out.setdefault(m.group(1), _mtime(Path(e.path)))
    live = _live_dir(store)
    loaded_ids = {str(s.get("session_id")) for s in loaded}
    for suffix in (".state.json", ".jsonl"):
        for p in live.glob(f"*{suffix}"):
            sid = p.name[: -len(suffix)]
            if sid and not sid.startswith(".") and safe_id(sid) and sid not in loaded_ids:
                out.setdefault(sid, _mtime(p))
    return out


def _files_named_for(store: Path, sid: str) -> list[Path]:
    """Every store file whose name says it belongs to session `sid`, whether or not its record loads: sessions/
    `<start>_<id tag>.*` (and temp files of interrupted writes), an old-form `<start>_<first 8>.*` whose text names the
    full id, and live/`<sid>.*`."""
    import glob as _glob
    if not safe_id(sid):
        return []
    store = Path(store)
    sessions_dir, live = store / "sessions", _live_dir(store)
    tag = _glob.escape(id_tag(sid))
    files = list(sessions_dir.glob(f"*_{tag}.*")) + list(sessions_dir.glob(f".*_{tag}.*"))
    for p in list(sessions_dir.glob(f"*_{_glob.escape(sid[:8])}.*")) + list(sessions_dir.glob(f".*_{_glob.escape(sid[:8])}.*")):
        try:
            if sid in read_store_text(p)[:_UNLOADED_HEAD]:
                files.append(p)
        except (OSError, ValueError):
            pass
    esc = _glob.escape(sid)
    files += list(live.glob(f"{esc}.*")) + list(live.glob(f".{esc}.*"))
    return [f for f in dict.fromkeys(files) if f.is_file()]


def _forget_files(store: Path, picked: list[dict], extra_ids: list[str]) -> list[Path]:
    files = [f for s in picked for f in _record_files(store, s)]
    for sid in {str(s.get("session_id") or "") for s in picked} | set(extra_ids):
        files += _files_named_for(store, sid)
    # The opt-in MCP call log cannot tell which session a call was about (a get_context_pack task, a read_session id),
    # so any forget clears it: nothing of a forgotten session may stay in the store.
    log = Path(store) / "mcp_calls.jsonl"
    if log.is_file():
        files.append(log)
    return sorted(set(files))


def forget_session(store: Path, id_prefix: str) -> dict:
    """Delete one session (record, raw record, live log and state, also when its record no longer loads) by
    session-id prefix, and the MCP call log. Raises ValueError if the prefix matches no session or more than one."""
    id_prefix = (id_prefix or "").strip()
    if len(id_prefix) < 4:
        raise ValueError("give at least 4 characters of the session id")
    loaded = _load_all(store)
    matches = [s for s in loaded if str(s.get("session_id") or "").startswith(id_prefix)]
    unloaded = [i for i in _unloaded_sessions(store, loaded) if i.startswith(id_prefix)]
    ids = sorted({str(s.get("session_id")) for s in matches} | set(unloaded))
    if not ids:
        raise ValueError(f"no session with id starting {id_prefix!r}")
    if len(ids) > 1:
        raise ValueError(f"{id_prefix!r} matches {len(ids)} sessions: {', '.join(i[:12] for i in ids)}")
    return {"sessions": ids, "removed": _remove(_forget_files(store, matches, unloaded), store)}


def forget_before(store: Path, before: str) -> dict:
    """Delete every session that started before `before` (ISO date or datetime, UTC); a session whose record no
    longer loads counts by the time its files were last written. Clears the MCP call log too."""
    cutoff = _parse_when(before)
    loaded = _load_all(store)
    picked = [s for s in loaded if s.get("started_at") and _parse_when(s["started_at"]) < cutoff]
    unloaded = [i for i, t in _unloaded_sessions(store, loaded).items() if t and t < cutoff.timestamp()]
    ids = sorted({str(s.get("session_id")) for s in picked} | set(unloaded))
    return {"sessions": ids, "removed": _remove(_forget_files(store, picked, unloaded), store)}


def forget_all(store: Path) -> dict:
    """Delete all captured sessions (records, raw records, live logs and their state, pending begins) and the
    MCP call log. The project map and settings stay; rebuild-able indexes are the caller's job."""
    store = Path(store)
    sids = sorted({str(s.get("session_id")) for s in _load_all(store)})
    return {"sessions": sids, "removed": _remove(forget_all_files(store), store)}


def forget_all_files(store: Path) -> list[Path]:
    """The files forget_all deletes (`devmem forget --all` counts them before it asks)."""
    store = Path(store)
    files = list((store / "sessions").glob("*")) + list(_live_dir(store).glob("*"))
    files += list((store / "pending").glob("*")) if (store / "pending").exists() else []
    for name in ("pending.json", "mcp_calls.jsonl"):
        if (store / name).exists():
            files.append(store / name)
    return sorted(f for f in set(files) if f.is_file())


def _parse_when(s: str) -> datetime:
    s = str(s).strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s += "T00:00:00+00:00"
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- transcript (best-effort)

# What one Stop reads of the transcript. The caller redacts every text it gets (about 2-5 microseconds a character on
# one Windows machine, linear), so the text handed over per event is what bounds the hook: 7.9 MB of new transcript
# once took the Stop hook 25 s of a 30 s timeout. Past either bound the offset stops at the last line taken and the
# next Stop continues from there (texts keep their timestamps, so they still go to the turn they belong to).
TRANSCRIPT_READ_CAP = 2 * 1024 * 1024        # bytes of transcript read per event
TRANSCRIPT_TEXT_BUDGET = 200_000             # characters of assistant text handed over per event (each up to TEXT_CAP)
LONG_LINE_SCAN = 64 * 1024 * 1024            # bytes scanned per event for the end of a line longer than the read cap
MSG_IDS_KEPT = 32                            # message ids remembered between reads (usage is counted once per message)

# Text the transcript held that a record does not: each turn's `transcript_gaps` (and the record's, all turns) lists
# {"what": "skipped-line" | "cut" | "unread", "bytes" | "chars": n, "note": <this text>, "source": GAP_SOURCE}. Absent
# when nothing was left out, and in records written before it existed (they load unchanged).
GAP_SOURCE = "devmem capture note (not assistant text)"


def gap_note(gap: dict) -> str:
    """The human-readable label of one transcript gap (what was left out, and why)."""
    what, n = gap.get("what"), gap.get("bytes") or gap.get("chars") or 0
    n = n if isinstance(n, int) and not isinstance(n, bool) else 0
    if what == "skipped-line":
        return (f"one transcript line of {n:,} bytes{'' if gap.get('complete', True) else ' or more'} was skipped: "
                f"it is longer than the {TRANSCRIPT_READ_CAP // (1024 * 1024)} MB devmem reads at a time, so any "
                "assistant-visible text in it is not in this record")
    if what == "cut":
        return (f"one transcript line held more assistant-visible text than devmem takes in at a time "
                f"({TRANSCRIPT_TEXT_BUDGET:,} characters); its remaining {n:,} characters are not in this record")
    if what == "unread":
        return (f"the last {n:,} bytes of the transcript were not read before the session was finalised (more than "
                "devmem reads at the end of a session); any assistant-visible text in them is not in this record")
    return "part of the transcript was not read; its assistant-visible text is not in this record"


def make_gap(what: str, **counts) -> dict:
    g = {"what": what, **counts}
    g["note"], g["source"] = gap_note(g), GAP_SOURCE
    return g


def _skip_long_line(f, offset: int, read: int) -> tuple[int, bool]:
    """(offset after a line longer than the read cap, whether its end was found). None of the line is used; at most
    LONG_LINE_SCAN bytes are scanned. When its end is not found by then, the offset reached (the rest of the line
    does not parse and is skipped by the next event, as any garbled line is)."""
    skipped = read
    while skipped < LONG_LINE_SCAN:
        block = f.read(1024 * 1024)
        if not block:
            break
        i = block.find(b"\n")
        if i >= 0:
            return offset + skipped + i + 1, True
        skipped += len(block)
    return offset + skipped, False


def transcript_backlog(path: str | None, offset: int) -> int:
    """Bytes of the transcript after `offset` (0 when it is missing, not a regular file or on a network share)."""
    if not path:
        return 0
    from .store import is_remote_path
    if is_remote_path(path):
        return 0
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return 0
    import stat as _stat
    if not _stat.S_ISREG(st.st_mode):
        return 0
    return max(0, st.st_size - offset) if st.st_size >= offset else st.st_size


def _id_key(mid: str) -> str:
    import hashlib
    return hashlib.sha256(mid.encode("utf-8", "replace")).hexdigest()[:20]


def _transcript_line(raw: bytes, seen_msg: set) -> list[dict]:
    if not raw.strip():
        return []
    try:
        e = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        return []
    if not isinstance(e, dict) or e.get("type") != "assistant" or e.get("isSidechain"):
        return []
    e = scrub_surrogates(e)
    out: list[dict] = []
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    if msg.get("model"):
        out.append({"kind": "model", "model": msg["model"]})
    # the id as a short hash: callers keep it between events, and no transcript string is stored unredacted
    mid = _id_key(msg["id"]) if isinstance(msg.get("id"), str) and msg["id"] else None
    u = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
    if u and mid and mid not in seen_msg:
        seen_msg.add(mid)
        out.append({"kind": "usage", "id": mid, "usage": {
            "input_tokens": u.get("input_tokens") or 0, "output_tokens": u.get("output_tokens") or 0,
            "cache_read_tokens": u.get("cache_read_input_tokens") or 0,
            "cache_creation_tokens": u.get("cache_creation_input_tokens") or 0}})
    content = msg.get("content")
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and str(b.get("text", "")).strip():
                out.append({"kind": "text", "text": str(b["text"]).strip(), "ts": e.get("timestamp")})
    return out


def read_transcript_since(path: str | None, offset: int = 0, max_bytes: int = TRANSCRIPT_READ_CAP,
                          text_budget: int = TRANSCRIPT_TEXT_BUDGET,
                          seen_msg: set | None = None) -> tuple[list[dict], int]:
    """Assistant-visible text, model and usage from the Claude Code transcript file, from byte `offset`.

    BEST-EFFORT: the transcript format is not a documented interface (only `transcript_path` is). Lines that
    do not parse or have an unexpected shape are skipped. Thinking blocks are never read into the result.
    Returns (items, new_offset); items are {"kind": "text"|"model"|"usage"|"gap", ...}. At most `max_bytes` are read
    and about `text_budget` characters of text returned (each text counted up to TEXT_CAP, what the caller keeps): the
    offset then stops after the last line taken and the next call continues there. A single line holding more text
    than the budget is cut to it (its later text blocks are not returned: a "cut" gap says how much), and a line
    longer than `max_bytes` is skipped (a "skipped-line" gap); see make_gap.

    Claude Code writes one line per content block, each with the message's id and usage: usage is returned once per
    message id. `seen_msg` carries the ids already counted across calls (the caller keeps it between events), so a
    message whose lines fall on both sides of a read's end is not counted twice."""
    items: list[dict] = []
    if not path:
        return items, offset
    from .store import _open_regular, is_remote_path, read_bounded
    if is_remote_path(path):
        return items, offset
    opened = _open_regular(path, None)     # a regular file only: never a FIFO (it would block) or a device
    if opened is None:
        return items, offset
    fd, size = opened
    try:
        if size < offset:  # file was replaced
            offset = 0
        with os.fdopen(fd, "rb") as f:
            f.seek(offset)
            data = read_bounded(f, size - offset, max_bytes)     # what is there, not the cap, asked for up front
            # only consume complete lines (the file is written asynchronously)
            end = data.rfind(b"\n")
            if end < 0:
                if len(data) < max_bytes:
                    return items, offset           # a line still being written
                new, complete = _skip_long_line(f, offset, len(data))
                return [dict(make_gap("skipped-line", bytes=new - offset, complete=complete), kind="gap")], new
    except OSError:
        return items, offset
    seen_msg = set() if seen_msg is None else seen_msg
    used, pos = 0, 0
    while pos <= end:
        nl = data.find(b"\n", pos)
        line = _transcript_line(data[pos:nl], seen_msg)
        cost = sum(min(len(it["text"]), TEXT_CAP) for it in line if it["kind"] == "text")
        if used + cost > text_budget:
            if used:
                break                              # this line and the rest: the next call
            kept, dropped = [], 0
            for it in line:                        # one line over the whole budget: cut to it
                if it["kind"] == "text":
                    if used >= text_budget:
                        dropped += len(it["text"])
                        continue
                    used += min(len(it["text"]), TEXT_CAP)
                kept.append(it)
            if dropped:
                kept.append(dict(make_gap("cut", chars=dropped), kind="gap"))
            line, cost = kept, 0
        used += cost
        items.extend(line)
        pos = nl + 1
    return items, offset + pos


def move_file(src: Path, dst: Path) -> None:
    """Move a store file (a session's raw log between live/ and sessions/). A rename when possible; otherwise (another
    file system, or on Windows a reader holding `dst` open) a copy that is private from the start (_copy_private)."""
    from .store import private_mkdir, tighten
    private_mkdir(dst.parent)
    try:
        os.replace(src, dst)
    except OSError:
        _copy_private(src, dst)
        try:
            os.remove(src)
        except OSError:
            pass
    tighten(dst)


def _copy_private(src: Path, dst: Path) -> None:
    """Copy `src` into `dst` without `dst` ever being readable by others (POSIX) or written through a link: a new
    `dst` is created 0600, an existing one is emptied and narrowed (fchmod) before any byte is written, and both are
    opened as regular files only (O_NOFOLLOW, O_NONBLOCK: a FIFO fails instead of blocking). In place rather than by
    rename, because on Windows the rename is what a reader holding `dst` open makes fail."""
    from .store import PRIVATE_FILE, _open_regular, private_fd
    opened = _open_regular(src, None)
    if opened is None:
        raise OSError(f"{src} is not a plain file; not moved")
    fd_in, _size = opened
    try:
        if os.path.islink(dst):
            raise OSError(f"{dst} is a symbolic link; devmem never writes through a link")
        fd_out = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
                         | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), PRIVATE_FILE)
        try:
            import stat as _st
            if not _st.S_ISREG(os.fstat(fd_out).st_mode):
                raise OSError(f"{dst} is not a plain file; not written")
            private_fd(fd_out)
            while True:
                chunk = os.read(fd_in, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd_out, view):]
        finally:
            os.close(fd_out)
    finally:
        os.close(fd_in)
