"""FG-2: connect and disconnect touch a devmemory block only when it is provably devmem's: its bytes hash to the
`block_sha256` connect recorded in .devmemory/connect.json (never in the edited file), the whole file is exactly what
an earlier connect wrote (`sha256`, a version before block hashes), or the block is byte for byte what this version
writes for the same inputs. Anything else (markers in a code fence or a TOML string, an edited block, a never-connected
project) is refused with nothing changed; --force keeps a copy of the file first."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from cli_helpers import make_project, run_cli

from devmem import connect as C
from devmem.connect import ConnectError, connect, disconnect


@pytest.fixture(autouse=True)
def _bare_devmem_command(monkeypatch):
    monkeypatch.setattr(C, "ABSOLUTE_DEVMEM", False)


def do_connect(project, tool, **kw):
    kw.setdefault("command", "devmem")
    kw.setdefault("which", lambda n: f"/usr/bin/{n}")
    return connect(project, tool, **kw)


@pytest.fixture()
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    (p / "a.py").write_text("x = 1\n", encoding="utf-8")
    return p


def files(project: Path) -> dict[str, bytes]:
    return {p.relative_to(project).as_posix(): p.read_bytes() for p in sorted(project.rglob("*"))
            if p.is_file() and ".devmemory" not in p.relative_to(project).parts}


MD_BEGIN, _P, MD_END = C.MARKERS["md"]


def inside(text: str, line: str) -> str:
    """`text` with `line` added inside its devmemory block (right after the begin marker line)."""
    i = text.index("\n", text.index(MD_BEGIN)) + 1
    return text[:i] + line + "\n" + text[i:]


FENCED = ("# Project notes\n\nHow devmem marks its block:\n\n```html\n<!-- devmemory:begin -->\n"
          "IMPORTANT USER PARAGRAPH\n<!-- devmemory:end -->\n```\n\nMore notes.\n")
TOML_STRING = '[tool]\nnote = """\n# devmemory:begin\nUSER TOML VALUE\n# devmemory:end\n"""\nother = 1\n'


@pytest.mark.parametrize("tool", ["cursor", "codex", "gemini"])
def test_disconnect_on_a_never_connected_project_removes_nothing(project, tool):
    (project / "AGENTS.md").write_text(FENCED, encoding="utf-8")
    (project / "GEMINI.md").write_text(FENCED, encoding="utf-8")
    (project / ".codex").mkdir()
    (project / ".codex" / "config.toml").write_text(TOML_STRING, encoding="utf-8")
    before = files(project)
    with pytest.raises(ConnectError, match="did not write"):
        disconnect(project, tool)
    assert files(project) == before


def test_disconnect_cli_exits_non_zero_and_changes_nothing(project):
    (project / "AGENTS.md").write_text(FENCED, encoding="utf-8")
    before = files(project)
    r = run_cli(["disconnect", "cursor", "--project", str(project)], cwd=project)
    assert r.returncode == 2 and b"--force" in r.stderr
    assert files(project) == before


@pytest.mark.parametrize("tool", ["cursor", "codex"])
def test_connect_does_not_replace_marker_text_it_did_not_write(project, tool):
    (project / "AGENTS.md").write_text(FENCED, encoding="utf-8")
    (project / ".codex").mkdir()
    (project / ".codex" / "config.toml").write_text(TOML_STRING, encoding="utf-8")
    before = files(project)
    with pytest.raises(ConnectError, match="did not write"):
        do_connect(project, tool)
    assert files(project) == before


def test_an_edited_block_is_refused_and_force_keeps_a_copy(project):
    do_connect(project, "cursor")
    agents = project / "AGENTS.md"
    edited = inside(agents.read_text(encoding="utf-8"), "my own note: KEEP")
    agents.write_text(edited, encoding="utf-8")
    raw = agents.read_bytes()
    with pytest.raises(ConnectError, match="did not write"):
        disconnect(project, "cursor")
    assert agents.read_bytes() == raw
    with pytest.raises(ConnectError, match="did not write"):
        do_connect(project, "cursor")
    assert agents.read_bytes() == raw
    r = disconnect(project, "cursor", force=True)
    copy = Path(next(c["backup"] for c in r["changes"] if c["rel"] == "AGENTS.md"))
    assert copy.read_bytes() == raw and copy.parent == project / ".devmemory" / "connect-backups"
    assert "devmemory:begin" not in (agents.read_text(encoding="utf-8") if agents.exists() else "")
    assert copy.exists()                     # kept after the run


def test_edits_outside_the_block_do_not_stop_disconnect(project):
    (project / "AGENTS.md").write_text("# Mine\n", encoding="utf-8")
    do_connect(project, "codex")
    agents = project / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8") + "\nAdded later.\n", encoding="utf-8")
    cfg = project / ".codex" / "config.toml"
    cfg.write_text("model = \"x\"\n" + cfg.read_text(encoding="utf-8"), encoding="utf-8")
    disconnect(project, "codex")
    assert agents.read_text(encoding="utf-8") == "# Mine\n\nAdded later.\n"
    assert cfg.read_text(encoding="utf-8").strip() == 'model = "x"'


