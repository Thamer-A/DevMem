"""The refresh a hook or an MCP call triggers (scan: list, extract, index, build_map), under a hard wall-clock bound.

A regex, `ast.parse` or the tokeniser cannot be interrupted from inside the process that runs it (they hold the GIL
for as long as they run), so the work runs in a child process that is killed at the budget:

1. The parent (the hook, the MCP server) first asks cheaply whether anything changed: `Index.check` lists the project,
   confirms a changed stat by the content hash and never extracts or tokenises; it stops at CHECK_S seconds, and is
   skipped for an index of more than CHECK_MAX_FILES files. Nothing changed and the map current: done, no child.
2. Otherwise, unless another refresh of this store is running (a non-blocking OS lock on `index/refresh.lock`, the
   same lock class as the hooks' session lock, devmem.store.OSLock, held by the child for its whole run), it starts
   `python -I -S -B -c CHILD_CODE <package parent> ...` through devmem.proc: the parent's own interpreter and the same
   devmem package copy, and nothing from the current folder, the project, site-packages or PYTHON* variables (a
   project can ship a `json.py`; -B: no bytecode is written into the package, as -I also ignores
   PYTHONDONTWRITEBYTECODE). The child runs devmem.scan.scan with a scan deadline CHILD_MARGIN_S inside the budget.
3. The parent waits until the budget is spent, then kills the child. What the child committed stays (the index commits
   at least every second of work, and SQLite keeps it consistent through a kill); the content it was stuck on carries
   an in-progress mark (devmem.index._Mark), and the parent stamps the child as stopped at its budget before it kills
   it (devmem.index.stamp_stopped), so the next refresh indexes that content as text only, and content that stops even
   that is indexed by its path only: one hostile file costs at most one budget, once per level. A child that died
   without the stamp (its parent was killed) makes the content a suspect only, demoted the second time. The caller then
   builds the pack from the index as it stands.

No child outlives its parent. Windows: the child is put in a job object that kills it when the job's only handle,
held by the parent, closes, however the parent ends; if that fails (AssignProcessToJobObject can be refused), the
child has the system end it when the parent ends (RegisterWaitForSingleObject on the parent's process handle with
TerminateProcess as the callback, run on a pool thread, so no running regex can hold it up). The child opens the
parent's handle before it reads its "go" line, and the parent writes that line only after the job is set up, so a
reused process id is never watched and a parent killed in between leaves a child that reads end-of-file and exits.
POSIX: SIGALRM with its default action at the budget (ends the process even inside a regex), and on Linux
PR_SET_PDEATHSIG (SIGKILL when the parent dies); elsewhere a thread polls the parent id.
git: the file list in the parent's check and in the child's scan gives git half of the time left (devmem.store.list_files;
one limit covers the whole git_run, its filter listing included), so the child ends a git that hangs before its own
budget ends, and a git that ran into its limit leaves a marker in devmem's per-user folder that keeps every devmem
process from starting git in that project for a minute (devmem.store._git_slow; the folders are walked meanwhile, and
an honest repository that was slow once is asked again after that minute). Linux: a git started from a process with
one thread (the hooks, the MCP server's own calls, this child) has PR_SET_PDEATHSIG (SIGKILL), so it ends with the
process that started it however that ends: the child's alarm, the parent's kill at the budget, the child's death with
its parent. Windows: the child's git is in the child's job object and ends with it. Not guaranteed: on macOS and
other POSIX systems, on Windows when the job could not be set up (the parent watch ends the child only), and from a
process that runs several threads (the viewer), a git that hangs at the moment its devmem process is killed from
outside can stay behind; a program git itself starts does not inherit the death signal (the commands devmem runs
start none).

The CLI (`devmem scan`, `devmem pack`) still scans in its own process, without this bound.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parent.parent
CHECK_S = 1.0               # seconds the parent may spend deciding whether a refresh is needed
CHECK_MAX_FILES = 20_000    # an index with more files goes straight to the child (the check would not be cheap)
CHILD_MARGIN_S = 1.5        # the child's own scan deadline is this much inside its budget (map and writes after it)
MIN_CHILD_S = 0.3           # less time left than this: no child is started
LOCK_NAME = "refresh.lock"
# test-only, honoured only with DEVMEM_TESTING=1: a smaller budget, and no job object (the child's own watch only)
TEST_BUDGET_ENV = "DEVMEM_TEST_REFRESH_BUDGET"
TEST_NOJOB_ENV = "DEVMEM_TEST_REFRESH_NOJOB"

# argv: <package parent> <project> <store> <seconds> <parent pid>. No double quote, $, backtick or backslash.
CHILD_CODE = ("import sys;R=sys.argv[1];sys.path.insert(0,R);"
              "from devmem.refresh import child_main;sys.exit(child_main(R,sys.argv[2:]))")


def _testing(name: str) -> str | None:
    return os.environ.get(name) if os.environ.get("DEVMEM_TESTING") == "1" else None


def _budget(budget: float) -> float:
    t = _testing(TEST_BUDGET_ENV)
    try:
        return min(budget, float(t)) if t else budget
    except ValueError:
        return budget


def refresh(project: Path, store: Path, budget: float) -> dict:
    """Bring the index and map of `project` up to date in at most about `budget` seconds (see the module docstring).
    {"state": ..., "seconds": ...}; state is "fresh" (nothing to do), "done", "incomplete" (the child ran out of its
    scan time; the rest waits for the next refresh), "stopped" (killed at the budget), "busy" (another refresh of this
    store is running), "skipped" (no time left) or "failed" (the child could not start or failed). Never raises for a
    refresh that went wrong: the caller builds its pack from the index as it is."""
    t0 = time.perf_counter()
    end = t0 + _budget(budget)
    project, store = Path(project).resolve(), Path(store)
    out: dict = {}
    try:
        why = _why(project, store, min(end, t0 + CHECK_S))
        if not why:
            out = {"state": "fresh"}
        elif _busy(store):
            out = {"state": "busy", "why": why}
        else:
            out = _run_child(project, store, end)
            out["why"] = why
    except Exception as e:  # noqa: BLE001 - a refresh must never break the prompt or the MCP call
        out = {"state": "failed", "error": type(e).__name__}
    out["seconds"] = round(time.perf_counter() - t0, 3)
    return out


def _why(project: Path, store: Path, deadline: float) -> str:
    """Why a refresh is needed, or "" when the index and map are current. Bounded by `deadline` (perf_counter)."""
    from .index import MARK_PREFIX, Index
    from .scan import map_current
    if not (store / "index" / "map.json").is_file():
        return "no map"
    with Index(store, timeout=CHECK_S) as idx:      # a write lock held longer than that: "index", the child decides
        if idx.degraded:
            return "degraded"
        n = idx.file_count()
        if n is None:
            return "index"
        if n > CHECK_MAX_FILES:
            return "large"
        try:
            with os.scandir(idx.path.parent) as it:
                if any(e.name.startswith(MARK_PREFIX) for e in it):
                    return "marks"          # a refresh was stopped: the child reaps its marks
        except OSError:
            return "index"
        c = idx.check(project, deadline)
        if c["needed"]:
            return c["why"] or "changed"
        if not map_current(idx, project, store, c):
            return "map"
    return ""


def _lock(store: Path, timeout: float = 0.0):
    from .store import OSLock
    return OSLock(store / "index" / LOCK_NAME, timeout=timeout)


def _busy(store: Path) -> bool:
    """True when another process holds this store's refresh lock (taken and released at once when it is free)."""
    with _lock(store) as lk:
        return not lk.held


