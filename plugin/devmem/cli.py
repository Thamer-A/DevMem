"""devmem command line. Run `devmem --help`, or `devmem <command> --help` for examples.

Imports are kept lazy: `devmem hook` and `devmem --help` should not pay for modules they do not use.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from . import __version__
from . import term
from .store import StoreRefused

SETTINGS_LOCAL = "settings.local.json"
SETTINGS_SHARED = "settings.json"

DESCRIPTION = """\
devmem keeps a local memory of what you and an AI coding assistant did in a project, and gives the next
session a small context pack with only what is relevant to its task. Everything stays on this machine.
"""

EPILOG = """\
getting started:
  devmem init              set the current project up; ordinary Claude Code sessions are then captured
                           (with the devmem Claude Code plugin: /devmem:setup, or `devmem setup`)
  devmem status            what has been captured so far
  devmem pack "fix login"  print the context pack devmem would give a session for that task
  devmem doctor            check the setup if something does not seem to work
  devmem connect cursor    use devmem from another AI tool (run `devmem connect` for the list)

Run `devmem <command> --help` for the options and examples of one command.
"""


class CliError(Exception):
    """A problem the user can fix. Printed as one line; exit status 2."""


# ---------------------------------------------------------------- output

def _safe_streams() -> None:
    """Never crash on a character the console cannot encode (cp1252 on Windows). A real console gets
    '?' for what it cannot show. Piped or redirected stdout is written as UTF-8 so nothing is lost: packs
    quote the user's own words, which may be Arabic or any other script."""
    for s in (sys.stdout, sys.stderr):
        try:
            # typed as a TextIO; a stream without reconfigure (a StringIO, a test double) raises AttributeError below
            text = cast(io.TextIOWrapper, s)
            if s is sys.stdout and not s.isatty():
                text.reconfigure(encoding="utf-8", errors="replace")
            else:
                text.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _say(*parts) -> None:
    term.say(*parts)


def _ranged(lo: int, hi: int):
    """An argparse type: a whole number from `lo` to `hi` (the same range the MCP tools accept)."""
    def parse(text: str) -> int:
        if not text.strip().lstrip("+-").isdigit() or len(text.strip()) > 12:
            raise argparse.ArgumentTypeError(f"not a whole number: {text[:20]!r}")
        n = int(text)
        if not lo <= n <= hi:
            raise argparse.ArgumentTypeError(f"must be from {lo} to {hi}")
        return n
    return parse


def _say_note(note: str | None) -> None:
    """A line on stderr when begin/end left something out (devmem.generic.begin_note / end_note)."""
    if note:
        term.say(note, file=sys.stderr)


# ---------------------------------------------------------------- shared helpers

PLUGIN_ROOT_ENV = "DEVMEM_PLUGIN_ROOT"   # = devmem.plugin.PLUGIN_ROOT_ENV: set by the plugin's launchers


def _local_arg(raw, what: str) -> Path:
    """A path the user gave (an option, an argument, CLAUDE_PROJECT_DIR), refused when it names a network location
    before anything stats, opens or resolves it (on Windows that contacts the host: seconds, and an authentication
    attempt), and in one local spelling (store.local_path: a device or localhost spelling becomes a drive path).
    Applied first wherever a
    user-given path enters the CLI."""
    from .store import is_remote_path, local_path
    if is_remote_path(raw):
        raise CliError(f"{what} {raw} is a network path; devmem works with local folders and files only")
    return Path(local_path(os.path.expanduser(os.fspath(raw))))


def _project(a) -> Path:
    """--project, else (run from the plugin's launcher) CLAUDE_PROJECT_DIR when it is set, else the current folder.
    The plugin's skills pass no project path, so no path goes through a shell or cmd's parser."""
    raw = a.project
    if raw is None:
        env = os.environ.get("CLAUDE_PROJECT_DIR") if os.environ.get(PLUGIN_ROOT_ENV) else None
        raw = env if env and "${" not in env else "."
    p = _local_arg(raw, "project folder")
    if not p.is_dir():
        raise CliError(f"no such project folder: {p}")
    from .store import NOT_A_PROJECT_TEXT, local_path, not_a_project
    why = not_a_project(p)    # every command: the home folder, a root or a folder above home is never a project
    if why:
        raise CliError(f"{p} is {NOT_A_PROJECT_TEXT.get(why, why)}, not a project: run devmem inside a project folder")
    return Path(local_path(p)).resolve()


def _plugin_data(a) -> str | None:
    """The plugin's data folder: --plugin-data (the skills pass ${CLAUDE_PLUGIN_DATA}), else CLAUDE_PLUGIN_DATA."""
    from .plugin import DATA_ENV, usable_data_dir
    d = usable_data_dir(getattr(a, "plugin_data", None) or os.environ.get(DATA_ENV))
    return str(_local_arg(d, "the plugin's data folder")) if d else None


INIT_OFF_HINT = ("hooks are installed but this project is not switched on for this user, so they record nothing: run "
                 "`devmem init` again (it asks for --adopt-store when it cannot tell that the store is yours; an "
                 "earlier devmem kept no switch outside the project)")
RECALL_REFUSED = ("store", "out", "copy")
ALLOW_UNREDACTED_HELP = ("let `\"redact\": false` in the store's config.json switch redaction off for this project "
                         "(recorded with your switch, outside the project; without it a config file, which can arrive "
                         "with a git pull, never turns redaction off). Running the command again without it takes the "
                         "permission back")   # options `sessions`/`show`/`pack` refuse when --plugin-data is given


def _lookup_store(a, project: Path) -> Path | None:
    """The store `sessions`, `show` and `pack` read, or None when memory is not set up for this project. Creates
    nothing. With --plugin-data (the plugin's recall skill, whose commands Claude may run without asking): only a
    project switched on with /devmem:setup, or one this user switched on with `devmem init` whose handlers can run
    (devmem.plugin.init_handlers: both switches live outside the project, which can ship anything), and the
    options that write a file or read another store are refused (CliError); an unusable --plugin-data value (an
    unsubstituted placeholder) counts as "not switched on". Without it: any existing store (CLAUDE_PLUGIN_DATA in
    the environment still requires the switch). Either way a store this user did not create or adopt is refused
    with the reason (CliError, devmem.consent.store_refusal)."""
    given = getattr(a, "plugin_data", None) is not None
    data = _plugin_data(a)
    if given:
        bad = [f"--{o}" for o in RECALL_REFUSED if getattr(a, o, None)]
        if bad:
            raise CliError(f"{', '.join(bad)} cannot be used with --plugin-data (the recall skill reads this "
                           "project's memory only and writes no file)")
    store = _store(a, project)
    if not store.is_dir():
        return None
    if given or data:
        from .plugin import init_handlers, is_enabled
        if not (data and is_enabled(project, data)) and not init_handlers(project):
            return None
    from .consent import store_refusal
    why = store_refusal(project, store, plugin_data=data)   # not this user's store: its sessions are not shown
    if why:
        raise CliError(why)
    return store


def store_policy_rows(project: Path, store: Path) -> list[tuple[str, str, str]]:
    """(level, label, detail) rows about what the store's own files ask for: redaction off (honoured only with the
    user's permission outside the project), capture off, embeddings from a tracked config (ignored), files git
    tracks in the store (a pull can change them), and a settings backup an earlier devmem left next to the file."""
    from .consent import allows_unredacted
    from .init import SETTINGS_FILES, legacy_backup
    from .redact import config_asks_unredacted
    from .store import tracked_in_store
    rows = []
    if config_asks_unredacted(store):
        if allows_unredacted(project):
            rows.append(("warn", "redaction", f"off for this project: {store / 'config.json'} asks for it and you "
                         "allowed it (--allow-unredacted); secrets in prompts and tool output are stored as they are"))
        else:
            rows.append(("warn", "redaction", f"{store / 'config.json'} asks for `\"redact\": false`, but you did not "
                         "allow that outside the project (`devmem init --allow-unredacted` or `devmem setup "
                         "--allow-unredacted`), so redaction stays on"))
    try:
        from .store import read_store_text
        cfg = json.loads(read_store_text(store / "config.json", 1024 * 1024))
        from .sessions import config_bool
        if isinstance(cfg, dict) and not config_bool(cfg.get("capture"), True):
            rows.append(("ok", "capture", f"off in {store / 'config.json'}: nothing is recorded in this project"))
    except (OSError, ValueError):
        pass
    tracked = tracked_in_store(project, store)
    if tracked:
        rows.append(("warn", "tracked", f"{len(tracked)} file(s) in the store are tracked by git ({', '.join(tracked[:3])}"
                     f"{' ...' if len(tracked) > 3 else ''}): a `git pull` can change them and `git add` can publish "
                     "them. `git rm -r --cached .devmemory` stops that (embeddings in a tracked config.json are "
                     "ignored)"))
    for name in SETTINGS_FILES:
        old = legacy_backup(project, name)
        if old.exists():
            rows.append(("warn", "backup", f"{old} was left by an earlier devmem next to the settings file, where git does "
                         "not ignore it: delete it, or move it into .devmemory/init-backups/"))
    return rows


