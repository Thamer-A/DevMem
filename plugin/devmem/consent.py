"""Which projects this user switched devmem on for. Consent lives OUTSIDE the project: nothing a repository ships
(a `.devmemory/` folder, a committed `.claude/settings.json`) can switch recording or lookups on.

  plugin route   ${CLAUDE_PLUGIN_DATA}/projects/<key>         written by /devmem:setup (devmem.plugin)
  init route     <user data>/projects/<key>                    written by `devmem init`, renamed to <key>.off by
                                                               `devmem uninstall`
  either route   <user data>/projects/<key>.plugin             a note that /devmem:setup set this project up (it
                                                               survives an uninstall of the plugin, which deletes
                                                               the plugin's data folder)
  any store      <user data>/stores/<store key>                 this user's devmem created the store, or the user
                                                               adopted it (--adopt-store): see store_refusal

<key> is a hash of the project's normalised real path (`project_key`), so a moved or renamed project is another
project. <user data> is devmem's per-user folder (the one that holds the app's recents.json): %APPDATA%\\DevMemory,
~/Library/Application Support/DevMemory or $XDG_CONFIG_HOME/DevMemory (~/.config/DevMemory); DEVMEM_USER_DATA
overrides it (the tests point it at a temporary folder). Checking a switch costs one hash and one stat.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

USER_DATA_ENV = "DEVMEM_USER_DATA"
PROJECTS = "projects"
OFF = ".off"
PLUGIN_NOTE = ".plugin"


def user_data_dir(platform: str | None = None, env=None, home: Path | None = None) -> Path:
    """devmem's per-user folder (all arguments injectable for tests)."""
    env = os.environ if env is None else env
    if env.get(USER_DATA_ENV):
        p = Path(env[USER_DATA_ENV])
        if not p.is_absolute():
            raise ValueError(f"{USER_DATA_ENV} must be an absolute path (it is {env[USER_DATA_ENV]!r})")
        return p
    platform = platform or sys.platform
    home = Path(home) if home else Path.home()
    if platform.startswith("win"):
        base = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    elif platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    return base / "DevMemory"


def _sha256(data: bytes) -> str:
    try:                          # the built-in module: importing hashlib loads OpenSSL (about 7 ms on Windows)
        from _sha2 import sha256  # type: ignore[import-not-found]  # Python 3.12+
    except ImportError:
        try:
            from _sha256 import sha256  # type: ignore[import-not-found,no-redef]  # Python 3.10, 3.11
        except ImportError:
            from hashlib import sha256  # type: ignore[assignment]  # builds without the built-in hashes
    return sha256(data).hexdigest()


def project_key(project) -> str:
    """The marker name of a project: a hash of its real path, normalised (case on Windows, separators, links)."""
    from .store import local_path   # `\\?\C:\x` and `\\localhost\C$\x` are the same folder as `C:\x`: one key
    real = os.path.normcase(os.path.realpath(local_path(project)))
    return _sha256(real.encode("utf-8", "surrogatepass"))[:40]


def marker_in(base, project) -> Path:
    """`<base>/projects/<key>`: the switch for `project` in a data folder (the plugin's or devmem's own)."""
    return Path(base) / PROJECTS / project_key(project)


def write_marker(path: Path, project, allow_unredacted: bool = False, redact_sha256: str | None = None) -> None:
    """A switch file. `allow_unredacted`: the user allowed the store's config.json to turn redaction off
    (`--allow-unredacted`); nothing in the project can grant that (see allows_unredacted). `redact_sha256`: the
    store's `redact` file this user approves (its SHA-256, or "absent"); by default the file as it is now, since the
    user is setting the project up (devmem.redact.user_patterns_state runs only an approved file)."""
    from datetime import datetime, timezone

    from .redact import redact_file_hash
    from .store import private_mkdir, write_private
    if redact_sha256 is None:
        redact_sha256 = redact_file_hash(Path(project) / ".devmemory")
    if redact_sha256 != "absent":
        from .redact import check_patterns_file
        check_patterns_file(Path(project) / ".devmemory")   # measured now, so no hook waits for it (redact.probe_patterns)
    try:
        own = user_data_dir()
    except ValueError:
        own = None
    if own is not None and path.parent.parent == own:
        private_mkdir(own)                  # devmem's per-user folder: 0700 on POSIX (the plugin's data folder is not ours)
    private_mkdir(path.parent)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    write_private(tmp, json.dumps({"project": str(project), "written_at": datetime.now(timezone.utc).isoformat(
        timespec="seconds"), "allow_unredacted": bool(allow_unredacted), "redact_sha256": redact_sha256,
        "note": "written by devmem: this user switched memory on for this project"}).encode("utf-8"))
    os.replace(tmp, path)


def _marker_field(path: Path, field: str):
    try:
        from .store import read_regular
        d = json.loads((read_regular(path, 64 * 1024) or b"null").decode("utf-8"))
        return d.get(field) if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def approved_redact_hashes(project) -> set[str]:
    """The `redact` file contents (SHA-256) this user approved for `project`: from the `devmem init` switch and the
    /devmem:setup note in devmem's per-user folder. A marker written before this field existed approves nothing, so
    an existing `redact` file stays unused until the user sets the project up again (`devmem doctor` says so)."""
    try:
        m = init_marker(project)
    except (OSError, RuntimeError, ValueError):
        return set()
    out = set()
    for p in (m, m.with_name(m.name + PLUGIN_NOTE)):
        v = _marker_field(p, "redact_sha256")
        if isinstance(v, str):
            out.add(v)
    return out


