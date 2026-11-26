"""Every pass `devmem import` makes over a line's raw text is linear in the line's length (sixth review, blocker 1: the
pre-parse counter was quadratic on a line whose string never closes: a transcript cut while it was written took
minutes for 256 KB). Each check feeds inputs built from the pattern's own pieces (an unclosed string, its escapes and
separators repeated) at 0.25 to 4 MB and compares CPU time from 1 to 4 MB: linear growth is 4x, quadratic 16x; the
test allows 9x, takes the best of three runs per size and retries the whole sweep, so a loaded machine does not
fail it."""
from __future__ import annotations

import io
import json
import time

import pytest

from devmem import capture as C
from devmem import redact as R

SIZES = [256 * 1024, 512 * 1024, 1024 * 1024, 2048 * 1024, 4096 * 1024]
def _cpu(fn, arg) -> float:
    """CPU seconds of one call: calls repeated until 0.1 s of CPU time has passed (Windows counts process time in
    15.6 ms steps), the best of three such rounds."""
    best = float("inf")
    for _ in range(3):
        t, k = time.process_time(), 0
        while True:
            fn(arg)
            k += 1
            spent = time.process_time() - t
            if spent >= 0.1:
                break
        best = min(best, spent / k)
        if best > 2:
            break                          # slow already: once is enough
    return best


def _linear(fn, make) -> None:
    """From 1 MB to 4 MB the CPU time may grow at most 9x (linear: 4x, quadratic: 16x; the smaller sizes fit in a CPU
    cache and are not compared); a sweep already 4x over linear stops early (a quadratic pass would otherwise take
    hours at 4 MB)."""
    ratios = []
    for _attempt in range(3):
        times: list[float] = []
        for n in SIZES:
            times.append(_cpu(fn, make(n)))
            if times[-1] > 1 and times[-1] / times[0] > 4 * 3 * n / SIZES[0]:
                break
        ratio = times[-1] / times[2] if len(times) == len(SIZES) else float("inf")
        ratios.append((round(ratio, 1), [round(t, 4) for t in times]))
        if ratio < 9:
            return
    raise AssertionError(f"grows faster than linear: {ratios}")


