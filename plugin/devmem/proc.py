"""The one place devmem starts another program (subprocess.run, subprocess.Popen, a browser).

On Windows, CreateProcess starts a `.bat` or `.cmd` file through cmd.exe, and cmd.exe parses the whole command line
again: `&`, `|`, `<`, `>` and `^` run or redirect commands, `%VAR%` (and `!VAR!`) expand, `"` flips its quote state
so that list2cmdline's quoting no longer protects anything, `(` and `)` close blocks inside the batch file, and an
argument ends at a line break ("BatBadBut"). Python's argument quoting cannot make that safe. So when the program
that would really start is a batch file, or is cmd, PowerShell or pwsh itself, every argument must be free of those
characters, or the program is not started and `UnsafeCommand` (an OSError and a ValueError, one line) says why.

Which program starts is decided the way CreateProcess decides it: the extension after trailing dots and spaces are
dropped (`x.bat.` and `x.bat ` start cmd.exe too), case-insensitive; an NTFS stream name (`x.bat::$DATA`) is refused.
A bare program name is looked up with `store.find_program` (never in the project or the current folder).
An argument holding a NUL character (which no program can receive) is refused the same way, for every program.
"""
from __future__ import annotations

import ntpath
import os
import posixpath
import subprocess

__all__ = ["UnsafeCommand", "check", "run", "run_capped", "run_to_file", "Captured", "popen", "open_url",
           "runs_through_cmd", "code_arguments"]

