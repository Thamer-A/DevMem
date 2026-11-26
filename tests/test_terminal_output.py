"""Nothing devmem prints to a terminal carries a raw control sequence (devmem.term): ESC/CSI/OSC (colours, the window
title, OSC 52 clipboard writes), C1 controls, bidi controls and invisible format characters from a project, a
recorded session or a model's tool arguments are shown escaped. Every message goes through devmem.term; this file
also fails when a bare print() or sys.stdout/stderr.write() appears elsewhere in the package."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from cli_helpers import make_project, write_session

from devmem import cli, term

PKG = Path(cli.__file__).resolve().parent
HOSTILE = "a\x1b]52;c;ZXZpbA==\x07b\x1b]0;title\x07c\x1b[31md\x9b2Je‮f⁦g️h\x00i\x7fj\rk"
RAW = ("\x1b", "\x07", "\x9b", "‮", "⁦", "️", "\x00", "\x7f", "\r")

# Output that is not a message for a person, with its own escaping: JSON with ensure_ascii (every control character
# escaped), a context pack (devmem.pack escapes what it quotes), the hook and MCP protocol streams, and the probe
# command line connect runs in a child interpreter.
ALLOWED = {
    ("term.py", None),
    ("hooks.py", "sys.stdout.write(data.decode(\"utf-8\"))"),     # the hook's JSON/pack output for Claude Code
    ("mcp_server.py", "stdout.write(_encode(out))"),              # JSON-RPC to the client
    ("refresh.py", "print(json.dumps("),                          # the refresh child's JSON line to its parent
    ("connect.py", "sys.stdout.write(devmem.__file__)"),          # text of a -c program run in another interpreter
    ("redact.py", "print(i, round("),                             # same: the pattern probe's numbers to its parent
}
BARE = re.compile(r"(?<![\w.])print\(|sys\.std(?:out|err)\.write\(|(?<![\w.])stdout\.write\(|stderr\.write\("
                  r"|\.print_(?:exc|exception|tb|stack)\(")   # traceback's printers write to stderr unescaped


def _allowed(name: str, line: str) -> bool:
    return any(n == name and (frag is None or frag in line) for n, frag in ALLOWED)


def test_no_bare_terminal_output_outside_devmem_term():
    found = []
    for f in sorted(PKG.glob("*.py")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if BARE.search(code) and not code.lstrip().startswith(('"', "'")) and not _allowed(f.name, line):
                found.append(f"{f.name}:{i}: {line.strip()}")
    assert found == [], "print through devmem.term.say / warn instead:\n" + "\n".join(found)


def test_clean_escapes_every_terminal_control_and_keeps_layout():
    out = term.clean(HOSTILE + "\nnext\tcol")
    for c in RAW:
        assert c not in out, repr(c)
    assert "\\x1b" in out and out.endswith("\nnext\tcol")
    assert term.clean("plain ascii, ü and العربية") == "plain ascii, ü and العربية"


def test_say_and_warn_escape(capsys):
    term.say("x", HOSTILE)
    term.warn(HOSTILE)
    out = capsys.readouterr()
    for c in RAW:
        assert c not in out.out and c not in out.err


@pytest.fixture()
def hostile_store(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    meta = write_session(store, "abcdef12-0000-0000-0000-000000000000", f"wallet {HOSTILE}")
    rec = json.loads(meta.read_text(encoding="utf-8"))
    rec.update(provider=HOSTILE, started_at=HOSTILE, session_id="abcdef12" + HOSTILE)
    meta.write_text(json.dumps(rec), encoding="utf-8")
    return project


@pytest.mark.parametrize("argv", [["sessions"], ["status"], ["show", "abcdef12"], ["doctor"],
                                  ["forget", "--all", "--yes"]])
def test_commands_print_recorded_fields_escaped(hostile_store, monkeypatch, capsys, argv):
    monkeypatch.chdir(hostile_store)
    cli.main(argv)
    out = capsys.readouterr()
    for c in RAW:
        assert c not in out.out and c not in out.err, (argv, repr(c))


def test_debug_tracebacks_are_escaped(monkeypatch, capsys, tmp_path):
    """DEVMEM_DEBUG=1 adds the traceback, which quotes the exception's message (it can hold a project's text)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEVMEM_DEBUG", "1")

    def boom(a):
        raise RuntimeError(f"from a store: {HOSTILE}")
    monkeypatch.setattr(cli, "cmd_sessions", boom)
    assert cli.main(["sessions"]) != 0
    err = capsys.readouterr().err
    assert "Traceback" in err and "from a store:" in err
    for c in RAW:
        assert c not in err


def test_error_messages_are_escaped(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)

    def boom(a):
        raise cli.CliError(f"tracked: {HOSTILE}")
    monkeypatch.setattr(cli, "cmd_sessions", boom)
    assert cli.main(["sessions"]) == 2
    err = capsys.readouterr().err
    assert "tracked:" in err
    for c in RAW:
        assert c not in err


def test_mcp_end_session_refuses_a_client_name_with_controls(tmp_path):
    from devmem.mcp_server import Server
    project = make_project(tmp_path)
    srv = Server(project)
    srv.call("begin_session", {})
    with pytest.raises(ValueError, match="client"):
        srv.call("end_session", {"prompt": "x", "client": "cursor\x1b]52;c;ZXZpbA==\x07"})
    srv.call("end_session", {"prompt": "x", "client": "cursor"})


def test_records_are_written_with_plain_provider_and_started_at(tmp_path):
    from devmem import sessions as S
    store = tmp_path / ".devmemory"
    meta = S.write_record(store, {"session_id": "abc12345", "provider": "evil\x1b[2J", "started_at": "x\x1b]0;t\x07",
                                  "turns": []})
    rec = json.loads(meta.read_text(encoding="utf-8"))
    assert "\x1b" not in rec["provider"] and "\x1b" not in rec["started_at"]
