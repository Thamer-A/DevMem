"""Per-file symbol/dependency extraction behind a small interface (0 LLM tokens).

An extractor turns one file's text into
  info  - the public per-file entry stored in map.json "files" (lang, module, symbols, deps, plus
          language extras such as Luau `kind`, `remotes_defined`, Python `methods`, `config_keys`)
  facts - internal observations used for conventions and never written to map.json:
          ops   {label: count}   sensitive operations seen (DB connection, env reads, network, ...)
          uses  [[dep, name]]    names this file takes from another module (`dep` is resolvable
                                 exactly like an entry of info["deps"])
          entry bool             runs as a program (`if __name__ == "__main__"`, Roblox script)
          config_used [..]       keys read from a config module (when recognisable)

Python uses the standard-library `ast` module (falls back to regexes on a syntax error). JS/TS and
Luau use regex extractors over comment-stripped code. Another backend (for example tree-sitter)
can replace any of them with `register(lang, extractor)`; the contract is just `extract(rel, text)`.
Bump EXTRACTOR_VERSION when the output changes; the index re-extracts every file on mismatch.
"""
from __future__ import annotations

import re
from pathlib import Path

EXTRACTOR_VERSION = 6

LANG_BY_EXT = {
    ".py": "python", ".pyi": "python", ".pyw": "python",
    ".luau": "luau", ".lua": "luau",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "ts", ".tsx": "ts", ".mts": "ts", ".cts": "ts",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".cs": "csharp",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".gd": "gdscript", ".sh": "shell", ".bash": "shell",
    ".ps1": "powershell", ".sql": "sql", ".vue": "js", ".svelte": "js",
}
DATA_EXT = {".json", ".jsonc", ".md", ".rst", ".toml", ".yaml", ".yml", ".txt", ".cfg", ".ini", ".html", ".htm",
            ".css", ".scss", ".xml", ".csv", ".proto", ".graphql", ".gradle", ".cmake", ".tscn", ".tres",
            ".godot", ".uproject", ".mod", ".sum"}
TEXT_EXT = set(LANG_BY_EXT) | DATA_EXT
TEXT_NAMES = {"Makefile", "Dockerfile", "Gemfile", "Rakefile", "Procfile", "CMakeLists.txt", "go.mod",
              "Justfile", "Vagrantfile"}
CODE_LANGS = {l for l in LANG_BY_EXT.values() if l not in ("sql",)}
CONFIG_NAMES = {"config", "configs", "settings", "constants", "tunables", "conf", "configuration", "defaults"}

# operation labels shared by all extractors (the wording shown in PROJECT.md / packs)
OP_DB = "database connection"
OP_ENV = "environment variable reads"
OP_NET = "network requests"
OP_PROC = "subprocess / shell commands"
OP_LOGCFG = "logging configuration"
OP_PRINT = "print/console output"
OP_SQL = "raw SQL statements"
OP_FILEIO = "direct file I/O (open)"
OP_REMOTE = "RemoteEvent/RemoteFunction creation (Instance.new)"
OP_DATASTORE = "DataStore access"
OP_ASSETID = "rbxassetid literal"
OP_RECEIPT = "purchase receipt handler (ProcessReceipt)"
OP_LEADERSTATS = "leaderstats access"
ABSENCE_OPS = {OP_PRINT}   # reported when a sizeable project has none at all
# bounded: an unbounded `SELECT .+? FROM` rescanned the rest of the line from every SELECT (quadratic)
SQL_RE = re.compile(r"\b(SELECT\s.{1,400}?\sFROM\s|INSERT\s+INTO\s|UPDATE\s+\w+\s+SET\s|DELETE\s+FROM\s|CREATE\s+TABLE\s)")
ASSET_RE = re.compile(r"rbxassetid://\d+|roblox\.com/asset/\?id=\d+")
# the name starts at a word boundary: without `\b` every position inside a long token (a hex blob, a minified
# identifier) started a new attempt over the rest of it (16 KB took 6 s)
NAMED_ASSET_RE = re.compile(r"""["']?\b([A-Za-z_]\w*)["']?[ \t]*[:=][ \t]*["']"""
                            r"""(?:rbxassetid://|https?://www\.roblox\.com/asset/\?id=)\d+""")


def language_of(rel: str) -> str:
    return LANG_BY_EXT.get(Path(rel).suffix.lower(), "text")


def is_text_candidate(rel: str) -> bool:
    p = Path(rel)
    return p.suffix.lower() in TEXT_EXT or p.name in TEXT_NAMES


def module_name(rel: str) -> str:
    stem = Path(rel).name.split(".")[0]
    return Path(rel).parent.name if stem in ("init", "__init__", "index", "mod") else stem


