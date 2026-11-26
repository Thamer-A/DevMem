"""Run headless Claude Code (`claude -p`) and parse its documented stream-json output.

Only documented CLI output is used: `--output-format stream-json --verbose` events
(system/init, assistant, user, rate_limit_event, result). No private transcript files.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from . import proc
from .store import find_program

# Identical isolation flags for every eval run (see bench/TELEMETRY.md).
DEFAULT_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]
FILE_READ_TOOLS = {"Read"}
FILE_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SEARCH_TOOLS = {"Glob", "Grep"}


def claude_executable() -> str:
    """Resolve the real claude binary. The npm .cmd shim goes through cmd.exe, which
    breaks multi-line arguments, so prefer the bundled claude.exe when present."""
    env = os.environ.get("DEVMEM_CLAUDE")
    if env:
        return env
    found = find_program("claude")
    if not found:
        raise FileNotFoundError("claude CLI not found on PATH (set DEVMEM_CLAUDE)")
    p = Path(found)
    if p.suffix.lower() in {".cmd", ".bat", ""}:
        exe = p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if exe.exists():
            return str(exe)
    return found


def build_command(prompt: str, model: str, max_turns: int, append_system_prompt: str | None = None,
                  tools: list[str] | None = None, isolated: bool = True,
                  allowed_tools: list[str] | None = None) -> list[str]:
    cmd = [claude_executable(), "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--model", model, "--max-turns", str(max_turns),
           "--permission-mode", "acceptEdits", "--no-session-persistence"]
    if isolated:
        cmd += ["--setting-sources", "project", "--strict-mcp-config"]
    cmd += ["--tools", *(tools or DEFAULT_TOOLS)]
    if allowed_tools:  # pre-approved commands (headless mode cannot ask for approval)
        cmd += ["--allowedTools", *allowed_tools]
    if append_system_prompt:
        cmd += ["--append-system-prompt", append_system_prompt]
    return cmd


def run_claude(prompt: str, cwd: Path, stream_out: Path, model: str = "haiku", max_turns: int = 30,
               append_system_prompt: str | None = None, tools: list[str] | None = None,
               timeout_s: int = 900, isolated: bool = True, allowed_tools: list[str] | None = None,
               env: dict[str, str] | None = None, parse: bool = True) -> dict:
    """Run one headless session in `cwd`, write the raw stream to `stream_out`, return metrics.
    `env`: extra environment variables for the session (added to the current environment). `parse=False`: the
    stream is not read here (devmem.capture.run_and_capture reads it once, for the record); the metrics are then
    only exit_code, wall_s, stderr_tail and stream_bytes_left_out."""
    from .capture import _load_lines
    cmd = build_command(prompt, model, max_turns, append_system_prompt, tools, isolated, allowed_tools)
    stream_out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    # the stream file holds at most proc.STREAM_FILE_HEAD + _TAIL bytes, whole lines and one saying what was left
    # out; stderr at most proc.CHILD_OUTPUT_HEAD + _TAIL bytes (its last 500 characters are kept). At the timeout the
    # program's process tree is killed, and what it wrote until then is kept and counted the same way.
    r = proc.run_to_file(cmd, stream_out, cwd=str(cwd), stdin=subprocess.DEVNULL, timeout=timeout_s,
                         env={**os.environ, **env} if env else None, gap_line=_gap_line)
    rc, stderr = r.returncode, ("timeout" if r.timed_out else r.stderr)
    metrics = parse_stream(stream_out, cwd, lines=_load_lines(stream_out)) if parse else {}   # within capture's bounds
    if r.stdout_dropped:
        metrics["stream_bytes_left_out"] = r.stdout_dropped
    if r.write_failed:
        metrics["stream_write_failed"] = True
    if r.output_not_waited:
        metrics["output_not_waited"] = True       # a process the program started still held its output
    if r.timed_out:
        metrics["timed_out"] = True
    if r.interrupted:
        metrics["interrupted"] = True      # Ctrl-C: the program was stopped; what it wrote is kept
    metrics["exit_code"] = rc
    metrics["wall_s"] = round(time.time() - t0, 2)
    if rc != 0:
        metrics["stderr_tail"] = (stderr or "")[-500:]
    return metrics


def _gap_line(n: int) -> str:
    """The line run_to_file writes where it left out the middle of the program's output."""
    return json.dumps({"type": "devmem_omitted", "reason": f"not written: about {n:,} bytes from the middle of the "
                       "program's output (devmem.proc.STREAM_FILE_HEAD/_TAIL)"})


def _rel(path: str, cwd: Path) -> str:
    from .sessions import rel_path      # never resolves a network path (devmem.store.is_remote_path)
    return rel_path(path, cwd) or ""