def _inside(child: Path, parent: Path) -> bool:
    try:
        c = Path(os.path.normcase(os.path.realpath(child)))
        return c == Path(os.path.normcase(os.path.realpath(parent))) or \
            c.is_relative_to(Path(os.path.normcase(os.path.realpath(parent))))
    except (OSError, ValueError):
        return False


def init_marker(project) -> Path:
    """`<user data>/projects/<key>`. Raises ValueError when the per-user folder is unusable for this project: a
    relative DEVMEM_USER_DATA, or one inside the project (the project could then hold its own switches)."""
    base = user_data_dir()
    if _inside(base, project):
        raise ValueError(f"devmem's per-user folder {base} is inside the project {project}; set {USER_DATA_ENV} "
                         "to a folder outside it")
    return marker_in(base, project)


def check_user_data(project) -> Path:
    """For `devmem init` / `devmem setup`, before they change anything: the per-user folder is usable for this
    project and can be written. Raises ValueError with a one-line reason."""
    m = init_marker(project)
    probe = m.parent / f".write-test.{os.getpid()}"
    try:
        from .store import private_mkdir, write_private
        private_mkdir(m.parent.parent)
        private_mkdir(m.parent)
        write_private(probe, b"")
        probe.unlink()
    except OSError as e:
        raise ValueError(f"cannot write this project's switch in devmem's per-user folder {m.parent} "
                         f"({e.strerror or e}); nothing changed") from None
    return m


def init_enabled(project) -> bool:
    """Did this user run `devmem init` for this project (and not `devmem uninstall` since)? One hash, one stat."""
    try:
        return init_marker(project).is_file()
    except (OSError, RuntimeError, ValueError):
        return False


def revoke_unredacted_note(project) -> None:
    """Take the --allow-unredacted permission out of the /devmem:setup note, if it holds one. Every command that
    switches a project on without the flag, or off, calls this: the permission must not outlive the command that
    gave it (a user who changes route, or switches off and on again, expects redaction to be back)."""
    try:
        note = init_marker(project)
        note = note.with_name(note.name + PLUGIN_NOTE)
        if _allows(note):        # keeps the approval it holds: revoking one permission approves nothing new
            write_marker(note, project, False, redact_sha256=str(_marker_field(note, "redact_sha256") or "absent"))
    except (OSError, RuntimeError, ValueError):
        pass


def enable_init(project, allow_unredacted: bool = False, store=None) -> Path:
    """`devmem init`: switch recording on for `project`, approving the store's `redact` file as it is now."""
    m = init_marker(project)
    from .redact import redact_file_hash
    write_marker(m, project, allow_unredacted,
                 redact_sha256=redact_file_hash(Path(store) if store else Path(project) / ".devmemory"))
    if not allow_unredacted:
        revoke_unredacted_note(project)
    try:
        m.with_name(m.name + OFF).unlink()
    except OSError:
        pass
    return m


def _allows(path: Path) -> bool:
    try:
        from .store import read_regular
        data = read_regular(path, 64 * 1024)     # a plain file only: a FIFO or link here never blocks a hook
        d = json.loads(data.decode("utf-8")) if data is not None else None
        return isinstance(d, dict) and d.get("allow_unredacted") is True
    except (OSError, ValueError):
        return False


def allows_unredacted(project) -> bool:
    """May the store's config.json turn redaction off for this project? Only when the user said so outside the
    project: `devmem init --allow-unredacted` (the switch is on) or `/devmem:setup --allow-unredacted` (its note)."""
    try:
        m = init_marker(project)
    except (OSError, RuntimeError, ValueError):
        return False
    return (m.is_file() and _allows(m)) or _allows(m.with_name(m.name + PLUGIN_NOTE))


def disable_init(project) -> bool:
    """`devmem uninstall`: <key> becomes <key>.off (so "this user set the project up before" is kept). The
    renamed file is not read for the --allow-unredacted permission, and the setup note loses it too."""
    m = init_marker(project)
    revoke_unredacted_note(project)
    if not m.is_file():
        return False
    os.replace(m, m.with_name(m.name + OFF))
    return True


def note_plugin_setup(project, allow_unredacted: bool = False) -> None:
    """/devmem:setup leaves a note in devmem's own folder too: the plugin's data folder goes with the plugin. It
    also carries the --allow-unredacted permission, so it must be written (setup checks the folder first)."""
    m = init_marker(project)
    write_marker(m.with_name(m.name + PLUGIN_NOTE), project, allow_unredacted)


def set_up_here_before(project) -> bool:
    """Has this user set this project up with either route, now or earlier (on, off, or the plugin note)?"""
    try:
        m = init_marker(project)
    except (OSError, RuntimeError, ValueError):
        return False
    return any(p.is_file() for p in (m, m.with_name(m.name + OFF), m.with_name(m.name + PLUGIN_NOTE)))


def init_state(project) -> str:
    """"on", "off" (switched off with `devmem uninstall`), "never" (no `devmem init` by this user here), or
    "unusable" (the per-user folder cannot hold switches for this project: see init_marker)."""
    try:
        m = init_marker(project)
    except (OSError, RuntimeError, ValueError):
        return "unusable"
    return "on" if m.is_file() else "off" if m.with_name(m.name + OFF).is_file() else "never"


# ---------------------------------------------------------------- is this store this user's?

