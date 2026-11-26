"""Hook events devmem does not record are counted where the user sees them (devmem.hooks._count_unrecorded).

While git cannot say which files of the store it tracks and its last answer for the store was not clean, the hooks
record nothing (devmem.consent.records_while_unknown); before, those events left no trace at all (a loaded Windows run
of test_hooks_capture.py::test_appends_lossless_across_processes lost 44 of 240). Now each is counted in devmem's
per-user folder, and the session's next recorded event carries the count into its record (`not_recorded`); a count
whose session has not recorded since is listed by `devmem status`. With a clean earlier answer the events are recorded
(record-only) and nothing is counted. git's answer is made to time out where devmem asks git (store.git_run) and where
it reads the index itself (gitindex.index_candidates)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from hooks_helpers import make_project

from devmem import cli, consent, gitindex, hooks, store
from devmem.sessions import load_sessions

SID = "22222222-3333-4444-5555-666666666666"
MARK = "ZQXNOTRECMARK"
REAL_GIT_RUN = store.git_run
REAL_READER = gitindex.index_candidates


def _event() -> None:
    """A new hook process: no answers kept in memory."""
    store._FILTERS.clear()
    store._git_down_until = 0.0
    consent.forget_decisions()
    consent._FRESH_FAILURES.clear()


def _hook(project: Path, event: str, route: str = "init", sid: str = SID, **payload) -> str:
    _event()
    code, out = hooks.run([event], json.dumps({"session_id": sid, "cwd": str(project), "hook_event_name": event,
                                               "transcript_path": str(project / "none.jsonl"), **payload}).encode(),
                          route=route)
    assert code == 0
    return out


def _timeout_git(monkeypatch) -> None:
    """From now on git does not answer which files of the store it tracks within its limit, and devmem's own reading
    of the index runs out of time too."""
    def slow(where, *args, timeout=store.GIT_TIMEOUT, **kw):
        if "ls-files" in args and any(str(a).startswith(":(top") for a in args):
            raise subprocess.TimeoutExpired(["git"], timeout)
        return REAL_GIT_RUN(where, *args, timeout=timeout, **kw)
    monkeypatch.setattr(store, "git_run", slow)

    def slow_reader(*a, **k):
        raise gitindex.IndexUnknown("reading git's index took too long")
    monkeypatch.setattr(gitindex, "index_candidates", slow_reader)


def _git_answers_again(monkeypatch, st: Path) -> None:
    monkeypatch.setattr(store, "git_run", REAL_GIT_RUN)
    monkeypatch.setattr(gitindex, "index_candidates", REAL_READER)
    p = consent._checked_path(st)
    d = json.loads(p.read_text(encoding="utf-8"))
    d["failed"]["at"] -= consent.FAILURE_SECONDS + 1          # the kept failure has run out
    p.write_text(json.dumps(d), encoding="utf-8")
    _event()


def _change_index(project: Path) -> None:
    (project / "other.py").write_text("y = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "other.py"], check=True, capture_output=True)


def _events(project: Path, sid: str = SID) -> list[dict]:
    log = project / ".devmemory" / "live" / f"{sid}.jsonl"
    return [json.loads(x) for x in log.read_text(encoding="utf-8").split("\n") if x.strip()] if log.exists() else []


def _turn(project: Path, n_tools: int, route: str = "init") -> int:
    """One turn's events: prompt, n_tools tool calls, Stop. Returns how many events that was."""
    _hook(project, "UserPromptSubmit", route=route, prompt=f"fix the label {MARK}")
    for i in range(n_tools):
        _hook(project, "PostToolUse", route=route, tool_name="Read", tool_use_id=f"t{route}{i}",
              tool_input={"file_path": str(project / "greet.py")}, tool_response={"type": "text"})
    _hook(project, "Stop", route=route, last_assistant_message="done")
    return n_tools + 2


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


