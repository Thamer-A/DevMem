"""A stand-in for `claude -p` (no model, no network) for testing the gate runner: `run.py --fake`.

It writes where a real session writes (its configuration folder, the temp folder, the home folder, the working
copy), fires the exec-form hooks of the working copy's `.claude/settings.json` and `.claude/settings.local.json`
(where `devmem init` installs its own) as Claude Code would, and prints a stream-json session with usage numbers. It
answers by writing the prompt into `answer.txt`, followed by what its automatic memory held before the session (so a
test can see memory carried from one session to the next). `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` turns that memory off.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

VERSION = "2.1.332 (Claude Code)"


def _hook(event: str, payload: dict) -> str:
    out = ""
    for name in ("settings.json", "settings.local.json"):
        settings = Path.cwd() / ".claude" / name
        if not settings.is_file():
            continue
        cfg = json.loads(settings.read_text(encoding="utf-8"))
        for group in cfg.get("hooks", {}).get(event, []):
            for h in group.get("hooks", []):
                argv = [h["command"], *h.get("args", [])] if h.get("args") is not None else None
                if argv is None:
                    continue
                p = subprocess.run(argv, input=json.dumps(payload).encode(), capture_output=True, timeout=60)
                out += p.stdout.decode("utf-8", "replace")
    return out


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print(VERSION)
        return 0
    prompt = argv[argv.index("-p") + 1] if "-p" in argv else ""
    model = argv[argv.index("--model") + 1] if "--model" in argv else "fake"
    sid = str(uuid.uuid4())
    base = {"session_id": sid, "cwd": str(Path.cwd()), "transcript_path": str(Path.cwd() / "none.jsonl")}
    _hook("SessionStart", {**base, "hook_event_name": "SessionStart", "source": "startup"})
    context = _hook("UserPromptSubmit", {**base, "hook_event_name": "UserPromptSubmit", "prompt": prompt,
                                         "prompt_id": "p1"})
    recalled = ""
    if os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") != "1":
        mem = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "fake" / "memory"
        mem.mkdir(parents=True, exist_ok=True)
        if (mem / "MEMORY.md").is_file():
            recalled = (mem / "MEMORY.md").read_text(encoding="utf-8")
        with open(mem / "MEMORY.md", "a", encoding="utf-8") as f:
            f.write(f"- {prompt[:200]}\n")
    Path(tempfile.gettempdir(), "fake-claude.tmp").write_text("x", encoding="utf-8")
    Path(os.path.expanduser("~"), ".fake-claude-home").write_text("x", encoding="utf-8")
    answer = Path.cwd() / "answer.txt"
    answer.write_text(prompt + ("\n" + recalled if recalled else ""), encoding="utf-8")
    _hook("PostToolUse", {**base, "hook_event_name": "PostToolUse", "tool_name": "Write",
                          "tool_input": {"file_path": str(answer)}, "tool_response": {"success": True},
                          "tool_use_id": "t1"})
    _hook("Stop", {**base, "hook_event_name": "Stop", "prompt_id": "p1", "last_assistant_message": "done"})
    _hook("SessionEnd", {**base, "hook_event_name": "SessionEnd", "reason": "exit"})
    events = [
        {"type": "system", "subtype": "init", "session_id": sid, "model": model},
        {"type": "assistant", "message": {"model": model, "content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": str(Path.cwd() / "answer.txt")}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "duration_ms": 1234,
         "total_cost_usd": 0.0001, "session_id": sid, "result": "done" + (" (with context)" if context else "") + (f" (remembered: {recalled.strip()})" if recalled else ""),
         "usage": {"input_tokens": 100, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 200,
                   "output_tokens": 30, "cache_creation": {"ephemeral_5m_input_tokens": 40,
                                                           "ephemeral_1h_input_tokens": 10}}},
    ]
    for e in events:
        print(json.dumps(e), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
