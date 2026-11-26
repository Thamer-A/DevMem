"""A diff that edits a value (verifier v14b cases_v14a.py, f14_e2e.py: block values kept in 12 of 12 git diffs, list-item
blocks in 12 of 12, and 6 of the 7 values that reached the store through real hooks). The name sits on an unchanged
context line and the old and new values on `-` and `+` lines, so in the text they interleave. Redactor._diff_sides reads
each hunk as its old text and its new text. Every view here is real output: the files are committed to a temporary git
repository and the view is what `git diff` (and the other commands named) print; the Edit tool's structuredPatch is
built from that real hunk. Each view goes through Redactor.text and through the PostToolUse hook entry point (the
store's content is searched), and no 8 consecutive characters of the old or the new value may remain."""
from __future__ import annotations

import json
import sys
import re
import time
from pathlib import Path

import pytest

from devmem import redact as R
from devmem.redact import Redactor

from redact_view_helpers import ENDINGS, bash_stored, git, leaks, run, stored, with_endings

OLD = "Hq7RtW2xLm9KpB4nVc8sYd3F"
NEW = "Zx5NvB8mQw1ErT6yUi3oPa9S"
MID = "Lk3JhG6fDs9AqW2eRt5YuI8O"     # the second parent's value in a merge


def _pem(v: str) -> str:
    return ("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA" + v + "\n" + v[::-1] + v + "\n"
            "-----END RSA PRIVATE KEY-----")


# value shape -> the file holding value v (every shape keeps its name on lines the edit does not change)
SHAPES = {
    "line pair": lambda v: f"db:\n  host: localhost\n  password: {v}\n  user: app\n  port: 5432\n",
    "block scalar": lambda v: f"tls:\n  cert: x.pem\n  private_key: |\n    {v}\n    {v[::-1]}\n  user: app\n",
    "list-item block": lambda v: f"users:\n  - password: |\n      {v}\n    name: bob\n    role: admin\n",
    "PEM body": lambda v: "key follows\n" + _pem(v) + "\ndone\n",
    "triple-quoted": lambda v: f'import os\napi_key = """\n{v}\n{v[::-1]}\n"""\nport = 80\n',
    "quoted value over lines": lambda v: f'db:\n  password: "{v[:12]}\n    {v[12:]}"\n  user: app\n',
    "JSON value on its own line": lambda v: '{\n  "api_key":\n    "' + v + '",\n  "port": 80\n}\n',
    "backslash continuation": lambda v: f"LOG=1\nAPI_KEY={v[:12]}\\\n{v[12:]}\nPORT=1\n",
    "TOML multi-line": lambda v: f"[auth]\nsecret = '''\n{v}\n'''\nport = 80\n",
    "XML element over lines": lambda v: f"<cfg>\n  <password>\n    {v}\n  </password>\n</cfg>\n",
    "k8s name/value": lambda v: f"env:\n  - name: DB_PASSWORD\n    value: {v}\n  - name: LOG_LEVEL\n    value: info\n",
    "24-line block scalar": lambda v: "tls:\n  private_key: |\n" + "".join(f"    {v[i:]}{v[:i]}\n" for i in range(24))
                                      + "  user: app\n",
    "20-line PEM body": lambda v: ("-----BEGIN RSA PRIVATE KEY-----\n" + "".join(f"{v[i:]}{v[:i]}{v}\n" for i in range(20))
                                   + "-----END RSA PRIVATE KEY-----\n"),
}

# views of an edit from OLD to NEW: (command line as the tool call states it, argv of the real tool)
GIT_VIEWS = {
    "git diff": ["diff", "HEAD~1", "HEAD"],
    "git diff -U1": ["diff", "-U1", "HEAD~1", "HEAD"],
    "git diff -U10": ["diff", "-U10", "HEAD~1", "HEAD"],
    "git diff --color=always": ["diff", "--color=always", "HEAD~1", "HEAD"],
    "git show": ["show", "HEAD"],
    "git log -p": ["log", "-p"],
    "git diff --word-diff": ["diff", "--word-diff", "HEAD~1", "HEAD"],
}
VIEWS = list(GIT_VIEWS) + ["diff -u", "diff -y", "diff -y -W200", "grep -n on a patch", "cat -n on a patch",
                           "Read tool on a patch", "Edit structuredPatch", "structuredPatch as JSON text",
                           "git show --cc of a merge"]
