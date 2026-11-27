"""On-disk layout of the memory store (plain files under <project>/.devmemory/), file listing and hashing.

Which files belong to a project:
  1. In a git work tree, git decides: tracked files plus untracked files that are not ignored
     (`git ls-files --cached --others --exclude-standard`), so every .gitignore, .git/info/exclude and
     the user's global excludes apply exactly as git applies them.
  2. Without git (not installed, not a work tree, or git lists nothing because the folder itself is
     ignored by an enclosing repository), the folder is walked and `.gitignore` files are read with
     the common subset of gitignore syntax (see `GitIgnore`).
  3. In both cases the built-in ignore list (DEFAULT_IGNORES: VCS/store/vendor/build folders and binary
     types) and the store's `ignore` file apply on top, and the secret patterns in HARD_IGNORES are a
     floor that nothing can switch off.
Symbolic links are skipped in both modes, so the index never follows a link out of the project.
"""
from __future__ import annotations

import errno
import fnmatch
import hashlib
import os
import re
import stat as _stat
import sys
import time
from pathlib import Path

STORE_DIR = ".devmemory"
# secrets: never listed, read, hashed or indexed, regardless of git or user settings
HARD_IGNORES = [
    ".git", STORE_DIR,
    ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "*secret*",
    "*credentials*", "*.p12", "*.pfx", "*.keystore", "*.jks", ".netrc", "_netrc", ".pgpass", ".pypirc",
    ".npmrc", "*.kdbx", ".htpasswd", ".envrc", "*.ppk", "*.tfstate", "*.tfstate.backup", ".dev.vars",
    "local.settings.json", ".vault-token", ".my.cnf", ".s3cfg", "kubeconfig", "*.kubeconfig", "*.p8",
    "service-account*.json", "*.env", ".env-*", ".env_*", ".env~", "terraform.tfvars", "terraform.tfvars.json",
    "*.auto.tfvars", "*.auto.tfvars.json", ".dockercfg", "wp-config.php",
]
DEFAULT_IGNORES = [
    ".git", STORE_DIR, ".claude", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "out",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox", "*.egg-info",
    "*.pyc", "*.pyo", "*.rbxl", "*.rbxlx", "*.rbxm", "*.rbxmx", "*.rbxl.lock", "*.png", "*.jpg", "*.jpeg",
    "*.gif", "*.webp", "*.ico", "*.ogg", "*.mp3", "*.wav", "*.mp4", "*.mov", "*.fbx", "*.obj", "*.zip",
    "*.gz", "*.tar", "*.7z", "*.exe", "*.dll", "*.so", "*.dylib", "*.db", "*.sqlite", "*.sqlite3",
] + [p for p in HARD_IGNORES if p not in (".git", STORE_DIR)]
MAX_TEXT_BYTES = 512_000


def store_path(project: Path) -> Path:
    return Path(project) / STORE_DIR


STORE_GITIGNORE = "# Written by devmem init: DevMemory's store stays local and is never committed.\n*\n"


def gitignore_ignores_all(text: str) -> bool:
    """True when a `.gitignore` text ignores everything below its folder: its last pattern is `*`, so no
    earlier line can re-include anything (in gitignore the last matching pattern wins; a `!` line before the
    `*` is overridden by it). Trailing spaces are not significant in gitignore; leading ones are."""
    patterns = [ln.rstrip() for ln in text.splitlines()]
    patterns = [ln for ln in patterns if ln and not ln.startswith("#")]
    return bool(patterns) and patterns[-1] == "*"


class StoreRefused(OSError):
    """devmem will not write this store (a link where it writes, or a folder that is not a store)."""


class StoreLinkError(StoreRefused):
    """The store, or a folder devmem writes in (index/, sessions/, live/, ...), is a symbolic link or a junction.
    devmem never writes through one: a cloned repository can ship `.devmemory -> anywhere`."""


_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003   # a Windows junction


def _is_link(p) -> bool:
    """A symbolic link or a Windows junction (also on Python 3.10/3.11, which have no os.path.isjunction)."""
    try:
        if os.path.islink(p):
            return True
        isjunction = getattr(os.path, "isjunction", None)   # Python 3.12+
        if isjunction is not None:
            return bool(isjunction(p))
        return os.name == "nt" and getattr(os.lstat(p), "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT
    except (OSError, ValueError):
        return False


def store_dir_links(store: Path) -> list[str]:
    """The store folder and the folders devmem writes in (LINK_DIRS) that are links, checked with one lstat each
    (cheap enough for every hook event). Links among the files inside those folders are handled where the files are
    written (replace, not follow; appends opened with O_NOFOLLOW; SQLite falls back to memory); `store_links` lists
    those too for the commands that set a store up."""
    store = Path(store)
    if _is_link(store):
        return [str(store)]
    return [str(store / d) for d in LINK_DIRS if _is_link(store / d)]


def check_store_links(store: Path) -> None:
    """Raise StoreLinkError when the store or a folder devmem writes in is a link."""
    links = store_dir_links(store)
    if links:
        raise StoreLinkError(f"the memory store {store} is or holds a symbolic link ({links[0]}); devmem never writes "
                             "through a link. Remove the link (or the folder) and run the command again")


class NotAStoreError(StoreRefused):
    """A `--store` folder that is not (and cannot become) a devmem store: the home folder, a root, or an existing
    folder with other content. devmem never writes its `*` .gitignore, map or index into such a folder."""


# what devmem itself puts at the top of a store (anything else means the folder is something else)
STORE_ENTRIES = {".gitignore", "PROJECT.md", "config.json", "ignore", "redact", "hook_errors.log", "hook_timing.log",
                 "mcp_calls.jsonl", "packs.jsonl", "index", "live", "sessions", "pending", "pending.json", "connect-backups",
                 "init-backups"}


def is_store_folder(store: Path) -> bool:
    """True for a folder devmem may use as a store: missing or empty, named `.devmemory`, carrying devmem's own
    .gitignore, or holding only entries devmem writes there (and dot-files such as temporary files)."""
    store = Path(store)
    if not store.is_dir() or store.name == STORE_DIR:
        return True
    try:
        names = [e.name for e in os.scandir(store)]
    except OSError:
        return True    # unreadable: the writes below fail on their own
    gi = store / ".gitignore"
    if ".gitignore" in names and _regular(gi):
        try:
            head = read_store_text(gi, GITIGNORE_CAP)[:4096]
            if "devmem" in head.lower() and gitignore_ignores_all(head):
                return True
        except OSError:
            pass
    return all(n in STORE_ENTRIES or n.startswith(".") for n in names)


def check_store_folder(store: Path) -> None:
    """Raise NotAStoreError for a store folder that is the home folder, a root or above home, or an existing folder
    that holds other things (`--store ~`, `--store .`, `--store src`)."""
    store = Path(store)
    why = not_a_project(store) if (store.exists() or chr(0) in str(store)) else None
    if why:
        raise NotAStoreError(f"{store} is {NOT_A_PROJECT_TEXT.get(why, why)}; it cannot hold a memory store")
    if not is_store_folder(store):
        raise NotAStoreError(f"{store} is not a devmem memory store (it already holds other files); devmem does not "
                             "write its store files there. Give --store an empty or new folder")


def ensure_store(store: Path) -> Path:
    """Create the store folder if needed and make `<store>/.gitignore` ignore everything in the store: write it
    when it is missing, and when one exists that does not end with an effective `*` line (a repository can ship
    a store whose .gitignore holds only a comment), append `*` and keep what else is there. Never writes through a
    link: a `.gitignore` that is a symbolic link (git does not read one either) is removed, the link itself and never
    its target, and the standard file is written; one that is a folder or another non-regular file is left alone
    (`gitignore_problem` reports it; `devmem setup`/`init` refuse such a store). Idempotent. Raises StoreLinkError
    when the store or a folder devmem writes in is a link (every path that writes the store comes through here);
    otherwise never raises (a store that cannot be written to is reported by `devmem doctor`, not by a crash here).
    A store created here is noted as this user's own outside the project (devmem.consent.claim_store)."""
    store = Path(store)
    check_store_links(store)
    check_store_folder(store)
    try:
        existed = store.is_dir()
        if existed:
            tighten_store(store)        # before private_mkdir narrows the folder: an open one is narrowed throughout
        private_mkdir(store)
        if not existed:
            try:
                from .consent import claim_store
                claim_store(store)
            except (OSError, ValueError):
                pass                    # no usable per-user folder: the store is not served (consent.store_refusal)
        gi = store / ".gitignore"
        if gi.is_symlink():
            gi.unlink()
        if not os.path.lexists(gi):
            write_private(gi, STORE_GITIGNORE.encode("utf-8"))
        elif _regular(gi):
            text = read_store_text(gi, GITIGNORE_CAP)
            if not gitignore_ignores_all(text):
                sep = "" if not text or text.endswith("\n") else "\n"
                with open_append(gi) as f:
                    f.write(sep + "# Added by devmem: the store stays local and is never committed.\n*\n")
    except OSError:
        pass
    return store


def _regular(p: Path) -> bool:
    try:
        return _stat.S_ISREG(os.lstat(p).st_mode)
    except OSError:
        return False


def gitignore_problem(store: Path) -> str | None:
    """Why `<store>/.gitignore` does not keep the store out of git, or None: missing, a link or a folder (git reads
    neither), or a file that does not end by ignoring everything."""
    gi = Path(store) / ".gitignore"
    if gi.is_symlink():
        return f"{gi} is a symbolic link; git does not read a linked .gitignore"
    if not os.path.lexists(gi):
        return f"{gi} is missing"
    if not _regular(gi):
        return f"{gi} is not a regular file (a folder?), so git does not read it and devmem cannot write it"
    try:
        if gitignore_ignores_all(read_store_text(gi, GITIGNORE_CAP)):
            return None
    except OSError as e:
        return f"{gi} cannot be read ({e.strerror or e})"
    return f"{gi} does not end with a `*` line, so git may commit the store"


LINK_DIRS = ("index", "sessions", "live", "pending", "connect-backups", "init-backups")


def store_links(store: Path) -> list[str]:
    """Symbolic links (and junctions) in the store devmem writes into: the store itself, its entries, and the entries
    of its index/, sessions/, live/, pending/ and connect-backups/ folders, other than `.gitignore` (which
    ensure_store replaces). devmem writes some files in place, so a link there could make it write elsewhere."""
    store = Path(store)
    found = []

    def is_link(p) -> bool:
        try:
            return p.is_symlink() or bool(getattr(p, "is_junction", lambda: False)())
        except OSError:
            return False
    if is_link(store):
        return [str(store)]
    if not store.is_dir():
        return []
    try:
        top = list(os.scandir(store))
    except OSError:
        return []
    for e in top:
        p = Path(e.path)
        if e.name == ".gitignore":
            continue
        if is_link(p):
            found.append(str(p))
        elif e.name in LINK_DIRS and e.is_dir(follow_symlinks=False):
            try:
                found += [x.path for x in os.scandir(p) if is_link(Path(x.path))]
            except OSError:
                pass
    return found


_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", ".")


def local_path(p) -> str:
    """One spelling of a local Windows path: `\\\\?\\C:\\x`, `\\\\.\\C:\\x`, `\\\\?\\UNC\\localhost\\C$\\x` and
    `\\\\localhost\\C$\\x` (also 127.0.0.1) all become `C:\\x`; `\\\\?\\UNC\\host\\share` becomes `\\\\host\\share`.
    Elsewhere the path is returned unchanged (as a string)."""
    s = os.fspath(p)
    if os.name != "nt":
        return s
    s = s.replace("/", "\\")
    low = s.lower()
    if low.startswith("\\\\?\\unc\\"):
        s, low = "\\\\" + s[8:], "\\\\" + low[8:]
    elif low.startswith(("\\\\?\\", "\\\\.\\")) and re.match(r"[a-z]:(\\|$)", low[4:]):
        # only a drive path is unwrapped: `\\?\Volume{...}\x` or `\\?\GLOBALROOT\...` would become a relative path
        s, low = s[4:], low[4:]
    if low.startswith("\\\\"):
        parts = s[2:].split("\\")
        if len(parts) >= 2 and parts[0].lower() in _LOCAL_HOSTS and len(parts[1]) == 2 and parts[1][1] == "$":
            s = parts[1][0] + ":\\" + "\\".join(parts[2:])
    return s


def is_remote_path(p) -> bool:
    """Does `p` name a network location (`\\\\host\\share\\...`, `//host/share/...`, `\\\\?\\UNC\\host\\...`)? On Windows,
    resolving, stat-ing or opening one contacts that host (seconds, and an authentication attempt), so a path that
    comes from a hook payload, a tool input or a record is only handled as text when this is true. Local device forms
    (`\\\\?\\C:\\x`, `\\\\localhost\\C$\\x`) are folded by local_path first and are not remote. Checked on every platform
    (on Linux and macOS such a name is unusual anyway)."""
    try:
        s = os.fspath(p)
    except TypeError:
        return False
    if not isinstance(s, str):
        return False
    s = local_path(s) if os.name == "nt" else s
    return s.startswith(("\\\\", "//")) or s.lower().startswith(("\\\\?\\unc\\", "//?/unc/"))


def _identity(p) -> tuple[int, int] | None:
    try:
        st = os.stat(p)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else None


NOT_A_PROJECT_TEXT = {"home": "your home folder", "root": "a drive or file-system root",
                      "above-home": "a folder that contains your home folder",
                      "device": "a device-namespace path (such as \\\\?\\Volume{...}\\...); use its drive-letter path",
                      "nul": "a path holding a NUL character, which no folder name can contain"}


def not_a_project(project: Path) -> str | None:
    """Why the folder cannot be used as a project (or hold a store), or None: "root" for a drive or file-system
    root, "home" for the home folder, "above-home" for a folder that contains it. Compared by identity (device and
    inode / file index), after local_path, so `\\\\?\\C:\\Users\\me`, `\\\\localhost\\C$\\Users\\me` or a link to the
    home folder are recognised too. Checked for every command's project folder (devmem.cli._project) and for a
    `--store` folder: such a folder holds the user's global configuration, and scanning it would index everything
    below it. A path holding a NUL character is refused ("nul"): no folder has such a name, and the OS calls below
    would raise on it."""
    if chr(0) in os.fspath(project):
        return "nul"
    spelled = local_path(project)
    if os.name == "nt" and spelled.startswith(("\\\\?\\", "\\\\.\\")):
        # a device-namespace spelling local_path cannot fold to a drive (`\\?\Volume{...}\Users\me`,
        # `\\?\GLOBALROOT\...`): compared by identity with the home folder, its parents and its drive's root
        # (os.stat works on those paths), and refused in any case: such a spelling is never needed for a project
        me = _identity(spelled)
        try:
            home = Path(local_path(Path.home())).resolve()
        except (OSError, RuntimeError):
            return "device"
        if me is not None:
            for label, other in [("home", home)] + [("above-home", h) for h in home.parents]:
                if _identity(other) == me:
                    return "root" if Path(other).parent == Path(other) else label
        return "device"
    norm = os.path.realpath(spelled)
    if os.path.dirname(norm) == norm or Path(norm).parent == Path(norm):
        return "root"
    try:
        home = Path(local_path(Path.home())).resolve()
    except (OSError, RuntimeError):
        return None
    me = _identity(norm)
    for label, other in [("home", home)] + [("above-home", h) for h in home.parents]:
        oid = _identity(other)
        if (me is not None and oid is not None and me == oid) or \
                os.path.normcase(norm) == os.path.normcase(str(other)):
            return "root" if label == "above-home" and Path(other).parent == Path(other) else label
    return None


def open_append(path):
    """A text file in the store opened for appending, never through a link: refused when `path` is a symbolic link
    (Windows) and opened with O_NOFOLLOW where the platform has it (a link arriving between the check and the open
    fails there too). Created 0600 on POSIX, and an older, wider file is narrowed."""
    if os.path.islink(path):
        raise OSError(f"{path} is a symbolic link; devmem never writes through a link")
    return os.fdopen(open_append_fd(path), "a", encoding="utf-8", errors="replace")


def open_append_fd(path) -> int:
    """A store file opened for appending (O_APPEND, created 0600, O_NOFOLLOW where the platform has it), refused
    unless it is a regular file. O_NONBLOCK: a FIFO a repository or another user planted at the name (a live log, a
    log file) fails at once or is refused here, instead of blocking every hook until its timeout; it changes nothing
    for a regular file."""
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0), PRIVATE_FILE)
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file; devmem does not write to it")
        private_fd(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


# ---- private permissions (POSIX) ---------------------------------------------------------------------------
# What devmem keeps (the store, its per-user folder, backups) holds prompts, code and tool output. With the usual umask
# 022 it would be readable by every user of the machine, so folders are created 0700 and files 0600, and older ones are
# narrowed when devmem next writes there. Nothing changes on Windows (per-user ACLs).
PRIVATE_DIR = 0o700
PRIVATE_FILE = 0o600


def tighten(path) -> bool:
    """POSIX: remove group and other permissions from a file or folder this user owns (never through a link). True when
    something was changed. Never raises; does nothing on Windows."""
    if os.name == "nt":
        return False
    try:
        st = os.lstat(path)
        if _stat.S_ISLNK(st.st_mode) or st.st_uid != os.geteuid() or not (st.st_mode & 0o077):
            return False
        os.chmod(path, _stat.S_IMODE(st.st_mode) & ~0o077)
        return True
    except (OSError, ValueError, AttributeError):
        return False


def private_fd(fd: int) -> None:
    """tighten() for an open file (fchmod, so a name swapped in between cannot redirect it)."""
    if os.name == "nt":
        return
    try:
        st = os.fstat(fd)
        if st.st_uid == os.geteuid() and st.st_mode & 0o077:
            os.fchmod(fd, _stat.S_IMODE(st.st_mode) & ~0o077)
    except (OSError, AttributeError):
        pass


def private_mkdir(path) -> Path:
    """mkdir -p for a folder devmem keeps: created 0700 on POSIX (missing parents too), an existing one narrowed."""
    path = Path(path)
    if os.name == "nt":
        path.mkdir(parents=True, exist_ok=True)
        return path
    try:
        os.mkdir(path, PRIVATE_DIR)
    except FileExistsError:
        if not path.is_dir():
            raise
    except FileNotFoundError:
        if path.parent == path:
            raise
        private_mkdir(path.parent)
        try:
            os.mkdir(path, PRIVATE_DIR)
        except FileExistsError:
            pass
    tighten(path)
    return path


def write_private(path, data: bytes) -> None:
    """Write `data` to a new file at `path` (a temporary name the caller renames into place): created 0600, exclusive
    (O_EXCL, so an entry that appeared there, or a planted link, is never opened). A leftover at that name is removed
    first, the entry itself and never its target."""
    if os.path.lexists(path):
        os.unlink(path)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0), PRIVATE_FILE)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def private_sqlite(path) -> None:
    """Before SQLite opens `path`: create it empty and 0600 when missing (SQLite gives its -wal and -shm files the
    database's permissions), and narrow an existing database and its companions."""
    if os.name == "nt":
        return
    p = os.fspath(path)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), PRIVATE_FILE)
        os.close(fd)
    except OSError:
        pass
    for suffix in ("", "-wal", "-shm", "-journal"):
        tighten(p + suffix)


