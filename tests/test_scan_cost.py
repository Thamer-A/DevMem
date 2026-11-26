"""build_map cost: dependency resolution among many same-named modules, Rojo `$path` lookup with many entries.

Before: 6000 folders each holding `X.luau` with `require(game.X)` took 50 s to scan (every importer compared with
every candidate), and 3000 files with 30,000 Rojo entries 39 s (every file tested against every entry). The answers
must stay what the pairwise comparisons gave; the reference implementations below are the old code.
"""
from __future__ import annotations

import random
import time
from pathlib import Path

import pytest

from devmem import scan as S


def _old_common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a.split("/")[:-1], b.split("/")[:-1]):
        if x != y:
            break
        n += 1
    return n


def _old_pick(importer: str, cands: list[str]) -> str | None:
    cands = [c for c in cands if c != importer]
    if not cands:
        return None
    return min(cands, key=lambda c: (-_old_common_prefix(importer, c), len(c), c))


def _old_rojo_instance(rel: str, rojo: dict) -> str | None:
    best = None
    for path, inst in rojo.items():
        if rel == path or rel.startswith(path + "/"):
            if best is None or len(path) > len(best[0]):
                best = (path, inst)
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


def _random_tree(rng: random.Random, n: int) -> dict:
    files = {}
    dirs = ["", "a", "a/b", "a/bb", "ab", "a/b/c", "x/y", "x/y/z", "src/shared", "src/server/Mods"]
    names = ["X", "Y", "Config", "init"]
    for _ in range(n):
        d = rng.choice(dirs)
        name = rng.choice(names)
        rel = f"{d}/{name}{rng.randint(0, 2) or ''}.luau".lstrip("/")
        files[rel] = {"lang": "luau", "module": S.Path(rel).name.split(".")[0], "deps": [rng.choice(names)]}
    return files


@pytest.mark.parametrize("seed", range(20))
def test_dependency_resolution_matches_the_pairwise_choice(seed):
    rng = random.Random(seed)
    files = _random_tree(rng, 40)
    _used_by, resolve = S._graph(files)
    by_module: dict[str, list[str]] = {}
    for rel, info in files.items():
        by_module.setdefault(info["module"], []).append(rel)
    for rel, info in files.items():
        for dep in info["deps"]:
            assert resolve(rel, dep) == _old_pick(rel, by_module.get(dep, [])), (rel, dep)


def test_python_resolution_matches_the_pairwise_choice():
    files = {}
    for rel in ("pkg/util.py", "pkg/sub/util.py", "other/util.py", "util.py", "pkg/sub/deep/m.py", "pkg/m.py",
                "pkg/sub/__init__.py"):
        files[rel] = {"lang": "python", "module": S.Path(rel).stem, "deps": ["util", "sub"]}
    _used_by, resolve = S._graph(files)
    index: dict[str, list[str]] = {}
    for rel in files:
        parts = rel.rsplit(".", 1)[0].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        for k in range(len(parts)):
            index.setdefault(".".join(parts[k:]), []).append(rel)
    for rel in files:
        for dep in ("util", "sub"):
            assert resolve(rel, dep) == _old_pick(rel, index.get(dep, [])), (rel, dep)


def test_rojo_lookup_matches_the_scan_over_every_entry():
    rojo = {"src": "ReplicatedStorage", "src/server": "ServerScriptService", "src/server/Shop.server.luau": "X.Shop",
            "srcx": "Other", "src/shared/Deep": "ReplicatedStorage.Deep", "a/b": "A.B"}
    for rel in ("src/a.luau", "src/server/Shop.server.luau", "src/server/k/init.luau", "srcx/q.lua", "srcy/q.lua",
                "src/shared/Deep/x.json", "src/shared/Deeper/x.luau", "a/b", "a/bc/x.luau", "a/b/c/d.luau"):
        assert S._rojo_instance(rel, rojo) == _old_rojo_instance(rel, rojo), rel


def _same_named(n: int) -> dict:
    return {f"d{i}/X.luau": {"lang": "luau", "module": "X", "deps": ["X"]} for i in range(n)}


