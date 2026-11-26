"""Third review pass: a modification time outside the session is no evidence of creation; `devmem run` writes and reads
its stream within the import bounds; `import` holds no more lines than it keeps, never stops on a malformed line, does
not call a file deleted because it is absent at import time, and looks at a bounded number of tool calls; a begin file
nested too deep is skipped and named."""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
import time
import tracemalloc

import pytest
from hooks_helpers import make_project, run_git

from devmem import capture as C
from devmem import generic as G
from devmem import proc
from devmem.redact import Redactor

PY = sys.executable
git_needed = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _rec(meta):
    return json.loads(meta.read_text(encoding="utf-8"))


def _raw(meta):
    return meta.with_name(meta.name.replace(".meta.json", ".jsonl")).read_text(encoding="utf-8")


# ---- A: a future modification time is no evidence either ----

@git_needed
@pytest.mark.parametrize("git", [True, False])
@pytest.mark.parametrize("offset", [24 * 3600, -3600])
def test_a_file_dated_outside_the_session_is_not_created(tmp_path, git, offset):
    project = make_project(tmp_path, git=git)
    store = project / ".devmemory"
    (project / ".gitignore").write_text("local.cfg\n", encoding="utf-8")
    (project / "local.cfg").write_text("keep = 2\n", encoding="utf-8")
    if git:
        run_git(project, "add", ".gitignore")
        run_git(project, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "ignore")
    when = time.time() + offset
    os.utime(project / "local.cfg", (when, when))        # a day ahead (an archive from a fast clock) or an hour old
    G.begin(project, store)
    (project / ".gitignore").write_text("\n", encoding="utf-8")
    rec = _rec(G.end(project, "x", store=store))
    assert "local.cfg" not in rec["files_edited"] and rec["turns"][0]["files_not_compared"] == ["local.cfg"]


# ---- B: `devmem run`'s stream file and its reading ----

def test_run_to_file_keeps_a_head_and_a_tail_of_the_stream(tmp_path):
    code = ("import sys\nw = sys.stdout.buffer\nw.write(b'{\"type\": \"system\"}\\n')\n"
            "for i in range(100000): w.write(b'ab\\n')\nw.write(b'{\"type\": \"result\", \"result\": \"done\"}\\n')")
    out = tmp_path / "s.jsonl"
    r = proc.run_to_file([PY, "-c", code], out, head=2000, tail=2000, gap_line=lambda n: C._gap_line(0, n))
    data = out.read_bytes()
    total = len(b'{"type": "system"}\n') + 300_000 + len(b'{"type": "result", "result": "done"}\n')
    gap = C._gap_line(0, r.stdout_dropped).encode() + b"\n"
    assert gap in data and len(data) <= 4000 and r.stdout_dropped == total - (len(data) - len(gap))
    assert data.startswith(b'{"type": "system"}\nab\n') and data.endswith(b'{"type": "result", "result": "done"}\n')
    kept = data.replace(gap, b"").split(b"\n")
    assert set(kept[1:-2]) == {b"ab"} and kept[-1] == b""                # whole lines on both sides of the gap


def test_run_to_file_writes_an_ordinary_stream_as_the_program_wrote_it(tmp_path):
    code = "import sys; sys.stdout.buffer.write(b'a\\r\\nb\\n\\xff\\xfe\\n' * 1000)"
    out = tmp_path / "s.jsonl"
    r = proc.run_to_file([PY, "-c", code], out)
    assert r.stdout_dropped == 0 and out.read_bytes() == b"a\r\nb\n\xff\xfe\n" * 1000


def test_run_claude_reads_its_stream_within_the_bounds(tmp_path, monkeypatch):
    from devmem import claude_cli
    monkeypatch.setattr(proc, "STREAM_FILE_HEAD", 5000)
    monkeypatch.setattr(proc, "STREAM_FILE_TAIL", 5000)
    code = ("import sys, json\nw = sys.stdout\nw.write(json.dumps({'type': 'system', 'subtype': 'init', "
            "'session_id': 's1', 'model': 'm'}) + '\\n')\nfor i in range(50000): w.write('ab\\n')\n"
            "w.write(json.dumps({'type': 'result', 'result': 'done', 'session_id': 's1'}) + '\\n')")
    monkeypatch.setattr(claude_cli, "build_command", lambda *a, **k: [PY, "-c", code])
    seen = []
    real = C._load_lines
    monkeypatch.setattr(C, "_load_lines", lambda p, *a: seen.append(p) or real(p, *a))
    out = tmp_path / "s.jsonl"
    m = claude_cli.run_claude("x", tmp_path, out)
    assert out.stat().st_size < 10_200 and m["stream_bytes_left_out"] > 140_000
    assert m["result_text"] == "done" and m["session_id"] == "s1" and seen == [out]


