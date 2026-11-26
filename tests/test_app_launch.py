"""`devmem app` as the Windows installer's Start Menu entry runs it: pythonw.exe (no console), no project, possibly
twice. The browser and the clock are injected; nothing here opens a window."""
from __future__ import annotations

import sys
import webbrowser
from pathlib import Path

import pytest

from devmem import app, cli, ui_server
from ui_helpers import request, running_server, with_token


# ---------------------------------------------------------------- one browser profile per window

def test_profile_in_use_follows_the_browser_lock(tmp_path):
    p = tmp_path / "browser"
    assert not app.profile_in_use(p)                      # not created yet
    p.mkdir()
    assert not app.profile_in_use(p)
    (p / "lockfile").write_text("", encoding="utf-8")     # Chromium on Windows, while it runs
    assert app.profile_in_use(p)
    q = tmp_path / "other"
    q.mkdir()
    (q / "SingletonLock").write_text("", encoding="utf-8")   # Chromium on macOS and Linux (a link there)
    assert app.profile_in_use(q)


def test_free_profile_skips_profiles_a_browser_is_running_on(tmp_path):
    base = tmp_path / "browser"
    busy: set[Path] = set()
    in_use = lambda p: p in busy   # noqa: E731
    assert app.free_profile(base, in_use) == base
    busy.add(base)
    assert app.free_profile(base, in_use) == tmp_path / "browser-2"
    busy.add(tmp_path / "browser-2")
    assert app.free_profile(base, in_use) == tmp_path / "browser-3"
    busy.update(tmp_path / f"browser-{i}" for i in range(3, app.PROFILE_SLOTS + 1))
    assert app.free_profile(base, in_use) == base          # all taken: the first one (the old behaviour)


def test_browser_command_uses_the_given_profile(tmp_path):
    cmd = app.browser_command("http://127.0.0.1:1/?token=t", exe="/x/chrome", platform="linux", env={},
                              home=Path("/h"), profile=tmp_path / "browser-2")
    assert f"--user-data-dir={tmp_path / 'browser-2'}" in cmd


# ---------------------------------------------------------------- the server outlives a handed-off window

class FakeProc:
    def __init__(self):
        self.waited = False

    def wait(self):
        self.waited = True
        return 0


class Clock:
    def __init__(self, *times):
        self.times = list(times)

    def __call__(self):
        return self.times.pop(0) if len(self.times) > 1 else self.times[0]


@pytest.fixture
def fake_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "find_browser", lambda *a, **k: "/x/chrome")
    monkeypatch.setattr(app, "profile_dir", lambda *a, **k: tmp_path / "profiles" / "browser")
    launched: list[list[str]] = []
    proc = FakeProc()

    def popen(cmd):
        launched.append(cmd)
        return proc
    return launched, proc, popen


def test_a_browser_that_keeps_running_ends_the_app_when_it_exits(fake_browser):
    launched, proc, popen = fake_browser
    idle_waits = []
    assert app._run_app(None, None, False, 0, popen=popen, clock=Clock(0.0, 600.0), wait_idle=idle_waits.append) == 0
    assert proc.waited and idle_waits == []
    assert launched[0][0] == "/x/chrome" and launched[0][1].startswith("--app=http://127.0.0.1:")


def test_a_browser_that_hands_its_window_over_does_not_stop_the_server(fake_browser):
    launched, proc, popen = fake_browser
    idle_waits = []
    assert app._run_app(None, None, False, 0, popen=popen, clock=Clock(0.0, 0.4), wait_idle=idle_waits.append) == 0
    assert proc.waited and len(idle_waits) == 1      # served until the page went idle, not stopped at once


def test_the_second_window_gets_its_own_profile(fake_browser, tmp_path):
    launched, proc, popen = fake_browser
    first = tmp_path / "profiles" / "browser"
    first.mkdir(parents=True)
    (first / "lockfile").write_text("", encoding="utf-8")       # the first window's browser is still running
    app._run_app(None, None, False, 0, popen=popen, clock=Clock(0.0, 600.0), wait_idle=lambda st: None)
    assert f"--user-data-dir={tmp_path / 'profiles' / 'browser-2'}" in launched[0]
    assert (tmp_path / "profiles" / "browser-2").is_dir()


