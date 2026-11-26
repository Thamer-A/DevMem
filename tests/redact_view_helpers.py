"""Helpers for the redaction tests that use real tool output: run a tool (or skip when it is not installed), pass its
output through the hook entry point for a tool call (devmem.hooks.on_tool: the output cut, filtered and redacted as a
hook does it, then the event through the store's write boundary), and look for any 8 characters of a value."""
from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from devmem import hooks
from devmem import redact as R
from devmem.redact import Redactor

GIT_ID = ["-c", "core.autocrlf=false", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
          "-c", "color.ui=never", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"]


@functools.lru_cache(maxsize=1)
def _ripgrep() -> str | None:
    """ripgrep: on PATH, else the binary named by DEVMEM_TEST_RG, else Claude Code's own executable when it answers
    as ripgrep under ARGV0=rg (it bundles one); each candidate must print `ripgrep ...` for `--version`."""
    for exe in (shutil.which("rg"), os.environ.get("DEVMEM_TEST_RG"), os.environ.get("CLAUDE_CODE_EXECPATH")):
        if not exe or not os.path.isfile(exe):
            continue
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, timeout=30, env=dict(os.environ, ARGV0="rg"))
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and r.stdout.startswith(b"ripgrep"):
            return exe
    return None


def tool_path(name: str) -> str:
    """The path of an installed tool, or a skip naming the tool (never a hand-built stand-in under the same test)."""
    found = _ripgrep() if name == "rg" else shutil.which(name)
    if not found:
        pytest.skip(f"{name} is not installed on this machine (rg: PATH, DEVMEM_TEST_RG, CLAUDE_CODE_EXECPATH)")
    return found


def run(cwd: Path, argv: list[str], ok=(0,)) -> str:
    """Run a real tool and return its standard output as text, exactly as printed (line endings included)."""
    exe = tool_path(argv[0])
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C", LANG="C")
    if argv[0] == "rg":
        env["ARGV0"] = "rg"
    r = subprocess.run([exe, *argv[1:]], cwd=str(cwd), capture_output=True, env=env, timeout=120)
    assert r.returncode in ok, (argv, r.returncode, r.stderr[:500])
    return r.stdout.decode("utf-8", "replace")


def git(cwd: Path, *args: str, ok=(0,)) -> str:
    return run(cwd, ["git", *GIT_ID, *args], ok=ok)


ENDINGS = ("lf", "crlf", "cr", "mixed")


def with_endings(text: str, kind: str) -> str:
    """`text` (written with `\\n`) with other line ends: CRLF, CR alone (old Mac), or mixed (CRLF and LF by turns)."""
    if kind == "lf":
        return text
    lines = text.split("\n")
    if kind == "mixed":
        return "".join(ln + ("\r\n" if i % 2 else "\n") for i, ln in enumerate(lines[:-1])) + lines[-1]
    return {"crlf": "\r\n", "cr": "\r"}[kind].join(lines)


_MARKER = re.compile(r"\[REDACTED[^\]\n]{0,64}\]")


def plain_reading(text: str) -> str:
    """The plain reading alone (every rule over the text and its decorated lines, no other reading)."""
    r = Redactor(None)
    return r._rules(r._line_view(text), R._RULES, R._ASSIGN_RULES)


def lost_redactions(orig: str, plain: str, final: str) -> list[str]:
    """Text that the plain reading replaced on a line and that the final output shows again on that line (an
    alternative reading may only add redaction). Line i of each is line i of the others (no rule changes a line
    count); on a line the plain reading changed, the parts it kept are found in order and what lies between them was
    replaced; any such part of 8 or more characters must not be in the final line."""
    out = []
    for o, p, f in zip(orig.split("\n"), plain.split("\n"), final.split("\n")):
        if o == p:
            continue
        segs = _MARKER.split(p)
        pos, gaps = 0, []
        for k, seg in enumerate(segs):
            at = o.find(seg, pos) if k else (0 if o.startswith(seg) else -1)
            if at < 0:
                break
            if k:
                gaps.append(o[pos:at])
            pos = at + len(seg)
        else:
            if pos < len(o) and len(segs) > 1:
                gaps.append(o[pos:])
        out += [g for g in gaps if len(g.strip()) >= 8 and g.strip() in f]
    return out


def leaks(text: str, values: list[str]) -> list[str]:
    """The values that still have 8 consecutive characters in `text`."""
    return [v for v in values if any(v[k:k + 8] in text for k in range(len(v) - 7))]


def stored(root: Path, tool: str, inp: dict, response) -> str:
    """Everything the store holds after one PostToolUse hook event of `tool` with this response (in-process:
    devmem.hooks.on_tool, as the hook command runs it)."""
    project = root / "p"
    store = project / ".devmemory"
    store.mkdir(parents=True, exist_ok=True)
    payload = {"session_id": "views-0000", "cwd": str(project), "hook_event_name": "PostToolUse", "tool_name": tool,
               "tool_input": inp, "tool_use_id": "t1", "tool_response": response}
    hooks.on_tool(payload, project, store, Redactor(store))
    return "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in sorted(store.rglob("*")) if f.is_file())


def bash_stored(root: Path, command: str, stdout: str) -> str:
    return stored(root, "Bash", {"command": command}, {"stdout": stdout, "stderr": "", "interrupted": False})