# ---- C: the line cap applies while reading ----

def _peak(fn) -> int:
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("line", [b"ab\n", b"\n", b"\r\n"])
def test_tiny_and_empty_lines_are_not_all_held(tmp_path, monkeypatch, line):
    monkeypatch.setattr(C, "STREAM_READ_HEAD", 4 * 1024 * 1024)
    monkeypatch.setattr(C, "STREAM_READ_TAIL", 4 * 1024 * 1024)
    monkeypatch.setattr(C, "MAX_STREAM_LINES", 2000)
    path = tmp_path / "s.jsonl"
    path.write_bytes(line * (7 * 1024 * 1024 // len(line)))    # under the byte cap: read whole, 2.3-7 M lines
    cut: dict = {}
    peak = _peak(lambda: C._load_lines(path, cut))
    # old: every line in a list first, about 55 bytes each (120-390 MB here); now about two chunks and the kept lines
    assert peak < 12 * 1024 * 1024, peak
    assert cut["lines"] == 7 * 1024 * 1024 // len(line) - 2000


def test_the_reader_splits_as_read_text_did(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "_READ_CHUNK", 7)          # every chunk edge: CR LF, a UTF-8 sequence, a long line
    rnd = random.Random(3)
    alphabet = [b"a", b"\n", b"\r", b"\r\n", "\u00e9".encode(), "\u2028".encode(), b"\xff", b"\xe2\x82", b"{}"]
    for case in range(300):
        data = b"".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 60)))
        path = tmp_path / f"s{case}.jsonl"
        path.write_bytes(data)
        old = [ln.rstrip("\r") for ln in path.read_text(encoding="utf-8", errors="replace").split("\n")]
        if old and old[-1] == "":
            old.pop()
        assert C._load_lines(path) == old, data


# ---- D: a malformed stream line is stored as a stub, never fatal ----

_GOOD = [
    {"type": "system", "subtype": "init", "session_id": "s", "model": "m"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"},
                                                  {"type": "tool_use", "id": "t1", "name": "Read",
                                                   "input": {"file_path": "greet.py"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x",
                                              "is_error": True}]}, "tool_use_result": {"file": {"content": "x"}}},
    {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {"w": {"utilization": 1}}}},
    {"type": "result", "result": "done", "session_id": "s", "usage": {"input_tokens": 1}, "num_turns": 1},
]
_WEIRD = [None, True, 1e308, -1, 10**30, "", "\ud800", [], {}, [[["deep"]]], {"a": {"b": {"c": 1}}}, ["x"], {"k": "v"},
          "x" * 5000]


def _mutate(rnd, e):
    """One field somewhere in a copy of `e` replaced by a value of another type."""
    e = json.loads(json.dumps(e))
    path, node = [], e
    while isinstance(node, (dict, list)) and node and rnd.random() < 0.8:
        k = rnd.choice(list(node)) if isinstance(node, dict) else rnd.randrange(len(node))
        path.append((node, k))
        node = node[k]
    if path:
        parent, k = path[-1]
        parent[k] = rnd.choice(_WEIRD)
    return e


def test_mutated_stream_lines_never_stop_an_import(tmp_path):
    project = make_project(tmp_path)
    rnd = random.Random(11)
    lines = []
    for _ in range(2500):
        e = _mutate(rnd, rnd.choice(_GOOD))
        line = json.dumps(e, ensure_ascii=rnd.random() < 0.5)
        if rnd.random() < 0.05:
            line = line.replace('"type"', '"type": 1, "type"', 1)                 # a duplicate key
        lines.append(line)
    lines += ["[" * 900 + "]" * 900, json.dumps({"type": "assistant", "message": {"content": [[[[[[]]]]]] * 3}}),
              "[" * 100_000, '{"type": "result", "result": NaN, "usage": {"input_tokens": Infinity}}',
              json.dumps({"type": "assistant", "deep": json.loads("[" * 500 + "]" * 500)}),
              json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ["a"]}]}}),
              json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": {"a": 1},
                                                                        "name": ["Read"], "input": []}]}})]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", errors="surrogatepass")
    info: dict = {}
    meta = C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory", info=info)
    raw = _raw(meta).splitlines()
    assert len(raw) == len(lines)                                     # line for line: one stub per unreadable line
    assert all(json.loads(r) is not None for r in raw if r.startswith("{"))


