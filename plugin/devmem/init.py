"""`devmem init`: set a project up for automatic capture of ordinary Claude Code sessions.

`init_project(project)`:
  1. merges DevMemory's hook handlers into `<project>/.claude/settings.json` without disturbing anything
     else in that file (other hooks, permissions, env, ...). Idempotent: running it again changes nothing.
     The original file is copied once to `<store>/init-backups/settings.json` before the first change (in the
     git-ignored store; earlier versions wrote `settings.json.devmem-backup` next to it, which git would show).
  2. writes `<store>/.gitignore` (`*`) so the memory store is never committed by accident,
  3. runs the first scan.
`uninstall(project)` removes exactly the handlers DevMemory added (the store is kept; deleting memory is
`devmem.sessions.forget_*`). When `init` itself created the settings file (and the `.claude` folder) and
nothing else is in it after the handlers are gone, `uninstall` deletes them again; that is remembered in
`<store>/init-created.json`. A file with any other content, or one init did not create, is never deleted.

The installed command is the absolute path of the current Python interpreter running a short bootstrap
(`python -c <BOOTSTRAP> <folder that holds devmem> <Event>`), in Claude Code's exec form (`command` + `args`):
no shell parses it, so paths with spaces need no quoting, and it works when `devmem` is not on PATH or not
installed. The bootstrap imports `devmem.hooks` from that folder and ALWAYS exits 0, whatever happens: the
folder was moved or deleted, devmem fails to import, or anything raises. That matters because Claude Code
treats exit code 2 as a blocking error (a UserPromptSubmit hook that exits 2 rejects the prompt, a Stop hook
keeps the model from stopping), and `python <missing script>` exits 2. A failure is written to
`devmem_hook_errors[_<uid>].log` in the temp folder. If the interpreter itself is gone, Claude Code cannot
start the hook, which it documents as a non-blocking error. Earlier versions installed
`python <abs path>/devmem/hooks.py <Event>`; `init` replaces that shape and `uninstall` removes both.
Because the paths are machine-specific, `settings_file="settings.local.json"` (Claude Code's personal,
git-ignored project settings) is the better choice when `.claude/settings.json` is shared with a team.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .plugin import ERROR_LOG_LINES, ERROR_LOG_PREFIX
from .store import (ADOPT_HINT, GITIGNORE_CAP, SHIPPED_HINT, STORE_DIR, ensure_store, gitignore_problem, not_a_project,
                    private_mkdir, read_regular, read_store_text, shipped_store, store_links, write_private)

# event -> timeout in seconds. Claude Code's hooks reference (checked 2026-11-14): SessionEnd hooks share a 1.5 s
# budget, and a longer per-hook `timeout` raises that budget to match, up to 60 s; so SessionEnd's 15 s is in force.
# It does not say whether this differs for plugin hooks; the plugin's hooks.json uses the same values.
HOOK_EVENTS: dict[str, int] = {
    "SessionStart": 30, "UserPromptSubmit": 60, "PostToolUse": 15, "PostToolUseFailure": 15,
    "Stop": 30, "StopFailure": 10, "PostCompact": 10, "SessionEnd": 15,
}
GITIGNORE = "# Written by devmem init: DevMemory's store stays local and is never committed.\n*\n"
_MARKERS = ("devmem.hooks", "devmem/hooks.py", "devmem\\hooks.py")

# The hook bootstrap, run with `python -c`. argv: [<folder that holds the devmem package>, <Event>, ...].
# It must never exit with code 2 (a blocking error for Claude Code), so everything, including the import of
# devmem, runs inside try/except and the process ends with os._exit(0). The working directory (the user's
# project) is dropped from sys.path so a project file such as json.py cannot shadow a module devmem needs.
# Written without double quotes or backslashes, so it survives the exec, bash and PowerShell forms unchanged.
_BOOT_LINES = [
    "try:",
    " if not os.path.isfile(D):",   # moved or deleted: never fall back to another installed devmem
    "  raise ImportError(D)",
    " from devmem.hooks import main",
    " main(sys.argv[1:])",
    "except BaseException as e:",
    " try:",
    *ERROR_LOG_LINES,                # devmem.plugin: no link, 0600, a regular file this user owns
    " except BaseException:",
    "  pass",
]
BOOTSTRAP = ("import sys,os;R=sys.argv.pop(1);D=os.path.join(R,'devmem','hooks.py');"
             "L='devmem_hook_errors'+('_'+str(os.getuid()) if hasattr(os,'getuid') else '')+'.log';"
             + ERROR_LOG_PREFIX + "sys.path[:]=[R]+[p for p in sys.path if p];"
             "exec(chr(10).join(" + repr(_BOOT_LINES) + "));os._exit(0)")
assert not any(c in BOOTSTRAP for c in '"\\$`')


def hooks_script() -> Path:
    return Path(__file__).resolve().parent / "hooks.py"


def package_root() -> Path:
    """The folder that holds the `devmem` package (what the bootstrap puts first on sys.path)."""
    return Path(__file__).resolve().parent.parent


def hook_handler(event: str, python: str | None = None, form: str = "exec",
                 store: Path | None = None, async_: bool = False) -> dict:
    """One Claude Code hook handler for `event`: `python -c BOOTSTRAP <package root> <event> [--store DIR]`.
    form="exec" (default, documented `args` exec form; no shell, no quoting issues);
    form="bash" or "powershell": a shell-form command string for Claude Code versions without `args`."""
    py = str(Path(python or sys.executable))
    root = str(package_root())
    extra = ["--store", str(store)] if store else []
    h: dict = {"type": "command"}
    if form == "exec":
        h["command"] = py
        h["args"] = ["-c", BOOTSTRAP, root, event, *extra]
    elif form == "bash":
        q = lambda s: "'" + s.replace("\\", "/").replace("'", "'\\''") + "'"  # noqa: E731  (paths)
        # BOOTSTRAP has no $, backtick, backslash or double quote, so bash double quotes pass it unchanged
        h["command"] = " ".join([q(py), "-c", '"' + BOOTSTRAP + '"', q(root), event, *map(q, extra)])
        h["shell"] = "bash"
    elif form == "powershell":
        q = lambda s: "'" + s.replace("'", "''") + "'"  # noqa: E731
        h["command"] = "& " + " ".join([q(py), "-c", q(BOOTSTRAP), q(root), event, *map(q, extra)])
        h["shell"] = "powershell"
    else:
        raise ValueError(f"unknown form {form!r}")
    h["timeout"] = HOOK_EVENTS.get(event, 30)
    if async_:
        h["async"] = True
    return h


def is_devmem_handler(h) -> bool:
    """A handler DevMemory installed (current or older `devmem hook` CLI forms)."""
    if not isinstance(h, dict):
        return False
    text = " ".join([str(h.get("command", ""))] + [str(a) for a in h.get("args") or []])
    if any(m in text for m in _MARKERS):
        return True
    norm = " ".join(text.replace("\\", "/").split())
    return " devmem hook" in f" {norm}" or "devmem.cli hook" in norm or "devmem/cli.py hook" in norm


def handler_paths(h) -> dict:
    """What an installed DevMemory handler runs, for checks such as `devmem doctor`:
    {"shape": "bootstrap" | "script" | "other", "python": interpreter or None,
     "hooks_py": the devmem/hooks.py it will run or None, "event": event name or None}.
    "bootstrap": the current `python -c BOOTSTRAP <root> <Event>` (exec form; a missing hooks_py is skipped
    silently by the bootstrap, so the hook records nothing but never blocks); "script": the older
    `python <path>/devmem/hooks.py <Event>` (a missing hooks_py makes Python exit 2, which blocks prompts)."""
    out = {"shape": "other", "python": None, "hooks_py": None, "event": None}
    if not isinstance(h, dict):
        return out
    args = [str(a) for a in h.get("args") or []]
    if args[:1] == ["-c"] and len(args) >= 4 and "devmem.hooks" in args[1]:
        return {"shape": "bootstrap", "python": h.get("command"),
                "hooks_py": str(Path(args[2]) / "devmem" / "hooks.py"), "event": args[3]}
    if args and args[0].replace("\\", "/").endswith("devmem/hooks.py"):
        return {"shape": "script", "python": h.get("command"), "hooks_py": args[0],
                "event": args[1] if len(args) > 1 else None}
    return out


SETTINGS_CAP = 16 * 1024 * 1024


def _read_settings(path: Path, project: Path | None = None) -> bytes | None:
    """The bytes of a project settings file (None when missing), at most SETTINGS_CAP, never blocking (no FIFO or
    device is read). project=None (hooks, status): only a plain file; a link, FIFO or folder there raises ValueError.
    project given (`devmem init` / `devmem uninstall`, run by the user): a link is never followed. A settings file or
    a `.claude` folder that is a symbolic link or a junction raises ValueError and nothing is read, wherever it leads:
    out of the project a cloned repository can ship `.claude/settings.json -> ~/.claude/settings.json` (init would
    copy the user's own settings, env values included, into a project file that is usually committed), and inside it
    `-> ../credentials.json` or `-> ../.env` (init would copy a secret file into its backup and into the settings
    file). The path is checked once and opened without following a link; the opened file is then checked to be that
    file, so a link swapped in between cannot bring other content in. A `.claude` folder that is a link is refused
    also when the settings file does not exist yet (init would create it there)."""
    if project is None:
        if not os.path.lexists(path):
            return None
        data = read_regular(path, SETTINGS_CAP)
        if data is None:
            raise ValueError(f"{path} is not a plain file (a link, FIFO or folder?) or is too large; not modified")
        return data
    from .consent import _inside
    from .store import _is_link, _open_regular, read_bounded
    real = os.path.realpath(path)
    hint = (" or run `devmem init` without --shared, which uses .claude/settings.local.json"
            if Path(path).name == "settings.json" else "")
    if not _inside(Path(real), project):
        what = "is a link leading" if os.path.islink(path) else "is in a linked folder leading"
        raise ValueError(f"{path} {what} outside the project (to {real}); devmem does not read or write through it "
                         f"and nothing was changed. Replace the link with a real file or folder{hint}")
    for p in (Path(path).parent, Path(path)):
        if _is_link(p):
            raise ValueError(f"{p} is a link or junction (to {os.path.realpath(p)}); devmem init and uninstall never "
                             f"read or write a settings file through a link, and nothing was changed. Replace the "
                             f"link with a real file (or folder){hint}")
    if not os.path.lexists(path):
        return None
    opened = _open_regular(real, SETTINGS_CAP)
    if opened is None:
        raise ValueError(f"{path} is not a regular file (a FIFO or folder?) or is too large; not modified")
    fd, size = opened
    with os.fdopen(fd, "rb") as f:
        try:
            proc_fd = f"/proc/self/fd/{fd}"
            where = os.readlink(proc_fd) if os.path.islink(proc_fd) else os.path.realpath(real)
            same = os.path.samestat(os.fstat(fd), os.stat(real)) and os.path.normcase(where) == os.path.normcase(real)
        except OSError:
            same = False
        if not same:
            raise ValueError(f"{path} changed while it was being read; not modified")
        data = read_bounded(f, size, SETTINGS_CAP + 1)    # the file's size, not the cap, asked for up front
    if len(data) > SETTINGS_CAP:
        raise ValueError(f"{path} is too large; not modified")
    return data


def _load_settings(path: Path, project: Path | None = None) -> dict:
    """A project settings file as a dict ({} when missing). How it is read, and which links are followed:
    _read_settings (`project` given only by `devmem init` / `devmem uninstall`)."""
    return _parse_settings(path, _read_settings(path, project))


def _parse_settings(path: Path, data: bytes | None) -> dict:
    if data is None:
        return {}
    text = data.decode("utf-8-sig", errors="replace")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(f"{path} is not valid JSON ({e}); not modified") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object; not modified")
    _check_shape(path, data)
    return data


SETTINGS_OBJECTS = ("hooks", "env", "permissions")             # Claude Code settings keys that hold an object
CREDENTIAL_KEYS = ("private_key", "private_key_id", "client_secret", "client_email", "refresh_token",
                   "access_token", "aws_secret_access_key", "secret_access_key", "password", "api_key", "token")


def _check_shape(path: Path, data: dict) -> None:
    """A settings file devmem may copy and rewrite: the keys devmem and Claude Code read as objects are objects, env
    values are text, and nothing at the top looks like a credential file (a service-account key, a token file).
    Raises ValueError otherwise, before anything is copied or written."""
    for k in SETTINGS_OBJECTS:
        if k in data and not isinstance(data[k], dict):
            raise ValueError(f"\"{k}\" in {path} is not an object; not modified")
    env = data.get("env")
    if isinstance(env, dict) and any(not isinstance(v, str) for v in env.values()):
        raise ValueError(f"\"env\" in {path} holds a value that is not text; not modified")
    odd = sorted(k for k in data if isinstance(k, str) and k.lower() in CREDENTIAL_KEYS)
    if odd or data.get("type") == "service_account":
        raise ValueError(f"{path} is not a Claude Code settings file (it holds {', '.join(odd) or 'type'}, as a "
                         "credentials file does); devmem does not copy or rewrite it, and nothing was changed")


def merge_hooks(settings: dict, handlers: dict[str, dict]) -> dict:
    """Return a copy of `settings` with DevMemory's handlers set for each event and every other entry kept."""
    out = json.loads(json.dumps(settings))
    hooks = out.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise ValueError("settings 'hooks' is not an object; not modified")
    for event in list(hooks):
        hooks[event] = _without_ours(hooks[event])
        if not hooks[event] and event in handlers:
            del hooks[event]
    for event, h in handlers.items():
        hooks.setdefault(event, [])
        hooks[event].append({"hooks": [h]})
    for event in [e for e, groups in hooks.items() if groups == []]:
        del hooks[event]
    out["hooks"] = hooks
    return out


def _without_ours(groups):
    if not isinstance(groups, list):
        return groups
    kept = []
    for g in groups:
        if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
            kept.append(g)
            continue
        hs = [h for h in g["hooks"] if not is_devmem_handler(h)]
        if len(hs) == len(g["hooks"]):
            kept.append(g)
        elif hs:
            kept.append({**g, "hooks": hs})
    return kept


def remove_hooks(settings: dict) -> dict:
    out = json.loads(json.dumps(settings))
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event in list(hooks):
        hooks[event] = _without_ours(hooks[event])
        if hooks[event] == []:
            del hooks[event]
    if not hooks:
        del out["hooks"]
    return out


def _write_json(path: Path, data: dict) -> None:
    """The settings file through a temporary file created exclusively (O_EXCL, O_NOFOLLOW) after removing a leftover
    entry at that name, the entry and never its target: a repository can ship `settings.json.devmem-tmp -> elsewhere`.
    The usual permissions (0666 minus umask): it is a project file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".devmem-tmp")
    if os.path.lexists(tmp):
        os.unlink(tmp)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0), 0o666)
    with os.fdopen(fd, "wb") as f:
        f.write((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    os.replace(tmp, path)


CREATED_NOTE = "init-created.json"


def _created_path(project: Path) -> Path:
    return project / STORE_DIR / CREATED_NOTE


def _write_created(project: Path, created: dict) -> None:
    p = _created_path(project)
    if p.is_symlink():
        raise OSError(f"{p} is a symbolic link; devmem never writes through a link")
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    write_private(tmp, json.dumps(created, indent=1).encode("utf-8"))     # 0600 on POSIX
    os.replace(tmp, p)


def _read_created(project: Path) -> dict:
    try:
        from .store import read_store_text
        d = json.loads(read_store_text(_created_path(project), 1024 * 1024))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _check_project(project: Path) -> Path:
    """The resolved project folder; refuses a drive or file-system root, the home folder and any folder that contains
    it (`devmem.store.not_a_project`, the check `devmem connect` and `devmem setup` share)."""
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise ValueError(f"{project} is not a directory")
    why = not_a_project(project)
    if why == "root":
        raise ValueError(f"refusing to set up {project}: it is a drive or file-system root, not a project")
    if why == "home":
        # <home>/.claude/settings.json is the user's global Claude Code config, not a project's
        raise ValueError("refusing to install into the home directory (that is the global Claude Code config)")
    if why:
        raise ValueError(f"refusing to set up {project}: it contains your home folder, so a scan would index "
                         "everything in it. Start in a project folder")
    return project


class ShippedStore(ValueError):
    """The project already has a store that this user did not create (see devmem.store.shipped_store)."""

    def __init__(self, reasons: list[str], tracked: bool = False, note: str = ""):
        self.reasons, self.tracked = reasons, tracked
        super().__init__("; ".join(reasons) + ". " + (SHIPPED_HINT if tracked else ADOPT_HINT) + note)


LEGACY_NOTE = (". devmem hooks from an earlier devmem are installed here, but this user's switch for the project "
               "(kept outside the project since then) is missing, and nothing in the project can show that the store is "
               "yours: if it is, `devmem init --adopt-store` records the switch and recording resumes")


def prepare_store(project: Path, store: Path, recorded_here: bool, adopt_store: bool, note: str = "") -> None:
    """What `devmem init` and `devmem setup` do to a store before using it: refuse one the user did not make (unless
    adopted), refuse one with links in it (even when adopted: devmem never writes through a link), create it with
    its .gitignore, refuse when that .gitignore still cannot keep the store out of git, and note outside the project
    that the store is this user's (devmem.consent.claim_store, which devmem.consent.store_refusal reads). Raises
    ValueError.

    A store devmem made for this user (`devmem scan`, `begin`, `wrap`...: its note is in devmem's per-user folder,
    outside the project, devmem.consent.made_here) counts as recorded here, so only a file git tracks in it asks
    for --adopt-store."""
    if not adopt_store:
        from .consent import made_here
        recorded_here = recorded_here or made_here(store)
        reasons, tracked = shipped_store(project, store, recorded_here=recorded_here)
        if reasons:
            raise ShippedStore(reasons, tracked, note)
    links = store_links(store)
    if links:
        raise ValueError(f"the memory store holds symbolic links ({', '.join(links[:3])}); devmem never writes "
                         "through a link. Remove them (or the folder) and run the command again")
    ensure_store(store)
    if not Path(store).is_dir():
        raise ValueError(f"cannot create the memory store at {store}")
    problem = gitignore_problem(store)
    if problem:
        raise ValueError(f"{problem}. Replace it with a file holding the line `*` and run the command again")
    from .consent import claim_store
    try:   # outside the project: this store is the user's (adopted: with the files git tracks in it now)
        claim_store(store, project, adopt=adopt_store)
    except OSError as e:
        raise ValueError(f"cannot note in devmem's per-user folder that this store is yours ({e.strerror or e})") from None


SETTINGS_FILES = ("settings.local.json", "settings.json")


def installed_handlers(project: Path) -> list[dict]:
    """devmem handlers in the project's `.claude/settings.local.json` and `.claude/settings.json`:
    [{file, events, handlers}]. Read-only; an unreadable or malformed file counts as holding none."""
    out = []
    for name in SETTINGS_FILES:
        f = Path(project) / ".claude" / name
        try:
            hooks = _load_settings(f).get("hooks") or {}
        except (ValueError, OSError):
            continue
        events, handlers = [], []
        for ev, groups in hooks.items() if isinstance(hooks, dict) else []:
            for g in groups if isinstance(groups, list) else []:
                for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
                    if is_devmem_handler(h):
                        events.append(ev)
                        handlers.append(h)
        if events:
            out.append({"file": f, "events": events, "handlers": handlers})
    return out


def handler_can_run(project: Path, handler, settings_file: Path | None = None) -> bool:
    """Can this init handler record at all? Its shape is the bootstrap or the older script form (never a shell-form
    command that merely mentions devmem: those cannot be checked), and its interpreter and the devmem/hooks.py it
    names are absolute paths that exist (`devmem init` always writes absolute ones). This says nothing about consent:
    that is the user's switch outside the project (devmem.consent.init_enabled); a handler in a committed settings
    file can pass this check."""
    info = handler_paths(handler)
    if info["shape"] not in ("bootstrap", "script"):
        return False
    py, hooks_py = info.get("python"), info.get("hooks_py")
    return bool(py and hooks_py and os.path.isabs(py) and os.path.isabs(hooks_py)
                and os.path.isfile(py) and os.path.isfile(hooks_py))


def runnable_handlers(project: Path) -> list[dict]:
    """installed_handlers(), keeping only the handlers for which handler_can_run() holds (files with none dropped).
    The `devmem init` route records only when this is not empty AND the user's switch exists (devmem.plugin.
    init_handlers combines both)."""
    out = []
    for entry in installed_handlers(project):
        ok = [h for h in entry["handlers"] if handler_can_run(project, h, entry["file"])]
        if ok:
            out.append({**entry, "handlers": ok})
    return out


def _gitignore_state(gi: Path, existed: bool, before: str | None) -> str:
    if not gi.is_file():
        return "could not write"
    if not existed:
        return "written"
    try:
        return "kept existing" if read_store_text(gi, GITIGNORE_CAP) == before else "fixed"
    except OSError:
        return "kept existing"


BACKUP_DIR = "init-backups"      # inside the store (git-ignored), like connect's connect-backups/


def settings_backup(store: Path, settings_file: str) -> Path:
    """Where `devmem init` keeps the copy of a settings file it changed for the first time: in the store, which git
    ignores. (Next to the settings file, git would show `settings.local.json.devmem-backup` as untracked.)"""
    return Path(store) / BACKUP_DIR / settings_file


def legacy_backup(project: Path, settings_file: str) -> Path:
    """Where devmem before this change kept that copy (left alone; `devmem doctor` mentions it)."""
    return Path(project) / ".claude" / (settings_file + ".devmem-backup")


def init_project(project: Path, store: Path | None = None, python: str | None = None, form: str = "exec",
                 settings_file: str = "settings.json", events: list[str] | None = None,
                 scan: bool = True, async_tools: bool = False, adopt_store: bool = False,
                 allow_unredacted: bool = False) -> dict:
    """Install capture hooks, write the store .gitignore, and run the first scan. Returns a report dict.

    async_tools=True installs PostToolUse/PostToolUseFailure with `"async": true` (documented): tool calls
    no longer wait for the hook (about 50 ms above interpreter start-up per call, measured on Windows), at
    the cost that in `claude -p` runs Claude Code kills async hooks still running at exit, so the last tool
    events of a headless run can be lost. Default: synchronous (complete record).

    An existing store with git-tracked files, or with any content when this user has not set the project up before
    (devmem.consent.set_up_here_before), raises ShippedStore (nothing is changed) unless adopt_store=True: a cloned
    repository can ship one. On success the user's switch is written outside the project (devmem.consent.enable_init):
    the installed hooks record nothing without it."""
    from . import consent
    project = _check_project(project)
    default_store = project / STORE_DIR
    store_p = Path(store).resolve() if store else default_store
    gi = store_p / ".gitignore"
    had_gitignore = os.path.lexists(gi)
    try:
        before = read_store_text(gi, GITIGNORE_CAP) if had_gitignore and not gi.is_symlink() else None
    except OSError:
        before = None
    consent.check_user_data(project)     # before anything is written: the switch must be writable
    settings_path = project / ".claude" / settings_file
    raw_settings = _read_settings(settings_path, project)    # before anything is written: a link out is refused
    current = _parse_settings(settings_path, raw_settings)
    legacy = consent.init_state(project) == "never" and bool(installed_handlers(project))
    prepare_store(project, store_p, consent.set_up_here_before(project), adopt_store, LEGACY_NOTE if legacy else "")
    report: dict = {"project": str(project), "store": str(store_p)}
    report["gitignore"] = _gitignore_state(gi, had_gitignore, before)
    try:   # the switch before the hook entries: hooks without a switch would record nothing, silently
        report["switch"] = str(consent.enable_init(project, allow_unredacted, store=store_p))
    except OSError as e:
        raise ValueError(f"cannot write this project's switch in devmem's per-user folder ({e.strerror or e}); the "
                         "hook entries were not written") from None
    report["allow_unredacted"] = allow_unredacted

    evs = list(events or HOOK_EVENTS)
    handlers = {e: hook_handler(e, python, form, store_p if store_p != default_store else None,
                                async_=async_tools and e in ("PostToolUse", "PostToolUseFailure")) for e in evs}
    merged = merge_hooks(current, handlers)
    changed = merged != current
    if changed:
        if not settings_path.exists():   # remember what init created, so uninstall can take it away again
            created = _read_created(project) if store_p == default_store else {}
            entry = created.setdefault(settings_file, {})
            entry["file"] = True
            if not settings_path.parent.exists():
                entry["dir"] = True
            try:
                _write_created(project, created)
            except OSError:
                pass
        if raw_settings is not None:
            backup = settings_backup(store_p, settings_file)
            if not backup.exists() and not legacy_backup(project, settings_file).exists():
                if os.path.islink(backup.parent) or os.path.islink(backup):
                    raise ValueError(f"{backup.parent} is a symbolic link; devmem never writes through a link")
                private_mkdir(backup.parent)
                # the bytes read above, written to a new 0600 file (O_EXCL, O_NOFOLLOW): a copy-then-chmod left it
                # readable by others (umask 022: 0644, 000: 0666) until the chmod. Not read again: one resolve, one read
                write_private(backup, raw_settings)
                report["backup"] = str(backup)
        _write_json(settings_path, merged)
    report.update(settings=str(settings_path), settings_changed=changed, events=evs,
                  command=handlers[evs[0]]["command"], args=handlers[evs[0]].get("args"))

    if scan:
        try:
            from .scan import scan as run_scan
            m = run_scan(project, store_p)
            report["scan"] = {"files": len(m.get("files", {}))}
        except Exception as e:  # noqa: BLE001 - hooks are installed; report the scan problem
            report["scan"] = {"error": f"{type(e).__name__}: {e}"}
    return report


def uninstall(project: Path, settings_file: str = "settings.json") -> dict:
    """Remove DevMemory's hook handlers from the project settings and turn this user's `devmem init` switch off (it
    becomes `<key>.off`, so a later setup of either route needs no --adopt-store). The memory store is left in place."""
    from .consent import disable_init
    project = _check_project(project)
    settings_path = project / ".claude" / settings_file
    current = _load_settings(settings_path, project)    # first: a link out of the project is refused, nothing changed
    try:
        disable_init(project)
    except OSError:
        pass
    cleaned = remove_hooks(current)
    changed = cleaned != current
    if changed:
        _write_json(settings_path, cleaned)
    report = {"settings": str(settings_path), "settings_changed": changed}
    created = _read_created(project)
    entry = created.get(settings_file) or {}
    if cleaned == {} and entry.get("file") and settings_path.exists():
        try:
            settings_path.unlink()
            report["settings_removed"] = True
            if entry.get("dir"):
                settings_path.parent.rmdir()        # only succeeds when the folder is empty
                report["claude_dir_removed"] = True
        except OSError:
            pass
        if not settings_path.exists():
            created.pop(settings_file, None)
            try:
                _write_created(project, created)
            except OSError:
                pass
    return report
