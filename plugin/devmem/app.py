"""`devmem app`: run the UI server on a free port and show it in a standalone Chrome/Edge app window.

Browser discovery and per-platform paths take the platform, environment and lookup functions as
arguments so the macOS and Linux branches can be unit-tested from any host.

The Windows installer's Start Menu entry runs `pythonw.exe -m devmem app`: no console, no project (the app opens on its
home page). So: each window gets a browser profile no other window is using (free_profile), a browser process that
hands its window over and exits does not stop the server (wait_until_idle), and an error is shown in a message box.
"""
from __future__ import annotations

import os
import shlex
import sys
import threading
import time
from pathlib import Path

from .proc import open_url, popen as _popen
from . import term
from .store import find_program
from .ui_server import LAUNCH_SECONDS, launch_url, make_server, new_links_on_enter

# Chromium-family browsers that understand --app=URL, in order of preference.
_LINUX_NAMES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                "microsoft-edge", "microsoft-edge-stable", "brave-browser"]
_MAC_APPS = [("Google Chrome", "Google Chrome"), ("Microsoft Edge", "Microsoft Edge"),
             ("Chromium", "Chromium"), ("Brave Browser", "Brave Browser")]
_WIN_REL = [r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe",
            r"BraveSoftware\Brave-Browser\Application\brave.exe"]


def browser_candidates(platform: str | None = None, env=None, home: Path | None = None) -> list[str]:
    """Absolute install paths worth probing (Windows and macOS). Linux uses PATH names instead."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = Path(home) if home else Path.home()
    out: list[str] = []
    if platform.startswith("win"):
        roots = [env.get("PROGRAMFILES") or r"C:\Program Files", env.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)",
                 env.get("LOCALAPPDATA") or str(home / "AppData" / "Local")]
        for rel in _WIN_REL:
            out += [str(Path(r) / rel) for r in roots]
    elif platform == "darwin":
        for base in (Path("/Applications"), home / "Applications"):
            out += [str(base / f"{app}.app" / "Contents" / "MacOS" / exe) for app, exe in _MAC_APPS]
    return out


def find_browser(platform: str | None = None, env=None, home: Path | None = None,
                 is_file=lambda p: Path(p).is_file(), which=find_program) -> str | None:
    """First Chromium-family browser found, or None (callers then fall back to the default browser)."""
    platform = platform or sys.platform
    for b in browser_candidates(platform, env, home):
        if is_file(b):
            return b
    if not platform.startswith("win") and platform != "darwin":
        for name in _LINUX_NAMES:
            found = which(name)
            if found:
                return found
    return None


def profile_dir(platform: str | None = None, env=None, home: Path | None = None) -> Path:
    """Private browser profile so the app window never touches the user's own profile."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = Path(home) if home else Path.home()
    if platform.startswith("win"):
        base = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(env.get("XDG_CACHE_HOME") or home / ".cache")
    return base / "DevMemory" / "browser"


PROFILE_SLOTS = 4          # app windows that can run side by side, each on its own browser profile
HANDOFF_SECONDS = 5.0      # a browser process that ends sooner than this handed its window to another instance
IDLE_SECONDS = 30 * 60     # then the server stays up until the page has made no request for this long


def profile_in_use(path: Path) -> bool:
    """Is a Chromium-family browser running on this user-data-dir? While it runs it holds `lockfile` in it open (Windows;
    the file is deleted when the browser closes) or keeps a `SingletonLock` link there (macOS, Linux)."""
    return os.path.lexists(Path(path) / "lockfile") or os.path.lexists(Path(path) / "SingletonLock")


def free_profile(base: Path, in_use=profile_in_use, slots: int = PROFILE_SLOTS) -> Path:
    """The first app profile no browser runs on: `base`, then `base-2`, `base-3` ... (`base` again when all are taken).
    A browser started on a profile that is in use hands its window to the running browser and exits at once, and the
    window would then show a server that `run_app` has already stopped (a second click on the DevMem shortcut)."""
    base = Path(base)
    for i in range(1, slots + 1):
        p = base if i == 1 else base.with_name(f"{base.name}-{i}")
        if not in_use(p):
            return p
    return base


def browser_command(url: str, exe: str | None = None, platform: str | None = None, env=None,
                    home: Path | None = None, profile: Path | None = None) -> list[str] | None:
    exe = exe or find_browser(platform, env, home)
    if not exe:
        return None
    profile = profile or profile_dir(platform, env, home)
    return [exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1440,900",
            "--no-first-run", "--no-default-browser-check"]


def has_console() -> bool:
    """False under pythonw.exe (the DevMem Start Menu shortcut): print() then goes nowhere and Ctrl+C cannot happen."""
    return sys.stdout is not None and sys.stderr is not None


def wait_until_idle(st, idle: float = IDLE_SECONDS, clock=time.monotonic, sleep=time.sleep, poll: float = 5.0) -> None:
    """Return once the server has answered no request for `idle` seconds (`st.last_seen`, set by the request handler)."""
    while clock() - st.last_seen < idle:
        sleep(poll)


def show_error(message: str, platform: str | None = None) -> None:
    """Tell the user something failed when there is no console to print it to: a message box on Windows."""
    if not (platform or sys.platform).startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "DevMem", 0x10 | 0x10000)   # MB_ICONERROR | MB_SETFOREGROUND
    except Exception:  # noqa: BLE001 - nothing else can report it
        pass


def run_app(project: Path | None, store: Path | None, dry_run: bool = False, port: int = 0) -> int:
    try:
        return _run_app(project, store, dry_run, port)
    except Exception as e:
        if has_console():
            raise
        show_error(f"DevMem could not start the viewer.\n\n{type(e).__name__}: {e}")
        return 1


def _run_app(project: Path | None, store: Path | None, dry_run: bool, port: int,
             popen=_popen, clock=time.monotonic, wait_idle=wait_until_idle) -> int:
    srv, st = make_server(project, store, port)
    # A single-use launch code, not the token: the browser's command line can be read by other local users. The page
    # exchanges it for the token at once (it is dead after that) and keeps the token in the tab, never in a URL.
    url = launch_url(srv, st)
    profile = free_profile(profile_dir())
    cmd = browser_command(url, profile=profile)
    if dry_run:
        term.say(url)
        term.say(" ".join(shlex.quote(c) if " " in c else c for c in cmd) if cmd else "(no Chrome/Edge found: would open the default browser)")
        srv.server_close()
        return 0
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        proc = None
        started = clock()
        if cmd:
            profile.mkdir(parents=True, exist_ok=True)
            try:
                proc = popen(cmd)
            except OSError:
                proc = None          # the browser found on disk cannot be started: use the default browser
        if proc is not None:
            # On its own profile the browser process lives until its window closes. One that ends at once handed the
            # window to a browser already running on that profile, and the window still needs this server.
            proc.wait()
            if clock() - started < HANDOFF_SECONDS:
                wait_idle(st)
        else:
            open_url(url)
            term.say(f"DevMemory running at {url}  (Ctrl+C to stop)", flush=True)
            if has_console():
                term.say(f"The link works once, within {LAUNCH_SECONDS:.0f} seconds. Press Enter here for a new one.",
                      flush=True)
                new_links_on_enter(srv, st)
                threading.Event().wait()
            else:
                wait_idle(st)        # no console, so no Ctrl+C: stop once the page is no longer used
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(run_app(None, None))