def test_with_a_clean_earlier_answer_every_event_is_recorded_and_none_counted(project, monkeypatch):
    st = project / ".devmemory"
    (st / "config.json").write_text("{}", encoding="utf-8")                   # packs on: nothing may be served
    _event()
    assert consent.store_refusal(project, st) is None                         # git's answer: nothing tracked
    _change_index(project)
    _timeout_git(monkeypatch)
    msg = json.loads(_hook(project, "SessionStart", source="startup"))["systemMessage"]
    assert "still recorded" in msg, msg
    out = _hook(project, "UserPromptSubmit", prompt=f"fix the label {MARK}")
    assert MARK not in out and "additionalContext" not in out               # nothing served
    for i in range(5):
        _hook(project, "PostToolUse", tool_name="Read", tool_use_id=f"t{i}",
              tool_input={"file_path": str(project / "greet.py")}, tool_response={"type": "text"})
    _hook(project, "Stop", last_assistant_message="done")
    names = [e["event"] for e in _events(project)]
    assert names == ["SessionStart", "UserPromptSubmit"] + ["PostToolUse"] * 5 + ["Stop"], names
    assert hooks.unrecorded_left(st) == [] and not hooks._unrecorded_file(st, SID).exists()


def test_without_one_nothing_is_appended_every_event_is_counted_and_the_count_arrives_later(project, monkeypatch,
                                                                                              capsys):
    st = project / ".devmemory"
    _timeout_git(monkeypatch)                                     # this store never had a clean answer
    n = 0
    # both handler sets fire (the init switch is on): counted once, by the init route, which also gives the notice
    assert "records nothing" in json.loads(_hook(project, "SessionStart", source="startup"))["systemMessage"]
    assert _hook(project, "SessionStart", route="plugin", source="startup") == ""
    n += 1
    n += _turn(project, 3)
    _turn(project, 3, route="plugin")
    assert not (st / "live").exists() and not (st / "sessions").exists()     # nothing appended to the store
    [left] = hooks.unrecorded_left(st)
    assert left["count"] == n and left["kinds"] == {"git_unknown": n}, left
    assert left["events"] == {"SessionStart": 1, "UserPromptSubmit": 1, "PostToolUse": 3, "Stop": 1}
    assert cli.main(["status", "--project", str(project)]) == 0
    status = capsys.readouterr().out
    assert f"{n} hook event(s) of 1 session(s) were not recorded (git_unknown: {n})" in status, status

    _git_answers_again(monkeypatch, st)
    _hook(project, "UserPromptSubmit", prompt="next")
    evs = _events(project)
    assert [e["event"] for e in evs] == ["NotRecorded", "UserPromptSubmit"], evs
    nr = evs[0]
    assert nr["count"] == n and nr["kinds"] == {"git_unknown": n} and nr["first"] <= nr["last"], nr
    assert hooks.unrecorded_left(st) == []                       # taken: not listed again, not carried twice
    _hook(project, "Stop", last_assistant_message="done")
    _hook(project, "SessionEnd", reason="other")
    assert [e["event"] for e in _events(project)].count("NotRecorded") <= 1
    rec = json.loads(next((st / "sessions").glob("*.meta.json")).read_text(encoding="utf-8"))
    assert rec["not_recorded"]["events"] == n and rec["not_recorded"]["kinds"] == {"git_unknown": n}, rec
    assert rec["not_recorded"]["first"] == nr["first"] and "not in this record" in rec["not_recorded"]["source"]
    assert len(rec["turns"]) == 1 and MARK not in json.dumps(rec)
    [s] = load_sessions(st, project=project)
    assert s["not_recorded"]["events"] == n
    assert cli.main(["status", "--project", str(project)]) == 0
    assert f"({n} hook event(s) not recorded)" in capsys.readouterr().out


_CHILD = """
import json, subprocess, sys, threading
from devmem import gitindex, hooks, store
real = store.git_run
def slow(where, *args, timeout=store.GIT_TIMEOUT, **kw):
    if "ls-files" in args and any(str(a).startswith(":(top") for a in args):
        raise subprocess.TimeoutExpired(["git"], timeout)
    return real(where, *args, timeout=timeout, **kw)
def slow_reader(*a, **k):
    raise gitindex.IndexUnknown("reading git's index took too long")
store.git_run, gitindex.index_candidates = slow, slow_reader
payload, tag, threads, per = json.loads(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
def work(t):
    for e in range(per):
        hooks.run(["PostToolUse"], json.dumps(dict(payload, tool_use_id=f"{tag}-{t}-{e}")).encode())
ts = [threading.Thread(target=work, args=(t,)) for t in range(threads)]
for t in ts:
    t.start()
for t in ts:
    t.join()
"""