STORES = "stores"
TRACKED_CAP = 20000          # tracked store entries remembered at adoption; more and the store is refused
NOT_MINE = ("{store} has no record of having been created by this version of devmem for you, so its recorded sessions "
            "are not used and no session is recorded there. A store made by devmem 0.3.0 or earlier has no such "
            "record; a cloned repository or an archive can also ship a store, whose sessions would be shown as your "
            "own earlier requests. If it is yours, run {cmd}: it shows what the store holds and keeps using it (`devmem "
            "init`, `devmem setup` and `devmem connect <tool>` do the same with --adopt-store). Otherwise delete the "
            "folder")
REPLACED = ("{n} file(s) in {store} are tracked by git and were not there, or were different, when this store was "
            "set up or adopted (for example {first}): a `git pull` can bring recorded sessions, so the store's "
            "recordings are not used and no session is recorded there. Delete those files from the store and commit "
            "that (`git rm` them), or if you trust them, run {cmd} to accept them (or --adopt-store with `devmem init`, "
            "`devmem setup` or `devmem connect <tool>`)")
UNUSABLE = ("devmem's per-user folder cannot hold a note for {store} ({why}), so devmem cannot tell that the store is "
            "yours and its recorded sessions are not used")


def store_key(store) -> str:
    """The note name of a store: a hash of its normalised real path (as project_key)."""
    from .store import local_path
    real = os.path.normcase(os.path.realpath(local_path(store)))
    return _sha256(("store:" + real).encode("utf-8", "surrogatepass"))[:40]


def adopt_command(project, store) -> str:
    """The command that adopts `store` for `project`, as a refusal names it: `devmem adopt`, with --store for a store
    that is not the project's own `.devmemory` (quoted when the path holds a space)."""
    try:
        if _is_default_store(project, store):
            return "`devmem adopt`"
    except (OSError, ValueError):
        pass
    s = str(store)
    return f'`devmem adopt --store "{s}"`' if any(c.isspace() for c in s) else f"`devmem adopt --store {s}`"


def is_git_unknown(why: str | None) -> bool:
    """Is this refusal "git could not say which files it tracks": GIT_UNKNOWN (a state that ends by itself) or
    GIT_LIMIT (one that does not)?"""
    return bool(why) and (str(why).startswith(GIT_UNKNOWN.split("{", 1)[0]) or is_git_limit(why))


# Why an event was not recorded, as the short kind devmem.hooks counts (a refusal's text, store_refusal, or a link)
REFUSAL_KINDS = ("git_unknown", "git_limit", "not_yours", "pulled_files", "link", "user_folder_unusable", "refused")


def refusal_kind(why: str | None) -> str:
    """The kind (REFUSAL_KINDS) of a refusal's text."""
    w = str(why or "")
    if is_git_limit(w):
        return "git_limit"
    if is_git_unknown(w):
        return "git_unknown"
    for kind, mark in (("not_yours", " has no record of having been created by"), ("pulled_files", " are tracked by "
                       "git and were not there"), ("user_folder_unusable", "devmem's per-user folder cannot hold a note")):
        if mark in w:
            return kind
    return "link" if "symbolic link or junction" in w else "refused"


def is_git_limit(why: str | None) -> bool:
    """Is this refusal GIT_LIMIT: the question reached a limit of devmem, which does not pass by itself?"""
    return bool(why) and str(why).startswith(GIT_LIMIT.split("{", 1)[0])


def _stays(why: str) -> bool:
    """Is this "git could not say" one that does not pass by itself: git is not on the PATH and devmem cannot read the
    index without it (for a reason other than time)?"""
    return "not on the PATH" in why and "took too long" not in why


def _when(why: str) -> str:
    """When the hooks ask git again after this "git could not say" (devmem.consent._hold), as words."""
    if DIED_AGAIN in why:
        return "in an hour, or as soon as git's index changes"
    if DIED in why:
        return "in a minute, or as soon as git's index changes"
    if _stays(why):
        return "when git's index changes, but this cause does not pass by itself"
    return "within a minute, or as soon as git's index changes"


def _fix_clause(fix: str) -> str:
    """git_fix's sentences after a refusal's last clause (refusals end without a full stop)."""
    return f". {fix.strip().rstrip('.')}" if fix.strip() else ""


def git_fix(why: str, home=None) -> str:
    """For a cause of "git could not say": what to do, as sentences with a leading space (or "")."""
    import re
    if DIED_AGAIN in why:
        return (" If it keeps happening, run `devmem status` in a terminal (it asks without a hook's time limit), and "
                "please report it as a devmem issue.")
    if DIED in why:
        return " There is nothing to do unless it happens again."
    m = re.search(r"dubious ownership in repository at '([^']+)'", why)
    if m or "dubious ownership" in why:
        path = m.group(1) if m else str(home or "<the repository folder>")
        quoted = f'"{path}"' if any(c.isspace() for c in path) else path
        return (" git refuses the repository because another user owns its folder; if you trust it, run "
                f"`git config --global --add safe.directory {quoted}`.")
    if "not on the PATH" in why:
        how = []
        if "split" in why:
            how.append("`git update-index --no-split-index` and `git config core.splitIndex false`")
        if "sparse index" in why:
            how.append("`git sparse-checkout reapply --no-sparse-index`")
        return (" Put git on the PATH of the program that starts devmem (the terminal or app that starts Claude Code); "
                "that is the usual fix" + (f". Or run {' and '.join(how)} in this repository, so that devmem can read "
                                           "its index without git" if how else "") +
                ". Until then, `devmem status` in a terminal where git works answers for git's index as it is now.")
    return ""


