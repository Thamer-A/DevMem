"""Deterministic project scanner (0 LLM tokens).

Builds <store>/index/map.json and <store>/PROJECT.md through the incremental index
(<store>/index/index.db, see devmem.index):
  - inventory with content hashes (for incremental updates and freshness checks)
  - symbols and the import/require graph (`used_by`), resolved per language
  - Roblox/Rojo: DataModel paths, remote definitions/uses, asset ids (devmem.extract_luau)
  - conventions, each an observation backed by file evidence:
      * exclusive sites: sensitive operations (DB connections, env reads, network, subprocess,
        logging setup, console output, raw SQL, file I/O; RemoteEvent creation, DataStore access,
        asset ids) that occur in only one or two non-test files
      * hub APIs (functions imported/called from many files) and hub modules (high fan-in)
      * where configuration lives (config modules and project config files), tests, entry points

Output is byte-stable: when nothing relevant changed, map.json and PROJECT.md are not rewritten and
`scanned_at` keeps the time the content last changed.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import stat
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from .extract import ABSENCE_OPS, CODE_LANGS, LANG_BY_EXT, TEXT_EXT  # noqa: F401  (re-exported)
from .index import MAP_MARK_HASH, MAP_MARK_LEVELS, TEST_SWITCH_ENV, Index, _Mark
from .store import (display_name, ensure_store, estimate_tokens, private_mkdir, read_project_text, store_path,
                    write_private)

SCAN_VERSION = "2"
LANG = {ext: lang for ext, lang in LANG_BY_EXT.items()}  # v1 name, kept for importers
MD_MAX_TOKENS = 3000           # PROJECT.md is meant to be loadable: summarise by directory beyond this
MD_ROJO_LINES = 40             # Rojo `$path` entries listed in PROJECT.md (all of them are in map.json)
TEST_DIRS = {"test", "tests", "spec", "specs", "__tests__", "testing"}
TEST_SUFFIXES = ("_test.py", ".test.js", ".test.ts", ".test.tsx", ".test.jsx", ".spec.js", ".spec.ts", ".spec.tsx",
                 "_test.go", ".spec.luau", ".spec.lua", ".test.luau", ".test.lua", "_spec.rb", "Test.java")
DOC_EXT = (".md", ".rst", ".txt")
CONFIG_FILES = {"pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini", "requirements.txt", "package.json",
                "tsconfig.json", "default.project.json", "wally.toml", "aftman.toml", "rokit.toml", "foreman.toml",
                "selene.toml", "stylua.toml", "Cargo.toml", "go.mod", "Makefile", "Dockerfile", "docker-compose.yml",
                "docker-compose.yaml", "compose.yaml", "project.godot", "vite.config.ts", "vite.config.js",
                "webpack.config.js", "next.config.js", "jest.config.js", "babel.config.js", "Gemfile", "composer.json",
                "pom.xml", "build.gradle", "settings.gradle", "CMakeLists.txt", "manage.py", "alembic.ini"}
CONFIG_DIRS = {"config", "configs", "settings", "conf"}


def is_test(rel: str) -> bool:
    """Tests may legitimately break production conventions, so they are left out of them."""
    parts = rel.split("/")
    name = parts[-1]
    return (any(p in TEST_DIRS for p in parts[:-1]) or name.startswith("test_") or name.endswith(TEST_SUFFIXES))


# ---- scan --------------------------------------------------------------------------------------
def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def _read(p: Path) -> bytes | None:
    from .store import STORE_FILE_CAP, read_regular
    return read_regular(p, STORE_FILE_CAP)      # a plain regular file only (never a FIFO a repository shipped)


def _write_if_changed(p: Path, text: str) -> None:
    data = text.encode("utf-8")
    if _read(p) == data:
        return
    private_mkdir(p.parent)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    # a new 0600 file (O_EXCL: never one that appeared in between, or a link with that name; a leftover is removed)
    write_private(tmp, data)
    for _ in range(5):
        try:
            os.replace(tmp, p)    # replaces a link at `p` rather than following it
            return
        except PermissionError:  # Windows: a reader has it open; retry, then write in place
            import time
            time.sleep(0.05)
    if os.path.islink(p):
        tmp.unlink()
        raise OSError(f"{p} is a symbolic link; devmem never writes through a link")
    p.write_bytes(data)
    try:
        tmp.unlink()
    except OSError:
        pass


# Seconds a scan may spend reading files when it runs inside a hook or the MCP server (they set this; None = no limit).
# Checked between files: on overrun what is indexed is kept, the map says the scan is incomplete, and the next scan
# continues. A future slow pattern can then cost one prompt a few seconds, never stall every session.
TIME_BUDGET: float | None = None


def scan(project: Path, store: Path | None = None, *, stats: dict | None = None,
         time_budget: float | None = None) -> dict:
    """Scan `project`; write map.json + PROJECT.md into the store; return the map.
    If `stats` is a dict it receives the index sync counts plus `fast_path` (True when the map was
    already current and nothing was rebuilt). `time_budget` (default TIME_BUDGET) bounds the file reading; the map's
    "scan" entry then says `incomplete` and how many files are `pending`. Files skipped because their names hold
    control or bidi characters are counted there too (`unsafe_names`)."""
    import time
    budget = time_budget if time_budget is not None else TIME_BUDGET
    deadline = time.perf_counter() + budget if budget is not None else None
    project = Path(project).resolve()
    store = Path(store) if store else store_path(project)
    map_path, md_path = store / "index" / "map.json", store / "PROJECT.md"
    ensure_store(store)
    with Index(store) as idx:
        sync_deadline = deadline
        if deadline is not None and budget is not None:
            # time kept for the map build, from what the last one took: a sync that used the whole budget left the
            # map step none, and the kill in it then demoted the map of a project that is only large
            sync_deadline = deadline - min(budget / 2, MAP_RESERVE_FACTOR * _meta_float(idx, "map_seconds"))
        counts = idx.sync(project, deadline=sync_deadline)
        scan_info, state_key = _scan_state(project, counts)
        unchanged = not any(counts.get(k) for k in ("added", "changed", "removed"))
        if unchanged:
            mb = _map_if_current(idx, project, store, counts)
            if mb is not None:
                try:
                    m = json.loads(mb.decode("utf-8"))
                    if not (budget is None and isinstance(m, dict) and m.get("reduced")):   # `devmem scan`: full
                        if stats is not None:
                            stats.update(counts, fast_path=True)
                        return m
                except (ValueError, RecursionError):
                    pass
        # under a budget (hooks, MCP: the refresh child) the map build is marked like file extraction: a build that
        # gets the child killed is demoted a level for the next ones (devmem.index.MAP_MARK_LEVELS)
        level = idx.map_level() if budget is not None else 0
        mark = _Mark(idx.path.parent) if budget is not None and not idx.degraded and level < MAP_LEVELS else None
        t_map = time.perf_counter()
        try:
            if mark:
                mark.set(MAP_MARK_HASH, MAP_MARK_LEVELS[level])
            _test_block_map(level)
            m = _build(project, idx.file_rows(with_facts=level < MAP_LEVELS), level)
            if scan_info:
                m["scan"] = scan_info
            existing = None
            mb = _read(map_path)
            if mb is not None:
                try:
                    existing = json.loads(mb.decode("utf-8"))
                except (ValueError, RecursionError):
                    existing = None
            core = {k: v for k, v in m.items() if k != "scanned_at"}
            if isinstance(existing, dict) and existing.get("scanned_at") and \
                    {k: v for k, v in existing.items() if k != "scanned_at"} == core:
                m["scanned_at"] = existing["scanned_at"]
            map_text = json.dumps(m, indent=1)
            if len(map_text) > MAP_MAX_BYTES:         # not expected within the caps above: the summary only
                m = _cut_map(m)
                map_text = json.dumps(m, indent=1)
            md_text = render_project_md(m)
            _write_if_changed(map_path, map_text)
            _write_if_changed(md_path, md_text)
        finally:
            if mark:
                mark.close()
        idx.meta_set("map_state", f"{state_key}|{_sha(map_text.encode('utf-8'))}|{_sha(md_text.encode('utf-8'))}")
        idx.meta_set("map_seconds", f"{time.perf_counter() - t_map:.3f}")
        if budget is None and level == 0:
            idx.clear_map_level()       # `devmem scan` built the full map: hooks try it again
    if stats is not None:
        stats.update(counts, fast_path=False)
    return m


MAP_RESERVE_FACTOR = 1.5    # the map step is given this many times what the last one took (at most half the budget)
MAP_LEVELS = len(MAP_MARK_LEVELS)   # 2: file entries only (see build_map)
TEST_BLOCK_MAP_ENV = "DEVMEM_TEST_BLOCK_MAP"     # with DEVMEM_TESTING=1: "<seconds>[|<levels>]", default level 0


def _test_block_map(level: int) -> None:
    """Test-only, like devmem.index._test_block: the map build at the given levels sleeps (at most 60 s), to prove a
    stuck map build is bounded from outside and demoted without relying on any step being fast."""
    if os.environ.get(TEST_SWITCH_ENV) != "1":
        return
    spec = os.environ.get(TEST_BLOCK_MAP_ENV)
    if not spec:
        return
    parts = spec.split("|")
    levels = parts[1] if len(parts) > 1 else "0"
    if str(level) not in levels:
        return
    try:
        secs = float(parts[0])
    except ValueError:
        return
    if secs == secs and secs > 0:
        import time
        time.sleep(min(secs, 60.0))


def _meta_float(idx: Index, key: str) -> float:
    try:
        v = float(idx.meta_get(key) or 0.0)
    except ValueError:
        return 0.0
    return v if 0.0 <= v < 1e6 else 0.0


def _scan_state(project: Path, counts: dict) -> tuple[dict, str]:
    scan_info = {k: counts[k] for k in ("incomplete", "pending", "unsafe_names", "too_deep") if counts.get(k)}
    return scan_info, f"{SCAN_VERSION}|{project}|{json.dumps(scan_info, sort_keys=True)}"


def _map_if_current(idx: Index, project: Path, store: Path, counts: dict) -> bytes | None:
    """map.json's bytes when it and PROJECT.md are what the last scan wrote for the index as it is (`counts`: the
    sync's, or Index.check's listing counts), else None."""
    _info, state_key = _scan_state(project, counts)
    mb, mdb = _read(store / "index" / "map.json"), _read(store / "PROJECT.md")
    if mb is not None and mdb is not None and idx.meta_get("map_state") == f"{state_key}|{_sha(mb)}|{_sha(mdb)}":
        return mb
    return None


def map_current(idx: Index, project: Path, store: Path, counts: dict) -> bool:
    """devmem.refresh: True when the map needs no rebuild for the index as it is (see _map_if_current)."""
    return _map_if_current(idx, Path(project).resolve(), Path(store), counts) is not None


# ---- what a project can make the map hold ------------------------------------------------------
# Every part of the map comes from the project (a manifest's tree, file names, the names in files), map.json is read on
# every prompt, and a few hundred KB of nested manifest once made a 1 GB map (one long DataModel path per entry). So
# each part is bounded, and what does not fit is left out with a count under the map's "truncated" key (PROJECT.md
# says so) instead of failing.
ROJO_MAX_ENTRIES = 50_000       # `$path` entries kept from default.project.json
ROJO_MAX_NAME = 512             # characters of one DataModel path or `$path`; a longer entry (and what is below it) is out
ROJO_MAX_CHARS = 1_000_000      # characters of all kept `$path`s and DataModel paths together
MAP_NAME_CHARS = 256            # characters of one string in a file entry (a symbol, a dependency, a key)
MAP_LIST_ITEMS = 500            # items of one list (or keys of one table) in a file entry
MAP_FULL_BYTES = 8_000_000      # file entries are kept whole up to about this much JSON, in path order ...
MAP_FILES_BYTES = 12_000_000    # ... then as path, language and size only up to this much; files beyond it are out
MAP_USED_BY_BYTES = 4_000_000   # the import graph (`used_by`); importer lists beyond it are cut
MAP_CONFIG_MODULES = 20         # config modules listed under conventions
MAP_HUB_FILES = 50              # caller files listed per hub API (`callers` keeps the count)
MAP_MAX_BYTES = 32 * 1024 * 1024    # map.json as written (below the 64 MB a store file may have): over it, summary only
MD_MAX_BYTES = 64 * 1024        # PROJECT.md as written: cut at a line, with a note
_LIGHT_KEYS = ("lang", "module", "lines", "bytes", "hash", "instance", "kind", "content_skipped")


def build_map(project: Path, files: dict, facts: dict, level: int = 0) -> dict:
    """The map (same top-level keys as v1) from file entries and facts (rel -> dict, as Index.files/facts). Deterministic
    for a given set of file entries. Bounded by the caps above whatever the entries hold. `level` (devmem.scan.scan
    under a time budget, after a map build was stopped): 1 leaves out the Rojo mapping, the import graph and the hub
    conventions, 2 keeps the file entries' path, language and size only and no conventions."""
    return _build(project, ((rel, info, facts.get(rel)) for rel, info in files.items()), level)