def test_across_processes_every_event_not_recorded_is_counted(project, monkeypatch):
    st = project / ".devmemory"
    procs, threads, per = 6, 4, 10
    payload = json.dumps({"session_id": SID, "cwd": str(project), "hook_event_name": "PostToolUse",
                          "tool_name": "Read", "tool_input": {"file_path": str(project / "greet.py")},
                          "tool_response": {"type": "text"}})
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project), "PYTHONPATH": str(Path(hooks.__file__).parents[1])}
    children = [subprocess.Popen([sys.executable, "-c", _CHILD, payload, f"p{i}", str(threads), str(per)], env=env)
                for i in range(procs)]
    for c in children:
        assert c.wait(timeout=180) == 0
    assert not (st / "live").exists()
    [left] = hooks.unrecorded_left(st)
    assert left["count"] == procs * threads * per and left["kinds"] == {"git_unknown": procs * threads * per}, left
    _git_answers_again(monkeypatch, st)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    _hook(project, "PostToolUse", tool_name="Read", tool_use_id="after", tool_input={"file_path": "greet.py"},
          tool_response={"type": "text"})
    evs = _events(project)
    assert [e["event"] for e in evs] == ["NotRecorded", "PostToolUse"] and evs[0]["count"] == procs * threads * per


def test_the_count_is_bounded_and_old_counts_are_deleted(project, monkeypatch):
    st = project / ".devmemory"
    _timeout_git(monkeypatch)
    monkeypatch.setattr(hooks, "UNRECORDED_FILE_CAP", 200)       # about 5 lines
    for i in range(20):
        _hook(project, "PostToolUse", tool_name="Read", tool_use_id=f"t{i}", tool_input={"file_path": "greet.py"},
              tool_response={"type": "text"})
    path = hooks._unrecorded_file(st, SID)
    assert path.stat().st_size < 200 + 64
    [left] = hooks.unrecorded_left(st)
    assert 0 < left["count"] < 20 and left["at_least"] is True
    assert path.parent.parent == consent.store_note(st).parent and path.name != SID and SID not in str(path)
    monkeypatch.setattr(hooks, "UNRECORDED_FILES", 3)            # at most 3 sessions per store
    for k in range(5):
        _hook(project, "SessionStart", sid=f"33333333-0000-0000-0000-00000000000{k}", source="startup")
    assert len(os.listdir(path.parent)) == 3
    old = time.time() - hooks.UNRECORDED_KEEP_S - 60
    for f in path.parent.iterdir():
        os.utime(f, (old, old))
    assert hooks.unrecorded_left(st) == [] and os.listdir(path.parent) == []


def test_counting_does_not_raise_when_the_per_user_folder_cannot_hold_it(project, monkeypatch):
    st = project / ".devmemory"
    _timeout_git(monkeypatch)
    blocked = Path(os.environ["DEVMEM_USER_DATA"]) / "stores" / (consent.store_key(st) + ".unrecorded")
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("a file where the folder belongs", encoding="utf-8")
    hooks._UNRECORDED_DIRS.clear()
    t = time.perf_counter()
    _hook(project, "PostToolUse", tool_name="Read", tool_use_id="x", tool_input={"file_path": "greet.py"},
          tool_response={"type": "text"})
    assert time.perf_counter() - t < 10
    assert hooks.unrecorded_left(st) == [] and not (st / "live").exists()


