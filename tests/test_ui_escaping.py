"""Every value the viewer interpolates into HTML (ui/app.js) is escaped, numeric, or markup this file built itself.

A store record is untrusted input: a record with `{"tools": {"length": "<img src=x onerror=...>"}}` once reached
innerHTML through `${(e.tools || []).length}`. This test reads app.js without a JavaScript engine: it finds every
template literal that builds HTML and checks each `${...}` in it against a small grammar of safe forms. A new
unescaped interpolation fails here, in CI, before it reaches a page.

Safe forms (combined freely with `? :`, `||`, `??`, `&&` and `+`): a string, number or template literal (a template
nested in an HTML template is checked as HTML too), a call to one of SAFE_FNS, `xs.map(SAFE_FN or arrow => safe).join("...")`,
and the names in HTML_VARS (variables that hold markup this file built from checked templates; reviewed by hand).
"""
from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "plugin" / "devmem" / "ui" / "app.js"

# Return escaped text, digits, or markup assembled only from escaped parts (their own templates are checked here too).
SAFE_FNS = {
    "esc", "num", "count", "fmt", "human", "val", "md", "mdInline", "renderMd", "wikiHtml", "tag", "fileChip",
    "sessChip", "linkItem", "itemHtml", "diffHtml", "jt", "propsBlock", "resultBlock", "keyInputs", "rawLines",
    "jsonlTimeline", "sessionNote", "noteBar", "crumbs", "barChart", "renderTree", "renderVTree", "secHead",
    "spinner", "vName", "card", "tlHtml", "mdList", "fl",
}
# Variables holding markup built in this file from checked templates or escaped text (each one reviewed by hand).
HTML_VARS = {
    "attrs",      # itemHtml: data-* attributes, every value passed through esc() by the callers
    "body",       # mdList: mdInline() output
    "bars",       # barChart: the <g> elements, numbers through num()
    "lineHtml",   # vFile: one line of highlight() output (esc() per token)
    "para",       # renderMd: mdInline() output
    "tl", "turnsHtml", "live", "why", "note", "edit", "tgt", "extra", "rem", "remU", "fb",
    "h", "head", "keyHtml", "lineTag", "props",
    "gapHtml",    # vSession: the transcript_gaps banner, each note and turn number through esc()
    "nrHtml",     # vSession: the not_recorded banner, its count, note and times through esc()
}
_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")
_SINK = re.compile(r"(?:innerHTML|outerHTML)\s*\+?=\s*$|insertAdjacentHTML\([^,]*,\s*$")


class _Tpl:
    def __init__(self, start: int):
        self.start = start
        self.static: list[str] = []
        self.exprs: list[tuple[int, int]] = []
        self.parent: _Tpl | None = None


def _skip_string(src: str, i: int) -> int:
    q, i = src[i], i + 1
    while src[i] != q:
        i += 2 if src[i] == "\\" else 1
    return i + 1


def _skip_regex(src: str, i: int) -> int:
    i, in_class = i + 1, False
    while True:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            break
        i += 1
    i += 1
    while i < len(src) and src[i].isalpha():
        i += 1
    return i


def _code(src: str, i: int, tpls: list, parent: _Tpl | None, end_on_brace: bool) -> int:
    """Lex JavaScript from i; with end_on_brace, return the index of the `}` that closes an interpolation."""
    depth, prev, n = 0, "", len(src)
    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            i = src.index("*/", i) + 2
            continue
        if c in "'\"":
            i, prev = _skip_string(src, i), "a"
            continue
        if c == "`":
            i, prev = _template(src, i, tpls, parent), "a"
            continue
        if c == "/" and (prev == "" or prev in _REGEX_PREV):
            i, prev = _skip_regex(src, i), "a"
            continue
        if c in "{([":
            depth += 1
        elif c in "})]":
            if depth == 0 and end_on_brace and c == "}":
                return i
            depth -= 1
        prev = "a" if (c.isalnum() or c in "_$") else c
        i += 1
    return i


