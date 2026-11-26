"""Per-platform branches, tested by injecting the platform and lookups. Only the host platform's branch
runs for real; the other platforms are covered by these unit tests alone."""
from __future__ import annotations

from pathlib import Path

from devmem import app, ui_server


def test_recents_file_per_platform(tmp_path):
    home = tmp_path / "home"
    win = ui_server.recents_file("win32", {"APPDATA": str(tmp_path / "roam")}, home)
    assert win == tmp_path / "roam" / "DevMemory" / "recents.json"
    assert ui_server.recents_file("win32", {}, home) == home / "AppData" / "Roaming" / "DevMemory" / "recents.json"
    assert ui_server.recents_file("darwin", {}, home) == home / "Library" / "Application Support" / "DevMemory" / "recents.json"
    assert ui_server.recents_file("linux", {}, home) == home / ".config" / "DevMemory" / "recents.json"
    assert ui_server.recents_file("linux", {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, home) == tmp_path / "xdg" / "DevMemory" / "recents.json"


def test_reveal_command_per_platform(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    assert ui_server.reveal_command(f, "win32")[0].lower().endswith("explorer.exe")   # absolute, never a bare name
    assert ui_server.reveal_command(f, "win32")[1].startswith("/select,")
    assert ui_server.reveal_command(f, "darwin") == ["open", "-R", str(f)]
    assert ui_server.reveal_command(f, "linux", which=lambda n: "/usr/bin/xdg-open") == ["/usr/bin/xdg-open", str(tmp_path)]
    assert ui_server.reveal_command(tmp_path, "linux", which=lambda n: "/usr/bin/xdg-open") == ["/usr/bin/xdg-open", str(tmp_path)]
    assert ui_server.reveal_command(f, "linux", which=lambda n: None) is None


def test_find_browser_windows_probes_install_dirs():
    env = {"PROGRAMFILES": r"C:\PF", "PROGRAMFILES(X86)": r"C:\PF86", "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
    cands = app.browser_candidates("win32", env, Path("/h"))
    assert any(c.endswith("chrome.exe") for c in cands) and any(c.endswith("msedge.exe") for c in cands)
    edge = next(c for c in cands if c.endswith("msedge.exe") and "PF86" in c)
    assert app.find_browser("win32", env, Path("/h"), is_file=lambda p: p == edge) == edge
    assert app.find_browser("win32", env, Path("/h"), is_file=lambda p: False) is None


def test_find_browser_macos_probes_app_bundles():
    home = Path("/Users/u")
    cands = app.browser_candidates("darwin", {}, home)
    assert any("Google Chrome.app" in c for c in cands)
    chrome_user = next(c for c in cands if "Google Chrome.app" in c and "Users" in c.replace("\\", "/"))
    assert app.find_browser("darwin", {}, home, is_file=lambda p: p == chrome_user) == chrome_user
    assert app.find_browser("darwin", {}, home, is_file=lambda p: False, which=lambda n: "/should/not/use") is None


def test_find_browser_linux_uses_path_in_preference_order():
    found = {"chromium": "/usr/bin/chromium", "brave-browser": "/usr/bin/brave-browser"}
    assert app.find_browser("linux", {}, Path("/h"), is_file=lambda p: False, which=found.get) == "/usr/bin/chromium"
    assert app.find_browser("linux", {}, Path("/h"), is_file=lambda p: False, which=lambda n: None) is None


def test_no_browser_means_no_command_so_caller_falls_back_to_default_browser():
    assert app.find_browser("linux", {}, Path("/h"), is_file=lambda p: False, which=lambda n: None) is None
    cmd = app.browser_command("http://127.0.0.1:1/?launch=c", exe="/x/chrome", platform="linux", env={}, home=Path("/h"))
    assert cmd[0] == "/x/chrome" and cmd[1] == "--app=http://127.0.0.1:1/?launch=c"


def test_profile_dir_per_platform(tmp_path):
    home = tmp_path / "h"
    assert app.profile_dir("win32", {"LOCALAPPDATA": str(tmp_path / "la")}, home) == tmp_path / "la" / "DevMemory" / "browser"
    assert app.profile_dir("darwin", {}, home) == home / "Library" / "Application Support" / "DevMemory" / "browser"
    assert app.profile_dir("linux", {}, home) == home / ".cache" / "DevMemory" / "browser"
    assert app.profile_dir("linux", {"XDG_CACHE_HOME": str(tmp_path / "c")}, home) == tmp_path / "c" / "DevMemory" / "browser"

# the dry run's launch link: tests/test_ui_launch_code.py::test_app_dry_run_prints_a_launch_link