# cmd.exe's own characters, and PowerShell's (the `-Command` text is code); controls, CR and LF end or split an argument
_CMD_CHARS = set('&|<>^%!"()')
_PS_CHARS = _CMD_CHARS | set("$`;{}'@")
_POSIX_SHELLS = ("sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "csh", "tcsh", "busybox")
_INTERPRETERS = {"python": ("-c",), "python3": ("-c",), "py": ("-c",), "pythonw": ("-c",),
                 "node": ("-e", "--eval", "-p", "--print"), "deno": ("eval",), "bun": ("-e", "--eval"),
                 "perl": ("-e", "-E"), "ruby": ("-e",), "php": ("-r",), "lua": ("-e",)}


class UnsafeCommand(OSError, ValueError):
    """The program would be started through cmd.exe (or a shell) with an argument that shell would read as code.
    An OSError (callers that run git or a browser treat it as "could not run") and a ValueError."""


def _clean_name(program: str, nt: bool | None = None) -> str:
    """The file name CreateProcess ends up with: trailing dots and spaces dropped, lower case. `nt` applies the
    Windows rule on any host (the tests check it on Linux); None means the host's own rule."""
    if not (os.name == "nt" if nt is None else nt):
        return posixpath.basename(str(program)).lower()
    return str(program).replace("/", "\\").rsplit("\\", 1)[-1].rstrip(". ").lower()


def _stem(program: str, nt: bool | None = None) -> str:
    name = _clean_name(program, nt)
    root, ext = (ntpath if (os.name == "nt" if nt is None else nt) else posixpath).splitext(name)
    return root if ext in (".exe", ".com", ".bat", ".cmd", ".ps1") else name


def runs_through_cmd(program: str, nt: bool | None = None) -> bool:
    """True when starting `program` on Windows runs cmd.exe: a .bat/.cmd file (after trailing dots and spaces), or
    cmd itself."""
    if not (os.name == "nt" if nt is None else nt):
        return False
    name = _clean_name(program, True)
    return ntpath.splitext(name)[1] in (".bat", ".cmd") or _stem(program, True) == "cmd"


def code_arguments(argv: list[str]) -> set[int] | None:
    """Indexes of the arguments `argv[0]` reads as code (a shell's `-c` text, `python -c`, `node -e`...), or None
    when the whole command line is code (cmd.exe, a batch file, PowerShell, wsl). `devmem wrap` never puts the prompt
    there: the pack quotes project files, and a shell would run them."""
    if not argv:
        return set()
    prog = str(argv[0])
    stem = _stem(prog)
    if runs_through_cmd(prog) or stem in ("powershell", "pwsh", "powershell_ise", "wsl"):
        return None
    out: set[int] = set()
    if stem in _POSIX_SHELLS:
        skip = False
        for i, a in enumerate(argv[1:], 1):
            if skip:
                skip = False
                continue
            if a in ("-o", "+o", "-O", "+O"):
                skip = True              # takes a value: `bash -o pipefail -c ...`
                continue
            if a == "--" or not a.startswith(("-", "+")):
                break
            if not a.startswith("--") and "c" in a[1:]:
                out.add(i + 1)
        return out
    flags = _INTERPRETERS.get(stem) or _INTERPRETERS.get(stem.rstrip("0123456789.")) or ()
    for i, a in enumerate(argv[1:], 1):
        if a in flags:
            out.add(i + 1)
        elif any(a.startswith(f) and len(a) > len(f) and not f.startswith("--") for f in flags):
            out.add(i)          # -c"code" written as one argument
        elif a == "--" or not a.startswith("-"):
            break               # the script and its own arguments
    return out


def _problem(argv: list[str], nt: bool | None = None) -> str | None:
    if not argv:
        return "no program to run"
    nt = os.name == "nt" if nt is None else nt
    prog = str(argv[0])
    for i, a in enumerate(argv):
        if chr(0) in str(a):      # no program can receive one (subprocess would raise a bare ValueError)
            return f"{'its path' if i == 0 else f'argument {i}'} contains a NUL character; nothing was run"
    if nt and ":" in prog.replace("/", "\\").rsplit("\\", 1)[-1]:
        return f"{prog!r} names an NTFS stream"
    stem = _stem(prog, nt)
    through_cmd = runs_through_cmd(prog, nt)
    powershell = nt and stem in ("powershell", "pwsh", "powershell_ise") or stem == "pwsh"
    if not (through_cmd or powershell):
        return None
    bad = _PS_CHARS if powershell else _CMD_CHARS
    what = "PowerShell" if powershell else ("cmd.exe" if stem == "cmd" else "a batch file, which cmd.exe runs")
    for i, a in enumerate(argv):
        a = str(a)
        for ch in a:
            if ch in bad or ord(ch) < 32 or ord(ch) == 127:
                where = "its path" if i == 0 else f"argument {i}"
                return (f"{os.path.basename(prog)} is {what}, and {where} contains {ch!r}, which it would read as "
                        "a command character; nothing was run")
    return None


def check(argv: list[str], project=None) -> list[str]:
    """The argv to start: a bare program name resolved on PATH (never from `project` or the current folder), and
    refused (UnsafeCommand) when cmd.exe or PowerShell would re-read an argument as code."""
    argv = [str(a) for a in argv]
    if argv and os.path.basename(argv[0]) == argv[0] and not os.path.isabs(argv[0]):
        from .store import find_program
        exe = find_program(argv[0], project)
        if not exe:
            raise FileNotFoundError(f"{argv[0]} was not found on PATH")
        argv[0] = exe
    p = _problem(argv)
    if p:
        raise UnsafeCommand(p)
    return argv


def run(argv, *, project=None, **kw) -> subprocess.CompletedProcess:
    """subprocess.run through `check`. Never a shell."""
    if kw.get("shell"):
        raise UnsafeCommand("devmem never runs a command through a shell")
    return subprocess.run(check(argv, project), **kw)


# What run_capped holds of one output stream of a program devmem runs and records (`devmem wrap`, `devmem codex`, the
# stderr of `devmem run`): the first CHILD_OUTPUT_HEAD and the last CHILD_OUTPUT_TAIL characters; the middle is read
# and counted, not kept. An ordinary run prints far less and is held whole, exactly as subprocess.run held it.
CHILD_OUTPUT_HEAD = 8_000_000
CHILD_OUTPUT_TAIL = 8_000_000


class Captured:
    """What run_capped returns: `returncode`; `stdout` and `stderr` (head + tail, the whole text when nothing was
    dropped); `stdout_head` / `stdout_tail` (and stderr_*) separately, and `stdout_dropped` / `stderr_dropped`, the
    characters left out between them (0: nothing)."""

    def __init__(self, returncode: int, out: tuple[str, str, int], err: tuple[str, str, int]):
        self.returncode = returncode
        self.stdout_head, self.stdout_tail, self.stdout_dropped = out
        self.stderr_head, self.stderr_tail, self.stderr_dropped = err
        self.stdout = self.stdout_head + self.stdout_tail
        self.stderr = self.stderr_head + self.stderr_tail
        # run_to_file: the file could not be written; the program was killed at its timeout; a process it started
        # still held its output when it ended, and was not waited for
        self.write_failed = self.timed_out = self.output_not_waited = self.interrupted = False


class _CharSink:
    """What run_capped keeps of one output stream: its first `head` and last `tail` characters, decoded as
    subprocess.run(text=True, encoding="utf-8", errors="replace") decodes them (universal newlines). The reading thread
    and the caller share it under `lock`, so that what was read is there when the caller stops waiting."""

    def __init__(self, head: int, tail: int):
        import codecs
        import io
        import threading
        from collections import deque
        self.lock = threading.Lock()
        self.head, self.tail = head, tail
        self.first: list[str] = []
        self.last: deque[str] = deque()
        self.kept = self.held = self.total = 0
        self.done = False
        self.dec = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")(errors="replace"), True)

    def add(self, b: bytes, final: bool = False) -> None:
        s = self.dec.decode(b, final)
        self.total += len(s)
        if self.kept < self.head:
            part = s[:self.head - self.kept]
            self.first.append(part)
            self.kept += len(part)
            s = s[len(part):]
        if s:
            self.last.append(s)
            self.held += len(s)
            while self.last and self.held - len(self.last[0]) >= self.tail:
                self.held -= len(self.last.popleft())

    def text(self) -> tuple[str, str, int]:
        end = "".join(self.last)
        end = end[len(end) - self.tail:] if self.tail and len(end) > self.tail else (end if self.tail else "")
        return "".join(self.first), end, self.total - self.kept - len(end)


def _read_into(stream, sink, finish=None) -> None:
    """Read a binary pipe to its end into `sink` (under its lock); `finish(sink)` at the end, unless the caller has
    stopped waiting (sink.done) and finished it itself."""
    import io
    read = stream.read1 if isinstance(stream, io.BufferedReader) else stream.read
    while True:
        try:
            b = read(1 << 20)
        except (OSError, ValueError):
            b = b""
        with sink.lock:
            if getattr(sink, "done", False):
                return
            if not b:
                if finish is not None:
                    finish(sink)
                elif isinstance(sink, _CharSink):
                    sink.add(b"", True)
                    sink.done = True
                return
            sink.add(b)


def _own_group(kw: dict) -> None:
    """POSIX: the program starts in a session of its own (its process group), so that a timeout or Ctrl-C reaches
    what it started too (_kill_tree, _interrupt). It then has no controlling terminal: both callers give it pipes for
    its output (and `run` /dev/null for its input)."""
    if os.name != "nt":
        kw["start_new_session"] = True


def _wait_threads(threads: list, grace: float) -> bool:
    """Join the reading threads within ONE shared deadline of `grace` seconds; True when all of them ended."""
    import time
    deadline = time.monotonic() + grace
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return not any(t.is_alive() for t in threads)


def run_capped(argv, *, project=None, input: str | None = None, timeout: float | None = None,
               head: int | None = None, tail: int | None = None, grace: float | None = None, **kw) -> Captured:
    """subprocess.run(argv, input=input, capture_output=True, text=True, encoding="utf-8", errors="replace") through
    `check`, holding at most `head` + `tail` characters (CHILD_OUTPUT_HEAD / CHILD_OUTPUT_TAIL) of each output stream
    in memory (Captured). Same decoding, newline translation and stdin writing as subprocess.run. As run_to_file: when
    the program ends, its output is waited for `grace` seconds (EXIT_GRACE_S) more, one deadline for both streams, not
    for a process it started that keeps them open (Captured.output_not_waited); at `timeout` the program and its process
    tree are killed (_kill_tree) and TimeoutExpired is raised after that grace at most; Ctrl-C: _interrupt."""
    import threading
    if kw.get("shell"):
        raise UnsafeCommand("devmem never runs a command through a shell")
    head = CHILD_OUTPUT_HEAD if head is None else head
    tail = CHILD_OUTPUT_TAIL if tail is None else tail
    grace = EXIT_GRACE_S if grace is None else grace
    for k in ("stdout", "stderr", "text", "universal_newlines", "encoding", "errors", "bufsize", "start_new_session"):
        kw.pop(k, None)
    if input is not None:
        kw["stdin"] = subprocess.PIPE
    _own_group(kw)
    p = subprocess.Popen(check(argv, project), stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    sinks = {"stdout": _CharSink(head, tail), "stderr": _CharSink(head, tail)}
    threads = [threading.Thread(target=_read_into, args=(getattr(p, k), sinks[k]), daemon=True) for k in sinks]

    def feed() -> None:
        try:
            if p.stdin is not None and input is not None:
                # what a text-mode stdin writes: "\n" as the system's line end, UTF-8, unencodable characters replaced
                p.stdin.write(input.replace("\n", os.linesep).encode("utf-8", errors="replace"))
        except (BrokenPipeError, OSError, ValueError):
            pass               # the program did not read it all: as subprocess.run, not an error
        finally:
            _close(p.stdin)
    if input is not None:
        threading.Thread(target=feed, daemon=True).start()
    timed_out = False
    try:
        for t in threads:
            t.start()
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(p)
            p.wait()
    except KeyboardInterrupt:
        _interrupt(p)
        raise
    except BaseException:
        _kill_tree(p)
        raise
    done = _wait_threads(threads, grace)
    out = {}
    for k, sink in sinks.items():
        with sink.lock:
            sink.done = True                     # a thread still reading stops adding
            out[k] = sink.text()
    if done:
        for s in (p.stdout, p.stderr):
            _close(s)                            # never while a thread reads it: closing would wait for that read
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout or 0, output=out["stdout"][0] + out["stdout"][1],
                                        stderr=out["stderr"][0] + out["stderr"][1])
    r = Captured(p.returncode, out["stdout"], out["stderr"])
    r.output_not_waited = not done
    return r


# What run_to_file writes of a program's standard output (the stream `devmem run` records): at most its first
# STREAM_FILE_HEAD and last STREAM_FILE_TAIL bytes in all, the same sizes devmem.capture reads of a stream file, so the
# file is read whole. When the middle is left out, only whole lines are kept on either side of it (half a line is no
# event: the secret-file rule could not see what it belongs to), and one line between them says how many bytes were
# left out; the tail gives up the room that line takes.
STREAM_FILE_HEAD = 64 * 1024 * 1024
STREAM_FILE_TAIL = 64 * 1024 * 1024


EXIT_GRACE_S = 2.0      # after the program (or its kill) ends, how long run_to_file waits for its output pipes to close


class _StreamSink:
    """What run_to_file keeps of standard output: the head written to the file as it comes, the tail held. The
    reading thread and the caller share it under `lock`; `finish` writes the end once, whoever calls it first."""

    def __init__(self, out_path, head: int, tail: int, gap_line):
        import threading
        from collections import deque
        self.lock = threading.Lock()
        self.head, self.tail, self.gap_line = head, tail, gap_line
        self.written = self.total = self.held = self.head_nl = 0
        self.last: deque[bytes] = deque()
        self.done = self.eof = False
        self.failed = False
        self.dropped = 0
        try:
            self.f = open(out_path, "wb")
        except OSError:
            self.f, self.failed = None, True

    def put(self, data) -> None:
        if self.f is not None:
            try:
                self.f.write(data)
            except (OSError, ValueError):
                _close(self.f)
                self.f, self.failed = None, True

    def add(self, b: bytes) -> None:
        self.total += len(b)
        if self.written < self.head:
            part = b[:self.head - self.written]
            self.put(part)
            i = part.rfind(b"\n")
            if i >= 0:
                self.head_nl = self.written + i + 1
            self.written += len(part)
            b = b[len(part):]
            if not b:
                return
        self.last.append(b)
        self.held += len(b)
        while self.last and self.held - len(self.last[0]) >= self.tail:
            self.held -= len(self.last.popleft())

    def finish(self, out_path) -> None:
        """Write the end of the file (call with `lock` held)."""
        if self.done:
            return
        self.done = True
        joined = b"".join(self.last)
        self.last.clear()
        if self.total - self.written <= self.tail:
            self.put(joined)                    # nothing left out: every byte as the program wrote it
        else:
            room = len(self.gap_line(self.total).encode("utf-8")) + 1     # the count left out is below total
            start = len(joined) - self.tail + min(room, self.tail)        # the tail, less the room of the gap line
            if joined[start - 1:start] != b"\n":                          # it would start inside a line: its next
                i = joined.find(b"\n", start)
                start = i + 1 if i >= 0 else len(joined)
            end = memoryview(joined)[start:]
            self.dropped = self.total - self.head_nl - len(end)
            if self.f is not None:
                try:
                    self.f.seek(self.head_nl)       # the head's last line, cut by `head`, goes too
                    self.f.truncate()
                except (OSError, ValueError):
                    self.failed = True
            self.put(self.gap_line(self.dropped).encode("utf-8") + b"\n")
            self.put(end)
        _close(self.f)
        self.f = None
        if self.failed:                          # a file written in part could end inside a line: keep none of it
            self.dropped = self.total
            try:
                with open(out_path, "wb"):
                    pass
            except OSError:
                pass


class _TextSink:
    """What run_to_file keeps of standard error: its first and last bytes (decoded when asked)."""

    def __init__(self, head: int, tail: int):
        import threading
        from collections import deque
        self.lock = threading.Lock()
        self.head, self.tail = head, tail
        self.first = bytearray()
        self.last: deque[bytes] = deque()
        self.held = self.total = 0

    def add(self, b: bytes) -> None:
        self.total += len(b)
        if len(self.first) < self.head:
            part = b[:self.head - len(self.first)]
            self.first += part
            b = b[len(part):]
        if b:
            self.last.append(b)
            self.held += len(b)
            while self.last and self.held - len(self.last[0]) >= self.tail:
                self.held -= len(self.last.popleft())

    def text(self) -> tuple[str, str, int]:
        end = b"".join(self.last)
        end = end[len(end) - self.tail:] if len(end) > self.tail else end
        return (bytes(self.first).decode("utf-8", errors="replace"), end.decode("utf-8", errors="replace"),
                self.total - len(self.first) - len(end))


def _kill_tree(p: subprocess.Popen) -> None:
    """Kill the program and what it started, where the system allows: its process group on POSIX (it was started
    in a session of its own), `taskkill /T` on Windows (the tree by parent id). A descendant that left the group, or
    whose parent died first, can survive; run_to_file does not wait for it."""
    try:
        if os.name == "nt":
            exe = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "taskkill.exe")
            subprocess.run([exe, "/T", "/F", "/PID", str(p.pid)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        else:
            import signal
            os.killpg(p.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    try:
        p.kill()
    except OSError:
        pass


INTERRUPT_GRACE_S = 3.0   # Ctrl-C: how long the program gets to end after SIGINT, and again after SIGTERM


def _interrupt(p: subprocess.Popen) -> None:
    """Ctrl-C while devmem waits for a program it started in its own process group (_own_group): on POSIX the
    terminal's SIGINT reached devmem only, so it is sent to the program's group; the program gets INTERRUPT_GRACE_S
    seconds to end, then SIGTERM and as long again, then its tree is killed (_kill_tree). On Windows the console
    already sent the program its Ctrl-C, and the KeyboardInterrupt reaches devmem only once the program has ended
    (Popen.wait cannot be interrupted there), so nothing is left to kill; a program still running would get the same
    time, then its tree is killed. A second Ctrl-C kills it at once."""
    import signal
    import time
    try:
        for sig in ((signal.SIGINT, signal.SIGTERM) if os.name != "nt" else (None,)):
            if sig is not None and os.name != "nt":
                try:
                    os.killpg(p.pid, sig)
                except OSError:
                    pass
            end = time.monotonic() + INTERRUPT_GRACE_S
            while p.poll() is None and time.monotonic() < end:
                time.sleep(0.05)
            if p.poll() is not None:
                break
    finally:
        if os.name != "nt" or p.poll() is None:
            _kill_tree(p)           # what is left of its group (POSIX) or of its tree
        try:
            p.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            pass


def run_to_file(argv, out_path, *, project=None, timeout: float | None = None, head: int | None = None,
                tail: int | None = None, gap_line=None, grace: float | None = None, **kw) -> Captured:
    """run_capped with standard output written to `out_path` instead of held: the program's bytes as it wrote them
    when they are at most `head` + `tail` (STREAM_FILE_HEAD + STREAM_FILE_TAIL). Past that, the middle is read and
    counted, not written: the file holds the whole lines within the first `head` bytes, the line
    `gap_line(bytes_left_out)`, and the whole lines within the last `tail` bytes less that line, at most `head` +
    `tail` bytes in all (for a tail longer than the gap line). Captured.stdout_dropped is the bytes left out; stderr
    keeps its first CHILD_OUTPUT_HEAD and last CHILD_OUTPUT_TAIL bytes.

    The program is read to its end even when the file cannot be written (Captured.write_failed: the file is then
    empty). When the program ends, its output is waited for `grace` seconds (EXIT_GRACE_S) more, no longer: a process
    it started that keeps the output open is not waited for (Captured.output_not_waited). At `timeout` the program and
    its process tree are killed (_kill_tree) and Captured.timed_out is set, with what was read until then."""
    import threading
    head = STREAM_FILE_HEAD if head is None else head
    tail = STREAM_FILE_TAIL if tail is None else tail
    grace = EXIT_GRACE_S if grace is None else grace
    gap_line = gap_line or (lambda n: f"[devmem: about {n:,} bytes of output left out here]")
    for k in ("stdout", "stderr", "text", "universal_newlines", "encoding", "errors", "bufsize", "start_new_session"):
        kw.pop(k, None)        # binary and buffered: read1 below
    if kw.get("shell"):
        raise UnsafeCommand("devmem never runs a command through a shell")
    _own_group(kw)
    p = subprocess.Popen(check(argv, project), stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    out = _StreamSink(out_path, head, tail, gap_line)
    err = _TextSink(CHILD_OUTPUT_HEAD, CHILD_OUTPUT_TAIL)

    def finish(sink) -> None:
        sink.eof = True
        sink.finish(out_path)

    threads = [threading.Thread(target=_read_into, args=(p.stdout, out, finish), daemon=True),
               threading.Thread(target=_read_into, args=(p.stderr, err), daemon=True)]
    timed_out = interrupted = False
    try:
        for t in threads:
            t.start()
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(p)
            p.wait()
    except KeyboardInterrupt:
        _interrupt(p)               # the program had its Ctrl-C; what it wrote until then is kept (a second one ends this)
        interrupted = True
    except BaseException:
        _kill_tree(p)
        raise
    try:
        _wait_threads(threads, grace)
    except KeyboardInterrupt:       # the program ended; Ctrl-C while a process it started holds its output
        if not interrupted:
            interrupted = True
            _interrupt(p)           # what is left of its group (POSIX); what was read until then is kept
        else:
            raise                   # a second Ctrl-C ends this
    with out.lock:
        not_waited = not out.done
        out.finish(out_path)                # a descendant still holds the pipe: what was read is what is kept
    with err.lock:
        not_waited = not_waited or threads[1].is_alive()
        err_text = err.text()
    if not any(t.is_alive() for t in threads):
        for s in (p.stdout, p.stderr):
            _close(s)                       # never while a thread reads it: closing would wait for that read
    r = Captured(-1 if timed_out else p.returncode, ("", "", out.dropped), err_text)
    r.write_failed, r.timed_out, r.output_not_waited = out.failed, timed_out, not_waited
    r.interrupted = interrupted
    return r


def _close(f) -> None:
    try:
        if f is not None:
            f.close()
    except (OSError, ValueError):
        pass


def popen(argv, *, project=None, **kw) -> subprocess.Popen:
    """subprocess.Popen through `check`. Never a shell."""
    if kw.get("shell"):
        raise UnsafeCommand("devmem never runs a command through a shell")
    return subprocess.Popen(check(argv, project), **kw)


def open_url(url: str) -> bool:
    """Open devmem's own viewer address in the default browser. Only http(s) to this computer: on Windows
    webbrowser.open hands the string to ShellExecute, which would start a program or open a file."""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url)
        host = u.hostname
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or host not in ("127.0.0.1", "localhost", "::1") or \
            any(ord(c) < 33 or c in '"<>^`{|}' for c in url):
        raise UnsafeCommand(f"devmem opens only its own local viewer address, not {url[:80]!r}")
    import webbrowser
    return webbrowser.open(url)
