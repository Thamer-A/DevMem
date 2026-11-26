"""What devmem reads from outside on the recording and lookup paths is bounded: a program's output (`wrap`, `codex`,
`run`), the begin files an `end` looks through, a stream-json file (`devmem import`), MCP requests and tool arguments,
and the CLI's numeric options. Bounds are lowered with monkeypatch where that keeps a test fast; sizes are measured
where the data is held or written."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from cli_helpers import cli_env, run_cli
from hooks_helpers import make_project

from devmem import capture as C
from devmem import generic as G
from devmem import proc
from devmem.mcp_server import MAX_LINE_BYTES, Server, _lines, respond

PY = sys.executable


def _rec(meta):
    return json.loads(meta.read_text(encoding="utf-8"))


def _raw(meta):
    return meta.with_name(meta.name.replace(".meta.json", ".jsonl"))


# ---- proc.run_capped: a program's output is held within a head and a tail ----

def test_run_capped_holds_what_subprocess_run_holds_for_ordinary_output(tmp_path):
    code = ("import sys; sys.stdout.write('a\\r\\nb\\rc\\n\\u00e9\\u4e2d' + sys.stdin.read()); "
            "sys.stderr.write('err\\r\\n')")
    want = subprocess.run([PY, "-c", code], input="in\nput", capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    got = proc.run_capped([PY, "-c", code], input="in\nput")
    assert (got.stdout, got.stderr, got.returncode) == (want.stdout, want.stderr, want.returncode)
    assert got.stdout_dropped == got.stderr_dropped == 0


def test_run_capped_keeps_a_head_and_a_tail_of_a_large_output():
    code = "import sys\nfor i in range(200000): sys.stdout.write(f'{i:07d}\\n')"   # 1.6 M characters
    got = proc.run_capped([PY, "-c", code], head=1000, tail=2000)
    assert got.stdout_head == "".join(f"{i:07d}\n" for i in range(125))
    assert got.stdout_tail == "".join(f"{i:07d}\n" for i in range(200000 - 250, 200000))
    assert got.stdout_dropped == 1_600_000 - 3000 and len(got.stdout) == 3000


def test_run_capped_timeout_kills_the_program():
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        proc.run_capped([PY, "-c", "import time; print('x', flush=True); time.sleep(60)"], timeout=1)
    assert time.monotonic() - t0 < 30


def test_wrap_records_the_tail_and_says_what_was_left_out(tmp_path, monkeypatch):
    monkeypatch.setattr(proc, "CHILD_OUTPUT_HEAD", 50_000)
    monkeypatch.setattr(proc, "CHILD_OUTPUT_TAIL", 50_000)
    project = make_project(tmp_path)
    code = "import sys\nfor i in range(100000): sys.stdout.write(f'line {i:06d}\\n')\nprint('THE END')"
    info: dict = {}
    meta = G.wrap(project, "print a lot", [PY, "-c", code], store=project / ".devmemory", with_pack=False, info=info)
    rec = _rec(meta)
    t = rec["turns"][0]
    assert t["result_text"].endswith("THE END") and t["output_left_out"] == {"stdout_chars": 1_200_008 - 100_000}
    raw = [json.loads(x) for x in _raw(meta).read_text(encoding="utf-8").splitlines()]
    out = next(x for x in raw if x["type"] == "output")
    assert out["text"].endswith("THE END\n") and out["left_out"] == t["output_left_out"]
    assert "middle of the program's output" in G.end_note(info)


def test_wrap_of_an_ordinary_program_records_as_before(tmp_path):
    project = make_project(tmp_path)
    meta = G.wrap(project, "hi", [PY, "-c", "import sys; print('hello'); print('warn', file=sys.stderr)"],
                  store=project / ".devmemory", with_pack=False)
    rec = _rec(meta)
    raw = [json.loads(x) for x in _raw(meta).read_text(encoding="utf-8").splitlines()]
    assert rec["result_text"] == "hello" and "output_left_out" not in rec["turns"][0]
    assert next(x for x in raw if x["type"] == "output") == {"type": "output", "text": "hello\n\n[stderr]\nwarn\n"}


def test_codex_events_from_a_cut_output(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    head = json.dumps({"type": "thread.started"}) + "\n" + '{"type": "item", "cut'
    tail = 'here"}\n' + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}) + "\n"
    fake = proc.Captured(0, (head, tail, 5_000_000), ("", "", 0))
    monkeypatch.setattr(G.proc, "run_capped", lambda *a, **k: fake)
    tmp = tmp_path / "codex-tmp"
    tmp.mkdir()
    meta = G._run_codex(project, "x", "x", PY, tmp, project / ".devmemory", G.Redactor(project / ".devmemory"),
                        None, "workspace-write", False, False)
    rec = _rec(meta)
    assert rec["usage"] == {"input_tokens": 7, "output_tokens": 3}          # from the tail
    assert rec["turns"][0]["output_left_out"] == {"stdout_chars": 5_000_000}
    events = [json.loads(x)["event"] for x in _raw(meta).read_text(encoding="utf-8").splitlines()
              if json.loads(x)["type"] == "tool_event"]
    assert [e["type"] for e in events] == ["thread.started", "turn.completed"]   # the two cut lines are not events


# ---- end: how many begin files it opens and reads ----

def _bad_begins(store, n: int, start: float):
    d = store / "pending"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        f = d / f"bad{i:03d}.json"
        f.write_text(json.dumps({"id": f"bad{i:03d}", "started_at": "--x", "kind": "git", "v": 2}), encoding="utf-8")
        os.utime(f, (start + i, start + i))


def test_end_opens_at_most_max_claim_files_begins(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    good = G.begin(project, store)
    os.utime(good, (1_000_000_000, 1_000_000_000))               # the oldest
    _bad_begins(store, G.MAX_CLAIM_FILES + 4, time.time() - 1000)
    opened = []
    real = G.read_store_text
    monkeypatch.setattr(G, "read_store_text", lambda p, *a, **k: opened.append(p) or real(p, *a, **k))
    with pytest.raises(RuntimeError, match=f"reads at most {G.MAX_CLAIM_FILES} files") as e:
        G.end(project, "x", store=store)
    # secure_pending (bounded by itself) parses a few of them too
    assert len([p for p in opened if p.parent.name == "pending"]) <= G.MAX_CLAIM_FILES + G.SECURE_PENDING_FILES
    assert "--id" in str(e.value) and good.exists()                    # nothing deleted
    assert G.end(project, "x", store=store, pending_id=good.stem).exists()   # its id still ends it


def test_end_reads_at_most_max_claim_bytes(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    _bad_begins(store, 3, time.time() - 100)
    for f in (store / "pending").glob("*.json"):
        f.write_text(json.dumps({"id": f.stem, "started_at": "--x", "pad": "p" * 50_000}), encoding="utf-8")
    monkeypatch.setattr(G, "MAX_CLAIM_BYTES", 120_000)
    with pytest.raises(RuntimeError, match="newest begin files devmem read"):
        G.end(project, "x", store=store)


def test_secure_pending_parses_a_bounded_number_per_call(tmp_path):
    project = make_project(tmp_path, git=False)
    store = project / ".devmemory"
    d = store / "pending"
    d.mkdir(parents=True)
    for i in range(10):
        (d / f"old{i}.json").write_text(json.dumps({"id": f"old{i}", "started_at": "2026-01-01T00:00:00Z",
                                                    "kind": "snapshot", "snapshot": {"a.py": "raw"}}), encoding="utf-8")
    first = G.secure_pending(store)
    assert len(first) == G.SECURE_PENDING_FILES
    while G.secure_pending(store):
        pass
    assert all(json.loads(f.read_text(encoding="utf-8"))["v"] == 2 for f in d.glob("*.json"))


def test_status_says_when_begins_pile_up(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    _bad_begins(store, G.MAX_CLAIM_FILES + 1, time.time() - 100)
    env = cli_env(DEVMEM_USER_DATA=os.environ["DEVMEM_USER_DATA"])
    out = run_cli(["status", "--project", str(project)], cwd=project, env=env).stdout.decode("utf-8", "replace")
    assert f"{G.MAX_CLAIM_FILES + 1} unfinished `devmem begin` snapshots" in out


# ---- devmem import: the stream file, the digest's lists and the prompt are bounded ----

def _events(project, tools: int, pad: int = 0) -> list[dict]:
    blocks = [{"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {"file_path": str(project / "greet.py")}}
              for i in range(tools)]
    return ([{"type": "system", "subtype": "init", "session_id": "5e55107d-0000-4000-8000-0000000000aa", "model": "m"}]
            + [{"type": "assistant", "message": {"content": [{"type": "text", "text": f"step {i} " + "w" * pad}]}}
               for i in range(200)]
            + [{"type": "assistant", "message": {"content": blocks}},
               {"type": "result", "result": "all done", "session_id": "5e55107d-0000-4000-8000-0000000000aa",
                "usage": {"input_tokens": 5, "output_tokens": 6}, "num_turns": 2}])


def _write(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def test_a_large_stream_file_is_read_as_its_head_and_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STREAM_READ_HEAD", 20_000)
    monkeypatch.setattr(C, "STREAM_READ_TAIL", 20_000)
    project = make_project(tmp_path)
    stream = _write(tmp_path / "s.jsonl", _events(project, 3, pad=1000))     # about 230 KB
    read = []
    real = C._region_lines
    monkeypatch.setattr(C, "_region_lines", lambda f, n, rest, *a: read.append(n) or real(f, n, rest, *a))
    info: dict = {}
    rec = _rec(C.record_session(project, stream, "x", "2026-11-15T00:00:00Z", store=project / ".devmemory",
                                info=info))
    assert sum(read) <= 40_000
    t = rec["turns"][0]
    assert t["stream_left_out"]["bytes"] > 150_000 and info["stream_left_out"] == t["stream_left_out"]
    assert rec["result_text"] == "all done" and rec["usage"]["input_tokens"] == 5        # the tail was read
    assert rec["session_id"] == "5e55107d-0000-4000-8000-0000000000aa"                 # and the head
    raw = _raw(next((project / ".devmemory" / "sessions").glob("*.meta.json"))).read_text(encoding="utf-8")
    assert "bytes from its middle" in raw and len(raw) < 60_000
    assert "read only in part" in C.import_note(info)


def test_stream_lines_and_digest_lists_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_STREAM_LINES", 50)
    monkeypatch.setattr(C, "MAX_STREAM_ITEMS", 5)
    project = make_project(tmp_path)
    stream = _write(tmp_path / "s.jsonl", _events(project, 20))
    meta = C.record_session(project, stream, "p" * 300_000, "2026-11-15T00:00:00Z", store=project / ".devmemory")
    rec = _rec(meta)
    t = rec["turns"][0]
    assert len(_raw(meta).read_text(encoding="utf-8").splitlines()) == 51
    assert t["stream_left_out"] == {"lines": 153, "read_bytes": stream.stat().st_size}
    assert len(t["assistant_text"]) == 5 and t["assistant_text_left_out"] > 0
    assert len(t["tool_calls"]) == 5 and t["tool_calls_left_out"] == 15
    assert t["prompt_truncated"] is True and len(t["prompt"]) <= C.PROMPT_CAP


def test_stream_fields_that_are_not_short_strings_are_dropped(tmp_path):
    project = make_project(tmp_path)
    ev = _events(project, 1)
    ev[0]["model"] = "m" * 10_000
    ev[-1]["usage"] = {"input_tokens": "lots", "output_tokens": 6}
    ev.insert(1, [1, 2])                                             # a JSON line that is not an event
    ev.insert(1, {"type": "assistant", "message": "not an object"})
    rec = _rec(C.record_session(project, _write(tmp_path / "s.jsonl", ev), "x", "2026-11-15T00:00:00Z",
                                store=project / ".devmemory"))
    assert rec["model"] is None and rec["usage"]["input_tokens"] is None and rec["usage"]["output_tokens"] == 6


def test_an_ordinary_stream_reads_as_before(tmp_path):
    project = make_project(tmp_path)
    stream = _write(tmp_path / "s.jsonl", _events(project, 3))
    old = [ln.rstrip("\r") for ln in stream.read_text(encoding="utf-8", errors="replace").split("\n")][:-1]
    cut: dict = {}
    assert C._load_lines(stream, cut) == old and cut == {}
    rec = _rec(C.record_session(project, stream, "x", "2026-11-15T00:00:00Z", store=project / ".devmemory"))
    assert not [k for k in rec["turns"][0] if k.endswith("_left_out") or k == "prompt_truncated"]


# ---- MCP: request size, numbers and string arguments ----

def _call(server, name, args):
    out = respond(server, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}}).encode("utf-8"))
    return out["result"]["isError"], out["result"]["content"][0]["text"]


@pytest.mark.parametrize("name,args,why", [
    ("get_context_pack", {"task": "t" * 100_001}, "task is 100,001 characters"),
    ("get_context_pack", {"task": "x", "budget": 10**9}, "budget must be a whole number from 100 to 20000"),
    ("get_context_pack", {"task": "x", "budget": 0}, "budget must be"),
    ("get_context_pack", {"task": "x", "budget": True}, "budget must be"),
    ("get_context_pack", {"task": "x", "budget": "800"}, "budget must be"),
    ("get_context_pack", {"task": ["x"]}, "task must be a string"),
    ("read_session", {"id": "a" * 65}, "id is 65 characters"),
    ("end_session", {"prompt": "x" * 100_001}, "prompt is 100,001 characters"),
    ("end_session", {"prompt": "x", "summary": "s" * 100_001}, "summary is"),
    ("end_session", {"prompt": "x", "client": "c" * 65}, "client is 65"),
    ("end_session", {"prompt": "x", "pending_id": 5}, "pending_id must be a string"),
])
def test_an_argument_past_its_bound_is_a_clear_tool_error(tmp_path, name, args, why):
    project = make_project(tmp_path)
    is_error, text = _call(Server(project, project / ".devmemory"), name, args)
    assert is_error and why in text


def test_arguments_inside_their_bounds_still_work(tmp_path):
    project = make_project(tmp_path)
    server = Server(project, project / ".devmemory")
    assert _call(server, "get_context_pack", {"task": "greet name", "budget": 800.0})[0] is False
    assert _call(server, "begin_session", {})[0] is False
    assert _call(server, "end_session", {"prompt": "x" * 100_000})[0] is False


def test_a_huge_integer_is_a_parse_error_at_once(tmp_path):
    project = make_project(tmp_path)
    raw = b'{"jsonrpc": "2.0", "id": ' + b"9" * 1_000_000 + b', "method": "ping"}'
    limit = getattr(sys, "get_int_max_str_digits", lambda: None)()
    if limit is not None:
        sys.set_int_max_str_digits(0)        # as a Python without the limit
    t0 = time.monotonic()
    try:
        out = respond(Server(project, project / ".devmemory"), raw)
    finally:
        if limit is not None:
            sys.set_int_max_str_digits(limit)
    # a Python without int_max_str_digits (before 3.11 / 3.10.7) took minutes to convert it; devmem refuses it itself
    assert out["error"]["code"] == -32700 and time.monotonic() - t0 < 2


@pytest.mark.parametrize("number, parse_error", [("1" + "0" * 99, False), ("1" + "0" * 100, True),
                                                 ("-" + "1" * 99, False), ("-" + "1" * 100, True)])
def test_an_integer_of_more_than_100_characters_anywhere_is_a_parse_error_with_no_id(tmp_path, number, parse_error):
    """SECURITY.md: past MAX_INT_DIGITS characters (a minus sign counts) the request is not parsed at all, so the
    reply cannot carry its id; up to it the budget is checked as an argument (a tool error)."""
    project = make_project(tmp_path)
    raw = ('{"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "get_context_pack", '
           '"arguments": {"task": "x", "budget": ' + number + '}}}').encode()
    out = respond(Server(project, project / ".devmemory"), raw)
    if parse_error:
        assert out["id"] is None and out["error"]["code"] == -32700
    else:
        assert out["id"] == 7 and out["result"]["isError"] and "budget" in out["result"]["content"][0]["text"]


def test_a_request_line_past_the_limit_is_answered_and_the_server_goes_on(tmp_path):
    project = make_project(tmp_path)
    big = b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "pad": "' + b"x" * (MAX_LINE_BYTES + 10) + b'"}\n'
    ping = b'{"jsonrpc": "2.0", "id": 2, "method": "ping"}\n'
    env = cli_env(DEVMEM_USER_DATA=os.environ["DEVMEM_USER_DATA"])
    r = subprocess.run([PY, "-m", "devmem", "mcp", "--project", str(project)], input=big + ping, capture_output=True,
                       env=env, timeout=120)
    replies = [json.loads(x) for x in r.stdout.decode("utf-8").splitlines()]
    assert replies[0]["error"]["code"] == -32700 and str(MAX_LINE_BYTES) in replies[0]["error"]["message"]
    assert replies[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_lines_never_holds_more_than_the_limit():
    import io
    held = []

    class Stream(io.BytesIO):
        def readline(self, n=-1):
            b = super().readline(n)
            held.append(len(b))
            return b
    got = list(_lines(Stream(b"y" * 5000 + b"\nok\n"), limit=100))
    assert got == [(b"", True), (b"ok\n", False)] and max(held) <= 101


# ---- CLI numbers ----

@pytest.mark.parametrize("args", [["pack", "x", "--budget", "99999999"], ["pack", "x", "--budget", "1e9"],
                                  ["run", "x", "--max-turns", "0"]])
def test_cli_numbers_out_of_range_are_refused(tmp_path, args):
    project = make_project(tmp_path)
    r = run_cli([*args, "--project", str(project)], cwd=project,
                env=cli_env(DEVMEM_USER_DATA=os.environ["DEVMEM_USER_DATA"]))
    assert r.returncode == 2 and (b"must be from" in r.stderr or b"not a whole number" in r.stderr), r.stderr