def tighten_store(store: Path) -> None:
    """Narrow an existing store: the store folder, the folders devmem writes in and the files at its top level, with
    a few lstat calls. Only when the store folder itself was open to others (a store from an older devmem) is
    everything below it narrowed, once."""
    if os.name == "nt":
        return
    store = Path(store)
    if tighten(store):
        for root, dirs, files in os.walk(store):
            for n in dirs + files:
                tighten(os.path.join(root, n))
        return
    try:
        top = list(os.scandir(store))
    except OSError:
        return
    for e in top:
        tighten(e.path)


def loose_permissions(path) -> str | None:
    """POSIX: "group/others can read" style text when the folder or file `path` is open to other users, else None."""
    if os.name == "nt":
        return None
    try:
        mode = _stat.S_IMODE(os.lstat(path).st_mode)
    except OSError:
        return None
    if not mode & 0o077:
        return None
    return f"mode {mode:o}: other users of this computer can {'read' if mode & 0o044 else 'enter'} it"


# ---- programs devmem starts --------------------------------------------------------------------------------
# On Windows, CreateProcess and shutil.which (Python 3.10/3.11 always, 3.12+ unless this variable is set) look in the
# current folder before PATH, so `git.exe` or `clip.bat` in a cloned project would run the first time devmem is used
# there from an ordinary terminal. Every program devmem starts is resolved by find_program, from absolute PATH entries
# only, and started by that absolute path; the variable below is a second layer for the programs those start.
if os.name == "nt":
    os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")

_PROGRAMS: dict[tuple, str | None] = {}


def _path_dirs(path: str | None = None) -> list[str]:
    """The PATH folders a program may come from: absolute entries only ("", "." and relative entries name the
    current folder, wherever devmem happens to run)."""
    out = []
    for d in (os.environ.get("PATH", "") if path is None else path).split(os.pathsep):
        d = d.strip().strip('"')
        if d and os.path.isabs(d) and d not in out:
            out.append(d)
    return out


def _under(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(child), os.path.normcase(parent)]) == os.path.normcase(parent)
    except ValueError:   # different drives
        return False


def find_program(name: str, project=None, path: str | None = None) -> str | None:
    """Absolute path of program `name` found on PATH, or None. Never the current folder or a relative PATH entry,
    and never a program inside `project` (a cloned repository can ship one) or directly in the current folder.
    On Windows the PATHEXT extensions are tried. Cached per (name, PATH, folders)."""
    def canon(p: str) -> str:
        # one spelling for comparisons: a PATH entry written `\\?\C:\proj\bin` is the project's folder too
        return os.path.normcase(local_path(os.path.realpath(p)))
    try:
        cwd = canon(os.getcwd())
    except OSError:
        cwd = ""
    proj = canon(str(project)) if project else ""
    key = (name, os.environ.get("PATH", "") if path is None else path, cwd, proj)
    if key in _PROGRAMS:
        return _PROGRAMS[key]
    exts = [""]
    if os.name == "nt" and not os.path.splitext(name)[1]:
        exts = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e] or [".EXE"]
    found = None
    if os.path.basename(name) == name and name not in ("", ".", ".."):
        for d in _path_dirs(path):
            for e in exts:
                cand = os.path.join(d, name + e)
                if not (os.path.isfile(cand) and os.access(cand, os.X_OK)):
                    continue
                real, real_d = canon(cand), canon(d)
                if (cwd and os.path.dirname(real) == cwd) or (cwd and real_d == cwd) or \
                        (proj and (_under(real, proj) or _under(real_d, proj))):
                    continue
                found = os.path.abspath(cand)
                break
            if found:
                break
    _PROGRAMS[key] = found
    return found


# Options that keep git from running a program a project's own git configuration (an archive or clone can ship one)
# names: no fsmonitor, no hooks (core.hooksPath points at nothing; `--no-optional-locks` also keeps status from
# refreshing and writing the index), no pager, no external diff, no credential or ssh helper, no editor, no prompts,
# no work inside submodules (a submodule has a configuration of its own that the project's `git config` never lists;
# git_argv also adds `--ignore-submodules=all` to status and diff, which a `.gitmodules` setting cannot override).
# Filter drivers (filter.<name>.clean/smudge/process, which `git status` runs on changed files) are emptied by name;
# git itself lists the names (_filter_names). Aliases cannot replace the builtin commands devmem runs, and textconv
# is off where devmem runs diff or show (--no-textconv).
_GIT_SAFE = ("-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", "-c", "core.pager=cat",
             "-c", "diff.external=", "-c", "core.sshCommand=", "-c", "core.askPass=", "-c", "credential.helper=",
             "-c", "core.editor=", "-c", "core.gitProxy=", "-c", "protocol.allow=never",
             "-c", "submodule.recurse=false", "-c", "diff.ignoreSubmodules=all", "-c", "status.submoduleSummary=false",
             "--no-pager", "--no-optional-locks")
GIT_TIMEOUT = 5.0            # seconds one git command may take (its filter listing included), unless the caller names
                             # another limit
_GIT_COOLDOWN = 60.0         # after a git command ran into its limit, no devmem process starts git in that project for
                             # this long (_git_slow), and this process starts no git at all
