"""G4: the Stop hook reads a bounded part of the transcript per event.

With 7.9 MB of new transcript (verify3/redact/mk_transcript.py) the Stop hook took about 25 s of Claude Code's 30 s
Stop timeout: read_transcript_since handed over up to 8 MB of assistant text and the hook redacted all of it (linear,
but 2-5 microseconds a character). Now at most TRANSCRIPT_READ_CAP bytes are read and about TRANSCRIPT_TEXT_BUDGET
characters returned per event; the next Stop continues where this one stopped, so nothing is lost or read twice.
"""
from __future__ import annotations

import json
import time

from hooks_helpers import make_project

from devmem import hooks
from devmem import sessions as S


def _line(i: int, text: str, ts: str = "2026-11-14T00:00:01Z") -> str:
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "id": f"m{i}", "model": "x", "content": [{"type": "text", "text": text}]}}) + "\n"


def _big_transcript(path, mb: float, body: str) -> list[str]:
    texts = []
    with open(path, "w", encoding="utf-8") as f:
        i = 0
        while f.tell() < mb * 1_000_000:
            t = f"#{i} " + body
            f.write(_line(i, t))
            texts.append(t.strip())
            i += 1
    return texts


def test_a_big_backlog_arrives_over_several_calls_without_loss(tmp_path):
    tp = tmp_path / "t.jsonl"
    texts = _big_transcript(tp, 8, ("plain words " * 400)[:3990])
    size = tp.stat().st_size
    got, offset, calls = [], 0, 0
    while offset < size:
        items, new = S.read_transcript_since(str(tp), offset)
        chars = sum(len(it["text"]) for it in items if it["kind"] == "text")
        assert chars <= 200_000 and new - offset <= 2 * 1024 * 1024, (chars, new - offset)
        assert new > offset
        got += [it["text"] for it in items if it["kind"] == "text"]
        offset, calls = new, calls + 1
        assert calls < 100
    assert got == texts            # each text once, in order


def test_a_line_longer_than_the_read_cap_is_skipped_not_stuck(tmp_path):
    tp = tmp_path / "t.jsonl"
    tp.write_bytes((_line(0, "x" * 5000) + _line(1, "after the long line")).encode("utf-8"))
    items, offset = S.read_transcript_since(str(tp), 0, max_bytes=1000)
    assert offset == len(_line(0, "x" * 5000).encode())
    assert [(it["kind"], it["what"], it["bytes"]) for it in items] == [("gap", "skipped-line", offset)]  # and says so
    items, offset = S.read_transcript_since(str(tp), offset, max_bytes=1000)
    assert [it["text"] for it in items if it["kind"] == "text"] == ["after the long line"]
    assert offset == tp.stat().st_size


def test_one_line_over_the_text_budget_is_cut_to_it(tmp_path):
    tp = tmp_path / "t.jsonl"
    blocks = [{"type": "text", "text": f"block {i} " + "y" * 3000} for i in range(10)]
    tp.write_text(json.dumps({"type": "assistant", "message": {"id": "m", "content": blocks}}) + "\n" + _line(1, "next"),
                  encoding="utf-8")
    items, offset = S.read_transcript_since(str(tp), 0, text_budget=10_000)
    texts = [it["text"] for it in items if it["kind"] == "text"]
    assert len(texts) == 4 and texts[0].startswith("block 0")       # 3 x 3008 below 10,000, the 4th crosses it
    items, offset = S.read_transcript_since(str(tp), offset, text_budget=10_000)
    assert [it["text"] for it in items if it["kind"] == "text"] == ["next"]


def _hook(event: str, project, **payload) -> None:
    hooks.run([event], json.dumps({"session_id": "sess-big", "cwd": str(project), "hook_event_name": event,
                                   **payload}).encode("utf-8"))


def test_the_stop_hook_stays_well_inside_its_timeout_on_a_big_transcript(tmp_path, monkeypatch):
    project = make_project(tmp_path, git=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    tp = tmp_path / "transcript.jsonl"
    secret = "AKIA" + "Q" * 16
    _big_transcript(tp, 8, (f"curl -u x {secret} " + "curl -ux " * 500)[:4000])   # the old reproducer's shape
    _hook("SessionStart", project, source="startup", transcript_path=str(tmp_path / "none.jsonl"))
    _hook("UserPromptSubmit", project, prompt="go", transcript_path=str(tp))
    walls = []
    for _ in range(2):
        t = time.perf_counter()
        _hook("Stop", project, transcript_path=str(tp), last_assistant_message="ok")
        walls.append(time.perf_counter() - t)
    # about 1 s each on the machine that measured 25 s before; Claude Code's Stop timeout is 30 s
    assert max(walls) < 10, walls
    store = project / ".devmemory"
    rec = json.loads(next((store / "sessions").glob("*.meta.json")).read_text(encoding="utf-8"))
    texts = [a["text"] for a in rec["assistant_text"]]
    assert texts and texts[0].startswith("#0 ") and len(texts) > 50          # the second Stop continued
    for p in store.rglob("*"):
        if p.is_file():
            assert secret.encode() not in p.read_bytes(), p