def _link(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("kind", ["not_yours", "pulled_files", "link"])
def test_events_of_a_store_refused_for_another_reason_are_counted_with_that_reason(tmp_path, kind):
    route = "init"
    if kind == "not_yours":         # a store this user never set up or adopted; the plugin's hooks fire for the project
        project, route = make_project(tmp_path, switched_on=False), "plugin"
    else:
        project = make_project(tmp_path)
    st = project / ".devmemory"
    if kind == "pulled_files":
        rec = st / "sessions" / "20261102T1000_bbbbbbbb.meta.json"
        rec.parent.mkdir()
        rec.write_text('{"session_id": "bbbbbbbb", "prompt": "pulled"}', encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "add", "-f", str(rec)], check=True, capture_output=True)
    elif kind == "link":
        (tmp_path / "elsewhere").mkdir()
        _link(st / "live", tmp_path / "elsewhere")
    n = _turn(project, 2, route=route)
    [left] = hooks.unrecorded_left(st)
    assert left["count"] == n and left["kinds"] == {kind: n}, left
    assert not list((tmp_path / "elsewhere").iterdir()) if kind == "link" else not (st / "live").exists()


def test_a_session_devmem_records_itself_is_not_counted(project, monkeypatch):
    _timeout_git(monkeypatch)
    monkeypatch.setenv(hooks.NO_CAPTURE_ENV, "1")
    _turn(project, 2)
    assert hooks.unrecorded_left(project / ".devmemory") == []


def test_a_refused_store_is_counted_even_with_capture_off_in_its_config(project, monkeypatch):
    """docs/INSTALL.md: the store's config.json is not read while the store is refused, so `"capture": false` there does not stop
    the count (the reviewer's h2 capture:false case)."""
    (project / ".devmemory" / "config.json").write_text('{"capture": false}', encoding="utf-8")
    _timeout_git(monkeypatch)
    n = _turn(project, 1)
    [left] = hooks.unrecorded_left(project / ".devmemory")
    assert left["count"] == n


def test_a_fully_taken_count_makes_room_for_another_session(project, monkeypatch):
    st = project / ".devmemory"
    _timeout_git(monkeypatch)
    monkeypatch.setattr(hooks, "UNRECORDED_FILES", 2)
    a, b, c = (f"44444444-0000-0000-0000-00000000000{k}" for k in range(3))
    _hook(project, "SessionStart", sid=a, source="startup")
    _hook(project, "SessionStart", sid=b, source="startup")
    fa = hooks._unrecorded_file(st, a)
    with open(fa, "ab") as f:              # a's count was taken into its record (as _fold_unrecorded marks it)
        f.write(f"{time.time():.3f} taken {fa.stat().st_size}\n".encode("ascii"))
    _hook(project, "SessionStart", sid=c, source="startup")
    assert not fa.exists() and hooks._unrecorded_file(st, b).exists() and hooks._unrecorded_file(st, c).exists()
    assert sorted(x["count"] for x in hooks.unrecorded_left(st)) == [1, 1]


def test_the_app_gets_the_count_with_the_session(project, monkeypatch):
    from ui_helpers import request, running_server, with_token

    from devmem.scan import scan
    st = project / ".devmemory"
    _timeout_git(monkeypatch)
    n = _turn(project, 1)
    _git_answers_again(monkeypatch, st)
    _turn(project, 1)
    _hook(project, "SessionEnd", reason="other")
    scan(project, st)
    with running_server(project, st) as (port, token):
        _, _, body = request(port, "GET", "/api/sessions", with_token(token))
        [row] = body["sessions"]
        _, _, d = request(port, "GET", "/api/session?id=" + row["id"][:8], with_token(token))
    assert d["meta"]["not_recorded"]["events"] == n
    app = (Path(hooks.__file__).parent / "ui" / "app.js").read_text(encoding="utf-8")
    assert "m.not_recorded" in app and "${nrHtml}" in app        # the session page's banner reads it