_SLOW_DIR = "git-slow"       # <user data>/git-slow/<project key>: "git did not answer in time here", see _git_slow
_FILTER_TTL = 2.0            # the filter names of a project are asked once per burst of git commands
_MAX_FILTERS = 200           # more filter drivers than this in one repository: git is not run there
_TRUSTED_SCOPES = ("system", "global")    # the user's own configuration files; never the repository's
_FILTERS: dict[str, tuple[float, list[str] | None]] = {}
_git_down_until = 0.0


class GitUnsafe(FileNotFoundError):
    """git is not run in this project: its filter drivers cannot be listed or switched off, the project path or an
    argument holds a NUL character (no program can receive one), or git did not answer in time a moment ago."""


def _kill_tree(p) -> None:
    """Ends a git process and what it started (Git for Windows starts the real git from a small launcher)."""
    import signal
    import subprocess
    from . import proc
    if os.name == "nt":
        taskkill = os.path.join(os.environ.get("SystemRoot") or "C:\\Windows", "System32", "taskkill.exe")
        try:
            proc.run([taskkill, "/T", "/F", "/PID", str(p.pid)], capture_output=True, timeout=3,
                     stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)       # the child leads its own session (start_new_session)
        except OSError:
            pass
    try:
        p.kill()
    except OSError:
        pass


_SLOW_PATHS: dict[str, Path | None] = {}
_PRCTL: list = []


def _slow_marker(project) -> Path | None:
    """`<user data>/git-slow/<project key>` (devmem.consent names per-project files the same way), or None when
    devmem's per-user folder cannot be used for this project (relative, or inside the project: a project could then
    ship its own marker; this process's own cooldown still applies)."""
    key = os.path.abspath(str(project))
    if key not in _SLOW_PATHS:
        try:
            from .consent import _inside, project_key, user_data_dir
            base = user_data_dir()
            _SLOW_PATHS[key] = None if _inside(base, Path(project)) else base / _SLOW_DIR / project_key(project)
        except (OSError, ValueError, RuntimeError):
            _SLOW_PATHS[key] = None
    return _SLOW_PATHS[key]


def _git_slow(project) -> bool:
    """Did a git command in `project` run into its limit less than _GIT_COOLDOWN seconds ago, in any devmem process?
    Then no git is started there until the marker expires: an honest repository that was slow once (a loaded
    machine, a lock) is asked again a minute later."""
    m = _slow_marker(project)
    if m is None:
        return False
    try:
        import json
        at = float(json.loads((read_regular(m, 4096) or b"null").decode("utf-8"))["at"])
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return False
    return 0.0 <= time.time() - at < _GIT_COOLDOWN


def _note_git_slow(project) -> None:
    m = _slow_marker(project)
    if m is None:
        return
    try:
        import json
        private_mkdir(m.parent.parent)
        private_mkdir(m.parent)
        tmp = m.with_name(f"{m.name}.{os.getpid()}.tmp")
        write_private(tmp, json.dumps({"at": time.time(), "project": str(project)}).encode("utf-8"))
        os.replace(tmp, m)
    except (OSError, ValueError):
        pass


def _die_with_parent():
    """Linux: a preexec_fn that has the kernel kill git (SIGKILL) when the devmem process that started it ends, however
    it ends (PR_SET_PDEATHSIG; the refresh child killed at its budget, a hook killed by its client). None elsewhere,
    and in a process that runs more than one thread (preexec_fn is not safe there): such a process ends its git itself,
    at the limit."""
    if not sys.platform.startswith("linux"):
        return None
    threading = sys.modules.get("threading")
    if threading is not None and threading.active_count() > 1:
        return None
    if not _PRCTL:
        try:
            import ctypes
            _PRCTL.append(ctypes.CDLL(None, use_errno=True).prctl)
        except (OSError, AttributeError):
            _PRCTL.append(None)
    prctl, ppid = _PRCTL[0], os.getpid()
    if prctl is None:
        return None

    def fn() -> None:
        prctl(1, 9, 0, 0, 0)                 # PR_SET_PDEATHSIG, SIGKILL
        if os.getppid() != ppid:             # the parent ended before that: end now
            os._exit(1)
    return fn


class GitOutputLimit(OSError):
    """git wrote more than the caller reads (`max_out` bytes): git was stopped there."""


_ERR_KEEP = 64 * 1024        # bytes of git's error output kept (the rest is read and dropped)


def _communicate_capped(p, timeout: float, max_out: int) -> tuple[bytes, bytes]:
    """p.communicate() that holds at most `max_out` bytes of standard output (more: GitOutputLimit, as soon as it is
    read) and _ERR_KEEP of error output: both pipes are read by threads in pieces, and the process is waited for."""
    import subprocess
    import threading
    out: list[bytes] = []
    err: list[bytes] = []
    over = threading.Event()
    done = threading.Event()

    def drain(f, keep: list[bytes], cap: int, stop: bool) -> None:
        n = 0
        try:
            while True:
                b = f.read1(65536) if hasattr(f, "read1") else f.read(65536)
                if not b:
                    break
                n += len(b)
                if n <= cap:
                    keep.append(b)
                elif stop:
                    over.set()
                    done.set()
                    return
                else:
                    keep.append(b[:max(0, cap - (n - len(b)))])
        except (OSError, ValueError):
            pass
        if stop:
            done.set()

    threads = [threading.Thread(target=drain, args=(p.stdout, out, max_out, True), daemon=True),
               threading.Thread(target=drain, args=(p.stderr, err, _ERR_KEEP, False), daemon=True)]
    for t in threads:
        t.start()
    end = time.monotonic() + max(0.0, timeout)
    done.wait(max(0.0, end - time.monotonic()))
    if over.is_set():
        raise GitOutputLimit(f"git wrote more than {max_out} bytes")
    if not done.is_set():
        raise subprocess.TimeoutExpired(p.args, timeout)
    p.wait(timeout=max(0.0, end - time.monotonic()))
    threads[1].join(max(0.0, end - time.monotonic()) + 0.5)
    for f in (p.stdout, p.stderr):
        try:
            f.close()
        except OSError:
            pass
    return b"".join(out), b"".join(err)


def _git_exec(argv: list[str], timeout: float, max_out: int | None = None):
    """Runs one git command (argv from git_argv) and returns a CompletedProcess with bytes output; with `max_out`, at
    most that many bytes of standard output are held (more: git is stopped and GitOutputLimit raised). Never waits longer
    than `timeout` plus a moment: at the limit the process and its children are killed and subprocess.TimeoutExpired
    is raised; for _GIT_COOLDOWN seconds afterwards this process starts no git at all, and no devmem process starts
    git in the command's project (its `-C`: _git_slow), so that a git that hangs (a named pipe where it expects a file, a dead network
    drive) costs one limit per minute, not one per command or per prompt (GitUnsafe meanwhile). On Linux git also
    ends when this process ends (_die_with_parent)."""
    import subprocess
    from . import proc
    global _git_down_until
    if time.monotonic() < _git_down_until:
        raise GitUnsafe("git did not answer in time a moment ago; devmem does not wait for it again yet")
    project = argv[argv.index("-C") + 1] if "-C" in argv[:-1] else None      # git_argv puts it there, once
    if project is not None and _git_slow(project):
        raise GitUnsafe("git did not answer in time in this project less than a minute ago; devmem does not wait "
                        "for it again yet")
    kw: dict = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else \
        {"start_new_session": True}
    die = _die_with_parent()
    if die is not None:
        kw["preexec_fn"] = die
    p = proc.popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}, **kw)
    try:
        if max_out is None:
            out, err = p.communicate(timeout=max(0.0, timeout))
        else:
            out, err = _communicate_capped(p, timeout, max_out)
    except GitOutputLimit:
        _kill_tree(p)
        try:
            p.wait(timeout=1)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        raise
    except subprocess.TimeoutExpired:
        _git_down_until = time.monotonic() + _GIT_COOLDOWN
        _kill_tree(p)
        if project is not None:
            _note_git_slow(project)
        try:                               # a grandchild that survived may hold the pipes: do not wait for it
            p.communicate(timeout=1) if max_out is None else p.wait(timeout=1)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        raise
    except BaseException:
        _kill_tree(p)
        raise
    return subprocess.CompletedProcess(argv, p.returncode, out, err)


def _list_filter_names(git: str, project, deadline: float | None = None) -> list[str] | None:
    """See _filter_names. `deadline` (time.monotonic()): the listing gets the time left until then (at most
    GIT_TIMEOUT), so that it and the command it is for share one limit (git_run)."""
    import subprocess
    base = [git, *_GIT_SAFE, "-C", str(project), "config", "--name-only", "-z"]
    scoped = True

    def left() -> float:
        return GIT_TIMEOUT if deadline is None else min(GIT_TIMEOUT, deadline - time.monotonic())
    try:
        if left() <= 0:
            return None
        r = _git_exec([*base, "--show-scope", "--get-regexp", r"^filter\."], left())
        if r.returncode == 129:              # usage error: a git older than 2.26 does not know --show-scope
            scoped = False
            if left() <= 0:
                return None
            r = _git_exec([*base, "--get-regexp", r"^filter\."], left())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if r.returncode == 1:                    # no such key anywhere
        return []
    if r.returncode != 0:
        return None
    fields = r.stdout.decode("utf-8", "surrogateescape").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if scoped:
        if len(fields) % 2:
            return None
        pairs = list(zip(fields[0::2], fields[1::2]))
    else:
        pairs = [("", f) for f in fields]    # no scopes known: every driver is emptied, the user's own too
    names: list[str] = []
    for scope, key in pairs:
        if scope in _TRUSTED_SCOPES or not key.lower().startswith("filter.") or key.count(".") < 2:
            continue
        name = key[len("filter."):].rsplit(".", 1)[0]
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            return None
        if not name or "=" in name or any(ord(c) < 32 for c in name):
            return None                      # cannot be written as `-c filter.<name>.clean=`: git is not run
        if name not in names:
            names.append(name)
            if len(names) > _MAX_FILTERS:
                return None
    return names


def _filter_names(git: str, project, deadline: float | None = None) -> list[str] | None:
    """Names of the filter drivers that this repository's configuration defines, or None when they cannot be listed
    or one cannot be passed back to `git -c` safely (git is then not run here at all).

    git itself is asked, every time (`git config --show-scope --get-regexp`, which runs no program): it reads exactly
    the files the next git command will read, wherever they are (`.git/config`, `config.worktree`, a `commondir` or
    `gitdir:` redirect, include files, a folder laid out as a bare repository), so devmem does not have to know the
    places a repository can put configuration. Every driver defined outside the user's own system and global files
    is emptied; a driver of the user's own (git-lfs) keeps working unless the repository redefines a key of it. A
    failure to list (git too slow, unreadable configuration) stops git rather than running it unprotected. The answer
    is reused for _FILTER_TTL seconds, the few git commands of one hook event."""
    key = os.path.normcase(os.path.abspath(str(project)))
    hit = _FILTERS.get(key)
    if hit and time.monotonic() - hit[0] < _FILTER_TTL:
        return hit[1]
    names = _list_filter_names(git, project, deadline)
    _FILTERS[key] = (time.monotonic(), names)
    return names


def _submodules_off(args: tuple[str, ...]) -> list[str]:
    """`args` with `--ignore-submodules=all` after a `status` or `diff` command word: without it git starts a
    `git status` inside every submodule, under that submodule's own configuration."""
    out = list(args)
    i = 0
    while i < len(out):
        if out[i] == "-c":
            i += 2
            continue
        if out[i].startswith("-"):
            i += 1
            continue
        if out[i] in ("status", "diff") and not any(a.startswith("--ignore-submodules") for a in out[i + 1:]):
            out.insert(i + 1, "--ignore-submodules=all")
        break
    return out