def _obj(d: dict, key: str) -> dict:
    """d[key] when it is an object, else {} (a hand-made stream can hold anything there)."""
    v = d.get(key)
    return v if isinstance(v, dict) else {}


# A stream line longer than this is not parsed (devmem.capture stores it as a stub): json.loads holds about 35 times
# a line of small values (one of 63 MB of empty tool_use blocks: 2.2 GB).
MAX_LINE_PARSE_CHARS = 4 * 1024 * 1024


def new_metrics() -> dict:
    """parse_stream's metrics before any event."""
    return {"session_id": None, "model": None, "tool_calls": 0, "tool_counts": {},
            "files_read": [], "files_edited": [], "search_calls": 0, "tool_errors": 0,
            "rate_limit": None, "result_text": None, "is_error": None, "subtype": None,
            "num_turns": None, "cost_usd_list": None, "duration_ms": None,
            "input_tokens": None, "output_tokens": None, "cache_read_tokens": None,
            "cache_creation_tokens": None, "total_input_tokens": None}


def parse_stream(stream_path: Path, cwd: Path | None = None, lines: list[str] | None = None,
                 paths: bool = True) -> dict:
    """Summarize a stream-json file into flat metrics. Missing fields stay None. `lines`: the file's lines, already
    read (devmem.capture reads a large file within its bounds); a line that is not a JSON object is not an event,
    and each event is parsed and dropped in turn. `paths=False`: files_read / files_edited stay empty
    (devmem.capture takes the files from its digest, which is bounded; each path made relative costs about 0.5 ms)."""
    if lines is None:
        lines = Path(stream_path).read_text(encoding="utf-8", errors="replace").split("\n")   # not splitlines(): U+2028
    m = new_metrics()
    root = (cwd or Path(".")) if paths else None
    for line in lines:
        line = line.strip()
        if not line or len(line) > MAX_LINE_PARSE_CHARS:
            continue
        try:
            e = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(e, dict):
            continue
        try:
            _metrics_from(e, m, root)
        except (TypeError, ValueError, AttributeError, KeyError, RecursionError):
            continue            # a field of the wrong type: that event adds nothing (devmem.capture stores it as a stub)
    m["files_read_unique"] = len(set(m["files_read"]))
    return m


def _metrics_from(e: dict, m: dict, root: Path | None) -> None:
    """One stream event into parse_stream's metrics (`root` None: no file paths)."""
    t = e.get("type")
    if m["session_id"] is None and isinstance(e.get("sessionId"), str) and e["sessionId"]:
        m["session_id"] = e["sessionId"]      # a Claude Code transcript: no system/init event, camelCase ids
    if t == "system" and e.get("subtype") == "init":
        m["session_id"], m["model"] = e.get("session_id"), e.get("model")
    elif t == "assistant":
        if m["model"] is None and isinstance(_obj(e, "message").get("model"), str):
            m["model"] = _obj(e, "message")["model"]     # a transcript's assistant message names its model
        content = _obj(e, "message").get("content")
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name, inp = b.get("name") if isinstance(b.get("name"), str) else None, _obj(b, "input")
            m["tool_calls"] += 1
            m["tool_counts"][name] = m["tool_counts"].get(name, 0) + 1
            fp = inp.get("file_path") if isinstance(inp.get("file_path"), str) else None
            if name in FILE_READ_TOOLS and fp:
                if root is not None:
                    m["files_read"].append(_rel(fp, root))
            elif name in FILE_EDIT_TOOLS and fp:
                if root is not None:
                    m["files_edited"].append(_rel(fp, root))
            elif name in SEARCH_TOOLS:
                m["search_calls"] += 1
    elif t == "user":
        content = _obj(e, "message").get("content")
        if isinstance(content, list):
            m["tool_errors"] += sum(1 for b in content
                                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"))
    elif t == "rate_limit_event":
        info = _obj(_obj(e, "rate_limit_info"), "unifiedWindows")
        m["rate_limit"] = {k: v.get("utilization") for k, v in info.items() if isinstance(v, dict)}
    elif t == "result":
        u = _obj(e, "usage")
        m.update(session_id=e.get("session_id") or m["session_id"], result_text=e.get("result"),
                 is_error=e.get("is_error"), subtype=e.get("subtype"), num_turns=e.get("num_turns"),
                 cost_usd_list=e.get("total_cost_usd"), duration_ms=e.get("duration_ms"),
                 input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
                 cache_read_tokens=u.get("cache_read_input_tokens"),
                 cache_creation_tokens=u.get("cache_creation_input_tokens"))
        parts = [m["input_tokens"], m["cache_read_tokens"], m["cache_creation_tokens"]]
        if all(isinstance(p, (int, float)) and not isinstance(p, bool) for p in parts):
            m["total_input_tokens"] = sum(parts)
