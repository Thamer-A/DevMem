"""A committed repository must not put devmem into "git could not say" for good (the reviewer's k1: 4,100 committed
folders `d~N/f` passed the 4,096-folder cap; `devmem init` promised recording, the SessionStart notice said it would
clear within a minute, and nothing was recorded or served, for ever). Folders whose names merely hold `~` are ruled
out by what the file system says once (store._Level); a real limit has its own wording on every route, `devmem adopt`
is the way past it, and the hooks keep recording through it when git's last answer was clean."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from cli_helpers import run_cli
from hooks_helpers import make_project

from devmem import consent, hooks, store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
G = ["-c", "core.protectNTFS=false", "-c", "core.autocrlf=false"]
SID = "c0ffee00-0000-4000-8000-000000000001"


def _git(project: Path, *args: str, input: bytes | None = None) -> str:
    return subprocess.run(["git", "-C", str(project), *G, *args], input=input, capture_output=True,
                          check=True).stdout.decode()


def _index_entries(project: Path, paths) -> None:
    """Entries in git's index only (as a commit brings them, without the files on disk)."""
    blob = _git(project, "hash-object", "-w", "--stdin", input=b"x\n").strip()
    _git(project, "update-index", "--index-info", input="".join(f"100644 {blob}\t{p}\n" for p in paths).encode())


def _fresh() -> None:
    store._FILTERS.clear()
    store._git_down_until = 0.0
    consent.forget_decisions()
    consent._FRESH_FAILURES.clear()


REAL_CANDIDATES = store._candidates_raw


def _no_git(monkeypatch) -> None:
    def no_git(*a, **k):
        raise FileNotFoundError("git was not found on PATH")
    monkeypatch.setattr(store, "_candidates_raw", no_git)


@pytest.mark.parametrize("route", ["git", "reader"])
@pytest.mark.parametrize("on_disk", [False, True])
def test_many_committed_folders_with_a_tilde_are_ruled_out_without_a_look_up_each(tmp_path, monkeypatch, route,
                                                                                    on_disk):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    n = 300 if on_disk else 100_000
    if on_disk:                                 # a fresh clone: the folders exist
        for i in range(n):
            (project / f"d~{i}").mkdir()
            (project / f"d~{i}" / "f").write_text("x\n", encoding="utf-8")
    _index_entries(project, (f"d~{i}/f" for i in range(n)))
    looked = []
    real = store._same_folder
    monkeypatch.setattr(store, "_same_folder", lambda *a: looked.append(a) or real(*a))
    if route == "reader":
        _no_git(monkeypatch)
    _fresh()
    t = time.perf_counter()
    assert store.store_index_entries(project, st, timeout=60) == []
    took = time.perf_counter() - t
    assert looked == [], looked[:3]
    print(f"{n} committed d~N folders ({route}, on disk: {on_disk}): {took:.2f} s")


def test_the_stores_own_short_name_still_lands(tmp_path, monkeypatch):
    """The rule out keeps the spelling it exists for: an 8.3 short name of the store (where the file system has one)."""
    project = make_project(tmp_path)
    st = project / ".devmemory"
    known, short = store._short_name(str(st)) if store.os.name == "nt" else (False, None)
    if not short:
        pytest.skip("no 8.3 short name for the store here")
    _index_entries(project, ["d~1/x", f"{short}/sessions/x.json"])
    _fresh()
    got = store.store_index_entries(project, st)
    assert [g.split(" ", 1)[1] for g in got] == [f"{short}/sessions/x.json"]


def _hook(project: Path, event: str, **payload) -> str:
    _fresh()
    code, out = hooks.run([event], json.dumps({"session_id": SID, "cwd": str(project), "hook_event_name": event,
                                               **payload}).encode("utf-8"))
    assert code == 0
    return out


