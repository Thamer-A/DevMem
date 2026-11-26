"""Local web UI for DevMemory (stdlib only, binds 127.0.0.1). Never modifies project files; scanning writes the store only."""
from __future__ import annotations

import errno
import hmac
import json
import os
import re
import secrets
import socket
import stat
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .pack import build_pack, decision_sentences, freshness
from .scan import convention_lines, scan
from .sessions import load_sessions
from . import proc
from . import term
from .store import _is_link, find_program, private_mkdir, read_store_text, store_dir_links, store_path, write_private

STATIC = Path(__file__).parent / "ui"
REFUSAL_SECONDS = 5.0     # how long the viewer reuses its answer to "is the open store this user's?"
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}


def _sid(s: dict) -> str:
    return str(s.get("session_id") or s.get("_meta", ""))


REPO = Path(__file__).resolve().parent.parent.parent   # <repo>/plugin/devmem/ui_server.py
# Example vaults only exist in a source checkout; entries whose folders are missing are not shown.
EXAMPLES = [
    {"name": "Blade Swarm (16 sessions)", "desc": "Roblox + Rojo game built over 16 captured sessions",
     "project": REPO / "fixtures/blade-swarm/project",
     "store": REPO / "fixtures/blade-swarm/memory"},
]
VAULT_CAP = 3 * 1024 * 1024
TOKEN_HEADER = "X-DevMemory-Token"
# The launch URL carries a single-use code, never the token: a browser's command line can be read by other local users
# (/proc/<pid>/cmdline, ps) and a URL lands in the browser's history. The page exchanges the code once for the token.
LAUNCH_PATH = "/api/launch"
LAUNCH_HEADER = "X-DevMemory-Launch"
LAUNCH_SECONDS = 60.0
_SPENT_KEPT = 64
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def recents_file(platform: str | None = None, env=None, home: Path | None = None) -> Path:
    """Where the list of recently opened projects lives: devmem's per-user folder (devmem.consent.user_data_dir, which
    also holds the `devmem init` switches), per platform (all arguments injectable for tests)."""
    from .consent import user_data_dir
    return user_data_dir(platform, env, home) / "recents.json"


def reveal_command(path: Path, platform: str | None = None, which=find_program) -> list[str] | None:
    """Command that shows `path` in the platform file manager, or None when there is no way to do it. Programs are
    started by absolute path (a bare `explorer` would be looked up in the current folder first on Windows)."""
    platform = platform or sys.platform
    path = Path(path)
    if platform.startswith("win"):
        windir = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
        return [os.path.join(windir, "explorer.exe"), "/select," + str(path).replace("/", "\\")]
    if platform == "darwin":
        return ["open", "-R", str(path)]
    opener = which("xdg-open")
    if not opener:
        return None
    # xdg-open cannot select a file, so open the folder that holds it.
    return [opener, str(path if path.is_dir() else path.parent)]


def load_recents() -> list[dict]:
    try:
        from .store import read_regular
        data = read_regular(recents_file(), 4 * 1024 * 1024)   # a plain file only, never a link or FIFO
        d = json.loads(data.decode("utf-8")) if data is not None else []
        return [r for r in d if isinstance(r, dict) and r.get("project")] if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def add_recent(project: Path, store: Path, name: str) -> None:
    rs = [r for r in load_recents() if r.get("project") != str(project) or r.get("store") != str(store)]
    rs.insert(0, {"project": str(project), "store": str(store), "name": name,
                  "last_opened": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    try:
        f = recents_file()
        private_mkdir(f.parent)                     # devmem's per-user folder: 0700 on POSIX, the file 0600
        tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
        write_private(tmp, json.dumps(rs[:12], indent=1).encode("utf-8"))
        os.replace(tmp, f)
    except (OSError, ValueError):   # ValueError: an unusable DEVMEM_USER_DATA (devmem.consent.user_data_dir)
        pass


class PickUnavailable(RuntimeError):
    """No folder dialog can be shown here (no tkinter, no display)."""


def _in_thread(fn, title: str) -> str:
    """Run a dialog function in a fresh thread (its own Tk root or COM apartment); "" when it is still open after
    5 minutes. A PickUnavailable it raises is raised here."""
    res: list = []

    def run():
        try:
            res.append(fn(title))
        except BaseException as e:  # noqa: BLE001 - handed to the caller
            res.append(e)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(300)
    if res and isinstance(res[0], BaseException):
        e = res[0]
        raise e if isinstance(e, PickUnavailable) else PickUnavailable(f"{type(e).__name__}: {e}")
    return res[0] if res and isinstance(res[0], str) else ""


def _pick_tk(title: str) -> str:
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        raise PickUnavailable("tkinter is not installed") from None
    try:
        root = tkinter.Tk()
    except Exception as e:  # no display
        raise PickUnavailable(f"tkinter cannot open a window ({e})") from None
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        return filedialog.askdirectory(parent=root, title=title, mustexist=True) or ""
    finally:
        root.destroy()


def _pick_windows(title: str) -> str:
    """The Windows shell's folder dialog through ctypes (SHBrowseForFolderW), for a Python without tkinter such as the
    embeddable one the Windows installer ships. Made topmost when it opens, as the tkinter dialog is."""
    try:
        import ctypes
        from ctypes import wintypes
        shell32, ole32, user32 = ctypes.windll.shell32, ctypes.windll.ole32, ctypes.windll.user32
    except (ImportError, AttributeError, OSError) as e:
        raise PickUnavailable(f"no Windows folder dialog ({e})") from None

    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.UINT, wintypes.LPARAM, wintypes.LPARAM)

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [("hwndOwner", wintypes.HWND), ("pidlRoot", ctypes.c_void_p), ("pszDisplayName", wintypes.LPWSTR),
                    ("lpszTitle", wintypes.LPCWSTR), ("ulFlags", wintypes.UINT), ("lpfn", callback_type),
                    ("lParam", wintypes.LPARAM), ("iImage", ctypes.c_int)]

    def on_event(hwnd, msg, lparam, data):
        if msg == 1:   # BFFM_INITIALIZED: HWND_TOPMOST, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
            user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
            user32.SetForegroundWindow(hwnd)
        return 0
    callback = callback_type(on_event)       # kept referenced until the dialog has closed

    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    display = ctypes.create_unicode_buffer(260)
    info = BROWSEINFOW(None, None, ctypes.cast(display, wintypes.LPWSTR), title,
                       0x0001 | 0x0040, callback, 0, 0)     # BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
    ole32.CoInitializeEx(None, 0x2)          # COINIT_APARTMENTTHREADED, which BIF_NEWDIALOGSTYLE needs
    try:
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(info))
        if not pidl:
            return ""                         # cancelled
        try:
            path = ctypes.create_unicode_buffer(32768)
            return path.value if shell32.SHGetPathFromIDListW(pidl, path) else ""
        finally:
            ole32.CoTaskMemFree(pidl)
    finally:
        ole32.CoUninitialize()