def test_connect_records_the_block_hash_in_its_own_state(project):
    do_connect(project, "codex")
    state = json.loads((project / ".devmemory" / "connect.json").read_text(encoding="utf-8"))
    for key in ("AGENTS.md", ".codex/config.toml"):
        rec = state["written"][key]
        text = (project / key).read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        style = "toml" if key.endswith(".toml") else "md"
        found = C._find_block(project / key, lines, style)
        assert rec["block_sha256"] == C.block_sha(lines, found)
        assert rec["block_sha256"] not in text             # nothing added to the edited file


# ---------------------------------------------------------------- upgrade from a version without block hashes

def _drop_block_hashes(project: Path) -> None:
    sp = project / ".devmemory" / "connect.json"
    state = json.loads(sp.read_text(encoding="utf-8"))
    for rec in state["written"].values():
        rec.pop("block_sha256", None)
    sp.write_text(json.dumps(state), encoding="utf-8")


def test_upgrade_file_untouched_since_connect_is_accepted(project):
    do_connect(project, "codex")
    _drop_block_hashes(project)
    agents = project / "AGENTS.md"
    old_text = inside(agents.read_text(encoding="utf-8"), "text of an older version")
    agents.write_text(old_text, encoding="utf-8")
    # what an older connect recorded: the whole file's hash as it wrote it
    sp = project / ".devmemory" / "connect.json"
    state = json.loads(sp.read_text(encoding="utf-8"))
    state["written"]["AGENTS.md"]["sha256"] = C._sha(agents.read_bytes())
    sp.write_text(json.dumps(state), encoding="utf-8")
    disconnect(project, "codex")
    assert not agents.exists()


def test_upgrade_edited_file_with_the_current_block_is_accepted(project):
    (project / "AGENTS.md").write_text("# Mine\n", encoding="utf-8")
    do_connect(project, "codex")
    _drop_block_hashes(project)
    agents = project / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8") + "\nmore\n", encoding="utf-8")
    disconnect(project, "codex")
    assert agents.read_text(encoding="utf-8") == "# Mine\n\nmore\n"


def test_upgrade_edited_file_with_an_older_block_needs_force(project):
    (project / "AGENTS.md").write_text("# Mine\n", encoding="utf-8")
    do_connect(project, "cursor")
    _drop_block_hashes(project)
    agents = project / "AGENTS.md"
    agents.write_text(inside(agents.read_text(encoding="utf-8"), "devmem v0.2 wording") + "\nmore\n",
                      encoding="utf-8")
    raw = agents.read_bytes()
    with pytest.raises(ConnectError, match="--force"):
        disconnect(project, "cursor")
    assert agents.read_bytes() == raw
    disconnect(project, "cursor", force=True)
    assert agents.read_text(encoding="utf-8") == "# Mine\n\nmore\n"


def test_never_connected_block_identical_to_this_version_is_devmems(project):
    body = "".join(C._block_lines(C.INSTRUCTIONS, "md", "\n"))
    (project / "AGENTS.md").write_text("# Mine\n\n" + body, encoding="utf-8")
    disconnect(project, "cursor")
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == "# Mine\n"


@pytest.mark.parametrize("entry", [
    {"command": "devmem", "args": ["mcp", "--project", "/p"]},
    {"command": "C:\\Tools\\dev mem.exe", "args": [], "env": {"PYTHONPATH": "a\"b\\c", "X_1": "\x7f\t\u00e9"}},
])
def test_toml_body_reads_back(entry):
    body = C.toml_block(entry)
    assert C._toml_body_entry(body) == entry
    assert C._toml_body_entry(body.replace("args", "argz")) is None
    assert C._toml_body_entry(body + "extra = 1\n") is None


def test_force_on_connect_keeps_a_copy(project):
    (project / "AGENTS.md").write_text(FENCED, encoding="utf-8")
    raw = (project / "AGENTS.md").read_bytes()
    r = do_connect(project, "cursor", force=True)
    copy = Path(next(c["backup"] for c in r["changes"] if c["rel"] == "AGENTS.md"))
    assert copy.read_bytes() == raw
    assert "IMPORTANT USER PARAGRAPH" not in (project / "AGENTS.md").read_text(encoding="utf-8")


def test_cli_force_flag(project):
    (project / "AGENTS.md").write_text(FENCED, encoding="utf-8")
    r = run_cli(["disconnect", "cursor", "--project", str(project), "--force"], cwd=project)
    assert r.returncode == 0, r.stderr
    assert b"copied the previous file to" in r.stdout


def test_make_project_helper_still_works(tmp_path):
    assert make_project(tmp_path).is_dir()


def test_in_a_store_that_is_not_yours_disconnect_does_not_recommend_force(project):
    """--force keeps its copy in the store, which a refused store must not get: the first message must not send the
    user to a flag that then refuses too."""
    from devmem import consent
    do_connect(project, "cursor")
    agents = project / "AGENTS.md"
    agents.write_text(inside(agents.read_text(encoding="utf-8"), "my own note: KEEP"), encoding="utf-8")
    consent.store_note(project / ".devmemory").unlink()          # the store is no longer known to be this user's
    consent.forget_decisions()
    with pytest.raises(ConnectError, match="did not write") as e:
        disconnect(project, "cursor")
    assert "--force" not in str(e.value) and "not yours" in str(e.value), e.value