def paused_notice(project, store, why: str | None = None, recording: bool = False) -> str:
    """One sentence for the user when a switched-on project's store is refused (store_refusal): the SessionStart hook
    shows it, so recording that stopped (an upgrade from 0.3.0, a pull that brought tracked files) does not stop
    silently. Names the command that resumes it. `why`: the refusal; "git could not say" gets its own wording (it
    passes by itself; `recording`: the hooks still record the session meanwhile, see records_while_unknown)."""
    from .store import store_dir_links
    if store_dir_links(Path(store)):
        return (f"devmem is not recording this project: its memory store {store} is or holds a symbolic link "
                "(`devmem status` says where); remove the link to resume.")
    if is_git_limit(why):
        inner = str(why)[len(GIT_LIMIT.split("{store}", 1)[0] + str(store) + " ("):].split(GIT_LIMIT_AFTER, 1)[0]
        return (f"devmem cannot tell which files of its memory store {store} git tracks, because this repository "
                f"reached a limit of devmem ({inner}); this does not clear by itself, so devmem adds no earlier work to "
                "prompts" + ("; this session is still recorded" if recording else " and records nothing") +
                f". If you trust this repository, run {adopt_command(project, store)} in a terminal to use the store "
                "anyway; if it is an ordinary repository, please report the reason above as a devmem issue.")
    if is_git_unknown(why):
        from .store import git_home
        inner = str(why).partition(" it tracks (")[2].rsplit("); a `git pull`", 1)[0]
        return (f"devmem cannot tell right now which files of its memory store {store} git tracks ({inner}), so it adds "
                "no earlier work to prompts" + ("; this session is still recorded" if recording else
                                                 " and records nothing") +
                f". It asks again {_when(str(why))}." + git_fix(str(why), git_home(project, store)))
    return (f"devmem is not recording this project and adds no earlier work to prompts: its memory store {store} is "
            f"not known to be yours (`devmem status` says why); if it is yours, run {adopt_command(project, store)} "
            "in a terminal to resume.")


def store_note(store) -> Path:
    """`<user data>/stores/<store key>`. Raises ValueError when the per-user folder is unusable (relative, or inside
    the folder that holds the store: the project could then ship its own notes)."""
    base = user_data_dir()
    if _inside(base, Path(store).parent):
        raise ValueError(f"devmem's per-user folder {base} is inside {Path(store).parent}")
    return base / STORES / store_key(store)


NOT_A_FILE = "|not a file"      # end of a _git_index_stamp for an index git cannot read as a file
NOT_REGULAR_INDEX = "git's index is not a regular file"
FAILURE_SECONDS = 60.0          # how long the hooks reuse "git could not answer" for an unchanged index
ASKING_STALE = 600.0            # an "asking" mark older than this counts as left by a process that ended
# A devmem process that ended while asking (Claude Code ends a hook at its time limit; out of memory; a crash) is not
# a limit of devmem: the hooks wait DIED_RETRY_SECONDS before they ask about that index again, and only when the
# question ended its process again within DIED_REPEAT_SECONDS of the last time do they wait DIED_SECONDS. A command the
# user runs (status, doctor, adopt) has no hook time limit to protect: it asks again at once, and its answer replaces
# the mark.
DIED_RETRY_SECONDS = 60.0
DIED_REPEAT_SECONDS = 1800.0
DIED_SECONDS = 3600.0
DIED = ("the devmem process that last asked about this index ended before it had the answer: it was killed (Claude Code "
        "ends a hook at its time limit), ran out of memory or crashed")
DIED_AGAIN = ("the devmem processes that asked about this index ended before they had the answer, more than once in a "
              "row: they were killed (Claude Code ends a hook at its time limit), ran out of memory or crashed")
CHECKED_CAP = 8 * 1024 * 1024   # largest <store key>.checked file read back
GIT_UNKNOWN = ("git could not say which files of {store} it tracks ({why}); a `git pull` can bring recorded sessions "
               "into a store, so no recorded session from it is used until it can. devmem asks again {when}{fix}")
GIT_LIMIT_AFTER = "), so it cannot tell which of its files git tracks"
GIT_LIMIT = ("git's index reached a limit of devmem for {store} ({why}" + GIT_LIMIT_AFTER + "; a `git pull` can bring "
             "recorded sessions into a store, so no recorded session from it is used. This does not clear by itself. If "
             "you trust this repository, run {cmd}: it uses the store anyway (devmem then cannot see what a pull brings "
             "into it while the limit holds); if it is an ordinary repository, please report the reason as a devmem "
             "issue. Otherwise delete the folder")
_FRESH_FAILURES: set[str] = set()   # stores whose "git could not say" this process found itself (not a kept one)
_LIMITS: dict[str, str] = {}       # stores whose last "git could not say" in this process was a limit: its kind
# The kinds of limit (a short code, whatever route gave the answer and however its text reads): "size" (git's list or
# devmem's reading of the index is past a bound: candidates, output, folders, path bytes, entries, time to place them),
# "tracked" (more entries of the index land in the store than devmem keeps: TRACKED_CAP).
LIMIT_KINDS = ("size", "tracked")


def _limit_kind(why: str) -> str:
    """The kind of a limit from its text: for a failure or an acceptance written before kinds were kept."""
    return "tracked" if "entries of git's index land in it" in str(why) else "size"


def _checked_path(store) -> Path:
    """`<user data>/stores/<store key>.checked`: git's last answer about this store (_tracked). ValueError: unusable."""
    return store_note(store).with_name(store_key(store) + ".checked")