def _template(src: str, i: int, tpls: list, parent: _Tpl | None) -> int:
    t = _Tpl(i)
    t.parent = parent
    tpls.append(t)
    i, buf = i + 1, []
    while True:
        c = src[i]
        if c == "\\":
            buf.append(src[i:i + 2])
            i += 2
            continue
        if c == "`":
            t.static.append("".join(buf))
            return i + 1
        if src.startswith("${", i):
            end = _code(src, i + 2, tpls, t, True)
            t.exprs.append((i + 2, end))
            buf.append("\x00")
            i = end + 1
            continue
        buf.append(c)
        i += 1


def _templates(src: str) -> list[_Tpl]:
    tpls: list[_Tpl] = []
    _code(src, 0, tpls, None, False)
    return tpls


def _top(e: str):
    """Yield (index, char) of characters of expression `e` outside brackets, strings, templates and regexes."""
    depth, prev, i, n = 0, "", 0, len(e)
    while i < n:
        c = e[i]
        if c in "'\"":
            i, prev = _skip_string(e, i), "a"
            continue
        if c == "`":
            i, prev = _template(e, i, [], None), "a"
            continue
        if c == "/" and (prev == "" or prev in _REGEX_PREV):
            i, prev = _skip_regex(e, i), "a"
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                yield i, c
                i += 1
                prev = c
                continue
        if depth == 0:
            yield i, c
        if not c.isspace():
            prev = "a" if (c.isalnum() or c in "_$") else c
        i += 1


def _close_of(e: str, i: int) -> int:
    """Index of the bracket that closes the one at e[i]."""
    for j, _c in _top(e[i:]):
        return i + j
    return -1


def _strip_parens(e: str) -> str:
    e = e.strip()
    while e.startswith("(") and _close_of(e, 0) == len(e) - 1:
        e = e[1:-1].strip()
    return e


def _split(e: str, ops: tuple[str, ...]) -> list[str]:
    parts, last, skip = [], 0, -1
    for i, _c in _top(e):
        if i < skip:
            continue
        for op in ops:
            if e.startswith(op, i) and not (op == "+" and (e.startswith("++", i) or e[i - 1:i] == "+")) \
                    and not (op == "&&" and e.startswith("&&=", i)) and not (op == "||" and e.startswith("||=", i)):
                parts.append(e[last:i])
                last = skip = i + len(op)
                break
    parts.append(e[last:])
    return parts


def _ternary(e: str):
    q, nest = -1, 0
    for i, c in _top(e):
        if c == "?" and q < 0:
            if e.startswith("?.", i) or e.startswith("??", i) or e[i - 1:i] == "?":
                continue
            q = i
        elif c == "?" and q >= 0 and not (e.startswith("?.", i) or e.startswith("??", i) or e[i - 1:i] == "?"):
            nest += 1
        elif c == ":" and q >= 0:
            if nest:
                nest -= 1
            else:
                return e[:q], e[q + 1:i], e[i + 1:]
    return None


_STR = re.compile(r"""^(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')$""", re.S)
_ARROW = re.compile(r"^(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*=>\s*(.*)$", re.S)
_JOIN = re.compile(r"""\.join\(\s*(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')\s*\)$""", re.S)