FILE = "settings.conf"


def _file(shape: str, ending: str, v: str) -> str:
    """The file of `shape` holding value v, with LF, CRLF, CR-only or mixed line ends."""
    return with_endings(SHAPES[shape](v), ending)


def _repo(tmp: Path, shape: str, ending: str = "lf") -> Path:
    d = tmp / "r"
    d.mkdir()
    git(d, "init", "-q")
    (d / "README").write_bytes(b"x\n")
    for i, v in enumerate((None, OLD, NEW)):
        if v is not None:
            (d / FILE).write_bytes(_file(shape, ending, v).encode())
        git(d, "add", "-A")
        git(d, "commit", "-qm", f"c{i}")
    return d


def _hunks(patch: str) -> list[dict]:
    """The hunks of a real unified diff in the shape of Claude Code's Edit structuredPatch."""
    out, cur = [], None
    for ln in patch.split("\n"):
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", ln)
        if m:
            cur = {"oldStart": int(m.group(1)), "oldLines": int(m.group(2) or 1), "newStart": int(m.group(3)),
                   "newLines": int(m.group(4) or 1), "lines": []}
            out.append(cur)
        elif cur is not None and ln[:1] in (" ", "+", "-", "\\"):
            cur["lines"].append(ln)
    return out


def _merge_view(tmp: Path, shape: str, ending: str = "lf") -> str:
    """A combined diff (`@@@`): two branches set the value to OLD and MID, the merge resolves it to NEW."""
    d = tmp / "m"
    d.mkdir()
    git(d, "init", "-q")
    (d / FILE).write_bytes(_file(shape, ending, "Base0Base0Base0Base0Base0").encode())
    git(d, "add", "-A")
    git(d, "commit", "-qm", "base")
    git(d, "checkout", "-q", "-b", "other")
    (d / FILE).write_bytes(_file(shape, ending, MID).encode())
    git(d, "commit", "-qam", "other")
    git(d, "checkout", "-q", "main")
    (d / FILE).write_bytes(_file(shape, ending, OLD).encode())
    git(d, "commit", "-qam", "main")
    git(d, "merge", "-q", "other", ok=(0, 1))
    (d / FILE).write_bytes(_file(shape, ending, NEW).encode())
    git(d, "add", "-A")
    git(d, "commit", "-qm", "merge")
    return git(d, "show", "--cc", "HEAD")


def view(tmp: Path, shape: str, name: str, ending: str = "lf") -> tuple[str, str | None, dict | None]:
    """(command, output) of a real tool, or (command, None, Edit response) for the structuredPatch view."""
    if name == "git show --cc of a merge":
        return "git show --cc HEAD", _merge_view(tmp, shape, ending), None
    d = _repo(tmp, shape, ending)
    if name in GIT_VIEWS:
        return name, git(d, *GIT_VIEWS[name]), None
    patch = git(d, "diff", "HEAD~1", "HEAD")
    if name == "diff -u":
        (d / "old.conf").write_bytes(_file(shape, ending, OLD).encode())
        return "diff -u old.conf settings.conf", run(d, ["diff", "-u", "old.conf", FILE], ok=(1,)), None
    if name.startswith("diff -y"):
        (d / "old.conf").write_bytes(_file(shape, ending, OLD).encode())
        argv = name.split() + ["old.conf", FILE]
        return " ".join(argv), run(d, argv, ok=(1,)), None
    (d / "edit.patch").write_bytes(patch.encode())
    if name == "grep -n on a patch":
        return "grep -n '' edit.patch", run(d, ["grep", "-n", "", "edit.patch"]), None
    if name == "cat -n on a patch":
        return "cat -n edit.patch", run(d, ["cat", "-n", "edit.patch"]), None
    if name == "Read tool on a patch":
        return "Read edit.patch", None, {"type": "text", "file": {"filePath": str(d / "edit.patch"), "content": patch}}
    if name == "Edit structuredPatch":
        return "Edit", None, {"filePath": str(d / FILE), "oldString": "x", "newString": "y",
                              "structuredPatch": _hunks(patch)}
    if name == "structuredPatch as JSON text":
        return "cat edit.json", json.dumps({"structuredPatch": _hunks(patch)}, indent=2), None
    raise AssertionError(name)