def _run_child(project: Path, store: Path, end: float) -> dict:
    import subprocess
    from . import proc
    left = end - time.perf_counter()
    if left < MIN_CHILD_S or not sys.executable:
        return {"state": "skipped"}
    argv = [sys.executable, "-I", "-S", "-B", "-c", CHILD_CODE, str(PACKAGE_PARENT), str(project), str(store),
            f"{left:.3f}", str(os.getpid())]
    kw: dict = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = proc.popen(argv, **kw)
    except (OSError, ValueError) as e:
        return {"state": "failed", "error": type(e).__name__}
    job = None
    try:
        if os.name == "nt" and not _testing(TEST_NOJOB_ENV):
            job = _job_for(p.pid)
        try:     # the "go" line goes only now: after the job is in place
            out, err = p.communicate(b"go 1\n" if job else b"go 0\n", timeout=max(0.0, end - time.perf_counter()))
        except subprocess.TimeoutExpired:
            from .index import stamp_stopped
            stamp_stopped(store / "index", p.pid)      # killed for its budget: what it was stuck on is demoted
            p.kill()
            try:
                p.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
            _remove_leftovers(store, p.pid)
            return {"state": "stopped"}
        res = _result(out)
        if p.returncode != 0 or res is None:
            tail = err.decode("utf-8", errors="replace").strip().splitlines()[-1:] if err else []
            return {"state": "failed", "code": p.returncode, "error": tail[0][:300] if tail else ""}
        return res
    finally:
        if p.poll() is None:
            p.kill()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if job:
            _close_handle(job)


def _result(out: bytes) -> dict | None:
    for line in reversed(out.decode("utf-8", errors="replace").splitlines()):
        try:
            res = json.loads(line)
        except ValueError:
            continue
        if isinstance(res, dict) and isinstance(res.get("state"), str):
            return {k: v for k, v in res.items() if k in ("state", "pending", "added", "changed", "removed",
                                                          "stopped_files", "stopped_map")}
    return None


def _remove_leftovers(store: Path, pid: int) -> None:
    """The temporary files a killed child may have left (devmem.scan._write_if_changed names them by its pid)."""
    for p in (store / "index" / f".map.json.{pid}.tmp", store / f".PROJECT.md.{pid}.tmp"):
        try:
            if os.path.lexists(p):
                os.unlink(p)
        except OSError:
            pass


