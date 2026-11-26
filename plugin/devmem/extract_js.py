"""JavaScript / TypeScript extractor (regexes over comment-stripped code; no dependencies).

Declarations: function/class declarations, `const f = (...) =>` / `function` expressions, TS
interface/type/enum. Dependencies: static `import ... from`, side-effect imports, `export ... from`,
`require()` and dynamic `import()`. Template literals that span lines are not treated as strings, so
a `//` inside one can be mistaken for a comment; that only loses symbols, never invents them.
"""
from __future__ import annotations

import re

from .extract import (OP_DB, OP_ENV, OP_FILEIO, OP_NET, OP_PRINT, OP_PROC, OP_SQL, SQL_RE, Extractor, count_op,
                      language_of, new_info, strip_comments)

ID = r"[A-Za-z_$][\w$]*"
# Linear in the file's length: every run that may cross a newline is bounded (`\s{1,64}`, `[^)]{0,300}`) and no two
# adjacent quantified classes overlap (`function\s*\*?\s*` was quadratic on `function` + a long whitespace run).
_S = r"\s{1,64}"
DECL = [
    re.compile(rf"(?:^|[\s;(])(?:export{_S}(?:default{_S})?)?(?:async{_S})?function(?:\s{{0,64}}\*)?\s{{0,64}}"
               rf"({ID})\s{{0,64}}[<(]", re.M),
    re.compile(rf"(?:^|[\s;])(?:export{_S}(?:default{_S})?)?(?:abstract{_S})?class{_S}({ID})", re.M),
    re.compile(rf"^[ \t]*(?:export{_S})?(?:const|let|var){_S}({ID})[ \t]{{0,64}}(?::[^=\n]{{1,300}})?=\s{{0,64}}"
               rf"(?:async{_S})?(?:function\b|\([^)]{{0,300}}\)\s{{0,64}}(?::[^=\n]{{1,300}})?=>|{ID}\s{{0,64}}=>)", re.M),
    re.compile(rf"^[ \t]*(?:export{_S})?(?:declare{_S})?(?:interface|enum|type){_S}({ID})\b", re.M),
]
# `import <clause> from '<spec>'`, read by _imports_from: each `import` owns the text up to the next one (at most
# IMPORT_WINDOW characters), where the first `from '...'` that follows a clause character ends the clause. One regex
# (`\s+` then a lazy clause whose class also held `\s`) split every whitespace run in quadratically many ways:
# 256 KB of `import` + 1999 x " \n" took 33 s.
IMPORT_WORD = re.compile(r"\bimport\b")
FROM_TAIL = re.compile(r"""(?<=[\w$*}])\s+from\s+['"]([^'"\n]+)['"]""")
CLAUSE = re.compile(r"[\w$*{}\s,]*")
TYPE_PREFIX = re.compile(r"type\s+(?=\S)")
IMPORT_WINDOW = 2100
IMPORT_SIDE = re.compile(r"""\bimport\s+['"]([^'"\n]+)['"]""")
EXPORT_FROM = re.compile(r"""\bexport\s+(?:type\s+)?(?:\*(?:\s+as\s+\w+)?|\{[^{}]{0,2000}\})\s*from\s+['"]([^'"\n]+)['"]""")
REQUIRE = re.compile(r"""\brequire\s*\(\s*['"]([^'"\n]+)['"]\s*\)""")
DYN_IMPORT = re.compile(r"""\bimport\s*\(\s*['"]([^'"\n]+)['"]\s*\)""")
CONST_REQUIRE = re.compile(rf"""\b(?:const|let|var)\s+(?:({ID})|\{{([^{{}}]{{0,2000}})\}})\s*=\s*require\s*\(\s*['"]([^'"\n]+)['"]""")
# starts only where a name starts: `\b` also fires after every `$` inside `a$a$a...`, and each start re-read the rest
MEMBER_CALL = re.compile(rf"(?<![\w$])({ID})\.({ID})\s{{0,64}}\(")
OPS = [
    (OP_DB, re.compile(r"\b(?:mongoose\.connect|createConnection|createPool|new\s+(?:Pool|PrismaClient|Sequelize|"
                       r"MongoClient|Database|DataSource)|knex)\s*\(")),
    (OP_ENV, re.compile(r"\bprocess\.env\b|\bimport\.meta\.env\b|\bDeno\.env\b")),
    (OP_NET, re.compile(r"(?<![\w$.])fetch\s*\(|\baxios(?:\.\w+)?\s*\(|\b(?:http|https)\.(?:request|get)\s*\(|"
                        r"new\s+(?:WebSocket|XMLHttpRequest|EventSource)\s*\(")),
    (OP_PROC, re.compile(r"""['"](?:node:)?child_process['"]|\bBun\.spawn\b|\bDeno\.(?:run|Command)\b""")),
    (OP_PRINT, re.compile(r"\bconsole\.(?:log|info|warn|error|debug)\s*\(")),
]
FS_CALL = re.compile(r"\.(?:readFileSync|writeFileSync|readFile|writeFile|appendFileSync|appendFile|"
                     r"createReadStream|createWriteStream)\s*\(")