def test_many_same_named_modules_resolve_in_linear_time():
    files = _same_named(4000)
    t = time.perf_counter()
    used_by, _resolve = S._graph(files)
    took = time.perf_counter() - t
    assert took < 2.0, f"{took:.2f}s for 4000 same-named modules (about 0.05 s measured; 20 s before)"
    assert used_by == {"d0/X.luau": sorted(f"d{i}/X.luau" for i in range(1, 4000)), "d1/X.luau": ["d0/X.luau"]}


def test_many_rojo_entries_are_looked_up_per_file(tmp_path: Path):
    import json
    tree = {"$className": "DataModel"}
    for j in range(30_000):
        tree[f"n{j}"] = {"$path": f"src/p{j}"}
    (tmp_path / "default.project.json").write_text(json.dumps({"name": "x", "tree": tree}), encoding="utf-8")
    rojo = S._rojo_mapping(tmp_path)
    assert len(rojo) == 30_000
    files = {f"src/p{i % 50}/m{i}.luau": {"lang": "luau", "module": f"m{i}", "deps": []} for i in range(3000)}
    t = time.perf_counter()
    m = S.build_map(tmp_path, files, {})
    took = time.perf_counter() - t
    assert took < 3.0, f"{took:.2f}s (about 0.3 s measured; 39 s before for the whole scan)"
    assert m["files"]["src/p7/m7.luau"]["instance"] == "n7.m7"
    md = S.render_project_md(m)
    assert len(md) < 40_000, "PROJECT.md lists a bounded number of Rojo entries"


@pytest.mark.stress
def test_many_same_named_python_and_luau_modules_scan_quickly(tmp_path: Path):
    """The reviewer's shape end to end: 6000 folders, each `X.luau` requiring game.X, scanned and rescanned."""
    project = tmp_path / "p"
    for i in range(6000):
        d = project / f"d{i}"
        d.mkdir(parents=True)
        (d / "X.luau").write_text("local a = require(game.X)\nreturn a\n", encoding="utf-8")
    t = time.perf_counter()
    S.scan(project, tmp_path / "store")
    first = time.perf_counter() - t
    (project / "d5" / "X.luau").write_text("local a = require(game.X)\nreturn 1\n", encoding="utf-8")
    t = time.perf_counter()
    S.scan(project, tmp_path / "store")
    again = time.perf_counter() - t
    assert first < 30 and again < 5, (first, again)


def _deep_importers(n_targets: int, n_importers: int, depth: int) -> dict:
    files = {f"m{i}.py": {"lang": "python", "module": f"m{i}", "symbols": [], "deps": []} for i in range(n_targets)}
    deep = "/".join(f"d{k}".ljust(20, "x") for k in range(depth))
    for j in range(n_importers):
        files[f"{deep}/imp{j}.py"] = {"lang": "python", "module": f"imp{j}", "symbols": [],
                                      "deps": [f"m{i}" for i in range(n_targets)]}
    return files


def test_deep_importers_resolve_in_near_linear_work():
    """Every dependency of an importer 100 folders deep walked its folder prefixes as strings: 5000 targets x 40
    importers took 10 s, 20,000 x 40 78 s (about 0.4 s and 1.8 s now)."""
    files = _deep_importers(5000, 40, 100)
    t = time.perf_counter()
    used_by, resolve = S._graph(files)
    took = time.perf_counter() - t
    assert len(used_by) == 5000 and len(used_by["m7.py"]) == 40
    deps = 5000 * 40
    assert resolve.work["steps"] < 2 * deps, resolve.work       # about one step per (folder, name), not per depth
    assert took < 5.0, f"{took:.2f}s"


def test_dependency_resolution_stops_at_its_step_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "GRAPH_MAX_STEPS", 500)
    m = S.build_map(tmp_path, _deep_importers(300, 3, 5), {})
    assert m["truncated"]["graph"] == 1 and 0 < len(m["used_by"]) < 300
    assert "step budget" in S.render_project_md(m)