def _build(project: Path, rows, level: int = 0) -> dict:
    """build_map over (rel, info, facts) rows; info and facts may be JSON text (Index.file_rows) or dicts."""
    trunc: dict[str, int] = {}
    rojo, rojo_out = _rojo_read(project) if level == 0 else ({}, 0)
    if rojo_out:
        trunc["rojo_entries"] = rojo_out
    files, facts = _bounded_files(rows, rojo, level, trunc)
    if level == 0:
        used_by, resolve = _graph(files)
    else:
        used_by, resolve = {}, (lambda importer, dep: None)
    conventions = _conventions(files, facts, used_by, resolve) if level < MAP_LEVELS else {}
    if getattr(resolve, "work", {}).get("over"):
        trunc["graph"] = 1
    used_by = _bounded_used_by(used_by, trunc)
    m = {"project": project.name,
         "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "llm_tokens_used": 0, "files": files, "engine": _detect_engine(project, files), "rojo": rojo,
         "conventions": conventions, "used_by": used_by}
    if trunc:
        m["truncated"] = trunc
    if level:
        m["reduced"] = level
    return json.loads(json.dumps(m))  # plain lists/dicts only, so equality with a loaded map is exact


def _loads(v):
    if isinstance(v, (str, bytes)):
        try:
            return json.loads(v)
        except (ValueError, RecursionError):
            return None
    return v