def _imports_from(code: str) -> list[tuple[str, str]]:
    """(clause, spec) of every `import <clause> from '<spec>'`. Linear: the windows do not overlap, and in each one
    FROM_TAIL starts only after a clause character, so a whitespace run is read once."""
    words = [m.end() for m in IMPORT_WORD.finditer(code)]
    nexts = [m - len("import") for m in words[1:]] + [len(code)]
    out = []
    for s, nxt in zip(words, nexts):
        end = min(nxt, s + IMPORT_WINDOW)
        if s >= end or not code[s].isspace():
            continue
        fm = FROM_TAIL.search(code, s + 1, end)
        if not fm or CLAUSE.fullmatch(code, s, fm.start()) is None:
            continue
        clause = code[s:fm.start()].lstrip()
        tm = TYPE_PREFIX.match(clause)
        out.append((clause[tm.end():] if tm else clause, fm.group(1)))
    return out


def _parse_clause(clause: str, spec: str, aliases: dict, uses: set) -> None:
    clause = clause.strip()
    ns = re.search(rf"\*\s+as\s+({ID})", clause)
    if ns:
        aliases[ns.group(1)] = spec
    head = clause.split("{", 1)[0].split(",", 1)[0].strip()
    if re.fullmatch(ID, head or "") and head != "type":
        aliases[head] = spec
    named = re.search(r"\{([^{}]*)\}", clause)
    if named:
        for part in named.group(1).split(","):
            name = part.strip().removeprefix("type ").split(" as ")[0].strip()
            if re.fullmatch(ID, name or ""):
                uses.add((spec, name))


class JsExtractor(Extractor):
    name = "js-regex"

    def extract(self, rel: str, text: str) -> tuple[dict, dict]:
        info, facts = new_info(rel, language_of(rel))
        code = strip_comments(text, "//", ("/*", "*/"), quotes="\"'`")
        syms = set()
        for rx in DECL:
            syms.update(rx.findall(code))
        info["symbols"] = sorted(syms)
        deps, aliases, uses = set(), {}, set()
        for clause, spec in _imports_from(code):
            deps.add(spec)
            _parse_clause(clause, spec, aliases, uses)
        for rx in (IMPORT_SIDE, EXPORT_FROM, REQUIRE, DYN_IMPORT):
            deps.update(rx.findall(code))
        for single, destructured, spec in CONST_REQUIRE.findall(code):
            if single:
                aliases[single] = spec
            for part in destructured.split(","):
                name = part.split(":")[0].strip()
                if re.fullmatch(ID, name or ""):
                    uses.add((spec, name))
        for obj, fn in MEMBER_CALL.findall(code):
            if obj in aliases:
                uses.add((aliases[obj], fn))
        info["deps"] = sorted(deps)
        facts["uses"] = sorted([s, n] for s, n in uses)
        for label, rx in OPS:
            count_op(facts, label, len(rx.findall(code)))
        if any(d in ("fs", "node:fs", "fs/promises", "node:fs/promises", "fs-extra") for d in deps):
            count_op(facts, OP_FILEIO, len(FS_CALL.findall(code)))
        count_op(facts, OP_SQL, 1 if SQL_RE.search(code) else 0)
        facts["entry"] = bool(re.search(r"\brequire\.main\s*===?\s*module\b", code))
        return info, facts
