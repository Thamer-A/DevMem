"""Luau / Roblox extractor (regexes over comment-stripped code; the Roblox engine adapter).

Uses only Roblox and Rojo conventions, never a particular game's helper names:
  - script kind from Rojo file naming (`*.server.luau`, `*.client.luau`, otherwise ModuleScript)
  - `require(...)` targets and the local each one is bound to
  - RemoteEvent/RemoteFunction creation sites (`Instance.new("RemoteEvent")`) and, in those files,
    remote names given as literals: `x.Name = "Foo"` on a created remote, an array of string literals
    (a name list the file loops over), or a call with one string literal to a function this same
    file defines (a registry such as `Net.declare("Foo")`). These are heuristics, labelled as such.
  - remote use sites through the Roblox API (`:FireServer`, `:FireClient`, `:FireAllClients`,
    `.OnServerEvent`, `.OnClientEvent`, `:InvokeServer`, `.OnServerInvoke`, ...)
  - asset ids (`rbxassetid://...`) and the names they are bound to
  - DataStore access, HttpService requests, MarketplaceService.ProcessReceipt, leaderstats
"""
from __future__ import annotations

import re
from collections import defaultdict

from .extract import (ASSET_RE, CONFIG_NAMES, NAMED_ASSET_RE, OP_ASSETID, OP_DATASTORE, OP_LEADERSTATS, OP_NET,
                      OP_PRINT, OP_RECEIPT, OP_REMOTE, Extractor, count_op, is_config_module, new_info)

_STRIP = re.compile(
    r"""(?P<bc>--\[(?P<e1>=*)\[.*?(?:\](?P=e1)\]|\Z))|(?P<lc>--[^\n]*)|(?P<ls>\[(?P<e2>=*)\[.*?(?:\](?P=e2)\]|\Z))"""
    r"""|(?P<s>"(?:\\.|[^"\\\n])*(?:"|(?=\n)|\Z)|'(?:\\.|[^'\\\n])*(?:'|(?=\n)|\Z)|`(?:\\.|[^`\\])*(?:`|\Z))""", re.S)