def _capped(v, depth: int = 0):
    """A copy of an entry's value with every string at most MAP_NAME_CHARS and every list or table at most
    MAP_LIST_ITEMS items (nested at most a few levels: entries are flat; a deeper value is dropped)."""
    if isinstance(v, str):
        return v[:MAP_NAME_CHARS]
    if depth > 3:
        return None
    if isinstance(v, list):
        return [_capped(x, depth + 1) for x in v[:MAP_LIST_ITEMS]]
    if isinstance(v, dict):
        return {str(k)[:MAP_NAME_CHARS]: _capped(x, depth + 1) for k, x in list(v.items())[:MAP_LIST_ITEMS]}
    return v


def _bounded_files(rows, rojo: dict, level: int, trunc: dict) -> tuple[dict, dict]:
    """(files, facts) within MAP_FULL_BYTES / MAP_FILES_BYTES (see the caps above), with Rojo instances set."""
    files: dict[str, dict] = {}
    facts: dict[str, dict] = {}
    used = 0
    details = level < MAP_LEVELS        # whole entries, until the first one that does not fit
    for rel, info, fact in rows:
        info = _loads(info)
        if not isinstance(rel, str) or not isinstance(info, dict):
            continue
        inst = _rojo_instance(rel, rojo) if rojo else None
        full = details
        entry = {k: _capped(v) for k, v in info.items()} if full else None
        if entry is not None:
            if inst:
                entry["instance"] = inst
            size = len(rel) + len(json.dumps(entry)) + 8
            if used + size > MAP_FULL_BYTES:
                entry, full, details = None, False, False
        if entry is None:
            entry = {k: _capped(info[k]) for k in _LIGHT_KEYS if k in info}
            entry.setdefault("lang", "text")
            if not isinstance(entry.setdefault("module", ""), str):
                entry["module"] = ""
            if inst:
                entry["instance"] = inst
            size = len(rel) + len(json.dumps(entry)) + 8
            if used + size > MAP_FILES_BYTES:
                trunc["files"] = trunc.get("files", 0) + 1
                continue
            if level < MAP_LEVELS:
                trunc["file_details"] = trunc.get("file_details", 0) + 1
        used += size
        files[rel] = entry
        fact = _loads(fact) if full else None
        if isinstance(fact, dict):
            facts[rel] = cast(dict, _capped(fact))
    return files, facts