def test_a_repository_cloned_again_at_the_same_path_is_written_into_until_git_answers(project, monkeypatch):
    """The known limit SECURITY.md states: git's last clean answer is kept by the store's path only. A project deleted
    and cloned again at the same path, with a `.devmemory/` the repository ships (tracked by git), inherits it: while
    git cannot answer, the hooks record this session's own files into that store (record-only: nothing is read or
    served); once git answers, the store is refused for its tracked files, the notice says so and events are counted."""
    import shutil
    st = project / ".devmemory"
    _event()
    assert consent.store_refusal(project, st) is None                         # a clean answer for this path
    shutil.rmtree(st)                                                         # the project is cloned again here ...
    (st / "sessions").mkdir(parents=True)                                     # ... and it ships a store
    shipped = st / "sessions" / "20261102T1000_bbbbbbbb.meta.json"
    shipped.write_text('{"session_id": "bbbbbbbb", "prompt": "shipped"}', encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "-f", str(shipped)], check=True, capture_output=True)
    _timeout_git(monkeypatch)
    out = _hook(project, "UserPromptSubmit", prompt=f"fix the label {MARK}")
    assert "shipped" not in out and [e["event"] for e in _events(project)] == ["UserPromptSubmit"]
    _hook(project, "Stop", last_assistant_message="done")                    # the turn's record is written there too
    assert "*" in (st / ".gitignore").read_text(encoding="utf-8").split()      # and devmem's ignore line
    monkeypatch.setattr(store, "git_run", REAL_GIT_RUN)                      # git answers again
    monkeypatch.setattr(gitindex, "index_candidates", REAL_READER)
    p = consent._checked_path(st)
    d = json.loads(p.read_text(encoding="utf-8"))
    d["failed"]["at"] -= consent.FAILURE_SECONDS + 1
    p.write_text(json.dumps(d), encoding="utf-8")
    msg = json.loads(_hook(project, "SessionStart", source="resume"))["systemMessage"]
    assert "is not recording this project" in msg and "not known to be yours" in msg, msg
    assert "tracked by git" in (consent.store_refusal(project, st) or "")
    _hook(project, "PostToolUse", tool_name="Read", tool_use_id="t", tool_input={"file_path": "greet.py"},
          tool_response={"type": "text"})
    assert [e["event"] for e in _events(project)] == ["UserPromptSubmit", "Stop"]     # nothing more written
    [left] = hooks.unrecorded_left(st)
    assert left["count"] == 2 and left["kinds"] == {"pulled_files": 2}, left


@pytest.mark.parametrize("when", ["during_the_question", "before_the_asking_mark"])
def test_a_failure_written_after_another_process_answered_keeps_that_answer(project, monkeypatch, when):
    """Two hook processes ask about the same index: A reads `<store key>.checked` (no answer yet), B gets git's clean
    answer and writes it, then A's git times out and A writes its failure. A's write replaced the file with what A had
    read, so B's answer was lost: every later event saw "no clean answer" and was refused (only counted) instead of
    recorded (loss loop evidence_new_19). B's write is simulated at the exact point, no timing involved."""
    st = project / ".devmemory"
    path = consent._checked_path(st)
    home_key = consent.project_key(store.git_home(project, st))
    stamp = consent._git_index_stamp(store.git_home(project, st))

    def b_answers() -> None:
        consent._write_json_private(path, {"home": home_key, "ok": {"stamp": stamp, "entries": []}})
    if when == "before_the_asking_mark":       # B writes right after A read the file, before A's "asking" mark
        real_read = store.read_regular

        def read(p, *a, **k):
            data = real_read(p, *a, **k)
            if Path(p) == path and not getattr(read, "done", False):
                read.done = True
                b_answers()
            return data
        monkeypatch.setattr(store, "read_regular", read)

    def a_times_out(*a, **k):
        if when == "during_the_question":
            b_answers()
        raise subprocess.TimeoutExpired(["git"], store.GIT_TIMEOUT)
    monkeypatch.setattr(store, "store_index_entries", a_times_out)
    assert consent._tracked(project, st, cached=True)[0] is None       # A: no answer of its own
    assert consent._last_answer(project, st) == []                     # B's clean answer is still there
    _event()
    _timeout_git(monkeypatch)                                         # git still cannot say: record-only, not refused
    _change_index(project)
    _hook(project, "PostToolUse", tool_name="Read", tool_use_id="t", tool_input={"file_path": "greet.py"},
          tool_response={"type": "text"})
    assert [e["event"] for e in _events(project)] == ["PostToolUse"] and hooks.unrecorded_left(st) == []


GIT_WHY = "git could not say which files of x it tracks (git did not answer within 5 s)"


