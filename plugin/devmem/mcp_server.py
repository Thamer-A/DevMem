"""DevMemory as an MCP server (stdio, JSON-RPC 2.0, stdlib only). Works with any MCP client:
Claude Code, Codex CLI, Cursor, Claude Desktop, VS Code, ...

  devmem mcp --project <dir> [--read-only] [--require-store]

--read-only offers only the three read tools (for a client whose hooks record sessions, such as Claude Code with
`devmem init` or the devmem plugin). "Read-only" means it never records memory content (no session, no pending
snapshot); get_context_pack still refreshes the derived project map and search index (`PROJECT.md`, `index/`), as
every pack does, and the opt-in call log is written when config.json switches it on. --require-store never creates
the store; calls answer NOT_SET_UP while it is missing. A store that is (or holds) a symbolic link where devmem
writes is refused (devmem.store.StoreLinkError): the tool call is an error result and nothing is written. So is
every call while the store is not this user's (devmem.consent.store_refusal: a store a repository shipped, never
adopted): its sessions are not served, and nothing is recorded or written there. `devmem sessions` and `devmem show`
print what list_sessions and read_session return.

Tools:
  get_context_pack(task, budget?)   the task-specific pack (requirements, origin sessions, files, STALE flags)
  list_sessions()                   captured sessions with freshness
  read_session(id)                  one session turn by turn: prompts, tool calls, edits, outcome
  begin_session()                   snapshot the project before working; returns a pending id
  end_session(prompt, pending_id?, summary?)
                                    record the diff since begin_session as a session; pass the pending id
                                    so overlapping sessions do not collide. The summary is stored labelled
                                    as the assistant's own report

Protocol (checked against modelcontextprotocol.io on 2026-11-13): the legacy `initialize` handshake for
2025-11-25, 2025-06-18, 2025-03-26 and 2024-11-05, and the 2026-07-28 revision's `server/discover` with
per-request `_meta` version (those results carry `resultType: "complete"`). Tool failures come back as
results with `isError: true`; an unknown tool is JSON-RPC error -32602. Only the legacy path has been
exercised against a real client; the 2026-07-28 path is covered by unit tests written from the spec text.
"""
from __future__ import annotations

import inspect
import json
import math
import os
import sys
import time
from pathlib import Path

from . import __version__, generic
from .consent import store_refusal
from . import term
from .pack import build_pack, freshness, freshness_statuses
from .refresh import refresh
from .sessions import config_bool, load_sessions
from .redact import Redactor
from .store import ensure_store, store_path

LEGACY_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MODERN_PROTOCOL = "2026-07-28"
PROTOCOLS = LEGACY_PROTOCOLS
META_VERSION = "io.modelcontextprotocol/protocolVersion"
EDIT_TOOLS = ("Edit", "Write", "MultiEdit")
TEXT_CAP = 1500        # characters of any one captured text shown by read_session
MCP_SCAN_BUDGET = 20.0  # seconds a get_context_pack refresh may take (devmem.refresh); the rest waits for the next call
MCP_PACK_BUDGET = 5.0   # then seconds for indexing session records and embeddings in the pack
EDITS_CAP = 20
CALL_LOG_ENTRY_CAP = 8000   # characters of one call-log line; longer arguments are cut, and the line says so
# Every tool argument has a bound, checked before anything else runs (the call log included); a call past one gets a
# tool error that names it. A request line is at most MAX_LINE_BYTES, and an integer in it at most MAX_INT_DIGITS
# characters (a minus sign counts); past either, the request is a JSON-RPC parse error (id null), not a tool error.
TEXT_ARG_CHARS = 100_000    # task, prompt, summary (as devmem.hooks.PROMPT_CAP)
ID_ARG_CHARS = 64           # id, pending_id, client (sessions.safe_id and PROVIDER_RE allow no more)
BUDGET_MIN, BUDGET_MAX = 100, 20_000   # get_context_pack's token budget
STRING_ARGS = {"task": TEXT_ARG_CHARS, "prompt": TEXT_ARG_CHARS, "summary": TEXT_ARG_CHARS, "id": ID_ARG_CHARS,
               "pending_id": ID_ARG_CHARS, "client": ID_ARG_CHARS}