def test_a_deep_begin_file_is_skipped_and_named(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    good = G.begin(project, store)
    os.utime(good, (1_000_000_000, 1_000_000_000))
    (store / "pending" / "deep.json").write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    G.begin(project, store)                                            # old: RecursionError in secure_pending
    assert G.end(project, "x", store=store, pending_id=good.stem).exists()
    with pytest.raises(RuntimeError, match="deep.json cannot be used: it is not valid JSON"):
        G.end(project, "x", store=store, pending_id="deep")


# ---- E: absence at import time is not a deletion ----

@git_needed
def test_import_lists_absent_files_without_calling_them_deleted(tmp_path):
    project = make_project(tmp_path)
    ev = [_GOOD[0], {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {"file_path": str(project / f)}}
        for i, f in enumerate(("greet.py", "elsewhere_only.py", "later_removed.py"))]}}, _GOOD[-1]]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in ev) + "\n", encoding="utf-8")
    rec = _rec(C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory"))
    t = rec["turns"][0]
    assert rec["files_deleted"] == [] and not [c for c in rec["tool_calls"] if c["tool"] == "Delete"]
    assert t["files_absent_at_capture"] == ["elsewhere_only.py", "later_removed.py"]
    # `devmem run` captures as the session ends: there the same absence is recorded as the session's deletion
    rec = _rec(C.record_session(project, path, "x", "2026-11-16T00:00:01Z", store=project / ".devmemory",
                                at_session_end=True))
    assert rec["files_deleted"] == ["elsewhere_only.py", "later_removed.py"]
    assert "files_absent_at_capture" not in rec["turns"][0]


@git_needed
def test_observed_deletions_stay_within_the_tool_call_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_STREAM_ITEMS", 5)
    project = make_project(tmp_path)
    ev = [_GOOD[0], {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {"file_path": str(project / f"gone{i}.py")}}
        for i in range(8)]}}, _GOOD[-1]]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in ev) + "\n", encoding="utf-8")
    rec = _rec(C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory",
                                at_session_end=True))
    assert len(rec["tool_calls"]) == 5 and len(rec["files_deleted"]) == 5


# ---- F: the tool calls devmem looks at for secret files ----

def test_tool_calls_past_the_cap_are_counted_and_not_stored(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    checked = []
    real = Redactor.tool_reads_secret_file
    monkeypatch.setattr(Redactor, "tool_reads_secret_file", lambda *a, **k: checked.append(1) or real(*a, **k))
    n = C.MAX_LINE_TOOL_USES + 500
    ev = [_GOOD[0],
          {"type": "assistant", "message": {"content": [
              {"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {"file_path": f"f{i}.txt"}}
              for i in range(n)]}},
          {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{n - 1}",
                                                    "content": "LATE-RESULT-TEXT"}]}}, _GOOD[-1]]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in ev) + "\n", encoding="utf-8")
    info: dict = {}
    t0 = time.monotonic()
    meta = C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory", info=info)
    assert time.monotonic() - t0 < 60
    assert len(checked) <= C.MAX_LINE_TOOL_USES and info["tool_calls_left_out"] == 500
    assert "LATE-RESULT-TEXT" not in _raw(meta) and C.LEFT_OUT_TOOL in _raw(meta)
    assert "tool calls not looked at" in C.import_note(info)


# ---- fourth pass: what the third one left open ----

def _fake_claude(monkeypatch, tmp_path, lines: list[str]):
    """build_command replaced by a program that prints `lines` (one per line) on stdout."""
    from devmem import claude_cli
    data = tmp_path / "fake_stream.bin"
    data.write_bytes(b"".join(ln.encode("utf-8") + b"\n" for ln in lines))
    src = "import sys\nsys.stdout.buffer.write(open(sys.argv[1], 'rb').read())\n"
    monkeypatch.setattr(claude_cli, "build_command", lambda *a, **k: [PY, "-c", src, str(data)])


