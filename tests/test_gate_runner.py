"""The gate experiment's runner (evals/gate/run.py) with the stand-in claude (evals/gate/fake_claude.py): no model, no
network. A tiny local repository stands in for the real ones. Checks the pre-registered run counts, that a dry run
of 2 tasks x 2 arms writes complete lines, that a run writes nothing outside its own folder, the oracle arm, the
DevMem arm's pack log, the harness-failure rules and the freeze."""
from __future__ import annotations

import json
import os
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


def test_confirm_must_match(gate):
    assert _run(gate, "--stage", "dry", "--arms", "S", "--repeats", "1", "--confirm", "5") == 2
    assert not (gate["results"] / "dry.jsonl").exists()


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
    monkeypatch.setattr(F, "MANIFEST", gate / "MANIFEST.sha256")
    assert F.check()[0] is False
    assert F.main(["--write"]) == 0 and F.check() == (True, [])
    f.write_bytes(b"{}\r\n")                              # line endings alone do not count
    assert F.check()[0] is True
    f.write_text("{\"changed\": 1}\n", encoding="utf-8")
    assert F.check() == (False, ["CHANGED tasks/x/task.json"])
    os.remove(f)
    assert "MISSING tasks/x/task.json" in F.check()[1]