def git_argv(project, *args: str, deadline: float | None = None) -> list[str]:
    """The argv of every git command devmem runs in a project: git by its absolute path (find_program, never from the
    project), the options in _GIT_SAFE, every filter driver the repository configures emptied, and submodules left
    alone. Raises FileNotFoundError when git is not on PATH, and GitUnsafe (a FileNotFoundError: callers then work as
    without git) when the filter drivers cannot be listed or switched off (also: not by `deadline`, a
    time.monotonic() value), git did not answer in time in this project less than a minute ago (_git_slow), or the
    project path or an argument holds a NUL character."""
    if chr(0) in str(project) or any(chr(0) in str(a) for a in args):
        raise GitUnsafe("a path or argument holds a NUL character; devmem does not run git with it")
    git = find_program("git", project)
    if not git:
        raise FileNotFoundError("git was not found on PATH")
    if _git_slow(project):
        raise GitUnsafe("git did not answer in time in this project less than a minute ago; devmem does not wait "
                        "for it again yet")
    names = _filter_names(git, project, deadline)
    if names is None:
        raise GitUnsafe("the filter drivers in this repository's git configuration could not be listed or switched "
                        "off; devmem does not run git here")
    off: list[str] = []
    for n in names:
        for k in ("clean", "smudge", "process"):
            off += ["-c", f"filter.{n}.{k}="]
        off += ["-c", f"filter.{n}.required=false"]
    return [git, *_GIT_SAFE, *off, "-C", str(project), *_submodules_off(args)]


def git_run(project, *args: str, timeout: float = GIT_TIMEOUT, max_out: int | None = None):
    """Runs `git <args>` in `project` through git_argv and _git_exec: the one way devmem starts git. `timeout` covers
    the whole call: the filter listing (git_argv) and the command share it, the command gets what is left. Returns a
    CompletedProcess with bytes output; raises OSError (FileNotFoundError, GitUnsafe, GitOutputLimit past `max_out`
    bytes of output) or subprocess.TimeoutExpired."""
    deadline = time.monotonic() + timeout
    argv = git_argv(project, *args, deadline=deadline)
    left = deadline - time.monotonic()
    if left <= 0:
        import subprocess
        raise subprocess.TimeoutExpired(argv, timeout)
    return _git_exec(argv, left, max_out)


SHIPPED_HINT = ("this repository ships a memory store; its contents would be added to your prompts. Look at it first; "
                "if you trust it, run the command again with --adopt-store (`devmem adopt` shows what it holds first). "
                "Otherwise delete the folder")
ADOPT_HINT = ("it was not created by this setup, and its contents would be added to your prompts. If it is yours (devmem "
              "0.3.0 or earlier made it, you wrote its ignore or config file by hand, moved or renamed the project, "
              "reinstalled the plugin, or devmem's per-user folder is another one now; a store this devmem made for you "
              "needs no flag) or you trust it, run the command again with --adopt-store (`devmem adopt` shows what it "
              "holds first). Otherwise delete the folder")


# ---- which entries of git's index land in a store
# A case-insensitive file system (Windows, macOS) writes `.DevMemory/sessions/x`, `.DEVMEMORY/...`, the 8.3 short name
# `DEVMEM~1/...` and, where a repository's own configuration switches core.protectNTFS off, `.devmemory./...`,
# `.devmemory::$INDEX_ALLOCATION/...` or `.devmemory\sessions\x` into the store `.devmemory`; `git ls-files -- <store>`
# lists none of them. So git is asked for every index entry that MIGHT land there (a broad pattern per spelling
# family), and each is placed by the file system itself: the folder at the store's depth is compared with the store
# by identity (device and inode / file index), never by its name. On Linux those spellings are other folders.
_FOLD_LETTERS = frozenset("fiklstFIKLST")   # ASCII letters that a non-ASCII letter equals under case folding
                                            # (K KELVIN SIGN, ſ LONG S, ı DOTLESS I, the ligatures ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ)


class GitNoAnswer(OSError):
    """git ran in the project but could not say what its index holds (its message is the text)."""


class GitLimit(GitNoAnswer):
    """The index holds more possible store entries than devmem looks through (reading the index file would not help)."""


def _name_glob(name: str) -> str:
    """A git glob for every name a case-insensitive, normalisation-insensitive file system may resolve to the folder
    `name` (used with icase magic, which folds ASCII letters only): non-ASCII characters (other cases, NFC/NFD), ASCII
    letters with a non-ASCII equal (_FOLD_LETTERS) and anything that is not a plain letter, digit, `.`, `-` or `_`
    become `*`. Anything after the name (trailing dots or spaces, `::$INDEX_ALLOCATION`) and before it (a folder joined
    with a backslash) is matched by the `*` around it in _store_pathspecs."""
    out: list[str] = []
    for c in name:
        keep = c.isascii() and (c.isalnum() or c in "._-") and c not in _FOLD_LETTERS
        ch = c if keep else "*"
        if not (ch == "*" and out and out[-1] == "*"):
            out.append(ch)
    return "".join(out)


def _store_pathspecs(names, depth: int | None = None, tilde: _ShortNames | None = None) -> list[str]:
    """Pathspecs for every index entry that may land in a folder named one of `names`, or be that folder: the name in
    any spelling (_name_glob) and an 8.3 short name (`~`): any `~` component when `tilde` is None, else the components
    `tilde` names (_tilde_rule: the store's own short name on Windows, the forms a generated one can take elsewhere).
    Without `depth`: from the top of the work tree, at any depth. With `depth` (the store is that many folders below
    the folder git runs in, which is the top of the work tree: _top_is): only entries with such a name in one of their
    first `depth` components (a component that holds the name with a backslash in it, or that is an 8.3 short name)."""
    specs = []
    for n in sorted(set(names) - {""}):
        g = _name_glob(n)
        if depth is None:
            specs += [f":(top,icase,glob)**/*{g}*", f":(top,icase,glob)**/*{g}*/**"]
        else:
            for j in range(depth):
                specs += [f":(top,icase,glob){'*/' * j}*{g}*", f":(top,icase,glob){'*/' * j}*{g}*/**"]
    globs, magic = (["*~*"], "glob") if tilde is None else (tilde.globs, "icase,glob")
    for c in sorted(HFS_IGNORED):       # HFS+ leaves these out when it compares names: `.dev<U+200C>memory` is the store
        if depth is None:
            specs.append(f":(top,glob)**/*{c}*/**")
        else:
            for j in range(depth):
                specs += [f":(top,glob){'*/' * j}*{c}*", f":(top,glob){'*/' * j}*{c}*/**"]
    for t in globs:
        if depth is None:
            specs.append(f":(top,{magic})**/{t}/**")
        else:
            for j in range(depth):
                specs += [f":(top,{magic}){'*/' * j}{t}", f":(top,{magic}){'*/' * j}{t}/**"]
    return specs


# Code points HFS+ ignores when it compares names (as devmem.gitindex._HFS_IGNORED): a component holding one may name
# any folder there, so git is asked for every entry holding one (honest repositories have next to none)
HFS_IGNORED = frozenset("\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u206a\u206b\u206c\u206d"
                        "\u206e\u206f\ufeff")


def _case_folds(parent: str, name: str) -> bool | None:
    """Does the file system holding `parent` take `name` in other letter case for the same folder (None: cannot be
    told)? One lstat of each spelling."""
    try:
        st = os.lstat(os.path.join(parent, name))
        me = (st.st_dev, st.st_ino) if st.st_ino else None
    except (OSError, ValueError):
        return None
    other = name.swapcase()
    if me is None or other == name:
        return None
    try:
        st = os.lstat(os.path.join(parent, other))
        return (st.st_dev, st.st_ino) == me
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None


class _ShortNames:
    """The path components that can be an 8.3 short name of one folder: `globs` for git's pathspecs (icase) and
    could_be(component) for the entries listed (devmem.store._Level, devmem.gitindex.index_candidates). A component
    that could be one is not taken for the folder: it is looked up on disk and compared by identity (_landing)."""
    __slots__ = ("globs", "_rx")

    def __init__(self, globs: list[str], rx: re.Pattern | None):
        self.globs, self._rx = globs, rx

    def could_be(self, c: str) -> bool:
        """As Windows resolves it: a stream suffix and trailing dots and spaces are dropped (as _may_alias)."""
        return self._rx is not None and self._rx.fullmatch(c.split(":", 1)[0].rstrip(" .")) is not None


_SHORT_SAFE = re.compile(r"[A-Z0-9!#$%&'()@^_`{}~-]*")   # what may stand in the base of an 8.3 name
MAX_SHORT_DIGITS = 6                                   # `~N` tails a generator writes: ~1 ... ~999999


def _generated_short_names(name: str) -> _ShortNames | None:
    """The 8.3 short names Windows (NTFS, FAT) and Linux's vfat can generate for a folder named `name`, wherever the
    process asking runs (a Linux process sees them through a Windows folder: a Docker bind mount, WSL's drvfs): the
    first letters of the name, upper case, then `~` and a number not starting with 0 (`DEVMEM~1`, `DEVME~12`), or its
    first two letters, four hexadecimal digits and `~` with one digit (`DE3F2A~1`), with an extension of up to three
    characters after it. None when the name has characters this does not model (the caller then lists every `~`
    entry). Committed folders like `d~000001/` or `d12~1/` are none of these for `.devmemory`: not even listed."""
    stem = name.lstrip(".")
    stem = (stem.rsplit(".", 1)[0] if "." in stem else stem).replace(" ", "").replace(".", "").upper()
    stem = re.sub(r"[+,;=\[\]]", "_", stem)
    if not stem or not stem.isascii() or not _SHORT_SAFE.fullmatch(stem):
        return None
    globs, forms = [], []
    for d in range(1, MAX_SHORT_DIGITS + 1):
        head = stem[:min(len(stem), 7 - d)]
        globs.append(f"{_glob_literal(head)}~[1-9]{'[0-9]' * (d - 1)}*")
        forms.append(f"{re.escape(head)}~[1-9][0-9]{{{d - 1}}}")
    two = stem[:2]
    globs.append(f"{_glob_literal(two)}{'[0-9a-f]' * 4}~[1-9]*")
    forms.append(f"{re.escape(two)}[0-9A-F]{{4}}~[1-9]")
    return _ShortNames(globs, re.compile("(?:" + "|".join(forms) + r")(?:\.[^.]{0,3})?", re.I))


def _glob_literal(text: str) -> str:
    """`text` as literal characters of a git glob."""
    return re.sub(r"([*?\[\]\\])", r"\\\1", text)


def _tilde_rule(store_real: str, names) -> _ShortNames | None:
    """Which index entries holding `~` git (or the index reader) must list for the store question: those whose
    component could be an 8.3 short name of the store's own folder, which an entry that lands in the store has to name
    (a parent's short name alone never reaches it). On Windows, where the store's short name can be asked for, only
    that name (none when it has none); elsewhere, every name a generator could have made for it
    (_generated_short_names: a Linux process on a case-sensitive Windows folder sees `DEVMEM~1` resolve to the store,
    though `.DEVMEMORY` does not; the same on Windows when the short name cannot be asked for). Each one listed is
    looked up on disk and compared by identity (_landing). None (every `~` entry) when this cannot be told."""
    if any("~" in n for n in names):
        return None
    name = os.path.basename(store_real)
    if os.name == "nt":
        known, short = _short_name(store_real)
        if not known:
            return _generated_short_names(name)
        if short is None:
            return _ShortNames([], None)
        return _ShortNames([f"*{_name_glob(short)}*"], re.compile(re.escape(short), re.I))
    return _generated_short_names(name)


_GIT_ENV_PLACES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")


def _top_is(where) -> bool:
    """Is `where` certainly the top of the work tree git uses when it runs there? Only for the plain layout: no git
    location in the environment, `where/.git` a real folder that git takes for a repository (HEAD, objects, refs),
    no commondir redirect, no config.worktree, and a `config` that names no work tree, include or bare repository.
    Anything else (a linked worktree, a submodule, an unusual configuration) is asked about with the broad pathspecs."""
    if any(os.environ.get(k) for k in _GIT_ENV_PLACES):
        return False
    g = os.path.join(str(where), ".git")
    try:
        if not _stat.S_ISDIR(os.lstat(g).st_mode) or not _stat.S_ISREG(os.lstat(os.path.join(g, "HEAD")).st_mode):
            return False
        if not (os.path.isdir(os.path.join(g, "objects")) and os.path.isdir(os.path.join(g, "refs"))):
            return False
        if os.path.lexists(os.path.join(g, "commondir")) or os.path.lexists(os.path.join(g, "config.worktree")):
            return False
        cfg = read_regular(os.path.join(g, "config"), 1024 * 1024)
    except (OSError, ValueError):
        return False
    if cfg is None:
        return False
    for line in cfg.lower().splitlines():
        line = line.strip()
        if b"include" in line or b"worktree" in line:      # an include, includeIf or (core.)worktree, anywhere
            return False
        key, eq, value = line.partition(b"=")
        if key.strip() == b"bare" and (not eq or value.split(b"#")[0].split(b";")[0].strip() not in
                                       (b"false", b"no", b"off", b"0")):
            return False
    return True