def _sized(monkeypatch, n: int) -> None:
    """devmem run's stream file and capture's reading at the same (small) sizes, as they are at full size."""
    for mod, names in ((proc, ("STREAM_FILE_HEAD", "STREAM_FILE_TAIL")), (C, ("STREAM_READ_HEAD", "STREAM_READ_TAIL"))):
        for name in names:
            monkeypatch.setattr(mod, name, n)


def test_run_stream_file_fits_what_capture_reads_and_holds_whole_lines(tmp_path, monkeypatch):
    from devmem import claude_cli
    _sized(monkeypatch, 20_000)
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": "s1", "model": "m"})]
    lines += [json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"{i:04d}" * 700}]}})
              for i in range(60)]                                         # 60 lines of about 2,850 bytes
    lines.append(json.dumps({"type": "result", "result": "done", "session_id": "s1"}))
    _fake_claude(monkeypatch, tmp_path, lines)
    out = tmp_path / "s.jsonl"
    m = claude_cli.run_claude("x", tmp_path, out)
    data = out.read_bytes()
    # old: 20,000 + 20,000 bytes and the gap line, cut inside a line at both edges: capture read neither edge, nor the gap
    assert len(data) <= 40_000, len(data)
    got = data.decode("utf-8").splitlines()
    assert all(isinstance(json.loads(ln), dict) for ln in got)            # whole lines only
    assert got[0] == lines[0] and got[-1] == lines[-1]
    assert len([ln for ln in got if ln not in lines]) == 1                # the gap line; every other line whole
    gap = [json.loads(ln) for ln in got if '"devmem_omitted"' in ln]
    assert len(gap) == 1 and f"{m['stream_bytes_left_out']:,} bytes" in gap[0]["reason"]
    assert m["stream_bytes_left_out"] == sum(len(ln) + 1 for ln in lines) - (len(data) - len(json.dumps(gap[0])) - 1)


@pytest.mark.parametrize("cut", [600, 2500, 5000])
def test_a_line_cut_by_runs_stream_file_is_never_stored(tmp_path, monkeypatch, cut):
    """A Read of .env whose result line is cut where the stream file's tail starts: half a line is no event, so the
    secret-file rule cannot see it; that half must not reach the record."""
    _sized(monkeypatch, 20_000)
    project = make_project(tmp_path)
    secret = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                       "content": " ".join(f"S3CRET{i:04d}" for i in range(500))}]}}
    pad = {"type": "assistant", "message": {"content": [{"type": "text", "text": "ab" * 990}]}}
    result = {"type": "result", "result": "done", "session_id": "s1"}
    lines = [json.dumps(_GOOD[0]),
             json.dumps({"type": "assistant", "message": {"content": [
                 {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": str(project / ".env")}}]}})]
    lines += [json.dumps(pad)] * 15 + [json.dumps(secret)]
    after = 20_000 - cut - len(json.dumps(result)) - 1                     # the tail starts `cut` bytes into that line
    n_pad = after // (len(json.dumps(pad)) + 1)
    lines += [json.dumps(pad)] * n_pad
    lines.append(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "z" * (after - n_pad * (len(json.dumps(pad)) + 1) - 60)}]}}))
    lines.append(json.dumps(result))
    _fake_claude(monkeypatch, tmp_path, lines)
    info: dict = {}
    meta = C.run_and_capture(project, "x", isolated=False, store=project / ".devmemory", info=info)
    store_text = "".join(f.read_text(encoding="utf-8", errors="replace")
                         for f in (project / ".devmemory").rglob("*") if f.is_file())
    assert "S3CRET" not in store_text
    assert _rec(meta)["result_text"] == "done"


def test_run_records_how_much_of_the_stream_it_did_not_write(tmp_path, monkeypatch):
    _sized(monkeypatch, 20_000)
    project = make_project(tmp_path)
    lines = [json.dumps(_GOOD[0])] + ["ab"] * 100_000 + [json.dumps(_GOOD[-1])]
    _fake_claude(monkeypatch, tmp_path, lines)
    info: dict = {}
    meta = C.run_and_capture(project, "x", isolated=False, store=project / ".devmemory", info=info)
    t = _rec(meta)["turns"][0]
    # old: the gap line lay outside what capture reads, so the record said about 300 bytes were left out
    assert t["stream_left_out"]["not_written"] > 250_000
    assert f"about {t['stream_left_out']['not_written']:,} bytes" in _raw(meta)
    assert "not written" in C.import_note(info)


def test_run_to_file_reads_the_program_to_its_end_when_the_file_cannot_be_written(tmp_path):
    code = "import sys; sys.stdout.buffer.write(b'x' * (8 * 1024 * 1024)); print('done', file=sys.stderr)"
    t0 = time.monotonic()
    r = proc.run_to_file([PY, "-c", code], tmp_path, timeout=60)          # a folder: open() fails
    assert time.monotonic() - t0 < 30 and r.returncode == 0 and "done" in r.stderr   # old: the program blocked


def test_a_tool_call_without_an_id_is_checked_too(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.setattr(C, "MAX_STREAM_ITEMS", 2)
    write = {"type": "tool_use", "name": "Write", "input": {"file_path": str(project / ".env"),
                                                             "content": "plain words one"}}
    late = {"type": "tool_use", "name": "Bash", "input": {"command": "echo plain words two"}}
    ev = [_GOOD[0], {"type": "assistant", "message": {"content": [write, {**write, "id": ""}, late]}},
          {"type": "user", "message": {"content": [{"type": "tool_result", "content": "plain words three"}]},
           "tool_use_result": {"stdout": "plain words four"}},
          {"type": "user", "message": {"content": "text"}, "tool_use_result": {"stdout": "plain words five"}},
          _GOOD[-1]]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in ev) + "\n", encoding="utf-8")
    info: dict = {}
    meta = C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory", info=info)
    raw = _raw(meta)
    # old: a call without an id was never looked at: its input was stored, past the cap too, and so was the output of
    # a result no looked-at call accounts for
    assert "plain words" not in raw, raw
    assert raw.count(C.OMITTED_SECRET_FILE) >= 2 and C.LEFT_OUT_TOOL in raw