def folder_pickers(platform: str | None = None) -> list:
    """The dialogs to try, in order: tkinter's, then (Windows) the shell's own."""
    out = [_pick_tk]
    if (platform or sys.platform).startswith("win"):
        out.append(_pick_windows)
    return out


def pick_dialog(kind: str, pickers: list | None = None) -> str:
    """Native folder dialog: the chosen folder, or "" when cancelled. Raises PickUnavailable when none can be shown."""
    title = "Select project folder" if kind == "project" else "Select memory store folder"
    reasons = []
    for picker in folder_pickers() if pickers is None else pickers:
        try:
            chosen = _in_thread(picker, title)
        except PickUnavailable as e:
            reasons.append(str(e))
            continue
        return str(Path(chosen)) if chosen else ""
    raise PickUnavailable("; ".join(reasons) or "no folder dialog on this system")


class State:
    def __init__(self, project: Path | None, store: Path | None):
        self.lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)   # per-launch secret required on every /api/ call
        self.allowed_hosts: set[str] = set()      # filled in by make_server once the port is known
        self.last_seen = time.monotonic()         # last request that passed check_request (devmem.app.wait_until_idle)
        self._codes_lock = threading.Lock()
        self.launch_codes: dict[str, float] = {}  # single-use launch code -> monotonic deadline
        self.spent_codes: list[str] = []          # codes already used or expired (the last _SPENT_KEPT)
        self.refused_launches = 0
        self.set(project, store)

    def new_launch_code(self) -> str:
        """A fresh code for one launch URL, valid once within LAUNCH_SECONDS."""
        code = secrets.token_urlsafe(24)
        with self._codes_lock:
            self.launch_codes[code] = time.monotonic() + LAUNCH_SECONDS
        return code

    def _spend(self, code: str) -> None:
        self.launch_codes.pop(code, None)
        self.spent_codes = (self.spent_codes + [code])[-_SPENT_KEPT:]

    def claim_launch(self, given: str) -> tuple[str | None, str]:
        """(token, "") for a launch code that is valid now, which is spent by this call; (None, why) otherwise, and a
        line on stderr: a code used twice means the link was opened twice or read by someone else."""
        g = given.encode("utf-8", "replace")
        same = lambda c: hmac.compare_digest(c.encode("utf-8"), g)   # noqa: E731
        with self._codes_lock:
            now = time.monotonic()
            hit = next(((c, until) for c, until in self.launch_codes.items() if same(c)), None)
            if hit is not None:
                self._spend(hit[0])
                if hit[1] > now:
                    return self.token, ""
                why = (f"this launch link has expired (links work for {LAUNCH_SECONDS:.0f} seconds); open a new one from "
                       "devmem app or devmem ui")
            elif any(same(c) for c in self.spent_codes):
                why = "this launch link was already used (each link works once); open a new one from devmem app or devmem ui"
                _warn("devmem viewer: a launch link was used a second time and refused. If you did not open it twice, "
                      "another program on this computer may have read it.")
            else:
                why = "unknown launch link; open a new one from devmem app or devmem ui"
            self.refused_launches += 1
        return None, why

    def set(self, project, store):
        self.project = Path(project).resolve() if project else None
        self.store = Path(store).resolve() if store else None
        # the store as given, links not resolved: resolve() would follow `.devmemory -> elsewhere` and hide the link
        self.store_given = Path(os.path.abspath(store)) if store else None
        self.map = None
        self._refusal: tuple[float, str | None] | None = None

    def refusal(self) -> str | None:
        """Why the open store's recordings are not shown (devmem.consent.store_refusal: a store this user did not
        create or adopt), or None. Asked again at most every REFUSAL_SECONDS (it runs git once)."""
        if self.project is None or self.store is None:
            return None
        cached = self._refusal
        if cached is not None and time.monotonic() - cached[0] < REFUSAL_SECONDS:
            return cached[1]
        from .consent import store_refusal
        why = store_refusal(self.project, self.store_given or self.store)
        self._refusal = (time.monotonic(), why)
        return why

    def name(self) -> str:
        return self.project.name if self.project else ""

    def paths(self) -> tuple[Path, Path]:
        """(project, store) of the open project; only called where the routes have established one is open."""
        if self.project is None or self.store is None:
            raise RuntimeError("no project open")
        return self.project, self.store

    def in_roots(self, p: Path) -> bool:
        p = p.resolve()
        for root in (self.project, self.store):
            if root and (p == root or root in p.parents):
                return True
        return False

    def read_only(self) -> str | None:
        """Why the open project is shown read-only (its store, or a folder devmem writes in, is a symbolic link, so
        nothing may be scanned or written there), or None."""
        if self.store_given is None:
            return None
        links = store_dir_links(self.store_given)
        if not links:
            why = self.refusal()
            return f"Read-only, recordings not shown: {why}." if why else None
        return (f"Read-only: the memory store is or holds a symbolic link ({links[0]}). devmem never writes through a "
                "link, so this project is not scanned; remove the link to use it normally.")

    def get_map(self, rescan: bool = False) -> dict:
        with self.lock:
            project, store = self.paths()
            p = store / "index" / "map.json"
            if self.read_only():   # show what is there, write nothing
                if self.map is None or rescan:
                    try:
                        self.map = _clean_map(json.loads(read_store_text(p)), project.name)
                    except (OSError, ValueError, RecursionError):
                        self.map = _clean_map({}, project.name)
                return self.map
            if rescan or self.map is None:
                data = None
                if not rescan and os.path.lexists(p):
                    try:
                        data = json.loads(read_store_text(p))
                    except (OSError, ValueError, RecursionError):
                        pass     # garbled, or not a plain file (a FIFO, a link, a folder): rebuilt like a missing one
                if data is None:
                    try:
                        data = scan(project, store)
                    except OSError as e:     # the map cannot be written (a folder in its place, a full disk)
                        _warn(f"devmem ui: the project map could not be rebuilt ({type(e).__name__}); showing none")
                        data = {}
                self.map = _clean_map(data, project.name)
            return self.map

    def sessions(self) -> list[dict]:
        project, store = self.paths()
        if self.refusal():      # not this user's store: no recorded session is shown, quoted or linked
            return []
        ss = load_sessions(store, project=project)
        ss.sort(key=lambda s: str(s.get("started_at") or ""))
        return ss

    def session_view(self, s: dict) -> dict:
        fr = freshness(s, self.paths()[0])
        return {"id": _sid(s), "meta_file": s.get("_meta", ""), "raw_record": s.get("raw_record", ""),
                "started_at": s.get("started_at") or "", "prompt": s.get("prompt") or "",
                "prompt_source": _prompt_source(s),
                "usage": _usage(s), "files_edited": s.get("files_edited", []),
                "files_read": s.get("files_read", []), "freshness": fr["status"],
                "in_progress": bool(s.get("in_progress")), "turn_count": len(s.get("turns") or [])}

    def decisions(self) -> list[dict]:
        out, seen = [], set()
        for s in self.sessions():
            prompts = [(t.get("prompt") or "", t.get("started_at") or s.get("started_at", ""), i + 1,
                        _prompt_source(t)) for i, t in enumerate(s.get("turns") or [])] or [
                (s.get("prompt") or "", s.get("started_at") or "", 1, _prompt_source(s))]
            for prompt, at, turn, src in prompts:   # every turn, not only the session's first prompt
                if src != "user":   # a client-reported or uncaptured prompt is kept out of "stated by the user"
                    continue
                for sent in decision_sentences(prompt):
                    if sent in seen:
                        continue
                    seen.add(sent)
                    out.append({"id": f"d{len(out) + 1}", "text": sent, "session": _sid(s), "turn": turn,
                                "started_at": at})
        return out


