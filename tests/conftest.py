"""Shared test setup."""
from __future__ import annotations

import tempfile

import pytest


@pytest.fixture(autouse=True)
def _own_temp_folder(tmp_path_factory, monkeypatch):
    """A hook error that cannot be logged in a store goes to the system temp folder (devmem.hooks.fallback_error_log),
    and `devmem doctor` warns about that log for seven days. Give every test its own temp folder so a test run leaves
    nothing in the real one: TMP/TEMP/TMPDIR for subprocesses, and `tempfile.tempdir` for this process, because
    `tempfile.gettempdir()` keeps its first answer. A test that sets these itself overrides this."""
    tmp = tmp_path_factory.mktemp("systemp")          # made before the patch, under pytest's own base folder
    for name in ("TMP", "TEMP", "TMPDIR"):
        monkeypatch.setenv(name, str(tmp))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))


@pytest.fixture(autouse=True)
def _no_real_claude_config(tmp_path_factory, monkeypatch):
    """`devmem status` / `doctor` read `enabledPlugins` from the user's Claude Code settings (CLAUDE_CONFIG_DIR, else
    ~/.claude). Point every test at an empty folder so results never depend on this machine's configuration."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude-config")))
    # devmem's per-user folder (the `devmem init` switches, the app's recents list): never the real one in a test
    monkeypatch.setenv("DEVMEM_USER_DATA", str(tmp_path_factory.mktemp("devmem-user-data")))
    monkeypatch.delenv("DEVMEM_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)     # which projects the plugin records: per test only
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    # a git that did not answer within its limit makes the process start no git for a while (devmem.store._git_exec):
    # under load that happens in a time-limited check of one test (the files-only pack's refresh) and must not make a
    # later test in the same process run without git
    from devmem import store
    monkeypatch.setattr(store, "_git_down_until", 0.0)


@pytest.fixture(autouse=True)
def _git_memory_per_test():
    """devmem.store keeps, per process, "git did not answer in time" (60 s without git) and the filter names it
    listed. One slow git on a loaded machine must not make the next tests of this process run without git (and refuse
    their stores: devmem.consent), so every test starts without that memory."""
    from devmem import store
    store._git_down_until = 0.0
    store._FILTERS.clear()
    store._SLOW_PATHS.clear()
    yield
