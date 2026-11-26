"""Session capture: run (or import) a headless Claude Code session and store its record.

Record layout under <store>/sessions/ (schema 2, see devmem/sessions.py and docs/DESIGN-v2.md):
  <stamp>_<sid8>.jsonl       the stream-json the CLI emitted, redacted before it is written: secrets
                             replaced, secret-file contents omitted, thinking blocks dropped
  <stamp>_<sid8>.meta.json   deterministic digest: one turn with the prompt, visible assistant text, tool
                             calls (Edit/Write/MultiEdit text capped at 4,000 chars with `truncated`),
                             touched and deleted files, git HEAD at start/end, usage metrics
No LLM is called to build the digest. Hidden reasoning is never stored; only the assistant-visible text
that the CLI emits. Interactive sessions are captured by devmem.hooks instead.
"""
from __future__ import annotations

import codecs
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import sessions as S
from .claude_cli import (FILE_EDIT_TOOLS, FILE_READ_TOOLS, MAX_LINE_PARSE_CHARS, parse_stream,  # noqa: F401
                         run_claude, _rel)
from .redact import (CAP_MARGIN, MAX_KEY, OMITTED_SECRET_FILE, UNCHECKED_OUTPUT, Redactor, _text_block, call_decision,
                     call_names_secret_file, scrub_secret_parts, secret_call_input, unchecked_input)
from .store import ensure_store, store_path

# In the environment of the session devmem launches: if the project has `devmem init` hooks, they deliver
# context but do not record this session a second time (it is recorded here from its stream-json output).
NO_CAPTURE_ENV = "DEVMEM_NO_CAPTURE"

_THINKING = {"thinking", "redacted_thinking"}

# Bounded cost whatever the stream holds (a tool result can be a whole file, a session can run for hours): one text
# keeps at most STREAM_TEXT_CAP characters (it is cut before it is filtered and redacted, with margin for a secret on
# the cut, and says so), and at most STREAM_TEXT_BUDGET characters of a stream are redacted at all; the lines after
# that are stored as a stub that says what was left out (one output line per input line: line numbers stay valid).
STREAM_TEXT_CAP = 64 * 1024
STREAM_TEXT_BUDGET = 8 * 1024 * 1024
# What is read of the stream file at all: a file over STREAM_READ_HEAD + STREAM_READ_TAIL bytes is read as its first and
# last parts (cut at whole lines), and more than MAX_STREAM_LINES lines keep their first and last halves; one stub line
# stands for what was not read, and the record's turn says how much (`stream_left_out`). Lists of the digest keep at most
# MAX_STREAM_ITEMS entries each (`*_left_out` counts the rest), and the prompt at most PROMPT_CAP characters.
STREAM_READ_HEAD = 64 * 1024 * 1024
STREAM_READ_TAIL = 64 * 1024 * 1024
MAX_STREAM_LINES = 200_000
MAX_STREAM_ITEMS = 10_000
PROMPT_CAP = 100_000                  # as devmem.hooks.PROMPT_CAP and devmem.generic.PROMPT_CAP
MAX_FIELD_CHARS = 256                 # a session id or model name taken from the stream
# A stream line is input: one that is not a JSON object, nests deeper than MAX_EVENT_DEPTH, is longer than
# MAX_LINE_PARSE_CHARS (never parsed: json holds about 35 times a line of small values), holds more values or keys
# than one line may (MAX_LINE_VALUES, MAX_LINE_KEYS, counted before it is parsed), comes after the stream's budgets
# (STREAM_VALUE_BUDGET, STREAM_KEY_BUDGET, STREAM_TEXT_BUDGET), or that devmem cannot take apart (a field of the
# wrong type) is stored as a stub and counted by its reason (`stream_lines_left_out`), never fatal.
# Tool calls looked at (_Calls, the one decision the raw record and the digest share; redact.call_names_secret_file
# on the call's whole input): well-formed calls of assistant events, the first MAX_STREAM_ITEMS of the stream and
# MAX_LINE_TOOL_USES of one line. Any other call is left out: its input and its results' text are stored nowhere,
# only counted (`tool_calls_left_out`); the digest's list keeps MAX_STREAM_ITEMS entries of the calls looked at
# (`tool_call_entries_left_out`: a MultiEdit is an entry per edit).
MAX_EVENT_DEPTH = 64
MAX_LINE_TOOL_USES = 1_000
LEFT_OUT_TOOL = ("[devmem: not stored: a tool call devmem did not look at (MAX_STREAM_ITEMS / MAX_LINE_TOOL_USES, "
                 "malformed, or not in an assistant event), or a result no call it looked at accounts for]")
_CUT_NOTE = " [devmem: cut here; the rest of this text ({n} characters in all) is not stored]"


def _precut(x, budget: list[int]) -> Any:
    """A copy of a parsed stream event with every string cut to STREAM_TEXT_CAP plus margin (raw, before filtering),
    the characters counted against `budget`. A list or object with nothing cut in it is returned as it is (no copy:
    a line of many small values is not held twice)."""
    if isinstance(x, str):
        budget[0] -= min(len(x), STREAM_TEXT_CAP)
        return x if len(x) <= STREAM_TEXT_CAP + 2 * CAP_MARGIN else _Cut(x[:STREAM_TEXT_CAP + 2 * CAP_MARGIN], len(x))
    if isinstance(x, list):
        out = [_precut(v, budget) for v in x]
        return x if all(a is b for a, b in zip(out, x)) else out
    if isinstance(x, dict):
        for k in x:
            budget[0] -= min(len(k), MAX_KEY) if isinstance(k, str) else 0    # a key is redacted too (Redactor.key)
        new = {k: _precut(v, budget) for k, v in x.items()}
        return x if all(new[k] is v for k, v in x.items()) else new
    return x


class _Cut(str):
    """A string cut by _precut; `full` is the length it had."""
    full: int

    def __new__(cls, s: str, full: int):
        o = super().__new__(cls, s)
        o.full = full
        return o


def _bounded_text(s: str, red: Redactor) -> str:
    """Redact one stream text with bounded cost: a text over STREAM_TEXT_CAP keeps its first STREAM_TEXT_CAP characters
    (Redactor.capped) and a note of how much was left out."""
    if len(s) <= STREAM_TEXT_CAP:
        return red.text(s)
    full = getattr(s, "full", None)
    return red.capped(str(s), STREAM_TEXT_CAP)[0] + _CUT_NOTE.format(n=full if full else f"over {len(s)}")


def _joinable(v) -> bool:
    s = v["text"] if _text_block(v) else v
    return isinstance(s, str) and len(s) <= STREAM_TEXT_CAP and not isinstance(s, _Cut)


def _bounded_items(vals: list, red: Redactor) -> list:
    """The values of a list or a dictionary, in order: a run of consecutive strings (or text blocks) is redacted as one
    text, one item per line (Redactor.joined), so a key block or a YAML block scalar a tool result gives one line per
    string (`tool_use_result.structuredPatch[].lines`) is seen whole; a string over STREAM_TEXT_CAP goes alone."""
    out, i, n = [], 0, len(vals)
    while i < n:
        j = i
        while j < n and _joinable(vals[j]) and not (j > i and vals[j] == "" and vals[j - 1] == ""):
            j += 1
        if j - i < 2:
            out.append(_bounded(vals[i], red))
            i += 1
            continue
        run = vals[i:j]
        texts = red.joined([v if isinstance(v, str) else v["text"] for v in run])
        for v, t in zip(run, texts):
            if isinstance(v, str):
                out.append(t)
                continue
            rest = _bounded({k: w for k, w in v.items() if k != "text"}, red)    # a text block's other fields
            out.append({**(rest if isinstance(rest, dict) else {}), "text": t})
        i = j
    return out


def _bounded(x, red: Redactor):
    if isinstance(x, str):
        return _bounded_text(x, red)
    if isinstance(x, list):
        return _bounded_items(x, red)
    if isinstance(x, dict):
        keys = [red.key(k) if isinstance(k, str) else k for k in x]
        return dict(zip(keys, _bounded_items(list(x.values()), red)))
    return x


def _stub(reason: str) -> str:
    return json.dumps({"type": "devmem_omitted", "reason": reason})


def _gap_line(lines: int, size: int | None = None) -> str:
    what = " and ".join(x for x in (f"{lines:,} lines of the stream" if lines else "",
                                    f"about {size:,} bytes from its middle" if size else "") if x)
    return json.dumps({"type": "devmem_omitted", "reason": f"not read: {what} "
                       "(devmem.capture.STREAM_READ_HEAD/_TAIL, MAX_STREAM_LINES)"})


_READ_CHUNK = 64 * 1024     # one read: its lines are one batch (64 KB of 3-byte lines: 22,000 strings)


_BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
         (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
_NEWLINE = {"utf-8": b"\n", "utf-16-le": b"\n\x00", "utf-16-be": b"\x00\n", "utf-32-le": b"\n\x00\x00\x00",
            "utf-32-be": b"\x00\x00\x00\n"}
# A line end of the current encoding followed by what may begin a line of another one (a byte order mark, or a `{` as
# another encoding writes it): only there is _line_encoding asked (an ordinary file has no such place)
_SWITCH = {enc: re.compile(re.escape(nl) + b"(?=" + starts + b")") for enc, nl, starts in (
    ("utf-8", b"\n", rb"\xff\xfe|\xfe\xff|\{\x00|\x00\{"),
    ("utf-16-le", b"\n\x00", rb"\xef\xbb\xbf|\xfe\xff|\{[^\x00]|\x00\{"),
    ("utf-16-be", b"\x00\n", rb"\xef\xbb\xbf|\xff\xfe|\{[^\x00]|\{\x00"),
    ("utf-32-le", b"\n\x00\x00\x00", rb"\xef\xbb\xbf|\xfe\xff|\{[^\x00]"),
    ("utf-32-be", b"\x00\x00\x00\n", rb"\xef\xbb\xbf|\xff\xfe|\{[^\x00]"))}
MAX_ENCODING_SWITCHES = 1_000     # changes of encoding followed in one region (more: the rest is read as the last one)
_UNDECODABLE = [0]                # byte sequences replaced while decoding (the "devmem-count" error handler)


def _count_replace(exc):
    _UNDECODABLE[0] += 1
    return "�", exc.end


codecs.register_error("devmem-count", _count_replace)


def _line_encoding(b: bytes) -> tuple[str, int] | None:
    """The encoding a stream line starting with these bytes is written in, and the length of its byte order mark: a
    BOM, or a `{` as UTF-8, UTF-16 or UTF-32 writes it (a JSON object line starts with one). None: no telling."""
    for bom, codec in _BOMS:
        if b.startswith(bom):
            return codec, len(bom)
    if len(b) >= 2 and b[0] == 0x7B:
        if b[1] != 0:
            return "utf-8", 0
        return ("utf-32-le" if b[2:4] == b"\x00\x00" else "utf-16-le"), 0
    if b[:2] == b"\x00{":
        return "utf-16-be", 0
    if b[:4] == b"\x00\x00\x00{":
        return "utf-32-be", 0
    return None


def _encoding(head: bytes) -> tuple[str, int]:
    """(codec, length of its byte order mark) of a stream file from its first bytes: a BOM of UTF-8, UTF-32 or UTF-16
    (what Windows PowerShell 5.1's `>` writes), the first line's `{` as one of them writes it, or UTF-16 without
    either (NUL in every other byte of its first 4 KB); else UTF-8."""
    first = _line_encoding(head)
    if first is not None:
        return first
    sample = head[:4096]
    pairs = len(sample) // 2
    if pairs >= 2:
        even = sum(1 for b in sample[0:2 * pairs:2] if b == 0)
        odd = sum(1 for b in sample[1:2 * pairs:2] if b == 0)
        if odd >= 0.4 * pairs and even <= 0.05 * pairs:
            return "utf-16-le", 0
        if even >= 0.4 * pairs and odd <= 0.05 * pairs:
            return "utf-16-be", 0
    return "utf-8", 0


def _region_lines(f, nbytes: int, rest: list[str], chunk: int | None = None, encoding: str = "utf-8",
                  state: dict | None = None):
    """Batches (lists) of the complete lines in the next `nbytes` bytes of binary file `f`, split as
    read_text().split("\\n") splits them (`encoding`, UTF-8 by default, with replacement; universal newlines; "\\n"
    only, not U+2028). A line that begins in another encoding (a byte order mark, or its `{` as another encoding
    writes it: Windows PowerShell 5.1's `>>` appends UTF-16 to a UTF-8 file) starts a region read in that one, at
    most MAX_ENCODING_SWITCHES times. The text after the last newline goes to `rest`; `state` gets the encoding at the
    end ("encoding") and counts "switches" and "undecodable" (byte sequences replaced). Memory: one chunk, the lines
    of one chunk, and the line being read."""
    state = state if state is not None else {}
    state.setdefault("switches", 0)
    enc = encoding

    def decoder(e: str):
        return codecs.getincrementaldecoder(e)(errors="devmem-count")
    dec = decoder(enc)
    part: list[str] = []          # the unfinished line, in pieces (a long line is not copied again per chunk)
    held = ""                     # a CR at the end of a chunk: it may be the first half of CR LF
    pend = b""                    # the last bytes read, not decoded yet: a line start check may need them
    at = 0                        # offset (in the region) of pend's first byte
    base = 0                      # offset where the current encoding's region began (code unit alignment)
    left, chunk = nbytes, chunk or _READ_CHUNK
    _UNDECODABLE[0] = 0
    while True:
        data = f.read(min(chunk, left)) if left > 0 else b""
        left -= len(data)
        final = not data
        buf = pend + data
        limit = len(buf) if final else max(0, len(buf) - 8)
        texts: list[str] = []
        start = 0
        m = _SWITCH[enc].search(buf, 0) if state["switches"] < MAX_ENCODING_SWITCHES else None
        while m is not None and m.start() < limit:
            nl_end = m.end()
            unit = len(_NEWLINE[enc])
            found = _line_encoding(buf[nl_end:nl_end + 8]) if (at + m.start() - base) % unit == 0 else None
            if found is not None and found[0] != enc:
                texts.append(dec.decode(buf[start:nl_end], True))
                enc, start = found[0], nl_end + found[1]
                dec, base = decoder(enc), at + start
                state["switches"] += 1
                if state["switches"] >= MAX_ENCODING_SWITCHES:
                    break
                m = _SWITCH[enc].search(buf, start)
            else:
                m = _SWITCH[enc].search(buf, m.start() + 1)
        upto = max(start, limit)
        texts.append(dec.decode(buf[start:upto], final))
        pend, at = buf[upto:], at + upto
        text = held + "".join(texts)
        held = ""
        state["encoding"] = enc
        state["undecodable"] = state.get("undecodable", 0) + _UNDECODABLE[0]
        _UNDECODABLE[0] = 0
        if not final and text.endswith("\r"):
            text, held = text[:-1], "\r"
        pieces = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if len(pieces) > 1:
            pieces[0] = "".join(part) + pieces[0]
            part = [pieces.pop()]
            yield pieces
        elif pieces[0]:
            part.append(pieces[0])
        if final:
            rest.append("".join(part))
            return


class _Lines:
    """The first `head` lines kept in a list and the last `tail` in a deque; the others counted, not held."""

    def __init__(self, head: int, tail: int):
        from collections import deque
        self.first: list[str] = []
        self.last: deque[str] = deque(maxlen=max(0, tail))
        self.head, self.dropped = head, 0

    def add(self, batch: list[str]) -> None:
        room = self.head - len(self.first)
        if room > 0:
            self.first.extend(batch[:room])
            batch = batch[room:]
        if batch:
            self.dropped += max(0, len(self.last) + len(batch) - (self.last.maxlen or 0))
            self.last.extend(batch)


def _load_lines(stream_path: Path, cut: dict | None = None) -> list[str]:
    """The lines of a stream file, within STREAM_READ_HEAD + STREAM_READ_TAIL bytes and MAX_STREAM_LINES lines, as
    read_text().split("\\n") gives them for a file under both. A larger file is read as its first STREAM_READ_HEAD and
    last STREAM_READ_TAIL bytes (a line cut at either edge is dropped); of more lines than MAX_STREAM_LINES the first
    and last halves are kept. The cap applies while reading: no more than those lines (and the line being read) is
    held. One stub line stands for what was left out; `cut` gets {"lines", "bytes", "read_bytes"}."""
    path = Path(stream_path)
    size = path.stat().st_size
    half = MAX_STREAM_LINES // 2
    left_bytes = 0
    with open(path, "rb") as f:
        enc, bom = _encoding(f.read(4096))
        unit = 4 if enc.startswith("utf-32") else 2 if enc.startswith("utf-16") else 1
        f.seek(bom)
        state: dict = {}
        if size <= STREAM_READ_HEAD + STREAM_READ_TAIL:
            got, rest = _Lines(half, half), []
            for batch in _region_lines(f, size - bom, rest, None, enc, state):
                got.add(batch)
            if rest[0]:                              # a last line without a newline
                got.add(rest)
            first, last, dropped = got.first, list(got.last), got.dropped
        else:
            head, cut_head = _Lines(half, 0), []
            for batch in _region_lines(f, STREAM_READ_HEAD, cut_head, None, enc, state):
                head.add(batch)
            start = max(bom, size - STREAM_READ_TAIL)
            f.seek(start + (-(start - bom)) % unit)       # a whole code unit of UTF-16 / UTF-32
            tail, rest, cut_tail = _Lines(0, half), [], ""
            for batch in _region_lines(f, STREAM_READ_TAIL, rest, None, state.get("encoding", enc), state):
                if cut_tail == "" and batch:
                    cut_tail, batch = batch[0] + "\n", batch[1:]          # the line cut at the tail's start
                tail.add(batch)
            if cut_tail == "":                       # no newline in the whole tail: all of it is that cut line
                cut_tail = rest[0]
            elif rest[0]:
                tail.add(rest)
            left_bytes = size - STREAM_READ_HEAD - STREAM_READ_TAIL + len(cut_head[0]) + len(cut_tail)
            first, last, dropped = head.first, list(tail.last), head.dropped + tail.dropped
    if cut is not None and (state.get("switches") or state.get("undecodable")):
        cut["encoding"] = {"read_as": enc, **{k: state[k] for k in ("switches", "undecodable") if state.get(k)}}
    if dropped or left_bytes:
        lines = first + [_gap_line(dropped, left_bytes)] + last
        if cut is not None:
            cut.update({k: v for k, v in (("lines", dropped), ("bytes", left_bytes)) if v > 0})
            cut["read_bytes"] = min(size, STREAM_READ_HEAD + STREAM_READ_TAIL)
        return lines
    return first + last


def _msg(e: dict) -> dict:
    """An event's `message` when it is an object, else {} (a hand-made stream can hold anything there)."""
    m = e.get("message")
    return m if isinstance(m, dict) else {}


def _key(v) -> str | None:
    """A tool_use id or tool_use_id as devmem uses it (a dictionary key): a non-empty string, or an integer as its
    text (so that 7 and "7" are one id: the worse decision of the two holds), else None."""
    if isinstance(v, str):
        return v or None
    return str(v) if isinstance(v, int) and not isinstance(v, bool) else None


def _is_call(b) -> bool:
    """A content block that is a tool call: `tool_use`, or another kind of one (`server_tool_use`, `mcp_tool_use`)."""
    kind = b.get("type") if isinstance(b, dict) else None
    return isinstance(kind, str) and (kind == "tool_use" or kind.endswith("_tool_use"))


def _is_result(b) -> bool:
    """A content block that is a tool result: `tool_result`, another kind (`mcp_tool_result`, `web_fetch_tool_result`),
    or any block with a `tool_use_id`."""
    if not isinstance(b, dict):
        return False
    kind = b.get("type")
    return (isinstance(kind, str) and (kind == "tool_result" or kind.endswith("_tool_result"))) or "tool_use_id" in b


# ids written on a line devmem does not take apart (`"id": "toolu_x"`, `"tool_use_id": 7`): each counts as the id of a
# call it did not look at, so that a result with that id elsewhere in the stream is not kept. At most MAX_MARKED_IDS per
# stream are remembered; past that, no result of the stream is kept as text (_Calls.unsure).
_ID_FIELD = re.compile(r'"(?:id|tool_use_id)"\s*:\s*(?:"((?:[^"\\]|\\.){1,256})"|(-?\d{1,20})(?![\d.eE]))')
MAX_MARKED_IDS = 100_000


class _Calls:
    """THE decision about the tool calls of a stream and the text of their results, which the raw record and the
    digest both take from here. A call is known by its place (line, block), so one without an id, or with an id
    another call has too, is decided on its own. Looked at (decided by redact.call_names_secret_file on the call's
    whole input, never on a cut copy): a well-formed call (a `tool_use` block with a string `name` and an object
    `input`) of an assistant event, on a line devmem takes apart within the stream's text budget, among the first
    MAX_STREAM_ITEMS of the stream and MAX_LINE_TOOL_USES of its line. Every other call is left out (`left_out`:
    counted, its input not stored): another block kind (`server_tool_use`), malformed, outside an assistant event,
    past those caps, on a line past the text budget, or found on a line too long to parse (_recover). An id written
    on any other line devmem does not take apart is marked too (mark_ids). A tool result is matched by its
    tool_use_id: its text is withheld (OMITTED_SECRET_FILE) when any call with that id names a secret file; left out
    (LEFT_OUT_TOOL) when any was left out, or none was looked at for any reason (no id, an id never seen); else kept,
    with the lines of secret files removed (Redactor.filter_tool_output, as the hooks do)."""

    _RANK = {"ok": 0, "out": 1, "unchecked": 2, "secret": 3}
    MARKERS = {"secret": OMITTED_SECRET_FILE, "unchecked": UNCHECKED_OUTPUT, "out": LEFT_OUT_TOOL}

    def __init__(self, project: Path, red: Redactor):
        self.project, self.red = project, red
        self.looked: set[tuple[int, int]] = set()        # places
        self.secret: set[tuple[int, int]] = set()
        self.unchecked: dict[tuple[int, int], dict] = {}  # places of calls too large to check -> unchecked_input
        self.left_out: set[tuple[int, int]] = set()
        self.ids: dict[str, str] = {}                     # tool_use id -> "ok" | "out" | "unchecked" | "secret" (worst)
        self.marked = 0                                   # ids marked from lines not taken apart
        self.unsure = False                               # more than MAX_MARKED_IDS of them: no result text is kept
        self._count = 0

    def _mark(self, tid: str | None, status: str) -> None:
        if tid and self._RANK[status] >= self._RANK[self.ids.get(tid, "ok")]:
            self.ids[tid] = status

    def add(self, n: int, e: dict, look: bool = True) -> None:
        """The tool calls of event `n` (parsed, whole). `look=False`: none is looked at (a line past the text
        budget): each is counted as left out."""
        content = _msg(e).get("content")
        in_line = 0
        for k, b in enumerate(content if isinstance(content, list) else []):
            if not _is_call(b):
                continue
            tid, name, inp = _key(b.get("id")), b.get("name"), b.get("input")
            in_line += 1
            if (not look or b.get("type") != "tool_use" or e.get("type") != "assistant" or not isinstance(name, str)
                    or not isinstance(inp, dict) or self._count >= MAX_STREAM_ITEMS or in_line > MAX_LINE_TOOL_USES):
                self.left_out.add((n, k))
                self._mark(tid, "out")
                continue
            self._count += 1
            self.looked.add((n, k))
            d = call_decision(name, inp, self.project, self.red.secret_patterns)
            if d == "secret":
                self.secret.add((n, k))
            elif d == "unchecked":
                self.unchecked[(n, k)] = unchecked_input(inp)      # measured on the whole input
            self._mark(tid, d)

    def recovered(self, n: int, k: int, tid: str | None, secret: bool) -> None:
        """A call found on a line too long to parse (_recover): left out, its id marked."""
        self.left_out.add((n, k))
        self._mark(tid, "secret" if secret else "out")

    def mark_ids(self, line: str) -> None:
        """Every id written on a line devmem does not take apart counts as a call it did not look at."""
        for m in _ID_FIELD.finditer(line):
            if self.marked >= MAX_MARKED_IDS:
                self.unsure = True
                return
            self.marked += 1
            tid = m.group(1) if m.group(1) is not None else m.group(2)
            if m.group(1) is not None and "\\" in tid:
                try:
                    tid = json.loads('"' + tid + '"')
                except ValueError:
                    pass
            self._mark(_key(tid), "out")

    def status(self, n: int, k: int) -> str:
        """The decision about the call at this place: "secret", "unchecked", "out" or "ok"."""
        return "secret" if (n, k) in self.secret else "unchecked" if (n, k) in self.unchecked else \
            "ok" if (n, k) in self.looked else "out"

    def verdict(self, tid: str | None) -> str:
        """The decision a tool result with this tool_use_id takes: the worst of the calls with that id ("out" when
        none, or when the stream has more marked ids than MAX_MARKED_IDS)."""
        status = self.ids.get(tid) if tid else None
        if status in ("secret", "unchecked"):
            return status
        return "ok" if status == "ok" and not self.unsure else "out"

    def decide(self, tid: str | None) -> str | None:
        """The fixed text a tool result with this tool_use_id gets instead of its own; None: its text may be kept."""
        return self.MARKERS.get(self.verdict(tid))

    def output(self, tid: str | None, text: str) -> str:
        """A tool result's text as it may be stored: the fixed text of `decide`, or the text without the lines of
        secret files (redaction is the writer's next step)."""
        instead = self.decide(tid)
        if instead is not None:
            return instead
        return self.red.filter_tool_output("", {}, text, self.project)[0]


# ---- what of a line is taken apart at all: counted before json parses it (cost and memory bounded per line and per
# stream, whatever the line holds) ----
# A line's values: each string, number, literal, list and object, and each key, counted from its text with the strings
# emptied (1 + commas + colons + brackets that open). A line with more than MAX_LINE_VALUES, or more distinct keys than
# MAX_LINE_KEYS, is never parsed; once the lines taken apart have used STREAM_VALUE_BUDGET values or
# STREAM_KEY_BUDGET distinct keys, the stream's further lines within the text budget are not either (each such line is
# a stub, counted by reason). The largest ordinary lines measured hold a few thousand values and some 200 keys (real
# Claude Code transcripts: at most 3,447 values and 214 distinct keys on one line). A line past the text budget is
# parsed only for the stream's metrics, while STREAM_PARSE_BUDGET values last. The counting is one left-to-right pass:
# a string that is not closed runs to the end of the line in the same match (a line cut in the middle of a string, as
# a transcript still being written ends, costs no more than any other), so its cost is linear in the line's length.
MAX_LINE_VALUES = 100_000
MAX_LINE_KEYS = 2_000
STREAM_VALUE_BUDGET = 600_000
STREAM_KEY_BUDGET = 20_000
STREAM_PARSE_BUDGET = 4_000_000
# a JSON string, or one never closed (it then ends at the end of the line): no match attempt ever fails part way, so no
# later quote starts a new scan to the end of the line
_JSON_STRING = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*(?:"|\\?\Z)', re.S)
_JSON_STRING_KEY = re.compile(r'("[^"\\]*(?:\\.[^"\\]*)*(?:"|\\?\Z))(\s*:)?', re.S)


def _values(line: str) -> int:
    """The values and keys a JSON line holds, never fewer than json gives (exact, but that an empty list or object
    counts twice; json.loads refuses any line that is not JSON)."""
    s = _JSON_STRING.sub('""', line)
    return 1 + s.count(",") + s.count(":") + s.count("[") + s.count("{")


def _line_keys(line: str) -> set[str]:
    """The keys of a JSON line as written (two spellings of one key count twice: never fewer than json gives)."""
    return {m.group(1)[:MAX_KEY] for m in _JSON_STRING_KEY.finditer(line) if m.group(2) is not None}


def _measure(e) -> tuple[bool, int]:
    """(e nests no deeper than MAX_EVENT_DEPTH, the text it holds as the text budget counts it: each string up to
    STREAM_TEXT_CAP characters, each key up to MAX_KEY), walked level by level without recursion."""
    level, depth, chars = [e], 0, 0
    while level:
        if depth >= MAX_EVENT_DEPTH:
            return False, chars
        nxt = []
        for v in level:
            items = v.items() if isinstance(v, dict) else enumerate(v)
            for k, w in items:
                if isinstance(k, str):
                    chars += min(len(k), MAX_KEY)
                if isinstance(w, str):
                    chars += min(len(w), STREAM_TEXT_CAP)
                elif isinstance(w, (list, dict)):
                    nxt.append(w)
        level = nxt
        depth += 1
    return True, chars


# ---- a line too long to parse: its start is read for what it is (_recover) ----
LONG_SCAN_CHARS = 64 * 1024     # characters of the line read
LONG_SCAN_STRING = 4096         # a longer string is passed over (not decoded); one not ending in what is read ends it
_LONG_STRING = object()         # stands for such a string's value
LONG_SCAN_DEPTH = 8
_SCAN_TOKEN = re.compile(r'[ \t\r\n]*(?:([{}\[\],:])|(")|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null))')
_SCAN_STRING = re.compile(r'[^"\\]*(?:\\.[^"\\]*)*"')


def _scan_prefix(line: str):
    """The start of a JSON line too long to parse, as far as it can be read within LONG_SCAN_CHARS characters and
    LONG_SCAN_DEPTH levels: its objects and lists with the values read before that point (the value being read there,
    and everything after it, is missing). A string longer than LONG_SCAN_STRING that ends within the window is passed
    over (its value is _LONG_STRING, never decoded), so a long text before a tool call does not hide the call. No
    object is built for anything past that point; None when the line does not start as an object."""
    end = min(len(line), LONG_SCAN_CHARS)
    stack: list[list] = []            # [container, the key whose value comes next (objects)]
    root = None
    pos = 0

    def put(v) -> bool:
        nonlocal root
        if not stack:
            root = v
            return True
        top = stack[-1]
        if isinstance(top[0], dict):
            if top[1] is None:
                return False
            top[0][top[1]] = v
            top[1] = None
        else:
            top[0].append(v)
        return True

    while pos < end:
        m = _SCAN_TOKEN.match(line, pos, end)
        if not m:
            break
        pos = m.end()
        if m.group(1):
            c = m.group(1)
            if c in "{[":
                if len(stack) >= LONG_SCAN_DEPTH:
                    break
                new: Any = {} if c == "{" else []
                if not put(new):
                    break
                stack.append([new, None])
            elif c in "}]":
                if not stack:
                    break
                stack.pop()
                if not stack:
                    break
            continue                  # "," and ":" carry nothing the reading needs
        if m.group(2):
            s = _SCAN_STRING.match(line, pos, end)
            if not s:
                break                 # a string that does not end within what is read: stop here
            pos = s.end()
            if pos - m.end() > LONG_SCAN_STRING:
                text: Any = _LONG_STRING          # passed over, never decoded (a long text before a tool call)
            else:
                try:
                    text = json.loads(line[m.end() - 1:pos])
                except ValueError:
                    break
            top = stack[-1] if stack else None
            if text is _LONG_STRING and top is not None and isinstance(top[0], dict) and top[1] is None:
                break                 # a key that long: no event is shaped so
            if top is not None and isinstance(top[0], dict) and top[1] is None:
                rest = _SCAN_TOKEN.match(line, pos, end)
                if rest is None or rest.group(1) != ":":
                    break
                top[1] = text
                pos = rest.end()
                continue
            if not put(text):
                break
            continue
        try:
            put(json.loads(m.group(3)))
        except ValueError:
            break
    return root if isinstance(root, dict) else None


_PATH_FIELDS = ("file_path", "notebook_path", "path")


def _recover(n: int, line: str, calls: _Calls) -> dict:
    """What can be read of a line too long to parse (_scan_prefix): its event type, the assistant texts and the
    tool calls (name, id, and the path of a file tool) that lie wholly within its first LONG_SCAN_CHARS characters.
    Each call found is left out (_Calls.recovered: its result's text is not kept), and is a secret file's call when
    its path names one. {"type", "texts": [...], "calls": [(name, {path field: path}, secret)]}"""
    e = _scan_prefix(line)
    if e is None:
        return {}
    kind = e.get("type") if isinstance(e.get("type"), str) else None
    out: dict = {"type": kind if kind and len(kind) <= MAX_FIELD_CHARS else None, "texts": [], "calls": []}
    content = _msg(e).get("content")
    for k, b in enumerate(content if isinstance(content, list) else []):
        if _is_call(b):
            name = b.get("name") if isinstance(b.get("name"), str) else ""
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            paths = {f: inp[f] for f in _PATH_FIELDS if isinstance(inp.get(f), str) and inp[f]}
            secret = call_names_secret_file(name, paths, calls.project, calls.red.secret_patterns) if paths else False
            calls.recovered(n, k, _key(b.get("id")), secret)
            out["calls"].append((name, paths, secret))
        elif kind == "assistant" and isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
            out["texts"].append(b["text"])
    return out


def _filter_strings(x, red: Redactor):
    if isinstance(x, str):
        return red.omit_secret_file_lines(x)
    if isinstance(x, list):
        return [_filter_strings(v, red) for v in x]
    if isinstance(x, dict):
        return {k: _filter_strings(v, red) for k, v in x.items()}
    return x


DEEP = object()       # a line nested deeper than devmem takes apart (MAX_EVENT_DEPTH), or than json itself parses
LONG = object()       # a line longer than MAX_LINE_PARSE_CHARS, never parsed

# why a line is a stub, as the record counts it (`stream_lines_left_out`) and the note names it
_REASONS = {
    "not_json": "not a JSON object (a line of another encoding, or not stream-json)",
    "too_deep": f"nested deeper than devmem reads (MAX_EVENT_DEPTH {MAX_EVENT_DEPTH})",
    "too_long": f"longer than devmem parses (MAX_LINE_PARSE_CHARS {MAX_LINE_PARSE_CHARS:,}); its first "
                f"{LONG_SCAN_CHARS // 1024} KB were read for its tool calls",
    "too_many_values": f"more values than devmem takes apart on one line (MAX_LINE_VALUES {MAX_LINE_VALUES:,})",
    "too_many_keys": f"more distinct keys than devmem takes apart on one line (MAX_LINE_KEYS {MAX_LINE_KEYS:,})",
    "value_budget": f"past the values devmem takes apart in one stream (STREAM_VALUE_BUDGET {STREAM_VALUE_BUDGET:,}, "
                    f"STREAM_KEY_BUDGET {STREAM_KEY_BUDGET:,} distinct keys)",
    "text_budget": f"past the first {STREAM_TEXT_BUDGET:,} characters of text of the stream (STREAM_TEXT_BUDGET)",
    "unreadable": "devmem could not take it apart (a field of the wrong type)",
}


def _parse(line: str) -> tuple[Any, int]:
    """(the JSON object of a stream line, the text it holds: _measure); None for a line that is not one (not JSON,
    or JSON of another type), DEEP for one nested deeper than MAX_EVENT_DEPTH or than json itself parses, LONG for
    one longer than MAX_LINE_PARSE_CHARS (stored as stubs, never as their text: the record's write would parse that
    text again)."""
    if line.startswith("\ufeff"):
        line = line[1:]                       # a byte order mark at the start of a line
    if len(line) > MAX_LINE_PARSE_CHARS:
        return LONG, 0
    try:
        e = json.loads(line)
    except RecursionError:
        return DEEP, 0
    except ValueError:
        return None, 0
    if not isinstance(e, dict):
        return (DEEP if isinstance(e, list) and not _measure(e)[0] else None), 0
    ok, chars = _measure(e)
    return (e if ok else DEEP), chars


def _event(line: str):
    """The JSON object of a stream line, or None / DEEP / LONG (_parse)."""
    return _parse(line)[0]


class _Pass:
    """The first reading of a stream's lines (record_session): which lines are taken apart at all and why the others
    are not (`kind`, `left`), the tool calls (_Calls, decided on each call's whole input), what a line too long to
    parse gives (`recovered`), the metrics parse_stream would give, and the last timestamp. Each line is parsed and
    dropped in turn. Every line within the text budget is counted (_values, _line_keys) before it is parsed, against
    the per-line and per-stream bounds above."""

    def __init__(self, lines: list[str], project: Path, red: Redactor, metrics: bool = True):
        from collections import Counter
        from .claude_cli import new_metrics
        self.calls = _Calls(project, red)
        self.kind: list = []           # per line: "event", "" (blank), or a reason of _REASONS
        self.left: Counter = Counter()
        self.recovered: dict[int, dict] = {}
        self.metrics = new_metrics()
        self.timestamp: str | None = None
        self._want_metrics = metrics
        self.text, self.values, self.keys, self.parse = (STREAM_TEXT_BUDGET, STREAM_VALUE_BUDGET, STREAM_KEY_BUDGET,
                                                         STREAM_PARSE_BUDGET)
        self.seen: set[str] = set()
        for n, line in enumerate(lines):
            kind = self._line(n, line[1:] if line.startswith("\ufeff") else line)
            self.kind.append(kind)
            if kind not in ("event", ""):
                self.left[kind] += 1
        self.metrics["files_read_unique"] = 0

    def _took(self, e: dict) -> None:
        if self._want_metrics:
            from .claude_cli import _metrics_from
            try:
                _metrics_from(e, self.metrics, None)
            except (TypeError, ValueError, AttributeError, KeyError, RecursionError):
                pass
        ts = e.get("timestamp")
        if isinstance(ts, str):
            self.timestamp = ts

    def _line(self, n: int, line: str) -> str:
        if not line.strip():
            return ""
        if self.text <= 0:                      # past the text budget: read for the metrics and its calls' ids
            e = None
            if len(line) <= MAX_LINE_PARSE_CHARS:
                v = _values(line)
                if v <= MAX_LINE_VALUES and v <= self.parse:
                    self.parse -= v
                    e = _event(line)
            if isinstance(e, dict):
                self._took(e)
                self.calls.add(n, e, look=False)
            else:
                self.calls.mark_ids(line)
            return "text_budget"
        if len(line) > MAX_LINE_PARSE_CHARS:
            self.text -= STREAM_TEXT_CAP
            self.recovered[n] = _recover(n, line, self.calls)
            self.calls.mark_ids(line)
            return "too_long"
        v = _values(line)
        why = "too_many_values" if v > MAX_LINE_VALUES else "value_budget" if v > self.values else None
        new: set[str] = set()
        e: Any = None
        chars = 0
        if why is None:
            keys = _line_keys(line)
            new = keys - self.seen
            why = "too_many_keys" if len(keys) > MAX_LINE_KEYS else "value_budget" if len(new) > self.keys else None
        if why is None:
            e, chars = _parse(line)
            why = "not_json" if e is None else "too_deep" if e is DEEP else None
        if why is not None or not isinstance(e, dict):
            self.text -= min(len(line), STREAM_TEXT_CAP)
            self.calls.mark_ids(line)
            return why or "not_json"
        self.values -= v
        self.keys -= len(new)
        self.seen |= new
        self.text -= chars
        self._took(e)
        self.calls.add(n, e)
        return "event"


def _stub_for(kind: str, found: dict | None = None) -> str:
    stub: dict = {"type": "devmem_omitted", "reason": f"not stored: {_REASONS[kind]}"}
    if found:
        if found.get("type"):
            stub["event_type"] = found["type"]
        if found.get("calls"):
            stub["tool_calls_found"] = len(found["calls"])
    return json.dumps(stub)


def redact_stream(lines: list[str], project: Path, red: Redactor, info: dict | None = None,
                  read: _Pass | None = None) -> list[str]:
    """Redacted copy of a stream-json record, line for line (line numbers stay valid). Tool output goes through
    the same filter as in the hooks (_Calls): nothing of a call that reads a secret file, nothing of a result no
    looked-at call accounts for, and no line or diff hunk of a secret file in any other output (`grep -rn KEY .`
    printing `./.env:1:KEY=...`). A line devmem does not take apart becomes a stub; `info` gets the counts
    (stream_info)."""
    read = read or _Pass(lines, project, red, metrics=False)
    raw, _digest_out, bad = _write_pass(lines, project, red, read)
    if info is not None:
        info.update(stream_info(read, bad))
    return raw


def stream_info(read: _Pass, bad: int = 0) -> dict:
    """What a stream's record left out, for the record's turn and import_note: `stream_lines_left_out` (lines by
    reason), `stream_lines_not_read` (their sum) and `tool_calls_left_out`."""
    left = dict(read.left)
    if bad:
        left["unreadable"] = left.get("unreadable", 0) + bad
    out: dict = {}
    if left:
        out["stream_lines_left_out"] = left
        out["stream_lines_not_read"] = sum(left.values())
    if read.calls.left_out:
        out["tool_calls_left_out"] = len(read.calls.left_out)
    if read.calls.unchecked:
        out["tool_calls_not_checked"] = len(read.calls.unchecked)
    if read.calls.unsure:          # more ids on lines not taken apart than MAX_MARKED_IDS: no result text was kept
        out["tool_result_texts_left_out"] = f"all (more than {MAX_MARKED_IDS:,} ids on stub lines: MAX_MARKED_IDS)"
    return out


def _write_pass(lines: list[str], project: Path, red: Redactor, read: _Pass) -> tuple[list[str], dict, int]:
    """The second reading (record_session): each line taken apart in the first is parsed again and cut once
    (_precut); the raw record's line and the digest's entries are both made from that one copy. (raw lines, digest,
    lines that could not be taken apart after all)."""
    out: list[str] = []
    texts: list = []
    tools: list = []
    errors: list = []
    left = {"assistant_text": 0, "tool_call_entries": 0, "errors": 0}
    by_id: dict[str, list[dict]] = {}
    bad = 0
    for n, (line, kind) in enumerate(zip(lines, read.kind)):
        if kind != "event":
            found = read.recovered.get(n)
            out.append(_stub_for(kind, found) if kind else "")
            if found:
                _digest_found(n, found, project, red, texts, tools, left)
            continue
        try:
            e = _precut(json.loads(line[1:] if line.startswith("﻿") else line), [STREAM_TEXT_BUDGET])
            stored = _redact_event(n, e, read.calls, red)
        except (TypeError, ValueError, AttributeError, KeyError, RecursionError) as exc:
            bad += 1
            out.append(json.dumps({"type": "devmem_omitted",
                                   "reason": f"not stored: devmem could not read this line ({type(exc).__name__})"}))
            continue
        out.append(stored)
        try:
            _digest_event(n, e, project, red, texts, tools, errors, left, by_id, read.calls)
        except (TypeError, ValueError, AttributeError, KeyError, RecursionError):
            pass
    digest = {"assistant_text": texts, "tool_calls": tools, "errors": errors,
              "left_out": {k: v for k, v in left.items() if v}}
    return out, digest, bad


def _result_content(content, tid: str | None, calls: _Calls):
    """A tool_result's content as it may be stored (_Calls.output), a string or a list of text blocks."""
    if isinstance(content, str):
        return calls.output(tid, content)
    instead = calls.decide(tid)
    if instead is not None:
        return instead
    if isinstance(content, list):
        return [{**c, "text": calls.output(tid, c["text"])} if isinstance(c, dict) and isinstance(c.get("text"), str)
                else _kept(c, calls) for c in content]
    return _kept(content, calls)


def _kept(v, calls: _Calls):
    """A structured value stored on an "ok" decision: every part that names a secret file keeps its paths only
    (redact.scrub_secret_parts), and the lines of secret files are removed from every string."""
    return _filter_strings(scrub_secret_parts(v, calls.project, calls.red.secret_patterns), calls.red)


# The fields of an event (and of its `message`) that say what it is, kept as they are when they hold a number, a
# boolean, null or a string of at most MAX_FIELD_CHARS characters. Every other field (`tool_use_result`, a transcript's
# `toolUseResult`, `wireToolInputs`, `attachment`, any field devmem does not know) goes by _side.
_META_FIELDS = frozenset({
    "type", "subtype", "uuid", "parentUuid", "logicalParentUuid", "parent_tool_use_id", "timestamp", "sessionId",
    "session_id", "cwd", "version", "gitBranch", "isSidechain", "isMeta", "userType", "entrypoint", "slug", "agentId",
    "promptId", "requestId", "sourceToolAssistantUUID", "sourceToolUseID", "toolUseID", "permissionDecision",
    "sandboxed", "isCompactSummary", "isVisibleInTranscriptOnly", "level", "role", "id", "model", "stop_reason",
    "stop_sequence", "is_error", "duration_ms", "duration_api_ms", "num_turns", "total_cost_usd"})
_RESULT_FIELDS = ("tool_use_result", "toolUseResult")
_REF_KEYS = frozenset({"id", "tool_use_id", "toolUseId", "toolUseID", "sourceToolUseID", "tool_use_ids"})


def _no_text(v) -> bool:
    """v holds no string except keys (usage counts, flags): nothing of a file can be in it."""
    stack = [v]
    while stack:
        x = stack.pop()
        if isinstance(x, str):
            return False
        if isinstance(x, dict):
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return True


def _refs(v, calls: _Calls) -> tuple[list[str], bool]:
    """(the decisions of the calls this value refers to: a key, or a string under an id-like key, that is the id of
    a call of the stream; whether it holds a tool call or result block, or a tool_use_id no call has)."""
    found: list[str] = []
    blocks = False
    stack: list = [(v, "", False)]      # (value, its key, under a key that names tools: `toolInputCopies`)
    while stack:
        x, key, tools = stack.pop()
        if isinstance(x, dict):
            kind = x.get("type")
            if _is_call(x) or isinstance(kind, str) and (kind == "tool_result" or kind.endswith("_tool_result")):
                blocks = True
            for k, w in x.items():
                if isinstance(k, str) and k in calls.ids:
                    found.append(calls.ids[k])
                ks = k if isinstance(k, str) else ""
                stack.append((w, ks, tools or "tool" in ks.lower()))
        elif isinstance(x, list):
            stack.extend((w, key, tools) for w in x)
        elif key in _REF_KEYS and _key(x) is not None:
            tid = _key(x)
            if tid in calls.ids:
                found.append(calls.ids[tid])
            elif key != "id" or tools:
                blocks = True          # a reference to a call devmem has no decision about
    return found, blocks


def _side(k, v, tied: list[str] | None, calls: _Calls, red: Redactor):
    """What is stored of field `k` of an event, or of its message, other than the message's content (_redact_event).
    A number, boolean or null, a value without any string, and a short string under a field of _META_FIELDS are kept.
    On an event that carries tool calls or results (`tied`: their decisions), any other field is the output (or the
    input) again in another shape: kept, without the lines of secret files, only when every decision is "ok", else
    the fixed text of the worst. On any other event, a field that refers to a call (its id) takes that call's
    decision the same way, one that holds a call or result block or a tool_use_id no call has is LEFT_OUT_TOOL, one
    that names a secret file is stored as redact.secret_call_input gives it, and any other has the lines of secret
    files removed."""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str) and len(v) <= MAX_FIELD_CHARS and k in _META_FIELDS:
        return v
    if _no_text(v):
        return v
    if tied is None:
        tied, blocks = _refs(v, calls)
        if blocks:
            return LEFT_OUT_TOOL
        if not tied:
            if call_names_secret_file("", {"x": v}, calls.project, red.secret_patterns):
                return secret_call_input({"x": v}, red.secret_patterns)["x"]
            return _kept(v, calls)
    for status in ("secret", "unchecked", "out"):
        if status in tied:
            return _Calls.MARKERS[status]
    if calls.unsure:
        return LEFT_OUT_TOOL
    return _kept(v, calls)


def _redact_event(n: int, e: dict, calls: _Calls, red: Redactor) -> str:
    """Stream event `n` (parsed, cut once), filtered and redacted, as a JSON line (_write_pass). The tool calls left
    out, the results whose text _Calls does not let through, and the results of one line past MAX_LINE_TOOL_USES are
    not stored at all: one block in their place counts them (a block each would still cost its strings' redaction).
    A call that names a secret file keeps of its input what redact.secret_call_input keeps. Every other field of the
    event and of its message goes by _side."""
    msg = e.get("message") if isinstance(e.get("message"), dict) else None
    content = msg.get("content") if msg else None
    tied: list[str] | None = None
    if isinstance(content, list):
        blocks = []
        results: list[str | None] = []
        statuses: list[str] = []
        omitted = {"tool_uses": 0, "tool_results": 0}
        at = -1                                   # where the counting block goes: the first block it stands for
        for k, b in enumerate(content):
            kind = b.get("type") if isinstance(b, dict) else None
            if _is_call(b):
                statuses.append(calls.status(n, k))
                if (n, k) not in calls.looked:
                    omitted["tool_uses"] += 1
                    at = len(blocks) if at < 0 else at
                    continue
            if _is_result(b):
                tid = _key(b.get("tool_use_id"))
                instead = calls.decide(tid) if len(results) < MAX_LINE_TOOL_USES else LEFT_OUT_TOOL
                results.append(tid if instead != LEFT_OUT_TOOL else None)
                if instead == LEFT_OUT_TOOL:
                    omitted["tool_results"] += 1
                    at = len(blocks) if at < 0 else at
                    continue
                kept = {key: (_result_content(v, tid, calls) if key == "content" else
                              v if key in ("type", "tool_use_id", "is_error") else
                              _side(key, v, [calls.verdict(tid)], calls, red))
                        for key, v in b.items()}
                blocks.append(kept)
                continue
            if not isinstance(b, dict):
                blocks.append(b)
            elif kind in _THINKING:
                blocks.append({"type": kind, "thinking": "[hidden reasoning is not stored]"})
            elif _is_call(b) and ((n, k) in calls.secret or (n, k) in calls.unchecked):
                # a secret file's call keeps its stored form; one too large to check, the fact and why only
                form = calls.unchecked.get((n, k)) or secret_call_input(b["input"], red.secret_patterns)
                blocks.append({key: (form if key == "input" else
                                     v if key in ("type", "id", "name") and isinstance(v, (str, int)) else
                                     OMITTED_SECRET_FILE) for key, v in b.items()})
            else:
                blocks.append(b)
        if at >= 0:
            blocks.insert(at, {"type": "devmem_omitted", "reason": LEFT_OUT_TOOL,
                               **{k: v for k, v in omitted.items() if v}})
        decisions = ["out" if t is None else calls.verdict(t) for t in results] + statuses
        if decisions or any(f in e for f in _RESULT_FIELDS):
            tied = decisions or ["out"]           # a result field with no result it belongs to
        msg = {**{k: (v if k == "content" else _side(k, v, tied, calls, red)) for k, v in (msg or {}).items()},
               "content": blocks}
    elif any(f in e for f in _RESULT_FIELDS):
        tied = ["out"]                            # no tool_result it belongs to
    elif msg is not None:
        msg = {k: (v if k == "content" else _side(k, v, tied, calls, red)) for k, v in msg.items()}
    e = {k: (msg if k == "message" and msg is not None else _side(k, v, tied, calls, red)) for k, v in e.items()}
    return json.dumps(_bounded(e, red), ensure_ascii=False)


OUTSIDE_PROJECT = "outside the project: the path is not stored"


def _digest(stream_path: Path, project: Path, red: Redactor | None = None, lines: list[str] | None = None,
            read: _Pass | None = None) -> dict:
    """Extract visible, source-linked facts from a stream-json record (one turn). `lines`: the file's lines, already
    read (_load_lines). The tool calls are those _Calls looked at (and those found on a line too long to parse), a
    result's text is what _Calls lets through, and the lines read are those the raw record reads (one text budget).
    Each list keeps at most MAX_STREAM_ITEMS entries; `left_out` counts the rest."""
    red = red or Redactor(None)
    lines = _load_lines(stream_path) if lines is None else lines
    read = read or _Pass(lines, project, red, metrics=False)
    return _write_pass(lines, project, red, read)[1]


def _path_form(p: str) -> str:
    """A path as a tool may spell one inside the project, in the form rel_path reads: `./src/x` -> `src/x`; on
    Windows also `\\\\?\\C:\\x` -> `C:\\x`, Git Bash's `/c/x` and WSL's `/mnt/c/x` -> `C:/x`."""
    while p.startswith(("./", ".\\")):
        p = p[2:]
    if os.name == "nt":
        if p.startswith(("\\\\?\\", "//?/")) and not p[4:8].upper() == "UNC\\":
            p = p[4:]
        m = re.match(r"^/(?:mnt/)?([A-Za-z])(/|$)", p)
        if m:
            p = f"{m.group(1).upper()}:/" + p[m.end():]
    return p


def _entries(name: str, inp: dict, project: Path, red: Redactor, decision, unchecked: dict | None = None) -> list[dict]:
    """The digest's entries for one call (sessions.tool_entries, with the decision _Calls took: "secret", "ok", or
    for a call too large to check the one entry `unchecked` (redact.unchecked_input, measured on the whole input)),
    each path spelled as rel_path reads it (_path_form) and one outside the project not stored."""
    if unchecked is not None:
        return [{"tool": name, **unchecked}]
    inp = {k: (_path_form(v) if k in _PATH_FIELDS and isinstance(v, str) else v) for k, v in inp.items()}
    out = []
    for entry in S.tool_entries(name, inp, project, red, secret_call=decision):
        if isinstance(entry.get("file"), str) and entry["file"] != "." and not S._is_inside(entry["file"]):
            del entry["file"]                       # an absolute path names a folder outside the project
            entry["path_omitted"] = OUTSIDE_PROJECT
        out.append(entry)
    return out


def _add_entries(entries: list[dict], tools: list, left: dict) -> list[dict]:
    """Entries into the digest's tool list, at most MAX_STREAM_ITEMS in all (the rest counted): those added."""
    room = max(0, MAX_STREAM_ITEMS - len(tools))
    tools.extend(entries[:room])
    left["tool_call_entries"] += max(0, len(entries) - room)
    return entries[:room]


def _digest_found(i: int, found: dict, project: Path, red: Redactor, texts: list, tools: list, left: dict) -> None:
    """What _recover read of a line too long to parse into the digest: its assistant texts, and an entry for each
    tool call with a path (input_left_out: the rest of the call's input was not read)."""
    for t in found.get("texts") or []:
        if t.strip():
            if len(texts) >= MAX_STREAM_ITEMS:
                left["assistant_text"] += 1
                continue
            texts.append({"line": i, "text": _bounded_text(t.strip(), red), "source": "assistant-visible"})
    if found.get("type") != "assistant":
        return
    for name, paths, secret in found.get("calls") or []:
        if not (name and paths):
            continue
        entries = [{"line": i, **x} for x in _entries(name, dict(paths), project, red, secret)]
        for entry in entries:
            for key in ("old", "new", "command", "pattern", "input_summary"):
                entry.pop(key, None)
            entry["truncated"] = True
            entry["input_left_out"] = "the call's line is longer than devmem parses (MAX_LINE_PARSE_CHARS)"
        _add_entries(entries, tools, left)


def _digest_event(i: int, e: dict, project: Path, red: Redactor, texts: list, tools: list, errors: list, left: dict,
                  by_id: dict, calls: _Calls) -> None:
    """One parsed event into the digest's lists (_write_pass)."""
    content = _msg(e).get("content")
    if not isinstance(content, list):
        return
    for k, b in enumerate(content):
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "text" and e.get("type") == "assistant" and isinstance(b.get("text"), str) and b["text"].strip():
            if len(texts) >= MAX_STREAM_ITEMS:
                left["assistant_text"] += 1
                continue
            texts.append({"line": i, "text": _bounded_text(b["text"].strip(), red), "source": "assistant-visible"})
        elif kind == "tool_use" and (i, k) in calls.looked:
            if len(tools) >= MAX_STREAM_ITEMS:         # an entry per edit of a MultiEdit (sessions.MAX_EDITS of them)
                edits = b["input"].get("edits") if b["name"] == "MultiEdit" else None
                left["tool_call_entries"] += min(len(edits), S.MAX_EDITS) if isinstance(edits, list) and edits else 1
                continue
            tid = _key(b.get("id"))
            entries = [{"line": i, **x} for x in _entries(b["name"], b["input"], project, red, calls.status(i, k),
                                                          calls.unchecked.get((i, k)))]
            for entry in _add_entries(entries, tools, left):
                if tid:
                    by_id.setdefault(tid, []).append(entry)
        elif _is_result(b) and e.get("type") == "user" and b.get("is_error"):
            if len(errors) >= MAX_STREAM_ITEMS:
                left["errors"] += 1
                continue
            tid = _key(b.get("tool_use_id"))
            for entry in by_id.get(tid, []) if tid else []:
                entry["ok"] = False
            instead = calls.decide(tid)
            if instead is None:
                text = b.get("content")
                text = text if isinstance(text, str) else S.output_text(_kept(text, calls), red, 300 + 2048)
                instead = calls.output(tid, text[:300 + 2048])
            errors.append({"line": i, "error": red.capped(instead, 300)[0]})


def _observed_deletions(project: Path, touched: set[str], git_before: dict, git_after: dict | None,
                        deleted_before: set[str] | None = None) -> list[str]:
    def absent(f: str) -> bool:
        if "$" in f or "%" in f or f.startswith("~"):
            return False         # `$HOME/x`, `%USERPROFILE%\x`, `~/x`: a name a shell expands is not this file
        try:
            return not (project / f).exists()
        except OSError:          # a name the system cannot look up (too long: ENAMETOOLONG) is no evidence
            return False
    gone = {f for f in touched if S._is_inside(f) and absent(f)}
    if git_before and git_after is not None:
        listed = git_before.get("deleted")      # a snapshot from before devmem 0.3.1 listed them
        before = deleted_before if deleted_before is not None else set(listed) if isinstance(listed, list) else set()
        gone |= {f for f, c in git_after.items() if "D" in c and f not in before}
    return sorted(gone)


def _field(v) -> str | None:
    """A session id or model name from the stream: a string of at most MAX_FIELD_CHARS, else None."""
    return v if isinstance(v, str) and len(v) <= MAX_FIELD_CHARS else None


def record_session(project: Path, stream_path: Path, prompt: str, started_at: str,
                   extra: dict | None = None, store: Path | None = None, git_start: dict | None = None,
                   deleted_before: set[str] | None = None, info: dict | None = None,
                   at_session_end: bool | None = None, not_written: int = 0,
                   write_failed: bool = False, run_flags: dict | None = None) -> Path:
    """Store a finished stream-json file as a schema-2 session record (one turn) and write its digest.

    `git_start` (from devmem.sessions.git_snapshot before the run) lets the record carry the HEAD at
    start; HEAD at the end is read now; `deleted_before` (the paths git listed as deleted then, devmem.sessions.git_observe)
    keeps deletions made before the run out of the record. Everything is redacted before it is written. What is read
    and stored is bounded (STREAM_READ_HEAD and its neighbours above); `info` gets what was left out (import_note).

    `at_session_end` (default: a `git_start` was given): the stream was captured as the session ended (`devmem run`),
    so a file its tool calls named that is gone now, or that git lists as deleted since the start, is recorded as
    deleted by the session ("observed on disk at capture"). Otherwise (`devmem import`, at any later time) absence now
    is no evidence: such a file is listed in the turn's `files_absent_at_capture` (bounded, the rest counted), with no
    Delete entry.

    `not_written`: bytes the program's output had that `devmem run` did not write to the stream file
    (proc.run_to_file), recorded in the turn's `stream_left_out`; `write_failed`: the stream file could not be
    written at all (its text is not recorded); `run_flags`: {"timed_out", "output_not_waited", "interrupted"} from
    run_claude."""
    project = Path(project)
    store_dir = ensure_store(Path(store or store_path(project)))
    red = Redactor(store_dir)
    cut: dict = {}
    lines = _load_lines(stream_path, cut)
    first = _Pass(lines, project, red)               # each line parsed here and once more for the raw record + digest
    metrics = first.metrics
    for k in ("session_id", "model"):
        metrics[k] = _field(metrics.get(k))
    raw_sid = metrics.get("session_id")
    if isinstance(raw_sid, str) and "\0" in raw_sid:
        metrics["session_id"] = None    # not kept, as one too long is not
    import hashlib
    # the record's file names: an id that is no plain file name (a NUL, a separator, `..`, a device name) is named by
    # its hash only, so it never reaches a path and two such ids never share a name
    name_id = raw_sid if isinstance(raw_sid, str) and S.safe_id(raw_sid) else \
        "id" + hashlib.sha256(raw_sid.encode("utf-8", "surrogatepass")).hexdigest()[:16] \
        if isinstance(raw_sid, str) and raw_sid else "unknown"
    base = S.session_base(store_dir, started_at, name_id)
    raw_lines, digest, bad = _write_pass(lines, project, red, first)   # one cut copy per line for both
    read = stream_info(first, bad)
    if "encoding" in cut:            # how much of a file whose encoding could not be read reliably was read
        cut["encoding"].update(lines_read=sum(1 for k in first.kind if k == "event") - bad,
                               lines_left_out=read.get("stream_lines_not_read", 0))
    prompt_r, prompt_cut = red.capped(prompt, PROMPT_CAP)
    turn = S.new_turn(0, prompt_r, started_at)
    turn["prompt_source"] = S.PROMPT_FROM_USER  # the task the user gave `devmem run` / `devmem import`
    if prompt_cut:
        turn["prompt_truncated"] = True
    if not_written or write_failed:
        cut["not_written"] = not_written
    if write_failed:
        cut["write_failed"] = True
    for k in ("timed_out", "output_not_waited", "interrupted"):
        if (run_flags or {}).get(k):
            cut[k] = True
    if cut:
        turn["stream_left_out"] = dict(cut)
    for k, n in (digest.get("left_out") or {}).items():
        turn[f"{k}_left_out"] = n
    turn.update(read)
    result = metrics.get("result_text")
    turn.update(assistant_text=digest["assistant_text"], tool_calls=digest["tool_calls"], errors=digest["errors"],
                result_text=_bounded_text(result, red) if isinstance(result, str) else result,
                result_source="assistant-visible (stream-json result)" if result else None)
    touched = {c.get("file") for c in digest["tool_calls"] if isinstance(c.get("file"), str) and c.get("file")}
    git_after = S.git_status(project)
    gone, more = S.bounded_paths(_observed_deletions(project, touched, git_start or {}, git_after, deleted_before))
    if at_session_end if at_session_end is not None else git_start is not None:
        for f in gone:
            turn["files_deleted"].append(f)
            if len(turn["tool_calls"]) < MAX_STREAM_ITEMS:      # the digest's cap holds for these entries too
                turn["tool_calls"].append({"tool": "Delete", "file": f, "source": "observed on disk at capture"})
            else:
                turn["tool_call_entries_left_out"] = int(turn.get("tool_call_entries_left_out") or 0) + 1
        if more:
            turn["files_deleted_more"] = more
    elif gone:
        turn["files_absent_at_capture"] = gone       # named by the stream's tool calls, not there when imported
        if more:
            turn["files_absent_at_capture_more"] = more
    git = {}
    if git_after is not None:  # a git work tree: HEAD at end always, at start when the caller took it
        git = {"head_end": S.git_head(project), "dirty_end": bool(git_after)}
        if git_start:
            git = {"head_start": git_start.get("head"), "dirty_start": git_start.get("dirty"), **git}
    rec = {
        "schema": S.SCHEMA,
        "session_id": metrics.get("session_id"),
        "started_at": started_at,
        "ended_at": first.timestamp,
        "provider": "claude-code",
        "capture_method": "stream-json",
        "model": metrics.get("model"),
        "git": git,
        "turns": [turn],
        "usage": {k: (v if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
                  for k, v in ((k, metrics.get(k)) for k in S.USAGE_KEYS)},
        **(extra or {}),
    }
    if info is not None:
        info.update({k: turn[k] for k in turn if k.endswith("_left_out") or k in ("prompt_truncated", "stream_lines_not_read",
                                                                                     "tool_calls_not_checked")})
    S.derive_aggregates(rec, project)
    if not result:
        rec["result_source"] = None  # v1 shape: absent summary means "assistant-visible summary" in pack.py
    return S.write_record(store_dir, rec, raw_lines=raw_lines, base=base, red=red)


def import_note(info: dict) -> str | None:
    """The line `devmem import` / `run` adds when the record left something out (record_session's `info`), else
    None."""
    parts = []
    cut = info.get("stream_left_out") or {}
    if cut.get("write_failed"):
        parts.append(f"the program's output could not be written to the stream file: {cut.get('not_written', 0):,} "
                     "bytes of it were not recorded")
    elif cut.get("not_written"):
        parts.append(f"about {cut['not_written']:,} bytes from the middle of the program's output were not written "
                     "(devmem.proc.STREAM_FILE_HEAD / STREAM_FILE_TAIL: the whole lines within its first and last 64 MB "
                     "are kept)")
    if cut.get("timed_out"):
        parts.append("the program was stopped at its timeout (it and the processes it started were killed)")
    if cut.get("output_not_waited"):
        parts.append("a process the program started still held its output when the program ended: output after "
                     "that was not waited for (devmem.proc.EXIT_GRACE_S)")
    if cut.get("bytes"):
        parts.append(f"the stream file was read only in part ({cut.get('read_bytes', 0):,} bytes of it: its first and "
                     f"last {STREAM_READ_HEAD // (1024 * 1024)} MB; "
                     f"about {cut['bytes']:,} bytes from its middle left out: STREAM_READ_HEAD / STREAM_READ_TAIL)")
    if cut.get("lines"):
        parts.append(f"{cut['lines']:,} lines of the stream were not read (MAX_STREAM_LINES: its first and last "
                     f"{MAX_STREAM_LINES // 2:,} are)")
    enc = cut.get("encoding") or {}
    if enc:
        why = [x for x in (f"its encoding changes {enc['switches']:,} times" if enc.get("switches") else "",
                           f"{enc['undecodable']:,} byte sequences are not valid in it" if enc.get("undecodable")
                           else "") if x]
        parts.append(f"the stream file's encoding could not be read reliably (read as {enc.get('read_as')}; "
                     f"{' and '.join(why)}): {enc.get('lines_read', 0):,} lines were taken apart, "
                     f"{enc.get('lines_left_out', 0):,} are stubs")
    for k, what in (("assistant_text", "assistant text entries"), ("tool_call_entries", "tool call entries of the "
                    "digest"), ("errors", "error entries")):
        if info.get(f"{k}_left_out"):
            parts.append(f"{info[f'{k}_left_out']:,} {what} left out (MAX_STREAM_ITEMS)")
    if info.get("prompt_truncated"):
        parts.append(f"the prompt was cut to {PROMPT_CAP:,} characters")
    left = info.get("stream_lines_left_out") or {}
    if left or info.get("stream_lines_not_read"):
        why = "; ".join(f"{n:,} {_REASONS.get(k, k)}" for k, n in sorted(left.items(), key=lambda x: -x[1]))
        parts.append(f"{info.get('stream_lines_not_read') or sum(left.values()):,} stream lines were left out and are "
                     f"stored as stubs" + (f" ({why})" if why else "") + (
                         "; of the tool calls on them only those found in the first 64 KB of a line too long to parse "
                         "are in the record (their name and path, counted below)" if left.get("too_long") else
                         "; the tool calls on them are not in the record"))
    if info.get("tool_calls_left_out"):
        parts.append(f"{info['tool_calls_left_out']:,} tool calls not looked at (past MAX_STREAM_ITEMS / "
                     "MAX_LINE_TOOL_USES or the text budget, malformed, outside an assistant event, or on a line too "
                     "long to parse) are counted; their input and their results' output are not stored")
    if info.get("tool_calls_not_checked"):
        parts.append(f"{info['tool_calls_not_checked']:,} tool calls had an input too large to check for secret-file "
                     "names (a command over 24,096 characters, say): only that is recorded, not their input or output")
    if info.get("tool_result_texts_left_out"):
        parts.append("no tool result's text was kept: the stream holds more than "
                     f"{MAX_MARKED_IDS:,} call ids on lines devmem does not take apart (MAX_MARKED_IDS)")
    if cut.get("interrupted"):
        parts.append("the run was interrupted (Ctrl-C): the record holds what the program wrote until then")
    return ("devmem: the record is bounded: " + "; ".join(parts)) if parts else None


def run_and_capture(project: Path, prompt: str, model: str = "haiku", max_turns: int = 30,
                    isolated: bool = True, store: Path | None = None, info: dict | None = None) -> Path:
    """Run a session in `project` and record it in `store` (default <project>/.devmemory); `info` as
    record_session's (import_note)."""
    project = Path(project).resolve()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    git_start, status = S.git_observe(project)
    deleted_before = {f for f, c in (status or {}).items() if "D" in c}
    # Stream to a temp file outside the project so the agent never sees a half-written record.
    tmp = Path(tempfile.mkdtemp(prefix="devmem-cap-")) / "incoming.jsonl"
    try:
        ran = run_claude(prompt, project, tmp, model=model, max_turns=max_turns, isolated=isolated,
                         env={NO_CAPTURE_ENV: "1"}, parse=False)       # the stream is read once, here
        meta = record_session(project, tmp, prompt, started, store=store, git_start=git_start or None,
                              deleted_before=deleted_before, info=info, at_session_end=True,
                              not_written=int((ran or {}).get("stream_bytes_left_out") or 0),
                              write_failed=bool((ran or {}).get("stream_write_failed")), run_flags=ran or {})
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    return meta