def test_a_count_whose_file_is_full_after_its_mark_says_at_least(project, monkeypatch):
    """The reviewer's h2 just_under_cap_then_taken: the fold's `taken` mark pushed the file past its bound, later
    refused events were not counted, and nothing said "at least"."""
    from devmem.redact import Redactor
    st = project / ".devmemory"
    line = len(f"{time.time():.3f} git_unknown PostToolUse\n")
    monkeypatch.setattr(hooks, "UNRECORDED_FILE_CAP", 7 * line + 5)          # 7 lines fit, the mark does not
    for _ in range(7):
        hooks._count_unrecorded(st, SID, "PostToolUse", GIT_WHY)
    hooks._fold_unrecorded(st, SID, Redactor(None))
    [nr] = [e for e in _events(project) if e["event"] == "NotRecorded"]
    assert nr["count"] == 7 and nr.get("at_least") is True, nr
    hooks._count_unrecorded(st, SID, "PostToolUse", GIT_WHY)                 # not counted any more: past the bound
    assert hooks.unrecorded_left(st) == []


def test_lines_are_never_carried_into_the_log_twice(project, monkeypatch):
    """The reviewer's h2 read-only count file: the NotRecorded event was appended, the `taken` mark could not be, and
    every later event appended the same count again. The mark is written first; without it nothing is carried."""
    from devmem.redact import Redactor
    st = project / ".devmemory"
    for _ in range(3):
        hooks._count_unrecorded(st, SID, "PostToolUse", GIT_WHY)
    count_file = hooks._unrecorded_file(st, SID)
    real = hooks._append_bytes

    def no_mark(path, data):
        if Path(path) == count_file:
            raise PermissionError("read-only")
        return real(path, data)
    monkeypatch.setattr(hooks, "_append_bytes", no_mark)
    for _ in range(3):
        hooks._fold_unrecorded(st, SID, Redactor(None))
    assert [e for e in _events(project) if e["event"] == "NotRecorded"] == []
    monkeypatch.setattr(hooks, "_append_bytes", real)
    for _ in range(3):
        hooks._fold_unrecorded(st, SID, Redactor(None))
    assert [e["count"] for e in _events(project) if e["event"] == "NotRecorded"] == [3]


def test_a_damaged_count_file_is_read_line_by_line_and_never_fails_status(project, capsys):
    """The reviewer's h2 tampered file: a time of nan, inf, 1e20 or a negative one, or a `taken` offset of 5,000
    digits, made `devmem status` exit 2 and logged a fold error on every event; a garbage `taken` line erased counts.
    Only lines devmem writes count; any other line is skipped."""
    import random
    from devmem.redact import Redactor
    st = project / ".devmemory"
    rng = random.Random(1951)
    bad = [b"nan git_unknown PostToolUse", b"inf git_unknown PostToolUse", b"1e20 git_unknown PostToolUse",
           b"-5 git_unknown PostToolUse", b"1794887945.670 taken " + b"9" * 5000, b"1794887945.670 taken 99999999",
           b"1794887945.670 git_unknown Nope", b"1794887945.670 bogus PostToolUse", b"\xff\xfe junk", b"", b"a b c d",
           b"1794887945.670 taken -1", b"1e400 taken 0", b"1794887945.670git_unknown PostToolUse"]
    good = 0
    lines = []
    for _ in range(400):
        if rng.random() < 0.3:
            lines.append(f"{1794887945 + rng.randrange(1000)}.{rng.randrange(1000):03d} git_unknown Stop".encode())
            good += 1
        else:
            junk = rng.choice(bad) if rng.random() < 0.7 else bytes(rng.randrange(256) for _ in range(rng.randrange(40)))
            lines.append(junk.replace(b"\n", b" "))
    path = hooks._unrecorded_file(st, SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\n".join(lines) + b"\n")
    [left] = hooks.unrecorded_left(st)
    assert left["count"] == good and left["kinds"] == {"git_unknown": good}, left
    assert cli.main(["status", "--project", str(project)]) == 0
    assert f"{good} hook event(s) of 1 session(s) were not recorded" in capsys.readouterr().out
    hooks._fold_unrecorded(st, SID, Redactor(None))
    assert not (st / "hook_errors.log").exists()
    assert [e["count"] for e in _events(project) if e["event"] == "NotRecorded"] == [good]
