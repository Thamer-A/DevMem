"""The project map (map.json, PROJECT.md) is bounded whatever the project holds, and a map build that stops the
refresh child is demoted like file extraction.

A default.project.json under 1 MB (a chain of nested names above many `$path` entries) once made a 246 MB to 1 GB
map.json (each entry's DataModel path repeats every name above it), 19 s and 3-4 GB of memory, and since the map build
was not marked, every later prompt spent the whole 8 s refresh budget on it again."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from devmem import index as I
from devmem import refresh as R
from devmem import scan as S
from devmem.index import Index
from devmem.store import ensure_store


def _manifest(depth: int, siblings: int, name_len: int) -> str:
    node: dict = {f"s{i}": {"$path": f"p{i}"} for i in range(siblings)}
    for _ in range(depth):
        node = {"a" * name_len: node}
    return json.dumps({"name": "x", "tree": node}, separators=(",", ":"))


@pytest.mark.parametrize("depth,siblings,name_len", [
    (300, 2000, 40),        # far past the name cap: every entry left out
    (24, 30000, 20),        # just under the name cap (504 characters), as many entries as fit in 1 MB
])
def test_a_nested_rojo_manifest_cannot_amplify_the_map(tmp_path, depth, siblings, name_len):
    text = _manifest(depth, siblings, name_len)
    assert len(text) < S.MAX_MANIFEST_BYTES
    (tmp_path / "default.project.json").write_text(text, encoding="utf-8")
    files = {f"p{i}/m.luau": {"lang": "luau", "module": "m", "symbols": [], "deps": []} for i in range(2000)}
    t = time.perf_counter()
    m = S.build_map(tmp_path, files, {})
    out = json.dumps(m, indent=1)
    md = S.render_project_md(m)
    took = time.perf_counter() - t
    # before: 24.6 MB and 15.1 MB of map (the second shape also 30 MB more of per-file instances)
    assert len(out) < 4 * len(text) + 2_000_000, len(out)
    assert sum(len(k) + len(v) for k, v in m["rojo"].items()) <= S.ROJO_MAX_CHARS
    assert all(len(v) <= S.ROJO_MAX_NAME for v in m["rojo"].values())
    assert m["truncated"]["rojo_entries"] > 0 and "Rojo entries left out" in md
    assert len(md.encode("utf-8")) <= S.MD_MAX_BYTES
    assert took < 10, took


def test_a_real_rojo_manifest_is_unchanged(tmp_path):
    tree = {"$className": "DataModel",
            "ReplicatedStorage": {"$className": "ReplicatedStorage", "Shared": {"$path": "src/shared"}},
            "ServerScriptService": {"$className": "ServerScriptService", "Server": {"$path": "src/server"}}}
    (tmp_path / "default.project.json").write_text(json.dumps({"name": "x", "tree": tree}), encoding="utf-8")
    files = {"src/server/Shop.server.luau": {"lang": "luau", "module": "Shop", "symbols": [], "deps": []}}
    m = S.build_map(tmp_path, files, {})
    assert m["rojo"] == {"src/shared": "ReplicatedStorage.Shared", "src/server": "ServerScriptService.Server"}
    assert m["files"]["src/server/Shop.server.luau"]["instance"] == "ServerScriptService.Server.Shop"
    assert "truncated" not in m and "reduced" not in m


def test_file_entries_with_huge_name_lists_are_capped(tmp_path):
    names = [f"{'n' * 190}{i}" for i in range(600)]
    info = {"lang": "python", "module": "m", "symbols": names, "deps": names, "config_keys": names}
    files = {f"d/m{i}.py": info for i in range(600)}        # before: 600 x 360 KB, a 230 MB map
    t = time.perf_counter()
    m = S.build_map(tmp_path, files, {})
    out = json.dumps(m)
    assert len(out) < S.MAP_FILES_BYTES + 1_000_000, len(out)
    assert time.perf_counter() - t < 20
    full = [e for e in m["files"].values() if "symbols" in e]
    assert full and all(len(e["symbols"]) <= S.MAP_LIST_ITEMS for e in full)
    assert all(len(s) <= S.MAP_NAME_CHARS for e in full for s in e["symbols"])
    assert len(m["files"]) == 600 and m["truncated"]["file_details"] == 600 - len(full)
    light = m["files"]["d/m599.py"]
    assert light["lang"] == "python" and light["module"] == "m" and "symbols" not in light
    assert "listed without symbols" in S.render_project_md(m)


def test_past_the_file_caps_entries_go_light_then_out(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "MAP_FULL_BYTES", 20_000)
    monkeypatch.setattr(S, "MAP_FILES_BYTES", 60_000)
    files = {f"f{i:05d}.py": {"lang": "python", "module": f"f{i}", "symbols": [f"fn{i}"], "deps": [], "lines": 3}
             for i in range(3000)}
    m = S.build_map(tmp_path, files, {})
    t = m["truncated"]
    names = list(m["files"])
    assert names == sorted(names) and names[0] == "f00000.py"          # kept in path order, a prefix of them
    assert t["files"] == 3000 - len(names) > 0 and t["file_details"] > 0
    assert len(json.dumps(m["files"])) < 70_000
    assert "files left out" in S.render_project_md(m)


def test_the_import_graph_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "MAP_USED_BY_BYTES", 10_000)
    files = {"core.py": {"lang": "python", "module": "core", "symbols": [], "deps": []}}
    files.update({f"user{i}.py": {"lang": "python", "module": f"user{i}", "symbols": [], "deps": ["core"]}
                  for i in range(2000)})
    m = S.build_map(tmp_path, files, {})
    assert 0 < len(m["used_by"]["core.py"]) < 2000 and m["truncated"]["used_by"] == 2000 - len(m["used_by"]["core.py"])
    assert m["conventions"]["hub_modules"][0] == {"file": "core.py", "used_by": 2000}    # counted before the cut


def test_conventions_list_a_bounded_number_of_modules_and_callers(tmp_path):
    files = {f"cfg{i}/config.py": {"lang": "python", "module": "config", "symbols": [], "deps": [],
                                   "config_keys": ["A", "B"]} for i in range(200)}
    files["lib.py"] = {"lang": "python", "module": "lib", "symbols": ["helper"], "deps": []}
    facts = {}
    for i in range(300):
        files[f"u{i}.py"] = {"lang": "python", "module": f"u{i}", "symbols": [], "deps": ["lib"]}
        facts[f"u{i}.py"] = {"ops": {}, "uses": [["lib", "helper"]], "entry": False}
    c = S.build_map(tmp_path, files, facts)["conventions"]
    assert len(c["config_modules"]) == S.MAP_CONFIG_MODULES
    assert c["hub_apis"][0]["callers"] == 300 and len(c["hub_apis"][0]["files"]) == S.MAP_HUB_FILES


def test_project_md_is_cut_at_its_byte_cap():
    long = "x" * 1000
    m = {"project": "p", "engine": "roblox", "llm_tokens_used": 0, "scanned_at": "now", "files": {},
         "rojo": {f"{long}{i}": f"{long}{i}" for i in range(40)}, "used_by": {},
         "conventions": {"config_modules": {f"{long}/{i}/config.py": {"keys": 1, "used_by": 0} for i in range(60)}}}
    md = S.render_project_md(m)
    assert len(md.encode("utf-8")) <= S.MD_MAX_BYTES and md.endswith("see index/map.json)\n")


def test_an_oversized_map_is_written_as_its_summary(tmp_path, monkeypatch):
    project = tmp_path / "p"
    project.mkdir()
    for i in range(30):
        (project / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
    monkeypatch.setattr(S, "MAP_MAX_BYTES", 2000)
    m = S.scan(project, tmp_path / "store")
    assert m["files"] == {} and m["truncated"]["map_cut"] == 1 and m["truncated"]["files"] == 30
    assert (tmp_path / "store" / "index" / "map.json").stat().st_size < 4000


def test_the_scan_streams_rows_from_the_index(tmp_path, monkeypatch):
    """scan() never loads the whole files table (Index.files/facts) to build the map."""
    project = tmp_path / "p"
    project.mkdir()
    (project / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")

    def refuse(self):
        raise AssertionError("whole table loaded")
    monkeypatch.setattr(Index, "files", refuse)
    monkeypatch.setattr(Index, "facts", refuse)
    assert "a.py" in S.scan(project, tmp_path / "store")["files"]


# ---------------------------------------------------------------- a stuck map build: killed once, then demoted

MARK_TEST_BUDGET = 5.0


def _proj(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "damage.py").write_text("def apply_armor(damage, armor):\n    return damage - armor\n", encoding="utf-8")
    (project / "net.py").write_text("import damage\n\ndef send():\n    return damage.apply_armor(1, 2)\n",
                                    encoding="utf-8")
    return project, ensure_store(project / ".devmemory")


def _block_map(monkeypatch, levels: str = "0") -> None:
    monkeypatch.setenv(I.TEST_SWITCH_ENV, "1")
    monkeypatch.setenv(S.TEST_BLOCK_MAP_ENV, f"30|{levels}")
    monkeypatch.setenv(R.TEST_BUDGET_ENV, str(MARK_TEST_BUDGET))


def _map(store: Path) -> dict:
    return json.loads((store / "index" / "map.json").read_text(encoding="utf-8"))


def test_a_stuck_map_build_is_killed_once_and_then_built_reduced(tmp_path, monkeypatch):
    project, store = _proj(tmp_path)
    S.scan(project, store)
    (project / "audio.py").write_text("def play_sound(name):\n    return name\n", encoding="utf-8")
    _block_map(monkeypatch)
    t = time.perf_counter()
    assert R.refresh(project, store, 8)["state"] == "stopped"
    assert time.perf_counter() - t < MARK_TEST_BUDGET + 4.0
    t = time.perf_counter()
    out = R.refresh(project, store, 8)            # the switch still blocks level 0: a retry would block again
    assert out["state"] == "done" and out.get("stopped_map") == 1, out
    assert time.perf_counter() - t < MARK_TEST_BUDGET
    m = _map(store)
    assert m["reduced"] == 1 and "audio.py" in m["files"] and m["used_by"] == {}
    assert "Map reduced" in (store / "PROJECT.md").read_text(encoding="utf-8")
    assert R.refresh(project, store, 8)["state"] == "fresh"
    (project / "audio.py").write_text("def play_music(name):\n    return name\n", encoding="utf-8")
    assert R.refresh(project, store, 8)["state"] == "done"         # stays reduced: no second stall
    monkeypatch.delenv(S.TEST_BLOCK_MAP_ENV)
    m = S.scan(project, store, time_budget=None)                     # `devmem scan`: the full map, demotion cleared
    assert "reduced" not in m and m["used_by"] == {"damage.py": ["net.py"]}
    with Index(store) as idx:
        assert idx.map_level() == 0


def test_a_map_build_that_stops_even_reduced_lists_files_only(tmp_path, monkeypatch):
    project, store = _proj(tmp_path)
    S.scan(project, store)
    (project / "audio.py").write_text("def play_sound(name):\n    return name\n", encoding="utf-8")
    _block_map(monkeypatch, levels="01")
    assert R.refresh(project, store, 8)["state"] == "stopped"
    (project / "audio.py").write_text("def play_music(name):\n    return name\n", encoding="utf-8")
    assert R.refresh(project, store, 8)["state"] == "stopped"
    out = R.refresh(project, store, 8)
    assert out["state"] == "done" and out.get("stopped_map") == 2, out
    m = _map(store)
    assert m["reduced"] == 2 and m["conventions"] == {} and "symbols" not in m["files"]["damage.py"]


def test_a_map_demotion_expires(tmp_path):
    _project, store = _proj(tmp_path)
    with Index(store) as idx:
        idx.meta_set("map_level", json.dumps({"level": 1, "t": time.time()}))
        assert idx.map_level() == 1
        idx.meta_set("map_level", json.dumps({"level": 1, "t": time.time() - I.MAP_DEMOTE_S - 1}))
        assert idx.map_level() == 0
        for bad in ("garbage", json.dumps({"level": 9, "t": time.time()}), json.dumps([1])):
            idx.meta_set("map_level", bad)
            assert idx.map_level() == 0