def _tracked(project, store, cached: bool = True) -> tuple[list[str] | None, str]:
    """(every index entry that lands in `store`, "") whether or not it is in the work tree now, or (None, why) when
    there is no answer for git's current index. Which entries land there is devmem.store.store_index_entries (by
    identity on disk, any spelling), asked in devmem.store.git_home.

    The entries can only change when git's index changes, so the last answer git gave is kept outside the project
    with the index stamp it was computed for (`<store key>.checked`) and reused while the stamp is unchanged, by every
    route and every process: git runs only after the index changed. When git cannot give the answer (not on PATH,
    failed, timed out, not run here: devmem.store.git_argv, or a git that did not answer in time less than a minute
    ago: devmem.store._git_slow), devmem reads git's index file itself (devmem.gitindex). When neither can say, or
    the index is not a regular file (git is not started: a named pipe there would make it wait), the answer is None,
    never "nothing tracked"; the failure is kept for FAILURE_SECONDS with its stamp, and cached=True (the hooks)
    reuses it for that long instead of asking again (a command, the MCP server and the viewer ask again). Nothing can
    be tracked ([]) only outside a git work tree (no `.git` at or above devmem.store.git_home)."""
    import subprocess
    import time
    from .store import GIT_TIMEOUT, git_home, read_regular, store_index_entries
    home = git_home(project, store)
    stamp = _git_index_stamp(home)
    if stamp is None:
        return [], ""             # not in a git work tree: nothing can be tracked
    hkey = project_key(home)
    _LIMITS.pop(store_key(store), None)
    try:
        path: Path | None = _checked_path(store)
    except ValueError:
        path = None
    d: dict = {}
    if path is not None:
        try:
            got = json.loads((read_regular(path, CHECKED_CAP) or b"null").decode("utf-8"))
            d = got if isinstance(got, dict) and got.get("home") == hkey else {}
        except (OSError, ValueError):
            d = {}
    ok, failed = d.get("ok"), d.get("failed")
    ok = ok if isinstance(ok, dict) and isinstance(ok.get("entries"), list) else None
    if ok is not None and ok.get("stamp") == stamp:
        return [str(x) for x in ok["entries"]], ""
    if isinstance(failed, dict) and failed.get("stamp") == stamp:
        try:
            age = time.time() - _note_time(failed.get("at"))
        except (TypeError, ValueError):
            age = -1.0
        if 0.0 <= age < _hold(failed) and cached:       # a command asks again
            if failed.get("limit"):
                kind = failed["limit"]
                _LIMITS[store_key(store)] = kind if kind in LIMIT_KINDS else _limit_kind(failed.get("why") or "")
            return None, str(failed.get("why") or "git did not answer")
    limit: str | None = None
    died = 0
    asking = d.get("asking")
    if cached and isinstance(asking, dict) and asking.get("stamp") == stamp and _asker_ended(asking):
        # a command (cached=False) asks itself instead: it has no hook time limit, and its answer replaces the mark
        died = 1 + (_died_count(failed) if isinstance(failed, dict) and failed.get("stamp") == stamp else 0)
        why = DIED if died == 1 else DIED_AGAIN
    elif stamp.endswith(NOT_A_FILE):
        why = NOT_REGULAR_INDEX
    else:
        if path is not None:          # written before the work: a process that ends inside it leaves this behind
            kept = _kept_ok(path, hkey, ok)
            try:
                _write_json_private(path, {**{k: v for k, v in d.items() if k != "asking"}, "home": hkey,
                                           **({"ok": kept} if kept is not None else {}),
                                           "asking": {"stamp": stamp, "at": time.time(), "pid": os.getpid()}})
            except OSError:
                pass
        from .store import GitLimit
        try:
            entries = store_index_entries(home, store)
        except subprocess.TimeoutExpired:
            why = f"git did not answer within {GIT_TIMEOUT:g} s"
        except GitLimit as e:
            why, limit = str(e) or type(e).__name__, "size"
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            why = str(e) or type(e).__name__
        else:
            if len(entries) <= TRACKED_CAP:
                if path is not None:
                    try:
                        _write_json_private(path, {"home": hkey, "ok": {"stamp": stamp, "entries": entries}})
                    except OSError:
                        pass
                return entries, ""
            why, limit = f"more than {TRACKED_CAP} entries of git's index land in it", "tracked"
    if path is not None:
        ok = _kept_ok(path, hkey, ok)       # a clean answer another process wrote meanwhile is kept, not overwritten
        try:
            _write_json_private(path, {"home": hkey, **({"ok": ok} if ok is not None else {}),
                                       "failed": {"stamp": stamp, "at": time.time(), "why": why[:300],
                                                  **({"limit": limit} if limit else {}),
                                                  **({"died": died} if died else {})}})
        except OSError:
            pass
    _FRESH_FAILURES.add(store_key(store))
    if limit:
        _LIMITS[store_key(store)] = limit
    return None, why


def _kept_ok(path: Path, hkey: str, ok: dict | None) -> dict | None:
    """The clean answer a write of `<store key>.checked` must keep: the one in the file now (another process may have
    written it since this one read the file), else `ok`. The file is replaced whole (os.replace), so a write that kept
    only what it read at its start dropped an answer given meanwhile, and later events refused instead of recording
    (records_while_unknown). Re-read just before the write: a window of one small read is left."""
    from .store import read_regular
    try:
        now = json.loads((read_regular(path, CHECKED_CAP) or b"null").decode("utf-8"))
    except (OSError, ValueError):
        return ok
    cur = now.get("ok") if isinstance(now, dict) and now.get("home") == hkey else None
    return cur if isinstance(cur, dict) and isinstance(cur.get("entries"), list) else ok


