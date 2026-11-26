"""Python extractor using the standard-library `ast` module (regex fallback on a syntax error)."""
from __future__ import annotations

import ast
import gc
import re
import warnings
from collections.abc import Iterator
from typing import Any

from .extract import (OP_DB, OP_ENV, OP_FILEIO, OP_LOGCFG, OP_NET, OP_PRINT, OP_PROC, OP_SQL, SQL_RE,
                      Extractor, cap_lines, count_op, is_config_module, new_info)

DB_CALLS = {
    "sqlite3.connect", "psycopg2.connect", "psycopg.connect", "psycopg.AsyncConnection.connect", "pymysql.connect",
    "MySQLdb.connect", "mysql.connector.connect", "sqlalchemy.create_engine",
    "sqlalchemy.ext.asyncio.create_async_engine", "asyncpg.connect", "asyncpg.create_pool", "pymongo.MongoClient",
    "motor.motor_asyncio.AsyncIOMotorClient", "redis.Redis", "redis.StrictRedis", "redis.from_url",
    "redis.asyncio.Redis", "duckdb.connect", "aiosqlite.connect", "cx_Oracle.connect", "oracledb.connect",
    "pyodbc.connect", "create_engine", "create_async_engine",
}
ENV_CALLS = {"os.getenv", "os.environ.get", "os.putenv", "os.getenvb", "os.environ.setdefault"}
NET_PREFIXES = ("requests.", "httpx.", "urllib3.", "aiohttp.", "websockets.", "grpc.")
NET_CALLS = {"urllib.request.urlopen", "http.client.HTTPConnection", "http.client.HTTPSConnection", "socket.socket",
             "socket.create_connection", "smtplib.SMTP", "smtplib.SMTP_SSL", "ftplib.FTP", "xmlrpc.client.ServerProxy"}
PROC_CALLS = {"os.system", "os.popen", "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell", "pty.spawn"}
LOGCFG_CALLS = {"logging.basicConfig", "logging.config.dictConfig", "logging.config.fileConfig"}
FILEIO_CALLS = {"open", "io.open", "codecs.open"}

_RX = {
    # on one line each, with bounded runs (linear; see devmem.extract)
    "func": re.compile(r"^(?:async[ \t]{1,64})?def[ \t]{1,64}([A-Za-z_]\w*)", re.M),
    "class": re.compile(r"^class[ \t]{1,64}([A-Za-z_]\w*)", re.M),
    "method": re.compile(r"^[ \t]+(?:async[ \t]{1,64})?def[ \t]{1,64}([A-Za-z_]\w*)", re.M),
    "import": re.compile(r"^[ \t]*(?:from[ \t]{1,64}([\w.]{1,300})[ \t]{1,64}import[ \t]{1,64}([\w, ]{1,2000})|"
                         r"import[ \t]{1,64}([\w.]{1,300}))", re.M),
}


def _dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


_NO_CHILDREN = (ast.Name, ast.Constant, ast.alias, ast.Pass, ast.Break, ast.Continue, ast.Global, ast.Nonlocal)
_SKIP_FIELDS = {"ctx", "type_comment", "id", "attr", "name", "arg", "asname", "module", "level", "kind",
                "conversion", "is_async", "lineno", "col_offset", "end_lineno", "end_col_offset", "simple"}
_FIELDS: dict[type, tuple[str, ...]] = {}


def _iter_nodes(tree: ast.AST) -> Iterator[Any]:  # Any: callers dispatch on type(node) and then use its fields
    """Every node once (like ast.walk), but skipping expression contexts and scalar fields, which
    halves the work on large files."""
    stack = [tree]
    fields_of = _FIELDS
    AST = ast.AST
    while stack:
        node = stack.pop()
        yield node
        t = type(node)
        if t in _NO_CHILDREN_SET:
            continue
        fields = fields_of.get(t)
        if fields is None:
            fields = fields_of[t] = tuple(f for f in t._fields if f not in _SKIP_FIELDS)
        for f in fields:
            v = getattr(node, f, None)
            if type(v) is list:
                stack.extend(x for x in v if isinstance(x, AST))
            elif isinstance(v, AST):
                stack.append(v)


_NO_CHILDREN_SET = set(_NO_CHILDREN)


def _is_main_guard(node: ast.AST) -> bool:
    t = getattr(node, "test", None)
    if not isinstance(t, ast.Compare) or len(t.ops) != 1 or not isinstance(t.ops[0], ast.Eq):
        return False
    sides = [t.left] + list(t.comparators)
    names = {s.id for s in sides if isinstance(s, ast.Name)}
    consts = {s.value for s in sides if isinstance(s, ast.Constant)}
    return "__name__" in names and "__main__" in consts