MAX_INT_DIGITS = 100

TOOLS = [
    {"name": "get_context_pack",
     "description": "Get DevMemory's compact, source-linked context for a coding task in this project: the user's "
                    "earlier requirements (verbatim), past sessions relevant to the task, the relevant files and lines, "
                    "and STALE warnings where code changed since a memory was recorded. Call this first, before "
                    "exploring the project.",
     "inputSchema": {"type": "object", "properties": {
         "task": {"type": "string", "maxLength": TEXT_ARG_CHARS, "description": "the user's request, verbatim"},
         "budget": {"type": "integer", "minimum": BUDGET_MIN, "maximum": BUDGET_MAX,
                    "description": "token budget (default 1500)"}}, "required": ["task"]}},
    {"name": "list_sessions", "description": "List captured past sessions with their freshness status.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "read_session",
     "description": "Read one captured session by id prefix, turn by turn: each prompt, the assistant-visible text, "
                    "the tool calls and edits under it, and the outcome. Texts are labelled with who wrote them.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string", "maxLength": ID_ARG_CHARS}},
                     "required": ["id"]}},
    {"name": "begin_session",
     "description": "Start recording: snapshot the project before you make changes. Returns a pending id; pass it "
                    "to end_session.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "end_session",
     "description": "Finish recording: DevMemory diffs the project against begin_session and stores the session, so "
                    "future sessions can retrieve it. Pass the user's request verbatim as prompt.",
     "inputSchema": {"type": "object", "properties": {
         "prompt": {"type": "string", "maxLength": TEXT_ARG_CHARS, "description": "the user's request, verbatim"},
         "pending_id": {"type": "string", "maxLength": ID_ARG_CHARS,
                        "description": "the id begin_session returned (default: the latest begin)"},
         "summary": {"type": "string", "maxLength": TEXT_ARG_CHARS,
                     "description": "short summary of what you did and decided"},
         "client": {"type": "string", "maxLength": ID_ARG_CHARS,
                    "description": "name of the AI tool, e.g. codex, cursor"}},
         "required": ["prompt"]}},
]


def check_args(args: dict) -> dict:
    """`args` with every argument inside its bound (STRING_ARGS, BUDGET_MIN..BUDGET_MAX), or ValueError naming the
    first one that is not. An absent or null argument stays absent."""
    for k, cap in STRING_ARGS.items():
        v = args.get(k)
        if v is None:
            continue
        if not isinstance(v, str):
            raise ValueError(f"{k} must be a string")
        if len(v) > cap:
            raise ValueError(f"{k} is {len(v):,} characters; at most {cap:,} are accepted")
    b = args.get("budget")
    if b is not None:
        if isinstance(b, float) and b.is_integer():
            b = int(b)
        if isinstance(b, bool) or not isinstance(b, int) or not BUDGET_MIN <= b <= BUDGET_MAX:
            raise ValueError(f"budget must be a whole number from {BUDGET_MIN} to {BUDGET_MAX}")
        args = {**args, "budget": b}
    return args


def _cap(text, n: int = TEXT_CAP):
    text = text if isinstance(text, str) else ("" if text is None else str(text))
    return text if len(text) <= n else text[:n] + f"... [{len(text) - n} more characters]"


def _tool_view(tc: dict) -> dict:
    """One tool call for read_session: what was done, with edit text capped (it stays labelled as observed)."""
    keep = {k: tc.get(k) for k in ("tool", "file", "command", "pattern", "ok", "truncated") if tc.get(k) not in (None, "")}
    for k in ("old", "new"):
        if tc.get(k):
            keep[k] = _cap(tc[k])
    return keep


