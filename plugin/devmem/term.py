"""The one way devmem writes a message to a terminal (stdout or stderr: the CLI, the app, the viewer's console lines,
hook and MCP messages on stderr). A message can carry text from a project or a store a repository shipped (a file
name, a session id, a recorded `provider`), so every character a terminal would act on or hide is shown escaped:
C0 controls other than newline and tab (ESC starts CSI/OSC sequences: colours, the window title, OSC 52 clipboard
writes), DEL, C1 controls (U+009B is a one-character CSI), bidi controls, zero-width and other format characters,
variation selectors such as U+FE0F, private-use and unassigned code points (devmem.store.display_text decides which).

Output that is not a message for a person is written elsewhere and escaped by its own rules: JSON (`json.dumps` with
its default ensure_ascii escapes every control and non-ASCII character), a context pack (devmem.pack escapes what it
quotes) and the hooks' and the MCP server's protocol streams. `tests/test_terminal_output.py` fails when a bare
print() or sys.stdout/stderr.write() appears anywhere else in the package.
"""
from __future__ import annotations

import sys


def clean(text) -> str:
    """`text` as it may reach a terminal: newlines and tabs kept, everything else display_text escapes."""
    from .store import display_text
    s = str(text)
    if s.isascii() and s.isprintable():
        return s
    return "\n".join("\t".join(display_text(part) for part in line.split("\t")) for line in s.split("\n"))


def say(*parts, sep: str = " ", end: str = "\n", file=None, flush: bool = False) -> None:
    """print() for a message: each part through clean(). file: sys.stdout by default (looked up at call time, so a
    test's redirected stream is used)."""
    stream = sys.stdout if file is None else file
    if stream is None:          # pythonw.exe: no console at all
        return
    stream.write(clean(sep.join(str(p) for p in parts)) + end)
    if flush:
        stream.flush()


def warn(*parts, sep: str = " ") -> None:
    """A message on stderr (say with file=sys.stderr, flushed)."""
    say(*parts, sep=sep, file=sys.stderr, flush=True)


def raw(text: str, file=None) -> None:
    """Text that has its own escaping (a context pack, JSON), written as it is. Only for those."""
    stream = sys.stdout if file is None else file
    if stream is not None:
        stream.write(text)