# ---------------------------------------------------------------- Windows: job object, parent watch

def _kernel32():
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.GetCurrentProcess.argtypes = []
    k.RegisterWaitForSingleObject.restype = wintypes.BOOL
    k.RegisterWaitForSingleObject.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.HANDLE, ctypes.c_void_p,
                                              ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG]
    return k


def _close_handle(h) -> None:
    try:
        _kernel32().CloseHandle(h)
    except OSError:
        pass


def _job_for(pid: int):
    """A job object (kill on close) holding process `pid`; its handle, or None when that cannot be set up."""
    import ctypes
    from ctypes import wintypes
    try:
        k = _kernel32()
    except (OSError, AttributeError):
        return None

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                                                   "OtherOperationCount", "ReadTransferCount",
                                                   "WriteTransferCount", "OtherTransferCount")]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    job = k.CreateJobObjectW(None, None)
    if not job:
        return None
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    proc_h = k.OpenProcess(0x0100 | 0x0001, False, pid)      # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    ok = bool(proc_h) and bool(k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))) and \
        bool(k.AssignProcessToJobObject(job, proc_h))       # 9: JobObjectExtendedLimitInformation
    if proc_h:
        k.CloseHandle(proc_h)
    if not ok:
        k.CloseHandle(job)
        return None
    return job


_WATCH: list = []       # handles the parent watch needs for the child's whole life


def _open_parent_nt(ppid: int):
    k = _kernel32()
    return k.OpenProcess(0x00100000, False, ppid)          # SYNCHRONIZE


def _watch_parent_nt(parent_handle) -> bool:
    """When the parent process ends, a system pool thread calls TerminateProcess(this process): no Python code (and
    so no GIL) is involved. False if it could not be set up."""
    import ctypes
    from ctypes import wintypes
    k = _kernel32()
    wait = wintypes.HANDLE()
    term = ctypes.cast(k.TerminateProcess, ctypes.c_void_p)
    # the callback is called as (context, timed_out): TerminateProcess(GetCurrentProcess(), exit code)
    ok = k.RegisterWaitForSingleObject(ctypes.byref(wait), parent_handle, term, k.GetCurrentProcess(),
                                       0xFFFFFFFF, 0x00000008)    # INFINITE, WT_EXECUTEONLYONCE
    if ok:
        _WATCH.extend([parent_handle, wait, term])
    return bool(ok)


# ---------------------------------------------------------------- POSIX: alarm, parent death

def _arm_posix(seconds: float, ppid: int) -> None:
    if sys.platform == "win32":     # never called there (the job object and the parent watch instead)
        return
    import math
    import signal
    signal.signal(signal.SIGALRM, signal.SIG_DFL)            # the default action ends the process, even mid-regex
    signal.alarm(max(1, int(math.ceil(seconds)) + 1))
    if sys.platform.startswith("linux"):
        try:
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            libc.prctl(1, int(signal.SIGKILL), 0, 0, 0)       # PR_SET_PDEATHSIG
        except (OSError, AttributeError):
            pass
    else:
        import threading

        def poll() -> None:
            while os.getppid() == ppid:
                time.sleep(0.2)
            os._exit(1)
        threading.Thread(target=poll, daemon=True).start()
    if os.getppid() != ppid:      # the parent died before the watch was set up
        os._exit(1)


# ---------------------------------------------------------------- the child

def child_main(root: str, argv: list[str]) -> int:
    """The child's entry point (CHILD_CODE). argv: project, store, seconds, parent pid. Prints one JSON line."""
    import devmem
    if Path(devmem.__file__).resolve().parent.parent != Path(root).resolve():
        return 3                    # not the package the parent named: refuse to run another copy
    project, store, seconds, ppid = Path(argv[0]), Path(argv[1]), float(argv[2]), int(argv[3])
    parent_h = None
    if os.name == "nt":
        parent_h = _open_parent_nt(ppid)      # before the "go" line: the id cannot have been reused yet
        if not parent_h:
            return 4
    else:
        _arm_posix(seconds, ppid)
    go = sys.stdin.readline().split()
    if not go or go[0] != "go":
        return 5                    # the parent ended before it was set up
    if os.name == "nt" and go[1:] != ["1"] and not _watch_parent_nt(parent_h):
        return 6                    # neither a job nor a watch: do not run unbounded
    with _lock(store) as lk:
        if not lk.held:
            print(json.dumps({"state": "busy"}))
            return 0
        from .scan import scan
        stats: dict = {}
        scan(project, store, stats=stats, time_budget=max(0.2, seconds - CHILD_MARGIN_S))
    state = "incomplete" if stats.get("incomplete") else "done"
    print(json.dumps({"state": state, **{k: stats[k] for k in ("pending", "added", "changed", "removed",
                                                               "stopped_files", "stopped_map") if stats.get(k)}}))
    return 0