def _check(tmp: Path, shape: str, name: str, ending: str = "lf") -> None:
    command, out, response = view(tmp, shape, name, ending)
    values = [v for v in (OLD, NEW, MID) if v[:12] in (out if out is not None else json.dumps(response))]
    assert values, f"the view holds no value: {out!r}"
    if out is not None:
        red = Redactor(None).text(out)
        assert not leaks(red, values), red
        kept = bash_stored(tmp, command, out)
    elif command == "Edit":
        assert not leaks(json.dumps(Redactor(None).record(response)), values)
        kept = stored(tmp, "Edit", {"file_path": response["filePath"], "old_string": "x", "new_string": "y"},
                      response)
    else:
        kept = stored(tmp, "Read", {"file_path": response["file"]["filePath"]}, response)
    assert not leaks(kept, values), kept[-3000:]


# Git for Windows' grep drops the carriage returns of a line: of a file with CR-only line ends (one line to git and
# grep) nothing of its line breaks is left, and a block scalar or a continued value is one line of words
GREP_DROPS_CR = {"block scalar", "list-item block", "backslash continuation", "24-line block scalar"}
# `diff -y` of a file with CRLF or mixed line ends: the first reading of a text with returns inside its lines takes
# the left column's `\r` for a line end, and a value over lines then runs on into the right column, so the row is no
# longer two columns when the text is read again as it is (b35165e, a017af9 and e8c24da keep these too)
SDIFF_CR_KEPT = {"triple-quoted", "backslash continuation", "TOML multi-line"}
SDIFF_CR_WHY = ("the first reading takes the left column's return for a line end, and the value over lines runs on "
                "into the right column")
# a coloured `git diff` of a 20-line key in a file with CR-only line ends (one diff line holding the whole file): once
# the returns are read as line ends only the first line carries the colour and the marker, the reading of decorated
# lines replaces the BEGIN line alone, and the body is then no key without a header (b35165e passed only because its
# budget ran out on this small text and replaced the whole line)
COLOUR_CR_KEPT = ("20-line PEM body", "git diff --color=always", "cr")
COLOUR_CR_WHY = "the BEGIN line is replaced alone by the reading of decorated lines; the body then has no header"


def _edit_cases():
    for shape in SHAPES:
        for name in VIEWS:
            for ending in ENDINGS:
                marks = []
                if sys.platform == "win32" and name == "grep -n on a patch" and ending == "cr" and shape in GREP_DROPS_CR:
                    marks = [pytest.mark.xfail(strict=True, reason="grep on Windows drops the returns: no line breaks")]
                if name.startswith("diff -y") and ending in ("crlf", "mixed") and shape in SDIFF_CR_KEPT:
                    marks = [pytest.mark.xfail(strict=True, reason=SDIFF_CR_WHY)]
                if (shape, name, ending) == COLOUR_CR_KEPT:
                    marks = [pytest.mark.xfail(strict=True, reason=COLOUR_CR_WHY)]
                yield pytest.param(shape, name, ending, marks=marks, id=f"{shape}-{name}-{ending}")


@pytest.mark.parametrize("shape,name,ending", list(_edit_cases()))
def test_an_edited_value_is_redacted_in_every_diff_view(tmp_path, shape, name, ending):
    """The file is written with LF, CRLF, CR-only (git shows it as one line) or mixed line ends."""
    _check(tmp_path, shape, name, ending)


# ---- plain diffs keep working, benign diffs are left alone ----

@pytest.mark.parametrize("text", [
    "+password=Ab12Cd34Ef56Gh78\n",
    "-password=Ab12Cd34Ef56Gh78\n+password=Zx98Yw76Vu54Ts32\n",
    "@@ -1,2 +1,2 @@\n x = 1\n-API_KEY=Ab12Cd34Ef56Gh78\n+API_KEY=Zx98Yw76Vu54Ts32\n",
])
def test_a_changed_line_that_holds_its_own_name_is_redacted_as_before(text):
    out = Redactor(None).text(text)
    assert "Ab12Cd34" not in out and "Zx98Yw76" not in out, out
    assert out.count("\n") == text.count("\n")
    assert all(a[:1] == b[:1] for a, b in zip(text.split("\n"), out.split("\n")))      # the markers stay


