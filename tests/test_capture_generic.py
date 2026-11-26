"""Tool-agnostic capture (begin/end): git-based before/after, snapshot fallback, overlapping sessions."""
from __future__ import annotations

import json
import threading

import pytest

from devmem import generic

from hooks_helpers import make_project, run_git


def _rec(meta):
    return json.loads(meta.read_text(encoding="utf-8"))


@pytest.mark.parametrize("git", [True, False])
def test_begin_end_records_write_edit_delete(tmp_path, git):
    project = make_project(tmp_path, git=git)
    (project / "draft.py").write_text("x = 1\n", encoding="utf-8")  # dirty (untracked) before begin
    store = project / ".devmemory"
    p = generic.begin(project, store)
    state = json.loads(p.read_text(encoding="utf-8"))
    assert state["kind"] == ("git" if git else "snapshot")
    if git:  # only dirty files are copied, not the whole tree
        assert set(state["dirty"]) == {"draft.py"}
    (project / "greet.py").write_text('def greet(name):\n    return "hi " + name\n', encoding="utf-8")
    (project / "created.py").write_text("def made():\n    return 1\n", encoding="utf-8")
    (project / "notes.txt").unlink()
    (project / "draft.py").write_text("x = 2\n", encoding="utf-8")
    (project / ".env").write_text("TOKEN=abc123abc123", encoding="utf-8")
    rec = _rec(generic.end(project, "edit things", provider="test", store=store))
    calls = {(c["tool"], c["file"]): c for c in rec["tool_calls"]}
    assert calls[("Write", "created.py")]["new"].startswith("def made()")
    assert calls[("Edit", "greet.py")]["new"].strip() == 'return "hi " + name'
    assert calls[("Edit", "draft.py")]["old"] == "x = 1" and calls[("Edit", "draft.py")]["new"] == "x = 2"
    assert ("Delete", "notes.txt") in calls
    assert rec["files_deleted"] == ["notes.txt"] and "created.py" in rec["files_edited"]
    assert not any(".env" in c["file"] for c in rec["tool_calls"])
    assert rec["schema"] == 2 and rec["turns"][0]["prompt"] == "edit things"
    if git:
        head = run_git(project, "rev-parse", "HEAD").strip()
        assert rec["git"]["head_start"] == head == rec["git"]["head_end"]
    assert not list((store / "pending").glob("*"))