def _bounded_used_by(used_by: dict, trunc: dict) -> dict:
    """The import graph within MAP_USED_BY_BYTES: past it, each importer list keeps what still fits (in order)."""
    out, used, cut = {}, 0, 0
    for target, importers in used_by.items():
        used += len(target) + 8
        kept = []
        for imp in importers:
            cost = len(imp) + 4
            if used + cost > MAP_USED_BY_BYTES:
                cut += 1
                continue
            used += cost
            kept.append(imp)
        if kept:
            out[target] = kept
    if cut:
        trunc["used_by"] = cut
    return out


def _cut_map(m: dict) -> dict:
    """The map without its per-file parts, for a map.json over MAP_MAX_BYTES (not expected within the caps)."""
    out = {k: v for k, v in m.items() if k not in ("files", "used_by", "rojo")}
    out.update(files={}, used_by={}, rojo={})
    out["truncated"] = dict(m.get("truncated") or {}, files=len(m.get("files") or {}), map_cut=1)
    return out


MAX_MANIFEST_BYTES = 1024 * 1024   # default.project.json and other fixed-name files the scan reads itself


def _lstat_kind(p: Path) -> str:
    """"file" or "dir" for a regular file or folder that is not a link, else "" (lstat: links are not followed)."""
    try:
        mode = os.lstat(p).st_mode
    except (OSError, ValueError):
        return ""
    return "file" if stat.S_ISREG(mode) else "dir" if stat.S_ISDIR(mode) else ""


def _detect_engine(project: Path, files: dict) -> str:
    rojo = _lstat_kind(project / "default.project.json") == "file"
    if rojo or any(i["lang"] == "luau" for i in files.values()):
        return "roblox (rojo)" if rojo else "roblox"
    if "project.godot" in files or any(f.endswith((".gd", ".tscn")) for f in files):
        return "godot"
    if any(f.endswith(".uproject") for f in files):
        return "unreal"
    if _lstat_kind(project / "ProjectSettings") == "dir" and _lstat_kind(project / "Assets") == "dir":
        return "unity"
    if any(i["lang"] == "python" for i in files.values()):
        return "python"
    if "package.json" in files or _lstat_kind(project / "package.json") == "file":
        return "node"
    if "Cargo.toml" in files:
        return "rust"
    if "go.mod" in files:
        return "go"
    return "generic"


def _rojo_mapping(project: Path) -> dict:
    """`$path` -> DataModel path from default.project.json (see _rojo_read)."""
    return _rojo_read(project)[0]


def _rojo_read(project: Path) -> tuple[dict, int]:
    """(`$path` -> DataModel path, number of entries left out). A regular, non-link file of at most MAX_MANIFEST_BYTES:
    a clone can ship it as a link to /dev/zero, a FIFO or another project's file (whose keys and paths would then
    appear in the map). Bounded by ROJO_MAX_ENTRIES / ROJO_MAX_NAME / ROJO_MAX_CHARS: each entry's DataModel path
    repeats every name above it, so a deep chain of long names above many entries multiplied the manifest's size (551
    KB once made a 246 MB map). An entry whose path is too long is left out with everything below it."""
    text = read_project_text(project / "default.project.json", MAX_MANIFEST_BYTES)
    if text is None:
        return {}, 0
    try:
        tree = json.loads(text).get("tree", {})
    except (ValueError, AttributeError, RecursionError):
        return {}, 0
    out: dict[str, str] = {}
    left, skipped = ROJO_MAX_CHARS, 0

    def walk(node, prefix: str) -> None:
        nonlocal left, skipped
        for k, v in node.items():
            if k.startswith("$") or not isinstance(v, dict):
                continue
            inst = f"{prefix}.{k}" if prefix else k
            if len(inst) > ROJO_MAX_NAME:      # nesting depth is bounded by this too (each level adds a character)
                skipped += 1
                continue
            p = v.get("$path")
            if isinstance(p, str):
                key = p.replace("\\", "/").strip("/")
                old = out.get(key)
                cost = len(key) + len(inst) - (len(key) + len(old) if old is not None else 0)
                if len(key) > ROJO_MAX_NAME or cost > left or (old is None and len(out) >= ROJO_MAX_ENTRIES):
                    skipped += 1
                else:
                    out[key] = inst
                    left -= cost
            walk(v, inst)
    if isinstance(tree, dict):
        try:
            walk(tree, "")
        except RecursionError:      # nesting too deep to be a real project file
            return {}, 0
    return dict(sorted(out.items())), skipped