REPO = Path(__file__).resolve().parents[1]
BENIGN = ["plugin/devmem/scan.py", "plugin/devmem/text.py", "plugin/devmem/extract_python.py", "SECURITY.md",
          "pyproject.toml"]


def _edited(text: str) -> str:
    """An ordinary edit of a file: every 7th line changed, every 13th removed, a line added after every 11th."""
    out = []
    for i, ln in enumerate(text.split("\n")):
        if i % 13 == 12:
            continue
        out.append(ln + " x" if i % 7 == 6 and ln.strip() else ln)
        if i % 11 == 10:
            out.append(ln.split("#")[0].rstrip() + "  # added")
    return "\n".join(out)


@pytest.mark.parametrize("rel", BENIGN)
def test_ordinary_edits_of_this_repository_change_nothing(tmp_path, rel):
    old = (REPO / rel).read_text(encoding="utf-8")
    new = _edited(old)
    red = Redactor(None)
    assert red.text(old) == old and red.text(new) == new, f"{rel}: the file itself is redacted"
    d = tmp_path / "r"
    d.mkdir()
    git(d, "init", "-q")
    (d / "f").write_bytes(old.encode())
    git(d, "add", "-A")
    git(d, "commit", "-qm", "a")
    (d / "f").write_bytes(new.encode())
    git(d, "commit", "-qam", "b")
    for argv in (["diff", "HEAD~1", "HEAD"], ["diff", "-U1", "HEAD~1", "HEAD"], ["show", "HEAD"], ["log", "-p"]):
        out = git(d, *argv)
        assert red.text(out) == out, argv
        assert red.hits.get("diff-unmapped") is None
    patch = git(d, "diff", "HEAD~1", "HEAD")
    hunks = _hunks(patch)
    assert red.record(hunks) == hunks


@pytest.mark.parametrize("text", [
    "name\t|\tvalue\nport\t|\t8080\nmode\t|\tfast\n",                                # a table with tabs and pipes
    "- item one\n  more about it\n- item two\n",                                    # a Markdown list
    "@@ -1 +1 @@\nnot a diff line\n",                                               # a header and no hunk
    "a <\tb\nc >\td\n",                                                              # marks at different columns
])
def test_text_that_only_looks_like_a_diff_or_columns_is_left_alone(text):
    red = Redactor(None)
    assert red.text(text) == text and not red.hits


# ---- what the side reading does at its edges ----

def test_a_hunk_whose_sides_change_their_line_count_fails_closed(monkeypatch):
    """A side that comes back with another number of lines cannot be mapped: every line of the hunk is replaced (no
    built-in rule changes a line count; this is the safety net)."""
    red = Redactor(None)
    monkeypatch.setattr(red, "_side_texts", lambda pieces, depth: [p + "\nextra" for p in pieces])
    text = "@@ -1,3 +1,3 @@\n db:\n-  x: 1\n+  x: 2\n   y: 3\n"
    out = red._diff_sides(text, 0)
    assert out.split("\n")[1:5] == [" " + R.DIFF_FAILED, "-" + R.DIFF_FAILED, "+" + R.DIFF_FAILED,
                                    " " + R.DIFF_FAILED], out
    assert red.hits.get("diff-unmapped") == 1


def test_hunks_over_the_bounds_fail_closed_on_their_changed_lines(monkeypatch):
    monkeypatch.setattr(R, "MAX_DIFF_HUNK_LINES", 3)
    red = Redactor(None)
    text = ("@@ -1,2 +1,2 @@\n a: 1\n-b: 2\n+b: 3\n"
            "@@ -1,4 +1,4 @@\n c: 1\n d: 1\n-e: 2\n+e: 3\n")
    out = red._diff_sides(text, 0).split("\n")
    assert out[1:4] == [" a: 1", "-b: 2", "+b: 3"]          # within the bound: read as sides, nothing to redact
    assert out[5:9] == [" c: 1", " d: 1", "-" + R.DIFF_FAILED, "+" + R.DIFF_FAILED]
    monkeypatch.setattr(R, "MAX_DIFF_HUNK_LINES", 100)
    monkeypatch.setattr(R, "MAX_DIFF_HUNKS", 1)
    out = Redactor(None)._diff_sides(text, 0).split("\n")
    assert out[1:4] == [" a: 1", "-b: 2", "+b: 3"] and out[7:9] == ["-" + R.DIFF_FAILED, "+" + R.DIFF_FAILED]