def test_a_limit_has_its_own_wording_and_records_nothing_even_after_a_clean_answer(tmp_path, monkeypatch):
    """The reviewer's j1 limit_after_clean: a pull brought more tracked store entries than devmem keeps, git answered,
    and the hooks went on writing into that store (record-only) because an earlier answer was clean. A limit does not
    pass by itself: nothing is recorded (each event is counted, kind git_limit) until the user adopts the store."""
    project = make_project(tmp_path)
    st = project / ".devmemory"
    assert consent.store_refusal(project, st) is None                       # a clean answer first
    _index_entries(project, (f".devmemory-{i}/f" for i in range(20)))                # a pull brings them
    monkeypatch.setattr(store, "MAX_STORE_CANDIDATES", 5)                    # a low cap: a limit is reached
    out = _hook(project, "SessionStart", source="startup")
    msg = json.loads(out)["systemMessage"]
    assert "does not clear by itself" in msg and "devmem adopt" in msg and "records nothing" in msg, msg
    assert "within a minute" not in msg and "right now" not in msg
    _hook(project, "UserPromptSubmit", prompt="explain greet")
    assert not list((st / "live").glob(f"{SID}*.jsonl")), "nothing is recorded through a limit"
    [left] = hooks.unrecorded_left(st)
    assert left["kinds"] == {"git_limit": 2}, left
    why = consent.store_refusal(project, st)
    assert consent.is_git_limit(why) and consent.is_git_unknown(why) and consent.is_limit_failure(st)


