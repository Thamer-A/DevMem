"""Claude Code plugin support: what the plugin's launchers run, which projects the plugin records, and how the
plugin route and the `devmem init` route stay out of each other's way.

The plugin is the folder that holds this package (`<plugin>/devmem/`), copied by Claude Code into its plugin
cache. Nothing is installed with pip. Its pieces:

  bin/devmem, bin/devmem.cmd   launchers (POSIX sh / Windows cmd): find a Python 3.10+ and run this package.
                               `devmem <command>` is the ordinary CLI; `devmem --plugin-hook <Event>` is one
                               hook event (HOOK_BOOTSTRAP -> `hook_main`). Both launchers carry the bootstraps
                               below verbatim (a test checks that).
  hooks/hooks.json             one shell-form handler per event, written so that the same text runs under
                               sh/bash (macOS, Linux, Git Bash) and under Windows PowerShell 5.1: see hook_command.
  skills/                      /devmem:setup, :status, :forget, :view (user-invoked) and recall (Claude may use it).

Opt-in per project, recorded OUTSIDE the project: `/devmem:setup` (`devmem setup --plugin-data <dir>`) writes a
marker `${CLAUDE_PLUGIN_DATA}/projects/<key>`, where <key> is a hash of the project's normalised real path
(`project_key`). A repository can ship a `.devmemory` folder, so a store in the project proves nothing about the
user's choice. The launchers keep a cheap pre-filter (no `.devmemory` folder: return before Python starts);
`hook_main` then requires the marker and does nothing at all without it (no record, no pack, nothing written).
CLAUDE_PLUGIN_DATA unset means not enabled. A moved or renamed project has another key and is not enabled until
setup runs again. `devmem setup --off` switches a project off and keeps its store.

One route per session: when `devmem init` handlers are also installed for the project, both handler sets fire for
every event. The route that records a session is decided at its first event (and again at a SessionStart that
starts or resumes it) and kept in `live/<session id>.route` (`devmem.hooks.session_route`): `init` when this user
switched the project on with `devmem init` (devmem.consent, outside the project) and init handlers that can run are
in its settings files, else `plugin`. Both handler sets obey that file for the rest of the session, whatever happens
to the settings files meanwhile; if the init switch is turned off mid-session (`devmem uninstall`), the init hooks
record nothing and the plugin takes the session over.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STORE_DIR = ".devmemory"          # = devmem.store.STORE_DIR (not imported: the hook path stays small)
PLUGIN_NAME = "devmem"             # plugin name; enabledPlugins keys look like "devmem@<marketplace>"
PLUGIN_ROOT_ENV = "DEVMEM_PLUGIN_ROOT"   # set by the launcher for the CLI it runs (shown by `devmem doctor`)
DATA_ENV = "CLAUDE_PLUGIN_DATA"          # exported by Claude Code to plugin hooks (not to the Bash tool)
ENABLED_DIR = "projects"           # ${CLAUDE_PLUGIN_DATA}/projects/<key>: the project is switched on
OFF_SUFFIX = ".off"                # .../projects/<key>.off: switched off with `setup --off` (it was set up here)

# ---------------------------------------------------------------- bootstraps (copied verbatim into the launchers)
# They are passed as `python -c "<text>"` by sh and by cmd, so they hold no double quote, $, backtick, backslash,
# percent sign or exclamation mark. Single quotes are fine.

# Hook event: `python -I -S -c HOOK_BOOTSTRAP <plugin root> <Event>`. -I keeps the user's project (and PYTHON*
# variables) off sys.path; -S skips site-packages, which the stdlib-only core does not need (so optional embeddings
# are not used on the plugin route). Whatever happens, including a missing or broken package, the process ends with
# exit status 0; a failure is appended to the same temp log the `devmem init` bootstrap uses.
# Appending to the temp folder's error log: never through a link (O_NOFOLLOW), never blocking on a FIFO planted there
# (O_NONBLOCK), created 0600, and only to a regular file this user owns (the name is predictable and /tmp is shared on
# Linux and macOS). Flags are added, not or-ed, and the owner test subtracts: the bootstraps may hold none of
# " \ $ ` % ! (cmd.exe and sh quote them). N and G are set in the bootstrap's first line (these lines cannot hold a
# single quote either: the list is written with repr()).
ERROR_LOG_PREFIX = "N=getattr(os,'O_NOFOLLOW',0)+getattr(os,'O_NONBLOCK',0);G=hasattr(os,'getuid');"
ERROR_LOG_LINES = [
    "  import tempfile,time,stat",
    "  p=os.path.join(tempfile.gettempdir(),L)",
    "  d=os.open(p,os.O_WRONLY+os.O_APPEND+os.O_CREAT+N,384)",
    "  s=os.fstat(d)",
    "  if not stat.S_ISREG(s.st_mode) or (G and s.st_uid-os.getuid()):",
    "   os.close(d)",
    "   raise OSError(p)",
    "  f=os.fdopen(d,chr(97))",
    "  f.write(repr((time.ctime(),R,sys.argv[1:2],e))+chr(10))",
    "  f.close()",
]
_HOOK_LINES = [
    "try:",
    " if not os.path.isfile(P):",
    "  raise ImportError(P)",
    " sys.path.insert(0,R)",
    " from devmem.plugin import hook_main",
    " hook_main(sys.argv[1:])",
    "except BaseException as e:",
    " try:",
    *ERROR_LOG_LINES,
    " except BaseException:",
    "  pass",
]
HOOK_BOOTSTRAP = ("import sys,os;R=os.path.abspath(sys.argv.pop(1));P=os.path.join(R,'devmem','plugin.py');"
                  "L='devmem_hook_errors'+('_'+str(os.getuid()) if hasattr(os,'getuid') else '')+'.log';"
                  + ERROR_LOG_PREFIX + "exec(chr(10).join(" + repr(_HOOK_LINES) + "));os._exit(0)")

# CLI: `python -I -c CLI_BOOTSTRAP <plugin root> <args>`; exit status is devmem's.
CLI_BOOTSTRAP = ("import sys,os;R=os.path.abspath(sys.argv.pop(1));"
                 "os.path.isfile(os.path.join(R,'devmem','cli.py')) or sys.exit('devmem: no devmem package in '+R);"
                 "sys.path.insert(0,R);os.environ['" + PLUGIN_ROOT_ENV + "']=R;"
                 "from devmem.cli import main;sys.exit(main())")

for _b in (HOOK_BOOTSTRAP, CLI_BOOTSTRAP):
    assert not any(c in _b for c in '"\\$`%!'), _b


def hook_command(event: str) -> str:
    """The shell-form command of the plugin's handler for `event` (hooks/hooks.json).

    Claude Code runs a shell-form hook with `sh -c` (macOS, Linux), Git Bash (Windows) or, on Windows without
    Git Bash, PowerShell. The text is valid in both languages:
      sh/bash:     `set +eu` first, so `set -e`/`set -u` from a BASH_ENV file cannot end the hook early with a
                   non-zero status. Then `set -- '" <#' --plugin-hook E; shift` (inside double quotes \\" is an
                   escaped quote); `trap - EXIT` and `trap - ERR` drop traps a BASH_ENV file may set (an EXIT or ERR
                   trap running `exit 2` would block the prompt; dash rejects the name ERR, which is harmless after
                   `set +e` and silenced). They sit after the `<#` because `trap` is a PowerShell keyword. Then the
                   sh launcher is sourced, and `exit 0`; the rest of the line is a comment. No
                   command substitution, so no extra fork (about 12 ms under Git Bash). A POSIX shell (dash, bash
                   as sh) aborts with status 2 on a syntax error in a sourced file, so there the launcher is sourced
                   in a subshell; plain bash (Git Bash) carries on after such an error, so it sources directly and
                   saves the subshell (a fork, about 15 ms under Git Bash). Either way a missing or broken launcher
                   cannot make the hook fail.
      PowerShell:  `set +eu` and `set -- "\\"` are Set-Variable calls (a backslash escapes nothing in PowerShell,
                   so the string ends there), then `<# ... #>` is a block comment that hides the sh part, then the
                   cmd launcher inside try/catch (a missing plugin folder must not end the hook with status 1 when
                   $ErrorActionPreference is Stop), then `exit 0`. cmd expands `%NAME%` and drops `^` in the
                   command line PowerShell hands it, so a plugin folder whose path holds % or ^ is not run at all:
                   one line goes to the temp folder's devmem_hook_errors.log instead (fail closed). `&` and spaces
                   are safe: the launcher's path is passed quoted and the launcher quotes every use of it.
    The plugin root comes from the CLAUDE_PLUGIN_ROOT environment variable that Claude Code exports to hooks."""
    return ('set +eu; set -- "\\" <#" --plugin-hook ' + event + '; shift; trap - EXIT; trap - ERR 2>/dev/null; '
            'f="$CLAUDE_PLUGIN_ROOT/bin/devmem"; '
            'if [ -n "${BASH_VERSION-}" ] && ! shopt -oq posix; then . "$f"; else (. "$f"); fi; exit 0 #>; '
            "$r = $env:CLAUDE_PLUGIN_ROOT; if ($r -match '[%^]') { try { Add-Content -LiteralPath "
            "(Join-Path $env:TEMP 'devmem_hook_errors.log') -Value ((Get-Date).ToString() + ' devmem plugin hook "
            + event + ": the plugin folder path holds % or ^, which cmd cannot pass safely; nothing recorded: ' + $r) "
            "} catch {} } else { try { & \"$r\\bin\\devmem.cmd\" --plugin-hook " + event + " } catch {} }; exit 0")


# ---------------------------------------------------------------- which projects are switched on

def usable_data_dir(data_dir) -> str | None:
    """The plugin data folder when it is a real path (not an unsubstituted `${...}` placeholder), else None."""
    d = os.fspath(data_dir) if data_dir else ""
    return d if d and "${" not in d and os.path.isabs(d) else None


def marker_path(data_dir, project) -> Path:
    """`${CLAUDE_PLUGIN_DATA}/projects/<key>` (devmem.consent: the same key as the `devmem init` switch)."""
    from .consent import marker_in
    return marker_in(data_dir, project)


def inside_project(data_dir, project) -> bool:
    """Is the plugin data folder the project or inside it? The project could then ship its own switches, so such a
    folder holds none (as devmem.consent.init_marker refuses a DEVMEM_USER_DATA inside the project)."""
    from .consent import _inside
    return _inside(Path(data_dir), Path(project))


def is_enabled(project, data_dir=None) -> bool:
    """Has the user switched memory on for this project with /devmem:setup? One stat on the hook path (and, when the
    switch is there, a check that the data folder is outside the project)."""
    d = usable_data_dir(data_dir if data_dir is not None else os.environ.get(DATA_ENV))
    return bool(d) and marker_path(d, project).is_file() and not inside_project(d, project)


def was_set_up(project, data_dir) -> bool:
    """Enabled now, or switched off with `setup --off` (so its store holds this user's own recordings)."""
    d = usable_data_dir(data_dir)
    if not d:
        return False
    m = marker_path(d, project)
    return (m.is_file() or m.with_name(m.name + OFF_SUFFIX).is_file()) and not inside_project(d, project)


# ---------------------------------------------------------------- the hook entry point

def _local(p: str) -> Path | None:
    """`p` as a Path, or None for a network path (never stat-ed: on Windows that contacts the host). The prefix test
    keeps devmem.store out of a hook in a project that is not switched on."""
    if p.startswith(("\\\\", "//")):
        from .store import is_remote_path
        if is_remote_path(p):
            return None
    return Path(p)


def _project_from(data: bytes) -> Path | None:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return _local(env)
    try:
        payload = json.loads(data.decode("utf-8", errors="replace")) if data.strip() else {}
    except ValueError:
        return None
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    return _local(cwd) if isinstance(cwd, str) and cwd else None


def _read_payload(stream, limit: int = 32 * 1024 * 1024) -> bytes:
    """The hook payload, a megabyte at a time (read(limit + 1) allocated 32 MB up front on every hook event); past
    `limit` the rest is drained and the cut payload is returned (the JSON parser then rejects it)."""
    if stream is None:
        return b""
    parts, n = [], 0
    while n <= limit:
        chunk = stream.read(min(1 << 20, limit + 1 - n))
        if not chunk:
            break
        parts.append(chunk)
        n += len(chunk)
    if n > limit:
        while stream.read(1 << 20):
            pass
    return b"".join(parts)


def hook_main(argv: list[str]) -> int:
    """One plugin hook event (argv: [Event]). Prints only what `devmem.hooks.run` returns (the context pack)."""
    event = argv[0] if argv else ""
    try:   # at most 32 MB, as devmem.hooks.read_stdin (not imported here: a project not switched on stays cheap)
        data = _read_payload(sys.stdin.buffer if sys.stdin is not None else None)
    except Exception:  # noqa: BLE001
        data = b""
    project = _project_from(data)
    if project is None:
        return 0
    store = project / STORE_DIR
    if not store.is_dir() or not is_enabled(project):
        return 0                       # memory is not switched on for this project: do nothing at all
    from .hooks import _write_stdout, run

    def propose() -> str:              # called once per session (see devmem.hooks.session_route)
        try:
            return "init" if init_hooks_active(project) else "plugin"
        except Exception as exc:  # noqa: BLE001 - when the check fails, record (better twice than not at all)
            _log(store, event, exc)
            return "plugin"

    _, out = run([event], data, route="plugin", propose=propose)
    _write_stdout(out)
    return 0


def _log(store: Path | None, event: str, exc: BaseException) -> None:
    try:
        from .hooks import _log_error
        _log_error(store, f"plugin:{event}", exc)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- one route per session

def init_handlers(project: Path) -> list[dict]:
    """The `devmem init` route as it really records: [] unless this user ran `devmem init` for the project (its
    switch in devmem's per-user folder, devmem.consent.init_enabled), else the handlers in the project's settings
    that can run (devmem.init.runnable_handlers): [{file, events, handlers}]. Nothing in the project, such as a
    committed settings file, counts on its own."""
    from .consent import init_enabled
    if not init_enabled(project):
        return []
    from .init import runnable_handlers
    return runnable_handlers(project)


def init_hooks_active(project: Path) -> bool:
    """True when `devmem init` handlers that can run are installed for this project (init_handlers). Anything else
    (a broken or forged handler) records nothing, so the plugin records instead; `devmem doctor` reports a broken
    handler. Read once per session, when its route is decided."""
    return bool(init_handlers(project))


# ---------------------------------------------------------------- `devmem setup` (/devmem:setup)

def check_data_dir(plugin_data, project=None) -> Path:
    d = usable_data_dir(plugin_data)
    if not d:
        raise ValueError("the plugin's data folder is not known here: run /devmem:setup inside Claude Code (or pass "
                         "--plugin-data with the folder Claude Code gives the plugin as ${CLAUDE_PLUGIN_DATA})")
    if project is not None and inside_project(d, project):
        raise ValueError(f"the plugin data folder {d} is inside the project {project} (the project could then hold "
                         "its own switches); nothing changed")
    if not Path(d).parent.is_dir():   # a path cmd mangled (a % in it) must not create folders somewhere else
        raise ValueError(f"the plugin data folder {d} is not inside an existing folder; nothing changed")
    return Path(d)


def _write_marker(path: Path, project: Path) -> None:
    from .consent import write_marker
    write_marker(path, project)


def setup_project(project: Path, plugin_data=None, scan: bool = True, adopt_store: bool = False,
                  allow_unredacted: bool = False) -> dict:
    """Switch memory on for a project used with the plugin: create the store (with its .gitignore), run the first
    scan and write the project's marker in the plugin data folder. Writes no hooks and nothing in `.claude/`. In a
    project that has `devmem init` hooks it changes nothing and reports them (one route per project). An existing
    store that git tracks, or that holds sessions this plugin did not record here, raises devmem.init.ShippedStore
    unless adopt_store is set; nothing is changed then either. A store with links in it, or whose .gitignore cannot
    be made to keep it out of git, is refused (devmem.init.prepare_store)."""
    from .init import _check_project, prepare_store
    project = _check_project(project)
    data = check_data_dir(plugin_data, project)
    store = project / STORE_DIR
    report: dict = {"project": str(project), "store": str(store), "init_hooks": [], "created": False,
                    "marker": str(marker_path(data, project))}
    init = init_handlers(project)
    if init:
        report["init_hooks"] = [str(e["file"]) for e in init]
        if not allow_unredacted:             # nothing else changes here, but a setup without the flag still
            from .consent import revoke_unredacted_note   # takes an earlier permission back
            revoke_unredacted_note(project)
        return report
    from .consent import check_user_data, note_plugin_setup, set_up_here_before, write_marker
    check_user_data(project)                 # before anything is written: the note there must be writable
    marker = marker_path(data, project)
    probe = marker.parent / f".write-test.{os.getpid()}"
    try:                                     # and the plugin's own switch
        from .store import private_mkdir, write_private
        private_mkdir(marker.parent)
        write_private(probe, b"")
        probe.unlink()
    except OSError as e:
        raise ValueError(f"cannot write this project's switch in the plugin's data folder {marker.parent} "
                         f"({e.strerror or e}); nothing changed") from None
    report["created"] = not store.is_dir()
    prepare_store(project, store, was_set_up(project, data) or set_up_here_before(project), adopt_store)
    try:
        note_plugin_setup(project, allow_unredacted)
        write_marker(marker, project, allow_unredacted)
    except OSError as e:
        raise ValueError(f"cannot write this project's switch ({e.strerror or e}); memory is not switched on") from None
    try:
        marker.with_name(marker.name + OFF_SUFFIX).unlink()
    except OSError:
        pass
    report["allow_unredacted"] = allow_unredacted
    if scan:
        try:
            from .scan import scan as run_scan
            report["scan"] = {"files": len(run_scan(project, store).get("files", {}))}
        except Exception as e:  # noqa: BLE001 - the store exists; report the scan problem
            report["scan"] = {"error": f"{type(e).__name__}: {e}"}
    return report


def disable_project(project: Path, plugin_data=None) -> dict:
    """`devmem setup --off`: the plugin's hooks stop recording this project; its store and recordings stay."""
    project = Path(project).expanduser().resolve()
    data = check_data_dir(plugin_data, project)
    marker = marker_path(data, project)
    was_on = marker.is_file()
    if was_on:
        os.replace(marker, marker.with_name(marker.name + OFF_SUFFIX))
    from .consent import revoke_unredacted_note
    revoke_unredacted_note(project)          # switching off also ends an --allow-unredacted permission
    return {"project": str(project), "was_on": was_on, "store": str(project / STORE_DIR)}


# ---------------------------------------------------------------- route report (devmem doctor / status)

def plugin_enabled(project: Path, config_dir: str | None = None) -> str | None:
    """Where the devmem plugin is enabled for this project, as text, or None. Reads `enabledPlugins` from the
    user, project and local settings (later ones win). A session started with `--plugin-dir` is not visible here."""
    cfg = Path(config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    where = None
    from .store import read_regular
    for scope, f in (("user", cfg / "settings.json"), ("project", Path(project) / ".claude" / "settings.json"),
                     ("local", Path(project) / ".claude" / "settings.local.json")):
        # plain files only, never blocking; the user's own settings may be a link (dotfile managers), the project's not
        raw = read_regular(f, 16 * 1024 * 1024, follow=scope == "user")
        try:
            data = json.loads(raw.decode("utf-8-sig")) if raw is not None else None
        except (UnicodeError, ValueError):
            continue
        plugins = data.get("enabledPlugins") if isinstance(data, dict) else None
        for key, on in (plugins.items() if isinstance(plugins, dict) else []):
            if isinstance(key, str) and key.split("@", 1)[0] == PLUGIN_NAME:
                where = f"{scope} settings ({f})" if on is True else None
    return where


def plugin_state(project: Path, plugin_data=None, config_dir: str | None = None) -> dict:
    """What devmem can tell about the plugin route here: `installed` (text or None: enabledPlugins, or this devmem
    runs from the plugin folder), `enabled` (True/False, or None when the plugin data folder is not known)."""
    where = plugin_enabled(project, config_dir)
    root = os.environ.get(PLUGIN_ROOT_ENV)
    if not where and root:
        where = f"this devmem runs from the plugin folder {root}"
    d = usable_data_dir(plugin_data if plugin_data is not None else os.environ.get(DATA_ENV))
    return {"installed": where, "enabled": is_enabled(project, d) if d else None, "data_dir": d}


def route_rows(project: Path, store: Path, init_state: list[dict], config_dir: str | None = None,
               plugin_data=None) -> list[tuple]:
    """(level, label, detail) rows for `devmem doctor`: which route records this project."""
    from .consent import init_marker
    from .consent import init_state as init_switch
    st = plugin_state(project, plugin_data, config_dir)
    files = ", ".join(str(h["file"]) for h in init_state)
    switch = init_switch(project)
    rows = []
    if init_state and switch != "on":
        rows.append(("warn", "route", f"`devmem init` hooks are installed ({files}) but this project is not switched on "
                     "for this user, so they record nothing: run `devmem init` again (it asks for --adopt-store when it "
                     "cannot tell that the store is yours; an earlier devmem kept no switch outside the project)"))
        init_state = []
    if init_state and st["enabled"]:
        rows.append(("warn", "route", f"mixed setup: `devmem init` hooks in {files}, and this project is also set up "
                     "for the devmem plugin. Each session is recorded once: by the init hooks when they are installed "
                     "at the session's first event, else by the plugin. To keep only the plugin, run `devmem "
                     "uninstall` here; to keep only init, run /devmem:setup with --off (or `devmem setup --off`)"))
    elif init_state:
        rows.append(("ok", "route", f"`devmem init` hooks ({files})"))
    elif st["enabled"]:
        rows.append(("ok", "route", "devmem Claude Code plugin: this project is set up (/devmem:setup), so the "
                     "plugin's hooks record its sessions"))
    elif st["enabled"] is False:
        rows.append(("warn", "route", "this project is not set up for the devmem plugin (or was switched off), so its "
                     "hooks record nothing here and add nothing to prompts: run /devmem:setup"))
    elif st["installed"]:
        rows.append(("warn", "route", f"the devmem plugin is installed ({st['installed']}); whether this project is set "
                     "up for it is known only inside Claude Code: run /devmem:status there"))
    elif not files:
        rows.append(("warn", "hooks", "not installed in .claude/settings.local.json or settings.json (run `devmem init`), "
                     "and the devmem plugin is not enabled in the settings devmem can read"))
    if switch == "unusable":
        try:
            init_marker(project)
            why = ""
        except ValueError as e:
            why = str(e)
        rows.append(("FAIL", "init switch", f"devmem's per-user folder cannot hold switches: {why}"))
    else:
        level = "warn" if files and switch != "on" else "ok"   # hooks installed, switch missing: they record nothing
        rows.append((level, "init switch", f"{switch} ({init_marker(project)}{'' if switch == 'on' else '[.off]'}; "
                     "outside the project: `devmem init` turns it on, `devmem uninstall` off)"))
    if st["enabled"] is not None:
        rows.append(("ok", "plugin", f"enabled for this project: {'yes' if st['enabled'] else 'no'} (marker "
                     f"{marker_path(st['data_dir'], project)})"))
    root = os.environ.get(PLUGIN_ROOT_ENV)
    if root:
        rows.append(("ok", "plugin", f"this devmem runs from the plugin folder {root}"))
        try:
            from .embed import settings
            if settings(Path(store)):
                rows.append(("warn", "embeddings", "on in this project's config, but the plugin's hooks start Python "
                             "without site-packages, so packs on the plugin route use keyword retrieval only"))
        except Exception:  # noqa: BLE001 - optional layer
            pass
    return rows