def _rojo_instance(rel: str, rojo: dict) -> str | None:
    """DataModel path Rojo gives a file (`src/server/Shop.server.luau` -> ServerScriptService.Server.Shop): the
    longest `$path` that is the file or one of its folders, looked up by the file's own prefixes (one dict lookup per
    folder level; comparing every file with every entry took 39 s for 3000 files and 30,000 entries)."""
    parts = rel.split("/")
    best = None
    for k in range(len(parts), 0, -1):
        path = "/".join(parts[:k])
        if path in rojo:
            best = (path, rojo[path])
            break
    if not best:
        return None
    path, inst = best
    if rel == path:
        return inst
    if not rel.endswith((".lua", ".luau", ".json", ".txt", ".csv", ".toml")) or rel.endswith(".meta.json"):
        return None
    parts = rel[len(path) + 1:].split("/")
    name = parts[-1]
    for suf in (".server.luau", ".server.lua", ".client.luau", ".client.lua", ".model.json", ".luau", ".lua",
                ".json", ".txt", ".csv", ".toml"):
        if name.endswith(suf):
            name = name[: -len(suf)]
            break
    parts = parts[:-1] + ([] if name == "init" else [name])
    return ".".join([inst] + parts)


# ---- dependency resolution ---------------------------------------------------------------------
PY_SUFFIX_PARTS = 16            # dotted names a Python file answers to: its path's last 1..16 parts (`a.b.c`, `b.c`, `c`)
GRAPH_MAX_STEPS = 5_000_000     # folder-table steps the whole resolution may take; past it nothing more resolves


def _graph(files: dict):
    """Resolve each file's deps to project files. Returns (used_by, resolve(importer, dep)); `resolve.work["over"]`
    says the step budget ran out (the rest resolved to nothing).

    Folders are numbered once (a trie of (parent, name) -> id) so no folder prefix string is ever built: walking the
    prefixes of a deep importer as strings for every dependency cost O(depth x path length) each (20,000 files with
    40 importers 100 folders deep: 10 s)."""
    py_index: dict[str, list[str]] = defaultdict(list)
    luau_names: dict[str, list[str]] = defaultdict(list)
    paths = set(files)
    for rel, i in files.items():
        if i["lang"] == "python":
            parts = rel.rsplit(".", 1)[0].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            name = ""
            for part in reversed(parts[-PY_SUFFIX_PARTS:]):      # `c`, `b.c`, `a.b.c`: no longer than a dependency
                name = f"{part}.{name}" if name else part         # can be (MAP_NAME_CHARS)
                if len(name) > MAP_NAME_CHARS:
                    break
                py_index[name].append(rel)
        if i["lang"] == "luau" or (i.get("instance") and rel.endswith(".json")):
            luau_names[i["module"]].append(rel)
            if i.get("instance"):
                last = i["instance"].rsplit(".", 1)[-1]
                if last != i["module"]:
                    luau_names[last].append(rel)
    cache: dict[tuple, str | None] = {}
    folder_ids: dict[tuple[int, str], int] = {}
    chains: dict[str, list[int]] = {}
    nearest: dict[tuple[str, str], tuple[dict[int, list], list[tuple[int, int]]]] = {}
    found: dict[tuple[int, str, str], list[str]] = {}
    work = {"steps": 0, "over": False}

    def chain(rel: str) -> list[int]:
        """Folder ids from the project root (0) down to the folder holding `rel`; index = depth."""
        c = chains.get(rel)
        if c is None:
            c, fid = [0], 0
            for part in rel.split("/")[:-1]:
                nxt = folder_ids.get((fid, part))
                if nxt is None:
                    nxt = folder_ids[(fid, part)] = len(folder_ids) + 1
                fid = nxt
                c.append(fid)
            chains[rel] = c
        return c

    def table(kind: str, name: str, cands: list[str]):
        """(folder id -> the two best candidates under it by (length, path), [(depth, folder id)] deepest first),
        built once per name."""
        got = nearest.get((kind, name))
        if got is None:
            t: dict[int, list] = {}
            depth: dict[int, int] = {}
            for c in cands:
                key = (len(c), c)
                ids = chain(c)
                work["steps"] += len(ids)
                for d, fid in enumerate(ids):
                    depth[fid] = d
                    top = t.setdefault(fid, [])
                    if len(top) < 2 or key < top[-1]:
                        top.append(key)
                        top.sort()
                        del top[2:]
            got = nearest[(kind, name)] = (t, sorted(((depth[f], f) for f in t), reverse=True))
        return got

    def pick(importer: str, kind: str, name: str, cands: list[str]) -> str | None:
        """The candidate sharing the most leading folders with the importer, then the shortest path, then by name
        (the importer itself excluded): the deepest of the importer's folders that holds a candidate, in a per-name
        table (comparing every pair was quadratic in same-named modules: 6000 `X.luau` files, 50 s). Per folder and
        name the first two distinct candidates found are kept: the importer is at most one of them."""
        if not cands or work["over"]:
            return None
        ids = chain(importer)
        key = (ids[-1], kind, name)
        firsts = found.get(key)
        if firsts is None:
            t, order = table(kind, name, cands)
            firsts = []
            if len(order) <= len(ids):      # walk whichever is shorter: the table's folders or the importer's
                work["steps"] += len(order)
                levels = [fid for d, fid in order if d < len(ids) and ids[d] == fid]
            else:
                work["steps"] += len(ids)
                levels = [fid for fid in reversed(ids) if fid in t]
            for fid in levels:
                for _len, c in t[fid]:
                    if c not in firsts:
                        firsts.append(c)
                if len(firsts) >= 2:
                    break
            found[key] = firsts
            if work["steps"] > GRAPH_MAX_STEPS:
                work["over"] = True
        for c in firsts:
            if c != importer:
                return c
        return None

    def resolve(importer: str, dep: str) -> str | None:
        lang = files[importer]["lang"]
        # per importer, not per folder: the importer itself is excluded, so two files in one folder can differ
        # (a cache by folder once gave a file the module its neighbour resolved to, or nothing when that was itself)
        if lang == "python":
            return pick(importer, "py", dep, py_index.get(dep, []))
        if lang == "luau":
            return pick(importer, "luau", dep, luau_names.get(dep, []))
        key = (posixpath.dirname(importer), lang, dep)
        if key in cache:
            hit = cache[key]
            return hit if hit != importer else None
        hit = None
        if lang in ("js", "ts") and dep.startswith("."):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), dep))
            for cand in [base] + [base + e for e in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".json",
                                                     ".vue", ".svelte")] + \
                        [base + "/index" + e for e in (".ts", ".tsx", ".js", ".jsx", ".mjs")]:
                if cand in paths:
                    hit = cand
                    break
        cache[key] = hit
        return hit if hit != importer else None

    used = defaultdict(set)
    for rel, info in files.items():
        for d in info.get("deps", []):
            target = resolve(rel, d)
            if target:
                used[target].add(rel)
    resolve.work = work         # type: ignore[attr-defined]
    return {k: sorted(v) for k, v in sorted(used.items())}, resolve