def _approved_patterns(store: Path) -> None:
    """After init/setup: the patterns of the store's `redact` file, which the command just approved (they run on every
    recorded text), so a file a repository shipped or a pull changed is not approved unseen."""
    from .redact import MAX_REDACT_FILE
    from .store import read_regular
    data = read_regular(Path(store) / "redact", MAX_REDACT_FILE)
    if data is None:
        return
    lines = [ln.strip() for ln in data.decode("utf-8", "replace").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return
    _say(f"  redact: you approved the {len(lines)} pattern(s) in {Path(store) / 'redact'} (devmem runs them on every "
         "recorded text; if you did not write them, check them, change the file and run this command again):")
    for ln in lines[:20]:
        _say(f"          {ln[:200]}")
    if len(lines) > 20:
        _say(f"          ... and {len(lines) - 20} more")


def _not_set_up(a, project: Path) -> int:
    """No memory to read. For the recall skill (--plugin-data): one line on stdout, exit 0. Otherwise (a terminal,
    another tool) the one-line error on stderr, exit 2, as before: a script must not take it for a pack."""
    msg = (f"devmem memory is not set up for {project}, so nothing has been recorded to look up "
           "(/devmem:setup with the devmem plugin, or `devmem init`, switches it on; `devmem scan` makes a map only).")
    if getattr(a, "plugin_data", None) is not None:
        _say(msg)
        return 0
    if _store(a, project).is_dir():      # a store, but CLAUDE_PLUGIN_DATA says this project was not switched on
        raise CliError(msg)
    raise CliError(f"no memory store at {_store(a, project)}. Run `devmem init` (capture sessions) or `devmem scan` "
                   "(map only) first.")


def _store(a, project: Path, must_exist: bool = False, create: bool = False) -> Path:
    """The store folder. create=True (commands that write to it) makes it and its .gitignore first."""
    from .store import ensure_store, store_path
    s = _local_arg(a.store, "--store") if getattr(a, "store", None) else store_path(project)
    if getattr(a, "store", None):
        from .store import NotAStoreError, check_store_folder
        try:
            if s.exists() and os.path.samefile(s, project):
                raise CliError(f"--store {a.store} is the project folder itself; give a folder of its own")
            check_store_folder(s)   # never the home folder, a root, or a folder that holds other things
        except NotAStoreError as e:
            raise CliError(str(e)) from None
    if must_exist and not s.is_dir():
        raise CliError(f"no memory store at {s}. Run `devmem init` (capture sessions) or `devmem scan` (map only) first.")
    if create:
        from .store import store_links
        links = store_links(s)
        if links:   # devmem writes some store files in place: a shipped link could point anywhere
            raise CliError(f"the memory store holds symbolic links ({', '.join(links[:3])}); devmem never writes "
                           "through a link. Remove them (or the folder) and run the command again")
        ensure_store(s)
    return s


def _recording_store(a, project: Path, refused_ok: bool = False) -> Path:
    """The store a recording command (run, import, begin, end, wrap, codex) writes, created with its .gitignore. A
    store this user did not create or adopt (devmem.consent.store_refusal) is neither created nor written: CliError,
    or with refused_ok (wrap and codex, which still run the agent, with a pack of project files only) the store as it
    is, untouched."""
    s = _store(a, project)
    from .consent import store_refusal
    why = store_refusal(project, s)
    if why:
        if refused_ok:
            return s
        raise CliError(f"nothing recorded: {why}")
    return _store(a, project, create=True)


def scan_notes(store: Path) -> list[str]:
    """What the last scan left out, from the map's "scan" entry (devmem.scan.scan): files skipped for unsafe names,
    folders too deep to walk, and an incomplete scan (out of time inside a hook or the MCP server)."""
    try:
        from .store import read_store_text
        si = json.loads(read_store_text(store / "index" / "map.json")).get("scan") or {}
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    if si.get("unsafe_names"):
        out.append(f"{si['unsafe_names']} files skipped: their names hold control or bidi characters (rename them to "
                   "index them)")
    if si.get("too_deep"):
        out.append(f"{si['too_deep']} folders nested too deep were not scanned")
    if si.get("incomplete"):
        out.append(f"the last scan ran out of time with {si.get('pending', 0)} files still to read; `devmem scan` "
                   "finishes it")
    return out


def _settings_files(project: Path, only: str | None = None) -> list[Path]:
    d = project / ".claude"
    names = {"local": [SETTINGS_LOCAL], "shared": [SETTINGS_SHARED]}.get(only or "", [SETTINGS_LOCAL, SETTINGS_SHARED])
    return [d / n for n in names]


def hooks_state(project: Path) -> list[dict]:
    """Where devmem's hooks are installed for this project: [{file, events, handlers}] (read-only)."""
    from .init import installed_handlers
    return installed_handlers(project)


def store_ignored(project: Path, store: Path) -> tuple[bool | None, str]:
    """Is the store kept out of git? (True/False/None for unknown, and a short reason)."""
    from .store import find_program, git_run, gitignore_problem
    gi = store / ".gitignore"
    problem = gitignore_problem(store)
    if problem is None:
        return True, f"{gi.name} inside the store ignores everything"
    if gi.is_symlink() or (os.path.lexists(gi) and not gi.is_file()):
        return False, problem      # git reads neither a linked nor a folder .gitignore; do not ask git about rules
    import subprocess
    if not find_program("git", project):
        return None, "git not found; cannot check"
    try:
        r = git_run(project, "check-ignore", "-q", "--", str(store / "x"), timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None, "could not run git"
    if r.returncode == 0:
        return True, "ignored by a .gitignore rule"
    if r.returncode == 1:
        return False, "not ignored by git"
    return None, "not a git repository (or git could not tell)"


def _sessions(store: Path, project: Path) -> list[dict]:
    from .sessions import load_sessions
    return load_sessions(store, project=project)


def _one_line(text, n: int = 70) -> str:
    return " ".join(str(text or "").split())[:n]


def _sync_index(store: Path) -> None:
    """Drop deleted sessions from the search index, when the retrieval layer provides a way to."""
    try:
        from . import pack
        fn = getattr(pack, "sync_session_index", None)
    except Exception:  # noqa: BLE001 - a broken optional layer must not hide the deletion result
        fn = None
    if fn is None:
        return
    try:
        fn(store)
    except Exception as e:  # noqa: BLE001
        term.say(f"warning: sessions were deleted, but the search index could not be updated ({type(e).__name__}: {e}). "
              "Delete .devmemory/index/index.db to rebuild it.", file=sys.stderr)
        return
    _scrub_index(store)


def _scrub_index(store: Path) -> None:
    """After a forget: overwrite the deleted text in the index, its write-ahead log and the vector cache, waiting
    a few seconds at most for other programs that have the database open."""
    ok = True
    try:
        from . import embed
        from .index import Index
        if (store / "index" / "index.db").exists():
            with Index(store) as idx:
                ok = idx.scrub()
        ok = embed.scrub(store) and ok
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        term.say("warning: the sessions were deleted, but their text may still be inside the search index files "
              f"({store / 'index'}) because another program has them open. Close it and run the command again, "
              "or delete the index folder (`devmem scan` rebuilds it).", file=sys.stderr)


# ---------------------------------------------------------------- commands

def _store_not_used_now(project: Path, store: Path) -> bool:
    """After a setup: when the store is refused right now (devmem.consent.store_refusal: git cannot say which of its
    files it tracks, a limit, a shipped store), say so as the hooks would, instead of promising recording. True then."""
    try:
        from .consent import forget_decisions, paused_notice, records_while_unknown, store_refusal
        forget_decisions()
        why = store_refusal(project, store, check_git="cached")
        if not why:
            return False
        _say("  warning: " + paused_notice(project, store, why, records_while_unknown(store, why)))
        return True
    except Exception:  # noqa: BLE001 - only a warning: the setup itself is done
        return False


def cmd_init(a) -> int:
    from .init import init_project
    project = _project(a)
    settings = SETTINGS_SHARED if a.shared else SETTINGS_LOCAL
    try:
        r = init_project(project, store=_local_arg(a.store, "--store") if a.store else None,
                         python=str(_local_arg(a.python, "--python")) if a.python else None,
                         form=a.form, settings_file=settings, scan=not a.no_scan, async_tools=a.async_tools,
                         adopt_store=a.adopt_store, allow_unredacted=a.allow_unredacted)
    except ValueError as e:
        raise CliError(str(e)) from None
    _say(f"devmem is set up for {r['project']}")
    _say(f"  hooks:  {len(r['events'])} Claude Code hook events written to {r['settings']}"
         + ("" if r["settings_changed"] else " (already installed, nothing changed)"))
    _say(f"          events: {', '.join(r['events'])}")
    if r.get("backup"):
        _say(f"          your previous settings file was copied to {r['backup']}")
    gi = {"written": "new .gitignore written", "fixed": "existing .gitignore did not ignore everything: `*` added",
          "kept existing": "existing .gitignore kept"}.get(r["gitignore"], "could not write .gitignore")
    _say(f"  store:  {r['store']}  ({gi}; the store is not committed)")
    warn = store_path_warning(Path(r["store"]))
    if warn:
        _say(f"  warning: {warn}")
    _say(f"  switch: {r['switch']} (outside the project: the hooks record only while it exists; "
         "`devmem uninstall` turns it off)")
    _approved_patterns(Path(r["store"]))
    sc = r.get("scan")
    if sc and "files" in sc:
        _say(f"  scan:   {sc['files']} files indexed")
    elif sc:
        _say(f"  scan:   failed: {sc.get('error')}")
    if a.shared:
        _say("  note:   settings.json is usually committed. The hook command contains absolute paths for this machine "
             "(" + str(r["command"]) + "); teammates would get a broken hook. Prefer the default (settings.local.json).")
    else:
        _say("  note:   settings.local.json is Claude Code's personal, per-project file; it is the right place for a "
             "command with this machine's paths.")
    _say("")
    if not _store_not_used_now(project, Path(r["store"])):
        _say("From the next Claude Code session in this project, prompts, replies, tool calls and file edits are saved "
             "to the store, and relevant earlier work is added to each prompt. Restart a running session to pick the "
             "hooks up.")
    _say("To undo: `devmem uninstall` removes the hooks. `devmem forget --all` deletes what was captured.")
    return 0


SUMMARY_SCAN_CAP = 100_000      # entries of one store folder looked at by `devmem adopt`'s summary


def _names(folder: Path) -> list[str]:
    try:
        with os.scandir(folder) as it:
            out = []
            for e in it:
                out.append(e.name)
                if len(out) >= SUMMARY_SCAN_CAP:
                    break
            return out
    except OSError:
        return []


def _patterns_in(path: Path) -> int | None:
    from .store import read_regular
    data = read_regular(path, 1024 * 1024)
    if data is None:
        return None
    return sum(1 for ln in data.decode("utf-8", "replace").splitlines() if ln.strip() and not ln.lstrip().startswith("#"))


# config.json keys that change what devmem does once the store is adopted, and what each does
CONFIG_EFFECTS = {
    "capture": "false stops recording sessions in this project",
    "deliver": "false stops adding context packs to prompts",
    "pack_budget": "the size of each context pack (tokens)",
    "embeddings": "turns semantic search on: devmem loads a local embedding model (installed separately) with every "
                  "pack and indexes recorded text with it",
    "mcp_call_log": "true makes the MCP server keep a log of every tool call (redacted) in the store",
    "redact": "false would turn redaction off; it counts only with your own --allow-unredacted",
}


def store_summary(project: Path, store: Path) -> list[str]:
    """What `devmem adopt` shows before adopting: the sessions the store holds (counted and dated by their file names,
    which the store's author chose: dates in the future and names devmem did not write are flagged; no record is
    opened), the newest session ids, unfinished sessions, the files git tracks in it (or that git could not say), and
    the store files that change what devmem does (config.json, each key that changes behaviour explained; redact,
    ignore)."""
    from datetime import datetime, timedelta, timezone
    from .store import display_name
    rows = []
    metas = sorted(n for n in _names(store / "sessions") if n.endswith(".meta.json"))
    days, odd, ids = [], 0, []
    later = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y%m%dT%H%M")
    future = 0
    for n in metas:
        try:
            days.append(datetime.strptime(n[:13], "%Y%m%dT%H%M").strftime("%Y-%m-%d"))
            future += n[:13] > later
            ids.append((n[:13], n[14:].split("_", 1)[0].removesuffix(".meta.json")))
        except ValueError:
            odd += 1
    span = f", named for {min(days)} to {max(days)}" if days else ""
    rows.append(f"sessions: {len(metas)} recorded session(s){span} (dates from the file names, not checked)")
    if future:
        rows.append(f"          WARNING: {future} of them are named for a date in the future: devmem did not write "
                    "those names; look at them before you adopt (`devmem show <id>` after adopting, or open the files)")
    if odd:
        rows.append(f"          WARNING: {odd} file name(s) are not in the form devmem writes")
    if ids:
        newest = [display_name(i) for _t, i in sorted(ids)[-5:][::-1]]
        rows.append(f"          newest ids: {', '.join(newest)}")
    live = [n for n in _names(store / "live") if n.endswith(".jsonl")]
    pending = [n for n in _names(store / "pending") if n.endswith(".json")]
    if live or pending:
        rows.append(f"unfinished: {len(live)} live session log(s), {len(pending)} `devmem begin` snapshot(s)")
    from .consent import is_limit_failure, tracked_answer
    got, why = tracked_answer(project, store, cached=False)
    tracked = [t.split(" ", 1)[-1] for t in got or []]
    if got is None and is_limit_failure(store):
        rows.append(f"git:      could not say which files of it git tracks ({why}): a limit of devmem. Adopting uses "
                    "the store anyway for git's index as it is now; until that index changes, devmem cannot see what "
                    "a pull brings into the store. When it changes, devmem asks again")
    elif got is None:
        rows.append(f"git:      could not say which files of it git tracks ({why}); adopting does not change that: "
                    "the store is not used until git (or devmem reading git's index) can say")
    elif tracked:
        rows.append(f"git:      tracks {len(tracked)} file(s) in it ({', '.join(tracked[:3])}"
                    f"{' ...' if len(tracked) > 3 else ''}): a repository put them there. Adopting accepts them as they "
                    "are now; a pull that changes them later makes the store not yours again")
    else:
        rows.append("git:      tracks no file in it")
    from .redact import config_asks_unredacted
    from .store import read_regular
    raw = read_regular(store / "config.json", 1024 * 1024)
    cfg = None
    if raw is not None:
        try:
            cfg = json.loads(raw.decode("utf-8", "replace"))
            keys = ", ".join(sorted(str(k) for k in cfg)[:12]) if isinstance(cfg, dict) else "not an object"
        except ValueError:
            keys = "unreadable"
        rows.append(f"config:   config.json ({keys})")
        if isinstance(cfg, dict):
            for k in sorted(set(cfg) & set(CONFIG_EFFECTS)):
                rows.append(f"          {display_name(k)}: {CONFIG_EFFECTS[k]}")
        if config_asks_unredacted(store):
            rows.append("          it asks to turn redaction off: redaction stays on unless you allow it yourself "
                        "(`devmem init --allow-unredacted` or `devmem setup --allow-unredacted`)")
    n = _patterns_in(store / "redact")
    if n is not None:
        rows.append(f"redact:   {n} redaction pattern(s) of its own: used only after you approve the file with `devmem "
                    "init` or `devmem setup` (adopting does not approve it)")
    n = _patterns_in(store / "ignore")
    if n is not None:
        rows.append(f"ignore:   {n} pattern(s): the files they match are left out of the project map")
    return rows


def cmd_adopt(a) -> int:
    """Mark an existing store as this user's (devmem.consent.claim_store with adopt=True, through the same steps as
    `devmem init --adopt-store`: devmem.init.prepare_store), after showing what it holds. Installs nothing. Asks first;
    when stdin is not a terminal, --yes is needed."""
    from .consent import forget_decisions, store_refusal
    from .store import store_links
    project = _project(a)
    store = _store(a, project)
    if not store.is_dir():
        raise CliError(f"no memory store at {store}: nothing to adopt")
    links = store_links(store)
    if links:
        raise CliError(f"the memory store holds symbolic links ({', '.join(links[:3])}); devmem never uses a store "
                       "like that. Remove them (or the folder)")
    why = store_refusal(project, store)
    _say(f"store:    {store}")
    for row in store_summary(project, store):
        _say(row)
    if why is None:
        _say("This store is already yours: nothing changed.")
        return 0
    _say(f"why it is not used now: {why}")
    from .consent import is_git_limit, is_limit_failure
    if is_git_limit(why) or is_limit_failure(store):      # store_summary asked git just now
        _say("Past this limit devmem cannot see what a pull brings into the store: adopt it only if you trust this "
             "repository and what the store holds now.")
    if not a.yes:
        if not sys.stdin or not sys.stdin.isatty():
            raise CliError("adopting makes devmem use this store's sessions as your own and record there, and stdin is "
                           "not a terminal: add --yes to adopt it")
        try:
            answer = input(term.clean("Adopt this store as yours? Its recorded sessions will be added to your prompts "
                                      "as your own earlier work. Type 'yes' to continue: ")).strip().lower()
        except EOFError:
            raise CliError("no answer could be read from stdin: add --yes to adopt it") from None
        if answer != "yes":
            _say("cancelled; nothing changed")
            return 1
    from .init import prepare_store
    try:
        prepare_store(project, store, True, True)
    except ValueError as e:
        raise CliError(str(e)) from None
    forget_decisions()
    still = store_refusal(project, store)
    if still:
        raise CliError(f"the store was noted as yours, but it is still not used: {still}")
    try:
        from .pack import drop_files_only_map
        drop_files_only_map(project)
    except Exception:  # noqa: BLE001 - a leftover temporary map is removed later anyway
        pass
    _say(f"adopted: {store} is yours now (noted in devmem's per-user folder, outside the project). Its sessions are "
         "used and recording there resumes from the next event; nothing was installed.")
    return 0


def cmd_embed(a) -> int:
    """The optional embeddings: which model is in force and whether it loads from local files; with --download, fetch
    it (the only command that may: devmem.embed). The store's config.json counts only when the store is yours."""
    from . import embed
    from .consent import store_refusal
    project = _project(a)
    store = _store(a, project)
    mine = store.is_dir() and store_refusal(project, store) is None
    if a.model:
        backend, _, model = a.model.partition(":")
        cfg: dict | None = {"backend": backend.strip().lower(), "model": model.strip() or None}
        if mine:     # the cache folder the store's config names, if any
            cfg["cache_dir"] = (embed.settings(store) or {}).get("cache_dir")
    else:
        cfg = embed.settings(store if mine else None)
    if not cfg:
        raise CliError("embeddings are off for this project: give --model fastembed[:<model>] (or model2vec:<model>), "
                       "or turn them on (DEVMEM_EMBED, or `embeddings` in the store's config.json); see the README")
    what = f"{cfg['backend']}:{cfg.get('model') or 'default model'}"
    if not a.download:
        emb = embed.get_embedder(store if mine else None, cfg, project=project)
        _say(f"embeddings: {what}: " + ("loads from local files" if emb else "not usable from local files (the "
             "package is missing, the model is not downloaded, or its folder is not allowed); `devmem embed "
             "--download` fetches it"))
        return 0
    embed.allow_download()
    emb = embed.get_embedder(store if mine else None, cfg, project=project)
    if emb is None:
        raise CliError(f"could not load or download {what}: the backend package is missing, the download failed, or "
                       "a model or cache folder is relative or inside the project (it must be an absolute folder "
                       "outside it)")
    emb.embed(["devmem"], "query")
    _say(f"embeddings: {what} is on this machine now; later commands, the hooks and the MCP server load it from "
         "local files")
    return 0


def cmd_uninstall(a) -> int:
    from .consent import disable_init, init_enabled
    from .init import uninstall
    project = _project(a)
    was_on = init_enabled(project)
    only = "shared" if a.shared else "local" if a.local else None
    any_change = False
    for f in _settings_files(project, only):
        if not f.exists():
            continue
        try:
            r = uninstall(project, settings_file=f.name)
        except ValueError as e:
            raise CliError(str(e)) from None
        if r["settings_changed"]:
            any_change = True
            _say(f"removed devmem hooks from {r['settings']}")
    try:
        disable_init(project)
    except (OSError, ValueError):
        pass
    switched_off = was_on and not init_enabled(project)
    if not any_change:
        _say("no devmem hooks found in .claude/settings.local.json or .claude/settings.json"
             + ("" if switched_off else "; nothing changed"))
    if switched_off:
        _say("this project's `devmem init` switch is off now (kept as off, so setting it up again asks nothing)")
    _say(f"The memory store is kept ({_store(a, project)}). `devmem forget --all` deletes the captured sessions; "
         "deleting the .devmemory folder removes everything.")
    return 0


def cmd_hook(a) -> int:
    from . import hooks
    argv = [a.event or "UserPromptSubmit"]
    if a.store:
        argv += ["--store", a.store]
    return hooks.main(argv)


def cmd_forget(a) -> int:
    from . import sessions as S
    project = _project(a)
    store = _store(a, project, must_exist=True)
    if sum([bool(a.id), bool(a.before), bool(a.all)]) != 1:
        raise CliError("choose one of: a session id prefix, --before DATE, or --all (see `devmem forget --help`)")
    try:
        if a.id:
            result = S.forget_session(store, a.id)
        else:
            existing = _sessions(store, project)
            if a.before:
                try:
                    cutoff = S._parse_when(a.before)
                    n = sum(1 for s in existing if s.get("started_at") and S._parse_when(s["started_at"]) < cutoff)
                except ValueError:
                    raise CliError(f"cannot read the date {a.before!r}; use YYYY-MM-DD or an ISO date-time (UTC)") from None
                what = f"delete {n} of {len(existing)} captured sessions that started before {a.before}"
            else:
                n_files = len(S.forget_all_files(store))
                what = (f"delete all {len(existing)} captured sessions: {n_files} file(s) in {store} (records, raw "
                        "records, live logs, `devmem begin` snapshots, the MCP call log and the pack log)")
                from .consent import store_refusal
                if n_files and store_refusal(project, store):
                    what += ("; its sessions are not counted because the store is not known to be yours (`devmem "
                             "status` says why)")
            if not a.yes:
                if not sys.stdin or not sys.stdin.isatty():
                    raise CliError(f"this would {what}, and stdin is not a terminal: add --yes to confirm")
                try:
                    answer = input(term.clean(f"This will {what}. Type 'yes' to continue: ")).strip().lower()
                except EOFError:  # e.g. NUL on Windows reports isatty() == True
                    raise CliError("no answer could be read from stdin: add --yes to confirm") from None
                if answer != "yes":
                    _say("cancelled; nothing deleted")
                    return 1
            result = S.forget_before(store, a.before) if a.before else S.forget_all(store)
    except ValueError as e:
        raise CliError(str(e)) from None
    _sync_index(store)
    _say(f"removed {len(result['sessions'])} session(s) and {len(result['removed'])} file(s)")
    for sid in result["sessions"]:
        _say(f"  session {sid}")
    for p in result["removed"]:
        _say(f"  deleted {p}")
    pl = result.get("pack_log") or {}
    if pl.get("deleted"):
        _say("  deleted the pack log (packs.jsonl): it could not be read whole")
    elif pl.get("lines_removed") or pl.get("lines_changed"):
        _say(f"  pack log (packs.jsonl): {pl.get('lines_removed', 0)} line(s) removed, "
             f"{pl.get('lines_changed', 0)} line(s) lost items of the forgotten session(s)")
    if not result["removed"]:
        _say("  (nothing matched)")
    return 0


def cmd_packs(a) -> int:
    """The pack log: one row per prompt of a recorded session, what its pack carried or why it had none."""
    from . import sessions as S
    project = _project(a)
    store = _lookup_store(a, project)
    if store is None:
        return _not_set_up(a, project)
    info: dict = {}
    rows = [e for e in S.read_events(store / S.PACK_LOG, info) if isinstance(e, dict)]
    if a.session:
        rows = [e for e in rows if str(e.get("session_id") or "").startswith(a.session)]
    if a.json:
        for e in rows[-a.limit:] if a.limit else rows:
            term.raw(json.dumps(e, ensure_ascii=False) + "\n")
        return 0
    if not rows:
        _say("no prompts in the pack log yet" + (f" for session {a.session}" if a.session else "")
             + " (it is written by the hooks of a recorded session, one line per prompt)")
        return 0
    served = [e for e in rows if e.get("keys")]
    tokens = sum(e["tokens_est"] for e in served if isinstance(e.get("tokens_est"), int))
    _say(f"{len(rows)} prompt(s), {len(served)} with a pack ({100 * len(served) // len(rows)}%), "
         f"about {tokens} tokens added in all")
    shown = rows[-a.limit:] if a.limit else rows
    if len(shown) < len(rows):
        _say(f"the last {len(shown)} (--limit 0 for all):")
    for e in shown:
        when = str(e.get("ts") or "")[:19].replace("T", " ")
        turn = e.get("turn")
        head = f"{when}  {str(e.get('session_id') or '?')[:8]}  turn {turn + 1 if isinstance(turn, int) else '?'}"
        keys = [str(k) for k in e.get("keys") or [] if isinstance(k, str)] if isinstance(e.get("keys"), list) else []
        if keys:
            line = f"{head}  ~{e.get('tokens_est', '?')} tokens, {len(keys)} item(s): {', '.join(keys)}"
            if e.get("cut"):
                line += " (cut at the hook output limit)"
        else:
            line = f"{head}  empty: {e.get('empty_reason') or 'no items'}"
        if e.get("forgotten_items"):
            line += f" [{e['forgotten_items']} item(s) of a forgotten session removed]"
        _say(line)
    if not info.get("complete", True):
        _say("(the log is longer than devmem reads at once; the rows above are its beginning)")
    return 0


def pending_begins(store: Path) -> int:
    """How many `devmem begin` snapshots wait in the store (counted up to 100,000)."""
    n = 0
    try:
        with os.scandir(Path(store) / "pending") as it:
            for e in it:
                n += e.name.endswith(".json")
                if n >= 100_000:
                    break
    except OSError:
        return 0
    return n


def cmd_status(a) -> int:
    from .pack import freshness_statuses
    project = _project(a)
    store = _store(a, project, must_exist=True)
    from .consent import store_refusal
    refused = store_refusal(project, store)   # not this user's store: nothing in it is shown as theirs or changed
    try:   # begin files written by older versions hold raw file text: rewrite them redacted; never fail status
        if not refused:
            from .generic import secure_pending
            secure_pending(store)
    except Exception:  # noqa: BLE001
        pass
    sessions = [] if refused else _sessions(store, project)
    state = hooks_state(project)
    ignored, why = store_ignored(project, store)
    _say(f"project: {project}")
    _say(f"store:   {store}  ({'git-ignored' if ignored else 'NOT git-ignored' if ignored is False else 'git status unknown'}: {why})")
    for level, label, detail in store_policy_rows(project, store):
        if level != "ok" or label == "capture":
            _say(f"{label + ':':<8} {detail}")
    for note in scan_notes(store):
        _say(f"map:     {note}")
    from .generic import MAX_CLAIM_FILES
    begins = pending_begins(store)
    if begins > MAX_CLAIM_FILES:    # abandoned begins piling up: `end` without --id looks at the newest ones only
        _say(f"begin:   {begins} unfinished `devmem begin` snapshots in {store / 'pending'}; `devmem end` without --id "
             f"looks at the newest {MAX_CLAIM_FILES} only. Delete the files of begins you will not end")
    from .sessions import LOAD_PROBLEMS
    if LOAD_PROBLEMS:
        _say(f"records: {len(LOAD_PROBLEMS)} unreadable session file(s) skipped (first: {LOAD_PROBLEMS[0]}); "
             "`devmem doctor` lists them")
    from .hooks import unrecorded_left
    left = unrecorded_left(store)      # counted in the per-user folder; sessions that have not recorded since
    if left:
        kinds: dict[str, int] = {}
        for x in left:
            for k, v in x["kinds"].items():
                kinds[k] = kinds.get(k, 0) + v
        _say(f"hooks:   {'at least ' if any(x.get('at_least') for x in left) else ''}"
             f"{sum(x['count'] for x in left)} hook event(s) of {len(left)} session(s) were not recorded "
             f"({', '.join(f'{k}: {v}' for k, v in sorted(kinds.items()))}), {min(x['first'] for x in left)} to "
             f"{max(x['last'] for x in left)}; a session that records again gets its count in its record")
    if state:
        from .consent import init_state
        for h in state:
            _say(f"hooks:   installed in {h['file']} ({len(h['events'])} events)")
            if any(_handler_paths(x).get("shape") == "script" for x in h["handlers"][:1]):
                _say("         old hook form: if the devmem folder is moved or deleted Claude Code will reject prompts; "
                     "`devmem init` upgrades it")
        if init_state(project) != "on":
            _say(f"         {INIT_OFF_HINT}")
    else:
        from .plugin import plugin_state
        ps = plugin_state(project, _plugin_data(a))
        if ps["enabled"]:
            _say("hooks:   from the devmem Claude Code plugin; this project is set up for it (/devmem:setup)")
        elif ps["enabled"] is False:
            _say("hooks:   devmem Claude Code plugin, but this project is not set up for it (or was switched off): "
                 "nothing is recorded here. Run /devmem:setup")
        elif ps["installed"]:
            _say(f"hooks:   from the devmem Claude Code plugin ({ps['installed']}) if this project is set up for it: "
                 "/devmem:status in Claude Code tells")
        else:
            _say("hooks:   not installed (run `devmem init` to capture ordinary Claude Code sessions, or use the devmem "
                 "Claude Code plugin)")
    if refused:     # not this user's store: its recorded requests are not counted, shown or served anywhere
        _say(f"sessions: not used: {refused}")
        return 0
    live = sum(1 for s in sessions if s.get("in_progress"))
    _say(f"{len(sessions)} captured session(s)" + (f", {live} in progress" if live else ""))
    big = sum(1 for s in sessions if isinstance(s.get("git"), dict) and s["git"].get("status_too_large"))
    if big:
        from .sessions import MAX_STATUS_ENTRIES
        _say(f"git:     {big} session(s) recorded without git's changed files: `git status` listed more than "
             f"{MAX_STATUS_ENTRIES:,} changed paths (or more output than devmem reads)")
    for s, fr in zip(sessions, freshness_statuses(sessions, project)):
        tag = " (in progress)" if s.get("in_progress") else ""
        nr = s.get("not_recorded")
        if isinstance(nr, dict) and isinstance(nr.get("events"), int):
            tag += f" ({'at least ' if nr.get('at_least') is True else ''}{nr['events']} hook event(s) not recorded)"
        _say(f"  {str(s.get('started_at') or '')[:16]}  {str(s.get('session_id'))[:8]}  {len(s.get('turns') or [])} turn(s)  "
             f"{fr:<15} {_one_line(s.get('prompt'))!r}{tag}")
    return 0


def cmd_scan(a) -> int:
    from .scan import scan
    project = _project(a)
    store = _store(a, project)
    refused = None
    if store.is_dir():           # a store this user did not create or adopt is not written: the map goes elsewhere
        from .consent import store_refusal
        from .store import store_links
        # a store that holds links is refused as every writing command refuses it (_store, below)
        refused = None if store_links(store) else store_refusal(project, store)
    if refused:
        from .pack import files_only_map
        try:
            target = files_only_map(project, store)
        except (OSError, ValueError) as e:
            raise CliError(f"nothing scanned: the memory store is not used ({refused}), and devmem's per-user folder "
                           f"cannot hold a map for this project ({e})") from None
    else:
        target = _store(a, project, create=True)
    d: dict = {}
    m = scan(project, target, stats=d)
    _say(f"{len(m['files'])} files in the map ({d.get('added', 0)} added, {d.get('changed', 0)} changed, "
         f"{d.get('removed', 0)} removed, {d.get('unchanged', 0)} unchanged)"
         + ("; map already current" if d.get("fast_path") else "") + ". No model tokens used.")
    if refused:
        _say(f"map written to {target / 'PROJECT.md'}, in devmem's per-user folder: the project's memory store "
             f"{store} is not used and was not changed. {refused}")
    else:
        _say(f"map written to {target / 'PROJECT.md'}")
    return 0


def cmd_sessions(a) -> int:
    from .mcp_server import list_sessions_text
    project = _project(a)
    store = _lookup_store(a, project)
    if store is None:
        return _not_set_up(a, project)
    _say(list_sessions_text(store, project))
    return 0


def cmd_show(a) -> int:
    from .mcp_server import read_session_text
    project = _project(a)
    store = _lookup_store(a, project)
    if store is None:
        return _not_set_up(a, project)
    try:
        _say(read_session_text(store, project, a.id))
    except ValueError as e:
        raise CliError(str(e)) from None
    return 0


def cmd_pack(a) -> int:
    from .pack import build_pack
    project = _project(a)
    out = _local_arg(a.out, "--out") if a.out else None
    store = _lookup_store(a, project)
    if store is None:
        return _not_set_up(a, project)
    text, info = build_pack(project, store, a.task, budget=a.budget, mode=a.mode)
    if out is not None:
        _write_output(out, text, project)
    if not a.copy:
        term.raw(text)                    # a pack: devmem.pack escapes what it quotes
    term.raw(json.dumps({k: v for k, v in info.items() if k != "files"}, default=str) + "\n", file=sys.stderr)
    if a.copy:
        if not text:
            _say(f"nothing copied: the pack is empty ({info.get('empty_reason') or 'nothing relevant found'}); "
                 "the clipboard was not changed")
            return 0
        from .clipboard import ClipboardError, copy
        try:
            via = copy(text)
        except ClipboardError as e:
            raise CliError(f"could not copy the pack: {e}") from None
        _say(f"copied the pack ({len(text)} characters, about {info.get('tokens_est', '?')} tokens) to the clipboard "
             f"with {via}; paste it above your request")
    return 0


def cmd_run(a) -> int:
    from .capture import import_note, run_and_capture
    from .scan import scan
    project = _project(a)
    store = _recording_store(a, project)
    scan(project, store)
    info: dict = {}
    try:
        meta = run_and_capture(project, a.prompt, model=a.model, max_turns=a.max_turns, isolated=False, store=store,
                               info=info)
    except KeyboardInterrupt:       # a second Ctrl-C (the first one stops the program and its run is recorded)
        _say_note("devmem: interrupted again while recording: nothing was recorded")
        return 130
    _say(f"captured -> {meta}")
    _say_note(import_note(info))
    return 130 if (info.get("stream_left_out") or {}).get("interrupted") else 0


def cmd_import(a) -> int:
    from .capture import import_note, record_session
    project = _project(a)
    stream = _local_arg(a.stream, "the stream file")
    if not stream.is_file():
        raise CliError(f"no such file: {a.stream}")
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    info: dict = {}
    _say(record_session(project, stream, a.prompt, started, store=_recording_store(a, project), info=info))
    _say_note(import_note(info))
    return 0


def cmd_mcp(a) -> int:
    from .mcp_server import serve
    project = _project(a)
    store = _store(a, project)
    if not a.require_store and not os.path.lexists(store):
        store = _store(a, project, create=True)   # an existing store is left as it is: each call asks store_refusal first
    return serve(project, store, read_only=a.read_only, require_store=a.require_store)


def cmd_setup(a) -> int:
    from .plugin import disable_project, setup_project
    project = _project(a)
    try:
        if a.off:
            r = disable_project(project, plugin_data=_plugin_data(a))
            _say(f"devmem memory is {'now off' if r['was_on'] else 'already off'} for {r['project']}: the plugin's hooks "
                 "record nothing there and add nothing to prompts. The store and what it holds are kept "
                 f"({r['store']}); /devmem:setup switches it on again, `devmem forget --all` deletes the recordings.")
            return 0
        r = setup_project(project, plugin_data=_plugin_data(a), scan=not a.no_scan, adopt_store=a.adopt_store,
                          allow_unredacted=a.allow_unredacted)
    except ValueError as e:
        raise CliError(str(e)) from None
    if r["init_hooks"]:
        _say(f"{r['project']} already records sessions through `devmem init` hooks ({', '.join(r['init_hooks'])}).")
        _say("Nothing changed. The plugin's hooks stay quiet in this project, so each session is recorded once. To use "
             "the plugin instead, run `devmem uninstall` here, then /devmem:setup again.")
        return 0
    _say(f"devmem memory is on for {r['project']}")
    _say(f"  store:  {r['store']}  ({'created' if r['created'] else 'already there'}; its .gitignore keeps it out of git)")
    sc = r.get("scan") or {}
    if "files" in sc:
        _say(f"  scan:   {sc['files']} files indexed")
    elif sc:
        _say(f"  scan:   failed: {sc.get('error')}")
    _say(f"  switch: {r['marker']} (in the plugin's data folder, outside the project)")
    _approved_patterns(Path(r["store"]))
    if _store_not_used_now(project, Path(r["store"])):
        _say("No hooks were written: the devmem plugin's hooks are switched on for this project, and record and add "
             "earlier work as the warning above says. `devmem setup --off` (or /devmem:setup off) switches them off "
             "and keeps the store; `devmem forget --all` deletes what was recorded. If the project folder is moved or "
             "renamed, run /devmem:setup again there.")
        return 0
    _say("No hooks were written: the devmem plugin's hooks record from their next event (the rest of this turn included: "
         "prompts, replies, tool calls, edits) and add relevant earlier work to prompts. `devmem setup --off` (or "
         "/devmem:setup off) switches recording off and keeps the store; `devmem forget --all` deletes what was "
         "recorded. If the project folder is moved or renamed, run /devmem:setup again there.")
    return 0


def _print_connect_report(r: dict, dry: bool) -> None:
    for c in r["changes"]:
        if not c["changed"]:
            _say(f"  {c['file']}: {c['verb']}")
            continue
        _say(f"  {'would ' if dry else ''}{c['verb']} {c['file']}")
        if c.get("backup"):
            _say(f"    {'would copy' if dry else 'copied'} the previous file to {c['backup']} first")
        if dry and c.get("preview"):
            _say("    ----- " + ("content" if c["verb"] in ("create",) or c["rel"].endswith(".json") else "block") + " -----")
            for line in c["preview"].rstrip("\n").split("\n"):
                _say("    " + line)
            _say("    -----")
    for title, text in r["printed"]:
        _say(f"\n{title}:")
        _say(text.rstrip("\n"))
    for n in r["notes"]:
        _say(f"\nnote: {n}")


def cmd_connect(a) -> int:
    from . import connect as C
    if a.list or not a.tool:
        _say("devmem connect <tool> sets devmem up for an AI tool in this project (project-level files only):")
        for row in C.describe_tools():
            _say(row)
        _say("\nClaude Code: use the devmem Claude Code plugin (/devmem:setup in the project) or `devmem init` instead; "
             "hooks record more than MCP can.")
        _say("Add --print to see exactly what would be written, without writing anything.")
        return 0
    project = _project(a)
    try:
        r = C.connect(project, a.tool, command=a.command, store=_local_arg(a.store, "--store") if a.store else None,
                      dry_run=a.print, force=a.force, adopt_store=a.adopt_store)
    except C.ConnectError as e:
        raise CliError(str(e)) from None
    _say(f"{'devmem connect --print (nothing written)' if a.print else 'connected'}: {r['label']} in {project}")
    _print_connect_report(r, a.print)
    if not a.print:
        _say("\nRestart the tool (or reload its MCP servers) to pick this up. To undo: "
             f"devmem disconnect {r['tool']}")
    return 0


def cmd_disconnect(a) -> int:
    from . import connect as C
    project = _project(a)
    try:
        r = C.disconnect(project, a.tool, dry_run=a.print, force=a.force)
    except C.ConnectError as e:
        raise CliError(str(e)) from None
    _say(f"{'devmem disconnect --print (nothing written)' if a.print else 'disconnected'}: {r['label']} in {project}")
    _print_connect_report(r, a.print)
    if not r["changes"] and not r["notes"]:
        _say("  nothing to remove")
    _say("The memory store is kept. `devmem forget --all` deletes the captured sessions.")
    return 0


def cmd_codex(a) -> int:
    from . import generic
    project = _project(a)
    info: dict = {}
    meta = generic.codex(project, a.prompt, model=a.model, store=_recording_store(a, project, refused_ok=True),
                         with_pack=not (a.no_pack or a.mcp), mcp=a.mcp, isolated=a.isolated, info=info)
    _say(f"captured -> {meta}" if meta else "not recorded (see above)")
    _say_note(generic.end_note(info))
    return 0


def cmd_wrap(a) -> int:
    from . import generic
    project = _project(a)
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not cmd:
        raise CliError("give the command to run after `--`, e.g. devmem wrap --prompt \"...\" -- aider --message {prompt}")
    info: dict = {}
    meta = generic.wrap(project, a.prompt, cmd, provider=a.provider, store=_recording_store(a, project, refused_ok=True),
                        with_pack=not a.no_pack, info=info)
    _say(f"captured -> {meta}" if meta else "not recorded (see above)")
    _say_note(generic.end_note(info))
    return 0


def cmd_begin(a) -> int:
    from . import generic
    project = _project(a)
    info: dict = {}
    p = generic.begin(project, _recording_store(a, project), info=info)
    _say(f"recording started; pending id: {p.stem}")
    _say_note(generic.begin_note(info))
    _say(f'When the work is done: devmem end --id {p.stem} --prompt "<what you asked>"')
    return 0


def cmd_end(a) -> int:
    from . import generic
    project = _project(a)
    info: dict = {}
    try:
        meta = generic.end(project, a.prompt, provider=a.provider, store=_recording_store(a, project), summary=a.summary,
                           summary_source="user-provided summary" if a.summary else None, pending_id=a.id, info=info)
    except RuntimeError as e:
        raise CliError(str(e)) from None
    _say(f"captured -> {meta}")
    _say_note(generic.end_note(info))
    return 0


def cmd_ui(a) -> int:
    from .ui_server import serve
    project = _project(a)
    serve(project, _store(a, project), port=a.port, open_browser=not a.no_browser)
    return 0


def cmd_app(a) -> int:
    from .app import run_app
    # the same guard as every other command (_project): never the home folder, a root or a folder above home
    project = _project(a) if a.project else None
    store = (_store(a, project) if project is not None else _local_arg(a.store, "--store")) if a.store else None
    return run_app(project, store, dry_run=a.dry_run, port=a.port)


def _handler_paths(h) -> dict:
    """devmem.init.handler_paths when available, else the old script form read by hand."""
    try:
        from .init import handler_paths
        return handler_paths(h)
    except ImportError:
        args = [str(x) for x in h.get("args") or []]
        return {"shape": "script", "python": h.get("command"), "hooks_py": args[0] if args else None, "event": None}


class _Refused(Exception):
    pass


def _store_unwritable(store: Path) -> str | None:
    """None if the current user can create a file in the store, else the reason. Writes (and removes) one
    empty temporary file: permission bits alone do not tell on Windows or on network folders."""
    import os
    import tempfile
    try:
        fd, name = tempfile.mkstemp(prefix=".doctor-", dir=str(store))
        os.close(fd)
        os.unlink(name)
    except OSError as e:
        return f"{type(e).__name__}: {e.strerror or e}"
    return None


def cmd_doctor(a) -> int:
    """Self-check; it changes nothing (the one temporary file it writes into the store is removed at once; none in a
    store that is not this user's). Exit 1 only when something is broken (warnings do not fail it)."""
    import os
    import sqlite3
    project = _project(a)
    store = _store(a, project)
    rows: list[tuple[str, str, str]] = []

    def add(level: str, label: str, detail: str) -> None:
        rows.append((level, label, detail))

    v = sys.version_info
    add("ok" if v >= (3, 10) else "FAIL", "python", f"{v.major}.{v.minor}.{v.micro} at {sys.executable}"
        + ("" if v >= (3, 10) else " (devmem needs 3.10 or newer)"))
    add("ok", "devmem", f"{__version__} from {Path(__file__).resolve().parent}")
    add("ok", "project", str(project))
    if store.is_dir():
        from .consent import store_refusal
        from .store import store_dir_links
        refused = store_refusal(project, store)
        problem = _store_unwritable(store) if not (store_dir_links(store) or refused) else None
        if store_dir_links(store):   # the write test would create a file wherever the link points
            add("warn", "store", f"{store} not tested for writing: it is or holds a symbolic link (see links below)")
        elif refused:                # nothing is written into a store that is not this user's, not even a test file
            add("warn", "store", f"{store} not used, not tested for writing: {refused}")
        elif problem:
            add("FAIL", "store", f"{store} cannot be written to by this user ({problem}): hooks, MCP and `devmem scan` "
                "will record nothing there. Fix the folder's permissions or use --store with another folder")
        else:
            add("ok", "store", f"{store} (writable)")
        from .store import loose_permissions
        loose = loose_permissions(store)
        if loose:
            add("warn", "permissions", f"{store}: {loose}. Prompts and code are kept there; devmem narrows it to this user "
                "the next time it writes the store, or run `chmod -R go-rwx` on it")
        mp, db = store / "index" / "map.json", store / "index" / "index.db"
        try:
            from .store import read_store_text
            n = len(json.loads(read_store_text(mp)).get("files", {}))
            add("ok", "map", f"{mp} readable, {n} files")
            for note in scan_notes(store):
                add("warn", "map", note)
        except (OSError, ValueError):
            add("warn", "map", f"{mp} missing or unreadable: run `devmem scan`")
        if db.is_file():
            try:
                con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True)
                try:
                    con.execute("select count(*) from sqlite_master").fetchone()
                finally:
                    con.close()
                add("ok", "index", f"{db} readable")
            except sqlite3.Error as e:
                add("FAIL", "index", f"{db} unreadable ({e}): delete it, `devmem scan` rebuilds it")
        else:
            add("warn", "index", f"{db} not there yet: run `devmem scan`")
        try:
            if refused:
                raise _Refused
            ss = _sessions(store, project)
            live = sum(1 for s in ss if s.get("in_progress"))
            add("ok", "sessions", f"{len(ss)} loaded" + (f", {live} in progress" if live else ""))
            from .sessions import LOAD_PROBLEMS
            for problem in LOAD_PROBLEMS[:10]:
                add("warn", "sessions", f"skipped {problem} (malformed; the other sessions are unaffected)")
        except _Refused:
            add("warn", "sessions", "not loaded: the store is not yours (see store above)")
        except Exception as e:  # noqa: BLE001
            add("FAIL", "sessions", f"could not load: {type(e).__name__}: {e}")
        log = store / "hook_errors.log"
        from .store import read_regular
        log_bytes = read_regular(log, 16 * 1024 * 1024) or b""
        if log_bytes.strip():
            last = [x for x in log_bytes.decode("utf-8", "replace").splitlines() if x.strip()][-1]
            add("warn", "hook errors", f"{log} is not empty; last line: {_one_line(last, 100)}")
        from .redact import user_patterns_state
        for problem in user_patterns_state(store, project)[1]:
            add("warn", "redact", problem)
        from .sessions import torn_lines
        live_dir = store / "live"
        for lg in sorted(live_dir.glob("*.jsonl"))[:200] if live_dir.is_dir() and not refused else []:
            n = torn_lines(lg)
            if n:
                add("warn", "event log", f"{lg} has {n} torn or unparseable line(s) (a write cut short, for example by "
                    "a full disk); those events are skipped, the others are kept")
        ig, why = store_ignored(project, store)
        add("ok" if ig else "warn", "git", ("store is git-ignored: " if ig else "store may be committed: " if ig is False
                                            else "unknown: ") + why)
        from .store import store_links
        links = store_links(store)
        if links:
            add("FAIL", "links", f"the store holds symbolic links ({', '.join(links[:3])}): devmem refuses to write "
                "there. Remove them")
        for row in store_policy_rows(project, store):
            add(*row)
    else:
        add("warn", "store", f"{store} does not exist: run `devmem init` or `devmem scan`")
    try:
        from .consent import user_data_dir
        from .store import loose_permissions
        ud = user_data_dir()
        loose = loose_permissions(ud) if ud.is_dir() else None
        if loose:
            add("warn", "permissions", f"devmem's per-user folder {ud}: {loose}. It lists your projects; devmem narrows it "
                "to this user the next time it writes there, or run `chmod -R go-rwx` on it")
    except (OSError, ValueError):
        pass
    state = hooks_state(project)
    from .plugin import route_rows
    for row in route_rows(project, store, state, plugin_data=_plugin_data(a)):   # init, the plugin, both or neither
        add(*row)
    for h in state:
        add("ok", "hooks", f"{len(h['events'])} events in {h['file']}")
        for hd in h["handlers"][:1]:
            if hd.get("args") is not None:  # exec form: `command` is the interpreter path
                info = _handler_paths(hd)
                py = str(info.get("python") or hd.get("command"))
                add("ok" if os.path.exists(py) else "FAIL", "hook python",
                    py if os.path.exists(py) else f"{py} does not exist, so the capture hook cannot run: Claude Code "
                    "sessions in this project may record nothing or show hook errors. `devmem init` repairs it "
                    "(run it with the Python you use now)")
                script = info.get("hooks_py")
                if info.get("shape") == "bootstrap":
                    if script and not os.path.exists(script):
                        add("FAIL", "hook script", f"{script} does not exist: Claude Code sessions in this project are "
                            "not being recorded (prompts are not blocked). `devmem init` repairs it")
                elif info.get("shape") == "script":
                    add("warn", "hook form", "old hook form (runs the script path directly): if the devmem folder is ever "
                        "moved or deleted, Claude Code will reject every prompt in this project. `devmem init` upgrades it")
                    if script and not os.path.exists(script):
                        add("FAIL", "hook script", f"{script} does not exist, so Claude Code sessions in this project may "
                            "reject prompts or record nothing. `devmem init` repairs it")
            else:
                add("ok", "hook command", _one_line(hd.get("command"), 100) + " (shell form, not checked)")
    try:
        from .connect import connected_tools
        for t in connected_tools(project):
            if t["command"] is None:
                add("ok", "connected", f"{t['label']} (instruction block in {t['file']}; its MCP entry is in a config "
                    "devmem does not read)")
            elif t["problem"]:
                add("FAIL", "connected", f"{t['label']}: the devmemory MCP entry in {t['file']} cannot start: "
                    f"{t['problem']}. `devmem connect {t['tool']}` rewrites it")
            else:
                add("ok", "connected", f"{t['label']}: devmemory MCP entry in {t['file']} ({_one_line(t['command'], 80)})")
    except Exception as e:  # noqa: BLE001 - a broken connect check must not hide the rest of the report
        add("warn", "connected", f"could not check other tools: {type(e).__name__}: {e}")
    try:
        import time
        from .hooks import fallback_error_log
        flog = fallback_error_log()
        if flog.is_file() and flog.stat().st_size and time.time() - flog.stat().st_mtime < 7 * 86400:
            from .store import read_regular
            raw = read_regular(flog, 16 * 1024 * 1024) or b""   # in the temp folder: a plain file only, never a FIFO
            heads = [ln for ln in raw.decode("utf-8", "replace").splitlines() if ln.startswith("--- ")]
            mine = os.path.normcase(os.path.abspath(str(store)))
            stores = [ln.split(" store=", 1)[1].strip() for ln in heads if " store=" in ln]
            if any(os.path.normcase(os.path.abspath(x)) == mine for x in stores):
                add("warn", "hook errors", f"{flog} holds a recent failure for this project's store ({store}): the hook "
                    "could not log there or import devmem")
            elif len(stores) < len(heads):
                add("warn", "hook errors", f"{flog} was written in the last 7 days by a hook failure that does not say "
                    "which project it belongs to (this project may not be the one affected)")
    except Exception:  # noqa: BLE001 - the helper may not exist in an older install
        pass
    try:
        from .claude_cli import claude_executable
        add("ok", "claude", claude_executable())
    except FileNotFoundError:
        add("warn", "claude", "not found on PATH (only needed for `devmem run`; capture through hooks does not use it)")
    width = max(len(r[1]) for r in rows)
    for level, label, detail in rows:
        _say(f"[{level:^4}] {label:<{width}}  {detail}")
    bad = sum(1 for r in rows if r[0] == "FAIL")
    warn = sum(1 for r in rows if r[0] == "warn")
    _say(f"{bad} problem(s), {warn} warning(s)")
    return 1 if bad else 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    RAW = argparse.RawDescriptionHelpFormatter
    ap = argparse.ArgumentParser(prog="devmem", description=DESCRIPTION, epilog=EPILOG, formatter_class=RAW)
    ap.add_argument("--version", action="version", version=f"devmem {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="<command>", title="commands")

    def add(name, help, example="", project=True, store=True, plugin=False):
        p = sub.add_parser(name, help=help, description=help + ".", epilog=("examples:\n" + example) if example else None,
                           formatter_class=RAW)
        if project:
            p.add_argument("--project", "-p", default=None, metavar="DIR", help="project folder (default: current folder)")
        if store:
            p.add_argument("--store", metavar="DIR", help="memory store folder (default: <project>/.devmemory)")
        if plugin:   # ${CLAUDE_PLUGIN_DATA}, passed by the plugin's skills: where the plugin keeps which projects are on
            p.add_argument("--plugin-data", metavar="DIR", help=argparse.SUPPRESS)
        p.set_defaults(func=globals()["cmd_" + name])
        return p

    p = add("init", "set a project up for automatic capture (installs Claude Code hooks, scans)",
            "  devmem init\n  devmem init --project ~/code/game\n  devmem init --shared   # write .claude/settings.json instead")
    p.add_argument("--shared", action="store_true",
                   help="write .claude/settings.json (usually committed) instead of the personal settings.local.json. "
                        "The hook command holds this machine's absolute paths.")
    p.add_argument("--no-scan", action="store_true", help="do not run the first scan")
    p.add_argument("--async-tools", action="store_true",
                   help="do not make tool calls wait for the hook (faster; the last events of a headless run can be lost)")
    p.add_argument("--python", metavar="EXE", help="interpreter the hooks run with (default: the one running devmem)")
    p.add_argument("--form", choices=["exec", "bash", "powershell"], default="exec",
                   help="hook command form; exec needs no shell (default), the others are for older Claude Code")
    p.add_argument("--adopt-store", action="store_true",
                   help="use an existing .devmemory folder that git tracks or that already holds sessions (a cloned "
                        "repository can ship one; its contents would be added to your prompts)")
    p.add_argument("--allow-unredacted", action="store_true", help=ALLOW_UNREDACTED_HELP)

    p = add("adopt", "show what an existing memory store holds and mark it as yours (installs nothing)",
            "  devmem adopt                  # a store an older devmem made (0.3.0 kept no record of it)\n"
            "  devmem adopt --store ~/mem/game\n  devmem adopt --yes            # from a script: no question")
    p.add_argument("--yes", "-y", action="store_true",
                   help="adopt without asking (needed when stdin is not a terminal)")

    p = add("embed", "show the optional embedding model in force; --download fetches it (nothing else downloads)",
            "  devmem embed\n  devmem embed --download\n  devmem embed --download --model fastembed:intfloat/multilingual-e5-small")
    p.add_argument("--download", action="store_true",
                   help="download the model files (from Hugging Face, through the backend library)")
    p.add_argument("--model", metavar="BACKEND[:MODEL]",
                   help="fastembed[:<model>] or model2vec:<model> (default: DEVMEM_EMBED, else the store's config.json)")

    p = add("uninstall", "remove devmem's hooks from the project (captured memory is kept)",
            "  devmem uninstall\n  devmem uninstall --shared")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--shared", action="store_true", help="only .claude/settings.json")
    g.add_argument("--local", action="store_true", help="only .claude/settings.local.json")

    p = add("hook", "run one Claude Code hook event (stdin: the hook JSON). Normally installed by `devmem init`",
            "  echo '{...}' | devmem hook PostToolUse\n  devmem hook            # same as UserPromptSubmit (older setups)",
            project=False)
    p.add_argument("event", nargs="?", help="hook event name (default: UserPromptSubmit)")
    p.add_argument("--project", "-p", help=argparse.SUPPRESS)   # accepted for older setups, unused
    p.add_argument("--budget", type=int, help=argparse.SUPPRESS)
    p.add_argument("--mode", help=argparse.SUPPRESS)
    p.add_argument("--out", help=argparse.SUPPRESS)

    p = add("forget", "delete captured sessions",
            "  devmem forget 3f9a1c         # one session, by id prefix (4+ characters)\n"
            "  devmem forget --before 2026-10-01\n  devmem forget --all --yes")
    p.add_argument("id", nargs="?", help="session id prefix (see `devmem status`)")
    p.add_argument("--before", metavar="DATE", help="every session that started before DATE (YYYY-MM-DD, UTC)")
    p.add_argument("--all", action="store_true", help="every captured session")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation (required when stdin is not a terminal)")

    add("status", "show captured sessions, whether hooks are installed, and whether the store is git-ignored",
        "  devmem status", plugin=True)
    add("scan", "build or update the project map (no model tokens)", "  devmem scan")
    add("doctor", "check the setup: Python, store (and that it is writable), index, hooks, claude, git-ignore; changes nothing",
        "  devmem doctor", plugin=True)

    add("sessions", "list the recorded sessions: id, start, provider, turns, freshness, first words of the request "
        "(what the MCP tool list_sessions returns)", "  devmem sessions", plugin=True)
    p = add("show", "print one recorded session turn by turn, each text labelled with who wrote it (what the MCP tool "
            "read_session returns)", "  devmem show 3f9a1c", plugin=True)
    p.add_argument("id", help="session id prefix (see `devmem sessions`)")
    p = add("packs", "list what the context pack of each recorded prompt carried, or why it was empty (the pack log, "
            ".devmemory/packs.jsonl)", "  devmem packs\n  devmem packs --session 3f9a1c --limit 0", plugin=True)
    p.add_argument("--session", metavar="PREFIX", help="only this session (id prefix)")
    p.add_argument("--limit", type=_ranged(0, 1_000_000), default=50, help="show the last N prompts (default 50; 0: all)")
    p.add_argument("--json", action="store_true", help="print the log's lines as JSON, one per line")

    p = add("pack", "print the context pack for a task", '  devmem pack "add a dash cooldown"\n'
            '  devmem pack "fix the login bug" --budget 800 --out pack.md', plugin=True)
    p.add_argument("task", help="the task, in the user's words")
    p.add_argument("--budget", type=_ranged(100, 20_000), default=1500,
                   help="approximate token budget, 100-20000 (default 1500)")
    p.add_argument("--mode", default="full", choices=["full", "static", "nofresh", "requests"],
                   help="full (default), static (map only, no sessions), nofresh (sessions without stale flags) "
                        "or requests (past sessions and earlier requirements only: no files, no conventions)")
    p.add_argument("--out", metavar="FILE", help="also write the pack to FILE (UTF-8)")
    p.add_argument("--copy", action="store_true",
                   help="put the pack on the clipboard instead of printing it (for chat interfaces without tools)")

    p = add("connect", "set devmem up for another AI tool: MCP entry and a short instruction block, project files only",
            "  devmem connect               # list the tools and what each writes\n"
            "  devmem connect cursor --print\n  devmem connect gemini --project ~/code/game\n"
            "  devmem connect generic       # any MCP client: prints the entry, writes an AGENTS.md block")
    p.add_argument("tool", nargs="?", help="cursor, vscode (copilot), codex, gemini, devin (windsurf), claude-desktop, generic")
    p.add_argument("--list", action="store_true", help="list the supported tools and what each one writes")
    p.add_argument("--print", action="store_true", help="write nothing; print exactly what would be written and where")
    p.add_argument("--command", choices=["devmem", "python"],
                   help="how the tool starts the server: `devmem` (default when on PATH) or this Python with -m devmem")
    p.add_argument("--force", action="store_true",
                   help="replace or remove a devmemory block devmem has no record of writing (edited, or written by "
                        "hand); a copy of the file is kept in .devmemory/connect-backups/ first")
    p.add_argument("--adopt-store", action="store_true",
                   help="use the existing memory store although devmem did not create it for you (a cloned repository "
                        "can ship one): look at it first. Without it the MCP tools refuse such a store")
    p = add("disconnect", "remove what `devmem connect <tool>` added (captured memory is kept)",
            "  devmem disconnect cursor", store=False)
    p.add_argument("tool")
    p.add_argument("--print", action="store_true", help="write nothing; show what would be removed")
    p.add_argument("--force", action="store_true",
                   help="replace or remove a devmemory block devmem has no record of writing (edited, or written by "
                        "hand); a copy of the file is kept in .devmemory/connect-backups/ first")

    p = add("run", "run headless `claude -p` on a prompt and capture the session", '  devmem run "add a settings screen"')
    p.add_argument("prompt")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--max-turns", type=_ranged(1, 1000), default=40)

    p = add("import", "record an existing Claude stream-json file (or a Claude Code transcript) as a session",
            '  devmem import run.jsonl --prompt "what I asked"')
    p.add_argument("stream")
    p.add_argument("--prompt", required=True)

    p = add("mcp", "serve devmem over MCP (stdio) for any MCP client",
            "  claude mcp add devmemory -- devmem mcp --project .\n"
            "  claude mcp add devmemory -- devmem mcp --project . --read-only --require-store   # with hooks recording")
    p.add_argument("--read-only", action="store_true",
                   help="offer only get_context_pack, list_sessions and read_session (no begin/end_session): for a "
                        "client whose hooks already record sessions, such as Claude Code with `devmem init` or the "
                        "devmem plugin. It records nothing, but get_context_pack still refreshes the derived project "
                        "map and search index in the store (PROJECT.md, index/)")
    p.add_argument("--require-store", action="store_true",
                   help="never create the memory store; answer that memory is not set up when it is missing")

    p = add("setup", "switch memory on for a project used with the devmem Claude Code plugin (creates the store, "
            "scans, and records the project as switched on in the plugin's data folder; writes no hooks)",
            "  /devmem:setup in Claude Code runs this with the plugin's data folder\n"
            "  devmem setup --plugin-data <folder> --off   # switch recording off again, keep the store",
            store=False, plugin=True)
    p.add_argument("--no-scan", action="store_true", help="do not run the first scan")
    p.add_argument("--off", action="store_true", help="switch recording off for this project (the store is kept)")
    p.add_argument("--adopt-store", action="store_true",
                   help="use an existing .devmemory folder that git tracks or that already holds sessions this plugin "
                        "did not record here (a cloned repository can ship one; its contents would reach your prompts)")
    p.add_argument("--allow-unredacted", action="store_true", help=ALLOW_UNREDACTED_HELP)

    p = add("codex", "run Codex CLI with the pack and capture the session", '  devmem codex "refactor the parser"')
    p.add_argument("prompt")
    p.add_argument("--model")
    p.add_argument("--no-pack", action="store_true", help="capture only, no context pack")
    p.add_argument("--mcp", action="store_true", help="give Codex devmem as an MCP server instead of a pack")
    p.add_argument("--isolated", action="store_true", help="ignore the user's Codex config (no personal plugins)")

    p = add("wrap", "run any command-line agent with the pack and capture the session",
            '  devmem wrap --prompt "add tests" --provider aider -- aider --message {prompt}')
    p.add_argument("--prompt", required=True)
    p.add_argument("--provider", default="cli")
    p.add_argument("--no-pack", action="store_true")
    p.add_argument("command", nargs=argparse.REMAINDER, help="the command; {prompt} and {pack_file} are replaced")

    add("begin", "start recording a session in an IDE tool (snapshot the project)",
        '  devmem begin\n  ... work in the IDE ...\n  devmem end --id <id> --prompt "what I asked"')
    p = add("end", "finish a `devmem begin` recording and store the diff as a session",
            '  devmem end --id 20261113T2007_ab12cd34 --prompt "what I asked" --provider cursor')
    p.add_argument("--prompt", required=True, help="what you asked the tool, in your words")
    p.add_argument("--id", help="pending id printed by `devmem begin` (default: the most recent begin)")
    p.add_argument("--provider", default="manual", help="name of the tool, e.g. cursor")
    p.add_argument("--summary", help="what the tool says it did; stored labelled as a user-provided summary")

    p = add("ui", "serve the local web app on 127.0.0.1 (never modifies project files; scan/rescan write its own store)", "  devmem ui --port 8765")
    p.add_argument("--port", type=_port, default=8765)
    p.add_argument("--no-browser", action="store_true")

    p = add("app", "open the local app in its own browser window", "  devmem app\n  devmem app --project .")
    p.set_defaults(project=None)
    p.add_argument("--dry-run", action="store_true", help="print the browser command and exit")
    p.add_argument("--port", type=_port, default=0)
    return ap


WIN_MAX_PATH = 260          # with long paths off, Windows refuses a file path of 260 characters or more
WIN_MAX_DIR = 248           # ... and a new folder's path of 248 or more (ERROR_FILENAME_EXCED_RANGE, 206)


def _long_path_hint(e: OSError) -> str:
    """Windows without long paths: a store file whose full path reaches 260 characters cannot be created, and the
    error says only "file not found". Say why (in practice a project folder of about 180 characters or more).

    Only for that cause: Windows, long paths off, and either error 206 (a name over the limit) or "not found"
    (errno 2 / Windows error 3, which is what a file path of 260+ characters gives) on a path that long. Another
    error on a long path, or any error with long paths on, gets no hint."""
    if os.name != "nt":
        return ""
    winerror = getattr(e, "winerror", None)
    if winerror != 206 and not (isinstance(e, FileNotFoundError) or winerror == 3):
        return ""
    names = [n for n in (e.filename, e.filename2) if isinstance(n, str) and n and not n.startswith("\\\\?\\")]
    lengths = []
    for n in names:
        try:
            lengths.append(len(os.path.abspath(n)))
        except (OSError, ValueError):
            lengths.append(len(n))
    limit = WIN_MAX_DIR if winerror == 206 else WIN_MAX_PATH
    longest = max(lengths, default=0)
    if longest < limit or long_paths_enabled():
        return ""
    return (f". That path is {longest} characters, over the limit Windows applies (260 for a file, 248 for a folder) "
            "unless long paths are turned on (LongPathsEnabled): move the project to a shorter folder, or turn long "
            "paths on")


# the longest names devmem writes inside a store for a Claude Code session (36-character id), while writing them:
# live/.<id>.state.json.<pid>.tmp and sessions/.<time>_<id tag>.meta.json.<pid>.tmp are 65-66 characters after the
# store folder (59 + the process id). Ids from other tools may be longer (up to 64 characters).
STORE_NAME_ROOM = 66


def long_paths_enabled() -> bool:
    """Windows: is the LongPathsEnabled switch on (Python is long-path aware when it is)? Elsewhere True."""
    if os.name != "nt":
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return winreg.QueryValueEx(k, "LongPathsEnabled")[0] == 1
    except (OSError, ImportError):
        return False


def store_path_warning(store: Path) -> str | None:
    """A warning when files in `store` would pass Windows' 260-character limit (long paths off): writes there fail,
    so sessions stay unfinished or are not recorded at all, and scans stop with "file not found"."""
    n = len(str(store))
    if os.name != "nt" or n + STORE_NAME_ROOM < WIN_MAX_PATH or long_paths_enabled():
        return None
    return (f"the store path is {n} characters; devmem's files in it need up to {STORE_NAME_ROOM} more, and Windows "
            f"refuses paths of {WIN_MAX_PATH} characters or more unless long paths are turned on (LongPathsEnabled). "
            "Writes there will fail (sessions are not recorded completely or at all): move the project to a shorter "
            "folder or turn long paths on")


def main(argv=None) -> int:
    _safe_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0
    # Embedding model files are fetched only by `devmem embed --download` (cmd_embed). Every other command, typed by
    # the user or run by an agent through its shell, loads a model from local files only (devmem.embed).
    try:
        return int(a.func(a) or 0)
    except CliError as e:
        term.say(f"devmem: {e}", file=sys.stderr)
        return 2
    except StoreRefused as e:     # a linked store folder, or a --store that is not a store: nothing was written
        term.say(f"devmem: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except FileNotFoundError as e:
        _debug_traceback()
        term.say(f"devmem: file not found: {_one_line(e.filename or e, 300)}{_long_path_hint(e)}", file=sys.stderr)
        return 2
    except OSError as e:          # an unreadable folder, a name the file system refuses, a full disk...
        _debug_traceback()
        where = f" ({_one_line(e.filename, 200)})" if e.filename else ""
        term.say(f"devmem: {_one_line(e.strerror or e, 300)}{where}{_long_path_hint(e)}", file=sys.stderr)
        return 2
    except (ValueError, OverflowError) as e:
        _debug_traceback()
        term.say(f"devmem: {_one_line(e, 300)}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - last resort: one line, the traceback only with DEVMEM_DEBUG=1
        _debug_traceback()
        term.say(f"devmem: unexpected error: {type(e).__name__}: {_one_line(e, 300)} (set DEVMEM_DEBUG=1 for details)",
              file=sys.stderr)
        return 2


def _inside(path: str, folder: str) -> bool:
    p, f = os.path.normcase(path), os.path.normcase(folder)
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)


def _split_output(path) -> tuple[str, list[str]]:
    """(anchor, components) of the absolute spelling of `path`, `..` kept (it is resolved physically, after links)."""
    full = os.path.join(os.getcwd(), os.fspath(path))
    if not os.path.isabs(full):            # a drive-relative `D:x` on Windows
        full = os.path.abspath(full)
    drive, rest = os.path.splitdrive(full)
    if os.altsep:
        rest = rest.replace(os.altsep, os.sep)
    return drive + os.sep, [c for c in rest.split(os.sep) if c]


def _output_folder(anchor: str, parts: list[str], project: Path) -> str:
    """The real folder a file the user named is written in (`parts` are the folders on the way), walked one component
    at a time from `anchor`. The user may write anywhere, but a link or junction that lives inside the project (which a
    cloned repository can ship: `notes -> ~/.ssh`) may not lead out of it; links elsewhere (`/tmp -> /private/tmp`)
    are followed. `..` after a link is the real folder's parent, as the operating system takes it."""
    from .store import _is_link
    root = os.path.realpath(str(project))
    current = anchor
    for c in parts:
        if c == ".":
            continue
        if c == "..":
            current = os.path.dirname(current)
            continue
        nxt = os.path.join(current, c)
        if _is_link(nxt):
            target = os.path.realpath(nxt)
            # where the link lives, by its real folder (the spelling given may use 8.3 short names or other links)
            if _inside(os.path.realpath(current), root) and not _inside(target, root):
                raise CliError(f"{nxt} is a symbolic link or junction inside the project that leads out of it "
                               f"(to {target}); devmem does not write through it. Name the real folder instead")
            current = target
        else:
            current = nxt
    return current


def _write_output(path: Path, text: str, project: Path) -> None:
    """Write a file the user named (`pack --out`): never through a link (a repository can ship `pack.md -> elsewhere`,
    or a folder `out -> elsewhere`: _output_folder), through a temporary file in the same folder that then replaces
    `path` (created O_EXCL|O_NOFOLLOW; os.replace replaces a link swapped in at `path`, never follows it)."""
    import stat as _st
    from .store import _is_link
    anchor, parts = _split_output(path)
    raw = os.fspath(path)
    if not parts or parts[-1] in (".", "..") or raw.endswith((os.sep, os.altsep or os.sep)):
        raise CliError(f"{path} names a folder; name a file")
    folder = Path(_output_folder(anchor, parts[:-1], project))
    path = folder / parts[-1]
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if _is_link(path) or (st is not None and _st.S_ISLNK(st.st_mode)):
        raise CliError(f"{path} is a symbolic link or junction; devmem does not write through a link. Remove it or "
                       "name another file")
    if st is not None and not _st.S_ISREG(st.st_mode):
        raise CliError(f"{path} exists and is not a regular file")
    tmp = folder / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
                 0o666)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        os.replace(tmp, path)        # replaces a link that appeared in between, never follows it
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _debug_traceback() -> None:
    if os.environ.get("DEVMEM_DEBUG"):
        import traceback
        term.warn(traceback.format_exc().rstrip())    # escaped: the message can quote a project's text


def _port(text: str) -> int:
    """--port: 0 (a free port) to 65535."""
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {text[:40]!r}") from None
    if not 0 <= n <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 0 to 65535 (0 picks a free one), not {text[:40]}")
    return n


if __name__ == "__main__":
    sys.exit(main())
