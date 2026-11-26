"""Put text on the system clipboard (`devmem pack "<task>" --copy`), for chat interfaces that have no tools:
paste the pack above your request.

Uses the platform's own command, fed through stdin: Windows `clip` (it reads UTF-16LE with a byte-order mark
as Unicode; plain UTF-8 or the console code page would garble non-ASCII text), macOS `pbcopy` (UTF-8 with a
UTF-8 locale), and on Linux `wl-copy` (Wayland), `xclip` or `xsel`, whichever is present. The runner and the
lookup are injectable so tests never touch the real clipboard.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

from . import proc
from .store import find_program


class ClipboardError(Exception):
    """No clipboard command, or it failed. Nothing was copied."""


def clipboard_command(platform: str | None = None, which=find_program, environ=None) -> tuple[list[str], str]:
    """(argv, encoding) of the clipboard command for this platform. Raises ClipboardError when none exists."""
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    if platform == "win32":
        exe = which("clip")
        if exe:
            return [exe], "utf-16"
        raise ClipboardError("the Windows `clip` command was not found")
    if platform == "darwin":
        exe = which("pbcopy")
        if exe:
            return [exe], "utf-8"
        raise ClipboardError("`pbcopy` was not found")
    candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    if not environ.get("WAYLAND_DISPLAY"):
        candidates = candidates[1:] + candidates[:1]
    for argv in candidates:
        exe = which(argv[0])
        if exe:
            return [exe, *argv[1:]], "utf-8"
    raise ClipboardError("no clipboard command found: install wl-clipboard (Wayland), xclip or xsel, "
                         "or use `devmem pack \"<task>\" --out pack.md` and open the file")


_SURROGATE = re.compile("[\ud800-\udfff]")


def encode_for(text: str, encoding: str) -> bytes:
    text = _SURROGATE.sub(chr(0xFFFD), text)   # a lone surrogate (from undecodable bytes) cannot be encoded: U+FFFD
    if encoding == "utf-16":
        # Windows: CRLF line ends for pasting into ordinary editors; little-endian with a byte-order mark
        return b"\xff\xfe" + text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-16-le")
    return text.encode("utf-8")


def copy(text: str, runner=None, platform: str | None = None, which=find_program, environ=None) -> str:
    """Copy `text`; returns the command's name. Raises ClipboardError when it cannot.

    xclip, xsel and wl-copy fork a child that keeps serving the selection after the command exits. The child
    inherits the command's stdout and stderr, so those are not pipes (reading a pipe to its end would wait for
    the child): stdout goes to DEVNULL and stderr to a temporary file read once the command has exited."""
    runner = runner or proc.run
    argv, encoding = clipboard_command(platform, which, environ)
    env = dict(os.environ if environ is None else environ)
    if encoding == "utf-8":
        env.setdefault("LANG", "en_US.UTF-8")   # pbcopy decodes stdin by the locale
    name = os.path.basename(argv[0])
    with tempfile.TemporaryFile() as errf:
        try:
            r = runner(argv, input=encode_for(text, encoding), stdout=subprocess.DEVNULL, stderr=errf, timeout=15,
                       env=env)
        except (OSError, subprocess.SubprocessError) as e:
            raise ClipboardError(f"{name} failed: {e}") from None
        if getattr(r, "returncode", 0) != 0:
            err = getattr(r, "stderr", None)
            if not isinstance(err, bytes) or not err:
                errf.seek(0)
                err = errf.read(4000)
            msg = err.decode("utf-8", errors="replace").strip()
            raise ClipboardError(f"{name} exited with {r.returncode}" + (f": {msg[:200]}" if msg else ""))
    return name