def test_the_lines_of_a_hunk_header_and_no_newline_markers():
    """`\\ No newline at end of file` belongs to neither side and stays; the hunk header's counts end the hunk, so a
    line after it is read as text again; a key changed on both sides is read on each side."""
    text = ("@@ -1,2 +1,2 @@\n password: |\n-  " + OLD + "\n\\ No newline at end of file\n+  " + NEW
            + "\n\\ No newline at end of file\nafter the hunk\n")
    out = Redactor(None).text(text)
    assert not leaks(out, [OLD, NEW]), out
    assert "\\ No newline at end of file" in out and "after the hunk" in out
    text = "@@ -1,2 +1,2 @@\n-password: |\n-  " + OLD + "\n+password: |\n+  " + NEW + "\n"
    assert not leaks(Redactor(None).text(text), [OLD, NEW])


def test_rename_and_binary_headers_are_left_to_the_ordinary_rules():
    text = ("diff --git a/a.yaml b/b.yaml\nsimilarity index 90%\nrename from a.yaml\nrename to b.yaml\n"
            "Binary files a/x.bin and b/x.bin differ\n@@ -1,3 +1,3 @@\n db:\n   password: |\n-    " + OLD
            + "\n+    " + NEW + "\n")
    out = Redactor(None).text(text)
    assert not leaks(out, [OLD, NEW]) and out.startswith("diff --git a/a.yaml b/b.yaml\nsimilarity index 90%\n")


def test_a_hunk_cut_at_its_head_keeps_what_has_no_name_in_view():
    """A known gap, stated by this test: a hunk whose head was cut (a tail cap, `git diff -U0`) has no name in view,
    so a block body alone is not known to be a secret."""
    out = Redactor(None).text("-    " + OLD + "\n+    " + NEW + "\n   user: app\n")
    assert leaks(out, [OLD, NEW]) == [OLD, NEW]


# ---- cost ----

# one generous ceiling of CPU time at 512 KB for these texts (the side readings are bounded by the call's budget:
# tests/test_redact_read_budget.py); what the tests below prove is the growth from the small text to the big one
SIDES_BOUND_S = 20.0


def _best(text: str) -> float:
    """CPU time of this process (time.process_time: other processes do not count), best of up to three."""
    best = 1e9
    for _ in range(3):
        t = time.process_time()
        Redactor(None).text(text)
        best = min(best, time.process_time() - t)
        if best < SIDES_BOUND_S:
            break
    return best


