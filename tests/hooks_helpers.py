"""Helpers for the capture/hooks tests: replay real Claude Code hook payloads into a temp project.

The fixtures in tests/test_hooks_fixtures/cc-2.1.323/ are the stdin payloads Claude Code 2.1.323 sent to a
probe hook during two real headless sessions (the second resumed the first). Machine paths were replaced
by {PROJECT} and {TRANSCRIPT}; thinking text in the transcript excerpt was replaced by a sentinel.

Session 1 prompt: edit greet.py ("hello " -> "hi "), Write farewell.py, `rm notes.txt` (via Bash), and a
failing `ls does_not_exist_dir`. Session 2 (resume): append "# end" to farewell.py with Edit.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "test_hooks_fixtures" / "cc-2.1.323"
SENTINEL = "HIDDEN-REASONING-SENTINEL"
SESSION_ID = "4a014bc1-dc45-4a0c-9e7a-ab4dec5491d8"

TURN1 = ["000_SessionStart", "002_UserPromptSubmit", "004_PostToolUse", "008_PostToolUse", "010_PostToolUse",
         "016_PostToolUse", "018_PostToolUseFailure", "022_Stop", "024_SessionEnd"]
TURN2 = ["026_SessionStart", "028_UserPromptSubmit", "030_PostToolUse", "034_PostToolUse", "038_Stop",
         "040_SessionEnd"]


_WIN_TAIL = re.compile(r"\{PROJECT\}((?:\\[^\\\"'\s]+)+)")


def _subst(x, mapping: dict[str, str]):
    if isinstance(x, str):
        if os.sep == "/":
            # The payloads were captured on Windows ("{PROJECT}\\greet.py"). On Linux/macOS Claude Code sends
            # POSIX paths, so replay them POSIX-shaped; a backslash is an ordinary filename character there.
            x = _WIN_TAIL.sub(lambda m: "{PROJECT}" + m.group(1).replace("\\", "/"), x)
        for k, v in mapping.items():
            x = x.replace(k, v)
        return x
    if isinstance(x, list):
        return [_subst(v, mapping) for v in x]
    if isinstance(x, dict):
        return {k: _subst(v, mapping) for k, v in x.items()}
    return x


def load_payload(name: str, project: Path, transcript: Path | None = None) -> dict:
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return _subst(data, {"{PROJECT}": str(project), "{TRANSCRIPT}": str(transcript or project / "none.jsonl")})


def make_project(root: Path, git: bool = True, init_store: bool = True, switched_on: bool | None = None) -> Path:
    """A small project. init_store: with a `.devmemory` store (delivery off). switched_on (default: same as
    init_store): this user's `devmem init` switch for it exists (devmem.consent, in the test's DEVMEM_USER_DATA), so
    the init route's hooks record it; pass False for a store the user never switched on (a shipped one)."""
    project = _make_project(root, git, init_store)
    if init_store if switched_on is None else switched_on:
        from devmem.consent import enable_init
        enable_init(project)
    return project


def _make_project(root: Path, git: bool, init_store: bool) -> Path:
    project = root / "proj with space"
    project.mkdir(parents=True)
    (project / "greet.py").write_text('def greet(name):\n    return "hello " + name\n', encoding="utf-8")
    (project / "notes.txt").write_text("notes\n", encoding="utf-8")
    if git:
        run_git(project, "init", "-q")
        run_git(project, "add", "-A")
        run_git(project, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "init")
    if init_store:
        (project / ".devmemory").mkdir()
        # these tests are about capture; delivery (scan + pack) is tested separately
        (project / ".devmemory" / "config.json").write_text('{"deliver": false}', encoding="utf-8")
    return project


def run_git(project: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(project), "-c", "core.autocrlf=false", *args], capture_output=True,
                          text=True, check=True).stdout


def transcript_parts() -> tuple[list[str], list[str]]:
    """The transcript excerpt split at the second user prompt (what existed at each Stop)."""
    lines = (FIXTURES / "transcript_excerpt.jsonl").read_text(encoding="utf-8").splitlines()
    second = [i for i, l in enumerate(lines)
              if json.loads(l).get("type") == "user" and isinstance(json.loads(l)["message"].get("content"), str)][1]
    return lines[:second], lines[second:]


def apply_effect(name: str, project: Path) -> None:
    """Do to the files what the real tool call did, just before its PostToolUse payload arrives."""
    if name == "008_PostToolUse":
        p = project / "greet.py"
        p.write_text(p.read_text(encoding="utf-8").replace('"hello "', '"hi "'), encoding="utf-8")
    elif name == "010_PostToolUse":
        (project / "farewell.py").write_text('def bye(name):\n    return "bye " + name\n', encoding="utf-8")
    elif name == "016_PostToolUse":
        (project / "notes.txt").unlink()
    elif name == "034_PostToolUse":
        p = project / "farewell.py"
        p.write_text(p.read_text(encoding="utf-8") + "# end\n", encoding="utf-8")


def replay(project: Path, names: list[str], transcript: Path, monkeypatch, hooks_mod,
           before_stop=None) -> list[tuple[int, str]]:
    """Feed payloads through devmem.hooks.run in-process (CLAUDE_PROJECT_DIR set like Claude Code does)."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    part1, part2 = transcript_parts()
    outs = []
    for name in names:
        apply_effect(name, project)
        if name == "022_Stop":
            transcript.write_text("\n".join(part1) + "\n", encoding="utf-8")
        elif name == "038_Stop":
            transcript.write_text("\n".join(part1 + part2) + "\n", encoding="utf-8")
        elif not transcript.exists():
            transcript.write_text("", encoding="utf-8")
        payload = load_payload(name, project, transcript)
        event = name.split("_", 1)[1]
        outs.append(hooks_mod.run([event], json.dumps(payload).encode("utf-8")))
    return outs


def one_meta(store: Path) -> dict:
    metas = sorted((store / "sessions").glob("*.meta.json"))
    assert len(metas) == 1, metas
    return json.loads(metas[0].read_text(encoding="utf-8"))