def test_a_browser_that_cannot_start_falls_back_to_the_default_browser(fake_browser, monkeypatch):
    _, _, _ = fake_browser
    opened = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    monkeypatch.setattr(app, "has_console", lambda: False)

    def broken(cmd):
        raise OSError("not a program")
    idle_waits = []
    assert app._run_app(None, None, False, 0, popen=broken, clock=Clock(0.0), wait_idle=idle_waits.append) == 0
    assert opened and opened[0].startswith("http://127.0.0.1:")
    assert len(idle_waits) == 1        # windowless: no Ctrl+C, so it stops once the page is idle (never runs forever)


def test_wait_until_idle_counts_from_the_last_request():
    class St:
        last_seen = 0.0
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s
        if now[0] == 10.0:
            St.last_seen = 10.0          # a request arrived: the idle time starts again
    app.wait_until_idle(St, idle=20.0, clock=lambda: now[0], sleep=sleep, poll=5.0)
    assert now[0] == 30.0


def test_only_accepted_requests_count_as_activity(tmp_path):
    with running_server(None, None) as (port, st):
        st.last_seen = 0.0
        assert request(port, "GET", "/api/state")[0] == 401           # no token: refused, not activity
        assert st.last_seen == 0.0
        assert request(port, "GET", "/api/state", with_token(st))[0] == 200
        assert st.last_seen > 0.0


def test_token_less_requests_do_not_keep_the_server_alive(tmp_path):
    """V12: a static page or a refused launch code needs no token, so it must not count as the page being used."""
    from devmem.ui_server import LAUNCH_HEADER, LAUNCH_PATH
    with running_server(None, None) as (port, st):
        st.last_seen = 0.0
        assert request(port, "GET", "/")[0] == 200
        assert request(port, "GET", "/app.js")[0] == 200
        assert request(port, "POST", LAUNCH_PATH, {LAUNCH_HEADER: "not-a-code"})[0] == 403
        assert st.last_seen == 0.0


# ---------------------------------------------------------------- no console (pythonw.exe)

def test_app_dry_run_without_a_console(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert cli.main(["app", "--dry-run"]) == 0     # (argparse's own output, e.g. --version, needs 3.12+ without streams)


def test_an_error_without_a_console_is_shown_not_lost(monkeypatch):
    shown = []
    monkeypatch.setattr(app, "show_error", shown.append)

    def boom(*a, **k):
        raise OSError("no port")
    monkeypatch.setattr(app, "_run_app", boom)
    monkeypatch.setattr(app, "has_console", lambda: False)
    assert app.run_app(None, None) == 1
    assert shown and "no port" in shown[0]
    monkeypatch.setattr(app, "has_console", lambda: True)
    with pytest.raises(OSError):
        app.run_app(None, None)


def test_show_error_does_nothing_off_windows():
    app.show_error("x", platform="linux")


# ---------------------------------------------------------------- the folder dialog without tkinter

def test_pick_dialog_falls_back_to_the_next_dialog():
    def no_tk(title):
        raise ui_server.PickUnavailable("tkinter is not installed")
    seen = []

    def shell(title):
        seen.append(title)
        return str(Path("C:/work/game"))
    assert ui_server.pick_dialog("project", pickers=[no_tk, shell]) == str(Path("C:/work/game"))
    assert seen == ["Select project folder"]
    assert ui_server.pick_dialog("store", pickers=[lambda t: ""]) == ""          # cancelled


def test_pick_dialog_reports_when_no_dialog_can_be_shown():
    def no_tk(title):
        raise ui_server.PickUnavailable("tkinter is not installed")

    def broken(title):
        raise RuntimeError("no display")
    with pytest.raises(ui_server.PickUnavailable) as e:
        ui_server.pick_dialog("project", pickers=[no_tk, broken])
    assert "tkinter is not installed" in str(e.value) and "no display" in str(e.value)


def test_folder_pickers_per_platform():
    assert ui_server.folder_pickers("linux") == [ui_server._pick_tk]
    assert ui_server.folder_pickers("win32") == [ui_server._pick_tk, ui_server._pick_windows]


def test_pick_endpoint_says_why_when_no_dialog_exists(monkeypatch):
    def unavailable(kind):
        raise ui_server.PickUnavailable("tkinter is not installed")
    monkeypatch.setattr(ui_server, "pick_dialog", unavailable)
    with running_server(None, None) as (port, st):
        status, _, body = request(port, "POST", "/api/pick?kind=project", with_token(st))
    assert status == 200 and body["path"] == "" and body["cancelled"] is False
    assert "devmem app --project" in body["error"]