# Statement boundaries for parsing a large file in pieces. CPython's parser costs about (f-string fields) x (text after
# them): each field is parsed from a copy of the rest of the input (measured: 1000 one-field f-strings followed by 120 KB
# of plain lines take 0.9 s on python.org 3.14; 256 KB of `f'{a}{b}...'` lines 26 s on Anaconda 3.13). Parsing
# PARSE_CHUNK-sized runs of top-level statements separately makes that linear. A boundary is the start of a line at
# column 0, outside brackets and strings, that does not continue a compound statement (else/elif/except/finally) and
# does not follow a decorator. The lexer below is one linear regex; where it misreads (a 3.12 f-string that reuses its
# quote, say) a piece fails to parse and the whole text is parsed once instead.
PARSE_CHUNK = 4096
# strings, comments, brackets, and the line ends that are followed by code at column 0 (with a backslash before one,
# a continued line: not a boundary); blank, comment and indented lines make no token
_LEX = re.compile(r'''(?P<s>[rRbBuUfFtT]{0,2}(?:"""(?:\\.|[^\\])*?(?:"""|\Z)|\'\'\'(?:\\.|[^\\])*?(?:\'\'\'|\Z)'''
                  r'''|"(?:\\.|[^"\\\n])*(?:"|$)|'(?:\\.|[^'\\\n])*(?:'|$)))|(?P<c>\#[^\n]*)|(?P<o>[(\[{])'''
                  r'''|(?P<x>[)\]}])|(?P<n>\\?\n(?=[^\s#)\]}]))''', re.M | re.S)
_CONTINUES = re.compile(r"(?:else|elif|except|finally)\b")
_FSTRING = re.compile(r"(?<![\w])[rRbB]?[fFtT][rRbB]?['\"]")
WHOLE_PARSE_LIMIT = 200_000_000   # f-string starts x characters above which an unsplittable text is not parsed whole


def top_level_starts(text: str) -> list[int]:
    """Offsets where a top-level statement starts (see PARSE_CHUNK). Linear in the text's length."""
    out, depth, after_decorator = [], 0, text.startswith("@")
    for m in _LEX.finditer(text):
        kind = m.lastgroup
        if kind == "o":
            depth += 1
        elif kind == "x":
            depth = max(0, depth - 1)
        elif kind == "n" and depth == 0 and m.group() == "\n":
            pos = m.end()
            if not after_decorator and not _CONTINUES.match(text, pos):
                out.append(pos)
            after_decorator = text.startswith("@", pos)
    return out


def cut_point(text: str, limit: int) -> int:
    """Where to cut a Python text longer than `limit`: the last top-level statement start within it (so the kept
    part still parses), else the last line end, else `limit`."""
    starts = [b for b in top_level_starts(text[:limit + 1]) if b <= limit]
    if starts:
        return starts[-1]
    end = text.rfind("\n", 0, limit)
    return end + 1 if end > 0 else limit


def _parse(text: str, rel: str) -> ast.Module:
    """ast.parse, in pieces of about PARSE_CHUNK characters cut at top-level statement starts (same module body)."""
    if len(text) > PARSE_CHUNK:
        pieces, start = [], 0
        for b in top_level_starts(text):
            if b - start >= PARSE_CHUNK:
                pieces.append(text[start:b])
                start = b
        pieces.append(text[start:])
        if len(pieces) > 1:
            try:
                body = []
                for piece in pieces:
                    body.extend(ast.parse(piece, filename=rel).body)
                return ast.Module(body=body, type_ignores=[])
            except SyntaxError:
                pass    # a misread boundary: parse it whole below
        if len(_FSTRING.findall(text)) * len(text) > WHOLE_PARSE_LIMIT:
            raise SyntaxError("not parsed: too many f-strings in a text that cannot be split")
    return ast.parse(text, filename=rel)