def _warn(message: str) -> None:
    """A line on stderr when there is one (pythonw.exe has none)."""
    try:
        if sys.stderr is not None:
            term.say(message, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _num(v) -> int | float:
    """A count or cost read from a record: a finite number of sane size, else 0 (a record can hold anything)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    if v != v or abs(v) > 1e15:          # NaN, infinity, or nothing a token count or cost can be
        return 0
    return v


def _usage(s: dict) -> dict:
    """A record's usage with every value a number (see _num)."""
    u = s.get("usage")
    return {str(k): _num(v) for k, v in u.items()} if isinstance(u, dict) else {}


def _clean_map(m, project_name: str) -> dict:
    """The project map in the shape the routes read: a map.json that a repository shipped, a crash cut short or a
    hand edit garbled is shown as far as it makes sense instead of failing every route."""
    m = dict(m) if isinstance(m, dict) else {}
    files = m.get("files")
    files = files if isinstance(files, dict) else {}
    clean = {}
    for rel, i in files.items():
        if not isinstance(rel, str):
            continue
        i = dict(i) if isinstance(i, dict) else {}
        i["deps"] = [d for d in i.get("deps") or [] if isinstance(d, str)] if isinstance(i.get("deps"), list) else []
        for k in ("module", "lang", "kind"):
            if not isinstance(i.get(k), str):
                i[k] = "" if k == "module" else None
        clean[rel] = i
    m["files"] = clean
    m["project"] = m["project"] if isinstance(m.get("project"), str) else project_name
    m["engine"] = m["engine"] if isinstance(m.get("engine"), str) else "unknown"
    for k in ("conventions", "used_by", "rojo"):
        if not isinstance(m.get(k), dict):
            m[k] = {}
    m["used_by"] = {k: [x for x in v if isinstance(x, str)] for k, v in m["used_by"].items() if isinstance(v, list)}
    if not isinstance(m.get("scanned_at"), (str, type(None))):
        m["scanned_at"] = None
    return m


def _conventions(m: dict) -> list[str]:
    try:
        return [str(x) for x in convention_lines(m)]
    except (TypeError, KeyError, AttributeError, ValueError):   # a garbled conventions block: shown as none
        return []


def _prompt_source(x: dict) -> str:
    """Who wrote a record's or turn's prompt text: "user" (also for records that predate the field),
    "reported-by-client", or the "not captured ..." placeholder."""
    return str(x.get("prompt_source") or "user")


def _tree_files(m: dict) -> list[dict]:
    return [{"path": rel, "lang": i.get("lang"), "module": i.get("module"), "lines": i.get("lines"),
             "kind": i.get("kind")} for rel, i in sorted(m["files"].items())]


VAULT_TREE_MAX_DEPTH = 8          # folder levels listed below the store
VAULT_TREE_MAX_ENTRIES = 5000     # files and folders listed in all


def _vault_tree(d: Path, root: Path, _left: list | None = None, _depth: int = 0) -> tuple[list[dict], bool]:
    """(tree, truncated): the store's folders and regular files for the viewer's left panel. Links (symbolic links,
    junctions) are skipped, never followed (a store can hold `loop -> .` or a link to a large folder elsewhere); at most
    VAULT_TREE_MAX_DEPTH levels and VAULT_TREE_MAX_ENTRIES entries are listed, and `truncated` says something was left
    out."""
    left = [VAULT_TREE_MAX_ENTRIES] if _left is None else _left
    out: list[dict] = []
    truncated = False
    try:
        with os.scandir(d) as it:
            entries = []
            for e in it:                    # read no more names than can still be listed
                entries.append(e)
                if len(entries) > left[0]:
                    truncated = True
                    break
    except OSError:
        return out, False

    def is_dir(e) -> bool:
        try:
            return e.is_dir(follow_symlinks=False)
        except OSError:
            return False
    entries.sort(key=lambda e: (not is_dir(e), e.name.lower()))
    for e in entries:
        if e.is_symlink() or _is_link(e.path):
            continue
        if left[0] <= 0:
            truncated = True
            break
        rel = Path(e.path).relative_to(root).as_posix()
        if is_dir(e):
            left[0] -= 1
            if _depth + 1 >= VAULT_TREE_MAX_DEPTH:
                kids, cut = [], True
            else:
                kids, cut = _vault_tree(Path(e.path), root, left, _depth + 1)
            truncated = truncated or cut
            out.append({"name": e.name, "path": rel, "type": "dir", "children": kids})
        else:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            left[0] -= 1
            out.append({"name": e.name, "path": rel, "type": "file", "size": st.st_size})
    return out, truncated


def _resolve_in(base: Path, rel: str) -> Path | None:
    if not rel or "\x00" in rel:
        return None
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        return None
    fp = (base / p).resolve()
    if fp != base.resolve() and base.resolve() not in fp.parents:
        return None
    return fp


def home_info(st: State) -> dict:
    def ex(e):
        return {"name": e["name"], "desc": e["desc"], "project": str(e["project"]), "store": str(e["store"]),
                "exists": Path(e["store"]).exists()}
    rec = [r for r in load_recents() if Path(r["project"]).exists()]
    return {"current": {"project": str(st.project) if st.project else None, "store": str(st.store) if st.store else None,
                        "name": st.name()},
            "recents": rec,
            "examples": [ex(e) for e in EXAMPLES if Path(e["project"]).is_dir() and Path(e["store"]).is_dir()]}


def api_post(st: State, path: str, q: dict):
    g = lambda k, d="": (q.get(k) or [d])[0]
    if path == "/api/pick":
        kind = g("kind", "project")
        try:
            p = pick_dialog(kind)
        except PickUnavailable as e:
            return {"path": "", "cancelled": False,
                    "error": f"No folder dialog is available here ({e}). Start the app on a project instead: "
                             "devmem app --project <folder>"}
        return {"path": p, "cancelled": not p}
    if path == "/api/open":
        pr, sr = g("project"), g("store")
        if not pr:
            return 400, {"error": "project required"}
        from .store import is_remote_path, local_path
        if is_remote_path(pr) or (sr and is_remote_path(sr)):
            return 400, {"error": "a network path is not opened here"}   # stat-ing it would contact the host
        project = Path(local_path(pr))      # one spelling of the folder; device-namespace ones are refused below
        if not project.is_dir():
            return 400, {"error": f"not a folder: {pr}"}
        from .store import not_a_project
        if not_a_project(project):
            return 400, {"error": f"{pr} is the home folder, a root or a folder above home, not a project"}
        store = Path(sr) if sr else project / ".devmemory"
        if not store.is_dir() and not sr:
            pass
        has_map = (store / "index" / "map.json").is_file()
        if not has_map and g("scan") != "1":
            return {"needs_scan": True, "project": str(project.resolve()), "store": str(store)}
        with st.lock:
            st.set(project, store)
        opened_project, opened_store = st.paths()
        ro = st.read_only()
        if not has_map and not ro:
            scan(opened_project, opened_store)
        st.get_map()
        add_recent(opened_project, opened_store, opened_project.name)
        return {"ok": True, "name": st.name(), "scanned": not has_map and not ro, "read_only": ro}
    if path == "/api/reveal":
        rel = g("path")
        cands = []
        from .store import is_remote_path
        if is_remote_path(rel):
            return 400, {"error": "path outside project/store"}           # never stat-ed: it would contact the host
        if rel and Path(rel).is_absolute():
            cands = [Path(rel)]
        else:
            for base in (st.store, st.project):
                if base and (c := _resolve_in(base, rel)):
                    cands.append(c)
        cands = [c for c in cands if c.exists() and st.in_roots(c)]
        if not cands or (rel and ".." in Path(rel).parts):
            return 400, {"error": "path outside project/store"}
        if g("dry") != "1":
            cmd = reveal_command(cands[0])
            if cmd is None:
                return 501, {"error": "no file manager found (install xdg-utils)", "path": str(cands[0])}
            proc.popen(cmd)
        return {"ok": True, "path": str(cands[0])}
    return 404, {"error": "unknown endpoint"}


def api(st: State, path: str, q: dict):
    g = lambda k, d="": (q.get(k) or [d])[0]
    if path == "/api/home":
        return home_info(st)
    if path == "/api/state":
        return {"project": str(st.project) if st.project else None, "name": st.name()}
    if st.project is None or st.store is None:
        return 409, {"error": "no project open"}
    if path == "/api/vault":
        if not st.store.is_dir():
            return {"root": st.store.name, "path": str(st.store), "tree": [], "truncated": False}
        tree, truncated = _vault_tree(st.store, st.store)
        return {"root": st.store.name, "path": str(st.store), "tree": tree, "truncated": truncated}
    if path == "/api/vault/file":
        fp = _resolve_in(st.store, g("path"))
        if fp is None or not fp.is_file():
            return 404, {"error": "not found in store"}
        size = fp.stat().st_size
        if size > VAULT_CAP:
            return 413, {"error": f"file too large ({size} bytes, cap {VAULT_CAP})", "size": size}
        return {"path": fp.relative_to(st.store.resolve()).as_posix(), "size": size, "abs": str(fp),
                "content": read_store_text(fp, VAULT_CAP)}
    if path == "/api/overview":
        m = st.get_map()
        ss = st.sessions()
        fresh = {}
        tin = tout = tcache = 0
        cost = 0.0
        for s in ss:
            f = freshness(s, st.project)["status"]
            fresh[f] = fresh.get(f, 0) + 1
            u = _usage(s)            # every value a number: one odd record must not fail the overview
            tin += u.get("input_tokens", 0)
            tout += u.get("output_tokens", 0)
            tcache += u.get("cache_read_tokens", 0) + u.get("cache_creation_tokens", 0)
            cost += u.get("cost_usd_list", 0)
        return {"project": m["project"], "dir_name": st.name(), "engine": m["engine"], "file_count": len(m["files"]),
                "session_count": len(ss), "freshness": fresh, "tokens": {"input": tin, "output": tout, "cache": tcache},
                "cost_usd": round(cost, 4), "conventions": _conventions(m), "rojo": m.get("rojo", {}),
                "scanned_at": m.get("scanned_at"), "store": str(st.store), "project_path": str(st.project),
                "decision_count": len(st.decisions()), "read_only": st.read_only(),
                "per_session": [{"id": _sid(s), "started_at": s.get("started_at") or "",
                                 "input": _usage(s).get("input_tokens", 0),
                                 "output": _usage(s).get("output_tokens", 0),
                                 "cache": _usage(s).get("cache_read_tokens", 0)
                                 + _usage(s).get("cache_creation_tokens", 0)} for s in ss]}
    if path == "/api/files":
        m = st.get_map(rescan=g("rescan") == "1")
        return {"files": _tree_files(m), "scanned_at": m.get("scanned_at"), "read_only": st.read_only()}
    if path == "/api/file":
        rel = g("path")
        m = st.get_map()
        if rel not in m["files"]:
            return 404, {"error": "not in map"}
        from .redact import is_secret_file
        from .store import project_path, read_project_text
        fp = project_path(st.project, rel)
        text = read_project_text(fp) if fp is not None and not is_secret_file(rel) else None
        if text is None:          # not a plain project path, another spelling of a file, a link, or a secret file
            return 404, {"error": "not found"}
        touched = []
        for s in st.sessions():
            role = "edited" if rel in s.get("files_edited", []) else "read" if rel in s.get("files_read", []) else None
            if role:
                touched.append({**st.session_view(s), "role": role, "prompt": (s.get("prompt") or "")[:140]})
        deps = m["files"][rel].get("deps", [])
        by_module = {i["module"]: r for r, i in m["files"].items()}
        requires = sorted({by_module[d.split(".")[-1]] for d in deps if d.split(".")[-1] in by_module} - {rel})
        return {"path": rel, "content": text, "entry": m["files"][rel],
                "used_by": m.get("used_by", {}).get(rel, []), "requires": requires, "sessions": touched}
    if path == "/api/sessions":
        return {"sessions": [{**st.session_view(s), "prompt": (s.get("prompt") or "")[:200]} for s in st.sessions()]}
    if path == "/api/session":
        sid = g("id")
        for s in st.sessions():
            if _sid(s) == sid or _sid(s).startswith(sid) and sid:
                meta = {k: v for k, v in s.items()}
                return {"meta": meta, "freshness": freshness(s, st.project), "id": _sid(s)}
        return 404, {"error": "unknown session"}
    if path == "/api/decisions":
        return {"decisions": st.decisions()}
    if path == "/api/graph":
        m = st.get_map()
        nodes, edges, ids = [], [], set()
        by_module = {i["module"]: r for r, i in m["files"].items()}
        used = m.get("used_by", {})
        for rel, i in m["files"].items():
            if i.get("lang") == "text" and not rel.endswith(".md"):
                continue
            ids.add("file:" + rel)
            nodes.append({"id": "file:" + rel, "type": "file", "label": Path(rel).name.split(".")[0],
                          "size": 4 + min(10, len(used.get(rel, [])) * 1.5), "status": i.get("lang", "")})
        for rel, i in m["files"].items():
            if "file:" + rel not in ids:
                continue
            for d in i.get("deps", []):
                t = by_module.get(d.split(".")[-1])
                if t and t != rel and "file:" + t in ids:
                    edges.append({"source": "file:" + rel, "target": "file:" + t, "kind": "requires"})
        for s in st.sessions():
            sid = "session:" + _sid(s)
            fr = freshness(s, st.project)["status"]
            nodes.append({"id": sid, "type": "session", "label": (s.get("prompt") or "session")[:28],
                          "size": 7, "status": fr})
            for f in s.get("files_edited", []):
                if "file:" + f in ids:
                    edges.append({"source": sid, "target": "file:" + f, "kind": "edited"})
            for f in s.get("files_read", []):
                if "file:" + f in ids and f not in s.get("files_edited", []):
                    edges.append({"source": sid, "target": "file:" + f, "kind": "read"})
        for d in st.decisions():
            nodes.append({"id": "decision:" + d["id"], "type": "decision", "label": d["text"][:30], "size": 5, "status": ""})
            edges.append({"source": "session:" + d["session"], "target": "decision:" + d["id"], "kind": "states"})
        for n, line in enumerate(_conventions(m)):
            cid = f"convention:c{n + 1}"
            nodes.append({"id": cid, "type": "convention", "label": re.sub(r"[`]", "", line)[:30], "size": 5, "status": ""})
            for f in re.findall(r"(?<![\w./-])[\w./-]{1,300}\.(?:luau|lua|py|js|ts|json)(?!\w)", line):
                if "file:" + f in ids:
                    edges.append({"source": cid, "target": "file:" + f, "kind": "states"})
        return {"nodes": nodes, "edges": edges}
    if path == "/api/pack":
        task = g("task").strip()
        if not task:
            return 400, {"error": "task required"}
        try:
            budget = max(100, min(20000, int(g("budget", "1500"))))
        except ValueError:
            budget = 1500
        mode = g("mode", "full")
        if mode not in ("full", "static", "nofresh"):
            mode = "full"
        if st.read_only():
            return 409, {"error": st.read_only()}
        text, info = build_pack(st.project, st.store, task, budget=budget, mode=mode)
        m = st.get_map()
        sess = {_sid(s): s for s in st.sessions()}
        for it in info["sessions"]:
            s = sess.get(str(it["session"]))
            if s:
                it["prompt"] = (s.get("prompt") or "")[:160]
                it["started_at"] = s.get("started_at", "")
        info["file_details"] = [{"path": f, "lines": m["files"].get(f, {}).get("lines"),
                                 "module": m["files"].get(f, {}).get("module")} for f in info["files"]]
        return {"markdown": text, "info": {k: v for k, v in info.items()}, "budget": budget}
    return 404, {"error": "unknown endpoint"}


def allowed_hosts_for(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


def check_request(method: str, path: str, headers, st: "State") -> tuple[int, str] | None:
    """Return (status, message) when the request must be refused, else None.

    - Host must be the address we are bound to (stops DNS rebinding).
    - A browser-supplied Origin must be this server (stops other web pages from sending requests).
      Requests without Origin (curl, the app's own GET navigation) are allowed by this rule alone.
    - Every /api/ call needs the per-launch token in the X-DevMemory-Token header, except the one call that obtains it:
      POST /api/launch with a single-use launch code in the X-DevMemory-Launch header (the code is checked by
      State.claim_launch). It passes the Host, Origin and Sec-Fetch checks above like every other call, and a custom
      header cannot be sent cross-origin without a CORS preflight, which this server never grants.
    """
    host = (headers.get("Host") or "").strip().lower()
    if host not in st.allowed_hosts:
        return 403, "bad Host header"
    origin = headers.get("Origin")
    if origin is not None:
        o = origin.strip().lower()
        if o not in {"http://" + h for h in st.allowed_hosts}:
            return 403, "cross-origin request refused"
    site = (headers.get("Sec-Fetch-Site") or "").lower()
    if site and site not in ("same-origin", "none"):
        # A person clicking the printed link in a chat, terminal, or document is a cross-site top-level
        # navigation. Serving the static page to it is harmless (the token is still needed for every /api/ call).
        top_level = ((headers.get("Sec-Fetch-Mode") or "").lower() == "navigate"
                     and (headers.get("Sec-Fetch-Dest") or "").lower() == "document")
        if not (method == "GET" and top_level and not path.startswith("/api/")):
            return 403, "cross-site request refused"
    if path == LAUNCH_PATH and headers.get(LAUNCH_HEADER):
        return None if method == "POST" else (405, "use POST")
    if path.startswith("/api/"):
        given = headers.get(TOKEN_HEADER) or ""
        if not hmac.compare_digest(given.encode("utf-8"), st.token.encode("utf-8")):
            return 401, "missing or invalid token (open the URL printed by devmem ui / use devmem app)"
    return None


MAX_BODY = 64 * 1024            # the API takes its parameters in the query string: no request needs a body
REQUEST_READ_SECONDS = 10.0     # the request line and headers must have arrived by then, or the connection is dropped
SOCKET_TIMEOUT = 30.0           # for each read or write on a connection
# a socket that went away under a request: not connected, shut down, a closed handle; Windows WSAENOTSOCK,
# WSAESHUTDOWN, WSAENOTCONN (the ConnectionError subclasses cover resets, aborts and broken pipes)
_CONNECTION_ERRNOS = {getattr(errno, n) for n in ("ENOTCONN", "ESHUTDOWN", "EBADF") if hasattr(errno, n)} | {
    10038, 10057, 10058}
MAX_CONNECTIONS = 64            # connections served at once; more are closed at once


class ViewerServer(ThreadingHTTPServer):
    """The viewer's server (127.0.0.1 only). On Windows SO_REUSEADDR lets another program bind the same port and
    receive connections meant for the viewer, so it is off there and SO_EXCLUSIVEADDRUSE is on (a restart on the same
    port still works at once: the tests check it). On Linux and macOS SO_REUSEADDR only lets a restart bind while old
    connections on the port are in TIME_WAIT, and a second listener is still refused, so it stays on there."""
    allow_reuse_address = os.name != "nt"
    daemon_threads = True
    max_connections = MAX_CONNECTIONS

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self.handler_errors: list[str] = []     # exception types that reached handle_error (not connection errors)

    def handle_error(self, request, client_address):
        """A connection a browser dropped (reset, aborted, a broken pipe, a timeout) is ordinary: nothing is printed.
        Anything else is one line with the exception type only (its message and the default traceback hold paths),
        the traceback with DEVMEM_DEBUG=1, and is kept in `handler_errors` so the tests see it."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)) or (
                isinstance(exc, OSError) and (exc.errno in _CONNECTION_ERRNOS
                                              or getattr(exc, "winerror", None) in _CONNECTION_ERRNOS)):
            return
        name = type(exc).__name__ if exc is not None else "unknown error"
        self.handler_errors.append(name)
        if os.environ.get("DEVMEM_DEBUG"):
            import traceback
            from . import term
            term.warn(traceback.format_exc().rstrip())    # escaped: the message can quote a request or a store
        else:
            _warn(f"devmem ui: a request failed ({name}); set DEVMEM_DEBUG=1 for details")

    def process_request(self, request, client_address):
        # at most max_connections at once (each holds a thread); one more is closed at once, not queued
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def server_bind(self):
        if os.name == "nt":
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


_DATA_ERRORS = (TypeError, KeyError, ValueError, AttributeError, IndexError, RecursionError, OverflowError,
                UnicodeError)


def _error_reply(e: BaseException) -> tuple[int, bytes, str]:
    """The answer to a request that raised: data in the store a route could not use (a record or map of the wrong
    shape) is 422 with a JSON error naming the kind of problem; anything else 500. Never a traceback."""
    if isinstance(e, _DATA_ERRORS):
        body = {"error": f"the store holds data this view cannot use ({type(e).__name__})"}
        return 422, json.dumps(body).encode("utf-8"), "application/json; charset=utf-8"
    return 500, json.dumps({"error": type(e).__name__}).encode("utf-8"), "application/json; charset=utf-8"


def make_handler(st: State):
    class H(BaseHTTPRequestHandler):
        timeout = SOCKET_TIMEOUT     # StreamRequestHandler sets it on the connection

        def log_message(self, format, *args):  # silence the per-request stderr line
            pass

        def setup(self):
            super().setup()
            # A request that has not fully arrived in REQUEST_READ_SECONDS is dropped: a slow or stalled local client
            # must not hold a server thread. Cancelled once the headers are in (the work itself may take longer).
            self._dropped = False
            self._deadline = threading.Timer(REQUEST_READ_SECONDS, self._drop)
            self._deadline.daemon = True
            self._deadline.start()

        def _drop(self):
            self._dropped = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def finish(self):
            self._deadline.cancel()
            super().finish()

        def parse_request(self) -> bool:
            ok = super().parse_request()      # reads the headers
            self._deadline.cancel()
            if self._dropped or not ok:
                self.close_connection = True
                return False
            if self.headers.get("Transfer-Encoding"):
                self.send_error(413, "request bodies are not accepted")
                return False
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self.send_error(400, "bad Content-Length")
                return False
            if length < 0 or length > MAX_BODY:
                self.send_error(413, "request body too large")
                return False
            return True

        def send_error(self, code, message=None, explain=None):
            """The standard library's error replies (400, 413, 414, 431, 501...) with the same headers as every other
            reply (CSP, no-store, nosniff): a JSON body, and the connection closed."""
            self.close_connection = True
            if getattr(self, "request_version", "") == "HTTP/0.9":
                self.request_version = "HTTP/1.0"     # a malformed request line: still a status line and headers
            body = json.dumps({"error": str(message or code)}).encode("utf-8")
            if self.command == "HEAD" or code < 200 or code in (204, 304):
                body = b""
            try:
                self._send(code, body, "application/json; charset=utf-8")
            except OSError:
                pass

        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _refused(self, u) -> bool:
            bad = check_request(self.command, u.path, self.headers, st)
            if bad:
                self._send(bad[0], json.dumps({"error": bad[1]}).encode("utf-8"), "application/json; charset=utf-8")
            elif u.path.startswith("/api/") and u.path != LAUNCH_PATH:
                # only token-checked API calls keep an idle app server alive (a static page or a launch code needs no
                # token; a successful launch-code exchange counts below)
                st.last_seen = time.monotonic()
            return bad is not None

        def do_POST(self):
            u = urlparse(self.path)
            try:
                if self._refused(u):
                    return
                if not u.path.startswith("/api/"):
                    return self._send(404, b"not found", "text/plain")
                if u.path == LAUNCH_PATH:
                    token, why = st.claim_launch(self.headers.get(LAUNCH_HEADER) or "")
                    res = {"token": token} if token else (403, {"error": why})
                    if token:
                        st.last_seen = time.monotonic()
                else:
                    res = api_post(st, u.path, parse_qs(u.query))
                code, data = res if isinstance(res, tuple) else (200, res)
                self._send(code, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                try:
                    self._send(*_error_reply(e))
                except Exception:
                    pass

        def do_GET(self):
            u = urlparse(self.path)
            try:
                if self._refused(u):
                    return
                if u.path.startswith("/api/"):
                    res = api(st, u.path, parse_qs(u.query))
                    code, data = res if isinstance(res, tuple) else (200, res)
                    return self._send(code, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")
                name = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
                fp = (STATIC / name).resolve()
                if STATIC.resolve() not in fp.parents or not fp.is_file():
                    return self._send(404, b"not found", "text/plain")
                return self._send(200, fp.read_bytes(), MIME.get(fp.suffix, "application/octet-stream"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # keep the viewer alive
                try:
                    self._send(*_error_reply(e))
                except Exception:
                    pass
    return H


class NotAProject(ValueError):
    """The viewer is not opened on the home folder, a root, a folder above home or a network path."""


def _viewer_folder(p, what: str, must_exist: bool = True) -> Path:
    """The folder `p` (project or store) the viewer is started on, or NotAProject: a network path is refused before
    anything touches it, and the home folder, a drive root or a folder above home is never a project (the same guard
    as devmem.cli._project, for every way into make_server: `devmem app`, `devmem ui`, DevMemory.bat)."""
    from .store import NOT_A_PROJECT_TEXT, is_remote_path, local_path, not_a_project
    if is_remote_path(p):
        raise NotAProject(f"{p} is a network path; the viewer opens only a local {what} folder")
    folder = Path(local_path(Path(p).expanduser()))
    if must_exist and not folder.is_dir():
        raise NotAProject(f"no such {what} folder: {p}")
    why = not_a_project(folder) if (must_exist or folder.exists()) else None
    if why:
        raise NotAProject(f"{p} is {NOT_A_PROJECT_TEXT.get(why, why)}, not a {what} folder")
    return folder.resolve()


def make_server(project: Path | None, store: Path | None = None, port: int = 8765, token: str | None = None):
    """Build (but do not start) the server; port 0 picks a free port. Returns (server, state).

    The per-launch token is `state.token`; `launch_url(server, state)` builds a URL with a single-use code for it.
    """
    if project is not None:
        project = Path(_viewer_folder(project, "project"))
        if store is not None:
            _viewer_folder(store, "store", must_exist=False)
        store = store or store_path(project)
        if (Path(store) / "index" / "map.json").is_file():
            add_recent(project, Path(store).resolve(), project.name)
    st = State(project, store)
    if token:
        st.token = token
    srv = ViewerServer(("127.0.0.1", port), make_handler(st))
    st.allowed_hosts = allowed_hosts_for(srv.server_address[1])
    return srv, st


def launch_url_for_port(port: int, st: State) -> str:
    return f"http://127.0.0.1:{port}/?launch={st.new_launch_code()}"


def launch_url(srv, st: State) -> str:
    """A link that opens the viewer: it carries a fresh single-use launch code (valid LAUNCH_SECONDS), never the token."""
    return launch_url_for_port(srv.server_address[1], st)


def new_links_on_enter(srv, st: State, stdin=None) -> None:
    """Print a fresh launch link each time Enter is pressed (a printed link works once, for LAUNCH_SECONDS)."""
    stdin = sys.stdin if stdin is None else stdin
    try:
        while stdin is not None and stdin.readline():
            term.say(f"New link (works once, for {LAUNCH_SECONDS:.0f} seconds): {launch_url(srv, st)}", flush=True)
    except (OSError, ValueError):
        pass


def serve(project: Path | None, store: Path | None = None, port: int = 8765, open_browser: bool = True):
    srv, st = make_server(project, store, port)
    term.say(f"DevMemory UI on http://127.0.0.1:{srv.server_address[1]}/  (project {st.project}, store {st.store})  "
          "Ctrl+C to stop", flush=True)
    if open_browser:
        url = launch_url(srv, st)
        threading.Timer(0.4, lambda: proc.open_url(url)).start()
        term.say("Opening it in your browser.", flush=True)
    else:
        term.say(f"Open: {launch_url(srv, st)}", flush=True)
    term.say(f"A link works once, within {LAUNCH_SECONDS:.0f} seconds. Press Enter here for a new one.", flush=True)
    threading.Thread(target=new_links_on_enter, args=(srv, st), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