DIFF_TEXTS = {
    "one long hunk": lambda n: "@@ -1,%d +1,%d @@\n" % (n, n) + " password: |\n" + "-  Ab12Cd34Ef\n+  Zx98Yw76Vu\n" * n,
    "many small hunks": lambda n: "@@ -1,2 +1,2 @@\n password: |\n-  Ab12Cd34Ef\n+  Zx98Yw76Vu\n" * n,
    "runs without header": lambda n: " password: |\n-  Ab12Cd34Ef\n+  Zx98Yw76Vu\nx\n" * n,
    "combined diff": lambda n: "@@@ -1,2 -1,2 +1,2 @@@\n  password: |\n- Ab12Cd34\n -  Ef56Gh78\n++ Zx98Yw76\n" * n,
    "grep -n over a diff": lambda n: "".join(f"{i}:{x}\n" for i, x in enumerate(
        (" password: |", "-  Ab12Cd34Ef", "+  Zx98Yw76Vu") * n, 1)),
    "context only": lambda n: " a: 1\n" * (3 * n) + "-b\n",
    # hostile shapes for each new reading
    "one-line hunks": lambda n: "@@ -1 +1 @@\n-a\n+b\n" * n,
    "headers without lines": lambda n: "@@ -1,5 +1,5 @@\n" * n,
    "combined diff of 8 parents": lambda n: "@@@@@@@@@ -1 -1 -1 -1 -1 -1 -1 -1 +1 @@@@@@@@@\n++++++++ x\n" * n,
    # (with context lines and a removed line per parent: nine sides of each hunk, review v15b)
    "combined diff of 8 parents with context": lambda n: "".join(
        "@@@@@@@@@ -1,3 -1,3 -1,3 -1,3 -1,3 -1,3 -1,3 -1,3 +1,3 @@@@@@@@@\n" + " " * 8 + f"ctx = {i}  # note\n"
        + " " * (i % 8) + "-" + " " * (7 - i % 8) + f"old_{i} = 1\n" + "+" * 8 + f"new_{i} = 2\n" for i in range(n)),
    "word diff, many markers": lambda n: ("@@ -1 +1 @@\n" + "[-a-]{+b+} " * 50 + "\n") * n,
    "word diff, unclosed markers": lambda n: ("@@ -1 +1 @@\n" + "[-" * 200 + "{+" * 200 + "\n") * n,
    "diff -y": lambda n: "password: |\t\t\t\t\t\t\t\tpassword: |\n  Ab12Cd34\t\t\t\t\t      |\t  Zx98Yw76\n" * n,
    "diff -y-like tab columns": lambda n: "a\t|\tb\t|\tc\t|\td\t|\te\n" * n,
    "unclosed quote on every line": lambda n: 'password: "abc\n' * n,
    "unclosed quote closed far away": lambda n: ('password: "abc\n' + "x" * 200 + "\n") * n + '"\n',
    "backslash on every line": lambda n: "API_KEY=abc\\\n" * n,
    "sibling keys before a value": lambda n: ('{"Name": "DB_PASSWORD", ' + '"k": "v", ' * 6 + '"x": 1}\n') * n,
    "sibling keys and no value": lambda n: ('"name": "a", ' + '"k1": "v", ' * 200 + "\n") * n,
    "git blame": lambda n: "".join(f"abc1234 (t 2024-01-01 10:00:00 +0000 {i}) x\n" for i in range(1, n + 1)),
}


@pytest.mark.parametrize("name", list(DIFF_TEXTS))
def test_the_side_reading_grows_linearly(name):
    """Each hunk's sides are redacted once, together: the cost follows the text (512 KB under SIDES_BOUND_S, best of three, and
    eight times the text costs at most about twelve times as much)."""
    unit = len(DIFF_TEXTS[name](8)) // 8
    small, big = DIFF_TEXTS[name](64_000 // unit), DIFF_TEXTS[name](512_000 // unit)
    assert 400_000 <= len(big) <= 700_000, len(big)
    t1, t2 = _best(small), _best(big)
    assert t2 < SIDES_BOUND_S, f"{name}: {t2:.2f}s on {len(big)} characters"
    assert t2 < 12 * max(t1, 0.05), f"{name}: {t1:.3f}s -> {t2:.3f}s"


def test_a_hundred_thousand_one_line_hunks_cost_what_their_text_costs():
    """100,000 hunks (1.7 MB) against 12,500: the per-hunk work is a constant (no hunk is read twice)."""
    unit = "@@ -1 +1 @@\n-a\n+b\n"
    t1, t2 = _best(unit * 12_500), _best(unit * 100_000)
    assert t2 < 12 * max(t1, 0.05), f"{t1:.3f}s -> {t2:.3f}s"


def test_hunks_at_and_over_the_bounds_cost_what_their_text_costs(monkeypatch):
    """With the bounds lowered so that most hunks are over them (failed closed without being read), the cost still
    follows the text."""
    monkeypatch.setattr(R, "MAX_DIFF_HUNKS", 100)
    monkeypatch.setattr(R, "MAX_DIFF_HUNK_LINES", 2)
    unit = "@@ -1,2 +1,2 @@\n password: |\n-  Ab12Cd34Ef\n+  Zx98Yw76Vu\n"
    t1, t2 = _best(unit * 1_200), _best(unit * 9_600)
    assert t2 < SIDES_BOUND_S and t2 < 12 * max(t1, 0.05), f"{t1:.3f}s -> {t2:.3f}s"