def _said(x) -> str:
    """One assistant text for read_session; a text cut when it was recorded says so."""
    if not isinstance(x, dict):
        return _cap(x)
    return _cap(x.get("text")) + (" [cut when recorded: the rest of this text was not kept]"
                                  if x.get("truncated") is True else "")


GAPS_KEY = "transcript_gaps (devmem's note: assistant-visible text in the transcript that is not in this record)"


def _gap_notes(gaps) -> list[str]:
    return [str(g.get("note") or "") for g in gaps or [] if isinstance(g, dict)][:20]


def _session_view(s: dict, project: Path) -> dict:
    turns = []
    for t in s.get("turns") or []:
        calls = t.get("tool_calls") or []
        view = {
            "index": t.get("index"), "started_at": t.get("started_at"),
            ("prompt (reported by the AI client, not necessarily the user's words)"
             if t.get("prompt_source") == "reported-by-client" else "prompt (written by the user)"): _cap(t.get("prompt")),
            "assistant_text (assistant-visible)": [_said(x) for x in t.get("assistant_text") or []][:5],
            "result_text": _cap(t.get("result_text")), "result_source": t.get("result_source"),
            "tool_calls": [_tool_view(c) for c in calls][:EDITS_CAP * 2],
            "tool_calls_total": len(calls),
            "files_edited": t.get("files_edited") or [], "errors": [_cap(e, 300) for e in (t.get("errors") or [])][:5]}
        if t.get("transcript_gaps"):
            view[GAPS_KEY] = _gap_notes(t["transcript_gaps"])
        turns.append(view)
    edits = [_tool_view(c) for c in s.get("tool_calls") or [] if c.get("tool") in EDIT_TOOLS and c.get("file")]
    return {"session_id": s.get("session_id"), "started_at": s.get("started_at"), "ended_at": s.get("ended_at"),
            "in_progress": bool(s.get("in_progress")), "provider": s.get("provider"),
            "capture_method": s.get("capture_method"), "files_edited": s.get("files_edited") or [],
            "freshness": freshness(s, project), "turns": turns,
            "edits": edits[:EDITS_CAP], "edits_total": len(edits)}


def call_log_enabled(store: Path) -> bool:
    """The call log (`<store>/mcp_calls.jsonl`) is opt-in: `{"mcp_call_log": true}` in `<store>/config.json`."""
    try:
        from .store import read_store_text
        cfg = json.loads(read_store_text(Path(store) / "config.json", 1024 * 1024))
        return config_bool(cfg.get("mcp_call_log"), False)
    except (OSError, ValueError, AttributeError):
        return False


def list_sessions_text(store: Path, project: Path) -> str:
    """The list_sessions answer, one line per session: id, start, provider, turns, freshness, first words of the
    request (`devmem sessions` prints the same)."""
    rows = []
    sessions = load_sessions(store, project=project)
    for s, status in zip(sessions, freshness_statuses(sessions, project)):
        rows.append(f"{str(s.get('session_id'))[:8]}  {str(s.get('started_at') or '')[:16]}  {s.get('provider', '')}  "
                    f"{len(s.get('turns') or [])} turn(s)  {status}"
                    f"{'  (in progress)' if s.get('in_progress') else ''}  {(s.get('prompt') or '')[:100]!r}")
    return "\n".join(rows) or "no sessions captured yet"


def read_session_text(store: Path, project: Path, prefix: str) -> str:
    """The read_session answer for a session-id prefix (`devmem show` prints the same): JSON, turn by turn, every text
    labelled with who wrote it and capped. Raises ValueError for no id, no match or more than one."""
    prefix = str(prefix or "")
    if not prefix:
        raise ValueError("id is required (see list_sessions)")
    found = [s for s in load_sessions(store, project=project) if str(s.get("session_id", "")).startswith(prefix)]
    if len(found) > 1:
        raise ValueError(f"{prefix!r} matches {len(found)} sessions; give more of the id")
    if not found:
        raise ValueError(f"no session with id {prefix!r}")
    from .store import display_text   # recorded text: nothing invisible or terminal-controlling is shown raw
    return display_text(json.dumps(_session_view(found[0], project), indent=1, ensure_ascii=False), json_text=True)