def test_a_limit_from_the_reader_is_a_limit_too(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    _index_entries(project, (f".devmemory-{i}/f" for i in range(20)))
    _no_git(monkeypatch)
    monkeypatch.setattr(store, "MAX_STORE_CANDIDATES", 5)
    _fresh()
    with pytest.raises(store.GitLimit):
        store.store_index_entries(project, st)


def test_init_does_not_promise_recording_through_a_limit_and_adopt_is_the_way_past_it(tmp_path):
    project = make_project(tmp_path, init_store=False)
    # more store entries than devmem keeps (consent.TRACKED_CAP): a limit with the real constants
    _index_entries(project, (f".devmemory/sessions/s{i}.json" for i in range(consent.TRACKED_CAP + 1)))
    r = run_cli(["init", "--project", str(project), "--no-scan"], cwd=tmp_path, timeout=300)
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, r.stderr
    assert "From the next Claude Code session" not in out, out
    assert "does not clear by itself" in out and "devmem adopt" in out, out
    r = run_cli(["adopt", "--project", str(project), "--yes"], cwd=tmp_path, timeout=300)
    assert r.returncode == 0, (r.stdout, r.stderr)
    _fresh()
    assert consent.store_refusal(project, project / ".devmemory") is None


def _adopt_past_a_limit(tmp_path, monkeypatch):
    """A store adopted while git's index is past a limit (a low cap), with the cap kept for what follows."""
    project = make_project(tmp_path)
    st = project / ".devmemory"
    _index_entries(project, (f".devmemory-{i}/f" for i in range(20)))
    monkeypatch.setattr(store, "MAX_STORE_CANDIDATES", 5)
    _fresh()
    why = consent.store_refusal(project, st)
    assert consent.is_git_limit(why), why
    consent.claim_store(st, project, adopt=True)
    _fresh()
    return project, st


def test_adopting_past_a_limit_covers_that_index_only(tmp_path, monkeypatch):
    """The reviewer's k3: after `devmem adopt` past a limit, ONE later index change that both reaches a limit and makes
    git track a store file was accepted for good. The acceptance is bound to the index stamp and the reason the user
    saw: a changed index is asked about again, and refused again, with a notice that says why."""
    project, st = _adopt_past_a_limit(tmp_path, monkeypatch)
    note = consent._note(st)
    assert note["accept_limit"]["stamp"] == consent._git_index_stamp(project), note
    assert consent.store_refusal(project, st) is None                  # the state the user saw: accepted
    assert consent.store_refusal(project, st, check_git="cached") is None
    _index_entries(project, [".devmemory/sessions/zz.meta.json"])      # still past the limit, and a store file tracked
    _fresh()
    why = consent.store_refusal(project, st)
    assert consent.is_git_limit(why) and "earlier `devmem adopt` covered git's index as it was then" in why, why
    notice = consent.paused_notice(project, st, why, recording=False)
    assert "earlier `devmem adopt`" in notice, notice


def test_an_unbound_acceptance_from_before_accepts_nothing(tmp_path, monkeypatch):
    project, st = _adopt_past_a_limit(tmp_path, monkeypatch)
    note = consent._note(st)
    note["accept_limit"] = True
    consent._write_json_private(consent.store_note(st), note)
    _fresh()
    assert consent.is_git_limit(consent.store_refusal(project, st))


def test_a_killed_question_is_not_a_limit_adopting_accepts(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    st = project / ".devmemory"
    stamp = consent._git_index_stamp(project)
    consent._write_json_private(consent._checked_path(st), {
        "home": consent.project_key(project), "failed": {"stamp": stamp, "at": time.time(), "why": consent.DIED,
                                                         "died": 1}})
    _fresh()
    consent._tracked(project, st, cached=True)
    assert not consent.is_limit_failure(st)
    consent.claim_store(st, project, adopt=True)        # a command asks itself: the answer replaces the mark
    assert "accept_limit" not in consent._note(st)


def test_adopt_says_what_adopting_past_a_limit_means(tmp_path):
    """`devmem adopt` past a limit: the summary does not say the store stays unused, says before the question that a
    pull is not seen, and the store is used afterwards."""
    project = make_project(tmp_path, init_store=False)
    _index_entries(project, (f".devmemory/sessions/s{i}.json" for i in range(consent.TRACKED_CAP + 1)))
    (project / ".devmemory").mkdir()
    r = run_cli(["adopt", "--project", str(project), "--yes"], cwd=tmp_path, timeout=300)
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, (out, r.stderr)
    assert "the store is not used until" not in out, out
    i_pull = out.index("Past this limit devmem cannot see what a pull brings")
    assert i_pull < out.index("adopted:"), out
    _fresh()
    assert consent.store_refusal(project, project / ".devmemory") is None


@pytest.mark.parametrize("shape", ["untracked", "manyfiles", "split"])
def test_without_git_an_untracked_cache_is_read_and_a_split_index_names_the_fix(tmp_path, monkeypatch, shape):
    """The reviewer's u5: core.untrackedCache and feature.manyFiles (both write UNTR) made the reader decline, and the
    notice gave no command and promised a retry for a cause that does not pass by itself. The untracked cache is now
    skipped (it changes no entry); a split index is still declined, and the notice says what to run."""
    project = make_project(tmp_path)
    st = project / ".devmemory"
    (project / "u.txt").write_text("u\n", encoding="utf-8")
    if shape == "split":
        _git(project, "config", "core.splitIndex", "true")
        _git(project, "update-index", "--split-index")
    else:
        _git(project, "config", *(("core.untrackedCache", "true") if shape == "untracked"
                                  else ("feature.manyFiles", "true")))
        _git(project, "update-index", "--untracked-cache")
        _git(project, "status")
    assert (b"link" if shape == "split" else b"UNTR") in (project / ".git" / "index").read_bytes()
    _no_git(monkeypatch)
    _fresh()
    why = consent.store_refusal(project, st, check_git="cached")
    if shape != "split":
        assert why is None, why
        return
    assert consent.is_git_unknown(why) and not consent.is_git_limit(why), why
    assert "within a minute" not in why and "does not pass by itself" in why, why
    notice = consent.paused_notice(project, st, why, recording=True)
    assert "git update-index --no-split-index" in notice and "Put git on the PATH" in notice, notice
    assert "within a minute" not in notice, notice


def test_setup_past_a_limit_does_not_promise_recording_after_its_warning(tmp_path):
    project = make_project(tmp_path, init_store=False)
    _index_entries(project, (f".devmemory/sessions/s{i}.json" for i in range(consent.TRACKED_CAP + 1)))
    r = run_cli(["setup", "--project", str(project), "--no-scan", "--plugin-data", str(tmp_path / "data")],
                cwd=tmp_path, timeout=300)
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, (out, r.stderr)
    assert "records nothing" in out and "record from their next event" not in out, out


@pytest.mark.parametrize("route", ["git", "reader"])
def test_committed_tilde_folders_are_not_even_listed_where_they_cannot_be_the_store(tmp_path, monkeypatch, route):
    """The reviewer's k2 and t8: 18,000 committed `d~N/<3.8 KB path>` entries put more than 64 MB on the store
    question, and 300,000 `~` entries more than 250,000 candidates: a limit for an honest repository. Where the store
    has no 8.3 short name (or the file system keeps case apart, which makes none), no `~` entry is asked for; where it
    has one, only entries holding it."""
    project = make_project(tmp_path)
    st = project / ".devmemory"
    tilde = store._tilde_rule(str(Path(st).resolve()), {".devmemory"})
    if tilde is None:
        pytest.skip("this file system cannot rule 8.3 names out")
    _index_entries(project, [f"d~{i}/{'x' * 200}" for i in range(3000)] + ["DEVMEM~9/x"])
    monkeypatch.setattr(store, "MAX_STORE_CANDIDATES", 100)         # the old pathspec listed 3,001: a limit
    if route == "reader":
        _no_git(monkeypatch)
    _fresh()
    assert store.store_index_entries(project, st) == []
    if route == "git":
        listed = store._candidates_raw(project, st, 10.0, 1, tilde)
        assert not any(p.startswith(b"d~") for _o, p in listed), listed[:3]
    _fresh()
    assert consent.store_refusal(project, st) is None


def test_with_git_unusable_a_repository_of_non_ascii_names_answers(tmp_path, monkeypatch):
    """Item 8: the reader counted every non-ASCII path as a candidate; for a store name no non-ASCII letter can spell
    (`.devmemory` has none of f, i, k, l, s, t) they are not, and 300,000 such files no longer reach the cap."""
    from devmem import gitindex
    project = make_project(tmp_path)
    st = project / ".devmemory"
    _index_entries(project, [f"src/\u6587\u4ef6{i // 100:03d}/\u0444\u0430\u0439\u043b_{i:05d}_\u00e9t\u00e9.txt"
                             for i in range(3000)] + [".dev\u200cmemory/x", ".\u0434evmemory/y"])
    monkeypatch.setattr(store, "MAX_STORE_CANDIDATES", 100)
    _no_git(monkeypatch)
    _fresh()
    assert store.store_index_entries(project, st) == []
    got = [p for _o, p in gitindex.index_candidates(project, {".devmemory"}, time.monotonic() + 30, 100)]
    assert got == [".dev\u200cmemory/x".encode()]                     # HFS+ ignores U+200C: still asked about
    # a store name a non-ASCII letter can spell (the long s, the Kelvin sign): those paths are still candidates
    got = [p for _o, p in gitindex.index_candidates(project, {"tools"}, time.monotonic() + 30, 100)]
    assert got == [] or all(not p.isascii() for p in got)


def test_an_adoption_past_a_limit_holds_whichever_route_answers(tmp_path, monkeypatch):
    """The reviewer's a3: adopted with git on the PATH; the hooks, without git, got the same limit for the same index
    from devmem's own reader, worded "git is not on the PATH ..., and ...": the store was not used, and the message
    said the index had changed. The acceptance is bound to the index stamp and the kind of limit, not its text."""
    project, st = _adopt_past_a_limit(tmp_path, monkeypatch)
    assert consent._note(st)["accept_limit"]["kind"] == "size"
    _no_git(monkeypatch)
    _fresh()
    assert consent.store_refusal(project, st, check_git="cached") is None
    _fresh()
    assert consent.store_refusal(project, st) is None


def test_an_acceptance_with_the_reason_text_of_the_previous_round(tmp_path, monkeypatch):
    """{stamp, why} as the previous round wrote it: accepted while the stamp is the same (the kind is told from the
    text, whatever route answers now); refused with a true reason once the index changed."""
    project, st = _adopt_past_a_limit(tmp_path, monkeypatch)
    note = consent._note(st)
    note["accept_limit"] = {"stamp": note["accept_limit"]["stamp"], "why": "more than 5 entries of git's index could "
                            "be in the store; devmem does not look through them all"}
    consent._write_json_private(consent.store_note(st), note)
    _no_git(monkeypatch)
    _fresh()
    assert consent.store_refusal(project, st, check_git="cached") is None
    monkeypatch.setattr(store, "_candidates_raw", REAL_CANDIDATES)
    _index_entries(project, ["other.py"])                                # the index changes
    _fresh()
    why = consent.store_refusal(project, st)
    assert consent.is_git_limit(why) and "the index has changed since" in why, why


def test_a_same_index_refusal_does_not_say_the_index_changed(tmp_path, monkeypatch):
    project, st = _adopt_past_a_limit(tmp_path, monkeypatch)
    note = consent._note(st)
    note["accept_limit"]["kind"] = "tracked"                              # another kind of limit, same index
    consent._write_json_private(consent.store_note(st), note)
    _fresh()
    why = consent.store_refusal(project, st)
    assert consent.is_git_limit(why) and "changed since" not in why and "another kind of limit" in why, why