def _note_time(v) -> float:
    """A time a per-user note holds, or 0 (long ago) for anything that is not a sane finite number
    (devmem.sessions.state_time): a huge integer, NaN or a string never raises."""
    from .sessions import state_time
    t = state_time(v)
    return t if t is not None else 0.0


def _died_count(failed: dict) -> int:
    """How many times in a row the question about `failed`'s index ended its process (0: it did not), counting only a
    death within DIED_REPEAT_SECONDS of the last one as "in a row"."""
    import time
    n = failed.get("died")
    n = 1 if n is True else n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0
    try:
        age = time.time() - _note_time(failed.get("at"))
    except (TypeError, ValueError):
        return 0
    return n if 0.0 <= age < DIED_REPEAT_SECONDS else 0


def _hold(failed: dict) -> float:
    """How long the hooks reuse a kept failure for an unchanged index instead of asking again."""
    n = failed.get("died")
    if n:
        return DIED_RETRY_SECONDS if n is True or n == 1 else DIED_SECONDS
    return FAILURE_SECONDS


def is_git_died(why: str | None) -> bool:
    """Is this refusal "the process that asked ended" (DIED, DIED_AGAIN), which is neither a limit nor git's doing?"""
    return bool(why) and (DIED in str(why) or DIED_AGAIN in str(why))


def _asker_ended(asking: dict) -> bool:
    """Did the process named by an "asking" mark end without replacing it (or is the mark too old to tell)?"""
    import time
    try:
        pid, age = int(asking.get("pid") or 0), time.time() - _note_time(asking.get("at"))
    except (TypeError, ValueError):
        return True
    if not 0.0 <= age < ASKING_STALE:
        return True
    if pid == os.getpid():
        return False
    try:
        from .index import _pid_gone
        return _pid_gone(pid)
    except Exception:  # noqa: BLE001 - cannot be told: as if it still runs (the question is asked again)
        return False


def is_limit_failure(store) -> bool:
    """Was the last "git could not say" found for `store` in this process a limit (devmem.store.GitLimit, more tracked
    entries than devmem keeps), which does not pass by itself? A question that ended its process (DIED) is not one."""
    return store_key(store) in _LIMITS


def limit_kind(store) -> str | None:
    """The kind (LIMIT_KINDS) of the limit is_limit_failure found, or None."""
    return _LIMITS.get(store_key(store))


