"""devmem.scan.scan: map shape, byte stability, conventions, PROJECT.md size, Roblox adapter."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import devmem.store as store_mod
from devmem.scan import MD_MAX_TOKENS, convention_lines, scan
from devmem.store import estimate_tokens

REPO = Path(__file__).resolve().parents[1]
P1 = REPO / "fixtures" / "coinrush" / "project"
BLADE = REPO / "fixtures" / "blade-swarm" / "project"
V1_KEYS = ["project", "scanned_at", "llm_tokens_used", "files", "engine", "rojo", "conventions", "used_by"]


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def no_git(monkeypatch):
    monkeypatch.setattr(store_mod, "_git_list", lambda project: None)


@pytest.fixture
def pyproj(tmp_path, no_git):
    """A small service that is not one of the hackathon fixtures."""
    root = tmp_path / "svc"
    write(root, "pyproject.toml", "[project]\nname = 'svc'\n")
    write(root, "svc/__init__.py", "")
    write(root, "svc/settings.py", "import os\nDB_PATH = os.environ.get('DB_PATH', 'svc.db')\nTIMEOUT = 5\n")
    write(root, "svc/db.py", "import sqlite3\nfrom svc.settings import DB_PATH\n\n"
                             "def connect():\n    return sqlite3.connect(DB_PATH)\n\n"
                             "def query(sql):\n    return connect().execute(sql)\n")
    write(root, "svc/log.py", "import logging\n\ndef setup():\n    logging.basicConfig(level='INFO')\n\n"
                              "def get(name):\n    return logging.getLogger(name)\n")
    for name in ("users", "orders", "billing", "reports"):
        write(root, f"svc/{name}.py",
              f"from svc.db import query\nfrom svc.log import get\nfrom svc import settings\n\n"
              f"log = get(__name__)\n\ndef list_{name}():\n    return query('SELECT id FROM {name} ')\n")
    write(root, "svc/http_client.py", "import urllib.request\n\ndef fetch(url):\n"
                                      "    return urllib.request.urlopen(url, timeout=5)\n")
    write(root, "svc/cli.py", "from svc.users import list_users\n\nif __name__ == '__main__':\n"
                              "    print(list_users())\n")
    write(root, "tests/test_users.py", "import sqlite3\n\ndef test_x():\n    sqlite3.connect(':memory:')\n")
    return root


def test_import_and_p1_fixture_scan(tmp_path):
    out = subprocess.run([sys.executable, "-c", "from devmem.scan import scan; print('ok')"], cwd=REPO / "plugin",
                         capture_output=True, text=True, encoding="utf-8")
    assert out.stdout.strip() == "ok", out.stderr
    store = tmp_path / "store"
    m = scan(P1, store)
    assert list(m) == V1_KEYS
    assert m["engine"] == "roblox (rojo)" and m["llm_tokens_used"] == 0
    assert m["rojo"]["src/server"] == "ServerScriptService.Server"
    assert m["files"]["src/shared/Remotes.luau"]["remotes_defined"]   # found without fixture-specific names
    assert "PurchaseItem" in m["files"]["src/client/controllers/ShopController.luau"]["remotes_used"]
    assert m["files"]["src/server/services/ShopService.luau"]["instance"] == \
        "ServerScriptService.Server.services.ShopService"
    assert "src/shared/Config.luau" in m["used_by"]
    ops = {e["op"]: e["only_in"] for e in m["conventions"]["exclusive_sites"]}
    assert ops["RemoteEvent/RemoteFunction creation (Instance.new)"] == ["src/shared/Remotes.luau"]
    assert ops["DataStore access"] == ["src/server/services/DataService.luau"]
    assert ops["rbxassetid literal"] == ["assets/manifest.json"]
    assert (store / "PROJECT.md").exists() and (store / "index" / "map.json").exists()
    assert not (P1 / ".devmemory").exists()


def test_blade_swarm_fixture(tmp_path):
    m = scan(BLADE, tmp_path / "store")
    assert not [f for f in m["files"] if f.startswith(".devmemory")]
    assert set(m["files"]["src/shared/Remotes.lua"]["remotes_defined"]) >= {"BuyOffer", "ShopSync", "CombatFx"}
    assert m["files"]["src/client/ShopUi.client.lua"]["remotes_used"]["BuyOffer"] == ["FireServer"]
    assert "src/shared/Config.lua" in m["conventions"]["config_modules"]


def test_no_fixture_specific_patterns_in_scanner():
    src = "".join((REPO / "plugin" / "devmem" / f).read_text(encoding="utf-8")
                  for f in ("scan.py", "extract.py", "extract_luau.py", "extract_python.py", "extract_js.py"))
    for fitted in ("Remotes.define", "Assets.get", "data.coins", "float() on money", "coins"):
        assert fitted not in src


def test_repeat_scan_is_byte_stable(pyproj, tmp_path):
    store = tmp_path / "store"
    scan(pyproj, store)
    map_p, md_p = store / "index" / "map.json", store / "PROJECT.md"
    before = (map_p.read_bytes(), md_p.read_bytes(), map_p.stat().st_mtime_ns, md_p.stat().st_mtime_ns)
    time.sleep(1.1)                              # scanned_at has one-second resolution
    stats = {}
    scan(pyproj, store, stats=stats)
    assert stats["fast_path"] is True
    assert (map_p.read_bytes(), md_p.read_bytes(), map_p.stat().st_mtime_ns, md_p.stat().st_mtime_ns) == before
    # touching a file without changing it keeps the output identical
    p = pyproj / "svc" / "db.py"
    os.utime(p, None)
    scan(pyproj, store)
    assert (map_p.read_bytes(), md_p.read_bytes()) == before[:2]
    # a fresh index (deleted db) must reproduce the same bytes too
    shutil.rmtree(store / "index" / "index.db", ignore_errors=True)
    for f in (store / "index").glob("index.db*"):
        f.unlink()
    scan(pyproj, store)
    assert (map_p.read_bytes(), md_p.read_bytes()) == before[:2]
    # a real change updates the map and scanned_at
    write(pyproj, "svc/new_mod.py", "def added():\n    pass\n")
    m = scan(pyproj, store)
    assert "svc/new_mod.py" in m["files"]
    assert m["scanned_at"] != json.loads(before[0])["scanned_at"]


def test_general_conventions(pyproj, tmp_path):
    m = scan(pyproj, tmp_path / "store")
    c = m["conventions"]
    ops = {e["op"]: e["only_in"] for e in c["exclusive_sites"]}
    assert ops["database connection"] == ["svc/db.py"]                 # the test's own connect is excluded
    assert ops["environment variable reads"] == ["svc/settings.py"]
    assert ops["network requests"] == ["svc/http_client.py"]
    assert ops["logging configuration"] == ["svc/log.py"]
    assert ops["print/console output"] == ["svc/cli.py"]
    apis = {h["api"]: h for h in c["hub_apis"]}
    assert apis["db.query"]["callers"] == 4 and apis["db.query"]["defined_in"] == "svc/db.py"
    assert apis["log.get"]["callers"] == 4
    hubs = {h["file"]: h["used_by"] for h in c["hub_modules"]}
    assert hubs["svc/db.py"] == 4 and hubs["svc/settings.py"] >= 5
    assert "svc/settings.py" in c["config_modules"]
    assert c["config_files"] == ["pyproject.toml"]
    assert c["tests"]["files"] == 1 and c["tests"]["dirs"] == ["tests/"]
    assert c["entry_points"]["files"] == ["svc/cli.py"]
    assert m["engine"] == "python"
    lines = convention_lines(m)
    assert any("database connection" in l and "svc/db.py" in l and "observed" in l for l in lines)
    assert sorted(m["used_by"]["svc/db.py"]) == ["svc/billing.py", "svc/orders.py", "svc/reports.py",
                                                 "svc/users.py"]


def test_js_graph_and_web_files(tmp_path, no_git):
    root = tmp_path / "web"
    write(root, "package.json", '{"name": "web"}\n')
    write(root, "index.html", "<link rel=stylesheet href=style.css><script src=app.js></script>\n")
    write(root, "style.css", ".board { display: grid }\n")
    write(root, "src/api.ts", "export async function getCards() { return fetch('/api/cards') }\n"
                              "export const addCard = async (c: string) => fetch('/api/cards', {method: 'POST'})\n")
    for n in ("a", "b", "c"):
        write(root, f"src/view_{n}.ts", "import { getCards, addCard } from './api'\n"
                                        "// http://example.com is not a comment start inside a string\n"
                                        f"export function render_{n}() {{ return getCards() }}\n")
    m = scan(root, tmp_path / "store")
    assert {"index.html", "style.css"} <= set(m["files"])
    assert m["files"]["src/api.ts"]["symbols"] == ["addCard", "getCards"]
    assert m["used_by"]["src/api.ts"] == ["src/view_a.ts", "src/view_b.ts", "src/view_c.ts"]
    assert {h["api"] for h in m["conventions"]["hub_apis"]} >= {"api.getCards", "api.addCard"}


def test_project_md_is_capped_for_large_projects(tmp_path, no_git):
    root = tmp_path / "big"
    for d in range(40):
        for f in range(15):
            write(root, f"pkg{d:02d}/sub/mod_{f:02d}.py",
                  f"def function_number_{f}_with_a_long_name():\n    pass\n\nclass Thing{f}:\n    pass\n")
    store = tmp_path / "store"
    m = scan(root, store)
    md = (store / "PROJECT.md").read_text(encoding="utf-8")
    assert len(m["files"]) == 600
    assert estimate_tokens(md) <= MD_MAX_TOKENS
    assert "Files by directory" in md and "pkg00/sub/" in md


def test_small_project_lists_every_file(pyproj, tmp_path):
    scan(pyproj, tmp_path / "store")
    md = (tmp_path / "store" / "PROJECT.md").read_text(encoding="utf-8")
    assert "## Files\n" in md and "`svc/db.py` — fn: connect, query" in md


def test_python_syntax_error_falls_back(tmp_path, no_git):
    root = tmp_path / "p"
    write(root, "old.py", "print 'python 2'\ndef still_found():\n    pass\n")
    m = scan(root, tmp_path / "store")
    assert m["files"]["old.py"]["symbols"] == ["still_found"]
