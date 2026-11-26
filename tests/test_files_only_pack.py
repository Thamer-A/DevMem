"""The files-only pack `wrap` and `codex` give an agent when the store is not this user's (devmem.pack._project_only_pack).
Nothing in the refused store is read (its ignore file once hid a project file from the pack); the map is kept in
devmem's per-user folder, so a second run does not rescan the project; the scan has a time limit, and the agent still
starts when it is cut short (with a note)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from cli_helpers import make_project, write_session

from devmem import cli, consent, pack

TASK = "fix the payment charge function"
AGENT = "import sys, shutil" + chr(10) + "shutil.copy(sys.argv[1], sys.argv[2])" + chr(10)


def shipped(tmp_path: Path) -> tuple[Path, Path]:
    """A project whose `.devmemory` came in an archive: an ignore file hiding payment.py, a config, a map."""
    project = make_project(tmp_path)
    (project / "payment.py").write_text("def charge(card):\n    return card\n", encoding="utf-8")
    store = project / ".devmemory"
    write_session(store, "abcdef12-0000-0000-0000-000000000000", "earlier request", claim=False)
    (store / "ignore").write_text("payment.py\n", encoding="utf-8")
    (store / "config.json").write_text(json.dumps({"pack_budget": 9000}), encoding="utf-8")
    (store / "index").mkdir()
    (store / "index" / "map.json").write_text(json.dumps({"files": {"SHIPPEDMAP.py": {"lang": "python"}}}),
                                              encoding="utf-8")
    return project, store


def _wrap(tmp_path: Path, project: Path, capsys, extra=()) -> tuple[str, str]:
    agent, got = tmp_path / "agent.py", tmp_path / "pack-seen.md"
    agent.write_text(AGENT, encoding="utf-8")
    got.unlink(missing_ok=True)
    code = cli.main(["wrap", "--project", str(project), *extra, "--prompt", TASK, "--",
                     sys.executable, str(agent), "{pack_file}", str(got)])
    err = capsys.readouterr().err
    assert code == 0, err[-400:]
    return got.read_text(encoding="utf-8"), err


def test_the_refused_stores_ignore_file_is_not_read(tmp_path, capsys):
    project, store = shipped(tmp_path)
    assert consent.store_refusal(project, store)
    seen, _err = _wrap(tmp_path, project, capsys)
    assert "payment.py" in seen and "SHIPPEDMAP" not in seen


def test_the_map_is_kept_outside_the_project_and_reused(tmp_path, capsys, monkeypatch):
    project, store = shipped(tmp_path)
    folder = pack.files_only_store(project)
    seen, _err = _wrap(tmp_path, project, capsys)
    assert "payment.py" in seen
    assert (folder / "index" / "index.db").is_file() and (folder / "index" / "map.json").is_file()
    assert not str(folder).startswith(str(project))
    calls = []
    from devmem import scan as scan_mod
    real = scan_mod.scan
    monkeypatch.setattr(scan_mod, "scan", lambda *a, **k: calls.append(a) or real(*a, **k))
    seen, _err = _wrap(tmp_path, project, capsys)
    assert "payment.py" in seen and calls == []          # nothing changed: the kept map is current, no scan
    cli.main(["adopt", "--project", str(project), "--yes"])
    assert not folder.exists()                            # the project's own store serves it now


def test_a_scan_cut_short_still_starts_the_agent_with_a_note(tmp_path, capsys, monkeypatch):
    project, _store = shipped(tmp_path)
    monkeypatch.setattr(pack, "FILES_ONLY_SCAN_S", 0.0)  # no time for the scan at all
    seen, err = _wrap(tmp_path, project, capsys)
    assert "the project scan for this pack did not run" in err
    assert isinstance(seen, str)                          # the agent ran: it copied the pack file it was given


def test_a_refused_store_inside_the_project_under_another_name_is_not_mapped(tmp_path, capsys):
    project = make_project(tmp_path)
    (project / "payment.py").write_text("def charge(card):\n    return card\n", encoding="utf-8")
    other = project / "mem"
    write_session(other, "abcdef12-0000-0000-0000-000000000000", "payment charge SHIPPEDREQUEST", claim=False)
    seen, _err = _wrap(tmp_path, project, capsys, extra=["--store", str(other)])
    assert "payment.py" in seen and "SHIPPED" not in seen and "mem/" not in seen
    mapped = json.loads((pack.files_only_store(project) / "index" / "map.json").read_text(encoding="utf-8"))["files"]
    assert "payment.py" in mapped and not [f for f in mapped if f.startswith("mem/")], sorted(mapped)


def test_only_a_few_maps_are_kept(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(pack, "FILES_ONLY_KEEP", 2)
    folders = []
    for i in range(3):
        project, _store = shipped(tmp_path / f"p{i}")
        _wrap(tmp_path, project, capsys)
        folders.append(pack.files_only_store(project))
    assert [f.is_dir() for f in folders] == [False, True, True]