class PythonExtractor(Extractor):
    name = "python-ast"

    def extract(self, rel: str, text: str) -> tuple[dict, dict]:
        # the collector's passes over the many nodes `ast` makes grow faster than the input; off while they exist
        was = gc.isenabled()
        gc.disable()
        try:
            return self._extract(rel, text)
        finally:
            if was:
                gc.enable()

    def _extract(self, rel: str, text: str) -> tuple[dict, dict]:
        info, facts = new_info(rel, "python")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = _parse(text, rel)
        except (SyntaxError, ValueError, RecursionError, MemoryError, TypeError):
            return _regex_fallback(cap_lines(text), info, facts)
        pkg = rel.rsplit(".", 1)[0].split("/")[:-1]
        symbols, methods, cfg = [], [], []
        want_cfg = is_config_module(info)

        def top(body):
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(node.name)
                elif isinstance(node, ast.ClassDef):
                    symbols.append(node.name)
                    for n in node.body:
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            methods.append(n.name)
                        elif want_cfg and isinstance(n, (ast.Assign, ast.AnnAssign)):
                            cfg.extend(_assigned(n))
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    if want_cfg:
                        cfg.extend(_assigned(node))
                elif isinstance(node, ast.If):
                    if _is_main_guard(node):
                        facts["entry"] = True
                    top(node.body)
                    top(node.orelse)
                elif isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                    top(node.body)
                    for h in node.handlers:
                        top(h.body)
                    top(node.orelse)
                    top(node.finalbody)
                elif isinstance(node, (ast.With, ast.AsyncWith)):
                    top(node.body)
        top(tree.body)

        aliases: dict[str, str] = {}      # local name -> dotted target
        module_aliases: set[str] = set()  # local names bound by `import x` (modules)
        deps: set[str] = set()
        uses: set[tuple[str, str]] = set()
        imports, call_names, env_hits, strings = [], [], 0, []
        for node in _iter_nodes(tree):
            t = type(node)
            if t is ast.Call:
                call_names.append(_dotted(node.func))
            elif t is ast.Attribute:
                if node.attr == "environ":
                    env_hits += 1
            elif t is ast.Name:
                if node.id == "environ":
                    env_hits += 1
            elif t is ast.Constant:
                v = node.value
                if type(v) is str and len(v) > 10:
                    strings.append(v)
            elif t is ast.Import or t is ast.ImportFrom:
                imports.append(node)
        for node in imports:
            if type(node) is ast.Import:
                for a in node.names:
                    deps.add(a.name)
                    local = a.asname or a.name.split(".")[0]
                    aliases[local] = a.name if a.asname else a.name.split(".")[0]
                    module_aliases.add(local)
                continue
            if node.level:
                base = pkg[: len(pkg) - (node.level - 1)] if node.level - 1 <= len(pkg) else []
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            if not mod:
                continue
            deps.add(mod)
            for a in node.names:
                if a.name == "*":
                    continue
                deps.add(f"{mod}.{a.name}")
                aliases[a.asname or a.name] = f"{mod}.{a.name}"
                module_aliases.add(a.asname or a.name)  # may be a submodule; resolved later
                uses.add((mod, a.name))

        def canon(d: str | None) -> str | None:
            if not d:
                return None
            first, _, rest = d.partition(".")
            if first in aliases:
                return aliases[first] + ("." + rest if rest else "")
            return d

        for raw in call_names:
            name = canon(raw)
            if not name:
                continue
            if name in DB_CALLS or name.endswith((".create_engine", ".create_async_engine")):
                count_op(facts, OP_DB)
            elif name in ENV_CALLS:
                count_op(facts, OP_ENV)
            elif name.startswith(NET_PREFIXES) or name in NET_CALLS:
                count_op(facts, OP_NET)
            elif name.startswith("subprocess.") or name in PROC_CALLS or name.startswith(("os.exec", "os.spawn")):
                count_op(facts, OP_PROC)
            elif name in LOGCFG_CALLS:
                count_op(facts, OP_LOGCFG)
            elif name == "print" and raw == "print":
                count_op(facts, OP_PRINT)
            elif name in FILEIO_CALLS:
                count_op(facts, OP_FILEIO)
            if "." in raw and raw.split(".")[0] in module_aliases:
                mod, _, fn = name.rpartition(".")
                if mod:
                    uses.add((mod, fn))
        if env_hits and (aliases.get("os") == "os" or aliases.get("environ") == "os.environ"):
            count_op(facts, OP_ENV, env_hits)
        if any(SQL_RE.search(v) for v in strings):
            count_op(facts, OP_SQL)

        info["symbols"] = sorted(set(symbols))
        info["methods"] = sorted(set(methods) - set(info["symbols"]))[:40]
        info["deps"] = sorted(d for d in deps if d)
        if want_cfg and cfg:
            info["config_keys"] = [k for k in dict.fromkeys(cfg) if not k.startswith("__")][:80]
        facts["uses"] = sorted([m, n] for m, n in uses)
        return info, facts


def _assigned(node) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    out = []
    for t in targets:
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            out += [e.id for e in t.elts if isinstance(e, ast.Name)]
    return out


def _regex_fallback(text: str, info: dict, facts: dict) -> tuple[dict, dict]:
    code = re.sub(r"#[^\n]*", "", text)
    info["symbols"] = sorted(set(_RX["func"].findall(code)) | set(_RX["class"].findall(code)))
    info["methods"] = sorted(set(_RX["method"].findall(code)) - set(info["symbols"]))[:40]
    deps = []
    for frm, names, imp in _RX["import"].findall(code):
        deps.append(frm or imp)
        if frm:
            deps += [f"{frm}.{n.strip()}" for n in names.split(",") if n.strip()]
    info["deps"] = sorted(set(d for d in deps if d))
    info["parse_error"] = True
    return info, facts