def test_git_commit_during_session_is_seen(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    generic.begin(project, store)
    (project / "greet.py").write_text("def greet(name):\n    return name\n", encoding="utf-8")
    run_git(project, "add", "greet.py")
    run_git(project, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "change")
    rec = _rec(generic.end(project, "commit it", store=store))
    assert rec["files_edited"] == ["greet.py"] and rec["git"]["head_start"] != rec["git"]["head_end"]


def test_overlapping_sessions_do_not_clobber(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    a = generic.begin(project, store)
    (project / "one.py").write_text("one = 1\n", encoding="utf-8")
    b = generic.begin(project, store)
    (project / "two.py").write_text("two = 2\n", encoding="utf-8")
    assert a != b and a.exists() and b.exists()
    ra = _rec(generic.end(project, "A", store=store, pending_id=a.stem))
    rb = _rec(generic.end(project, "B", store=store, pending_id=b.stem))
    assert ra["files_edited"] == ["one.py", "two.py"]  # the tree changed under A (both edits)
    assert rb["files_edited"] == ["two.py"]             # B began after one.py existed
    with pytest.raises(RuntimeError):
        generic.end(project, "C", store=store)


def test_concurrent_ends_claim_one_begin_each(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    begins, ends = 4, 9
    ids = sorted(generic.begin(project, store).stem for _ in range(begins))
    results, no_session, other = [], [], []

    def run():
        try:
            results.append(generic.end(project, "x", store=store))
        except RuntimeError as e:  # the documented "no session in progress"
            no_session.append(e)
        except BaseException as e:  # noqa: BLE001 - anything else is a bug (was FileNotFoundError)
            other.append(e)
    threads = [threading.Thread(target=run) for _ in range(ends)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert other == []
    assert len(results) == begins and len(set(results)) == begins
    assert sorted(_rec(m)["begin_id"] for m in results) == ids  # every begin consumed exactly once
    assert len(no_session) == ends - begins
    assert not list((store / "pending").iterdir())


def test_legacy_pending_json_still_ends(tmp_path):
    project = make_project(tmp_path, git=False)
    store = project / ".devmemory"
    (store / "pending.json").write_text(json.dumps({"started_at": "2026-11-01T00:00:00Z",
                                                    "snapshot": generic.snapshot(project)}), encoding="utf-8")
    (project / "greet.py").write_text("changed\n", encoding="utf-8")
    rec = _rec(generic.end(project, "old begin", store=store))
    assert rec["files_edited"] == ["greet.py"] and not (store / "pending.json").exists()


def test_long_texts_capped_with_flag():
    edits, changed, _ = generic.diff_edits({}, {"a.txt": "q" * 9000})
    assert changed == ["a.txt"] and len(edits[0]["new"]) == 4000 and edits[0]["truncated"] is True


def test_failed_end_releases_the_begin_and_held_claim_blocks(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    pid = generic.begin(project, store).stem
    marker = store / "pending" / f"{pid}.json.claim"
    marker.write_text("other end", encoding="utf-8")  # a concurrent end holds it
    with pytest.raises(RuntimeError, match="no session"):
        generic.end(project, "x", store=store)
    marker.unlink()

    def boom(*a, **k):
        raise OSError("disk full")
    real = generic.record_generic
    monkeypatch.setattr(generic, "record_generic", boom)
    with pytest.raises(OSError):
        generic.end(project, "x", store=store)
    monkeypatch.setattr(generic, "record_generic", real)   # not undo(): that would also undo DEVMEM_USER_DATA
    assert (store / "pending" / f"{pid}.json").exists() and not marker.exists()  # released, not deleted
    assert _rec(generic.end(project, "retry", store=store))["begin_id"] == pid


# ---- nothing unredacted is written by `begin`; the diff still works (redacted against redacted) ----

KEY = "sk-proj-" + "A1b2C3d4E5f6G7h8" * 2


@pytest.mark.parametrize("git", [True, False])
def test_begin_writes_no_secret_and_end_still_diffs(tmp_path, git):
    project = make_project(tmp_path, git=git)
    (project / "settings.py").write_text(f'OPENAI_API_KEY = "{KEY}"\nDEBUG = True\n', encoding="utf-8")
    (project / ".env").write_text("DB_PASS=hunter2hunter2\n", encoding="utf-8")
    store = project / ".devmemory"
    p = generic.begin(project, store)
    on_disk = p.read_text(encoding="utf-8")
    assert KEY not in on_disk and "hunter2hunter2" not in on_disk and "[REDACTED:openai-key]" in on_disk
    assert ".env" not in json.loads(on_disk).get("dirty", json.loads(on_disk).get("snapshot"))
    (project / "settings.py").write_text(f'OPENAI_API_KEY = "{KEY}"\nDEBUG = False\n', encoding="utf-8")
    meta = generic.end(project, "turn debug off", store=store, pending_id=p.stem)
    rec = _rec(meta)
    edit = next(c for c in rec["tool_calls"] if c["file"] == "settings.py")
    assert edit["old"] == "DEBUG = True" and edit["new"] == "DEBUG = False"
    assert rec["files_edited"] == ["settings.py"]
    assert not any(KEY in f.read_text(encoding="utf-8") for f in store.rglob("*") if f.is_file()
                   and f.suffix in (".json", ".jsonl"))


@pytest.mark.parametrize("git", [True, False])
def test_change_only_inside_a_redacted_secret_is_still_listed(tmp_path, git):
    project = make_project(tmp_path, git=git)
    (project / "settings.py").write_text(f'OPENAI_API_KEY = "{KEY}"\n', encoding="utf-8")
    store = project / ".devmemory"
    generic.begin(project, store)
    (project / "settings.py").write_text(f'OPENAI_API_KEY = "{KEY[:-4]}ZZZZ"\n', encoding="utf-8")  # rotated
    rec = _rec(generic.end(project, "rotate the key", store=store))
    assert rec["files_edited"] == ["settings.py"]
    assert not [c for c in rec["tool_calls"] if c["file"] == "settings.py"]  # the diff has nothing to show


def test_v1_begin_with_raw_text_is_redacted_at_end(tmp_path):
    project = make_project(tmp_path, git=False)
    store = project / ".devmemory"
    (store / "pending").mkdir(parents=True)
    (store / "pending" / "old.json").write_text(json.dumps(
        {"id": "old", "started_at": "2026-01-01T00:00:00Z", "kind": "snapshot",
         "snapshot": {"greet.py": f'KEY = "{KEY}"\nx = 1\n'}}), encoding="utf-8")
    (project / "greet.py").write_text(f'KEY = "{KEY}"\nx = 2\n', encoding="utf-8")
    rec = _rec(generic.end(project, "x", store=store, pending_id="old"))
    edit = next(c for c in rec["tool_calls"] if c["file"] == "greet.py")
    assert edit["old"] == "x = 1" and edit["new"] == "x = 2"


def test_prompt_source_user_by_default_and_reported_by_client_for_mcp(tmp_path):
    project = make_project(tmp_path)
    store = project / ".devmemory"
    generic.begin(project, store)
    rec = _rec(generic.end(project, "typed by me", store=store))
    assert rec["turns"][0]["prompt_source"] == "user" and rec["prompt_source"] == "user"
    generic.begin(project, store)
    rec = _rec(generic.end(project, "an AI client wrote this", store=store, prompt_source="reported-by-client"))
    assert rec["turns"][0]["prompt_source"] == "reported-by-client"


def test_wrap_tells_claude_code_hooks_not_to_record_again(tmp_path):
    import sys
    project = make_project(tmp_path)
    cmd = [sys.executable, "-c", "import os; print('flag=' + os.environ.get('DEVMEM_NO_CAPTURE', 'unset'))"]
    rec = _rec(generic.wrap(project, "check env", cmd, store=project / ".devmemory", with_pack=False))
    assert "flag=1" in rec["result_text"] and rec["turns"][0]["prompt_source"] == "user"


@pytest.mark.parametrize("kind", ["git", "snapshot", "legacy-slot"])
def test_old_raw_begins_are_rewritten_redacted_and_still_end(tmp_path, kind):
    """A begin written by an older version kept raw text; the next begin/end rewrites it in place (hash +
    redacted copy), keeps its mtime (so `end` without an id still picks the newest), and it still ends."""
    import os
    project = make_project(tmp_path, git=(kind == "git"))
    store = project / ".devmemory"
    old_text = f'KEY = "{KEY}"\nx = 1\n'
    (project / "greet.py").write_text(old_text, encoding="utf-8")
    body = {"started_at": "2026-01-01T00:00:00Z"}
    if kind == "git":
        body.update(id="old", kind="git", head=run_git(project, "rev-parse", "HEAD").strip(),
                    dirty={"greet.py": old_text, ".env": "DB_PASS=hunter2hunter2\n"})
    else:
        body.update(id="old", snapshot={"greet.py": old_text, ".env": "DB_PASS=hunter2hunter2\n"})
        if kind == "snapshot":
            body["kind"] = "snapshot"
    path = store / "pending.json" if kind == "legacy-slot" else store / "pending" / "old.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    os.utime(path, (1_000_000_000, 1_000_000_000))
    assert generic.secure_pending(store) == ["old"]
    text = path.read_text(encoding="utf-8")
    assert KEY not in text and "hunter2hunter2" not in text and json.loads(text)["v"] == 2
    assert path.stat().st_mtime == 1_000_000_000
    assert generic.secure_pending(store) == []                     # idempotent
    (project / "greet.py").write_text(f'KEY = "{KEY}"\nx = 2\n', encoding="utf-8")
    rec = _rec(generic.end(project, "x", store=store, pending_id=None if kind == "legacy-slot" else "old"))
    edit = next(c for c in rec["tool_calls"] if c["file"] == "greet.py")
    assert edit["old"] == "x = 1" and edit["new"] == "x = 2"


def test_secure_pending_leaves_a_begin_an_end_is_holding(tmp_path):
    project = make_project(tmp_path, git=False)
    store = project / ".devmemory"
    (store / "pending").mkdir(parents=True)
    p = store / "pending" / "held.json"
    p.write_text(json.dumps({"id": "held", "started_at": "2026-01-01T00:00:00Z", "kind": "snapshot",
                             "snapshot": {"a.py": "raw"}}), encoding="utf-8")
    (store / "pending" / "held.json.claim").write_text("123", encoding="utf-8")
    assert generic.secure_pending(store) == [] and '"raw"' in p.read_text(encoding="utf-8")
