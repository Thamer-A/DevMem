"""Secrets in the output of real tools (verifier v14b gen_views.py / tab_views.py: the views were built by hand before).
Each secret shape is written to a file in a temporary git repository (the value changed once, so the history holds an
old and a new value), the real tool is run, and its output goes through the PostToolUse hook entry point
(devmem.hooks.on_tool: cut, filtered, redacted and written as a hook does). No 8 consecutive characters of a value
the view shows may be left anywhere in the store. A tool that is not installed is skipped, never replaced by a
hand-built view. Views that are known to keep a value are strict expected failures, with the reason. The list
of views in docs/PRIVACY.md is generated from this module's tables and checked by test_the_readme_lists_exactly_these_views."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from devmem.redact import Redactor
from redact_view_helpers import ENDINGS, bash_stored, git, leaks, lost_redactions, plain_reading, run, tool_path, \
    with_endings

OLD = "Hq7RtW2xLm9KpB4nVc8sYd3F"
NEW = "Zx5NvB8mQw1ErT6yUi3oPa9S"

# shape: (file name, text of the file holding value v, a word on the name's line, a word on the line after the value)
SHAPES = {
    "single-line pair": ("app.yaml", lambda v: f"db:\n  host: localhost\n  password: {v}\n  user: app\n  port: 5432\n",
                         "password", "user"),
    "block scalar": ("tls.yaml", lambda v: f"tls:\n  cert: x.crt\n  private_key: |\n    {v}\n    {v[::-1]}\n  user: app\n",
                     "private_key", "user"),
    "list-item block": ("users.yaml", lambda v: f"users:\n  - password: |\n      {v}\n    name: bob\n    role: admin\n",
                        "password", "name"),
    "PEM key": ("notes.txt", lambda v: "key follows\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA" + v + "\n"
                + v[::-1] + v + "\n-----END RSA PRIVATE KEY-----\ndone\n", "BEGIN", "END"),
    "quoted value over lines": ("db.yaml", lambda v: f'db:\n  password: "{v[:12]}\n    {v[12:]}"\n  user: app\n',
                          "password", "user"),
}

POWERSHELL = "powershell" if sys.platform == "win32" else "pwsh"


def _ps(script: str) -> list[str]:
    return [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script + " | Out-String -Width 4096"]


# view: (the command as the tool call states it, argv given the file name and the two words; ok exit codes)
VIEWS = {
    "git diff": (lambda f, a, b: ["git", "diff", "HEAD~1", "HEAD"], (0,)),
    "git diff -U0": (lambda f, a, b: ["git", "diff", "-U0", "HEAD~1", "HEAD"], (0,)),
    "git diff -U1": (lambda f, a, b: ["git", "diff", "-U1", "HEAD~1", "HEAD"], (0,)),
    "git diff -U10": (lambda f, a, b: ["git", "diff", "-U10", "HEAD~1", "HEAD"], (0,)),
    "git show": (lambda f, a, b: ["git", "show", "HEAD"], (0,)),
    "git log -p": (lambda f, a, b: ["git", "log", "-p"], (0,)),
    "git log -p --graph": (lambda f, a, b: ["git", "log", "-p", "--graph"], (0,)),
    "git diff --word-diff": (lambda f, a, b: ["git", "diff", "--word-diff", "HEAD~1", "HEAD"], (0,)),
    "git blame": (lambda f, a, b: ["git", "blame", f], (0,)),
    "git grep -n": (lambda f, a, b: ["git", "grep", "-n", "", "--", f], (0,)),
    "grep -n": (lambda f, a, b: ["grep", "-n", "", f], (0,)),
    "grep -n -A3": (lambda f, a, b: ["grep", "-n", "-A3", a, f], (0,)),
    "grep -n -B3": (lambda f, a, b: ["grep", "-n", "-B3", b, f], (0,)),
    "grep -n -C3": (lambda f, a, b: ["grep", "-n", "-C3", a, f], (0,)),
    "grep -rn": (lambda f, a, b: ["grep", "-rn", "--exclude-dir=.git", "", "."], (0,)),
    "rg -n": (lambda f, a, b: ["rg", "-n", "", f], (0,)),
    "rg -n -C3": (lambda f, a, b: ["rg", "-n", "-C3", a, "."], (0,)),
    "diff -u": (lambda f, a, b: ["diff", "-u", "old.txt", f], (1,)),
    "diff -y": (lambda f, a, b: ["diff", "-y", "old.txt", f], (1,)),
    "cat -n": (lambda f, a, b: ["cat", "-n", f], (0,)),
    "nl -ba": (lambda f, a, b: ["nl", "-ba", f], (0,)),
    "Select-String -Context": (lambda f, a, b: _ps(f"Select-String -Path {f} -Pattern '{a}' -Context 2,3"), (0,)),
    "findstr /n": (lambda f, a, b: ["findstr", "/n", "^", f], (0,)),
}

# (view, shape) pairs known to keep a value, and why
KNOWN_KEPT = {
    ("git diff -U0", "block scalar"): "the name is outside the hunk: the body alone has no name",
    ("git diff -U0", "list-item block"): "the name is outside the hunk: the body alone has no name",
    ("git diff -U0", "PEM key"): "the BEGIN line is outside the hunk: the key material alone has no header",
}

NOT_TESTED = ["git diff --word-diff=porcelain", "git diff --color-words", "diff -c", "diff (normal format)", "grep -o",
              "rg --json", "sed -n", "head/tail", "less/more", "jq", "a pager or a terminal that wraps lines"]


def _repo(root: Path, shape: str, ending: str = "lf") -> tuple[Path, str]:
    fn, make0, _a, _b = SHAPES[shape]

    def make(v: str) -> str:
        return with_endings(make0(v), ending)

    d = root / "r"
    d.mkdir(parents=True)
    git(d, "init", "-q")
    (d / "README").write_bytes(b"x\n")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "c0")
    for i, v in enumerate((OLD, NEW), 1):
        (d / fn).write_bytes(make(v).encode())
        git(d, "add", "-A")
        git(d, "commit", "-qm", f"c{i}")
    (d / "old.txt").write_bytes(make(OLD).encode())
    return d, fn


def _known(name: str, shape: str, ending: str) -> str | None:
    """Why (view, shape) is known to keep a value with these line ends (a file with CR-only line ends is one line to
    git, so its every hunk shows the whole file, name included)."""
    why = KNOWN_KEPT.get((name, shape))
    return None if ending == "cr" and name.startswith("git diff -U0") else why


def _cases():
    for name in VIEWS:
        for shape in SHAPES:
            for ending in ENDINGS:
                why = _known(name, shape, ending)
                marks = [pytest.mark.xfail(strict=True, reason=why)] if why else []
                yield pytest.param(name, shape, ending, marks=marks, id=f"{name}|{shape}|{ending}")


@pytest.mark.parametrize("name,shape,ending", list(_cases()))
def test_no_value_is_kept_from_a_real_tool_view(tmp_path, name, shape, ending):
    """The file is written with LF, CRLF, CR-only or mixed line ends. Also, on the same output: what the plain reading
    replaced stays replaced (every other reading only adds to it), and a second redaction changes nothing."""
    if name == "findstr /n" and sys.platform != "win32":
        pytest.skip("findstr is a Windows tool")
    argv_of, ok = VIEWS[name]
    _fn, _make, a, b = SHAPES[shape]
    tool_path(argv_of("x", a, b)[0])
    d, fn = _repo(tmp_path, shape, ending)
    out = run(d, argv_of(fn, a, b), ok=ok)
    values = [v for v in (OLD, NEW) if v[:12] in out or v[12:] in out or v[::-1][:12] in out]
    assert values, f"{name} printed no value of {shape}: {out!r}"
    final = Redactor(None).text(out)
    assert not lost_redactions(out, plain_reading(out), final), final
    assert Redactor(None).text(final) == final
    kept = bash_stored(tmp_path, " ".join(argv_of(fn, a, b)), out)
    assert not leaks(kept, values + [v[::-1] for v in values]), f"{name} | {shape}:\n{out}\n---\n{kept[-2500:]}"


def matrix_text() -> str:
    """The docs/PRIVACY.md paragraph about these views, generated from the tables above and those of
    tests/test_redact_diff_edits.py."""
    import test_redact_diff_edits as E
    known = sorted({f"`{v}` with the {s} ({why})" for (v, s), why in KNOWN_KEPT.items()})
    kept_views = {v for (v, _s) in KNOWN_KEPT}
    full = [v for v in VIEWS if v not in kept_views]
    skippable = {"rg": [v for v in VIEWS if v.startswith("rg ")], "PowerShell": ["Select-String -Context"],
                 "Windows": ["findstr /n"]}
    lines = [
        "Views tested with real tool output, each passed through the hook for a tool call and the store searched",
        "(tests/test_redact_tool_views.py): with each value shape (" + ", ".join(SHAPES) + "), in a file with LF, "
        "CRLF, CR-only or mixed (CRLF and LF by turns) line ends, nothing of the value is kept in "
        + ", ".join(f"`{v}`" for v in full)
        + "; in " + ", ".join(f"`{v}`" for v in sorted(kept_views)) + " nothing is kept either, except (with LF, CRLF "
        "and mixed line ends: git shows a file with CR-only line ends as one line, name included): " + "; ".join(known)
        + ". On each of these outputs what the plain reading replaced stays replaced (every other reading only adds to "
        "it), and a second redaction changes nothing.",
        "A tool that is not installed is skipped, so on a machine without ripgrep (looked for on PATH, then in "
        "DEVMEM_TEST_RG, then as Claude Code's bundled one) " + ", ".join(f"`{v}`" for v in skippable["rg"])
        + " are not run, without PowerShell " + ", ".join(f"`{v}`" for v in skippable["PowerShell"])
        + ", and off Windows " + ", ".join(f"`{v}`" for v in skippable["Windows"]) + ".",
        "A value edited inside a diff (tests/test_redact_diff_edits.py): with each value shape ("
        + ", ".join(E.SHAPES) + "), in a file with LF, CRLF, CR-only or mixed (CRLF and LF by turns) line ends, "
        "neither the old nor the new "
        "value is kept in " + ", ".join(f"`{v}`" for v in E.VIEWS) + ", except, on Windows, `grep -n on a patch` of a "
        "file with CR-only line ends for the " + ", ".join(s for s in E.SHAPES if s in E.GREP_DROPS_CR)
        + " (Git for Windows' grep drops the returns: the file is one line without line breaks), and `diff -y` and "
        "`diff -y -W200` of a file with CRLF or mixed line ends for the "
        + ", ".join(s for s in E.SHAPES if s in E.SDIFF_CR_KEPT) + " (" + E.SDIFF_CR_WHY + "), and `"
        + E.COLOUR_CR_KEPT[1] + "` of the " + E.COLOUR_CR_KEPT[0] + " in a file with CR-only line ends ("
        + E.COLOUR_CR_WHY + ").",
        "Not tested: " + ", ".join(f"`{v}`" for v in NOT_TESTED) + ".",
    ]
    return "\n".join(lines)


def test_the_readme_lists_exactly_these_views():
    readme = (Path(__file__).resolve().parents[1] / "docs" / "PRIVACY.md").read_text(encoding="utf-8")
    start, end = "<!-- view-matrix (generated: tests/test_redact_tool_views.py) -->", "<!-- /view-matrix -->"
    assert start in readme and end in readme
    block = readme.split(start, 1)[1].split(end, 1)[0].strip()
    assert " ".join(block.split()) == " ".join(matrix_text().split()), "\n" + matrix_text()


if __name__ == "__main__":       # print the docs/PRIVACY.md paragraph
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    print(matrix_text())