def strip_luau_comments(text: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group("bc") is not None or m.group("lc") is not None:
            return "\n" * m.group(0).count("\n")
        return m.group(0)
    return _STRIP.sub(repl, text)


FUNC = re.compile(r"^[ \t]*function\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*[.:][A-Za-z_]\w*)\s*\(", re.M)
LOCAL_FUNC = re.compile(r"^[ \t]*local\s+function\s+([A-Za-z_]\w*)", re.M)
GLOBAL_FUNC = re.compile(r"^[ \t]*function\s+([A-Za-z_]\w*)\s*\(", re.M)
# the argument starts with a non-space and the whitespace around it is bounded: with spaces in the argument's class and
# `\s*` on both sides, `require(` + 290 spaces cost 300 x 290 steps per opener (32 s for 511 KB)
_REQ_ARG = r"([\w.:\"'()\[\]][\w.:\"'()\[\] ]{0,299}?)"
REQUIRE = re.compile(r"require\([ \t]{0,64}" + _REQ_ARG + r"[ \t]{0,8}\)")
REQUIRE_LOCAL = re.compile(r"\blocal[ \t]{1,64}([A-Za-z_]\w*)[ \t]{0,64}(?::[^=\n]{1,200})?=[ \t]{0,64}require\([ \t]{0,64}"
                           + _REQ_ARG + r"[ \t]{0,8}\)(?:[ \t]{0,64}\.[ \t]{0,64}([A-Za-z_]\w*))?")
MEMBER_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*[.:]\s*([A-Za-z_]\w*)\s*\(")
REMOTE_NEW = re.compile(r"""Instance\.new\(\s*["'](?:Unreliable)?Remote(?:Event|Function)["']""")
# the annotation stops at the next `:` too: every identifier is a start here, and `:a:a:a...` made each one scan on
REMOTE_VAR = re.compile(r"""(?:\blocal\s+)?\b([A-Za-z_]\w*)\s*(?::[^=\n:]{1,200})?=\s*Instance\.new\(\s*["']"""
                        r"""(?:Unreliable)?Remote(?:Event|Function)["']""")
# one unambiguous way to split `{ "A", "B"; 'C' }`: every element is a quoted name followed either by a separator
# (with optional spaces around it) or by the closing brace. The earlier `(...\s*[,;]?\s*)+` could split the spaces
# between elements in exponentially many ways when the brace never closed (a 150-byte file took minutes).
STRING_ARRAY = re.compile(r"""\{\s*(?:["'][A-Za-z_]\w*["']\s*(?:[,;]\s*|(?=\})))+\}""")
REGISTRY_CALL = re.compile(r"""^[ \t]*([A-Za-z_]\w*)\s*([.:])\s*([A-Za-z_]\w*)\(\s*["']([A-Za-z_]\w*)["']\s*\)""", re.M)
REMOTE_API = ("FireServer|FireClient|FireAllClients|InvokeServer|InvokeClient|"
              "OnServerEvent|OnClientEvent|OnServerInvoke|OnClientInvoke")
REMOTE_USE = re.compile(r"""(?:\(\s*["']([A-Za-z_]\w*)["']\s*\)|\.([A-Za-z_]\w*)|\b([A-Za-z_]\w*))\s*[.:]\s*"""
                        rf"""({REMOTE_API})\b""")
REMOTE_ALIAS = re.compile(r"""\blocal\s+([A-Za-z_]\w*)\s*(?::[^=\n]{1,200})?=\s*[\w.:]*?"""
                          r"""(?:\(\s*["']([A-Za-z_]\w*)["']\s*\)|\.([A-Za-z_]\w*))\s*$""", re.M)
HTTP_ALIAS = re.compile(r"""\b([A-Za-z_]\w*)\s*=\s*game\s*:\s*GetService\(\s*["']HttpService["']\s*\)""")
PRINT = re.compile(r"(?<![.\w:])(?:print|warn)\s*\(")
DATASTORE = re.compile(r":\s*Get(?:Ordered)?DataStore\s*\(")
RECEIPT = re.compile(r"\.ProcessReceipt\s*=")
LEADERSTATS = re.compile(r"""["']leaderstats["']""")
CONFIG_ASSIGN = re.compile(r"^[ \t]*([A-Za-z_]\w*)[ \t]*=", re.M)
# One pass each, looked up by name afterwards. A pattern built per variable (`\b{var}\.Name`, `\b{alias}\.(\w+)`, an
# alternation of every HttpService alias) rescanned the whole file once per name: 256 KB of distinct
# `local a=require(conf)` lines took 49-68 s.
NAME_SET = re.compile(r"""\b([A-Za-z_]\w*)\.Name\s{0,64}=\s{0,64}["']([A-Za-z_]\w*)["']""")
# zero-width after the first name, so `a.b.c` yields (a, b) and (b, c) as the per-name searches did
FIELD_READ = re.compile(r"\b([A-Za-z_]\w*)(?=\.([A-Za-z_]\w*))")
HTTP_CALL = re.compile(r"\b([A-Za-z_]\w*)\s{0,64}:\s{0,64}(?:GetAsync|PostAsync|RequestAsync)\s{0,64}\(")


def _last_ident(expr: str) -> str | None:
    names = re.findall(r"[A-Za-z_]\w*", expr)
    return names[-1] if names else None


class LuauExtractor(Extractor):
    name = "luau-regex"

    def extract(self, rel: str, text: str) -> tuple[dict, dict]:
        info, facts = new_info(rel, "luau")
        code = strip_luau_comments(text)
        info["kind"] = ("server-script" if ".server." in rel else "client-script" if ".client." in rel
                        else "module")
        facts["entry"] = info["kind"] != "module"
        info["symbols"] = sorted(set(FUNC.findall(code)))
        info["local_functions"] = sorted(set(LOCAL_FUNC.findall(code)) | set(GLOBAL_FUNC.findall(code)))[:30]
        deps = {d for d in (_last_ident(r) for r in REQUIRE.findall(code)) if d}
        info["deps"] = sorted(deps)

        aliases = {}  # local -> (required module name, field accessed on the result or "")
        for var, expr, field in REQUIRE_LOCAL.findall(code):
            mod = _last_ident(expr)
            if mod:
                aliases[var] = (mod, field)
        uses = set()
        for obj, fn in MEMBER_CALL.findall(code):
            if obj in aliases and not aliases[obj][1]:
                uses.add((aliases[obj][0], fn))
        facts["uses"] = sorted([m, n] for m, n in uses)

        # remotes
        n_new = len(REMOTE_NEW.findall(code))
        count_op(facts, OP_REMOTE, n_new)
        defined = set()
        if n_new:
            remote_vars = set(REMOTE_VAR.findall(code))
            defined.update(lit for var, lit in NAME_SET.findall(code) if var in remote_vars)
            for arr in STRING_ARRAY.findall(code):
                defined.update(re.findall(r"""["']([A-Za-z_]\w*)["']""", arr))
            own = set(info["symbols"])
            for tbl, sep, fn, lit in REGISTRY_CALL.findall(code):
                if f"{tbl}{sep}{fn}" in own or f"{tbl}.{fn}" in own or f"{tbl}:{fn}" in own:
                    defined.add(lit)
        info["remotes_defined"] = sorted(defined)
        ralias = {}
        for var, lit, field in REMOTE_ALIAS.findall(code):
            ralias[var] = lit or field
        used = defaultdict(set)
        for lit, field, ident, how in REMOTE_USE.findall(code):
            name = lit or field or ralias.get(ident, ident)
            if name and name not in ("self", "script", "game"):
                used[name].add(how)
        info["remotes_used"] = {k: sorted(v) for k, v in sorted(used.items())}

        # assets and engine services
        count_op(facts, OP_ASSETID, len(ASSET_RE.findall(code)))
        info["assets"] = sorted(set(NAMED_ASSET_RE.findall(code)))[:100]
        count_op(facts, OP_PRINT, len(PRINT.findall(code)))
        count_op(facts, OP_DATASTORE, len(DATASTORE.findall(code)))
        count_op(facts, OP_RECEIPT, len(RECEIPT.findall(code)))
        count_op(facts, OP_LEADERSTATS, len(LEADERSTATS.findall(code)))
        http = set(HTTP_ALIAS.findall(code)) | {"HttpService"}
        count_op(facts, OP_NET, sum(1 for v in HTTP_CALL.findall(code) if v in http))

        # configuration
        if is_config_module(info):
            keys = CONFIG_ASSIGN.findall(code)
            info["config_keys"] = [k for k in dict.fromkeys(keys) if k not in ("local", info["module"])][:80]
        cfg_used = set()
        cfg_vars = {var: field for var, (mod, field) in aliases.items() if mod.lower() in CONFIG_NAMES}
        cfg_used.update(field for field in cfg_vars.values() if field)
        if cfg_vars:
            for var, attr in FIELD_READ.findall(code):
                if var in cfg_vars:
                    field = cfg_vars[var]
                    cfg_used.add(f"{field}.{attr}" if field else attr)
        info["config_keys_used"] = sorted(cfg_used)
        return info, facts
