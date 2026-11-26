"""In-progress marks: content a process was extracting when it was killed is never tried the same way again.

A real child process is killed while it works on a file (the test-only switch DEVMEM_TESTING=1 +
DEVMEM_TEST_BLOCK_EXTRACT makes extraction of a file that contains a marker sleep), so these tests do not depend on
any extractor being slow."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from devmem import index as I
from devmem.index import Index
from devmem.store import ensure_store, normalized_hash

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"
MARKER = "BLOCK_ME_7f3a"
SYNC = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from devmem.index import Index\n"
        "with Index(Path(sys.argv[2])) as idx:\n    print(json.dumps(idx.sync(Path(sys.argv[3]))))")


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (project / "hostile.py").write_text(f"# {MARKER}\ndef uniquebodyword_zq():\n    pass\n", encoding="utf-8")
    return project, ensure_store(project / ".devmemory")


def _env(block: str | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in (I.TEST_SWITCH_ENV, I.TEST_BLOCK_ENV)}
    if block:
        env[I.TEST_SWITCH_ENV] = "1"
        env[I.TEST_BLOCK_ENV] = block
    return env


def _start_sync(project: Path, store: Path, block: str | None) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import json\n" + SYNC, str(PLUGIN), str(store), str(project)],
                            env=_env(block), stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _marks(store: Path) -> list[Path]:
    return sorted((store / "index").glob(I.MARK_PREFIX + "*"))


def _marked(store: Path, pid: int) -> dict | None:
    for m in _marks(store):
        if m.name.startswith(f"{I.MARK_PREFIX}{pid}."):
            try:
                return json.loads(m.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
    return None


def _kill_while_marked(project: Path, store: Path, block: str, level: str = "full", stamped: bool = True) -> None:
    """Start a sync that blocks on the marker file, wait until its mark names that content at `level`, kill it hard.
    stamped: as devmem.refresh does when it kills a child at its budget (else: as if the process died for another
    reason, such as its parent being killed)."""
    want = normalized_hash((project / "hostile.py").read_bytes())
    p = _start_sync(project, store, block)
    try:
        end = time.monotonic() + 30
        while time.monotonic() < end:
            m = _marked(store, p.pid)
            if m and m.get("h") == want and m.get("level") == level:
                break
            assert p.poll() is None, p.communicate()
            time.sleep(0.05)
        else:
            pytest.fail("the child never marked the file")
    finally:
        if stamped:
            I.stamp_stopped(store / "index", p.pid)
        p.kill()
        p.wait(timeout=10)


def _hash_of(store: Path, rel: str) -> str:
    with Index(store) as idx:
        return idx.files()[rel]["hash"]


def test_a_mark_survives_a_hard_kill_and_that_content_is_then_text_only(tmp_path):
    project, store = _project(tmp_path)
    _kill_while_marked(project, store, f"{MARKER}|30")
    left = _marks(store)
    assert len(left) == 1 and json.loads(left[0].read_text(encoding="utf-8"))["level"] == "full"
    # the next sync runs with the switch still on: if it tried a full extraction again it would block for 30 s
    p = _start_sync(project, store, f"{MARKER}|30")
    out, err = p.communicate(timeout=25)
    assert p.returncode == 0, err
    counts = json.loads(out)
    assert counts["stopped_files"] == 1
    assert _marks(store) == []
    with Index(store) as idx:
        files = idx.files()
        h = files["hostile.py"]["hash"]
        assert h in json.loads(idx.meta_get("slow_extract"))
        assert not files["hostile.py"].get("symbols")          # text only: not extracted
        assert [r["key"] for r in idx.search("file", "uniquebodyword_zq")] == ["hostile.py"]   # its text is found
        assert files["a.py"]["symbols"]                         # the other file is extracted as usual


def test_content_that_stops_even_the_text_pass_is_indexed_by_its_path_only(tmp_path):
    project, store = _project(tmp_path)
    spec = f"{MARKER}|30|plain"
    _kill_while_marked(project, store, spec)        # stopped while extracting: text only from now on
    _kill_while_marked(project, store, spec, "plain")   # stopped again while indexing it as text: path only
    p = _start_sync(project, store, spec)
    _out, err = p.communicate(timeout=25)
    assert p.returncode == 0, err
    with Index(store) as idx:
        info = idx.files()["hostile.py"]
        assert info.get("content_skipped") is True
        assert info["hash"] in json.loads(idx.meta_get("skip_extract"))
        assert idx.search("file", "uniquebodyword_zq") == []
        assert [r["key"] for r in idx.search("file", "hostile")] == ["hostile.py"]
    assert _marks(store) == []


def test_marks_of_live_processes_and_threads_are_left_alone(tmp_path):
    project, store = _project(tmp_path)
    folder = store / "index"
    folder.mkdir(exist_ok=True)
    mark = json.dumps({"h": "feedfacefeedface", "level": "full", "t": time.time()})
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    stop = threading.Event()
    other = threading.Thread(target=stop.wait)
    other.start()
    try:
        other_proc = folder / f"{I.MARK_PREFIX}{live.pid}.1"
        other_thread = folder / f"{I.MARK_PREFIX}{os.getpid()}.{other.ident}"
        for m in (other_proc, other_thread):
            m.write_text(mark, encoding="utf-8")
        I.stamp_stopped(folder, live.pid)       # stopped at its budget (once it is gone)
        with Index(store) as idx:
            out = idx.sync(project)
            assert "stopped_files" not in out
            assert "feedfacefeedface" not in (idx.meta_get("slow_extract") or "")
        assert other_proc.exists() and other_thread.exists()
    finally:
        stop.set()
        other.join()
        live.kill()
        live.wait(timeout=10)
    with Index(store) as idx:   # now both are gone: the stamped one's content is demoted, the other's is a suspect
        assert idx.sync(project)["stopped_files"] == 1
        assert "feedfacefeedface" in json.loads(idx.meta_get("slow_extract"))
        assert json.loads(idx.meta_get("suspect_extract")) == []     # demoted: no longer a suspect
    assert _marks(store) == [] and not list(folder.glob(I.STAMP_PREFIX + "*"))


def test_a_process_that_died_for_another_reason_does_not_demote_the_content(tmp_path):
    """A refresh child dies with its parent (the user cancels, Claude Code kills the hook): the file it was on is not
    to blame, and is extracted fully by the next sync."""
    project, store = _project(tmp_path)
    _kill_while_marked(project, store, f"{MARKER}|30", stamped=False)
    with Index(store) as idx:
        out = idx.sync(project)                 # no switch: a full extraction now finishes
        assert "stopped_files" not in out
        info = idx.files()["hostile.py"]
        assert info.get("symbols") and info["hash"] not in (idx.meta_get("slow_extract") or "")
        assert json.loads(idx.meta_get("suspect_extract")) == [f"full:{info['hash']}"]
    assert _marks(store) == []


def test_the_same_content_found_marked_twice_is_demoted(tmp_path):
    project, store = _project(tmp_path)
    _kill_while_marked(project, store, f"{MARKER}|30", stamped=False)     # a suspect
    _kill_while_marked(project, store, f"{MARKER}|30", stamped=False)     # again: demoted when this mark is reaped
    p = _start_sync(project, store, f"{MARKER}|30")     # switch still on: a third full try would block
    out, err = p.communicate(timeout=25)
    assert p.returncode == 0, err
    assert json.loads(out)["stopped_files"] == 1
    with Index(store) as idx:
        h = idx.files()["hostile.py"]["hash"]
        assert h in json.loads(idx.meta_get("slow_extract"))
        assert f"full:{h}" not in json.loads(idx.meta_get("suspect_extract"))


def test_a_stamp_without_a_mark_is_removed(tmp_path):
    project, store = _project(tmp_path)
    (store / "index").mkdir(exist_ok=True)
    I.stamp_stopped(store / "index", 99999999)
    (store / "index" / f"{I.STAMP_PREFIX}junk").write_text("", encoding="utf-8")
    with Index(store) as idx:
        idx.sync(project)
    assert not list((store / "index").glob(I.STAMP_PREFIX + "*"))


@pytest.mark.parametrize("name,content", [
    ("extracting.99999999.1", b"not json"),
    ("extracting.99999999.2", b'{"h": 5}'),
    ("extracting.99999999.3", b"[1, 2]"),
    ("extracting.99999999.4", b'{"h": "x", "level": "full", "t": "soon"}'),
    ("extracting.99999999.5", b"\xff\xfe\x00garbage"),
    ("extracting.99999999.6", b"[" * 100_000),
    ("extracting.notapid", b'{"h": "aa", "level": "full", "t": 0}'),
    ("extracting.1.2.3", b'{"h": "aa", "level": "full", "t": 0}'),
    ("extracting.99999999.7", b""),
])
def test_a_garbled_mark_is_removed_and_the_sync_goes_on(tmp_path, name, content):
    project, store = _project(tmp_path)
    folder = store / "index"
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(content)
    with Index(store) as idx:
        out = idx.sync(project)
        assert "error" not in out and out["added"] == 2
        assert "stopped_files" not in out
        assert idx.meta_get("slow_extract") in (None, "[]")
    assert _marks(store) == []


def test_a_finished_sync_leaves_no_mark(tmp_path):
    project, store = _project(tmp_path)
    with Index(store) as idx:
        idx.sync(project)
    assert _marks(store) == []


@pytest.mark.parametrize("env", [{I.TEST_BLOCK_ENV: f"{MARKER}|5"},
                                 {I.TEST_SWITCH_ENV: "1"},
                                 {I.TEST_SWITCH_ENV: "yes", I.TEST_BLOCK_ENV: f"{MARKER}|5"}])
def test_the_test_switch_is_inert_unless_both_variables_are_set(monkeypatch, env):
    monkeypatch.delenv(I.TEST_SWITCH_ENV, raising=False)
    monkeypatch.delenv(I.TEST_BLOCK_ENV, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    slept = []
    monkeypatch.setattr(I.time, "sleep", slept.append)
    I._test_block(f"x {MARKER} y", "full")
    assert slept == []


def test_the_test_switch_sleep_is_capped(monkeypatch):
    monkeypatch.setenv(I.TEST_SWITCH_ENV, "1")
    slept = []
    monkeypatch.setattr(I.time, "sleep", slept.append)
    for spec in (f"{MARKER}|1e9", f"{MARKER}|inf", f"{MARKER}|nan", f"{MARKER}|-3", f"{MARKER}|x", f"{MARKER}"):
        monkeypatch.setenv(I.TEST_BLOCK_ENV, spec)
        I._test_block(MARKER, "full")
    assert slept == [I.TEST_BLOCK_MAX_S, I.TEST_BLOCK_MAX_S]
    monkeypatch.setenv(I.TEST_BLOCK_ENV, f"{MARKER}|2")
    I._test_block(MARKER, "plain")          # the text-only pass blocks only with |plain
    I._test_block("no marker here", "full")
    assert slept == [I.TEST_BLOCK_MAX_S, I.TEST_BLOCK_MAX_S]


@pytest.mark.parametrize("stamped", [99999991, 99999992])
def test_one_stamped_and_one_unstamped_mark_of_the_same_content_demote_it_in_any_order(tmp_path, stamped):
    project, store = _project(tmp_path)
    folder = store / "index"
    folder.mkdir(exist_ok=True)
    mark = json.dumps({"h": "feedfacefeedface", "level": "full", "t": time.time()})
    for pid in (99999991, 99999992):
        (folder / f"{I.MARK_PREFIX}{pid}.1").write_text(mark, encoding="utf-8")
    I.stamp_stopped(folder, stamped)
    with Index(store) as idx:
        assert idx.sync(project)["stopped_files"] == 1
        assert "feedfacefeedface" in json.loads(idx.meta_get("slow_extract"))
        assert json.loads(idx.meta_get("suspect_extract")) == []