def new_info(rel: str, lang: str) -> tuple[dict, dict]:
    return ({"lang": lang, "module": module_name(rel), "symbols": [], "deps": []},
            {"ops": {}, "uses": [], "entry": False, "config_used": []})


def count_op(facts: dict, label: str, n: int = 1) -> None:
    if n:
        facts["ops"][label] = facts["ops"].get(label, 0) + n


def strip_comments(text: str, line: str, block: tuple[str, str] | None, quotes: str = "\"'") -> str:
    """Remove comments but not comment-like text inside string literals (keeps line count). An unterminated string
    ends at the end of its line and an unterminated block comment at the end of the text, as a lexer would read
    them: otherwise every opener rescanned the rest of the line or file, which is quadratic on a run of openers."""
    q = "|".join(f"{re.escape(c)}(?:\\\\.|[^{re.escape(c)}\\\\\\n])*(?:{re.escape(c)}|(?=\\n)|\\Z)" for c in quotes)
    parts = [f"(?P<s>{q})", f"(?P<c>{re.escape(line)}[^\\n]*)"]
    if block:
        parts.insert(1, f"(?P<b>{re.escape(block[0])}.*?(?:{re.escape(block[1])}|\\Z))")
    rx = re.compile("|".join(parts), re.S)

    def repl(m: re.Match) -> str:
        if m.group("s") is not None:
            return m.group("s")
        return "\n" * m.group(0).count("\n")
    return rx.sub(repl, text)


class Extractor:
    """Interface: extract(rel, text) -> (info, facts). Subclasses fill what they can."""
    name = "base"

    def extract(self, rel: str, text: str) -> tuple[dict, dict]:
        return new_info(rel, language_of(rel))


# ---- generic extractor for languages without a dedicated one -----------------------------------
# Every pattern is linear in the file's length: no negated class or `\s` run may cross a newline unless it is bounded
# (`[^)\n]{0,200}`, `\s{1,64}`), and no two adjacent quantified classes overlap. An unbounded `\([^)]*\)` read to the
# end of the file from every `func (` / `pub(` that never closed (a 511 KB .rs file of `pub(\n` took 42 s).
# `^[ \t]*`, not `^\s*`: under re.M `\s` also crosses newlines, so every line of a run of blank lines rescanned
# the rest of the run. tests/test_extract_growth.py checks every extension on inputs made of each pattern's opener.
_GENERIC = {
    "go": [r"^func[ \t]{1,64}(?:\([^)\n]{0,200}\)[ \t]{0,64})?([A-Za-z_]\w*)", r"^type[ \t]{1,64}([A-Za-z_]\w*)"],
    "rust": [r"^[ \t]*(?:pub(?:\([^)\n]{0,100}\))?[ \t]{1,64})?(?:async[ \t]{1,64})?fn[ \t]{1,64}([A-Za-z_]\w*)",
             r"^[ \t]*(?:pub(?:\([^)\n]{0,100}\))?[ \t]{1,64})?(?:struct|enum|trait)[ \t]{1,64}([A-Za-z_]\w*)"],
    "java": [r"\b(?:class|interface|enum|record)\s{1,64}([A-Za-z_]\w*)"],
    "kotlin": [r"\b(?:class|interface|object)\s{1,64}([A-Za-z_]\w*)",
               r"^[ \t]*(?:\w{1,64}[ \t]{1,16}){0,8}fun[ \t]{1,64}(?:<[^>\n]{0,200}>[ \t]{0,64})?([A-Za-z_]\w*)"],
    "csharp": [r"\b(?:class|interface|struct|enum|record)\s{1,64}([A-Za-z_]\w*)"],
    "ruby": [r"^[ \t]*def[ \t]{1,64}(?:self\.)?([A-Za-z_]\w*[?!]?)", r"^[ \t]*(?:class|module)[ \t]{1,64}([A-Z]\w*)"],
    "php": [r"\bfunction\s{1,64}([A-Za-z_]\w*)", r"\b(?:class|interface|trait)\s{1,64}([A-Za-z_]\w*)"],
    "swift": [r"\bfunc\s{1,64}([A-Za-z_]\w*)", r"\b(?:class|struct|enum|protocol)\s{1,64}([A-Za-z_]\w*)"],
    "gdscript": [r"^func[ \t]{1,64}([A-Za-z_]\w*)", r"^class_name[ \t]{1,64}([A-Za-z_]\w*)"],
    "shell": [r"^[ \t]*(?:function[ \t]{1,64})?([A-Za-z_][\w-]*)[ \t]*\(\)\s{0,64}\{"],
}
_GENERIC_RX = {k: [re.compile(p, re.M) for p in v] for k, v in _GENERIC.items()}
_GENERIC_COMMENTS = {"go": ("//", ("/*", "*/")), "rust": ("//", ("/*", "*/")), "java": ("//", ("/*", "*/")),
                     "kotlin": ("//", ("/*", "*/")), "csharp": ("//", ("/*", "*/")), "swift": ("//", ("/*", "*/")),
                     "php": ("//", ("/*", "*/")), "c": ("//", ("/*", "*/")), "cpp": ("//", ("/*", "*/")),
                     "ruby": ("#", None), "gdscript": ("#", None), "shell": ("#", None)}