READ_TOOLS = ("get_context_pack", "list_sessions", "read_session")
NOT_SET_UP = ("devmem memory is not set up for this project (no .devmemory folder), so there is nothing to look up. "
              "It is switched on with `devmem init` (or /devmem:setup with the devmem Claude Code plugin).")
# Read-only mode (`devmem mcp --read-only`): for a client whose hooks record the session and already add a pack to
# each prompt (Claude Code with `devmem init` or the devmem plugin, when this server is added with `claude mcp add`),
# so the server offers lookups only and does not ask to be called first.
READ_ONLY_INSTRUCTIONS = ("devmem's hooks already add relevant earlier work to prompts. When a task refers to earlier "
                          "work that is not in the context, list_sessions and read_session look it up.")
READ_ONLY_PACK_DESCRIPTION = ("DevMemory's context pack for a task: earlier requirements (verbatim), related past "
                              "sessions, relevant files, STALE warnings. Prompts already get one from the hooks; use "
                              "this for a different wording of the task or a larger budget.")


class Server:
    def __init__(self, project: Path, store: Path | None = None, read_only: bool = False, require_store: bool = False):
        self.project = Path(project).resolve()
        self.store = Path(store or store_path(self.project))
        self.read_only, self.require_store = read_only, require_store

    def tools(self) -> list[dict]:
        if not self.read_only:
            return TOOLS
        return [dict(t, description=READ_ONLY_PACK_DESCRIPTION) if t["name"] == "get_context_pack" else t
                for t in TOOLS if t["name"] in READ_TOOLS]

    def call(self, name: object, args: dict) -> str:  # name comes straight from the JSON-RPC request
        if name not in {t["name"] for t in self.tools()}:
            raise KeyError(name)
        if self.require_store and not self.store.is_dir():
            return NOT_SET_UP
        why = store_refusal(self.project, self.store)   # a store this user did not create or adopt: nothing served,
        if why:                                         # recorded or written (not even the map or the call log)
            raise ValueError(why)
        args = check_args(args)
        ensure_store(self.store)
        if call_log_enabled(self.store):  # opt-in; arguments are redacted before they reach the disk
            try:
                logged = Redactor(self.store).obj(args)
                line = json.dumps({"ts": generic._now(), "tool": name, "args": logged}, ensure_ascii=False)
                if len(line) > CALL_LOG_ENTRY_CAP:     # one call never grows the log by more than this
                    text = json.dumps(logged, ensure_ascii=False)
                    line = json.dumps({"ts": generic._now(), "tool": name, "args_cut": text[:CALL_LOG_ENTRY_CAP // 2],
                                       "args_chars": len(text)}, ensure_ascii=False)
                from .store import open_append
                with open_append(self.store / "mcp_calls.jsonl") as f:
                    f.write(line + "\n")
            except OSError:
                pass
        if name == "get_context_pack":
            if not isinstance(args.get("task"), str) or not args["task"].strip():
                raise ValueError("task is required: pass the user's request")
            refresh(self.project, self.store, MCP_SCAN_BUDGET)   # a child killed at the budget: never stalls the client
            text, _info = build_pack(self.project, self.store, args["task"], budget=args.get("budget") or 1500,
                                     deadline=time.monotonic() + MCP_PACK_BUDGET)
            return text
        if name == "list_sessions":
            return list_sessions_text(self.store, self.project)
        if name == "read_session":
            return read_session_text(self.store, self.project, args.get("id") or "")
        if name in ("begin_session", "end_session") and config_bool(os.environ.get(generic.NO_CAPTURE_ENV), False):
            # started (directly or through the client) by `devmem codex` or `devmem wrap`, which record the run
            # themselves: a second record from begin/end would duplicate it
            return ("this session is already being recorded by devmem (devmem codex / devmem wrap); nothing was "
                    "created. begin_session and end_session are not needed here")
        if name == "begin_session":
            info: dict = {}
            p = generic.begin(self.project, self.store, info=info)
            note = generic.begin_note(info)
            return (f"recording started. pending id: {p.stem} (pass it as pending_id to end_session)"
                    + (f"\n{note}" if note else ""))
        if name == "end_session":
            if not isinstance(args.get("prompt"), str) or not args["prompt"].strip():
                raise ValueError("prompt is required: pass the user's request verbatim")
            client = args.get("client") or "mcp-client"
            from .sessions import PROVIDER_RE
            if not isinstance(client, str) or not PROVIDER_RE.fullmatch(client):
                raise ValueError("client must be a short tool name (letters, digits, spaces and . _ + -, at most 64)")
            extra = {}
            if "prompt_source" in inspect.signature(generic.end).parameters:
                extra["prompt_source"] = "reported-by-client"  # the AI client supplied this text, not the user
            summary = args.get("summary")
            if summary is not None and not isinstance(summary, str):
                raise ValueError("summary must be a string")
            done: dict = {}
            meta = generic.end(self.project, args["prompt"], provider=client, store=self.store,
                               summary=summary,
                               summary_source="assistant self-report" if summary else None,
                               pending_id=args.get("pending_id") or None, info=done, **extra)
            from .store import read_store_text
            s = json.loads(read_store_text(meta))
            note = generic.end_note(done)
            return (f"recorded session {s['session_id'][:8]}: {len(s['files_edited'])} files changed"
                    + (f"\n{note}" if note else ""))
        raise KeyError(name)

    def handle(self, msg) -> dict | None:
        if not isinstance(msg, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        mid, method = msg.get("id"), msg.get("method")
        raw_params = msg.get("params")
        params: dict = raw_params if isinstance(raw_params, dict) else {}
        if mid is None:  # notification (or a response): nothing to answer
            return None
        if not _valid_id(mid):   # an object, list, bool, or a number like 1e999 (Infinity) is no JSON-RPC id
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request id"}}

        def err(code, message, data=None):
            e = {"code": code, "message": message}
            if data is not None:
                e["data"] = data
            return {"jsonrpc": "2.0", "id": mid, "error": e}

        # 2026-07-28 and later: no handshake, the version rides on every request in _meta
        raw_meta = params.get("_meta")
        meta: dict = raw_meta if isinstance(raw_meta, dict) else {}
        modern = META_VERSION in meta
        if modern and meta[META_VERSION] != MODERN_PROTOCOL:
            return err(-32022, "Unsupported protocol version",
                       {"supported": [MODERN_PROTOCOL, *LEGACY_PROTOCOLS], "requested": meta[META_VERSION]})
        done = {"resultType": "complete"} if modern else {}
        instructions = ("Before working on a task in this project, call get_context_pack with the user's "
                        "request. To remember your work for later sessions, call begin_session before "
                        "editing and end_session (with the pending id) when done.")
        if self.read_only:
            instructions = READ_ONLY_INSTRUCTIONS
        if method == "initialize":
            ver = params.get("protocolVersion")
            res = {"protocolVersion": ver if ver in LEGACY_PROTOCOLS else LEGACY_PROTOCOLS[0],
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "devmemory", "version": __version__},
                   "instructions": instructions}
        elif method == "server/discover":
            res = {"resultType": "complete", "supportedVersions": [MODERN_PROTOCOL, *LEGACY_PROTOCOLS],
                   "capabilities": {"tools": {}},
                   "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "devmemory", "version": __version__}},
                   "instructions": instructions}
        elif method == "ping":
            res = dict(done)
        elif method == "tools/list":
            res = {**done, "tools": self.tools()}
        elif method == "tools/call":
            try:
                if not isinstance(params.get("arguments") or {}, dict):
                    return err(-32602, "arguments must be an object")
                text = self.call(params.get("name"), params.get("arguments") or {})
                res = {**done, "content": [{"type": "text", "text": text}], "isError": False}
            except KeyError as e:
                return err(-32602, f"Unknown tool: {e.args[0] if e.args else ''}")
            except Exception as e:  # tool errors go back to the model, not the transport
                res = {**done, "content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
        else:
            return err(-32601, f"method not found: {method}")
        return {"jsonrpc": "2.0", "id": mid, "result": res}


MAX_LINE_BYTES = 4 * 1024 * 1024   # one JSON-RPC message; a longer line is answered with an error and skipped


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _valid_id(mid) -> bool:
    """A JSON-RPC id: a string or a finite number (never a bool, an object, a list, Infinity or NaN)."""
    if isinstance(mid, bool) or not isinstance(mid, (str, int, float)):
        return False
    return not isinstance(mid, float) or math.isfinite(mid)


def _reply_id(msg):
    """The request id to echo in an error reply: only a valid JSON-RPC id, else null."""
    mid = msg.get("id") if isinstance(msg, dict) else None
    return mid if _valid_id(mid) else None


def respond(srv: Server, raw: bytes) -> dict | None:
    """One input line -> the reply to write (None for a notification). Never raises: a line that is not UTF-8 is
    decoded with replacement characters, anything json cannot parse (including nesting too deep for it) is a parse
    error, and an unexpected failure while handling a request is an internal error that carries no exception text,
    traceback or path."""
    try:
        msg = json.loads(raw.decode("utf-8", errors="replace"), parse_int=_parse_int)
    except (ValueError, RecursionError):
        return _error(None, -32700, "parse error")
    try:
        return srv.handle(msg)
    except Exception as e:  # noqa: BLE001 - the server must keep serving
        try:
            term.say(f"devmem mcp: internal error ({type(e).__name__})", file=sys.stderr)
        except (OSError, ValueError):
            pass
        if isinstance(msg, dict) and "id" not in msg:
            return None   # a notification gets no reply, not even an error
        return _error(_reply_id(msg), -32603, "internal error")


def _parse_int(text: str) -> int:
    """An integer of a request: at most MAX_INT_DIGITS digits. A Python without the int_max_str_digits limit (before
    3.11, 3.10.7 and 3.9.14) converts any length in quadratic time: a 4 MB number would hold the server for minutes."""
    if len(text) > MAX_INT_DIGITS:
        raise ValueError("integer too long")
    return int(text)


def _encode(out: dict) -> bytes:
    try:   # allow_nan=False: Infinity and NaN are not JSON; such a reply becomes an internal error
        return (json.dumps(out, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8", errors="replace")
    except (ValueError, TypeError, RecursionError):
        return (json.dumps(_error(_reply_id(out), -32603, "internal error"), allow_nan=False) + "\n").encode("utf-8")


def _lines(stream, limit: int = MAX_LINE_BYTES):
    """Yield (line, too_long) from a binary stream; a line longer than `limit` bytes is skipped to its end and
    reported once as too long, without ever holding more than `limit` bytes of it."""
    while True:
        chunk = stream.readline(limit + 1)
        if not chunk:
            return
        if len(chunk) > limit and not chunk.endswith(b"\n"):
            while True:   # drain the rest of the oversized line
                rest = stream.readline(limit + 1)
                if not rest or rest.endswith(b"\n"):
                    break
            yield b"", True
            continue
        yield chunk, False


def serve(project: Path, store: Path | None = None, read_only: bool = False, require_store: bool = False) -> int:
    srv = Server(project, store, read_only=read_only, require_store=require_store)
    stdin = open(sys.stdin.fileno(), "rb", closefd=False)
    stdout = open(sys.stdout.fileno(), "wb", closefd=False)
    for raw, too_long in _lines(stdin):
        if too_long:
            out = _error(None, -32700, f"parse error: request longer than {MAX_LINE_BYTES} bytes")
        else:
            raw = raw.strip()
            if not raw:
                continue
            out = respond(srv, raw)
        if out is not None:
            stdout.write(_encode(out))
            stdout.flush()
    return 0