def test_the_digest_looks_at_as_many_calls_of_one_line_as_the_raw_record(tmp_path, monkeypatch):
    from devmem import sessions as S
    project = make_project(tmp_path)
    seen = []
    real = S.tool_entries
    monkeypatch.setattr(S, "tool_entries", lambda *a, **k: seen.append(1) or real(*a, **k))
    n = C.MAX_LINE_TOOL_USES + 300
    ev = [_GOOD[0], {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {"file_path": f"f{i}.txt"}} for i in range(n)]}},
        _GOOD[-1]]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in ev) + "\n", encoding="utf-8")
    info: dict = {}
    rec = _rec(C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory", info=info))
    assert len(seen) == C.MAX_LINE_TOOL_USES and len(rec["turns"][0]["tool_calls"]) == C.MAX_LINE_TOOL_USES
    assert rec["turns"][0]["tool_calls_left_out"] == 300


def test_one_line_of_many_calls_costs_what_the_calls_looked_at_cost(tmp_path, monkeypatch):
    """The review's 400,000-call line took 320 s after the third pass: every call left out still cost a path made
    relative (parse_stream) and three strings redacted (the marker in its block). Now nothing is done per call left
    out but counting it, and one block per line stands for them."""
    from devmem import sessions as S
    project = make_project(tmp_path)
    paths = []
    real = S.rel_path
    monkeypatch.setattr(S, "rel_path", lambda *a, **k: paths.append(1) or real(*a, **k))
    n = C.MAX_LINE_TOOL_USES + 5000
    ev = [_GOOD[0],
          {"type": "assistant", "message": {"content": [
              {"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {"file_path": f"f{i}.txt"}}
              for i in range(n)]}},
          {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "same id"}
                                                   for _ in range(n)]}, "tool_use_result": {"stdout": "x"}},
          _GOOD[-1]]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in ev) + "\n", encoding="utf-8")
    info: dict = {}
    meta = C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory", info=info)
    raw = [json.loads(ln) for ln in _raw(meta).splitlines()]
    uses, results = raw[1]["message"]["content"], raw[2]["message"]["content"]
    assert len(uses) == C.MAX_LINE_TOOL_USES + 1 and uses[-1] == {"type": "devmem_omitted", "reason": C.LEFT_OUT_TOOL,
                                                                    "tool_uses": 5000}
    assert len(results) == C.MAX_LINE_TOOL_USES + 1 and results[-1]["tool_results"] == 5000
    assert raw[2]["tool_use_result"] == C.LEFT_OUT_TOOL
    assert len(paths) <= C.MAX_LINE_TOOL_USES + 10 and info["tool_calls_left_out"] == 5000
