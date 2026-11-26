"""`devmem import`/`run`: the work of one import is bounded for every file inside the read bounds (fifth review,
blocker 4), and what a bound leaves out is counted and said (items 5, 6, 7).

Each line within the text budget is counted before json parses it (capture._values, capture._line_keys): a line of
more than MAX_LINE_VALUES values or MAX_LINE_KEYS distinct keys, and every line once STREAM_VALUE_BUDGET values or
STREAM_KEY_BUDGET distinct keys of the stream are used, is a stub counted by its reason and never parsed. A line past
the text budget is parsed only for the metrics, while STREAM_PARSE_BUDGET values last. A line too long to parse has
its first 64 KB read for its event type, tool calls and paths (capture._recover)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import capture as C

ROOT = Path(__file__).resolve().parent.parent
SECRET = "QX7SECRETPLAIN"
ENV_TEXT = f"SERVICE_HOST=internal-db-zq81.corp.example\nNOTE={SECRET}\n"
INIT = {"type": "system", "subtype": "init", "session_id": "11111111-2222-3333-4444-555555555555", "model": "m"}
END = {"type": "result", "result": "done", "session_id": "11111111-2222-3333-4444-555555555555"}


def _write(path, events):
    path.write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def _record(tmp_path, project, events, info=None):
    info = {} if info is None else info
    meta = C.record_session(project, _write(tmp_path / "s.jsonl", events), "x", "2026-11-16T00:00:00Z",
                            store=project / ".devmemory", info=info)
    raw = meta.with_name(meta.name.replace(".meta.json", ".jsonl")).read_text(encoding="utf-8").splitlines()
    return json.loads(meta.read_text(encoding="utf-8")), raw, info


def _call_line(i: int, payload: str) -> str:
    return ('{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t%d",'
            '"name":"Mcp","input":{"x":%s}}]}}' % (i, payload))


def _parsed(monkeypatch) -> list[int]:
    seen: list[int] = []
    real = json.loads
    monkeypatch.setattr(C.json, "loads", lambda s, *a, **k: seen.append(len(s)) or real(s, *a, **k))
    return seen


# ---------------------------------------------------------------- the per-line and per-stream counts (item 4)

def test_values_and_keys_are_counted_as_json_would_give_them():
    for text in ['{"a":[1,2,{"b":"c,:[{"}],"d":null}', '{"k":"a\\"b:c","l":[[],{}]}', '{}', '{"a":""}']:
        def count(x):
            if isinstance(x, dict):
                return 1 + sum(1 + count(v) for v in x.values())
            if isinstance(x, list):
                return 1 + sum(count(v) for v in x)
            return 1
        empty = text.count("[]") + text.count("{}")          # an empty list or object counts twice: an upper bound
        assert C._values(text) == count(json.loads(text)) + empty, text
    assert C._line_keys('{"a":1,"b":{"a":2,"c d":"e:"}}') == {'"a"', '"b"', '"c d"'}


@pytest.mark.parametrize("over", [False, True], ids=["at_the_cap", "just_over"])
def test_a_line_over_the_value_cap_is_a_counted_stub_never_parsed(tmp_path, monkeypatch, over):
    monkeypatch.setattr(C, "MAX_LINE_VALUES", 1_000)
    project = make_project(tmp_path)
    payload = "[" + ",".join(["0"] * 900) + "]"
    line = _call_line(1, payload)
    pad = C.MAX_LINE_VALUES - C._values(line) + (1 if over else 0)
    line = _call_line(1, "[" + ",".join(["0"] * (900 + pad)) + "]")
    assert C._values(line) == C.MAX_LINE_VALUES + (1 if over else 0)
    seen = _parsed(monkeypatch)
    rec, raw, info = _record(tmp_path, project, [INIT, line, END])
    assert (len(line) in seen) is not over
    if over:
        assert info["stream_lines_left_out"] == {"too_many_values": 1} and "MAX_LINE_VALUES" in raw[1]
        assert "1 more values than devmem takes apart on one line" in C.import_note(info)
    else:
        assert "stream_lines_left_out" not in info


def test_the_stream_value_budget_ends_taking_lines_apart(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STREAM_VALUE_BUDGET", 5_000)
    project = make_project(tmp_path)
    lines = [_call_line(i, "[" + ",".join(["0"] * (1_900 + i)) + "]") for i in range(4)]     # about 1,920 values each
    seen = _parsed(monkeypatch)
    rec, raw, info = _record(tmp_path, project, [INIT, *lines, END])
    assert info["stream_lines_left_out"] == {"value_budget": 2}
    assert len(lines[2]) not in seen and len(lines[3]) not in seen and seen.count(len(lines[1])) == 2
    assert "STREAM_VALUE_BUDGET" in raw[3] and "STREAM_VALUE_BUDGET" in raw[4]
    assert rec["result_text"] == "done"                  # a line of few values still is taken apart


def test_distinct_keys_are_capped_per_line_and_per_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_LINE_KEYS", 100)
    monkeypatch.setattr(C, "STREAM_KEY_BUDGET", 150)
    project = make_project(tmp_path)

    def keys(i, n):
        return _call_line(i, "{" + ",".join(f'"k{i}_{j}":0' for j in range(n)) + "}")
    lines = [keys(1, 120), keys(2, 80), keys(3, 81), keys(2, 80) + " "]   # over the line cap; ok; over the budget; seen
    seen = _parsed(monkeypatch)
    rec, raw, info = _record(tmp_path, project, [INIT, *lines, END])
    assert info["stream_lines_left_out"] == {"too_many_keys": 1, "value_budget": 1}
    assert len(lines[0]) not in seen and len(lines[2]) not in seen and seen.count(len(lines[1])) == 2
    assert "MAX_LINE_KEYS" in raw[1] and "STREAM_KEY_BUDGET" in raw[3]
    assert json.loads(raw[4])["type"] == "assistant"       # the keys it holds were all seen before: no new key


def test_past_the_text_budget_lines_are_parsed_for_metrics_while_the_parse_budget_lasts(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STREAM_TEXT_BUDGET", 1_000)
    monkeypatch.setattr(C, "STREAM_PARSE_BUDGET", 3_000)
    project = make_project(tmp_path)
    text = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "w" * 2_000}]}}
    lines = [_call_line(i, "[" + ",".join(["0"] * (1_900 + i)) + "]") for i in range(3)]
    seen = _parsed(monkeypatch)
    rec, raw, info = _record(tmp_path, project, [INIT, text, *lines, END])
    assert info["stream_lines_left_out"] == {"text_budget": 4}         # the three and the result event
    assert seen.count(len(lines[0])) == 1 and seen.count(len(lines[2])) == 0   # 1,920 + 1,920 > 3,000
    assert info["tool_calls_left_out"] == 1                             # the call on the line it parsed
    assert rec["result_text"] is None or rec["result_text"] == "done"


# ---------------------------------------------------------------- a line too long to parse (item 5)

def _big_write(project, path_first=True, file="big.py"):
    content = "x = 1\n" * 20_000                                          # 120 KB: over the patched limit
    inp = {"file_path": str(project / file), "content": content} if path_first else \
        {"content": content, "file_path": str(project / file)}
    return [{"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Writing the data file."},
        {"type": "tool_use", "id": "w1", "name": "Write", "input": inp}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "w1", "content": f"File created: {file}\n{ENV_TEXT}"}]}}]


def test_a_write_on_a_line_too_long_to_parse_is_recorded_without_its_content(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_LINE_PARSE_CHARS", 50_000)
    project = make_project(tmp_path)
    seen = _parsed(monkeypatch)
    rec, raw, info = _record(tmp_path, project, [INIT, *_big_write(project), END])
    assert rec["files_edited"] == ["big.py"] and "Writing the data file." in [t["text"] for t in rec["assistant_text"]]
    entry = rec["tool_calls"][0]
    assert entry["tool"] == "Write" and entry["truncated"] is True and "MAX_LINE_PARSE_CHARS" in entry["input_left_out"]
    assert "new" not in entry and not [n for n in seen if n > 50_000]
    assert info["stream_lines_left_out"] == {"too_long": 1} and info["tool_calls_left_out"] == 1
    assert json.loads(raw[1])["tool_calls_found"] == 1
    assert SECRET not in "".join(raw) and C.LEFT_OUT_TOOL in raw[2]     # its result: the call was not looked at
    assert "tool calls not looked at" in C.import_note(info)


@pytest.mark.parametrize("path_first", [True, False], ids=["path_first", "content_first"])
def test_a_secret_file_write_on_a_long_line_keeps_nothing_of_it(tmp_path, monkeypatch, path_first):
    monkeypatch.setattr(C, "MAX_LINE_PARSE_CHARS", 50_000)
    project = make_project(tmp_path)
    events = _big_write(project, path_first, ".env")
    events[0]["message"]["content"][1]["input"]["content"] = ENV_TEXT * 4000
    rec, raw, info = _record(tmp_path, project, [INIT, *events, END])
    store = project / ".devmemory"
    assert not [f for f in store.rglob("*") if f.is_file() and SECRET.encode() in f.read_bytes()]
    if path_first:
        assert rec["tool_calls"][0].get("secret_file") is True and C.OMITTED_SECRET_FILE in raw[2]
    else:
        assert rec["tool_calls"] == [] and C.LEFT_OUT_TOOL in raw[2]     # path not reached: the call is unknown


def test_the_prefix_scan_reads_no_further_than_its_bounds():
    long = '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"a","name":"Write","input":' \
           '{"file_path":"x.py","content":"' + "y" * 10_000 + '"}}]}}'
    got = C._scan_prefix(long)                # a long string within the window is passed over, never decoded
    assert got == {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a", "name": "Write",
                                                                 "input": {"file_path": "x.py",
                                                                           "content": C._LONG_STRING}}]}}
    cut = long[:-C.LONG_SCAN_CHARS] if len(long) > C.LONG_SCAN_CHARS else long.replace("y" * 10_000, "y" * 70_000)
    got = C._scan_prefix(cut)                 # one that does not end within LONG_SCAN_CHARS ends the reading
    assert got["message"]["content"][0]["input"] == {"file_path": "x.py"}
    deep = '{"a":' * 20 + "1" + "}" * 20
    assert C._scan_prefix(deep) is not None and C._scan_prefix("[1,2]") is None and C._scan_prefix("x") is None


# ---------------------------------------------------------------- what is left out is counted (item 6)

def test_lines_are_counted_by_reason_in_the_record_and_the_note(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_LINE_PARSE_CHARS", 100_000)
    monkeypatch.setattr(C, "STREAM_TEXT_BUDGET", 400_000)
    project = make_project(tmp_path)
    calls = [{"type": "tool_use", "id": f"c{i}", "name": "Read", "input": {"file_path": "greet.py"}} for i in range(3)]
    events = [INIT, "not json at all", json.dumps({"type": "assistant", "deep": json.loads("[" * 70 + "]" * 70)}),
              *_big_write(project)[:1],
              {"type": "assistant", "message": {"role": "assistant", "content": [
                  {"type": "text", "text": "z" * 60_000}]}}] + \
             [{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "z" * 60_000}]}}
              for _ in range(6)] + [{"type": "assistant", "message": {"role": "assistant", "content": calls}}, END]
    rec, raw, info = _record(tmp_path, project, events)
    turn = rec["turns"][0]
    left = turn["stream_lines_left_out"]
    assert left["not_json"] == 1 and left["too_deep"] == 1 and left["too_long"] == 1 and left["text_budget"] >= 2
    assert turn["stream_lines_not_read"] == sum(left.values()) and turn["tool_calls_left_out"] == 1 + 3
    note = C.import_note(info)
    for why in ("not a JSON object", "nested deeper", "longer than devmem parses", "STREAM_TEXT_BUDGET"):
        assert why in note


def test_the_digest_list_holds_exactly_max_stream_items(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_STREAM_ITEMS", 10)
    project = make_project(tmp_path)
    edits = [{"old_string": f"a{i}", "new_string": f"b{i}"} for i in range(4)]
    calls = [{"type": "tool_use", "id": f"m{i}", "name": "MultiEdit",
              "input": {"file_path": str(project / "greet.py"), "edits": edits}} for i in range(4)]
    rec, raw, info = _record(tmp_path, project, [INIT, {"type": "assistant", "message": {"content": calls}}, END])
    assert len(rec["turns"][0]["tool_calls"]) == 10 and rec["turns"][0]["tool_call_entries_left_out"] == 6


# ---------------------------------------------------------------- side channels of a hostile stream (item 7)

@pytest.mark.parametrize("case", ["assistant_tur", "mcp_blocks", "server_blocks", "int_and_str_id",
                                  "reused_id_past_budget", "unknown_field"])
def test_a_side_channel_of_a_hostile_stream_keeps_no_secret_file_text(tmp_path, monkeypatch, case):
    project = make_project(tmp_path)
    (project / ".env").write_text(ENV_TEXT, encoding="utf-8")
    env = str(project / ".env")
    rd = {"type": "tool_use", "id": "R", "name": "Read", "input": {"file_path": env}}
    ok = {"type": "tool_use", "id": "A", "name": "Read", "input": {"file_path": str(project / "greet.py")}}
    a = {"type": "assistant", "message": {"content": [rd]}}
    events: list = [INIT]
    if case == "assistant_tur":
        events += [a, {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]},
                       "tool_use_result": {"file": {"content": ENV_TEXT}}}]
    elif case == "mcp_blocks":
        events += [{"type": "assistant", "message": {"content": [
            {"type": "mcp_tool_use", "id": "M", "name": "read", "server_name": "fs", "input": {"path": env}}]}},
            {"type": "user", "message": {"content": [{"type": "mcp_tool_result", "tool_use_id": "M",
                                                      "content": [{"type": "text", "text": ENV_TEXT}]}]}}]
    elif case == "server_blocks":
        events += [{"type": "assistant", "message": {"content": [
            {"type": "server_tool_use", "id": "S", "name": "web_fetch", "input": {"url": "file:///" + env}},
            {"type": "web_fetch_tool_result", "tool_use_id": "S", "content": {"text": ENV_TEXT}}]}}]
    elif case == "int_and_str_id":
        events += [{"type": "assistant", "message": {"content": [{**rd, "id": 7}]}},
                   {"type": "assistant", "message": {"content": [{**ok, "id": "7"}]}},
                   {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "7",
                                                             "content": ENV_TEXT}]}}]
    elif case == "reused_id_past_budget":
        monkeypatch.setattr(C, "STREAM_TEXT_BUDGET", 5_000)
        events += [{"type": "assistant", "message": {"content": [{**ok, "id": "X"}]}},
                   {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "X",
                                                             "content": ENV_TEXT}]}},
                   {"type": "assistant", "message": {"content": [{"type": "text", "text": "p" * 6_000}]}},
                   {"type": "assistant", "message": {"content": [{**rd, "id": "X"}]}}]
    else:
        events += [a, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "R",
                                                                "content": "x"}]},
                       "stdout_copy": ENV_TEXT, "extra": {"lines": ENV_TEXT.splitlines()}}]
    rec, raw, info = _record(tmp_path, project, events + [END])
    store = project / ".devmemory"
    assert not [f for f in store.rglob("*") if f.is_file() and (SECRET.encode() in f.read_bytes()
                                                                 or b"zq81" in f.read_bytes())]


# ---------------------------------------------------------------- paths and encodings (item 7)

def test_in_project_paths_in_other_spellings_are_not_labelled_outside(tmp_path):
    project = make_project(tmp_path)
    (project / "src").mkdir()
    (project / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    forms = ["./src/main.py", "src/main.py", str(project / "src" / "main.py")]
    if os.name == "nt":
        p = str(project.resolve() / "src" / "main.py")
        forms += ["\\\\?\\" + p, "/" + p[0].lower() + p[2:].replace("\\", "/"), "/mnt/" + p[0].lower() + p[2:].replace("\\", "/")]
    calls = [{"type": "tool_use", "id": f"r{i}", "name": "Read", "input": {"file_path": f}} for i, f in enumerate(forms)]
    rec, raw, info = _record(tmp_path, project, [INIT, {"type": "assistant", "message": {"content": calls}}, END])
    assert [c.get("file") for c in rec["tool_calls"]] == ["src/main.py"] * len(forms)
    assert not [c for c in rec["tool_calls"] if "path_omitted" in c]


TEXT = "".join(json.dumps(e) + "\n" for e in [
    INIT, {"type": "assistant", "message": {"content": [{"type": "text", "text": "HEAD_MARK"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "漢字" * 2000}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "TAIL_MARK"}]}}, END])


@pytest.mark.parametrize("case", ["utf8_then_utf16", "utf8_then_utf16_bom", "utf16_then_utf8", "utf16_cjk_first"])
def test_a_stream_file_in_two_encodings_keeps_both_parts_and_says_so(tmp_path, case):
    project = make_project(tmp_path)
    lines = TEXT.splitlines(keepends=True)
    head, tail = "".join(lines[:2]), "".join(lines[2:])
    data = {"utf8_then_utf16": head.encode() + tail.encode("utf-16-le"),
            "utf8_then_utf16_bom": head.encode() + b"\xff\xfe" + tail.encode("utf-16-le"),
            "utf16_then_utf8": b"\xff\xfe" + head.encode("utf-16-le") + tail.encode(),
            "utf16_cjk_first": json.dumps(INIT).encode("utf-16-le") + "\n".encode("utf-16-le")
            + "".join(lines[2:]).replace("\\u6f22\\u5b57", "漢字").encode("utf-16-le")}[case]
    path = tmp_path / "s.jsonl"
    path.write_bytes(data)
    info: dict = {}
    meta = C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory", info=info)
    rec = json.loads(meta.read_text(encoding="utf-8"))
    texts = [t["text"] for t in rec["assistant_text"]]
    assert "TAIL_MARK" in texts and (case == "utf16_cjk_first" or "HEAD_MARK" in texts)
    if case != "utf16_cjk_first":
        assert "could not be read reliably" in C.import_note(info) and info["stream_left_out"]["encoding"]["switches"] == 1


# ---------------------------------------------------------------- the worst shapes at the bounds (Docker)

STRESS = """
import json, resource, sys, time
sys.path.insert(0, sys.argv[1])
from devmem import capture as C
shape, path, project = sys.argv[2], sys.argv[3], sys.argv[4]
t = time.perf_counter()
info = {}
C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project + "/.devmemory", info=info)
print(json.dumps({"s": time.perf_counter() - t, "mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}))
"""


def _stress_file(path: Path, shape: str) -> None:
    target = C.STREAM_READ_HEAD + C.STREAM_READ_TAIL
    nest = "[" + ",".join(["[" * 57 + "]" * 57] * ((C.MAX_LINE_VALUES - 140) // 58)) + "]"
    size = 0
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        i = 0
        while size < target:
            if shape == "values_in_results":       # small objects in the output of an ordinary call: the dearest
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": f"r{i}", "name": "Read", "input": {"file_path": "a.py"}}]}}) + "\n")
                line = ('{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"r%d","content":'
                        '"ok"}]},"tool_use_result":[%s]}' % (i, ",".join(['{"a":"b"}'] * ((C.MAX_LINE_VALUES - 300) // 4))))
            elif shape == "nested":
                line = _call_line(i, nest)
            else:                                  # distinct keys, then lines too long to parse
                line = _call_line(i, "{" + ",".join(f'"k{i}_{j}":0' for j in range(C.MAX_LINE_KEYS - 10)) + "}") \
                    if i < 40 else _call_line(i, '"' + "w" * (C.MAX_LINE_PARSE_CHARS + 10) + '"')
            f.write(line + "\n")
            size += len(line) + 1
            i += 1


@pytest.mark.stress
@pytest.mark.skipif(sys.platform == "win32", reason="peak memory read with resource.getrusage")
@pytest.mark.parametrize("shape", ["values_in_results", "nested", "keys_then_long"])
def test_the_worst_shapes_at_the_bounds_stay_within_the_stated_time_and_memory(tmp_path, shape):
    project = make_project(tmp_path)
    path = tmp_path / "s.jsonl"
    _stress_file(path, shape)
    r = subprocess.run([sys.executable, "-c", STRESS, str(ROOT / "plugin"), shape, str(path), str(project)],
                       capture_output=True, text=True, timeout=900)
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got["s"] < 60 and got["mb"] < 600, got