def _rep(unit: str, n: int, head: str = "", tail: str = "") -> str:
    return head + unit * max(1, (n - len(head) - len(tail)) // len(unit)) + tail


COUNTER_INPUTS = {
    "escaped_quotes_unclosed": lambda n: _rep('\\"', n, '{"a":"'),
    "quote_backslash_pairs": lambda n: _rep('"\\', n),
    "backslash_quote_x_unclosed": lambda n: _rep('\\"x', n, '{"a":"'),
    "escaped_quotes_closed": lambda n: _rep('\\"', n, '{"a":"', '"}'),
    "backslash_runs": lambda n: _rep("\\\\", n, '{"a":"'),
    "lone_quote_then_escapes": lambda n: _rep("\\a", n, '"'),
    "keys_unclosed": lambda n: _rep('"k":"\\"', n, "{"),
    "quote_colon_runs": lambda n: _rep('":', n),
    "trailing_backslash": lambda n: _rep("\\", n, '{"a":"'),
}


@pytest.mark.parametrize("shape", COUNTER_INPUTS)
@pytest.mark.parametrize("fn", ["_values", "_line_keys"])
def test_the_pre_parse_counter_is_linear(fn, shape):
    _linear(getattr(C, fn), COUNTER_INPUTS[shape])


ID_INPUTS = {
    "id_keys_unclosed": lambda n: _rep('"id":"\\"', n),
    "id_keys_no_value": lambda n: _rep('"id"   :   ', n),
    "tool_use_id_spaces": lambda n: _rep('"tool_use_id"' + " " * 50, n),
    "id_long_values": lambda n: _rep('"id":"' + "x" * 300, n),
    "id_numbers": lambda n: _rep('"id":' + "9" * 30, n),
}


@pytest.mark.parametrize("shape", ID_INPUTS)
def test_marking_the_ids_of_a_stub_line_is_linear(tmp_path, shape):
    from devmem.redact import Redactor

    def mark(line):
        C._Calls(tmp_path, Redactor(None)).mark_ids(line)
    _linear(mark, ID_INPUTS[shape])


SCAN_INPUTS = {
    "string_unclosed": lambda n: _rep('\\"', n, '{"type":"assistant","message":{"content":[{"type":"text","text":"'),
    "deep_open": lambda n: _rep('{"a":', n),
    "spaces": lambda n: _rep(" ", n, "{"),
    "commas": lambda n: _rep(",", n, '{"a":['),
    "numbers": lambda n: _rep("1e1", n, '{"a":'),
}


@pytest.mark.parametrize("shape", SCAN_INPUTS)
def test_the_prefix_scan_of_a_long_line_is_bounded(shape):
    _linear(C._scan_prefix, SCAN_INPUTS[shape])


REGION_INPUTS = {
    "utf8_switch_candidates": lambda n: _rep("\n{\x00", n).encode("latin-1"),
    "utf8_nul_runs": lambda n: b"{" + b"\x00" * n,
    "utf16_newlines": lambda n: b"\xff\xfe" + b"\n\x00" * (n // 2),
    "utf16_switch_candidates": lambda n: b"\xff\xfe" + b'\n\x00{"' * (n // 4),
    "bom_runs": lambda n: b"\xef\xbb\xbf\n" * (n // 4),
    "invalid_utf8": lambda n: b"\xff\xc3" * (n // 2),
    "cr_runs": lambda n: b"\r" * n,
}


@pytest.mark.parametrize("shape", REGION_INPUTS)
def test_reading_a_stream_file_by_encoding_regions_is_linear(shape):
    def read(data: bytes):
        enc, bom = C._encoding(data[:4096])
        f = io.BytesIO(data)
        f.seek(bom)
        rest: list = []
        for _batch in C._region_lines(f, len(data) - bom, rest, None, enc, {}):
            pass
    _linear(read, REGION_INPUTS[shape])


COMMAND_INPUTS = {
    "one_word": lambda n: "a" * n,
    "words_of_4096": lambda n: _rep("a" * 4096 + " ", n),
    "separators": lambda n: _rep(";|&<>()=,", n),
    "quotes": lambda n: _rep("\"'`", n),
    "spaces_then_word": lambda n: " " * (n - 5000) + "b" * 5000,
}


@pytest.mark.parametrize("shape", COMMAND_INPUTS)
def test_the_decision_and_the_stored_form_of_a_command_are_linear(shape):
    def decide(cmd):
        R._read_in_full({"command": cmd, "x": cmd, "file_path": cmd[:5000]})
        R.secret_command_marker(cmd)
        R.secret_call_input({"command": cmd, "path": cmd})
    _linear(decide, COMMAND_INPUTS[shape])


def test_a_side_field_is_walked_in_linear_time(tmp_path):
    from devmem.redact import Redactor
    calls = C._Calls(tmp_path, Redactor(None))
    red = Redactor(None, enabled=False)

    def side(n):
        v = [{"id": f"x{i}", "tool_use_id": "nope", "v": [1, 2, {"k": None}]} for i in range(n // 64)]
        C._side("extra", v, None, calls, red)
        C._side("extra", v, ["ok"], calls, red)
    _linear(side, lambda n: n)


def test_a_stream_whose_last_line_is_cut_inside_a_string_imports_in_seconds(tmp_path):
    """The shape a transcript still being written ends with: a tool result quoting JSON (many `\\"`), cut inside it.
    1 MB of it took about an hour (extrapolated from 256 KB: 320 s); it must not take a minute."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    from hooks_helpers import make_project
    project = make_project(tmp_path)
    head = json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}) + "\n"
    cut = '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t","content":"' + \
          '{\\"k\\": \\"v\\", ' * (1024 * 1024 // 16)
    path = tmp_path / "s.jsonl"
    path.write_text(head + cut, encoding="utf-8")
    code = ("import sys\nfrom devmem import capture as C\n"
            "C.record_session(sys.argv[1], sys.argv[2], 'x', '2026-11-16T00:00:00Z', store=sys.argv[1] + '/.devmemory')\n")
    plugin = str(Path(C.__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-c", code, str(project), str(path)], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": plugin}, timeout=60)
    assert r.returncode == 0, r.stderr[-500:]
