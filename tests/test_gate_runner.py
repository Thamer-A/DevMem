"""The gate experiment's runner (evals/gate/run.py) with the stand-in claude (evals/gate/fake_claude.py): no model, no
network. A tiny local repository stands in for the real ones. Checks the pre-registered run counts, that a dry run
of 2 tasks x 2 arms writes complete lines, that a run writes nothing outside its own folder, the oracle arm, the
DevMem arm's pack log, the harness-failure rules, the freeze, and that a run which writes into a shared Node install
says so (deps_changed) and has that install dropped."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from evals.gate import run as R
from evals.gate import verify_freeze as F
from evals.gate import workspace as W

ORACLE = "Every export file name must end in .gate42.txt."
COMPLETE = ("passed", "uncached_input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens",
            "price_weighted", "turns", "wall_s", "model", "claude_version", "arm", "task", "repeat", "kind")


def _git(cwd, *args):
    subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, check=True, capture_output=True)


def _source_repo(tmp: Path) -> tuple[str, str]:
    src = tmp / "upstream"
    src.mkdir()
    (src / "app.py").write_text("def export(name):\n    return name\n", encoding="utf-8")
    _git(src, "init", "-q")
    _git(src, "add", "-A")
    _git(src, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "init")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True).stdout.strip()
    return src.as_uri(), sha


GRADE_ANSWER = (
    "from pathlib import Path\n\n\n"
    "def grade(work, env):\n"
    "    text = (Path(work) / 'answer.txt').read_text(encoding='utf-8') if (Path(work) / 'answer.txt').exists() else ''\n"
    "    ok = {NEEDLE!r} in text\n"
    "    return {'passed': ok, 'checks': {'answer': ok}}\n")


def _task_set(tmp: Path, monkeypatch) -> Path:
    tasks = tmp / "gate" / "tasks"
    url, sha = _source_repo(tmp)
    repo = tasks / "mini"
    (repo / "history").mkdir(parents=True)
    (repo / "repo.json").write_text(json.dumps({
        "name": "mini", "url": url, "commit": sha, "language": "python", "license": "MIT",
        "deps": {"kind": "python", "requirements": []}, "test": ["python", "-c", "pass"], "test_timeout_s": 60}),
        encoding="utf-8")
    patch = ("diff --git a/notes.md b/notes.md\nnew file mode 100644\n--- /dev/null\n+++ b/notes.md\n@@ -0,0 +1 @@\n"
             "+exports are plain text\n")
    (repo / "history" / "01.patch").write_text(patch, encoding="utf-8", newline="\n")
    (repo / "history" / "history.json").write_text(json.dumps({"sessions": [
        {"n": 1, "prompt": f"Add notes about exports. {ORACLE}", "patch": "01.patch",
         "requirements": [{"id": "R1", "text": ORACLE}]}]}), encoding="utf-8")
    for tid, kind, needle, oracle in (("H01", "history", ".gate42.txt", ORACLE), ("R01", "ordinary", "export", None)):
        d = repo / tid
        d.mkdir()
        (d / "task.json").write_text(json.dumps({
            "id": tid, "repo": "mini", "kind": kind, "prompt": "Add an export of the report to a file.",
            "requirement": "R1" if oracle else None, "oracle": oracle, "max_turns": 5, "timeout_s": 60}),
            encoding="utf-8")
        (d / "grade.py").write_text(GRADE_ANSWER.replace("{NEEDLE!r}", repr(needle)), encoding="utf-8")
    monkeypatch.setattr(W, "TASKS", tasks)
    monkeypatch.setattr(W, "TUNING", tmp / "gate" / "tuning")
    # The freeze check looks at the same (unfrozen) mini set, not at the real, frozen evals/gate/tasks.
    monkeypatch.setattr(F, "GATE", tmp / "gate")
    monkeypatch.setattr(F, "MANIFEST", tasks / "MANIFEST.sha256")
    return tasks


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    _task_set(tmp_path, monkeypatch)
    outer_home, outer_tmp = tmp_path / "outer-home", tmp_path / "outer-tmp"
    outer_home.mkdir()
    outer_tmp.mkdir()
    for k, v in (("HOME", outer_home), ("USERPROFILE", outer_home), ("TEMP", outer_tmp), ("TMP", outer_tmp),
                 ("TMPDIR", outer_tmp)):
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-never-reach-a-session")
    return {"root": tmp_path / "root", "results": tmp_path / "results", "home": outer_home, "tmp": outer_tmp}


def _run(gate, *args) -> int:
    return R.main(["run", "--root", str(gate["root"]), "--results", str(gate["results"]), "--fake", "--unfrozen",
                   *args])


def _rows(gate, stage) -> list[dict]:
    p = gate["results"] / f"{stage}.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _snapshot(top: Path) -> dict[str, tuple]:
    out = {}
    for p in top.rglob("*"):
        if p.is_file():
            st = p.stat()
            out[p.relative_to(top).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


def test_dry_run_two_tasks_two_arms_writes_complete_lines(gate):
    assert _run(gate, "--stage", "dry", "--arms", "S,D", "--repeats", "1", "--confirm", "4") == 0
    rows = _rows(gate, "dry")
    assert len(rows) == 4 and {(r["task"], r["arm"]) for r in rows} == {("H01", "S"), ("H01", "D"), ("R01", "S"),
                                                                         ("R01", "D")}
    for r in rows:
        assert all(r.get(k) is not None for k in COMPLETE), {k: r.get(k) for k in COMPLETE}
        assert r["harness_failure"] is None and r["fake"] and not r["frozen"]
        assert r["deps_changed"] is None                    # a python repository: no shared node_modules
        assert r["cache_write_5m"] == 40 and r["cache_write_1h"] == 10
        assert r["price_weighted"] == 100 + 1.25 * 40 + 2 * 10 + 0.1 * 200 + 5 * 30
        assert (gate["results"] / "raw" / r["run_id"] / "stream.jsonl").is_file()
    for r in rows:
        if r["arm"] == "D":     # DevMem's hook ran in the session and wrote its pack log line
            assert r["pack_lines"] == 1
        else:
            assert "pack_lines" not in r
    # a second call skips what is done
    assert _run(gate, "--stage", "dry", "--arms", "S,D", "--repeats", "1", "--confirm", "0") == 0
    assert len(_rows(gate, "dry")) == 4


def test_a_run_writes_nothing_outside_its_own_folder(gate, tmp_path):
    cfg = W.load_repo("mini")
    W.deps(cfg, gate["root"])
    W.base(cfg, gate["root"])            # shared caches, prepared once before any run
    before = _snapshot(tmp_path)
    assert _run(gate, "--stage", "iso", "--arms", "D", "--tasks", "H01", "--repeats", "1", "--confirm", "1") == 0
    row = _rows(gate, "iso")[0]
    changed = {k for k, v in _snapshot(tmp_path).items() if before.get(k) != v}
    own = (f"root/runs/{row['run_id']}/", f"results/raw/{row['run_id']}/", "results/iso.jsonl")
    assert changed and all(k.startswith(own) for k in changed), sorted(k for k in changed if not k.startswith(own))
    run = gate["root"] / "runs" / row["run_id"]
    # what the session wrote to its configuration, temp and home folders stayed in the run's folder
    assert (run / "config" / "projects" / "fake" / "memory" / "MEMORY.md").is_file()
    assert (run / "tmp" / "fake-claude.tmp").is_file() and (run / "home" / ".fake-claude-home").is_file()
    assert not (gate["home"] / ".fake-claude-home").exists() and not (gate["tmp"] / "fake-claude.tmp").exists()
    assert not (gate["root"] / "current").exists()


def test_no_api_key_reaches_a_session(gate):
    cfg = W.load_repo("mini")
    env = R.session_env(cfg, W.deps(cfg, gate["root"]), gate["root"] / "current", devmem=False)
    assert not [k for k in env if k.upper().startswith("ANTHROPIC_")]
    assert env["CLAUDE_CONFIG_DIR"].startswith(str(gate["root"] / "current"))
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "0"          # automatic memory forced on in every arm


def test_the_token_file_reaches_the_session_and_no_saved_file(gate, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    cfg = W.load_repo("mini")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in R.session_env(cfg, W.deps(cfg, gate["root"]), gate["root"] / "current",
                                                          devmem=False)
    secret = "tok-made-up-for-the-test-7f3a"
    (gate["root"] / R.TOKEN_FILE).write_text(f"﻿{secret}\r\n", encoding="utf-8")   # as Notepad might save it
    env = R.session_env(cfg, W.deps(cfg, gate["root"]), gate["root"] / "current", devmem=False)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == secret
    assert _run(gate, "--stage", "tok", "--arms", "S", "--tasks", "R01", "--repeats", "1", "--confirm", "1") == 0
    for top in (gate["root"] / "runs", gate["results"]):
        for p in top.rglob("*"):
            if p.is_file():
                assert secret.encode() not in p.read_bytes(), p


def test_a_prompt_that_starts_with_a_dash_is_passed_after_the_end_of_options(tmp_path, monkeypatch):
    R._sys_path()
    from devmem import proc
    from types import SimpleNamespace
    seen = {}

    def fake_run(argv, out, **_):
        seen["argv"] = argv
        Path(out).write_text("", encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="", timed_out=False, interrupted=False)

    monkeypatch.setattr(proc, "run_to_file", fake_run)
    cur = tmp_path / "current"
    (cur / "logs").mkdir(parents=True)
    (cur / "work").mkdir()
    prompt = "--skip-title-heading never removes the first heading. Fix it."    # md2conf R04 starts this way
    R.run_session(["claude"], prompt, cur, {}, 5)
    argv = seen["argv"]
    assert argv[-2:] == ["--", prompt] and argv.count(prompt) == 1
    assert argv.index("-p") < argv.index("--allowedTools") < argv.index("--")


@pytest.mark.parametrize("ignored", [True, False])
def test_the_diff_leaves_out_claude_and_devmemory_even_when_the_repo_ignores_them(tmp_path, ignored):
    work = tmp_path / "work"
    (work / ".claude").mkdir(parents=True)
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    (work / ".gitignore").write_text(".claude/\n" if ignored else "x\n", encoding="utf-8")   # bonsai-js lists .claude/
    (work / ".claude" / "tracked.md").write_text("keep\n", encoding="utf-8")
    _git(work, "init", "-q")
    _git(work, "add", "-A", "-f")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "init")
    (work / "a.txt").write_text("a\nb\n", encoding="utf-8")
    (work / "new.txt").write_text("new\n", encoding="utf-8")
    (work / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")   # a DevMem arm's hooks
    (work / ".claude" / "tracked.md").write_text("changed\n", encoding="utf-8")
    (work / ".devmemory").mkdir()
    (work / ".devmemory" / "x.json").write_text("{}\n", encoding="utf-8")
    diff = R.diff_against_start(work)
    assert "a/a.txt" in diff and "b/new.txt" in diff
    assert ".claude" not in diff and ".devmemory" not in diff


def test_the_oracle_arm_gets_the_requirement_in_its_prompt(gate):
    assert _run(gate, "--stage", "orc", "--arms", "S,O", "--kinds", "history", "--repeats", "1", "--confirm", "2") == 0
    by_arm = {r["arm"]: r for r in _rows(gate, "orc")}
    assert by_arm["O"]["passed"] and not by_arm["S"]["passed"]
    prompt = (gate["results"] / "raw" / by_arm["O"]["run_id"] / "prompt.txt").read_text(encoding="utf-8")
    assert prompt.startswith(f'Earlier in this project the user said: "{ORACLE}"')


def test_replay_keeps_the_configuration_and_the_store(gate):
    root = gate["root"]
    assert R.main(["replay", "--root", str(root), "--results", str(gate["results"]), "--fake", "--repo", "mini",
                   "--family", "D", "--confirm", "1"]) == 0
    state = root / "replay" / "mini" / "D"
    assert (state / "config" / "projects" / "fake" / "memory" / "MEMORY.md").is_file()
    assert (state / "store" / "packs.jsonl").is_file()
    # a later DevMem run starts from that state
    assert _run(gate, "--stage", "after", "--arms", "D", "--tasks", "H01", "--repeats", "1", "--confirm", "1") == 0
    row = _rows(gate, "after")[0]
    assert row["replayed_state"] and row["pack_lines"] == 2      # the replay's prompt and this one
    assert R.main(["replay", "--root", str(root), "--results", str(gate["results"]), "--fake", "--repo", "mini",
                   "--family", "D", "--confirm", "1"]) == 2       # never replayed twice by accident


def test_a_grader_timeout_is_a_harness_failure(gate, monkeypatch):
    monkeypatch.setattr(W, "grade", lambda task, work, env: {"passed": False, "checks": {}, "grader_timeout": True})
    assert _run(gate, "--stage", "gt", "--arms", "S", "--tasks", "R01", "--repeats", "1", "--confirm", "1") == 0
    row = _rows(gate, "gt")[0]
    assert row["harness_failure"] == "grader timed out" and not row["passed"]
    # it is run once more
    assert _run(gate, "--stage", "gt", "--arms", "S", "--tasks", "R01", "--repeats", "1", "--confirm", "1") == 0
    assert [r["attempt"] for r in _rows(gate, "gt")] == [1, 2]


def _node_install(gate) -> Path:
    """Make the mini repository a Node one, with its shared install already in place (so no npm runs)."""
    repo = W.TASKS / "mini"
    cfg = json.loads((repo / "repo.json").read_text(encoding="utf-8"))
    cfg["deps"] = {"kind": "node", "lock": "package-lock.json"}
    (repo / "repo.json").write_text(json.dumps(cfg), encoding="utf-8")
    (repo / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    d = W.deps_path(W.load_repo("mini"), gate["root"])
    (d / "node_modules" / "vitest").mkdir(parents=True)
    (d / "node_modules" / "vitest" / "package.json").write_text("{}\n", encoding="utf-8")
    (d / "node_modules" / ".package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    (d / ".installed").write_text("ok\n", encoding="utf-8")
    assert W.deps(W.load_repo("mini"), gate["root"]) == d
    return d


def test_deps_fingerprint_sees_top_level_names_and_the_lock(tmp_path):
    nm = tmp_path / "node_modules"
    (nm / "vitest").mkdir(parents=True)
    (nm / ".package-lock.json").write_text("{}\n", encoding="utf-8")
    first = W.deps_fingerprint(tmp_path)
    assert W.deps_fingerprint(tmp_path) == first
    (nm / "vitest" / "cache.json").write_text("{}\n", encoding="utf-8")   # cheap: inside a package is not looked at
    assert W.deps_fingerprint(tmp_path) == first
    for name in (".vite", ".vitest", ".cache"):                            # dot-entries count
        (nm / name).mkdir()
        assert W.deps_fingerprint(tmp_path) != first
        (nm / name).rmdir()
        assert W.deps_fingerprint(tmp_path) == first
    (nm / ".package-lock.json").write_text('{"packages": {"left-pad": {}}}\n', encoding="utf-8")
    assert W.deps_fingerprint(tmp_path) != first
    W.rmtree(nm)
    assert W.deps_fingerprint(tmp_path) == "no node_modules"


def test_an_untouched_node_install_is_kept(gate):
    d = _node_install(gate)
    assert _run(gate, "--stage", "nd", "--arms", "S", "--tasks", "R01", "--repeats", "1", "--confirm", "1") == 0
    row = _rows(gate, "nd")[0]
    assert row["deps_changed"] is False and row["passed"]
    diff = (gate["results"] / "raw" / row["run_id"] / "diff.patch").read_text(encoding="utf-8")
    assert "answer.txt" in diff and "node_modules" not in diff     # the link is left out of the run's diff
    assert (d / ".installed").is_file() and (d / "node_modules" / "vitest" / "package.json").is_file()


@pytest.mark.parametrize("write", ["new top-level entry", "lock changed"])
def test_a_run_that_writes_into_the_shared_install_keeps_its_result_and_the_install_is_dropped(gate, monkeypatch,
                                                                                              write):
    d = _node_install(gate)
    real = R.run_session

    def session_that_installs(cmd, prompt, cur, env, max_turns):
        m = real(cmd, prompt, cur, env, max_turns)
        nm = cur / "work" / "node_modules"          # the working copy's link to the shared install
        if write == "new top-level entry":
            (nm / ".vite").mkdir()
        else:
            (nm / ".package-lock.json").write_text('{"packages": {"left-pad": {}}}\n', encoding="utf-8")
        return m

    monkeypatch.setattr(R, "run_session", session_that_installs)
    assert _run(gate, "--stage", "nd", "--arms", "S", "--tasks", "R01", "--repeats", "1", "--confirm", "1") == 0
    row = _rows(gate, "nd")[0]
    assert row["deps_changed"] is True
    assert row["passed"] and row["harness_failure"] is None       # the agent's own doing: not a harness failure
    assert not d.exists()                                          # deps() installs it fresh before the next run
    assert (gate["results"] / "raw" / row["run_id"] / "row.json").is_file()


def test_confirm_must_match(gate):
    assert _run(gate, "--stage", "dry", "--arms", "S", "--repeats", "1", "--confirm", "5") == 2
    assert not (gate["results"] / "dry.jsonl").exists()


@pytest.mark.parametrize("with_spec", [True, False])
def test_a_run_the_runner_never_finished_is_a_logged_harness_failure(gate, with_spec):
    assert _run(gate, "--stage", "cut", "--arms", "S", "--repeats", "1", "--confirm", "2") == 0
    rows = _rows(gate, "cut")
    first = min(rows, key=lambda r: r["order"])
    # rebuild what a runner stopped in the middle of the first run leaves behind: its row gone, current/ in place
    (gate["results"] / "cut.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows if r is not first), encoding="utf-8")
    cur = gate["root"] / "current"
    (cur / "logs").mkdir(parents=True)
    shutil.copyfile(gate["results"] / "raw" / first["run_id"] / "prompt.txt", cur / "logs" / "prompt.txt")
    if with_spec:
        spec = {k: first[k] for k in ("stage", "repo", "task", "arm", "repeat", "order")}
        (cur / "logs" / "spec.json").write_text(json.dumps({**spec, "attempt": 1, "run_id": first["run_id"]}),
                                                encoding="utf-8")
    assert _run(gate, "--stage", "cut", "--arms", "S", "--repeats", "1", "--confirm", "1") == 0
    got = [r for r in _rows(gate, "cut") if (r["task"], r["arm"]) == (first["task"], first["arm"])]
    assert [(r["attempt"], r["harness_failure"]) for r in got] == [(1, R.INTERRUPTED), (2, None)]
    assert not cur.exists() and (gate["root"] / "runs" / first["run_id"] / "logs" / "prompt.txt").is_file()
    # a leftover folder whose run did finish is not logged again
    (cur / "logs").mkdir(parents=True)
    shutil.copyfile(gate["results"] / "raw" / first["run_id"] / "prompt.txt", cur / "logs" / "prompt.txt")
    assert _run(gate, "--stage", "cut", "--arms", "S", "--repeats", "1", "--confirm", "0") == 0
    assert len(_rows(gate, "cut")) == 3


def test_the_run_stops_at_the_owners_weekly_limit(gate, monkeypatch):
    monkeypatch.setenv("FAKE_SEVEN_DAY", "0.35")
    assert _run(gate, "--stage", "lim", "--arms", "S", "--repeats", "1", "--confirm", "2", "--stop-weekly", "0.35") == 3
    rows = _rows(gate, "lim")
    assert len(rows) == 1 and rows[0]["rate_limit"]["seven_day"] == 0.35      # stopped after the first run
    monkeypatch.setenv("FAKE_SEVEN_DAY", "0.20")                             # below the limit: the rest runs
    assert _run(gate, "--stage", "lim", "--arms", "S", "--repeats", "1", "--confirm", "1", "--stop-weekly", "0.35") == 0
    assert len(_rows(gate, "lim")) == 2


def test_preregistered_run_counts(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    for repo in ("one", "two"):
        for i in range(8):
            (tasks / repo / f"H{i:02d}").mkdir(parents=True)
            (tasks / repo / f"H{i:02d}" / "task.json").write_text(json.dumps({"id": f"H{i:02d}", "kind": "history"}))
        for i in range(4):
            (tasks / repo / f"R{i:02d}").mkdir(parents=True)
            (tasks / repo / f"R{i:02d}" / "task.json").write_text(json.dumps({"id": f"R{i:02d}", "kind": "ordinary"}))
    monkeypatch.setattr(W, "TASKS", tasks)
    counts = {s: len(R.plan(s, ["one", "two"], 2)) for s in ("A", "B", "C", "retry", "extension")}
    assert counts == {"A": 64, "B": 64, "C": 48, "retry": 48, "extension": 96}
    assert len(R.plan("extension", ["one", "two"], 1)) == 48          # the third repeat alone
    a1, a2 = R.plan("A", ["one", "two"], 2), R.plan("A", ["one", "two"], 2)
    assert a1 == a2 and [r["arm"] for r in a1[:8]] != ["S"] * 8      # seeded, interleaved


@pytest.mark.parametrize("m, why", [
    ({"has_result": True, "is_error": False, "subtype": "success"}, None),
    ({"has_result": True, "is_error": True, "subtype": "error_max_turns"}, None),
    ({"timed_out": True, "has_result": False}, None),
    ({"has_result": False, "exit_code": 1}, "no result event"),
    ({"has_result": True, "is_error": True, "subtype": "success", "result_text": "API Error: 529"}, "error result"),
    ({"interrupted": True}, "interrupted"),
])
def test_harness_failure_rules(m, why):
    got = R.harness_failure(m)
    assert (got is None) if why is None else (got or "").startswith(why)


def test_freeze_detects_any_change(tmp_path, monkeypatch):
    gate = tmp_path / "gate"
    (gate / "tasks" / "x").mkdir(parents=True)
    f = gate / "tasks" / "x" / "task.json"
    f.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(F, "GATE", gate)
    monkeypatch.setattr(F, "MANIFEST", gate / "tasks" / "MANIFEST.sha256")
    assert F.check()[0] is False
    assert F.main(["--write"]) == 0 and F.check() == (True, [])
    assert "MANIFEST.sha256" not in F.MANIFEST.read_text(encoding="utf-8")   # not listed in itself
    f.write_bytes(b"{}\r\n")                              # line endings alone do not count
    assert F.check()[0] is True
    f.write_text("{\"changed\": 1}\n", encoding="utf-8")
    assert F.check() == (False, ["CHANGED tasks/x/task.json"])
    os.remove(f)
    assert "MISSING tasks/x/task.json" in F.check()[1]
