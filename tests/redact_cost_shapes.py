"""Texts for the cost tests of the redaction readings (tests/test_redact_read_budget.py): a base text of distinct lines
(settings, code, comments, a YAML block, values) decorated by the shapes tools give text, stacked: a unified diff, a
combined diff of 2 to 8 parents, a word diff, `diff -y`, `grep -n` numbering, JSON string escaping (groups of lines as
one string each, on lines of their own: a diff whose context lines hold JSON strings), marked runs without a header,
YAML list items. Every line differs from every other (no reading can be saved by reading a repeated text once), and
in a combined diff each parent's old side differs. All values are synthetic."""
from __future__ import annotations

import json


def base_lines(n: int, salt: str = "") -> list[str]:
    out = []
    for i in range(n):
        k = i % 8
        if k == 0:
            out.append(f"service_{salt}{i}:")
        elif k == 1:
            out.append(f"  password: Xk4{salt}{i}mLp9QrT2b  # rotated")
        elif k == 2:
            out.append("  private_key: |")
        elif k == 3:
            out.append(f"    MIIEv{salt}{i}AIBADANBgkqhkiG9w0BAQ")
        elif k == 4:
            out.append(f"  url: https://example.invalid/{salt}{i}  // endpoint")
        elif k == 5:
            out.append(f'  api_key = "Zq{salt}{i}Wd7Lk2Pn5Rx8"')
        elif k == 6:
            out.append(f"  # note {salt}{i}: value = compute(a, b) - offset")
        else:
            out.append(f'  "token_{salt}{i}": "Hb3{salt}{i}Vn8Kt1Mz",')
    return out


def unified(lines: list[str]) -> list[str]:
    body = []
    for i, ln in enumerate(lines):
        if i % 3 == 1:
            body += ["-" + ln, "+" + ln + "x"]
        else:
            body.append(" " + ln)
    old = sum(1 for b in body if b[0] in " -")
    new = sum(1 for b in body if b[0] in " +")
    return [f"@@ -1,{old} +1,{new} @@"] + body


def combined(parents: int):
    def deco(lines: list[str]) -> list[str]:
        body = []
        for i, ln in enumerate(lines):
            if i % 3 == 1:
                p = i % parents          # the parent this line is removed from (each parent's old side differs)
                body.append(" " * p + "-" + " " * (parents - p - 1) + ln)
                body.append("+" * parents + ln + "y")
            else:
                body.append(" " * parents + ln)
        at = "@" * (parents + 1)
        return [f"{at} " + " ".join(["-1,999999"] * parents) + f" +1,999999 {at}"] + body
    return deco


def word(lines: list[str]) -> list[str]:
    out = ["@@ -1,999999 +1,999999 @@"]
    for i, ln in enumerate(lines):
        out.append(f"{ln} [-o{i}-]{{+n{i}+}}" if i % 2 else ln)
    return out


def sdiff(lines: list[str]) -> list[str]:
    out = []
    for i, ln in enumerate(lines):
        left = ln.replace("\t", " ")[:60]
        mark = "|" if i % 3 == 1 else " "
        right = left + ("z" if mark == "|" else "")
        pad = left.ljust(61) + " " + mark if mark != " " else left.ljust(63)
        out.append(pad + "\t" + right if mark != " " else left.ljust(64) + right)
    return out


def numbered(lines: list[str]) -> list[str]:
    return [f"f.txt:{i}:{ln}" for i, ln in enumerate(lines, 1)]


def json_groups(lines: list[str]) -> list[str]:
    """Groups of 6 lines, each group one JSON string on a line of its own."""
    return [f"  v{i} = " + json.dumps("\n".join(lines[i:i + 6])) for i in range(0, len(lines), 6)]


def headerless(lines: list[str]) -> list[str]:
    return [(" ", "+", "-")[i % 3] + ln for i, ln in enumerate(lines)]


def yaml_items(lines: list[str]) -> list[str]:
    return [("- " if i % 4 == 0 else "  ") + ln for i, ln in enumerate(lines)]


DECORATIONS = {
    "unified": unified,
    "combined2": combined(2),
    "combined3": combined(3),
    "combined8": combined(8),
    "word": word,
    "sdiff": sdiff,
    "numbered": numbered,
    "json": json_groups,
    "headerless": headerless,
    "yaml_items": yaml_items,
}


def stacked(names: tuple[str, ...], size: int) -> str:
    """The base text decorated by each of `names` in turn (the first is innermost), about `size` characters."""
    def build(n: int) -> str:
        lines = base_lines(n)
        for nm in names:
            lines = DECORATIONS[nm](lines)
        return "\n".join(lines) + "\n"
    n = 64
    t = build(n)
    n = max(8, int(n * size / max(len(t), 1)))
    t = build(n)
    return t