class GenericExtractor(Extractor):
    name = "generic-regex"

    def extract(self, rel: str, text: str) -> tuple[dict, dict]:
        lang = language_of(rel)
        info, facts = new_info(rel, lang)
        code = text
        if lang in _GENERIC_COMMENTS:
            line, block = _GENERIC_COMMENTS[lang]
            code = strip_comments(text, line, block)
        syms = set()
        for rx in _GENERIC_RX.get(lang, []):
            syms.update(rx.findall(code))
        info["symbols"] = sorted(syms)[:200]
        if lang != "text" or not rel.endswith((".md", ".rst", ".txt")):
            count_op(facts, OP_ASSETID, len(ASSET_RE.findall(code)))
            names = sorted(set(NAMED_ASSET_RE.findall(code)))
            if names:
                info["assets"] = names[:100]
        if lang in CODE_LANGS:
            count_op(facts, OP_SQL, 1 if SQL_RE.search(code) else 0)
        return info, facts


_REGISTRY: dict[str, Extractor] = {}
_GENERIC_EXTRACTOR = GenericExtractor()


def register(lang: str, extractor: Extractor) -> None:
    """Install an extractor for a language (e.g. an optional tree-sitter backend)."""
    _REGISTRY[lang] = extractor


def get_extractor(lang: str) -> Extractor:
    if not _REGISTRY:
        _register_defaults()
    return _REGISTRY.get(lang, _GENERIC_EXTRACTOR)


def _register_defaults() -> None:
    from .extract_js import JsExtractor
    from .extract_luau import LuauExtractor
    from .extract_python import PythonExtractor
    for lang, ex in (("python", PythonExtractor()), ("luau", LuauExtractor()), ("js", JsExtractor()),
                     ("ts", JsExtractor())):
        _REGISTRY.setdefault(lang, ex)


MAX_EXTRACT_LINE = 4096   # longer lines (minified or generated code) are blanked before regex extraction


def cap_lines(text: str, limit: int = MAX_EXTRACT_LINE) -> str:
    """`text` with every line longer than `limit` emptied (line numbers kept). Defence in depth for the regex
    extractors: a pattern that is accidentally superlinear in a line's length then costs at most limit**2 per line."""
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    if all(len(ln) <= limit for ln in lines):
        return text
    return "\n".join("" if len(ln) > limit else ln for ln in lines)


MAX_EXTRACT_CHARS = 256 * 1024   # regex extraction reads this much of a file; the whole file is still searchable


def extract_file(rel: str, text: str, plain: bool = False) -> tuple[dict, dict]:
    """Never raises: an extractor failure degrades to an entry without symbols. Every extractor gets the first
    MAX_EXTRACT_CHARS characters (the entry then says `extract_truncated`: declarations further down are not listed):
    Python cut at a top-level statement start and parsed with `ast` in pieces (devmem.extract_python), the regex
    extractors cut at a line end and with over-long lines emptied (cap_lines).
    plain=True: no extraction at all (`extract_skipped`), for a file whose extraction was too slow before."""
    lang = language_of(rel)
    if plain:
        info, facts = new_info(rel, lang)
        info["extract_skipped"] = True
        return info, facts
    cut = False
    if lang == "python":
        if len(text) > MAX_EXTRACT_CHARS:   # cut at a top-level statement start, so the part kept still parses
            from .extract_python import cut_point
            text, cut = text[:cut_point(text, MAX_EXTRACT_CHARS)], True
    else:
        if len(text) > MAX_EXTRACT_CHARS:
            end = text.rfind("\n", 0, MAX_EXTRACT_CHARS)
            text, cut = text[:end if end > 0 else MAX_EXTRACT_CHARS], True
        text = cap_lines(text)
    try:
        info, facts = get_extractor(lang).extract(rel, text)
    except Exception:  # noqa: BLE001 - one odd file must not stop a scan
        info, facts = new_info(rel, lang)
    if cut:
        info["extract_truncated"] = True
    return info, facts


def is_config_module(info: dict) -> bool:
    return info.get("module", "").lower() in CONFIG_NAMES
