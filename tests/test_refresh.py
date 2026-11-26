"""devmem.refresh: the refresh a hook or an MCP call triggers runs in a child process killed at its budget.

The proof does not rely on any extractor being fast: the test-only switch (DEVMEM_TESTING=1 +
DEVMEM_TEST_BLOCK_EXTRACT) makes extraction of one file block for 30 s. Budgets are shortened with
DEVMEM_TEST_REFRESH_BUDGET (also test-only) so the default suite stays fast; tests marked `stress` use the real ones."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from devmem import index as I
from devmem import refresh as R
from devmem.index import Index
from devmem.scan import map_current, scan
from devmem.store import ensure_store

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin"
MARKER = "BLOCK_ME_51c0"
TEST_BUDGET = 5.0     # seconds; long enough for a child to start and reach the file on a loaded machine


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "damage.py").write_text("def apply_armor(damage, armor):\n    return max(0, damage - armor)\n",
                                       encoding="utf-8")
    (project / "audio.py").write_text("def play_sound(name):\n    return name\n", encoding="utf-8")
    return project, ensure_store(project / ".devmemory")


def _hostile(project: Path) -> Path:
    p = project / "hostile.py"
    p.write_text(f"# {MARKER}\ndef hostile_thing():\n    return 2\n", encoding="utf-8")
    return p


def _block(monkeypatch, secs: float = 30, budget: float | None = TEST_BUDGET, nojob: bool = False) -> None:
    monkeypatch.setenv(I.TEST_SWITCH_ENV, "1")
    monkeypatch.setenv(I.TEST_BLOCK_ENV, f"{MARKER}|{secs}")
    if budget is not None:
        monkeypatch.setenv(R.TEST_BUDGET_ENV, str(budget))
    if nojob:
        monkeypatch.setenv(R.TEST_NOJOB_ENV, "1")


def _gone(pid: int) -> bool:
    if sys.platform.startswith("linux"):
        try:     # a killed child nobody has reaped yet is a zombie: it runs nothing
            with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                return f.read().rsplit(")", 1)[1].split()[0] in ("Z", "X")
        except OSError:
            return True
    return I._pid_gone(pid)


def _child_pids(store: Path, not_pid: int | None = None) -> set[int]:
    """Process ids named by in-progress marks (a child working on a file leaves one)."""
    out = set()
    for m in (store / "index").glob(I.MARK_PREFIX + "*"):
        pid = int(m.name[len(I.MARK_PREFIX):].split(".")[0])
        if pid != not_pid:
            out.add(pid)
    return out


def _wait_for(cond, seconds: float = 20.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.05)
    return None


def _integrity(store: Path) -> str:
    conn = sqlite3.connect(str(store / "index" / "index.db"))
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------- nothing changed: no child at all

def _no_child(monkeypatch):
    from devmem import proc
    real = proc.popen

    def refuse(argv, *a, **k):
        if os.path.basename(str(argv[0])).lower().startswith("git"):
            return real(argv, *a, **k)      # git answers the file list; the refresh child is a Python
        raise AssertionError("a child was started")
    monkeypatch.setattr(proc, "popen", refuse)


def test_nothing_changed_starts_no_child(tmp_path, monkeypatch):
    project, store = _project(tmp_path)
    scan(project, store)
    _no_child(monkeypatch)
    assert R.refresh(project, store, 8)["state"] == "fresh"


def test_a_touched_file_with_the_same_content_needs_no_child(tmp_path, monkeypatch):
    project, store = _project(tmp_path)
    scan(project, store)
    later = time.time() + 100
    os.utime(project / "audio.py", (later, later))
    _no_child(monkeypatch)
    assert R.refresh(project, store, 8)["state"] == "fresh"
    with Index(store) as idx:
        assert idx.check(project, time.perf_counter() + 5)["needed"] is False


@pytest.mark.parametrize("change", ["edit", "add", "remove", "map", "mark", "large", "no map"])
def test_any_change_runs_the_child(tmp_path, monkeypatch, change):
    project, store = _project(tmp_path)
    scan(project, store)
    if change == "edit":
        (project / "audio.py").write_text("def play_music(name):\n    return name\n", encoding="utf-8")
    elif change == "add":
        (project / "new.py").write_text("def brand_new():\n    pass\n", encoding="utf-8")
    elif change == "remove":
        (project / "audio.py").unlink()
    elif change == "map":
        (store / "PROJECT.md").write_text("edited by hand\n", encoding="utf-8")
    elif change == "mark":
        (store / "index" / f"{I.MARK_PREFIX}99999999.1").write_text("", encoding="utf-8")
    elif change == "large":
        monkeypatch.setattr(R, "CHECK_MAX_FILES", 1)
    else:
        (store / "index" / "map.json").unlink()
    out = R.refresh(project, store, 20)
    assert out["state"] == "done", out
    m = json.loads((store / "index" / "map.json").read_text(encoding="utf-8"))
    assert ("play_music" in json.dumps(m)) == (change == "edit")
    assert ("new.py" in m["files"]) == (change == "add")
    assert ("audio.py" in m["files"]) == (change != "remove")
    assert R.refresh(project, store, 20)["state"] == ("done" if change == "large" else "fresh")   # large: always
    assert not list((store / "index").glob(I.MARK_PREFIX + "*"))


def test_the_check_is_bounded_and_then_leaves_it_to_the_child(tmp_path):
    project, store = _project(tmp_path)
    scan(project, store)
    with Index(store) as idx:
        assert idx.check(project, time.perf_counter() - 1) == {"needed": True, "why": "time"}
    from devmem.store import ListingTimeout, list_files
    with pytest.raises(ListingTimeout):
        list_files(project, deadline=time.perf_counter() - 1)


def test_the_child_runs_the_parents_package_and_nothing_from_the_project(tmp_path, monkeypatch):
    project, store = _project(tmp_path)
    hostile = tmp_path / "hostile_path"
    hostile.mkdir()
    sentinel = tmp_path / "imported.txt"
    code = f"open({str(sentinel)!r}, 'a').write('x')\nraise SystemExit(9)\n"
    for d in (project, hostile):
        (d / "json.py").write_text(code, encoding="utf-8")
        (d / "devmem").mkdir()
        (d / "devmem" / "__init__.py").write_text(code, encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.setenv("PYTHONPATH", str(hostile))
    monkeypatch.setenv("PYTHONSTARTUP", str(hostile / "json.py"))
    out = R.refresh(project, store, 20)
    assert out["state"] == "done", out
    assert not sentinel.exists()
    m = json.loads((store / "index" / "map.json").read_text(encoding="utf-8"))
    assert "damage.py" in m["files"]


def test_the_child_refuses_another_package_copy(tmp_path):
    me = str(os.getpid())
    p = subprocess.run([sys.executable, "-I", "-S", "-c", R.CHILD_CODE, str(PLUGIN), "x", "y", "1", me],
                       input=b"", capture_output=True, timeout=60)
    assert p.returncode == 5, p.stderr          # the right copy: it waits for "go", and its stdin is closed
    other = tmp_path / "copy"
    import shutil
    shutil.copytree(PLUGIN / "devmem", other / "devmem", ignore=shutil.ignore_patterns("__pycache__"))
    code = R.CHILD_CODE.replace("sys.path.insert(0,R)", "sys.path.insert(0,R);sys.path.insert(0,sys.argv.pop())")
    p = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(PLUGIN), "x", "y", "1", me, str(other)],
                       input=b"", capture_output=True, timeout=60)
    assert p.returncode == 3, p.stderr


def test_the_child_writes_no_bytecode_into_the_package(tmp_path, monkeypatch):
    """The child runs with -I, which ignores PYTHONDONTWRITEBYTECODE: without -B it wrote __pycache__ folders into
    the installed package (the plugin's folder, a read-only install raises nothing but a wheel's files change)."""
    import shutil
    copy = tmp_path / "pkg"
    shutil.copytree(PLUGIN / "devmem", copy / "devmem", ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(R, "PACKAGE_PARENT", copy)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    project, store = _project(tmp_path)
    out = R.refresh(project, store, 20)
    assert out["state"] == "done", out
    assert not list(copy.rglob("__pycache__")) and not list(copy.rglob("*.pyc"))


def test_a_running_refresh_makes_others_use_the_index_as_it_is(tmp_path, monkeypatch):
    project, store = _project(tmp_path)
    scan(project, store)
    (project / "audio.py").write_text("def play_music(name):\n    return name\n", encoding="utf-8")
    _no_child(monkeypatch)
    with R._lock(store) as lk:
        assert lk.held
        t = time.perf_counter()
        out = R.refresh(project, store, 20)
        assert out["state"] == "busy" and time.perf_counter() - t < 5


# ---------------------------------------------------------------- a stuck file: killed at the budget

def test_a_stuck_file_is_killed_at_the_budget_and_never_retried(tmp_path, monkeypatch):
    project, store = _project(tmp_path)
    scan(project, store)
    _hostile(project)
    _block(monkeypatch, budget=TEST_BUDGET)
    t = time.perf_counter()
    out = R.refresh(project, store, 8)
    took = time.perf_counter() - t
    assert out["state"] == "stopped", out
    assert took < TEST_BUDGET + 4.0, took
    children = _child_pids(store)
    assert len(children) == 1 and all(_gone(pid) for pid in children)
    assert _integrity(store) == "ok"
    t = time.perf_counter()
    out = R.refresh(project, store, 8)          # the switch is still on: a retry would block again
    assert out["state"] == "done" and out.get("stopped_files") == 1, out
    assert time.perf_counter() - t < TEST_BUDGET + 4.0
    m = json.loads((store / "index" / "map.json").read_text(encoding="utf-8"))
    assert "hostile.py" in m["files"] and not m["files"]["hostile.py"].get("symbols")    # text only
    # nothing is left to do: the parent's own check finds the index and map current. Under load that check can run
    # past its CHECK_S (a check that cannot settle in time counts as "needed", by design: why "time"), and a child then
    # runs and finds nothing to do; so the check is asked with time to spare, and the refresh may be "done" only for
    # that reason and without any work (a retry of hostile.py would be "stopped" at the budget)
    with Index(store, timeout=30) as idx:
        c = idx.check(project.resolve(), time.perf_counter() + 30)
        assert not c["needed"], c
        assert map_current(idx, project.resolve(), store, c)
    out = R.refresh(project, store, 8)
    work = {k: out[k] for k in ("added", "changed", "removed", "pending", "stopped_files", "stopped_map") if out.get(k)}
    assert out["state"] == "fresh" or (out["state"] == "done" and out.get("why") == "time" and not work), out
    assert _integrity(store) == "ok"


def test_killed_at_any_point_the_index_stays_consistent(tmp_path, monkeypatch):
    project, store = _project(tmp_path)
    for i in range(300):
        (project / f"m{i}.py").write_text(f"# {MARKER}\ndef handler_{i}():\n    return {i}\n", encoding="utf-8")
    _block(monkeypatch, secs=0.01, budget=None)
    for budget in (0.45, 0.6, 0.75, 0.9, 1.1, 1.4, 1.8, 2.3):
        monkeypatch.setenv(R.TEST_BUDGET_ENV, str(budget))
        R.refresh(project, store, 8)
        assert _integrity(store) == "ok", budget
    monkeypatch.delenv(I.TEST_BLOCK_ENV)
    m = scan(project, store)
    assert len(m["files"]) == 302
    assert _integrity(store) == "ok"


@pytest.mark.parametrize("nojob", [False, True] if os.name == "nt" else [False])
def test_no_child_outlives_a_killed_parent(tmp_path, monkeypatch, nojob):
    """Claude Code kills a hook that runs past its timeout: the refresh child must go with it (Windows: the job
    object, or with nojob the child's own watch on its parent; POSIX: SIGKILL on parent death)."""
    project, store = _project(tmp_path)
    scan(project, store)
    _hostile(project)
    _block(monkeypatch, budget=None, nojob=nojob)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from devmem.refresh import refresh; "
            "print(refresh(sys.argv[2], sys.argv[3], 40))")
    parent = subprocess.Popen([sys.executable, "-c", code, str(PLUGIN), str(project), str(store)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        child = _wait_for(lambda: _child_pids(store, parent.pid), 30)
        assert child, "the child never started work on the file"
        (pid,) = child
        assert not _gone(pid)
    finally:
        parent.kill()
        parent.wait(timeout=10)
    assert _wait_for(lambda: _gone(pid), 3.0), "the refresh child outlived its parent"
    # the child died because its parent did, not because of the file: the next refresh extracts it fully
    monkeypatch.delenv(I.TEST_BLOCK_ENV)
    # (Windows can release a killed process's lock a moment after the process is seen gone: "busy" until then)
    out = _wait_for(lambda: (lambda o: o if o["state"] != "busy" else None)(R.refresh(project, store, 20)), 10.0)
    assert out and out["state"] == "done" and not out.get("stopped_files"), out
    m = json.loads((store / "index" / "map.json").read_text(encoding="utf-8"))
    assert m["files"]["hostile.py"].get("symbols")


# ---------------------------------------------------------------- the hook and the MCP server

def _hook(project: Path, prompt: str, timeout: float = 120) -> tuple[subprocess.CompletedProcess, float]:
    from hooks_helpers import load_payload
    payload = dict(load_payload("002_UserPromptSubmit", project), prompt=prompt)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    t = time.perf_counter()
    p = subprocess.run([sys.executable, str(PLUGIN / "devmem" / "hooks.py"), "UserPromptSubmit"],
                       input=json.dumps(payload).encode("utf-8"), capture_output=True, env=env, cwd=project,
                       timeout=timeout)
    return p, time.perf_counter() - t


def _hook_project(tmp_path: Path) -> tuple[Path, Path]:
    from hooks_helpers import make_project
    project = make_project(tmp_path)
    (project / ".devmemory" / "config.json").unlink()      # delivery on
    (project / "damage.py").write_text("def apply_armor(damage, armor):\n    return max(0, damage - armor)\n",
                                       encoding="utf-8")
    store = project / ".devmemory"
    scan(project, store)
    return project, store


def _check_hook_bound(tmp_path, monkeypatch, budget: float, real: bool) -> None:
    project, store = _hook_project(tmp_path)
    _hostile(project)
    _block(monkeypatch, budget=None if real else budget)
    p, took = _hook(project, "change how apply_armor handles damage")
    assert p.returncode == 0, p.stderr
    assert took < budget + 4.0, took
    out = p.stdout.decode("utf-8")
    assert "DevMemory context pack" in out and "damage.py" in out          # from the index as it stood
    assert not (store / "hook_errors.log").exists()
    children = _child_pids(store)
    assert len(children) == 1 and all(_gone(pid) for pid in children)
    p, took2 = _hook(project, "change how apply_armor handles damage again")   # next prompt: no retry
    assert p.returncode == 0 and took2 < budget, took2
    m = json.loads((store / "index" / "map.json").read_text(encoding="utf-8"))
    assert "hostile.py" in m["files"]
    assert not list((store / "index").glob(I.MARK_PREFIX + "*"))


def test_hook_returns_within_its_budget_with_a_stuck_file(tmp_path, monkeypatch):
    _check_hook_bound(tmp_path, monkeypatch, TEST_BUDGET, real=False)


@pytest.mark.stress
def test_hook_returns_within_its_real_budget_with_a_stuck_file(tmp_path, monkeypatch):
    from devmem.hooks import HOOK_SCAN_BUDGET
    _check_hook_bound(tmp_path, monkeypatch, HOOK_SCAN_BUDGET, real=True)


def _mcp_call(srv, i: int, name: str, args: dict) -> tuple[dict, float]:
    t = time.perf_counter()
    r = srv.handle({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}})
    return r, time.perf_counter() - t


def _check_mcp_bound(tmp_path, monkeypatch, budget: float, real: bool) -> None:
    from devmem import mcp_server
    project, store = _project(tmp_path)
    scan(project, store)
    _hostile(project)
    _block(monkeypatch, budget=None if real else budget)
    srv = mcp_server.Server(project, store)
    r, took = _mcp_call(srv, 1, "get_context_pack", {"task": "change how apply_armor handles damage"})
    assert r["result"]["isError"] is False and took < budget + 4.0, took
    assert "damage.py" in r["result"]["content"][0]["text"]
    children = _child_pids(store)
    assert len(children) == 1 and all(_gone(pid) for pid in children)
    r, took = _mcp_call(srv, 2, "list_sessions", {})
    assert r["result"]["isError"] is False and took < 2.0
    r, took = _mcp_call(srv, 3, "get_context_pack", {"task": "hostile_thing"})
    assert r["result"]["isError"] is False and took < budget, took
    m = json.loads((store / "index" / "map.json").read_text(encoding="utf-8"))
    assert "hostile.py" in m["files"]


def test_mcp_pack_returns_within_its_budget_with_a_stuck_file(tmp_path, monkeypatch):
    _check_mcp_bound(tmp_path, monkeypatch, TEST_BUDGET, real=False)


@pytest.mark.stress
def test_mcp_pack_returns_within_its_real_budget_with_a_stuck_file(tmp_path, monkeypatch):
    from devmem.mcp_server import MCP_SCAN_BUDGET
    _check_mcp_bound(tmp_path, monkeypatch, MCP_SCAN_BUDGET, real=True)
