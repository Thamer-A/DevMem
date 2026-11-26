"""The secret-file line filter and stream capture cost time linear in their input, with a small constant (verifier
round 5: omit_secret_file_lines took 17 s on 2 MB of `a/b:c` lines and 18.5 s on `./x.py-1-y` lines, about 50 us per
line, because every candidate path on every line ran every secret pattern through fnmatch; `devmem run` / `devmem
import` filtered and redacted uncapped tool results)."""
from __future__ import annotations

import json
import time

import pytest

from devmem import redact as R

BOUND_S = 3.0          # 1 MB of any shape (measured about 0.3-0.6 s on the development machine)

SHAPES = {
    "short-path-colon": lambda i: "a/b:c",
    "context-line": lambda i: "./x.py-1-y",
    "dotted-path": lambda i: "src/a.b.c.d.e.f.g.h.i.j.k:" + "x" * 10,
    "distinct-paths": lambda i: f"d{i}/f{i}.py:{i}:x",
    "distinct-many-dashes": lambda i: f"x{i}" + "/a-" * 30 + ":",
    "distinct-names-dashes": lambda i: f"x{i}/" + "".join(f"q{i}{j}-" for j in range(40)) + ":",
    "distinct-many-slashes": lambda i: f"x{i}/" + "".join(f"q{i}/{j}." for j in range(60)) + ":",
    "long-line": lambda i: "a." * 250 + "-",
    "diff-of-secret": lambda i: "diff --git a/.env b/.env\n+x",
}


def _text(shape, size: int) -> str:
    out, n, i = [], 0, 0
    while n < size:
        line = SHAPES[shape](i)
        out.append(line)
        n += len(line) + 1
        i += 1
    return "\n".join(out)


def _best(fn, s, tries: int = 3) -> float:
    best = 1e9
    for _ in range(tries):
        for cached in ("_is_secret_file", "_secret_part_cached"):
            if hasattr(R, cached):
                getattr(R, cached).cache_clear()
        t = time.perf_counter()
        fn(s)
        best = min(best, time.perf_counter() - t)
    return best


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_omit_secret_file_lines_is_linear_and_fast(shape):
    small, big = _text(shape, 128 * 1024), _text(shape, 1024 * 1024)
    t_small, t_big = _best(R.omit_secret_file_lines, small), _best(R.omit_secret_file_lines, big)
    assert t_big < BOUND_S, f"{shape}: {t_big:.2f}s on {len(big)} characters"
    # 8 times the input: at most about 8 times the time (generous for a busy machine; quadratic would be 64)
    assert t_big < 24 * max(t_small, 0.005), f"{shape}: {t_small:.3f}s -> {t_big:.3f}s"


def test_the_incremental_line_check_agrees_with_is_secret_file():
    """_secret_line_head judges each candidate head as is_secret_file would (it only avoids repeating work)."""
    import random
    rnd = random.Random(7)
    alpha = ["a", "b", ".", "/", "\\", ":", "-", "1", ".env", "secret", "id_rsa", "x.pem", "~1", "*", "?", "C:", " ",
             "_netrc", "ENV", "credentials", "::$data", "key", ".envrc", "x.ppk"]

    def reference(line: str) -> int:
        lim = min(len(line), 512)
        tried, i = 0, 2 if len(line) > 2 and line[1] == ":" and line[0].isalpha() else 0
        while i < lim and tried < 16:
            c = line[i]
            if c.isspace():
                return 0
            if c in ":-" and i:
                head = line[:i]
                if any(ch in head for ch in "./\\"):
                    tried += 1
                    if R.is_secret_file(head):
                        return i
            i += 1
        return 0
    for _ in range(20000):
        line = "".join(rnd.choice(alpha) for _ in range(rnd.randint(1, 14)))
        assert R._secret_line_head(line, None) == reference(line), line


def test_stream_capture_cuts_huge_texts_before_filtering_and_redacting(tmp_path):
    from hooks_helpers import make_project

    import devmem.capture as C
    STREAM_TEXT_CAP, record_session = getattr(C, "STREAM_TEXT_CAP", 64 * 1024), C.record_session
    project = make_project(tmp_path)
    huge = ("./src/a.py:1:x = 1\n" * (6 * 1024 * 1024 // 19))
    tail_secret = "gh" + "p_" + "T" * 36          # beyond the cap: never stored
    sid = "5e55107d-0000-4000-8000-0000000000dd"
    events = [
        {"type": "system", "subtype": "init", "session_id": sid, "model": "m"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "big " * (STREAM_TEXT_CAP // 2)},
            {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "grep -rn x ."}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "b1", "content": huge + tail_secret}]},
         "tool_use_result": {"stdout": huge + tail_secret, "stderr": ""}},
        {"type": "result", "subtype": "success", "result": "r " * STREAM_TEXT_CAP, "session_id": sid},
    ]
    stream = tmp_path / "s.jsonl"
    stream.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    t = time.perf_counter()
    meta = record_session(project, stream, "check", "2026-11-14T10:00:00Z", store=project / ".devmemory")
    took = time.perf_counter() - t
    raw = (meta.parent / json.loads(meta.read_text(encoding="utf-8"))["raw_record"]).read_text(encoding="utf-8")
    assert took < 20, took        # 12 MB of tool output (measured about 1 s; uncut it was over 30 s)
    assert tail_secret not in raw and "T" * 36 not in raw
    assert "is not stored]" in raw
    assert len(raw) < 1024 * 1024


def test_stream_capture_stops_redacting_at_its_budget(tmp_path, monkeypatch):
    from hooks_helpers import make_project

    import devmem.capture as C
    monkeypatch.setattr(C, "STREAM_TEXT_BUDGET", 10_000)
    project = make_project(tmp_path)
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": "5e55107d-0000-4000-8000-0000000000ee"})]
    lines += [json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"t{i} " * 500}]}})
              for i in range(20)]
    stream = tmp_path / "s.jsonl"
    stream.write_text("\n".join(lines) + "\n", encoding="utf-8")
    meta = C.record_session(project, stream, "check", "2026-11-14T10:00:00Z", store=project / ".devmemory")
    raw = (meta.parent / json.loads(meta.read_text(encoding="utf-8"))["raw_record"]).read_text(encoding="utf-8")
    out = raw.splitlines()
    assert len(out) == len(lines)                 # one line per input line
    assert "devmem_omitted" in out[-1] and "t19" not in raw