def _candidates_raw(where, store, timeout: float, depth: int | None = None,
                    tilde: _ShortNames | None = None) -> list[tuple[str, bytes]]:
    """(object id, path as git prints it relative to `where`) of every index entry that may land in `store`."""
    store_real = os.path.realpath(store)
    try:
        r = git_run(where, "ls-files", "-s", "-z", "--",
                    *_store_pathspecs({Path(store).name, Path(store_real).name}, depth, tilde), timeout=timeout,
                    max_out=MAX_CANDIDATE_OUTPUT)
    except GitOutputLimit:
        raise GitLimit(f"git's list of the index entries that could be in the store is longer than "
                       f"{MAX_CANDIDATE_OUTPUT // (1024 * 1024)} MB; devmem does not read it all") from None
    if r.returncode != 0:
        err = [ln.strip() for ln in r.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
        raise GitNoAnswer(display_text(err[0][:200]) if err else f"git ls-files exited with code {r.returncode}")
    if r.stdout.count(b"\0") > MAX_STORE_CANDIDATES:
        raise GitLimit(f"more than {MAX_STORE_CANDIDATES} entries of git's index could be in the store; devmem "
                          "does not look through them all")
    out = []
    for rec in r.stdout.split(b"\0"):
        head, _tab, path = rec.partition(b"\t")
        fields = head.split()
        if path and len(fields) >= 2:
            out.append((fields[1].decode("ascii", "replace"), path))
    return out


def _candidates(where, store, timeout: float = GIT_TIMEOUT) -> list[str]:
    """The paths git lists for _store_pathspecs (tests: what git is asked for, before the file system decides)."""
    return [p.decode("utf-8", "replace") for _oid, p in _candidates_raw(where, store, timeout)]


def _same_folder(q: str, store_real: str, want: tuple[int, int] | None) -> bool:
    try:
        st = os.lstat(q)
    except (OSError, ValueError):
        return False
    if want is not None and st.st_ino:
        return (st.st_dev, st.st_ino) == want
    try:                          # no inode numbers on this file system (FAT): the resolved path, case folded
        return os.path.normcase(os.path.realpath(q)) == os.path.normcase(store_real)
    except (OSError, ValueError):
        return False


MAX_STORE_CANDIDATES = 250_000   # index entries git may list for the question; more: "git could not say"
MAX_CANDIDATE_OUTPUT = 64 * 1024 * 1024   # bytes of that list read from git; more: git is stopped (a limit)
MAX_STORE_FOLDERS = 4_096        # distinct folders compared with the store on disk; more: "git could not say"


def _may_alias(c: str, name: str) -> bool:
    """Can the path component `c` name the folder `name` on some file system (other letter case, trailing dots or
    spaces, an NTFS stream suffix, an 8.3 short name, another Unicode form)? False only when it certainly cannot:
    such a component is not looked up on disk."""
    if "~" in c or not c.isascii() or not name.isascii():
        return True
    return c.split(":", 1)[0].rstrip(" .").lower() == name.rstrip(" .").lower()


MAX_LEVEL_LISTED = 100_000      # entries of a folder on the store's path that are listed (more: not used)


def _short_name(path: str) -> tuple[bool, str | None]:
    """Windows: (True, the 8.3 short name of the last component of `path`, or None when it has none), or (False, None)
    when that cannot be told."""
    try:
        import ctypes
        from ctypes import wintypes
        f = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        f.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        f.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(32768)
        n = f(path, buf, 32768)
    except (OSError, AttributeError, ValueError):
        return False, None
    if not n or n >= 32768:
        return False, None
    short = os.path.basename(buf.value.rstrip("\\/"))
    return True, (None if short.lower() == os.path.basename(path.rstrip("\\/")).lower() else short)


class _Level:
    """What the file system says, asked once, about one folder F on the store's path (the store or a parent) and the
    folder P that holds it: which names P really holds, whether P's file system folds letter case, and (Windows) F's
    8.3 short name. A component of an index path that differs from F's name can then be ruled out without a look-up
    per component: the number of file-system calls is bounded by the folders on the store's path, not by what the
    index names."""

    def __init__(self, parent: str, name: str):
        self.parent, self.name = parent, name
        folder = os.path.join(parent, name)
        try:
            st = os.lstat(folder)
            me = (st.st_dev, st.st_ino) if st.st_ino else None
        except (OSError, ValueError):
            me = None
        self.names: set[str] | None = None
        try:
            with os.scandir(parent) as it:
                names = set()
                for e in it:
                    names.add(e.name)
                    if len(names) > MAX_LEVEL_LISTED:
                        names = None
                        break
                self.names = names
        except (OSError, ValueError):
            self.names = None
        self.folds = _case_folds(parent, name) if me is not None else None   # None: cannot be told
        self.short_known, self.short = _short_name(folder) if os.name == "nt" else (False, None)

    def rules_out(self, c: str) -> bool:
        """Is the component `c` (not F's own name) certainly not a name of F on this file system?"""
        win = os.name == "nt"
        if self.names is not None and c in self.names and not (win and (c[-1:] in (" ", ".") or ":" in c)):
            return True                         # another real entry of P (an exact name is not resolved further)
        if "~" in c and "~" not in self.name:   # can only be an 8.3 short name
            if win and self.short_known:        # as _may_alias: Windows drops a stream suffix, trailing dots, spaces
                bare = c.split(":", 1)[0].rstrip(" .")
                return self.short is None or bare.upper() != self.short.upper()
            # whatever the file system says about letter case (one that keeps case apart can still resolve a short
            # name: a case-sensitive Windows folder through a bind mount), a component in a form F's generated short
            # name could take is looked up on disk (_landing compares by identity); any other is ruled out
            shapes = _generated_short_names(self.name)
            return shapes is not None and not shapes.could_be(c)
        if not win and self.folds is False:     # case, trailing dots or spaces, streams are other names there
            return sys.platform != "darwin" or c.isascii()
        return False


def _landing(raw, base: str, store_real: str, want, deadline: float) -> list[str]:
    """The entries of `raw` ((object id, path relative to `base` as git prints it)) that land in `store_real`. Cheap
    per entry: the components up to the store's depth are compared with the store's own path by string first; a
    component that may be another spelling of the store's (or a parent's) name is ruled out by what the file system
    says once about that folder (_Level), and only one that cannot be ruled out is looked up on disk, once per
    distinct folder. Raises GitNoAnswer past `deadline` (time.monotonic()) or MAX_STORE_FOLDERS (a backstop)."""
    sparts = Path(store_real).parts
    bparts = Path(base).parts
    nb, depth = len(bparts), len(sparts)
    inside = nb < depth and tuple(os.path.normcase(x) for x in sparts[:nb]) == tuple(os.path.normcase(x) for x in bparts)
    win = os.name == "nt"
    placed: dict[str, bool] = {}
    levels: dict[int, _Level] = {}
    out: set[str] = set()

    def ruled_out(tail, top: int) -> bool:
        for j, c in enumerate(tail, top):
            if c != sparts[j]:
                if j not in levels:
                    levels[j] = _Level(os.path.join(*sparts[:j]), sparts[j])
                if levels[j].rules_out(c):
                    return True
        return False

    def place(q: str) -> bool:
        hit = placed.get(q)
        if hit is None:
            if len(placed) >= MAX_STORE_FOLDERS:
                raise GitLimit(f"more than {MAX_STORE_FOLDERS} folders in git's index could be the store; devmem "
                                  "does not look them all up")
            hit = placed[q] = _same_folder(q, store_real, want)
        return hit

    for i, (oid, path) in enumerate(raw):
        if not i & 1023 and time.monotonic() > deadline:
            raise GitLimit("devmem could not place git's index entries in time")
        s = path.decode("utf-8", "surrogateescape")
        if inside:
            # only the first components are needed, however long the path
            comps = (s.replace("\\", "/") if win and "\\" in s else s).split("/", nb + depth + 1)
            k = 0
            while k < len(comps) and comps[k] == "..":
                k += 1
            top = nb - k                        # the entry's own components start at this depth of the store's path
            need = depth - top
            tail = comps[k:k + need]
            if k < nb and len(tail) < need:
                continue                        # ends above the store
            if k < nb and not any(c in ("", ".", "..") for c in tail):
                if all(c == n or _may_alias(c, n) for c, n in zip(tail, sparts[top:depth])):
                    if tail == list(sparts[top:depth]) or (not ruled_out(tail, top)
                                                           and place(os.path.join(*bparts[:top], *tail))):
                        out.add(f"{oid} {path.decode('utf-8', 'replace')}")
                continue
        try:                                    # the general way: the joined path, normalised
            parts = Path(os.path.normpath(os.path.join(base, s))).parts
        except ValueError:
            continue
        if len(parts) >= depth and place(os.path.join(*parts[:depth])):
            out.add(f"{oid} {path.decode('utf-8', 'replace')}")
    return sorted(out)


def store_index_entries(where, store, timeout: float = GIT_TIMEOUT) -> list[str]:
    """`<object id> <path>` for every entry of git's index that lands in `store` on this file system (or is the store
    itself), whether or not it is in the work tree now; paths as git prints them, relative to `where` (`../` above
    it). The one answer to "which files of the store does git track" (devmem.consent keeps it per index, and
    tracked_in_store, embeddings and status read it from there). Runs git once, in `where`; when git cannot be run
    there (not on PATH, not run by devmem in this project: GitUnsafe), fails or does not answer in time, the index file
    is read directly instead (devmem.gitindex, which answers only when it can be certain). `timeout` covers the whole
    question, git (or the index file) and devmem's own work on the answer (_landing), and so do the caps
    MAX_STORE_CANDIDATES and MAX_STORE_FOLDERS: whatever the index holds, this costs at most about `timeout`. Raises
    GitNoAnswer when neither git nor the index file could say (its text gives both reasons) or a limit was reached
    (GitLimit)."""
    deadline = time.monotonic() + timeout
    store_real = os.path.realpath(store)
    try:
        st = os.lstat(store_real)
        want = (st.st_dev, st.st_ino) if st.st_ino else None
    except (OSError, ValueError):
        want = None
    base = os.path.realpath(where)
    depth = None
    try:
        rel = os.path.relpath(store_real, base)
        if not rel.startswith("..") and not os.path.isabs(rel) and rel != "." and _top_is(where):
            depth = len(Path(rel).parts)
    except (OSError, ValueError):
        depth = None
    import subprocess
    names = {Path(store).name, Path(store_real).name}
    tilde = _tilde_rule(store_real, names)
    try:
        raw = _candidates_raw(where, store, max(0.0, deadline - time.monotonic()), depth, tilde)
    except GitLimit:
        raise
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        from . import gitindex
        try:
            raw = gitindex.index_candidates(where, names, deadline, MAX_STORE_CANDIDATES, tilde)
        except FileNotFoundError:
            raise GitNoAnswer(f"{git_failure_text(e)}, and devmem found no git repository to read itself") from None
        except gitindex.IndexLimit as e2:
            raise GitLimit(f"{git_failure_text(e)}, and {e2}") from None
        except Exception as e2:  # noqa: BLE001 - anything the reader cannot do is "cannot say"
            raise GitNoAnswer(f"{git_failure_text(e)}, and devmem could not read git's index itself ({e2})") from None
    return _landing(raw, base, store_real, want, deadline)


def git_failure_text(e: BaseException) -> str:
    """Why git gave no answer, in one clause (for GitNoAnswer and the "git could not say" notices)."""
    import subprocess
    if isinstance(e, subprocess.TimeoutExpired):
        return f"git did not answer within {GIT_TIMEOUT:g} s"
    if isinstance(e, GitUnsafe) or not isinstance(e, FileNotFoundError):
        return str(e) or type(e).__name__
    return "git is not on the PATH devmem was started with"


def git_home(project, store) -> Path:
    """The folder git is asked in about `store`: the project when the store is inside it (paths then read as before:
    relative to the project), else the folder that holds the store (whose work tree, if any, could write into it)."""
    try:
        p, s = os.path.normcase(os.path.realpath(project)), os.path.normcase(os.path.realpath(store))
        if os.path.commonpath([p, s]) == p:
            return Path(project)
    except (OSError, ValueError):
        pass
    return Path(store).parent


def tracked_in_store(project: Path, store: Path) -> list[str]:
    """Files in the store that git tracks and that are in the work tree (paths as git prints them, relative to
    git_home), whatever spelling of the store's path the index uses (store_index_entries). [] when there are none, git
    is not on PATH, this is not a work tree, or git could not answer: devmem.consent.store_refusal refuses such a store
    (tracked_file answers True then). git keeps tracking a file inside an ignored folder, so a `git pull` can bring or
    change one after setup. Reuses devmem.consent's answer for an unchanged git index (no git call)."""
    from .consent import tracked_answer
    got, _why = tracked_answer(project, store)
    return [t.split(" ", 1)[1] for t in got or []]


def tracked_file(project: Path, store: Path, name: str) -> bool:
    """Does git track the store's file `name` (in any spelling: compared by identity), or can it not say? True then:
    a file a `git pull` can bring is not used as the user's own setting (embeddings in config.json)."""
    from .consent import tracked_answer
    got, _why = tracked_answer(project, store)
    if got is None:
        return True
    home, target = git_home(project, store), Path(store) / name
    for t in got:
        rel = t.split(" ", 1)[-1]
        if not _may_alias(os.path.basename(rel), name):     # by string first: one look-up per possible spelling only
            continue
        try:
            if os.path.samefile(os.path.join(home, rel), target):
                return True
        except (OSError, ValueError):
            continue
    return False


def shipped_store(project: Path, store: Path, recorded_here: bool = False) -> tuple[list[str], bool]:
    """(reasons, tracked): why an existing store may not come from this user's own work ([] for no store, or one
    that holds nothing but the standard .gitignore), and whether git tracks a file in it. Two signs: a file in it is
    tracked by git (a cloned repository committed it), or it already holds anything else (settings such as
    `{"redact": false}`, an ignore list, a map, an index, sessions) although this user has not set this project up
    before with either route (`recorded_here` False, devmem.consent.set_up_here_before): a store can also come in a
    zip file. Its settings would govern what is recorded and its text would reach the user's prompts, so `devmem
    setup` and `devmem init` ask the user to adopt it explicitly."""
    store = Path(store)
    if not store.is_dir():
        return [], False
    reasons = []
    tracked = tracked_in_store(project, store)
    if tracked:
        reasons.append(f"{len(tracked)} file(s) in {store} are tracked by git (for example {tracked[0]})")
    if not recorded_here:
        try:
            names = sorted(e.name for e in os.scandir(store))
        except OSError:
            names = []
        gi = store / ".gitignore"
        if ".gitignore" in names and _regular(gi):
            try:
                if read_store_text(gi, GITIGNORE_CAP) == STORE_GITIGNORE:
                    names.remove(".gitignore")
            except OSError:
                pass
        if names:
            reasons.append(f"{store} already exists and holds {', '.join(names[:6])}{' ...' if len(names) > 6 else ''}")
    return reasons, bool(tracked)


def _read_patterns(path: Path) -> list[str]:
    return [l.strip() for l in _ignore_lines(path) if l.strip() and not l.lstrip().startswith("#")]


def load_ignores(project: Path, store: Path | None = None) -> list[str]:
    """Built-in ignore globs plus the `ignore` file (one glob per line) of the store in use and of the project's own
    `.devmemory`. When another store is in use (`--store`, the files-only pack's map: devmem.pack.files_only_store),
    the project's `.devmemory/ignore` counts only while that store is this user's (devmem.consent.recorded_refusal):
    nothing in a refused store is read."""
    pats = list(DEFAULT_IGNORES)
    default = store_path(project)
    stores = [default] + ([Path(store)] if store else [])
    seen = set()
    for st in stores:
        key = os.path.normcase(os.path.abspath(st))
        if key in seen:
            continue
        seen.add(key)
        if st is default and store and os.path.normcase(os.path.abspath(store)) != key and default.is_dir():
            from .consent import recorded_refusal
            if recorded_refusal(project, default):
                continue
        pats += [p for p in _read_patterns(st / "ignore") if p not in pats]
    return pats


# ---- glob matching (compiled once per pattern list; fnmatch per call is too slow on big trees) ----
_MATCHERS: dict[tuple, _Globs] = {}
_GLOB_SPECIAL = re.compile(r"[*?\[\]]")


class _Globs:
    """fnmatch globs, matched case-insensitively against a whole name (`.match(name)`, as one regex would), with a
    cheap test in front of the regex. Each `*` glob is translated to `.*`, which the regex engine scans to the end
    of the name and back for every glob in turn: over the long folder paths a hand-made index can list (1,600 paths
    of 3.8 KB, 19 folders each) that cost 4 s of one prompt's 8 s refresh. A glob matches only names that start with
    the text before its first wildcard and end with the text after its last one, so for an ASCII name (where str.lower
    and the regex's case folding agree) the regex runs only when one such start or end is there, and `*word*` globs
    are a substring test. Non-ASCII names and globs of other shapes always go through the regex. Same answers as
    the regex alone (tests/test_store_globs.py compares them)."""
    __slots__ = ("_patterns", "_full", "_ends", "_starts", "_rx_cheap", "_words", "_rx_rest")

    def __init__(self, patterns: tuple[str, ...]):
        def rx(ps):
            return re.compile("|".join(f"(?:{fnmatch.translate(p)})" for p in ps) or r"(?!)", re.I)
        self._patterns, self._full = patterns, None     # the whole set: compiled for the first non-ASCII name
        ends, starts, cheap, words, rest = [], [], [], [], []
        for p in patterns:
            if not p.isascii():
                rest.append(p)
                continue
            lits = _GLOB_SPECIAL.split(p)
            head, tail = lits[0].lower(), lits[-1].lower()
            if len(lits) == 3 and not head and not tail and lits[1] and p == f"*{lits[1]}*":
                words.append(lits[1].lower())       # `*word*`: the word anywhere in the name
            elif tail:
                ends.append(tail)
                cheap.append(p)
            elif head:
                starts.append(head)
                cheap.append(p)
            else:
                rest.append(p)
        self._ends, self._starts, self._words = tuple(ends), tuple(starts), tuple(words)
        self._rx_cheap, self._rx_rest = rx(cheap), rx(rest)

    def match(self, name: str) -> bool:
        if not name.isascii():
            if self._full is None:
                self._full = re.compile("|".join(f"(?:{fnmatch.translate(p)})" for p in self._patterns) or r"(?!)",
                                        re.I)
            return self._full.match(name) is not None
        low = name.lower()
        if (low.endswith(self._ends) or low.startswith(self._starts)) and self._rx_cheap.match(name):
            return True
        return any(w in low for w in self._words) or self._rx_rest.match(name) is not None


def _matcher(patterns) -> _Globs:
    key = tuple(patterns)
    rx = _MATCHERS.get(key)
    if rx is None:
        rx = _Globs(key)
        if len(_MATCHERS) > 64:
            _MATCHERS.clear()
        _MATCHERS[key] = rx
    return rx


def is_ignored(rel: str, patterns: list[str]) -> bool:
    """True if any path component, or the whole relative path, matches one of the globs
    (case-insensitive, so `SECRET.txt` is excluded on every platform)."""
    rx = _matcher(patterns)
    return bool(rx.match(rel) or any(rx.match(p) for p in rel.split("/")))


# ---- .gitignore fallback reader ----------------------------------------------------------------
def _gi_regex(p: str) -> str:
    i, n, out = 0, len(p), []
    while i < n:
        c = p[i]
        if c == "*":
            if p.startswith("**", i):
                if i + 2 < n and p[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = p.find("]", i + 2)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = p[i + 1:j]
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j + 1
                continue
        elif c == "\\" and i + 1 < n:
            out.append(re.escape(p[i + 1]))
            i += 2
            continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


class GitIgnore:
    """Rules from one .gitignore file: blank/# lines, `!` negation, trailing `/` (directories only),
    a slash anywhere else anchors the pattern to the file's directory, `*`, `?`, `[...]`, `**`.
    Not supported: trailing-space escapes and core.excludesFile."""

    def __init__(self, base: str, lines: list[str]):
        self.base = base  # project-relative POSIX dir ("" for the root)
        flags = re.I if os.name == "nt" else 0
        self.rules = []
        for raw in lines:
            line = raw.rstrip("\n").rstrip()
            if not line or line.startswith("#"):
                continue
            neg = line.startswith("!")
            if neg:
                line = line[1:]
            elif line.startswith("\\#") or line.startswith("\\!"):
                line = line[1:]
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            if not line:
                continue
            anchored = "/" in line
            line = line.lstrip("/")
            rx = re.compile(_gi_regex(line) + r"\Z", flags)
            self.rules.append((rx, neg, dir_only, anchored))

    def match(self, rel: str, is_dir: bool) -> bool | None:
        """None if no rule applies; True if ignored; False if re-included by a `!` rule."""
        if self.base:
            if not rel.startswith(self.base + "/"):
                return None
            rel = rel[len(self.base) + 1:]
        name = rel.rsplit("/", 1)[-1]
        verdict = None
        for rx, neg, dir_only, anchored in self.rules:
            if dir_only and not is_dir:
                continue
            if rx.match(rel if anchored else name):
                verdict = not neg
        return verdict


def _gitignored(rules: list[GitIgnore], rel: str, is_dir: bool) -> bool:
    verdict = False
    for r in rules:
        v = r.match(rel, is_dir)
        if v is not None:
            verdict = v
    return verdict


# ---- listing ---------------------------------------------------------------------------------
MAX_GIT_LISTED = 200_000   # names in git's file list (a hand-made index can hold millions that are not on disk)
MAX_GIT_LISTED_BYTES = 64 * 1024 * 1024   # bytes of that list read from git (more: git is stopped, the folder walked)


def _git_list(project: Path, timeout: float = 60) -> list[str] | None:
    import subprocess  # lazy: about 12 ms, and most processes (hooks) never list files
    try:
        r = git_run(project, "ls-files", "-z", "--cached", "--others", "--exclude-standard", timeout=timeout,
                    max_out=MAX_GIT_LISTED_BYTES)
    except (OSError, subprocess.SubprocessError, ValueError):     # GitOutputLimit too: the folder is walked
        return None
    if r.returncode != 0 or r.stdout.count(b"\0") > MAX_GIT_LISTED:
        return None              # more names than devmem looks through: the folder itself is walked instead
    names = r.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return sorted({n for n in names if n and not n.endswith("/")})


MAX_IGNORE_FILE_BYTES = 1024 * 1024   # a larger .gitignore / info/exclude is not read (a walk would exhaust memory)
MAX_IGNORE_LINE = 4096                # longer lines in one are skipped
MAX_WALK_DEPTH = 100                  # folders deeper than this are not walked


def _ignore_lines(path) -> list[str]:
    """Patterns of a .gitignore-style file: regular, non-link files up to MAX_IGNORE_FILE_BYTES only."""
    text = read_project_text(path, MAX_IGNORE_FILE_BYTES)
    if text is None:
        return []
    return [ln for ln in text.splitlines() if len(ln) <= MAX_IGNORE_LINE]


def _user_file_lines(path, cap: int) -> list[str]:
    """Lines of one of the user's own git files (it may be a link: dotfile managers make them so), up to `cap` bytes."""
    try:
        data = read_regular(path, cap, follow=True)
    except (OSError, ValueError):
        return []
    return data.decode("utf-8", "replace").splitlines() if data else []


def _excludes_file_setting(config_lines: list[str]) -> str | None:
    """core.excludesFile in one git configuration file (a small tolerant reader: the last value in a [core] section;
    includes are not followed)."""
    section, value = "", None
    for raw in config_lines:
        line = raw.strip()
        if line.startswith("["):
            section = line[1:].split("]", 1)[0].strip().lower()
            continue
        key, eq, v = line.partition("=")
        if eq and section == "core" and key.strip().lower() == "excludesfile":
            v = v.strip()
            if v[:1] == '"' and '"' in v[1:]:
                v = v[1:v.index('"', 1)]
            else:
                v = v.split("#", 1)[0].split(";", 1)[0].strip()
            value = v or None
    return value


def _global_exclude_lines() -> list[str]:
    """The user's global git excludes (core.excludesFile from the user's own ~/.gitconfig, $XDG_CONFIG_HOME/git/config
    or GIT_CONFIG_GLOBAL, never from a repository; by default $XDG_CONFIG_HOME/git/ignore or ~/.config/git/ignore),
    for the folder walk that stands in for git: a file the user ignores everywhere stays out of the map then too.
    Bounded like the other ignore files."""
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    configs = [Path(os.environ["GIT_CONFIG_GLOBAL"])] if os.environ.get("GIT_CONFIG_GLOBAL") else \
        [xdg / "git" / "config", home / ".gitconfig"]          # git reads them in this order; the later one wins
    setting = None
    for c in configs:
        setting = _excludes_file_setting(_user_file_lines(c, MAX_IGNORE_FILE_BYTES)) or setting
    if setting:
        p = Path(os.path.expanduser(setting))
        path = p if p.is_absolute() else home / p
    else:
        path = xdg / "git" / "ignore"
    return [ln for ln in _user_file_lines(path, MAX_IGNORE_FILE_BYTES) if len(ln) <= MAX_IGNORE_LINE]


def _walk_list(project: Path, rx: _Globs, exclude_prefixes: tuple[str, ...], stats: dict | None = None,
               deadline: float | None = None):
    """(rel, size, mtime_ns) for regular files, honouring .gitignore files found on the way. An explicit stack (a
    folder thousands of levels deep cannot exhaust Python's recursion limit) and MAX_WALK_DEPTH."""
    out = []
    root_rules = []
    global_lines = _global_exclude_lines()
    if global_lines:
        root_rules.append(GitIgnore("", global_lines))
    info_exclude = project / ".git" / "info" / "exclude"
    root_lines = _ignore_lines(info_exclude)
    if root_lines:
        root_rules.append(GitIgnore("", root_lines))
    stack = [(str(project), "", root_rules, 0)]
    while stack:
        if deadline is not None and time.perf_counter() > deadline:
            raise ListingTimeout(str(project))
        dirpath, rel_dir, rules, depth = stack.pop()
        lines = _ignore_lines(os.path.join(dirpath, ".gitignore"))
        if lines:
            rules = rules + [GitIgnore(rel_dir, lines)]
        try:
            entries = sorted(os.scandir(dirpath), key=lambda e: e.name)
        except (OSError, ValueError):     # ValueError: a NUL character in the path
            continue
        subdirs = []
        for e in entries:
            rel = rel_dir + "/" + e.name if rel_dir else e.name
            if UNSAFE_NAME_CHARS.search(e.name):   # never listed; counted so status/doctor can say so
                if stats is not None:
                    stats["unsafe_names"] = stats.get("unsafe_names", 0) + 1
                continue
            try:
                if e.is_symlink():
                    continue
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if rx.match(e.name) or rx.match(rel) or rel.startswith(exclude_prefixes) or rel in exclude_prefixes:
                continue
            if _gitignored(rules, rel, is_dir):
                continue
            if is_dir:
                if depth + 1 < MAX_WALK_DEPTH:
                    subdirs.append((e.path, rel, rules, depth + 1))
                elif stats is not None:
                    stats["too_deep"] = stats.get("too_deep", 0) + 1
            else:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if _stat.S_ISREG(st.st_mode):
                    out.append((rel, st.st_size, st.st_mtime_ns))
        stack.extend(reversed(subdirs))
    out.sort()
    return out


class ListingTimeout(TimeoutError):
    """list_files ran past its `deadline` (devmem.refresh's bounded "did anything change?" check)."""


def list_files(project: Path, store: Path | None = None, extra_patterns: list[str] | None = None,
               exclude_prefixes: tuple[str, ...] = (), stats: dict | None = None,
               deadline: float | None = None,
               git_by: float | None = None) -> tuple[list[tuple[str, int, int]], str]:
    """All project files that may be indexed, as sorted (rel, size, mtime_ns), and the listing
    method used ("git" or "walk"). `exclude_prefixes` are project-relative paths (e.g. a store kept
    inside the project under another name) excluded with everything below them. Every listed name is safe_rel and
    reaches no folder through a link (git lists the names of tracked files even when a folder of them was replaced
    by a link to elsewhere, and a hand-written .git/index can hold `../x` or absolute names); names with control or
    bidi characters are skipped and counted in `stats["unsafe_names"]`. With `deadline` (a time.perf_counter() value)
    it raises ListingTimeout once that passes, instead of finishing the listing. `git_by` (a time.perf_counter()
    value, without `deadline`): only git is bounded by it, the listing is finished (the refresh child's scan)."""
    project = Path(project)
    pats = load_ignores(project, store) + list(extra_patterns or [])
    rx = _matcher(pats)
    prefixes = tuple(p.rstrip("/") + "/" for p in exclude_prefixes if p)
    # under a deadline git gets half of the time left (its filter listing included: git_run): a git that hangs is
    # then ended by this process before this process is killed at its deadline, and the walk below still has time
    by = deadline if deadline is not None else git_by
    names = _git_list(project) if by is None else \
        _git_list(project, max(0.05, min(60.0, (by - time.perf_counter()) / 2)))
    if deadline is not None and time.perf_counter() > deadline:
        raise ListingTimeout(str(project))
    if names:
        out, dir_cache, link_dirs = [], {}, {}
        root = str(project)
        for n, rel in enumerate(names):
            if deadline is not None and not n % 256 and time.perf_counter() > deadline:
                raise ListingTimeout(str(project))
            if safe_rel(rel) is None:
                if stats is not None and UNSAFE_NAME_CHARS.search(rel):
                    stats["unsafe_names"] = stats.get("unsafe_names", 0) + 1
                continue
            parts = rel.split("/")
            bad = False
            for i in range(len(parts) - 1):  # cache per directory: most files share them
                d = "/".join(parts[: i + 1])
                v = dir_cache.get(d)
                if v is None:
                    v = dir_cache[d] = bool(rx.match(parts[i]) or rx.match(d))
                if v:
                    bad = True
                    break
            if bad or rx.match(parts[-1]) or rx.match(rel) or rel.startswith(prefixes):
                continue
            try:    # first: an entry whose file is not there (a hand-made index lists 1,600 of them) costs no
                st = os.lstat(os.path.join(root, rel))     # look-up of every folder on its way
            except (OSError, ValueError):
                continue  # tracked but deleted in the work tree, or unreadable
            if project_path(project, rel, link_dirs) is None:
                continue  # a folder on the way is a link (possibly out of the project): `st` is not used
            if _stat.S_ISREG(st.st_mode):
                out.append((rel, st.st_size, st.st_mtime_ns))
        return out, "git"
    return _walk_list(project, rx, tuple(p.rstrip("/") for p in exclude_prefixes if p) + prefixes, stats,
                      deadline), "walk"


def iter_files(project: Path, store: Path | None = None):
    """Yield project-relative POSIX paths of indexable files, sorted (see module docstring)."""
    for rel, _size, _mtime in list_files(project, store)[0]:
        yield rel


def normalized_hash(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()[:16]


MAX_HASH_BYTES = 64 * 1024 * 1024   # a larger file is identified by its size instead of its content


def _open_regular(path, max_bytes: int | None, follow: bool = False):
    """(fd, size) of `path` opened for reading when it is a regular file (not a link, FIFO, device or folder) no
    larger than `max_bytes`, else None. lstat first, then O_NOFOLLOW (and O_NONBLOCK, so a FIFO swapped in between
    cannot block) where the platform has them, then fstat on the handle. follow=True (a file of the user's own
    configuration, which a dotfile manager may link): a link is followed, but what it reaches must be a regular file
    too, and the open never blocks."""
    try:
        st = os.stat(path) if follow else os.lstat(path)
    except (OSError, ValueError):
        return None
    if not _stat.S_ISREG(st.st_mode) or (max_bytes is not None and st.st_size > max_bytes):
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    if not follow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except (OSError, ValueError):
        return None
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode) or (max_bytes is not None and st.st_size > max_bytes):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd, st.st_size


def read_regular(path, limit: int = MAX_TEXT_BYTES, follow: bool = False) -> bytes | None:
    """The bytes of `path` when it is a regular, non-link file of at most `limit` bytes, else None. Every read of a
    project file goes through here: a cloned repository can ship a link to /dev/zero, a FIFO or a huge file.
    follow=True: see _open_regular."""
    opened = _open_regular(path, limit, follow)
    if opened is None:
        return None
    fd, size = opened
    with os.fdopen(fd, "rb") as f:
        data = read_bounded(f, size, limit + 1)
    return data if len(data) <= limit else None


def read_bounded(f, size: int, cap: int) -> bytes:
    """At most `cap` bytes from the open binary file `f`, asking first for the `size` its fstat gave (plus one byte, to
    see whether it grew), then a megabyte at a time up to `cap`: read(n) allocates n bytes up front, and asking for a
    64 MB cap on every small file took about 20 ms each on Windows."""
    first = min(max(size, 0) + 1, cap)
    data = f.read(first)
    if len(data) < first:
        return data
    parts, n = [data], len(data)
    while n < cap:             # it grew since (or is at least `cap` bytes): read on
        chunk = f.read(min(1 << 20, cap - n))
        if not chunk:
            break
        parts.append(chunk)
        n += len(chunk)
    return b"".join(parts)


STORE_FILE_CAP = 64 * 1024 * 1024
GITIGNORE_CAP = 1024 * 1024


def read_store_text(path, limit: int = STORE_FILE_CAP) -> str:
    """The text of a store file (config.json, map.json, a record, a state or pending file, a log). Raises
    FileNotFoundError (an OSError) unless it is a regular, non-link file of at most `limit` bytes: a store can come
    from a repository, and a FIFO shipped as `config.json` would block every hook (the open is non-blocking and
    checked with fstat, store.read_regular)."""
    data = read_regular(path, limit)
    if data is None:
        raise FileNotFoundError(f"{path} is missing or not a plain file")
    return data.decode("utf-8", errors="replace")


def read_project_text(path, limit: int = MAX_TEXT_BYTES) -> str | None:
    """read_regular decoded as UTF-8 (undecodable bytes replaced), or None."""
    data = read_regular(path, limit)
    return None if data is None else data.decode("utf-8", errors="replace")


def file_hash(path: Path) -> str | None:
    """Line-ending-insensitive content hash (CRLF vs LF must not mark memory stale). None for anything that is not a
    regular file (a link, FIFO or device is never read); a file over MAX_HASH_BYTES is identified by its size."""
    opened = _open_regular(path, None)
    if opened is None:
        return None
    fd, size = opened
    if size > MAX_HASH_BYTES:
        os.close(fd)
        return f"size:{size}"
    h, carry = hashlib.sha256(), b""
    with os.fdopen(fd, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            chunk = carry + chunk
            carry = b"\r" if chunk.endswith(b"\r") else b""   # a CRLF split between two chunks
            if carry:
                chunk = chunk[:-1]
            h.update(chunk.replace(b"\r\n", b"\n"))
    h.update(carry)
    return h.hexdigest()[:16]


def hash_files(project: Path, rels) -> dict[str, str | None]:
    out = {}
    for r in sorted(set(rels)):
        p = project_path(project, r)
        out[r] = file_hash(p) if p is not None else None
    return out


# ---- names devmem accepts for project files ----------------------------------------------------------------
# control characters (C0, DEL, C1), line/paragraph separators and the Unicode bidi and invisible format characters:
# a file name holding one could fake a section in a pack or PROJECT.md, or rewrite the terminal
_UNSAFE_RANGES = ((0x00, 0x1F), (0x7F, 0x9F), (0x061C, 0x061C), (0x200B, 0x200B), (0x200E, 0x200F), (0x2028, 0x2029),
                  (0x202A, 0x202E), (0x2066, 0x2069), (0xFEFF, 0xFEFF))
UNSAFE_NAME_CHARS = re.compile("[" + "".join(re.escape(chr(a)) + ("-" + re.escape(chr(b)) if b != a else "")
                                             for a, b in _UNSAFE_RANGES) + "]")


# Shown escaped wherever devmem shows a name or recorded text (pack, hook output, MCP, PROJECT.md, terminal): every
# control (Cc), format (Cf: bidi controls, zero-width characters, the tag block U+E0000-E007F used to smuggle invisible
# text, soft hyphen, word joiner, invisible operators), private-use (Co), unassigned (Cn) and surrogate (Cs) code point,
# line/paragraph separators, variation selectors, and a few letters and marks that render as nothing. Only non-ASCII
# characters (and ASCII controls) are looked at, one category lookup each, so ordinary text costs a regex scan.
_NOT_PRINTABLE_ASCII = re.compile(r"[^\x20-\x7e]")
_INVISIBLE = frozenset([0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x3164, 0xFFA0, *range(0x180B, 0x1810),
                        *range(0xFE00, 0xFE10), *range(0xE0100, 0xE01F0)])
_ESCAPED_CATEGORIES = frozenset(("Cc", "Cf", "Co", "Cn", "Cs", "Zl", "Zp"))
_JOINERS = ("\u200c", "\u200d")


def _hidden(c: str) -> bool:
    o = ord(c)
    if o < 0x20 or 0x7F <= o <= 0x9F or o in _INVISIBLE:
        return True
    import unicodedata
    return unicodedata.category(c) in _ESCAPED_CATEGORIES


def _joins(c: str) -> bool:
    """A character a ZWJ/ZWNJ may sit next to in real text: a non-ASCII letter or mark (Persian, Indic, ...) or an
    emoji-like symbol."""
    if ord(c) < 0x80:
        return False
    import unicodedata
    cat = unicodedata.category(c)
    return cat[0] in "LM" or cat == "So" or 0x1F3FB <= ord(c) <= 0x1F3FF


def _escape_char(c: str) -> str:
    return c.encode("unicode_escape").decode("ascii")


def display_name(name) -> str:
    """A path or name as it may be shown in a pack, PROJECT.md or a terminal: unsafe characters (see _hidden) as
    \\xNN/\\uNNNN/\\UNNNNNNNN. ZWJ and ZWNJ are escaped too (a name or a header never needs them)."""
    s = str(name)
    return _NOT_PRINTABLE_ASCII.sub(lambda m: _escape_char(m.group(0)) if _hidden(m.group(0)) else m.group(0), s)


def _json_escape_char(c: str) -> str:
    import json
    return json.dumps(c)[1:-1]        # \uXXXX, a surrogate pair for U+10000 and above: still valid JSON


def display_text(text, json_text: bool = False) -> str:
    """Recorded text (a prompt, a reply, a tool result) as it may be shown: like display_name, except that a ZWJ or
    ZWNJ between two characters that use them (non-ASCII letters or marks, emoji) is kept, so Persian or Indic words
    and emoji sequences read correctly. Anywhere else (at an end, next to ASCII or another invisible) it is escaped.
    json_text: `text` is JSON (from json.dumps(ensure_ascii=False)); characters are escaped as JSON escapes, so it
    still parses to the same value while showing nothing invisible."""
    s = str(text)
    esc = _json_escape_char if json_text else _escape_char

    def repl(m: re.Match) -> str:
        c, i = m.group(0), m.start()
        if c in _JOINERS and 0 < i < len(s) - 1 and _joins(s[i - 1]) and _joins(s[i + 1]):
            return c
        if json_text and c in "\n\t":
            return c                  # JSON layout from indent=1, never inside a string (those are \n already)
        return esc(c) if _hidden(c) else c
    return _NOT_PRINTABLE_ASCII.sub(repl, s)


def safe_rel(rel) -> str | None:
    """`rel` when it is a plain project-relative POSIX path, else None. Refused: not a string, empty, absolute, a
    drive or any `:` (Windows drive-relative paths and alternate data streams), a backslash (\\\\host\\share), an
    empty, `.` or `..` part, and control, bidi or invisible format characters. Used for every path that comes from
    git, a recorded session or the viewer before anything is opened."""
    if not isinstance(rel, str) or not rel or len(rel) > 4096:
        return None
    if rel.startswith("/") or "\\" in rel or ":" in rel or UNSAFE_NAME_CHARS.search(rel):
        return None
    if any(p in ("", ".", "..") for p in rel.split("/")):
        return None
    return rel


def project_path(project, rel, _dirs: dict | None = None) -> Path | None:
    """The path of `rel` inside `project`, or None when `rel` is not safe_rel or a folder on the way is a link out of
    the project (its real path is checked against the project's), or when `rel` is another spelling of a file whose
    real name differs (_may_be_alias). Readers open the file with read_regular, which refuses a link. `_dirs` caches
    folder verdicts during a listing."""
    r = safe_rel(rel)
    if r is None:
        return None
    root = _dirs.get("\0root") if _dirs is not None else None
    if root is None:
        root = os.path.realpath(str(project))
        if _dirs is not None:
            _dirs["\0root"] = root
    parent = r.rsplit("/", 1)[0] if "/" in r else ""
    if parent:
        ok = _dirs.get(parent) if _dirs is not None else None
        if ok is None:
            # no link (or junction) anywhere between the project and the file: the real path is the path itself
            joined = os.path.join(root, *parent.split("/"))
            ok = os.path.normcase(os.path.realpath(joined)) == os.path.normcase(joined)
            if _dirs is not None:
                _dirs[parent] = ok
        if not ok:
            return None
    full = os.path.join(root, *r.split("/"))
    if _may_be_alias(r):
        # another spelling of a file (a Windows 8.3 short name `MY_SEC~1.TXT`, a trailing dot or space) would pass
        # the secret-name and ignore rules, which look at the name given: only the file's own spelling is accepted
        try:
            real = os.path.realpath(full)
        except (OSError, ValueError):
            return None
        if os.path.normcase(real) != os.path.normcase(full):
            return None
    return Path(full)


def _may_be_alias(rel: str) -> bool:
    """Could `rel` name a file under another spelling? 8.3 short names hold `~`; Windows drops trailing dots and spaces
    from a name. (Only these are resolved: a realpath for every file would slow listings down.)"""
    return "~" in rel or any(p.endswith((".", " ")) for p in rel.split("/"))


def estimate_tokens(text: str) -> int:
    """Rough token estimate (chars/4). Labelled as an estimate wherever reported."""
    return max(1, len(text) // 4) if text else 0


# ---------------------------------------------------------------- an OS file lock (devmem.hooks, devmem.refresh)

_NOT_TRANSIENT = {getattr(errno, n) for n in ("EROFS", "ENOSPC", "EDQUOT") if hasattr(errno, n)}


class OSLock:
    """Cross-process lock for the read-modify-write of a session's state and record (devmem.hooks) and the store-wide refresh (devmem.refresh): an OS lock on `live/<sid>.lock`
    (fcntl.flock on POSIX, msvcrt.locking on Windows). The OS drops it when its holder exits or is killed, so a hook
    killed while holding it never leaves a lock behind, and no contender ever has to guess whether a holder is still
    alive (the PID file this replaced was taken over by check-then-remove, which let two live hooks hold it at once).
    Only the holder deletes the file, and only while it still holds the OS lock: on POSIX a contender that locked a
    file deleted under it sees that the path no longer names its file and opens it again; on Windows a file that any
    contender has open cannot be deleted (the delete then fails and the file stays, which is harmless).

    It is NOT what keeps the event log complete: `_append` is atomic on its own (see there), so an event is never lost
    even when this lock cannot be taken. Waits at most `timeout` seconds (devmem.hooks: LOCK_TIMEOUT_S); a caller that did not get it (`held` False) only
    appends its event and leaves the state and the record alone (the next locked Stop/SessionEnd rebuilds the record
    from the whole log). A read-only or full file system, or a PermissionError opening the file when it is not there
    (Windows: three times in a row; POSIX: once), means the store cannot be written: it gives up at once
    (`cannot_write`). A file system without lock support (`unsupported`) proceeds without exclusion, as if held.
    Anything other than a regular file at the lock's path (a link, FIFO, empty folder) is removed, never followed or
    read; a non-empty folder there makes it give up at once."""

    def __init__(self, path: Path, timeout: float | None = 3.0):
        self.path, self.held, self.cannot_write, self.unsupported = path, False, False, False
        self.timeout = timeout          # 0: one try (devmem.refresh's store-wide refresh lock)
        self._fd: int | None = None

    def _timeout(self) -> float:
        return 3.0 if self.timeout is None else self.timeout

    def __enter__(self):
        deadline = time.monotonic() + self._timeout()
        private_mkdir(self.path.parent)
        denied_absent = 0
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        while True:
            fd = None
            try:
                if os.name == "nt" and not self._clear():
                    return self
                fd = os.open(str(self.path), flags, PRIVATE_FILE)
                denied_absent = 0
                st = os.fstat(fd)
                if not _stat.S_ISREG(st.st_mode):
                    os.close(fd)
                    fd = None
                    if not self._clear():
                        return self
                    continue
                if _os_lock(fd):
                    if os.name != "nt" and not _same_file(self.path, st):
                        continue        # the previous holder deleted it after we opened it: open the new one
                    self._fd, fd, self.held = fd, None, True
                    return self
            except PermissionError:
                if os.name != "nt" or not os.path.lexists(self.path):
                    denied_absent += 1
                    if os.name != "nt" or denied_absent >= 3:
                        self.cannot_write = True
                        return self     # not contention: this directory cannot be written
                    time.sleep(0.005)
                    continue
                denied_absent = 0       # Windows: a sharing violation while another process opens or deletes it
            except OSError as e:
                if e.errno in _NOT_TRANSIENT:      # read-only or full file system
                    self.cannot_write = True
                    return self
                if e.errno in _NO_LOCKS:           # the file system has no locks: go on without exclusion
                    self._fd, fd, self.held, self.unsupported = fd, None, True, True
                    return self
                if e.errno in (errno.ELOOP, errno.EISDIR, getattr(errno, "EMLINK", -1), errno.ENXIO):
                    if not self._clear():
                        return self
                    continue
                # otherwise e.g. a transient sharing violation; retry until the deadline
            finally:
                if fd is not None:
                    os.close(fd)
            if time.monotonic() >= deadline:
                return self
            time.sleep(0.005)

    def _clear(self) -> bool:
        """Remove what sits at the lock's path when it is not a regular file. False: it cannot be removed."""
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        is_link = _stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)  # reparse point
        if _stat.S_ISREG(st.st_mode) and not is_link:
            return True
        try:
            if _stat.S_ISDIR(st.st_mode) and not _stat.S_ISLNK(st.st_mode):
                try:
                    os.rmdir(self.path)
                except OSError:
                    if not is_link:
                        return False
                    os.unlink(self.path)   # a Windows junction: rmdir/unlink remove the link, never its target
            else:
                os.unlink(self.path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def __exit__(self, *exc):
        fd, self._fd = self._fd, None
        if fd is None:
            return
        if self.unsupported:
            os.close(fd)
            return
        if os.name == "nt":
            try:
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(fd)
            try:
                os.remove(self.path)    # fails while a contender has it open; then it simply stays
            except OSError:
                pass
            return
        try:
            if _same_file(self.path, os.fstat(fd)):
                os.unlink(self.path)    # still holding the lock: no one else can be relying on this file
        except OSError:
            pass
        finally:
            os.close(fd)                # releases the flock


_NO_LOCKS = {getattr(errno, n) for n in ("ENOLCK", "EOPNOTSUPP", "ENOTSUP", "ENOSYS") if hasattr(errno, n)}


def _os_lock(fd: int) -> bool:
    """Try once to take the exclusive OS lock on `fd`. False: another process holds it. Other errors propagate."""
    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EDEADLK, getattr(errno, "EDEADLOCK", -1)):
                return False
            raise
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _same_file(path: Path, st: os.stat_result) -> bool:
    try:
        cur = os.lstat(path)
    except OSError:
        return False
    return (cur.st_ino, cur.st_dev) == (st.st_ino, st.st_dev)