def _last_answer(project, store) -> list[str] | None:
    """The entries of the last answer kept for `store` (whatever index it was for), or None when there is none."""
    from .store import git_home, read_regular
    try:
        d = json.loads((read_regular(_checked_path(store), CHECKED_CAP) or b"null").decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("home") != project_key(git_home(project, store)):
        return None
    ok = d.get("ok")
    if not isinstance(ok, dict) or not isinstance(ok.get("entries"), list):
        return None
    return [str(x) for x in ok["entries"]]


def failure_is_fresh(store) -> bool:
    """Did this process itself find that git could not say (not a failure kept from an earlier event)? The hooks
    tell the user once per failure, on the event that found it."""
    return store_key(store) in _FRESH_FAILURES


def tracked_answer(project, store, cached: bool = True) -> tuple[list[str] | None, str]:
    """(`<object id> <path>` for each file in `store` that git tracks and that is in the work tree, "") or (None, why)
    when git could not say (_tracked). The object id changes when a pull or a checkout changes the file, not when devmem
    writes to it. Paths as git prints them, relative to devmem.store.git_home. While git's index is unchanged only
    which entries exist on disk is checked again (one lstat per tracked entry, usually none)."""
    from .store import git_home
    got, why = _tracked(project, store, cached)
    if got is None:
        return None, why
    home = str(git_home(project, store))
    return sorted(t for t in got if os.path.lexists(os.path.join(home, t.split(" ", 1)[1]))), ""


def tracked_entries(project, store) -> list[str]:
    """tracked_answer's entries; [] when git could not answer (store_refusal refuses such a store)."""
    return tracked_answer(project, store, cached=False)[0] or []


def _write_json_private(path: Path, data: dict) -> None:
    from .store import private_mkdir, write_private
    private_mkdir(path.parent.parent)
    private_mkdir(path.parent)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    write_private(tmp, json.dumps(data).encode("utf-8"))
    os.replace(tmp, path)


def claim_store(store, project=None, adopt: bool = False) -> None:
    """Note outside the project that this store is this user's: devmem has just created it (ensure_store), a setup
    made sure it holds nothing shipped (devmem.init.prepare_store), or the user adopted it (adopt=True, which also
    accepts the files git tracks in it now, by object id). Either way the answer to "which files of it does git track"
    is asked now and kept (_tracked), so that the first hook event has one even where the hooks cannot run git.
    Raises OSError or ValueError when the note cannot be written."""
    from datetime import datetime, timezone
    tracked: list[str] = []
    accept_limit: dict | None = None
    if adopt and project is not None:
        from .store import git_home
        home = git_home(project, store)
        stamp = _git_index_stamp(home)
        got, why = tracked_answer(project, store, cached=False)
        tracked = got or []
        if got is None and is_limit_failure(store) and stamp and stamp == _git_index_stamp(home):
            # the user saw this limit for this index (store_refusal): adopting accepts that kind of limit for this state
            # of the index, whichever route (git, or devmem reading the index) answers later; not a later state
            accept_limit = {"stamp": stamp, "kind": limit_kind(store), "why": why[:300]}
    if not adopt:
        try:
            _tracked(project if project is not None else Path(store).parent, store, cached=False)
        except Exception:  # noqa: BLE001 - only a head start: the hooks ask themselves when there is no answer
            pass
    if len(tracked) > TRACKED_CAP:
        raise ValueError(f"git tracks {len(tracked)} files in {store}; devmem does not adopt a store like that")
    _write_json_private(store_note(store), {
        "store": str(store), "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "adopted": bool(adopt), "tracked": tracked, **({"accept_limit": accept_limit} if accept_limit else {}),
        "note": "written by devmem: this store is this user's own (created here, or adopted)"})


def _note(store) -> dict | None:
    """The store's note, None when there is none. Raises ValueError when the per-user folder is unusable."""
    from .store import read_regular
    path = store_note(store)
    try:
        d = json.loads((read_regular(path, 4 * 1024 * 1024) or b"null").decode("utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def made_here(store) -> bool:
    """Does devmem's per-user folder hold this user's note for `store` (devmem created it for this user, a setup
    checked it, or the user adopted it: claim_store)? False when the folder is unusable for it (store_note). A project
    cannot write that note: the per-user folder may not be inside the folder that holds the store."""
    try:
        return Path(store).is_dir() and _note(store) is not None
    except (OSError, ValueError):
        return False


def _is_default_store(project, store) -> bool:
    from .store import STORE_DIR
    return os.path.normcase(os.path.realpath(store)) == os.path.normcase(os.path.realpath(Path(project) / STORE_DIR))


def _set_up_with_plugin(project, plugin_data=None) -> bool:
    try:
        from .plugin import DATA_ENV, was_set_up
        return was_set_up(project, plugin_data or os.environ.get(DATA_ENV))
    except (OSError, ValueError, ImportError):
        return False


def _git_index_stamp(project) -> str | None:
    """Where git keeps the index of the work tree holding `project`, with its size, modification time, file id and
    change time, or None (no repository found). Any git command that changes what git tracks rewrites that file (git
    writes a new file and renames it into place: a new file id)."""
    p = Path(os.path.abspath(project))
    for d in (p, *p.parents):
        g = d / ".git"
        try:
            if g.is_dir():
                idx = g / "index"
            elif g.is_file():
                from .store import read_regular
                line = (read_regular(g, 4096) or b"").decode("utf-8", "replace").strip()
                if not line.startswith("gitdir:"):
                    return None
                gd = Path(line[len("gitdir:"):].strip())
                idx = (gd if gd.is_absolute() else d / gd) / "index"
            else:
                continue
            st = os.stat(idx)
            if not stat.S_ISREG(st.st_mode):
                return f"{idx}{NOT_A_FILE}"
            return f"{idx}|{st.st_mtime_ns}|{st.st_size}|{st.st_ino}|{st.st_ctime_ns}"
        except OSError:
            return f"{g}|no index"
    return None


def _accepted_limit(acc, kind: str | None, stamp: str | None) -> bool:
    """Did `devmem adopt` accept this limit (claim_store): the same kind of limit (LIMIT_KINDS), for git's index in the
    same state? An acceptance written before kinds were kept ({stamp, why}) counts when its kind can be told from its
    text (_limit_kind); one from before stamps were kept (`true`) accepts nothing: the question is asked again."""
    if not (isinstance(acc, dict) and stamp and acc.get("stamp") == stamp):
        return False
    had = acc.get("kind") if acc.get("kind") in LIMIT_KINDS else (_limit_kind(acc["why"])
                                                                  if isinstance(acc.get("why"), str) else None)
    return had is not None and had == kind


def _adoption_note(acc, kind: str | None, stamp: str | None) -> str:
    """Why an earlier `devmem adopt` does not cover this refusal, as a clause (or "" when there was none)."""
    if not isinstance(acc, dict) and acc is not True:
        return ""
    if not isinstance(acc, dict) or not acc.get("stamp"):
        return "; the earlier `devmem adopt` was written by an older devmem and is not bound to git's index: adopt again"
    if acc.get("stamp") != stamp:
        return ("; the earlier `devmem adopt` covered git's index as it was then, and the index has changed since")
    return ("; the earlier `devmem adopt` accepted another kind of limit for this same index (now: "
            + ("more entries of git's index land in the store than devmem keeps" if kind == "tracked"
               else "git's index is larger than devmem reads") + ")")


def _tracked_extra(project, store, accepted: set[str], cached: bool) -> tuple[list[str] | None, str]:
    """(tracked store entries not in `accepted`, "") or (None, why) when git could not say (tracked_answer). git runs
    only after its index changed; cached=True (the hooks) also reuses a failure for FAILURE_SECONDS."""
    from .store import git_home
    got, why = _tracked(project, store, cached)
    if got is None:
        return None, why
    home = str(git_home(project, store))        # accepted entries by string first: a look-up only for the others
    return sorted(t for t in got if t not in accepted and os.path.lexists(os.path.join(home, t.split(" ", 1)[1]))), ""


def store_refusal(project, store, check_git: bool | str = True, plugin_data=None) -> str | None:
    """THE decision whether recorded content in `store` may be served to this user, and whether devmem may write
    there: the hooks, `devmem pack`, `sessions`, `show`, `status`, `doctor`, the recording commands, the MCP tools and
    the viewer ask here first, and the code that reads recorded sessions asks again by itself (recorded_refusal), so
    a route that forgets to ask serves nothing. None: yes (or there is no store); else why not, as one paragraph that
    names --adopt-store.

    The store is this user's when devmem's per-user folder holds a note for it (claim_store: devmem created it, a setup
    found nothing shipped in it, or the user adopted it) or, for the project's own `.devmemory` (an older devmem wrote
    no note), when this user set the project up with either route (set_up_here_before, the plugin's own switch). It
    stops being this user's when git tracks a file in it that was not accepted when it was adopted (a pull brought or
    changed it), in any spelling of the store's path (devmem.store.store_index_entries), and is not used while git
    cannot say which files it tracks there (GIT_UNKNOWN; _tracked). check_git=False skips that question; git runs only
    after its index changed, and "cached" (the hooks) also waits FAILURE_SECONDS after a failure before asking again.
    plugin_data: the plugin's data folder when it is not in the environment (the recall skill passes --plugin-data)."""
    store = Path(store)
    if not store.is_dir():
        return None
    if check_git:                  # one lstat per folder (the hooks also check their own files per event)
        from .store import store_dir_links     # links among the files are replaced or skipped where they are used
        links = store_dir_links(store)
        if links:
            return _decided(store, f"the memory store or one of its folders is a symbolic link or junction ({links[0]}); "
                            "devmem never reads recordings or writes through a link. Remove the link (or the folder)")
    try:
        note = _note(store)
    except ValueError as e:
        return _decided(store, UNUSABLE.format(store=store, why=e)) if check_git else UNUSABLE.format(store=store, why=e)
    accepted: set[str] | None = None
    if note is not None:
        tracked = note.get("tracked")
        accepted = {t for t in tracked if isinstance(t, str)} if isinstance(tracked, list) else set()
    elif _is_default_store(project, store) and (set_up_here_before(project)
                                                or _set_up_with_plugin(project, plugin_data)):
        accepted = set()
    if accepted is None:
        why = NOT_MINE.format(store=store, cmd=adopt_command(project, store))
        return _decided(store, why) if check_git else why
    if check_git:
        extra, why = _tracked_extra(project, store, accepted, cached=check_git == "cached")
        if extra is None:
            from .store import git_home
            last = _last_answer(project, store)
            # not for an index that is not a regular file: that is a shape a repository was given, not a git that
            # was slow once, and recording would run git (status) against it
            _RECORDABLE[_decision_key(store)] = (last is not None and set(last) <= accepted
                                                 and why != NOT_REGULAR_INDEX)
            if is_limit_failure(store):
                acc = note.get("accept_limit") if note is not None else None
                stamp = _git_index_stamp(git_home(project, store))
                if _accepted_limit(acc, limit_kind(store), stamp):
                    return _decided(store, None)       # the user adopted the store knowing this (devmem adopt)
                why += _adoption_note(acc, limit_kind(store), stamp)
                return _decided(store, GIT_LIMIT.format(store=store, why=why, cmd=adopt_command(project, store)))
            return _decided(store, GIT_UNKNOWN.format(store=store, why=why, when=_when(why),
                                                      fix=_fix_clause(git_fix(why, git_home(project, store)))))
        if extra:
            return _decided(store, REPLACED.format(n=len(extra), store=store, first=extra[0].split(" ", 1)[-1],
                                                   cmd=adopt_command(project, store)))
        return _decided(store, None)
    return None


# ---------------------------------------------------------------- the decision where recorded sessions are read

DECISION_SECONDS = 2.0       # how long one process reuses an answer for a store (an explicit store_refusal renews it)
_RECORDABLE: dict[str, bool] = {}


def records_while_unknown(store, why: str | None) -> bool:
    """May the hooks keep RECORDING (never serving) while `why` (store_refusal's last answer for `store` in this
    process) is only "git could not say", because git's last answer for this store had nothing tracked in it that the
    user had not accepted? Writing the user's own session into their own store is not what the check protects
    against: a pulled file is a risk when it is read and served, and the hooks then read nothing from the store but
    the session's own files (no other session's log is finalised, no begin file rewritten, no pack built). Not for a
    limit (GIT_LIMIT): git may have answered, with more of the store tracked than devmem keeps (a pull), and a limit
    does not pass by itself; `devmem adopt` accepts it."""
    return is_git_unknown(why) and not is_git_limit(why) and _RECORDABLE.get(_decision_key(store), False)
_DECISIONS: dict[str, tuple[float, str | None]] = {}


def _decision_key(store) -> str:
    return os.path.normcase(os.path.abspath(store))


def _decided(store, why: str | None) -> str | None:
    import time
    _DECISIONS[_decision_key(store)] = (time.monotonic(), why)
    return why


def recorded_refusal(project, store) -> str | None:
    """store_refusal for the code that READS recorded sessions (devmem.sessions.load_sessions, the session index sync
    and the session part of devmem.pack.build_pack): each of them asks here itself, so a route that forgets to ask
    store_refusal still serves nothing from a store that is not this user's. One answer per process and store, reused
    for DECISION_SECONDS (the hooks, the commands, the MCP server and the viewer ask store_refusal at the start of
    their work, which renews it, with their own git and plugin settings); otherwise asked here as the hooks ask
    (check_git="cached"). `project`: None for the store's parent folder."""
    import time
    store = Path(store)
    hit = _DECISIONS.get(_decision_key(store))
    if hit is not None and time.monotonic() - hit[0] < DECISION_SECONDS:
        return hit[1]
    try:
        return store_refusal(Path(project) if project is not None else store.parent, store, check_git="cached")
    except Exception as e:  # noqa: BLE001 - when the answer cannot be had, the recordings are not used
        return _decided(store, UNUSABLE.format(store=store, why=f"{type(e).__name__}: {e}"))


def forget_decisions() -> None:
    """Drop the answers this process keeps (a test that changes a store's state in place)."""
    _DECISIONS.clear()