def safe(e: str) -> bool:
    e = _strip_parens(e)
    if not e:
        return False
    t = _ternary(e)
    if t:
        return safe(t[1]) and safe(t[2])
    for ops in (("||", "??"), ("+",)):
        parts = _split(e, ops)
        if len(parts) > 1:
            return all(safe(p) for p in parts)
    parts = _split(e, ("&&",))
    if len(parts) > 1:      # a falsy left side prints false, 0, "", null, undefined or NaN
        return safe(parts[-1])
    if _STR.match(e) or re.fullmatch(r"\d+(?:\.\d+)?", e) or e in HTML_VARS:
        return True
    if e.startswith("`") and _template(e, 0, [], None) == len(e):
        return True
    m = re.match(r"[A-Za-z_$][\w$]*", e)
    if m and m.group(0) in SAFE_FNS and e[m.end():m.end() + 1] == "(" and _close_of(e, m.end()) == len(e) - 1:
        return True
    j = _JOIN.search(e)
    if j:
        head = e[:j.start()].rstrip()
        if head.endswith(")"):
            o = next((k for k in range(len(head) - 1, -1, -1) if head[k] == "(" and _close_of(head, k) == len(head) - 1), -1)
            if o > 0 and head[:o].endswith(".map"):
                cb = head[o + 1:-1].strip()
                if cb in SAFE_FNS:
                    return True
                a = _ARROW.match(cb)
                return bool(a) and not a.group(1).lstrip().startswith("{") and safe(a.group(1))
    return False


def html_templates(src: str) -> list[_Tpl]:
    """Templates that build HTML: markup in their text, assigned to an HTML sink, or nested in one of those."""
    tpls = _templates(src)
    html: set[int] = set()
    for t in tpls:
        if "<" in "".join(t.static) or _SINK.search(src[max(0, t.start - 80):t.start]):
            html.add(id(t))
    changed = True
    while changed:
        changed = False
        for t in tpls:
            if id(t) not in html and t.parent is not None and id(t.parent) in html:
                html.add(id(t))
                changed = True
    return [t for t in tpls if id(t) in html]


def unsafe_interpolations(src: str) -> list[str]:
    out = []
    for t in html_templates(src):
        for a, b in t.exprs:
            expr = src[a:b]
            if not safe(expr):
                out.append(f"app.js:{src.count(chr(10), 0, a) + 1}: ${{{' '.join(expr.split())}}}")
    return out


def test_every_html_interpolation_in_app_js_is_escaped_or_numeric():
    bad = unsafe_interpolations(APP_JS.read_text(encoding="utf-8"))
    assert not bad, "unescaped values in HTML (wrap in esc(), num() or count()):\n" + "\n".join(bad)


def test_the_allowlists_name_only_what_app_js_has():
    src = APP_JS.read_text(encoding="utf-8")
    for fn in SAFE_FNS:
        assert re.search(rf"(?:function {fn}\(|const {fn} = )", src), f"SAFE_FNS: {fn} is not defined in app.js"
    for v in HTML_VARS:
        assert re.search(rf"\$\{{{v}\b", src), f"HTML_VARS: {v} is not interpolated in app.js"


def test_the_checker_catches_what_it_should():
    assert unsafe_interpolations("el.innerHTML = `<b>${(e.tools || []).length} tools</b>`;")
    assert unsafe_interpolations("x.innerHTML = `${name}`;")
    assert unsafe_interpolations("h += `<i>${a ? b : esc(c)}</i>`;")
    assert unsafe_interpolations("h += `<i>${xs.map((x) => x.name).join('')}</i>`;")
    assert unsafe_interpolations("h += `<i>${cond ? `S${n}` : ''}</i>`;")   # a nested template is HTML too
    assert not unsafe_interpolations("h += `<i>${esc(a)} ${num(b)} ${c ? '<b>' : ''} ${xs.map(fileChip).join('')}</i>`;")
    assert not unsafe_interpolations("h += `<i>${xs.map((x) => `<b>${esc(x)}</b>`).join('') || '<p>none</p>'}</i>`;")
    assert not unsafe_interpolations("const re = /[`'\"]/g; t.textContent = `${a} files`;")   # text, not HTML


def test_props_values_are_escaped_unless_marked_as_markup():
    src = APP_JS.read_text(encoding="utf-8")
    # the right-hand panel and note properties escape every value that is not explicitly wrapped in H(...)
    for fn in ("setSide", "propsBlock"):
        body = src[src.index(f"function {fn}("):]
        body = body[:body.index("\n}\n")]
        assert '<span class="v">${val(v)}</span>' in body, fn
