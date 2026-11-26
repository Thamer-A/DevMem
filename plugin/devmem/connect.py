"""`devmem connect <tool>` / `devmem disconnect <tool>`: one-command setup of devmem for AI tools other than
Claude Code (Claude Code itself uses `devmem init`, whose hooks record more).

For each tool, at PROJECT level only:
  1. the devmem MCP server (`devmemory`) is merged into the tool's project MCP config file. Other servers and
     unrelated keys are kept, and running it again changes nothing. Before the first change to an existing file
     that holds nothing of devmem's, a copy is saved in the store (`.devmemory/connect-backups/`, git-ignored).
     JSON is parsed and rewritten (indent, line endings and a BOM are kept); a file that does not parse (JSONC
     with comments, for example) is left untouched and the entry is printed for the user to add by hand. TOML
     (Codex) is never parsed and rewritten: a marked block is appended as text, and the result is parsed
     (Python 3.11+) before it is written.
  2. a short instruction block, between `devmemory:begin` / `devmemory:end` marker lines, is added to the
     tool's project instruction file. `disconnect` removes exactly that block (and the blank line `connect`
     put before it). One block in AGENTS.md serves every tool that reads AGENTS.md, so connecting two such
     tools does not load the text twice; it is removed when the last tool that uses it is disconnected.
A file that is a link to another file inside the project is changed through the link (the link stays); a link
that leads outside the project is refused. A user-GLOBAL config file is never edited: for a tool whose MCP config
is only global (Claude Desktop), the snippet to paste is printed. Nothing is written outside the project folder.

Every file is planned before anything is written; if one cannot be handled (malformed, unmatched markers) nothing
is written. If a write fails (a read-only or locked file), what this run already changed is put back as it was.

`disconnect` restores a file from its backup only when the file is byte for byte what `connect` last wrote (a
hash of those bytes is kept); otherwise it removes devmem's entry or block and leaves the rest as the user has it.
A file `connect` created and that holds nothing else afterwards is deleted, with the folder `connect` created for
it when that folder is empty. Once a file holds nothing of devmem's, its backup is deleted. What `connect`
created and wrote, and which tools are connected, is remembered in `<project>/.devmemory/connect.json`.

That file is inside the project, so a repository can ship a forged one: it is read as untrusted input. The only
files connect or disconnect ever delete are devmem's own, whatever the state file says: the target file itself (inside
the project, once it holds nothing but devmem's entry and connect created it), its backup by the one name devmem
gives it (`_backup_name`) as a regular file in the store's real backup folder, an old-style `<file>.devmem-backup`
next to it (a regular file), and folders connect created for the target (only ancestors of the target inside the
project, never a link). A name in the state file that is not one of those is ignored, kept out of the new state and
reported. Temporary files are created exclusively (never through a planted link).

Config formats, each checked against the tool's documentation on 2026-11-14:
  Cursor          https://cursor.com/docs/context/mcp  (.cursor/mcp.json, "mcpServers", type "stdio",
                  ${workspaceFolder} = "project root (the folder that contains .cursor/mcp.json)")
                  https://cursor.com/docs/context/rules  ("Cursor supports AGENTS.md in the project root")
  VS Code         https://code.visualstudio.com/docs/agents/reference/mcp-configuration  (.vscode/mcp.json,
                  "servers", type "stdio", ${workspaceFolder}; cwd "Defaults to the workspace folder". The Local
                  harness that reads this file is deprecated, removal February 2027; the Copilot harness does
                  not read it directly but can forward its entries. The Copilot harness reads a workspace-root
                  .mcp.json directly (an experimental setting only decides whether MCP: Add Server offers it);
                  that path is also Claude Code's project MCP file, so devmem does not write it)
                  https://code.visualstudio.com/docs/copilot/customization/custom-instructions  (AGENTS.md)
  Codex CLI       https://learn.chatgpt.com/docs/extend/mcp?surface=cli  ([mcp_servers.<name>] in
                  .codex/config.toml, "trusted projects only"; ~/.codex/config.toml is global)
                  https://learn.chatgpt.com/docs/agent-configuration/agents-md  (AGENTS.md, 32 KiB default cap)
  Gemini CLI      https://geminicli.com/docs/tools/mcp-server/ and https://geminicli.com/docs/reference/configuration/
                  (.gemini/settings.json "mcpServers"; no workspace variable, cwd default not documented; string
                  values may reference $VAR / ${VAR} (and %VAR% on Windows), no escape documented)
                  https://geminicli.com/docs/cli/gemini-md/  (GEMINI.md in workspace directories and parents)
  Devin Desktop   https://docs.devin.ai/desktop/cascade/mcp  (the Windsurf docs redirect here) and
  (Windsurf)      https://docs.devin.ai/cli/extensibility/mcp/configuration.md  (.devin/mcp_config.local.json,
                  documented as the git-ignored local override; "mcpServers", command/args/env; no workspace
                  variable) and https://docs.devin.ai/desktop/cascade/memories  (AGENTS.md read as always-on)
  Claude Desktop  https://modelcontextprotocol.io/docs/develop/connect-local-servers  (global
                  claude_desktop_config.json only, absolute paths: printed, never written)

Project path in the MCP entry: `${workspaceFolder}` where the tool documents that variable for this file
(Cursor, VS Code), so the file works in every clone as long as the command is `devmem`. Everywhere else an
absolute path: no workspace variable is documented, and the directory a server is started in is not documented
either, so `--project .` could point at whatever folder the tool was launched from. Whenever the entry holds an
absolute path (project, interpreter, PYTHONPATH, --store), the report says the file is machine-specific.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import re
import shlex
import stat as _stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from . import proc
from .store import (STORE_DIR, _is_link, ensure_store, find_program, not_a_project, private_mkdir, read_regular,
                    safe_rel, write_private)

SERVER_NAME = "devmemory"
STATE_FILE = "connect.json"
BACKUP_DIR = "connect-backups"           # inside the store
LEGACY_BACKUP_SUFFIX = ".devmem-backup"   # the first version kept backups next to the file
TMP_SUFFIX = ".devmem-tmp"
CHECKED = "2026-11-14"
WORKSPACE_VAR = "${workspaceFolder}"

# The instruction block. Loaded into every prompt of the tool, so it stays short.
INSTRUCTIONS = """\
## devmem project memory
This project keeps a local memory, served by the `devmemory` MCP tools.
- At the start of a task, call `get_context_pack` with the user's request. Parts marked STALE may be outdated: check the code.
- Before editing, call `begin_session`. When done, call `end_session` with its pending id, the user's request and a short summary.
- Never put secrets (keys, passwords, tokens) in the summary.
- Skip begin/end when devmem already records the session (`devmem init` hooks, `devmem codex`, `devmem wrap`).
"""

# style -> (begin line, prefix that identifies a begin line, end line)
MARKERS = {
    "md": ("<!-- devmemory:begin (added by devmem connect; devmem disconnect removes it) -->", "<!-- devmemory:begin",
           "<!-- devmemory:end -->"),
    "toml": ("# devmemory:begin (added by devmem connect; devmem disconnect removes it)", "# devmemory:begin",
             "# devmemory:end"),
}


class ConnectError(Exception):
    """A problem the user can fix. Nothing was changed: either nothing was written, or what was written was put
    back (the message says so when that was not possible)."""


@dataclass(frozen=True)
class Tool:
    key: str
    label: str
    mcp_file: str | None = None        # project-relative, "/" separated
    mcp_format: str = "json"           # json | toml
    mcp_key: str = "mcpServers"        # top-level JSON object holding the servers
    stdio_type: bool = False           # add "type": "stdio" to the entry
    project_ref: str = "absolute"      # "absolute" or a documented workspace variable
    instructions: str | None = None    # project-relative instruction file
    global_hint: str = ""              # tools whose MCP config is only global: where to paste the printed snippet
    notes: tuple[str, ...] = ()
    docs: tuple[str, ...] = ()
    aliases: tuple[str, ...] = field(default_factory=tuple)
    expands_vars: bool = False         # the tool replaces $VAR / ${VAR} / %VAR% in config values, with no escape


# The tools. Adding one is a few lines; see the module docstring for the documentation each line comes from.
TOOLS: dict[str, Tool] = {t.key: t for t in [
    Tool("cursor", "Cursor", ".cursor/mcp.json", stdio_type=True, project_ref=WORKSPACE_VAR, instructions="AGENTS.md",
         docs=("https://cursor.com/docs/context/mcp", "https://cursor.com/docs/context/rules")),
    Tool("vscode", "VS Code / GitHub Copilot", ".vscode/mcp.json", mcp_key="servers", stdio_type=True,
         project_ref=WORKSPACE_VAR, instructions="AGENTS.md", aliases=("copilot",),
         notes=("VS Code reads .vscode/mcp.json in its Local harness (deprecated, removal planned for February "
                "2027); the Copilot harness does not read it directly but can forward its entries. The Copilot "
                "harness reads a workspace-root .mcp.json directly; devmem does not write that file because "
                "Claude Code also reads it as its project MCP config. To set it up there yourself, add the entry "
                "from `devmem connect generic --print` under \"mcpServers\" in .mcp.json.",),
         docs=("https://code.visualstudio.com/docs/agents/reference/mcp-configuration",
               "https://code.visualstudio.com/docs/copilot/customization/custom-instructions")),
    Tool("codex", "Codex CLI", ".codex/config.toml", mcp_format="toml", instructions="AGENTS.md",
         notes=("Codex reads a project's .codex/config.toml only when the project is trusted. Alternative for all "
                "projects (edits your global ~/.codex/config.toml, so devmem does not run it):\n{codex_add}",),
         docs=("https://learn.chatgpt.com/docs/extend/mcp?surface=cli",
               "https://learn.chatgpt.com/docs/agent-configuration/agents-md")),
    Tool("gemini", "Gemini CLI", ".gemini/settings.json", instructions="GEMINI.md", expands_vars=True,
         notes=("Gemini's documentation says that for unpaid-tier and Google One users Gemini CLI was replaced by "
                "Antigravity CLI on June 18th, 2026. devmem has no Antigravity target; use `devmem connect generic` "
                "and add the printed entry to Antigravity's MCP configuration.",),
         docs=("https://geminicli.com/docs/tools/mcp-server/", "https://geminicli.com/docs/reference/configuration/",
               "https://geminicli.com/docs/cli/gemini-md/")),
    Tool("devin", "Devin Desktop (formerly Windsurf)", ".devin/mcp_config.local.json", instructions="AGENTS.md",
         aliases=("windsurf",),
         notes=("Devin documents .devin/mcp_config.local.json as the git-ignored local override; check that your "
                ".gitignore covers it before committing.",),
         docs=("https://docs.devin.ai/desktop/cascade/mcp", "https://docs.devin.ai/cli/extensibility/mcp/configuration.md",
               "https://docs.devin.ai/desktop/cascade/memories")),
    Tool("claude-desktop", "Claude Desktop", global_hint="claude_desktop_config.json", aliases=("claude_desktop",),
         notes=("Claude Desktop has no project config file: merge the printed entry into {desktop_config} yourself "
                "(Settings > Developer > Edit Config), restart Claude Desktop, and paste the instruction text where "
                "you keep instructions.",),
         docs=("https://modelcontextprotocol.io/docs/develop/connect-local-servers",)),
    Tool("generic", "any other MCP client", instructions="AGENTS.md", global_hint="the client's MCP config",
         notes=("Add the printed entry to your client's MCP configuration (most use this mcpServers shape).",),
         docs=("https://agents.md/",)),
]}


def find_tool(name: str) -> Tool:
    n = (name or "").strip().lower()
    for t in TOOLS.values():
        if n == t.key or n in t.aliases:
            return t
    if n in ("claude", "claude-code", "claudecode"):
        raise ConnectError("Claude Code is set up with the devmem Claude Code plugin (/devmem:setup) or with "
                           "`devmem init`: hooks record more than MCP can")
    raise ConnectError(f"unknown tool {name!r}; supported: {', '.join(TOOLS)}")


# ---------------------------------------------------------------- the server command

def _probe_import(python: str) -> str | None:
    """Where `python -m devmem` would import devmem from when started outside this folder, or None."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    try:
        # `-c` puts the current folder on sys.path: a new private folder, not the shared temporary folder, where
        # another local user could leave a devmem.py
        with tempfile.TemporaryDirectory(prefix="devmem-probe-") as empty:
            r = proc.run([python, "-c", "import devmem,sys;sys.stdout.write(devmem.__file__)"], cwd=empty,
                         capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


# Windows: write the absolute path of `devmem` (found on PATH, never in the project) into a tool's config, since the
# tool would look for a bare `devmem` in its current folder (the project) first. Elsewhere PATH alone is searched.
ABSOLUTE_DEVMEM = os.name == "nt"


def server_command(command: str | None = None, which=find_program, python: str | None = None,
                   probe=_probe_import) -> tuple[str, list[str], dict, list[str]]:
    """(command, leading args, env, warnings). Default: `devmem` when it is on PATH, else this interpreter with
    `-m devmem`. When that interpreter would not import THIS devmem from another folder (not installed, or an
    install of another checkout), PYTHONPATH points at the folder holding this package."""
    warnings: list[str] = []
    mode = command or ("devmem" if which("devmem") else "python")
    if mode == "devmem":
        found = which("devmem")
        if not found:
            warnings.append("`devmem` is not on PATH here; the tool will not be able to start the server unless "
                            "it is on the tool's PATH")
        elif ABSOLUTE_DEVMEM:
            # Windows tools start a bare command name by searching their current folder (the project) before PATH,
            # so a `devmem.cmd` a repository ships would run instead: write the absolute path found on PATH
            return found, [], {}, warnings
        return "devmem", [], {}, warnings
    if mode != "python":
        raise ConnectError(f"--command must be devmem or python, not {command!r}")
    py = os.path.abspath(python or sys.executable)   # not resolve(): a venv's python is often a symlink
    here = Path(__file__).resolve().parent
    found = probe(py)
    env = {}
    if not found or Path(found).resolve().parent != here:
        env["PYTHONPATH"] = str(here.parent)
    return py, ["-m", "devmem"], env, warnings


def _entry_values(entry: dict) -> list[str]:
    return [str(entry.get("command") or ""), *[str(a) for a in entry.get("args") or []],
            *[str(v) for v in (entry.get("env") or {}).values()]]


def server_entry(tool: Tool, project: Path, command: str | None = None, store: Path | None = None,
                 which=find_program, python: str | None = None, probe=_probe_import) -> tuple[dict, list[str]]:
    cmd, lead, env, warnings = server_command(command, which, python, probe)
    ref = tool.project_ref if tool.project_ref != "absolute" else str(project)
    args = [*lead, "mcp", "--project", ref]
    if store:
        args += ["--store", str(Path(store).resolve())]
    entry: dict = {"type": "stdio"} if tool.stdio_type else {}
    entry.update(command=cmd, args=args)
    if env:
        entry["env"] = env
    for v in _entry_values(entry):
        try:
            v.encode("utf-8")
        except UnicodeEncodeError:
            raise ConnectError(f"{v!r} is not valid Unicode (a file name in another encoding?); it cannot be written "
                               "to a config file. Not modified") from None
    return entry, warnings


# $VAR, ${VAR} (also ${VAR:-default}) and %VAR%: what Gemini CLI replaces in settings values
_EXPANDED = re.compile(r"\$\{|\$[A-Za-z_]|%[^%]*%")


def _check_expansion(tool: Tool, entry: dict) -> None:
    if not tool.expands_vars:
        return
    bad = [v for v in _entry_values(entry) if _EXPANDED.search(v)]
    if bad:
        raise ConnectError(f"{tool.label} replaces $NAME, ${{NAME}} and (on Windows) %NAME% in its settings with "
                           f"environment variables (an undefined one becomes empty) and documents no escape, so it "
                           f"would not start {bad[0]!r} as written. Not modified. Use a project folder (and --store) "
                           "whose path has no such sequence")


_TOML_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _toml_str(s: str) -> str:
    """A TOML basic string: every character TOML forbids unescaped (", \\, U+0000-U+001F, U+007F) is escaped."""
    out = []
    for ch in s:
        o = ord(ch)
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\u{o:04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def toml_block(entry: dict) -> str:
    lines = [f"[mcp_servers.{SERVER_NAME}]", f"command = {_toml_str(entry['command'])}",
             "args = [" + ", ".join(_toml_str(a) for a in entry["args"]) + "]"]
    if entry.get("env"):
        lines.append(f"[mcp_servers.{SERVER_NAME}.env]")
        lines += [f"{k} = {_toml_str(v)}" for k, v in entry["env"].items()]
    return "\n".join(lines) + "\n"


def posix_quote(s: str) -> str:
    """For sh, bash, zsh: single quotes, inside which nothing is expanded; a ' is written as '"'"'."""
    return shlex.quote(s)


_PS_SAFE = re.compile(r"[A-Za-z0-9_./:\\=+-]+")


def ps_quote(s: str) -> str:
    """For PowerShell: single quotes, inside which nothing is expanded; each quote character (PowerShell also
    treats the typographic single quotes as quotes) is doubled."""
    if _PS_SAFE.fullmatch(s):
        return s
    return "'" + re.sub("['‘’‚‛]", lambda m: m.group(0) * 2, s) + "'"


def codex_add_command(entry: dict) -> str:
    """The `codex mcp add` line, once per shell family, each quoted so that nothing in a path is expanded."""
    out = []
    for label, q in (("POSIX shells (bash, zsh, sh)", posix_quote), ("PowerShell", ps_quote)):
        words = ["codex", "mcp", "add", SERVER_NAME]
        for k, v in (entry.get("env") or {}).items():
            words += ["--env", q(f"{k}={v}")]
        words += ["--", *(q(a) for a in [entry["command"], *entry["args"]])]
        out.append(f"  {label}: {' '.join(words)}")
    out.append("  (no line for cmd.exe: it expands %NAME% even inside quotes)")
    return "\n".join(out)


def desktop_config_path() -> str:
    if sys.platform == "win32":
        return r"%APPDATA%\Claude\claude_desktop_config.json"
    if sys.platform == "darwin":
        return "~/Library/Application Support/Claude/claude_desktop_config.json"
    return "~/.config/Claude/claude_desktop_config.json"


# ---------------------------------------------------------------- reading and writing files

@dataclass
class Change:
    rel: str                   # the file as the tool names it
    path: Path                 # the file written: a link inside the project is followed
    old: bytes | None          # None: the file does not exist
    new: bytes | None          # None: delete the file
    verb: str
    preview: str = ""
    key: str = ""              # project-relative path of `path`: the key in the state file
    pristine: bool = False     # connect: the existing file holds nothing of devmem's, so it may be backed up
    meta: dict = field(default_factory=dict)      # connect: added_key (JSON) / added_eol (text), for disconnect
    mkdirs: list[str] = field(default_factory=list)   # connect: folders created for a new file, innermost first
    rmdirs: list[str] = field(default_factory=list)   # disconnect: folders to remove (if empty) after a delete
    backup_path: Path | None = None   # set when a backup is (or, with --print, would be) made
    cleanup: bool = False      # disconnect: afterwards the file holds nothing of devmem's
    forced_copy: Path | None = None   # --force: a copy of the file as it was, kept in the store's backup folder

    @property
    def changed(self) -> bool:
        return self.old != self.new


def _check_project(project) -> Path:
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise ConnectError(f"{project} is not a directory")
    why = not_a_project(project)
    if why == "root":
        raise ConnectError(f"refusing to connect {project}: it is a drive or file-system root, not a project")
    if why:
        # ~/.cursor/mcp.json, ~/.gemini/settings.json, ~/.codex/config.toml are the user's GLOBAL configs
        raise ConnectError("refusing to connect the home directory or a folder that contains it: the home's .cursor, "
                           ".gemini and .codex folders are the global configuration of those tools, not a project's")
    return project


def _target(project: Path, rel: str) -> tuple[Path, str]:
    """(the file to read and write, its project-relative key). A link is followed so that it survives (the file
    it points at is changed); a link leading outside the project is refused."""
    p = project.joinpath(*rel.split("/"))
    real = p.resolve()
    try:
        key = real.relative_to(project).as_posix()
    except ValueError:
        raise ConnectError(f"{p} resolves outside the project (a link to another folder or file?); not modified") from None
    if key in ("", "."):
        raise ConnectError(f"{p} resolves to the project folder itself; not modified")
    return real, key


CONNECT_READ_CAP = 16 * 1024 * 1024    # an instruction or MCP file larger than this is not one devmem edits


def _read(path: Path, strict: bool = True) -> bytes | None:
    """The bytes of a file connect may change, None when it does not exist. Read only as a plain file (never a FIFO,
    which would block, a device, or a link: `_target` has already resolved the path inside the project), at most
    CONNECT_READ_CAP bytes; anything else raises ConnectError, or (strict=False) counts as absent."""
    if not os.path.lexists(path):
        return None
    data = read_regular(path, CONNECT_READ_CAP)
    if data is None and strict:
        raise ConnectError(f"cannot read {path}: not a plain file, unreadable, or over "
                           f"{CONNECT_READ_CAP // (1024 * 1024)} MB; not modified")
    return data


def _decode(path: Path, raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConnectError(f"{path} is not UTF-8 text; not modified") from None


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _backup_name(key: str) -> str:
    """One file name per target: the project-relative path with "/" and "%" percent-encoded (reversible, so two
    targets never share a name)."""
    return quote(key, safe="")


def _own_backup(key: str, name) -> str | None:
    """`name` when it is the backup name devmem gives the target `key` (the only name it ever records), else None: a
    name from a shipped connect.json (`../../x`, an absolute path, another file in the store) is never used."""
    return name if isinstance(name, str) and name == _backup_name(key) else None


def _own_dirs(key: str, dirs) -> list[str]:
    """The folders connect may have created for the target `key`: its ancestors inside the project, innermost first.
    Anything else a state file lists (`..`, an absolute path, an unrelated folder) is dropped."""
    parts = key.split("/")[:-1]
    ancestors = ["/".join(parts[:i]) for i in range(len(parts), 0, -1)]
    return [d for d in ancestors if isinstance(dirs, list) and d in dirs]


def _deletable(path: Path, folder: Path) -> bool:
    """May devmem delete `path`, a file it made in `folder`? Only a regular file (not a link) whose real parent is
    `folder`, which is not a link either."""
    try:
        return (not _is_link(folder) and not _is_link(path) and os.path.isdir(folder)
                and _stat.S_ISREG(os.lstat(path).st_mode)
                and os.path.normcase(os.path.realpath(path.parent)) == os.path.normcase(os.path.realpath(folder)))
    except OSError:
        return False


def _state_path(project: Path) -> Path:
    return project / STORE_DIR / STATE_FILE


def read_state(project: Path) -> dict:
    try:
        d = json.loads((read_regular(_state_path(project), 16 * 1024 * 1024) or b"null").decode("utf-8"))
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    for k in ("tools", "created", "written"):
        if not isinstance(d.get(k), dict):
            d[k] = {}
    return d


def _write_state(project: Path, state: dict) -> None:
    ensure_store(project / STORE_DIR)
    _write_atomic(_state_path(project), (json.dumps(state, indent=1, ensure_ascii=False) + "\n").encode("utf-8"),
                  private=True)


def _write_atomic(path: Path, data: bytes, private: bool = False) -> None:
    """Replace `path` with `data` through a temporary file. private: a file in the store (0600 on POSIX); the tools'
    configuration files keep the usual permissions."""
    tmp = path.with_name(path.name + TMP_SUFFIX)
    try:
        if private:
            write_private(tmp, data)     # removes a leftover entry (never its target), then O_EXCL
        else:
            _write_new(tmp, data)
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        if e.errno is not None and e.strerror:
            raise type(e)(e.errno, e.strerror, str(path)) from None   # name the file, not the temporary one
        raise


def _write_new(tmp: Path, data: bytes) -> None:
    """A project config file's temporary copy: a leftover at that name (a repository can ship one, or a link there)
    is removed, the entry itself and never its target, then the file is created exclusively (O_EXCL, O_NOFOLLOW) with
    the usual permissions (0666 minus umask), as a plain write would."""
    if os.path.lexists(tmp):
        os.unlink(tmp)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0), 0o666)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _valid_backup(project: Path, key: str, rec: dict, raw: bytes) -> bytes | None:
    """The backup's bytes when the file is exactly what connect last wrote (so restoring loses nothing)."""
    name = _own_backup(key, rec.get("backup"))
    if name is None or safe_rel(name) is None or rec.get("sha256") != _sha(raw):
        return None          # only the name devmem gives this target, inside the backup folder
    folder = project / STORE_DIR / BACKUP_DIR
    if _is_link(project / STORE_DIR) or _is_link(folder):
        return None
    return read_regular(folder / name, 64 * 1024 * 1024)


# ---- JSON MCP files

def _load_json(path: Path, raw: bytes | None, tool: Tool) -> dict:
    if raw is None:
        return {}
    text = _decode(path, raw).lstrip("﻿")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ConnectError(f"{path} is not plain JSON ({e}; comments are not supported); not modified. "
                           f"Add the entry under \"{tool.mcp_key}\" by hand (`devmem connect {tool.key} --print` shows it)") from None
    if not isinstance(data, dict):
        raise ConnectError(f"{path} does not hold a JSON object; not modified")
    if not isinstance(data.get(tool.mcp_key, {}), dict):
        raise ConnectError(f"\"{tool.mcp_key}\" in {path} is not an object; not modified")
    return data


def _dump_json(data: dict, like: bytes | None) -> bytes:
    """Serialise `data` the way the existing file was written: indent (or all on one line), line ending, final
    line ending or none, BOM."""
    indent: str | int | None = 2
    text = ""
    final = "\n"
    if like:
        text = like.decode("utf-8", errors="replace")
        body = text.lstrip("﻿").strip()
        m = re.search(r"\n([ \t]+)\S", text)
        if body and "\n" not in body:
            indent = None
        elif m:
            indent = m.group(1)
        if body and not text.endswith("\n"):
            final = ""
    out = json.dumps(data, indent=indent, ensure_ascii=False) + final
    out = out.replace("\n", _eol(text))
    return (b"\xef\xbb\xbf" if text.startswith("﻿") else b"") + out.encode("utf-8")


def _plan_json_add(project: Path, tool: Tool, rel: str, entry: dict) -> Change:
    path, key = _target(project, rel)
    raw = _read(path)
    data = _load_json(path, raw, tool)
    servers = dict(data.get(tool.mcp_key) or {})
    if servers.get(SERVER_NAME) == entry:
        return Change(rel, path, raw, raw, "unchanged (already connected)", key=key)
    new = dict(data)
    had = SERVER_NAME in servers
    servers[SERVER_NAME] = entry
    new[tool.mcp_key] = servers
    out = _dump_json(new, raw)
    verb = "create" if raw is None else ("update the devmemory entry in" if had else "add the devmemory entry to")
    return Change(rel, path, raw, out, verb, preview=out.decode("utf-8").lstrip("﻿"), key=key,
                  pristine=raw is not None and not had, meta={"added_key": tool.mcp_key not in data})


def _plan_json_remove(project: Path, tool: Tool, rel: str, rec: dict, created: bool) -> Change | None:
    path, key = _target(project, rel)
    raw = _read(path)
    if raw is None:
        return None
    data = _load_json(path, raw, tool)
    servers = data.get(tool.mcp_key)
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return Change(rel, path, raw, raw, "unchanged (no devmemory entry)", key=key, cleanup=True)
    back = _valid_backup(project, key, rec, raw)
    if back is not None:
        return Change(rel, path, raw, back, "restore the backup of", preview="(the file as it was before connect)",
                      key=key, cleanup=True)
    new = dict(data)
    rest = {k: v for k, v in servers.items() if k != SERVER_NAME}
    if rest or not rec.get("added_key", created):
        new[tool.mcp_key] = rest
    else:
        new.pop(tool.mcp_key, None)
    if created and not new:
        return Change(rel, path, raw, None, "delete (devmem created it and nothing else is in it)", key=key, cleanup=True)
    out = _dump_json(new, raw)
    return Change(rel, path, raw, out, "remove the devmemory entry from", preview=out.decode("utf-8").lstrip("﻿"),
                  key=key, cleanup=True)


# ---- marked text blocks (instruction files, Codex TOML)

BOM = chr(0xFEFF)


def _find_block(path: Path, lines: list[str], style: str) -> tuple[int, int] | None:
    _begin, prefix, end = MARKERS[style]
    # a UTF-8 byte order mark in front of the first line is not whitespace to strip(): ignore it here
    starts = [i for i, ln in enumerate(lines) if ln.lstrip(BOM).strip().startswith(prefix)]
    ends = [i for i, ln in enumerate(lines) if ln.lstrip(BOM).strip() == end]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise ConnectError(f"{path} has unmatched or repeated devmemory marker lines; fix them by hand. Not modified")
    return starts[0], ends[0]


def _block_lines(body: str, style: str, eol: str) -> list[str]:
    begin, _prefix, end = MARKERS[style]
    return [ln + eol for ln in [begin, *body.rstrip("\n").split("\n"), end]]


def _block_text(lines: list[str], found: tuple[int, int]) -> str:
    """The block between (and with) its marker lines, exactly as in the file; a byte order mark in front is not part
    of it."""
    b, e = found
    return "".join([lines[b].lstrip(BOM) if b == 0 else lines[b], *lines[b + 1:e + 1]])


def block_sha(lines: list[str], found: tuple[int, int]) -> str:
    return _sha(_block_text(lines, found).encode("utf-8"))


_TOML_S = r'"(?:[^"\\\x00-\x1f\x7f]|\\[btnfr"\\]|\\u[0-9A-F]{4})*"'
_TOML_BODY = re.compile(r"\[mcp_servers\.devmemory\]\ncommand = (" + _TOML_S + r")\nargs = \[((?:" + _TOML_S +
                        r")(?:, " + _TOML_S + r")*)?\]\n(\[mcp_servers\.devmemory\.env\]\n(?:[A-Za-z0-9_-]+ = " +
                        _TOML_S + r"\n)+)?")


def _toml_body_entry(body: str) -> dict | None:
    """The entry a toml_block() body was written for, or None when `body` is not exactly such a body."""
    m = _TOML_BODY.fullmatch(body)
    if not m:
        return None
    try:
        entry: dict = {"command": json.loads(m.group(1)), "args": json.loads("[" + (m.group(2) or "") + "]")}
        if m.group(3):
            entry["env"] = {}
            for ln in m.group(3).split("\n")[1:-1]:
                k, v = ln.split(" = ", 1)
                entry["env"][k] = json.loads(v)
    except ValueError:
        return None
    return entry


def _is_current_block(lines: list[str], found: tuple[int, int], style: str) -> bool:
    """Is the block byte for byte what this version of connect writes for the same inputs (the instruction text, or
    the server entry it names)? Such a block holds nothing but devmem's text, whoever wrote it."""
    text = _block_text(lines, found)
    eol = "\r\n" if text.endswith("\r\n") else "\n"
    inner = "".join(lines[found[0] + 1:found[1]])
    if style == "md":
        body = INSTRUCTIONS
    else:
        entry = _toml_body_entry(inner.replace("\r\n", "\n") if eol == "\r\n" else inner)
        if entry is None:
            return False
        body = toml_block(entry)
    return text == "".join(_block_lines(body, style, eol))


def _owns_block(lines: list[str], found: tuple[int, int], style: str, rec: dict, raw: bytes | None) -> bool:
    """May connect/disconnect replace or remove this block? Only when it is provably devmem's: its bytes hash to what
    connect recorded writing (`block_sha256`, in .devmemory/connect.json, not in the file), or the whole file is
    exactly what an earlier connect wrote (its `sha256`: a version before block hashes, file untouched since), or the
    block is exactly what this version writes for the same inputs. A marker pair in a code fence, a TOML string or
    a block someone edited is none of those."""
    if isinstance(rec.get("block_sha256"), str) and rec["block_sha256"] == block_sha(lines, found):
        return True
    if raw is not None and isinstance(rec.get("sha256"), str) and rec["sha256"] == _sha(raw):
        return True
    return _is_current_block(lines, found, style)


def _not_owned(path: Path, verb: str, tool: str, store_ok: bool = True) -> ConnectError:
    """store_ok=False: the memory store is not this user's, so --force (which keeps its copy there) is refused too."""
    if not store_ok:
        return ConnectError(f"{path} has devmemory marker lines around text devmem did not write (or that was changed "
                            "since); not modified. Edit the file by hand (the memory store is not yours, `devmem "
                            "status` says why, so devmem keeps no copy of the file in it)")
    return ConnectError(f"{path} has devmemory marker lines around text devmem did not write (or that was changed "
                        f"since); not modified. Edit the file by hand, or run `devmem {verb} {tool} --force`, which "
                        f"keeps a copy of the file in {STORE_DIR}/{BACKUP_DIR}/ first")


def _forced_copy(project: Path, key: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return project / STORE_DIR / BACKUP_DIR / f"{_backup_name(key)}.forced-{stamp}-{os.getpid()}"


def _toml_loads(text: str) -> dict | None:
    """Parsed TOML, or None on Python 3.10 (no tomllib). Raises ValueError (TOMLDecodeError) on invalid TOML."""
    try:
        tomllib = importlib.import_module("tomllib")
    except ImportError:
        return None
    return tomllib.loads(text)


def _toml_conflict(lines: list[str], skip: tuple[int, int] | None) -> bool:
    """Does the TOML already define mcp_servers.devmemory outside devmem's block (so appending would duplicate a
    key), or set mcp_servers as an inline value (so a [mcp_servers.x] table cannot be added)? Text check only,
    used where tomllib is missing (Python 3.10)."""
    table = ""
    q = r"""["']?"""
    name = rf"""\s*{q}devmemory{q}\s*"""
    for i, ln in enumerate(lines):
        if skip and skip[0] <= i <= skip[1]:
            continue
        s = ln.strip()
        m = re.match(r"^\[\[?([^\]]*)\]\]?", s)
        if m:
            table = re.sub(r"\s+", "", m.group(1)).replace('"', "").replace("'", "")
            if table == "mcp_servers.devmemory" or table.startswith("mcp_servers.devmemory."):
                return True
            continue
        if table == "" and re.match(rf"^{q}mcp_servers{q}\s*(=|\.{name}[=.])", s):
            return True
        if table == "mcp_servers" and re.match(rf"^{name}[=.]", s):
            return True
    return False


_DEFINES = "already defines mcp_servers.devmemory (or sets mcp_servers inline); not modified. " \
           "Remove that entry first, or keep it and skip this step"


def _check_toml(path: Path, lines: list[str], found: tuple[int, int] | None, new_text: str, entry: dict) -> None:
    """Refuse to write a .codex/config.toml that would not parse or would not hold exactly devmem's entry."""
    rest = "".join(lines[:found[0]] + lines[found[1] + 1:]) if found else "".join(lines)
    try:
        before = _toml_loads(rest)
    except ValueError as e:
        raise ConnectError(f"{path} is not valid TOML ({e}); not modified") from None
    if before is None:                                  # Python 3.10: text checks only
        if _toml_conflict(lines, found):
            raise ConnectError(f"{path} {_DEFINES}")
        return
    servers = before.get("mcp_servers")
    if servers is not None and (not isinstance(servers, dict) or SERVER_NAME in servers):
        raise ConnectError(f"{path} {_DEFINES}")
    try:
        after = _toml_loads(new_text) or {}
    except ValueError as e:
        raise ConnectError(f"adding devmem's table would make {path} invalid TOML ({e}; mcp_servers set inline?); "
                           "not modified") from None
    want = {"command": entry["command"], "args": entry["args"], **({"env": entry["env"]} if entry.get("env") else {})}
    if (after.get("mcp_servers") or {}).get(SERVER_NAME) != want:
        raise ConnectError(f"{path} would not hold the intended mcp_servers.devmemory entry after the change; not modified")


def _plan_block_add(project: Path, rel: str, body: str, style: str, entry: dict | None = None,
                    state: dict | None = None, force: bool = False, tool: str = "") -> Change:
    path, key = _target(project, rel)
    rec = ((state or {}).get("written") or {}).get(key) or {}
    raw = _read(path)
    text = _decode(path, raw) if raw is not None else ""
    eol = _eol(text)
    lines = text.splitlines(keepends=True)
    found = _find_block(path, lines, style)
    block = _block_lines(body, style, eol)
    added_eol = False
    forced = None
    if found:
        b, e = found
        bom = BOM if b == 0 and lines[0].startswith(BOM) else ""     # kept in front of the block it precedes
        block = [bom + block[0], *block[1:]]
        if lines[b:e + 1] == block:
            return Change(rel, path, raw, raw, "unchanged (block already there)", key=key)
        if not _owns_block(lines, found, style, rec, raw):
            if not force:
                raise _not_owned(path, "connect", tool)
            forced = _forced_copy(project, key)
        new_lines, verb = lines[:b] + block + lines[e + 1:], "replace the devmemory block in"
    else:
        sep: list[str] = []
        if text:
            added_eol = not text.endswith(("\n", "\r"))
            sep = ([eol] if added_eol else []) + [eol]
        new_lines, verb = lines + sep + block, ("create" if raw is None else "append a devmemory block to")
    out = "".join(new_lines)
    if style == "toml" and entry is not None:
        _check_toml(path, lines, found, out, entry)
    return Change(rel, path, raw, out.encode("utf-8"), verb, preview="".join(block).replace("\r\n", "\n"), key=key,
                  pristine=raw is not None and not found, forced_copy=forced,
                  meta={"added_eol": added_eol, "block_sha256": _sha("".join(block).lstrip(BOM).encode("utf-8"))})


def _plan_block_remove(project: Path, rel: str, style: str, rec: dict, created: bool, force: bool = False,
                       tool: str = "", store_ok: bool = True) -> Change | None:
    path, key = _target(project, rel)
    raw = _read(path)
    if raw is None:
        return None
    text = _decode(path, raw)
    lines = text.splitlines(keepends=True)
    found = _find_block(path, lines, style)
    if not found:
        return Change(rel, path, raw, raw, "unchanged (no devmemory block)", key=key, cleanup=True)
    forced = None
    if not _owns_block(lines, found, style, rec, raw):
        if not force:
            raise _not_owned(path, "disconnect", tool, store_ok)
        forced = _forced_copy(project, key)
    c = _plan_block_removal(project, rel, path, key, raw, lines, found, rec, created)
    c.forced_copy = forced
    return c


def _plan_block_removal(project: Path, rel: str, path: Path, key: str, raw: bytes, lines: list[str],
                        found: tuple[int, int], rec: dict, created: bool) -> Change:
    back = _valid_backup(project, key, rec, raw)
    if back is not None:
        return Change(rel, path, raw, back, "restore the backup of", preview="(the file as it was before connect)",
                      key=key, cleanup=True)
    b, e = found
    if b > 0 and not lines[b - 1].strip():
        b -= 1                                   # the blank line connect put before the block
    rest = "".join(lines[:b] + lines[e + 1:])
    if b == 0 and lines[0].startswith(BOM) and not rest.startswith(BOM):
        rest = BOM + rest                        # the file's byte order mark was in front of the block
    last = e + 1 == len(lines)
    if last and rec.get("added_eol"):
        # connect added a line ending to a file that did not end with one
        rest = rest[:-2] if rest.endswith("\r\n") else rest[:-1] if rest.endswith(("\n", "\r")) else rest
    elif not rec:
        # a project connected by the first version: backups next to the file, no hashes. Restore that backup
        # only when it is byte for byte what removing the block gives (allowing for that added line ending)
        legacy = _read(path.with_name(path.name + LEGACY_BACKUP_SUFFIX), strict=False)   # a link: absent
        before = legacy.decode("utf-8", errors="strict") if legacy is not None and _is_utf8(legacy) else None
        if legacy is not None and before is not None and (
                rest == before or (last and rest.rstrip("\r\n") == before and rest[len(before):] in ("\n", "\r\n"))):
            return Change(rel, path, raw, legacy, "restore the backup of", preview="(the file as it was before connect)",
                          key=key, cleanup=True)
    if created and not rest.strip():
        return Change(rel, path, raw, None, "delete (devmem created it and nothing else is in it)", key=key, cleanup=True)
    return Change(rel, path, raw, rest.encode("utf-8"), "remove the devmemory block from", key=key, cleanup=True)


def _is_utf8(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


# ---------------------------------------------------------------- writing, all or nothing

def _missing_dirs(project: Path, path: Path) -> list[str]:
    out = []
    d = path.parent
    while d != project and not d.exists():
        out.append(d.relative_to(project).as_posix())
        d = d.parent
    return out


def _rmdirs(project: Path, dirs: list[str]) -> None:
    for d in dirs:                                   # innermost first; only empty folders go, never a link
        p = project.joinpath(*d.split("/"))
        if safe_rel(d) is None or _is_link(p):
            break
        try:
            p.rmdir()
        except OSError:
            break


def _write_change(project: Path, c: Change) -> None:
    if c.new is None:
        c.path.unlink()
        _rmdirs(project, c.rmdirs)
        return
    if c.old is None:
        c.path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(c.path, c.new)


def _undo_change(project: Path, c: Change) -> None:
    if c.old is None:
        try:
            c.path.unlink()
        except FileNotFoundError:
            pass
        _rmdirs(project, c.mkdirs)
        return
    c.path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(c.path, c.old)


def _oserror(e: OSError) -> str:
    return f"{e.filename}: {e.strerror}" if e.filename and e.strerror else str(e)


class _Txn:
    """Backups, the state file and the project files of one connect or disconnect, all or nothing: on an OSError
    everything done so far is put back and a ConnectError is raised."""

    def __init__(self, project: Path, store_ok: bool = True):
        self.project = project
        self.store_ok = store_ok                  # False: a store not this user's, never written (disconnect)
        self.store = project / STORE_DIR
        self.store_existed = self.store.exists()
        self.gitignore_existed = (self.store / ".gitignore").exists()
        self.state_before = _read_raw(_state_path(project))
        self.backups: list[Path] = []
        self.done: list[Change] = []
        self.state_written = False

    def backup(self, c: Change) -> None:
        assert c.backup_path is not None and c.old is not None
        ensure_store(self.store)
        private_mkdir(c.backup_path.parent)
        self.backups.append(c.backup_path)
        _write_atomic(c.backup_path, c.old, private=True)

    def keep_copy(self, c: Change) -> None:
        """--force: the file as it was, kept after a successful run (removed again if the run fails)."""
        assert c.forced_copy is not None and c.old is not None
        ensure_store(self.store)
        private_mkdir(c.forced_copy.parent)
        self.backups.append(c.forced_copy)
        _write_atomic(c.forced_copy, c.old, private=True)

    def state(self, state: dict) -> None:
        self.state_written = True
        _write_state(self.project, state)

    def change(self, c: Change) -> None:
        try:
            _write_change(self.project, c)
        except OSError:
            if c.old is None:
                _rmdirs(self.project, c.mkdirs)       # folders made for a file that could not be written
            raise
        self.done.append(c)

    def fail(self, e: OSError) -> ConnectError:
        failed = []
        for c in reversed(self.done):
            try:
                _undo_change(self.project, c)
            except OSError as u:
                failed.append(f"{c.path} ({_oserror(u)})")
        if failed:   # keep the new state: it records what connect wrote, so `devmem disconnect` can clean up
            return ConnectError(f"cannot write {_oserror(e)}. It was rolled back except for: {', '.join(failed)}; "
                                "could not undo those. Run `devmem disconnect` for this tool, or fix them by hand")
        for b in self.backups:
            try:
                b.unlink()
            except OSError:
                pass
        if self.store_ok:
            _rmdirs(self.project, [f"{STORE_DIR}/{BACKUP_DIR}"])
        if self.state_written:
            try:
                if self.state_before is None:
                    _state_path(self.project).unlink()
                else:
                    _write_atomic(_state_path(self.project), self.state_before, private=True)
            except OSError:
                pass
        if not self.store_existed:   # the store this run created for its backup or state, nothing else in it
            if not self.gitignore_existed:
                try:
                    (self.store / ".gitignore").unlink()
                except OSError:
                    pass
            _rmdirs(self.project, [STORE_DIR])
        return ConnectError(f"cannot write {_oserror(e)}; nothing was changed")


def _read_raw(path: Path) -> bytes | None:
    return read_regular(path, 64 * 1024 * 1024)      # a plain regular file only (never a FIFO)


def _report(tool: Tool, changes: list[Change], printed: list[tuple[str, str]], notes: list[str], dry_run: bool) -> dict:
    return {"tool": tool.key, "label": tool.label, "dry_run": dry_run,
            "changes": [{"file": str(c.path), "rel": c.rel, "verb": c.verb, "changed": c.changed, "preview": c.preview,
                         "backup": str(c.backup_path or c.forced_copy) if (c.backup_path or c.forced_copy) else None}
                        for c in changes],
            "printed": printed, "notes": notes, "docs": list(tool.docs)}


def _link_notes(changes: list[Change]) -> list[str]:
    return [f"{c.rel} is a link to {c.key}: {c.key} is changed and the link is kept." for c in changes
            if os.path.normcase(c.rel) != os.path.normcase(c.key)]


def _machine_note(tool: Tool, entry: dict) -> str | None:
    paths = [v for v in _entry_values(entry) if any(os.path.isabs(p) for p in v.split(os.pathsep) if p)]
    if not paths:
        return None
    shown = ", ".join(dict.fromkeys(paths))
    if tool.mcp_file:
        return (f"The entry in {tool.mcp_file} holds absolute paths of this machine ({shown}), so the file is "
                f"machine-specific: do not commit it; teammates run `devmem connect {tool.key}` themselves.")
    return (f"The printed entry holds absolute paths of this machine ({shown}), so it is machine-specific: on "
            f"another machine run `devmem connect {tool.key}` there.")


# ---------------------------------------------------------------- connect / disconnect

def connect(project, tool_name: str, command: str | None = None, store=None, dry_run: bool = False,
            which=find_program, python: str | None = None, probe=_probe_import, force: bool = False,
            adopt_store: bool = False) -> dict:
    """Connect `tool_name` to devmem in `project`. All files are planned first; if any of them cannot be
    handled (malformed, unmatched markers) nothing is written. If a write fails, everything this call changed is
    put back and ConnectError is raised. dry_run=True writes nothing. adopt_store=True: the user adopts the store
    the entry names (devmem.consent.claim_store); without it a store this user did not create or adopt stays unused
    by the MCP tools, and a note says so."""
    tool = find_tool(tool_name)
    project = _check_project(project)
    own = project / STORE_DIR
    if not dry_run and not adopt_store and own.is_dir():    # connect keeps its notes there: never in a refused store
        from .consent import store_refusal
        why = store_refusal(project, own)
        if why:
            raise ConnectError(f"nothing connected: devmem keeps its connection notes in {own}, which is not used "
                               f"now. {why}")
    entry, warnings = server_entry(tool, project, command, store, which, python, probe)
    _check_expansion(tool, entry)
    changes: list[Change] = []
    printed: list[tuple[str, str]] = []
    state = read_state(project)
    mcp_file = tool.mcp_file
    if mcp_file and tool.mcp_format == "json":
        changes.append(_plan_json_add(project, tool, mcp_file, entry))
    elif mcp_file:
        changes.append(_plan_block_add(project, mcp_file, toml_block(entry), "toml", entry, state, force, tool.key))
    else:
        printed.append((f"MCP entry: merge into {tool.global_hint}" if tool.key != "claude-desktop"
                        else f"MCP entry: merge into {desktop_config_path()}",
                        json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2, ensure_ascii=False)))
    if tool.instructions:
        changes.append(_plan_block_add(project, tool.instructions, INSTRUCTIONS, "md", None, state, force, tool.key))
    if tool.key in ("claude-desktop", "generic"):
        printed.append(("Instruction text" + (" (also written to AGENTS.md)" if tool.instructions else ""), INSTRUCTIONS))
    notes = list(warnings) + [n.format(codex_add=codex_add_command(entry), desktop_config=desktop_config_path())
                              for n in tool.notes]
    machine = _machine_note(tool, entry)
    if machine:
        notes.append(machine)
    if entry["command"] != "devmem":
        notes.append("The entry runs this Python interpreter (`devmem` is not on PATH, or --command python was given).")
    notes += _link_notes(changes)

    new_state = copy.deepcopy(state)
    for c in changes:
        if not c.changed or c.new is None:
            continue
        rec = state["written"].get(c.key) or {}
        newrec: dict = {"sha256": _sha(c.new), "backup": None}
        for k in ("added_key", "added_eol"):
            if k in rec:
                newrec[k] = rec[k]
            elif k in c.meta:
                newrec[k] = c.meta[k]
        if c.meta.get("block_sha256"):
            newrec["block_sha256"] = c.meta["block_sha256"]   # the exact block written: what disconnect may remove
        olds = rec.get("old_backups")
        old_backups = [n for n in olds if _own_backup(c.key, n)] if isinstance(olds, list) else []
        if c.old is None:
            c.mkdirs = _missing_dirs(project, c.path)
            new_state["created"][c.key] = {"dirs": c.mkdirs}
        elif _own_backup(c.key, rec.get("backup")) and rec.get("sha256") == _sha(c.old):
            newrec["backup"] = rec["backup"]              # only devmem changed the file since that backup
        elif c.pristine and c.key not in state["created"]:
            name = _backup_name(c.key)
            c.backup_path = project / STORE_DIR / BACKUP_DIR / name
            newrec["backup"] = name
        if _own_backup(c.key, rec.get("backup")) and rec["backup"] != newrec["backup"]:
            old_backups.append(rec["backup"])             # no longer usable for a restore: deleted at disconnect
        if old_backups:
            newrec["old_backups"] = sorted(set(old_backups) - {newrec["backup"]})
        new_state["written"][c.key] = newrec
    new_state["tools"][tool.key] = {"files": [c.rel for c in changes], "command": entry["command"]}
    if not dry_run:
        txn = _Txn(project)
        try:
            for c in changes:
                if c.backup_path is not None:
                    txn.backup(c)
                if c.forced_copy is not None:
                    txn.keep_copy(c)
            txn.state(new_state)      # first, so that a later disconnect can clean up whatever happens next
            for c in changes:
                if c.changed:
                    txn.change(c)
        except OSError as e:
            raise txn.fail(e) from None
    from .consent import claim_store, store_refusal
    served = Path(store).resolve() if store else project / STORE_DIR
    if adopt_store and not dry_run:
        try:
            ensure_store(served)
            claim_store(served, project, adopt=True)
        except (OSError, ValueError) as e:
            raise ConnectError(f"connected, but the store could not be adopted ({e})") from None
    why = store_refusal(project, served)
    if why:
        notes.append(f"The devmemory MCP tools will refuse to work until this is fixed: {why}.")
    return _report(tool, changes, printed, notes, dry_run)


def _delete_quietly(paths: list[tuple[Path, Path]]) -> None:
    """Delete devmem's own leftover files: each (file, the folder devmem made it in), checked by _deletable."""
    for p, folder in paths:
        if not _deletable(p, folder):
            continue
        try:
            os.remove(p)
        except OSError:
            pass


def _foreign_names(state: dict) -> int:
    """How many file or folder names a connect.json lists that are not devmem's own (see _own_backup, _own_dirs)."""
    n = 0
    for key, rec in (state.get("written") or {}).items():
        if not isinstance(rec, dict):
            continue
        olds = rec.get("old_backups")
        names = [rec.get("backup"), *(olds if isinstance(olds, list) else [olds])]
        n += sum(1 for x in names if x not in (None, "") and not _own_backup(str(key), x))
    for key, rec in (state.get("created") or {}).items():
        dirs = rec.get("dirs") if isinstance(rec, dict) else None
        if isinstance(dirs, list):
            n += len([d for d in dirs if d not in _own_dirs(str(key), dirs)])
    return n


FOREIGN_NOTE = ("{n} file or folder name(s) in {state} are not devmem's own backups or folders (that file is inside "
                "the project, so a repository can ship one); they were ignored and nothing they name was deleted.")


def _uses_instructions(project: Path, state: dict, key: str, but: str) -> list[str]:
    """Other connected tools whose instruction file is (or links to) the file `key`."""
    out = []
    for k, t in TOOLS.items():
        if k == but or not t.instructions:
            continue
        try:
            _p, tkey = _target(project, t.instructions)
        except ConnectError:
            continue
        if os.path.normcase(tkey) != os.path.normcase(key):
            continue
        if k in state["tools"] or mcp_entry(project, t) is not None:
            out.append(k)
    return out


def disconnect(project, tool_name: str, dry_run: bool = False, force: bool = False) -> dict:
    """Remove what `connect` added for `tool_name`. If a write fails, everything this call changed is put back and
    ConnectError is raised. dry_run=True writes nothing. In a store that is not this user's (devmem.consent.store_refusal:
    shipped, or git cannot say what it tracks) nothing is read or written: devmem's entries are removed from the tools'
    files by what they hold, with no bookkeeping (connect.json, backups) in or from the store."""
    tool = find_tool(tool_name)
    project = _check_project(project)
    own = project / STORE_DIR
    refused = None
    if own.is_dir():
        from .consent import store_refusal
        refused = store_refusal(project, own)
    state = read_state(project) if refused is None else {"tools": {}, "created": {}, "written": {}}
    connected = tool.key in state["tools"]
    changes: list[Change] = []
    notes: list[str] = []

    def rec_of(rel: str) -> tuple[dict, bool]:
        _p, key = _target(project, rel)
        return state["written"].get(key) or {}, key in state["created"]

    mcp_file = tool.mcp_file
    if mcp_file:
        rec, created = rec_of(mcp_file)
        c = (_plan_json_remove(project, tool, mcp_file, rec, created) if tool.mcp_format == "json"
             else _plan_block_remove(project, mcp_file, "toml", rec, created, force, tool.key, refused is None))
        if c:
            changes.append(c)
    else:
        notes.append(f"Remove the \"{SERVER_NAME}\" entry from "
                     f"{desktop_config_path() if tool.key == 'claude-desktop' else tool.global_hint} yourself; devmem "
                     "never edits a global config file.")
    if tool.instructions:
        _p, ikey = _target(project, tool.instructions)
        others = _uses_instructions(project, state, ikey, tool.key)
        if others:
            notes.append(f"The devmemory block in {tool.instructions} is kept: {', '.join(others)} still use(s) it.")
        else:
            rec, created = rec_of(tool.instructions)
            c = _plan_block_remove(project, tool.instructions, "md", rec, created, force, tool.key, refused is None)
            if c:
                changes.append(c)
    notes += _link_notes(changes)

    foreign = _foreign_names(state)
    if foreign:
        notes.append(FOREIGN_NOTE.format(n=foreign, state=_state_path(project)))
    new_state = copy.deepcopy(state)
    backup_dir = project / STORE_DIR / BACKUP_DIR
    leftovers: list[tuple[Path, Path]] = []   # devmem's backup copies of files that no longer hold anything of devmem's
    for c in changes:
        if c.changed:
            if c.new is None:
                made = state["created"].get(c.key)
                c.rmdirs = _own_dirs(c.key, made.get("dirs") if isinstance(made, dict) else None)
            # deleted, or left in place holding other content: either way not "created by devmem" any more
            new_state["created"].pop(c.key, None)
        if c.cleanup and (connected or c.changed):
            rec = new_state["written"].pop(c.key, None)
            rec = rec if isinstance(rec, dict) else {}
            olds = rec.get("old_backups")
            olds = olds if isinstance(olds, list) else []
            if any(_own_backup(c.key, n) for n in [rec.get("backup"), *olds]):
                leftovers.append((backup_dir / _backup_name(c.key), backup_dir))
            for legacy in {project.joinpath(*c.rel.split("/")), c.path}:
                lp = legacy.with_name(legacy.name + LEGACY_BACKUP_SUFFIX)
                leftovers.append((lp, legacy.parent))
    new_state["tools"].pop(tool.key, None)
    if refused is not None:
        if any(c.forced_copy is not None for c in changes):
            raise ConnectError(f"--force keeps a copy of the file in the memory store {own}, which is not yours "
                               "(`devmem status` says why): nothing was changed. Edit the file by hand, or adopt the "
                               "store first if it is yours")
        notes.append(f"The memory store {own} is not yours (`devmem status` says why), so nothing in it was read or "
                     "changed: devmem's entries were found in the tool's files by what they hold.")
    if not dry_run:
        txn = _Txn(project, store_ok=refused is None)
        try:
            for c in changes:
                if c.forced_copy is not None:
                    txn.keep_copy(c)
            for c in changes:
                if c.changed:
                    txn.change(c)
            # after the files: a disconnect interrupted here still finds connect's record and can be run again
            if refused is None and (_state_path(project).exists() or new_state["tools"] or new_state["created"]
                                    or new_state["written"]):
                txn.state(new_state)
        except OSError as e:
            raise txn.fail(e) from None
        if refused is None:
            _delete_quietly(leftovers)
            _rmdirs(project, [f"{STORE_DIR}/{BACKUP_DIR}"])
    return _report(tool, changes, [], notes, dry_run)


# ---------------------------------------------------------------- status (devmem doctor)

def mcp_entry(project: Path, tool: Tool) -> dict | None:
    """The devmemory entry in the tool's project MCP file, or None (read-only; malformed files count as none)."""
    mcp_file = tool.mcp_file
    if not mcp_file:
        return None
    try:
        path = _target(project, mcp_file)[0]      # a link is followed only inside the project
    except (ConnectError, OSError, RuntimeError):
        return None
    raw = _read(path, strict=False)                # a plain file only: a FIFO there never blocks `devmem doctor`
    try:
        text = raw.decode("utf-8") if raw is not None else None
    except UnicodeDecodeError:
        return None
    if text is None:
        return None
    if tool.mcp_format == "json":
        try:
            data = json.loads(text.lstrip("﻿"))
        except ValueError:
            return None
        servers = data.get(tool.mcp_key) if isinstance(data, dict) else None
        e = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
        return e if isinstance(e, dict) else None
    lines = text.splitlines()
    try:
        found = _find_block(path, lines, "toml")
    except ConnectError:
        return None
    if not found:
        return None
    entry: dict = {}
    for ln in lines[found[0]:found[1]]:
        m = re.match(r'^\s*(command|PYTHONPATH)\s*=\s*(".*")\s*$', ln)
        if m:
            try:
                val = json.loads(m.group(2))
            except ValueError:
                continue
            if m.group(1) == "command":
                entry["command"] = val
            else:
                entry.setdefault("env", {})["PYTHONPATH"] = val
    return entry or None


def command_problem(entry: dict, which=find_program) -> str | None:
    """None when the entry's command can be started, else why not."""
    cmd = str(entry.get("command") or "")
    if not cmd:
        return "the entry has no command"
    if os.path.isabs(cmd) or os.sep in cmd or "/" in cmd:
        if not os.path.exists(cmd):
            return f"{cmd} does not exist"
    elif not which(cmd):
        return f"`{cmd}` is not on PATH"
    pp = (entry.get("env") or {}).get("PYTHONPATH")
    if pp and not any((Path(p) / "devmem" / "__init__.py").is_file() for p in str(pp).split(os.pathsep) if p):
        return f"PYTHONPATH {pp} no longer holds the devmem package"
    return None


def connected_tools(project, which=find_program) -> list[dict]:
    """[{tool, label, file, command, problem}] for each tool with a devmemory entry in its project MCP file, plus
    tools recorded in the state file that have no project MCP file (claude-desktop, generic)."""
    project = Path(project).resolve()
    state = read_state(project)
    out = []
    for key, t in TOOLS.items():
        e = mcp_entry(project, t)
        mcp_file = t.mcp_file
        if e is not None and mcp_file:
            out.append({"tool": key, "label": t.label, "file": str(project.joinpath(*mcp_file.split("/"))),
                        "command": e.get("command"), "problem": command_problem(e, which)})
        elif not mcp_file and key in state["tools"]:
            out.append({"tool": key, "label": t.label, "file": t.instructions, "command": None, "problem": None})
    return out


def describe_tools() -> list[str]:
    rows = []
    for t in TOOLS.values():
        writes = []
        if t.mcp_file:
            writes.append(f"{t.mcp_file} (MCP entry)")
        if t.instructions:
            writes.append(f"{t.instructions} (instruction block)")
        prints = " ; prints the MCP entry to paste into " + (desktop_config_path() if t.key == "claude-desktop" else t.global_hint) \
            if not t.mcp_file else ""
        alias = f" (also: {', '.join(t.aliases)})" if t.aliases else ""
        rows.append(f"  {t.key:<15}{t.label}{alias}: writes {', '.join(writes) or 'nothing'}{prints}")
    return rows