# ---- conventions -------------------------------------------------------------------------------
def _defined_names(info: dict) -> set[str]:
    return {s.replace(":", ".").split(".")[-1] for s in info.get("symbols", [])}


def _test_root(rel: str) -> str:
    parts = rel.split("/")[:-1]
    for k, p in enumerate(parts):
        if p in TEST_DIRS:
            return "/".join(parts[: k + 1]) + "/"
    return ("/".join(parts) + "/") if parts else "./"


def _conventions(files: dict, facts: dict, used_by: dict, resolve) -> dict:
    """Observed, not inferred: every entry names the files it was seen in."""
    src = [r for r, i in files.items() if i["lang"] in CODE_LANGS and not is_test(r)]
    n_src = len(src)
    # exclusive sites
    by_op = defaultdict(list)
    for rel, f in facts.items():
        if is_test(rel) or rel.endswith(DOC_EXT):
            continue
        for op, c in (f.get("ops") or {}).items():
            if c:
                by_op[op].append(rel)
    print_langs = {"python", "luau", "js", "ts"}
    exclusive = []
    if n_src >= 6:
        for op in sorted(set(by_op) | ABSENCE_OPS):
            hits = sorted(by_op.get(op, []))
            dirs = {posixpath.dirname(h) for h in hits}
            if 1 <= len(hits) <= 2:
                exclusive.append({"op": op, "only_in": hits, "of_files": n_src})
            elif 3 <= len(hits) <= 6 and len(dirs) == 1 and dirs != {""}:
                exclusive.append({"op": op, "only_in": [dirs.pop() + "/"], "of_files": n_src})
            elif not hits and op in ABSENCE_OPS and any(files[r]["lang"] in print_langs for r in src):
                exclusive.append({"op": op, "only_in": [], "of_files": n_src})
    # hub APIs: names other files import or call, resolved to the defining file
    calls = defaultdict(set)
    defined: dict[str, set[str]] = {}      # per target once: per use it was uses x symbols
    for rel, f in facts.items():
        if rel not in files:
            continue
        for dep, name in f.get("uses") or []:
            target = resolve(rel, dep)
            if target and target != rel:
                names = defined.get(target)
                if names is None:
                    names = defined[target] = _defined_names(files[target])
                if name in names:
                    calls[(target, name)].add(rel)
    hubs = sorted(((t, n, sorted(c)) for (t, n), c in calls.items() if len(c) >= 3),
                  key=lambda x: (-len(x[2]), files[x[0]]["module"], x[1], x[0]))
    hub_apis = [{"api": f"{files[t]['module']}.{n}", "callers": len(c), "files": c[:MAP_HUB_FILES], "defined_in": t}
                for t, n, c in hubs[:12]]
    hub_modules = [{"file": rel, "used_by": len(v)} for rel, v in
                   sorted(used_by.items(), key=lambda kv: (-len(kv[1]), kv[0])) if len(v) >= 3][:8]
    config_modules = {}
    for rel, i in files.items():
        if i.get("config_keys") and len(config_modules) < MAP_CONFIG_MODULES:
            config_modules[rel] = {"keys": len(i["config_keys"]), "used_by": len(used_by.get(rel, []))}
    config_files = sorted(r for r in files if (("/" not in r and (r in CONFIG_FILES or r.endswith(".project.json")))
                                               or (any(p in CONFIG_DIRS for p in r.split("/")[:-1])
                                                   and not is_test(r))))[:12]
    tests = [r for r in files if is_test(r) and files[r]["lang"] in CODE_LANGS]
    test_dirs = Counter(_test_root(r) for r in tests)
    entries = sorted(r for r in src if (facts.get(r) or {}).get("entry"))
    return {"hub_apis": hub_apis,
            "exclusive_sites": exclusive,
            "config_modules": config_modules,
            "hub_modules": hub_modules,
            "config_files": config_files,
            "tests": {"files": len(tests),
                      "dirs": [d for d, _ in sorted(test_dirs.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]},
            "entry_points": {"count": len(entries), "files": entries[:8]},
            "source_files": n_src}


def _code(name) -> str:
    """A name from the project (a file, folder, symbol) as a Markdown code span that nothing in the name can close
    (a backtick in a file name), with control characters escaped and conversation-control tags neutralised: the same
    quoting packs use (devmem.pack._code). PROJECT.md and these lines are loaded into prompts."""
    from .pack import _code as code
    return code(display_name(name))


def _text(name) -> str:
    """A name shown outside a code span: escaped like _code, without the span."""
    from .pack import _neutral
    return _neutral(display_name(name))


def _fmt_files(rels: list[str], limit: int = 3) -> str:
    shown = ", ".join(_code(r) for r in rels[:limit])
    return shown + (f" and {len(rels) - limit} more" if len(rels) > limit else "")


def convention_lines(m: dict) -> list[str]:
    """Human-readable convention hints, each traceable to observed evidence. Most useful first."""
    c, lines = m.get("conventions", {}), []
    for e in c.get("exclusive_sites", []):
        if e["only_in"]:
            lines.append(f"{_code(e['op'])} appears only in {_fmt_files(e['only_in'])} (observed); "
                         f"reuse that site instead of adding another.")
    for rel, info in c.get("config_modules", {}).items():
        lines.append(f"Tunables/settings live in {_code(rel)} ({info['keys']} keys, used by {info['used_by']} files); "
                     f"add new constants there.")
    for h in c.get("hub_apis", [])[:6]:
        where = f"; defined in {_code(h['defined_in'])}" if h.get("defined_in") else ""
        lines.append(f"{_code(str(h['api']) + '(...)')} is a shared entry point, used from {h['callers']} files "
                     f"(observed{where}).")
    for h in c.get("hub_modules", [])[:5]:
        lines.append(f"{_code(h['file'])} is imported by {h['used_by']} files (observed).")
    for e in c.get("exclusive_sites", []):
        if not e["only_in"]:
            lines.append(f"{_code(e['op'])} appears in none of the {e.get('of_files', 'non-test')} non-test source "
                         f"files (observed).")
    if c.get("config_files"):
        lines.append(f"Project config files: {_fmt_files(c['config_files'], 6)}.")
    t = c.get("tests") or {}
    if t.get("files"):
        lines.append(f"Tests: {t['files']} files, mostly under {_fmt_files(t['dirs'])} (observed).")
    return lines


# ---- PROJECT.md ----------------------------------------------------------------------------------
def _file_line(rel: str, i: dict) -> str:
    bits = []
    if i.get("instance"):
        bits.append(_text(i["instance"]))
    if i.get("symbols"):
        bits.append("fn: " + ", ".join(_text(str(s).split(".")[-1].split(":")[-1]) for s in i["symbols"][:8]))
    if i.get("remotes_defined"):
        bits.append("defines remotes: " + ", ".join(_text(r) for r in i["remotes_defined"]))
    if i.get("remotes_used"):
        bits.append("remotes: " + ", ".join(_text(r) for r in i["remotes_used"]))
    if i.get("config_keys"):
        bits.append(f"{len(i['config_keys'])} config keys")
    return f"- {_code(rel)}" + (" — " + "; ".join(bits) if bits else "")


def _dir_lines(m: dict, depth: int | None) -> list[str]:
    groups = defaultdict(list)
    for rel in m["files"]:
        parts = rel.split("/")[:-1]
        key = "/".join(parts if depth is None else parts[:depth])
        groups[key].append(rel)
    used = m.get("used_by", {})
    lines = []
    for d in sorted(groups):
        rels = groups[d]
        langs = Counter(m["files"][r]["lang"] for r in rels)
        lang_s = ", ".join(f"{l} {n}" for l, n in sorted(langs.items(), key=lambda kv: (-kv[1], kv[0]))[:3])
        key_files = sorted(rels, key=lambda r: (-len(used.get(r, [])), -len(m["files"][r].get("symbols", [])), r))[:3]
        ks = ", ".join(_code(posixpath.basename(r)) + (f" (used by {len(used[r])})" if used.get(r) else "")
                       for r in key_files)
        lines.append(f"- {_code((d or '.') + '/')} — {len(rels)} files ({lang_s}); key: {ks}")
    return lines


def _displayable(obj):
    """A copy of map data with every string (keys too) passed through display_name."""
    if isinstance(obj, str):
        return display_name(obj)
    if isinstance(obj, dict):
        return {(display_name(k) if isinstance(k, str) else k): _displayable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_displayable(x) for x in obj]
    return obj


def render_project_md(m: dict) -> str:
    """Compact, always-loadable map (also readable in any Markdown viewer / Obsidian). Lists every
    file for small projects; beyond about MD_MAX_TOKENS it summarises by directory. Every name is shown with
    control and bidi characters escaped (display_name), so nothing in a file or folder name can start a line, and in a
    code span it cannot close (_code) or with conversation-control tags neutralised (_text)."""
    m = cast(dict, _displayable(m))
    head = [f"# {_text(m['project'])} — project map", "",
            f"Engine: {m['engine']}. Files indexed: {len(m['files'])}. Built deterministically "
            f"({m['llm_tokens_used']} LLM tokens); content last changed {m['scanned_at']}.", ""]
    si = m.get("scan") or {}
    if si.get("incomplete"):
        head += [f"Scan incomplete: it ran out of time with {si.get('pending', 0)} files still to read; the next "
                 "scan continues.", ""]
    if si.get("unsafe_names"):
        head += [f"Skipped {si['unsafe_names']} files whose names hold control or bidi characters (not indexed).", ""]
    head += _cut_notes(m)
    if m.get("rojo"):
        shown = list(m["rojo"].items())[:MD_ROJO_LINES]
        more = len(m["rojo"]) - len(shown)
        head += (["## Rojo mapping"] + [f"- {_code(k)} -> {_text(v)}" for k, v in shown]
                 + ([f"- ... and {more} more (see index/map.json)"] if more > 0 else []) + [""])
    conv = convention_lines(m)
    if conv:
        head += ["## Conventions (observed)"] + [f"- {l}" for l in conv] + [""]
    listed = [r for r in m["files"] if not r.endswith(DOC_EXT)]
    if len(listed) <= MD_MAX_TOKENS:        # every line is at least a token: more lines cannot fit
        body = ["## Files"] + [_file_line(r, m["files"][r]) for r in listed]
        if estimate_tokens("\n".join(head + body)) <= MD_MAX_TOKENS:
            return _md_capped(head + body)
    note = (f"## Files by directory\n{len(m['files'])} files; summarised by directory to keep this page loadable "
            f"(full per-file entries are in index/map.json).")
    for depth in (None, 4, 3, 2, 1):
        lines = _dir_lines(m, depth)
        if estimate_tokens("\n".join(head + [note] + lines)) <= MD_MAX_TOKENS:
            return _md_capped(head + [note] + lines)
    budget = MD_MAX_TOKENS - estimate_tokens("\n".join(head + [note])) - 20
    kept, used = [], 0
    for l in lines:
        cost = estimate_tokens(l) + 1
        if used + cost > budget:
            break
        kept.append(l)
        used += cost
    kept.append(f"- ... and {len(lines) - len(kept)} more top-level directories (see index/map.json)")
    return _md_capped(head + [note] + kept)


def _cut_notes(m: dict) -> list[str]:
    """PROJECT.md lines saying what the map left out (its "truncated" counts) or that it was built reduced."""
    t = m.get("truncated")
    t = t if isinstance(t, dict) else {}
    bits = []
    for key, what in (("files", "files left out"), ("file_details", "files listed without symbols or dependencies"),
                      ("rojo_entries", "Rojo entries left out"), ("used_by", "import links left out")):
        n = t.get(key)
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            bits.append(f"{n} {what}")
    if t.get("graph"):
        bits.append("dependency resolution stopped at its step budget")
    if t.get("map_cut"):
        bits.append("only the summary kept")
    out = [f"Map truncated at its size caps: {'; '.join(bits)} (see index/map.json `truncated`).", ""] if bits else []
    level = m.get("reduced")
    if isinstance(level, int) and not isinstance(level, bool) and level > 0:
        out += ["Map reduced: building it ran out of time, so the Rojo mapping, import graph and conventions are left "
                "out" + (" and files are listed by path only" if level >= MAP_LEVELS else "")
                + "; `devmem scan` builds the full map.", ""]
    return out


def _md_capped(lines: list[str]) -> str:
    """The page, at most MD_MAX_BYTES: cut at a line, with a note (the head alone can be long: many long names)."""
    text = "\n".join(lines) + "\n"
    if len(text) <= MD_MAX_BYTES // 4 or len(text.encode("utf-8")) <= MD_MAX_BYTES:
        return text
    note = "\n- ... (PROJECT.md cut at its size cap; see index/map.json)\n"
    cut = text.encode("utf-8")[: MD_MAX_BYTES - len(note)].decode("utf-8", errors="ignore")
    nl = cut.rfind("\n")
    return (cut[:nl] if nl > 0 else cut) + note
