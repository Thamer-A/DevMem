"""The gate experiment's runner (evals/gate/PREREGISTRATION.md, TASK-FORMAT.md). One process at a time.

    python -m evals.gate.run plan   --stage A                          # what would run, and how many sessions
    python -m evals.gate.run run    --stage A --confirm 64             # run them (each a real `claude -p` session)
    python -m evals.gate.run replay --repo md2conf --family S --confirm 6
    python -m evals.gate.run run    --stage dry --arms S,D --tasks H01,R01 --repeats 1 --fake --unfrozen --confirm 4

Every run happens in `<root>/current/` (a fixed path, so Claude Code's automatic memory, which is kept per project
path, finds what the replay left for that path): `work/` (a fresh working copy of the starting code), `config/`
(CLAUDE_CONFIG_DIR, a copy of the arm's replayed configuration folder), `home/` (HOME and USERPROFILE), `tmp/`
(TEMP, npm and pip caches), `userdata/` (DEVMEM_USER_DATA, DevMem arms) and `logs/`. Afterwards the folder is moved
to `<root>/runs/<run id>/` and its logs are copied to `<results>/raw/<run id>/`; one JSON line per run goes to
`<results>/<stage>.jsonl`. `ANTHROPIC_API_KEY` is never passed to a session (runs use the subscription); a
`CLAUDE_CODE_OAUTH_TOKEN` in the environment is, or else the token in the file `<root>/oauth-token`.

A run whose key (stage, repo, task, arm, repeat) already has a line is skipped, so an interrupted stage continues
where it stopped; a harness failure is run once more (`attempt` 2), and a second one stays a harness failure.

For a Node repository the working copy's `node_modules` is a link to one shared install. Its fingerprint
(workspace.deps_fingerprint) is taken before each session and after grading: a run that changed it (an agent's
`npm install`, a cache written through the link) keeps its result and has `"deps_changed": true` in its row, and the
install is deleted so that the next run gets a fresh one (`false` when unchanged, `null` for a python repository).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from evals.gate import workspace as W

GATE = W.GATE
REPO_ROOT = GATE.parent.parent
PLUGIN = REPO_ROOT / "plugin"
RESULTS = GATE / "results"
RUNNER = json.loads((GATE / "runner.json").read_text(encoding="utf-8"))

ARMS = {
    "S": {"family": "S", "pack_mode": None, "oracle": False},
    "O": {"family": "S", "pack_mode": None, "oracle": True},
    "D": {"family": "D", "pack_mode": "requests", "oracle": False},
    "DF": {"family": "D", "pack_mode": "full", "oracle": False},
}
# stage -> [(arm, task kind)], as pre-registered (section 4 and 9)
STAGES = {
    "A": [("S", "history"), ("O", "history")],
    "B": [("D", "history"), ("D", "ordinary"), ("S", "ordinary")],
    "C": [("DF", "history"), ("DF", "ordinary")],
    "retry": [("D", "history"), ("D", "ordinary")],
    "extension": [("S", "history"), ("O", "history"), ("D", "history")],
}


def _sys_path() -> None:
    if str(PLUGIN) not in sys.path:
        sys.path.insert(0, str(PLUGIN))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def claude_cmd(fake: bool, claude: str | None) -> list[str]:
    if fake:
        return [sys.executable, str(GATE / "fake_claude.py")]
    if claude:
        return [claude]
    _sys_path()
    from devmem.claude_cli import claude_executable
    return [claude_executable()]


def claude_version(cmd: list[str]) -> str:
    p = subprocess.run([*cmd, "--version"], capture_output=True, text=True, timeout=60)
    return (p.stdout.strip().split() or ["?"])[0]


def repos_present() -> list[str]:
    return sorted(p.parent.name for p in W.TASKS.glob("*/repo.json"))


def plan(stage: str, repos: list[str], repeats: int, arms: list[str] | None = None,
         kinds: list[str] | None = None, tasks: list[str] | None = None, seed: int | None = None) -> list[dict]:
    """Every run of a stage, in the pre-registered order (a seeded shuffle that interleaves arms)."""
    pairs = STAGES.get(stage) or [(a, k) for a in (arms or ["S"]) for k in (kinds or ["history", "ordinary"])]
    if arms:
        pairs = [(a, k) for a, k in pairs if a in arms] if stage in STAGES else pairs
    runs = []
    for repo in repos:
        for d in W.task_dirs(repo):
            t = W.load_task(d)
            if tasks and t["id"] not in tasks:
                continue
            for arm, kind in pairs:
                if t["kind"] != kind:
                    continue
                for rep in range(1, repeats + 1):
                    runs.append({"stage": stage, "repo": repo, "task": t["id"], "arm": arm, "repeat": rep})
    rng = random.Random(RUNNER["seed"] if seed is None else seed)
    rng.shuffle(runs)
    for i, r in enumerate(runs):
        r["order"] = i
    return runs


def run_key(r: dict) -> tuple:
    return (r["stage"], r["repo"], r["task"], r["arm"], r["repeat"])


def _copytree(src: Path, dest: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True, symlinks=True)


TOKEN_FILE = "oauth-token"


def oauth_token(root: Path) -> str | None:
    """The sign-in token for the runs (`claude setup-token`): CLAUDE_CODE_OAUTH_TOKEN, or else the file
    `<root>/oauth-token`, which the owner writes in their own terminal so the token never passes through a session
    or a log. It sits beside `current/` and `runs/`, never inside a folder that is archived or copied to results."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    f = Path(root) / TOKEN_FILE
    try:
        text = f.read_text(encoding="utf-8-sig").strip() if f.is_file() else ""
    except OSError:
        text = ""
    return text or None


def session_env(cfg: dict, deps_dir: Path, cur: Path, devmem: bool) -> dict:
    env = W.tool_env(cfg, deps_dir, cur / "work")
    for k in list(env):
        if k.upper().startswith("ANTHROPIC_"):     # never an API key: runs go through the subscription
            del env[k]
    token = oauth_token(cur.parent)
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    tmp = cur / "tmp"
    # automatic memory on in every arm (PREREGISTRATION.md section 3): "0" forces it on whatever a setting says
    # (https://code.claude.com/docs/en/env-vars.md, read 2026-11-26)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "0"
    # the pinned version must not update itself in the background partway through a stage
    env["DISABLE_AUTOUPDATER"] = "1"
    env.update(CLAUDE_CONFIG_DIR=str(cur / "config"), HOME=str(cur / "home"), USERPROFILE=str(cur / "home"),
               TEMP=str(tmp), TMP=str(tmp), TMPDIR=str(tmp), npm_config_cache=str(tmp / "npm-cache"),
               PIP_CACHE_DIR=str(tmp / "pip-cache"), XDG_CACHE_HOME=str(tmp / "xdg-cache"))
    if devmem:
        env["DEVMEM_USER_DATA"] = str(cur / "userdata")
    return env


def prepare_current(root: Path) -> Path:
    cur = root / "current"
    W.rmtree(cur)
    for sub in ("config", "home", "tmp", "userdata", "logs"):
        (cur / sub).mkdir(parents=True)
    return cur


def install_devmem(cur: Path, env: dict, pack_mode: str) -> None:
    """DevMem's hooks in the working copy (`devmem init`, from this repository's plugin/ folder), and its pack mode."""
    e = dict(env, PYTHONPATH=str(PLUGIN))
    p = subprocess.run([sys.executable, "-m", "devmem", "init", "--project", str(cur / "work"), "--no-scan"],
                       env=e, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if p.returncode != 0:
        raise RuntimeError(f"devmem init failed: {(p.stderr or p.stdout)[-400:]}")
    store = cur / "work" / ".devmemory"
    cfg_path = store / "config.json"
    c = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
    c["pack_mode"] = pack_mode
    cfg_path.write_text(json.dumps(c, indent=1), encoding="utf-8")


def restore_state(state: Path, cur: Path, family: str) -> bool:
    """The arm's replayed state into the current run folder; False when there is none (a fresh configuration)."""
    if not (state / "config").is_dir():
        return False
    _copytree(state / "config", cur / "config")
    if family == "D":
        _copytree(state / "userdata", cur / "userdata")
        _copytree(state / "store", cur / "work" / ".devmemory")
    return True


def save_state(state: Path, cur: Path, family: str) -> None:
    tmp = state.with_name(state.name + ".new")
    W.rmtree(tmp)
    tmp.mkdir(parents=True)
    _copytree(cur / "config", tmp / "config")
    if family == "D":
        _copytree(cur / "userdata", tmp / "userdata")
        _copytree(cur / "work" / ".devmemory", tmp / "store")
    W.rmtree(state)
    os.replace(tmp, state)


def run_session(cmd: list[str], prompt: str, cur: Path, env: dict, max_turns: int) -> dict:
    """One headless session in cur/work; the raw stream in cur/logs/stream.jsonl. Metrics from the documented
    stream-json output only."""
    _sys_path()
    from devmem import proc
    from devmem.claude_cli import parse_stream
    argv = [*cmd, "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", RUNNER["model"],
            "--max-turns", str(max_turns), "--permission-mode", RUNNER["permission_mode"],
            "--allowedTools", *RUNNER["allowed_tools"]]
    out = cur / "logs" / "stream.jsonl"
    t0 = time.time()
    r = proc.run_to_file(argv, out, cwd=str(cur / "work"), stdin=subprocess.DEVNULL,
                         timeout=RUNNER["session_timeout_s"], env=env)
    m = parse_stream(out, cur / "work")
    m.update(exit_code=r.returncode, timed_out=bool(r.timed_out), interrupted=bool(r.interrupted),
             wall_s=round(time.time() - t0, 2))
    if r.returncode != 0:
        (cur / "logs" / "stderr.txt").write_text((r.stderr or "")[-20000:], encoding="utf-8")
    m.update(_usage_split(out))
    return m


def _usage_split(stream: Path) -> dict:
    """Cache writes split by lifetime, from the result event's usage.cache_creation (None when not reported)."""
    five = one = None
    have_result = False
    for line in stream.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") == "result":
            have_result = True
            cc = (e.get("usage") or {}).get("cache_creation") or {}
            if isinstance(cc, dict):
                five, one = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
    return {"cache_write_5m": five, "cache_write_1h": one, "has_result": have_result}


def price_weighted(m: dict) -> float | None:
    """The pre-registered total in units of the model's uncached input price (PREREGISTRATION.md section 5)."""
    u, r, o, cw = m.get("input_tokens"), m.get("cache_read_tokens"), m.get("output_tokens"), m.get("cache_creation_tokens")
    if not all(isinstance(x, (int, float)) for x in (u, r, o, cw)):
        return None
    five, one = m.get("cache_write_5m"), m.get("cache_write_1h")
    writes = 1.25 * five + 2 * one if isinstance(five, int) and isinstance(one, int) else 1.25 * cw
    return round(u + writes + 0.1 * r + 5 * o, 1)


def harness_failure(m: dict) -> str | None:
    """Why a run did not test its arm (PREREGISTRATION.md section 10), or None. A timeout, the turn limit or a wrong
    answer is a fail for the arm, not a harness failure."""
    if m.get("interrupted"):
        return "interrupted"
    if m.get("timed_out"):
        return None
    if not m.get("has_result"):
        return f"no result event (exit {m.get('exit_code')})"
    if m.get("is_error") and m.get("subtype") != "error_max_turns":
        return f"error result: {m.get('subtype')}: {str(m.get('result_text') or '')[:200]}"
    return None


def diff_against_start(work: Path) -> str:
    # node_modules is left out by .git/info/exclude (workspace.base); naming it in an exclude pathspec as well makes
    # `git add` fail ("paths are ignored", exit 1, git 2.51) whenever the link is there, as in every Node run.
    W.git(work, "add", "-A", "--", ".", ":(exclude).claude", ":(exclude).devmemory")
    return W.git(work, "diff", "--cached", "HEAD")


def pack_summary(work: Path) -> dict:
    log = work / ".devmemory" / "packs.jsonl"
    if not log.is_file():
        return {}
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
    return {"pack_lines": len(lines), "pack_served": sum(1 for e in lines if e.get("keys")),
            "pack_tokens_est": sum(e.get("tokens_est") or 0 for e in lines),
            "pack_keys": [k for e in lines for k in (e.get("keys") or [])],
            "pack_empty_reasons": [e.get("empty_reason") for e in lines if not e.get("keys")]}


def deps_before(cfg: dict, deps_dir: Path) -> str | None:
    """The shared Node install's fingerprint before a session (None for a python repository)."""
    return W.deps_fingerprint(deps_dir) if cfg["deps"]["kind"] == "node" else None


def deps_changed(deps_dir: Path, before: str | None) -> bool | None:
    """Whether the run wrote into the shared Node install through its `node_modules` link (an `npm install`, a
    cache). The run keeps its own result (the agent did it in its own session, PREREGISTRATION.md section 10); the
    caller then deletes the install (workspace.rmtree, links inside removed as links), so that deps() installs it
    fresh before the next run and no later run or arm uses what this one wrote."""
    return None if before is None else W.deps_fingerprint(deps_dir) != before


def one_run(spec: dict, root: Path, results: Path, cmd: list[str], version: str, frozen: bool, fake: bool,
            attempt: int, keep_work: bool) -> dict:
    cfg = W.load_repo(spec["repo"])
    task = W.load_task(W.TASKS / spec["repo"] / spec["task"])
    arm = ARMS[spec["arm"]]
    run_id = f"{spec['stage']}_{spec['repo']}_{spec['task']}_{spec['arm']}_r{spec['repeat']}_a{attempt}"
    deps_dir = W.deps(cfg, root)
    cur = prepare_current(root)
    # which run this folder holds, so that a run the runner never finished is found and logged (recover_interrupted)
    (cur / "logs" / "spec.json").write_text(json.dumps({**spec, "attempt": attempt, "run_id": run_id}),
                                            encoding="utf-8")
    W.work_copy(cfg, root, cur / "work")
    env = session_env(cfg, deps_dir, cur, devmem=arm["pack_mode"] is not None)
    replayed = restore_state(root / "replay" / spec["repo"] / arm["family"], cur, arm["family"])
    if arm["pack_mode"]:
        install_devmem(cur, env, arm["pack_mode"])
    prompt = RUNNER["oracle_wrapper"].format(oracle=task["oracle"], prompt=task["prompt"]) if arm["oracle"] \
        else task["prompt"]
    (cur / "logs" / "prompt.txt").write_text(prompt, encoding="utf-8")
    fingerprint = deps_before(cfg, deps_dir)
    m = run_session(cmd, prompt, cur, env, int(task.get("max_turns", 30)))
    diff = diff_against_start(cur / "work")
    (cur / "logs" / "diff.patch").write_text(diff, encoding="utf-8", newline="\n")
    g = W.grade(task, cur / "work", W.tool_env(cfg, deps_dir, cur / "work"))
    (cur / "logs" / "grade.json").write_text(json.dumps(g, indent=1), encoding="utf-8")
    changed_deps = deps_changed(deps_dir, fingerprint)
    packs = pack_summary(cur / "work")
    if (cur / "work" / ".devmemory" / "packs.jsonl").is_file():
        shutil.copyfile(cur / "work" / ".devmemory" / "packs.jsonl", cur / "logs" / "packs.jsonl")
    hf = harness_failure(m)
    if hf is None and g.get("grader_timeout"):      # the working copy could not be graded (section 10)
        hf = "grader timed out"
    row = {**{k: spec[k] for k in ("stage", "repo", "task", "arm", "repeat", "order")}, "attempt": attempt,
           "run_id": run_id, "ts": now(), "kind": task["kind"], "passed": bool(g.get("passed")) and not hf,
           "checks": g.get("checks"), "grader_error": g.get("error"), "harness_failure": hf,
           "uncached_input_tokens": m.get("input_tokens"), "cache_write_tokens": m.get("cache_creation_tokens"),
           "cache_write_5m": m.get("cache_write_5m"), "cache_write_1h": m.get("cache_write_1h"),
           "cache_read_tokens": m.get("cache_read_tokens"), "output_tokens": m.get("output_tokens"),
           "price_weighted": price_weighted(m), "turns": m.get("num_turns"), "wall_s": m.get("wall_s"),
           "duration_ms": m.get("duration_ms"), "exit_code": m.get("exit_code"), "timed_out": m.get("timed_out"),
           "subtype": m.get("subtype"), "model_requested": RUNNER["model"], "model": m.get("model"),
           "claude_version": version, "cost_usd_list": m.get("cost_usd_list"), "rate_limit": m.get("rate_limit"),
           "tool_calls": m.get("tool_calls"), "diff_lines": diff.count("\n"), "replayed_state": replayed,
           "deps_changed": changed_deps, "frozen": frozen, "fake": fake,
           "devmem_rev": W.git(REPO_ROOT, "rev-parse", "--short", "HEAD").strip(), **packs}
    (cur / "logs" / "row.json").write_text(json.dumps(row, indent=1), encoding="utf-8")
    raw = results / "raw" / run_id
    W.rmtree(raw)
    shutil.copytree(cur / "logs", raw)
    if not keep_work:
        W.rmtree(cur / "work")
    dest = root / "runs" / run_id
    W.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(cur, dest)
    if changed_deps:            # after the working copy's link to it is gone; deps() reinstalls it for the next run
        W.rmtree(deps_dir)
    return row


def _done(path: Path) -> dict[tuple, dict]:
    done: dict[tuple, dict] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                k = run_key(r)
                if k not in done or done[k].get("harness_failure"):
                    done[k] = r
    return done


INTERRUPTED = "interrupted: the runner stopped before the session ended"


def recover_interrupted(root: Path, results: Path, out: Path, runs: list[dict], done: dict) -> dict | None:
    """A run of this stage that the runner started but never finished (the runner was stopped: a crash, a machine
    out of memory, a suspend) is a harness failure (PREREGISTRATION.md section 10). Log it as one, keep its logs,
    and delete a shared Node install it may have written into, which no fingerprint check saw. Returns the row, or
    None when `<root>/current` holds no unfinished run of this stage.

    The folder names its run in logs/spec.json; a folder from before that file existed is matched to the first run
    not yet done in the stage's order, and only when its prompt is that run's prompt."""
    cur = root / "current"
    logs = cur / "logs"
    spec = None
    if (logs / "spec.json").is_file():
        spec = json.loads((logs / "spec.json").read_text(encoding="utf-8"))
    elif (logs / "prompt.txt").is_file():
        nxt = next((r for r in runs if run_key(r) not in done), None)
        if nxt is not None:
            task = W.load_task(W.TASKS / nxt["repo"] / nxt["task"])
            want = RUNNER["oracle_wrapper"].format(oracle=task["oracle"], prompt=task["prompt"]) \
                if ARMS[nxt["arm"]]["oracle"] else task["prompt"]
            if (logs / "prompt.txt").read_text(encoding="utf-8") == want:
                spec = {**nxt, "attempt": 1,
                        "run_id": f"{nxt['stage']}_{nxt['repo']}_{nxt['task']}_{nxt['arm']}_r{nxt['repeat']}_a1"}
    if spec is None or not runs or spec.get("stage") != runs[0]["stage"]:
        return None
    prev = done.get(run_key(spec))
    if prev is not None and prev.get("attempt", 1) >= spec["attempt"]:
        return None                       # that run finished; the folder is only a leftover
    task = W.load_task(W.TASKS / spec["repo"] / spec["task"])
    row = {**{k: spec.get(k) for k in ("stage", "repo", "task", "arm", "repeat", "order")},
           "attempt": spec["attempt"], "run_id": spec["run_id"], "ts": now(), "kind": task["kind"], "passed": False,
           "harness_failure": INTERRUPTED, "recovered": True, "model_requested": RUNNER["model"]}
    raw = results / "raw" / spec["run_id"]
    W.rmtree(raw)
    shutil.copytree(logs, raw)
    dest = root / "runs" / spec["run_id"]
    W.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(cur, dest)
    cfg = W.load_repo(spec["repo"])
    if cfg["deps"]["kind"] == "node":
        W.rmtree(W.deps_path(cfg, root))  # deps() installs it fresh before the next run
    with open(out, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    done[run_key(spec)] = row
    return row


def cmd_run(a) -> int:
    from evals.gate import verify_freeze
    frozen, msgs = verify_freeze.check()
    if not frozen and not a.unfrozen:
        print("the task set is not frozen as recorded (python -m evals.gate.verify_freeze):", *msgs[:10], sep="\n  ")
        return 2
    cmd = claude_cmd(a.fake, a.claude)
    version = claude_version(cmd)
    if version != RUNNER["claude_version"] and not a.any_version:
        print(f"claude is {version}, runner.json pins {RUNNER['claude_version']}: refusing (--any-version to override)")
        return 2
    runs = plan(a.stage, a.repo or repos_present(), a.repeats, a.arms, a.kinds, a.tasks)
    results = Path(a.results)
    out = results / f"{a.stage}.jsonl"
    done = _done(out)
    results.mkdir(parents=True, exist_ok=True)
    lost = recover_interrupted(Path(a.root), results, out, runs, done)
    if lost:
        print(f"logged {lost['run_id']} as a harness failure ({INTERRUPTED}); it is run once more")
    todo = []
    for r in runs:
        prev = done.get(run_key(r))
        if prev is None:
            todo.append((r, 1))
        elif prev.get("harness_failure") and prev.get("attempt", 1) < 2:
            todo.append((r, 2))
    print(f"stage {a.stage}: {len(runs)} runs planned, {len(todo)} to do ({'fake' if a.fake else 'real'} claude "
          f"{version}, model {RUNNER['model']})")
    if a.confirm != len(todo):
        print(f"--confirm {len(todo)} is needed to start them (each one is a {'fake' if a.fake else 'real'} session)")
        return 2
    results.mkdir(parents=True, exist_ok=True)
    root = Path(a.root)
    for i, (r, attempt) in enumerate(todo, 1):
        row = one_run(r, root, results, cmd, version, frozen, a.fake, attempt, a.keep_work)
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        print(f"[{i}/{len(todo)}] {row['repo']} {row['task']} {row['arm']} r{row['repeat']} "
              f"{'PASS' if row['passed'] else 'fail'}{' HARNESS: ' + row['harness_failure'] if row['harness_failure'] else ''}"
              f"{' DEPS CHANGED (reinstalled next run)' if row['deps_changed'] else ''}"
              f" turns={row['turns']} weighted={row['price_weighted']} {row['wall_s']}s", flush=True)
        weekly = (row.get("rate_limit") or {}).get("seven_day")
        if a.stop_weekly is not None and isinstance(weekly, (int, float)) and weekly >= a.stop_weekly:
            # the owner's usage limit (PREREGISTRATION.md section 4): stop and ask; the next call resumes here
            print(f"STOPPED: the subscription's seven-day use is {weekly:.0%}, at or above --stop-weekly "
                  f"{a.stop_weekly:.0%}; {len(todo) - i} run(s) not started", flush=True)
            return 3
    return 0


def cmd_plan(a) -> int:
    runs = plan(a.stage, a.repo or repos_present(), a.repeats, a.arms, a.kinds, a.tasks)
    by: dict[tuple, int] = {}
    for r in runs:
        by[(r["repo"], r["arm"])] = by.get((r["repo"], r["arm"]), 0) + 1
    for (repo, arm), n in sorted(by.items()):
        print(f"  {repo:<12} {arm:<3} {n} runs")
    print(f"stage {a.stage}: {len(runs)} sessions")
    return 0


def cmd_replay(a) -> int:
    """Run the history's prompts as real sessions, one after another, in the fixed path, and keep the resulting
    configuration folder (and DevMem store) as the family's replayed state. The code they write is discarded: every
    session starts from the pinned tree plus the reference patches of the sessions before it."""
    cfg = W.load_repo(a.repo)
    sessions = W.load_history(a.repo)
    root = Path(a.root)
    state = root / "replay" / a.repo / a.family
    if state.exists() and not a.again:
        print(f"{state} exists: this family was replayed already (--again replaces it)")
        return 2
    cmd = claude_cmd(a.fake, a.claude)
    version = claude_version(cmd)
    if version != RUNNER["claude_version"] and not a.any_version:
        print(f"claude is {version}, runner.json pins {RUNNER['claude_version']}: refusing")
        return 2
    if a.confirm != len(sessions):
        print(f"--confirm {len(sessions)} is needed: {len(sessions)} {'fake' if a.fake else 'real'} sessions")
        return 2
    W.rmtree(state)
    results = Path(a.results)
    results.mkdir(parents=True, exist_ok=True)
    deps_dir = W.deps(cfg, root)
    base = W.base(cfg, root)
    steps = W.git(base, "rev-list", "--reverse", "HEAD").split()     # pinned, then one commit per session
    for s in sessions:
        cur = prepare_current(root)
        W.work_copy(cfg, root, cur / "work")
        W.git(cur / "work", "reset", "-q", "--hard", steps[s["n"] - 1])
        env = session_env(cfg, deps_dir, cur, devmem=a.family == "D")
        restore_state(state, cur, a.family)
        if a.family == "D":
            install_devmem(cur, env, "requests")
        fingerprint = deps_before(cfg, deps_dir)
        m = run_session(cmd, s["prompt"], cur, env, 40)
        changed_deps = deps_changed(deps_dir, fingerprint)
        save_state(state, cur, a.family)
        row = {"stage": "replay", "repo": a.repo, "family": a.family, "session": s["n"], "ts": now(),
               "harness_failure": harness_failure(m), "exit_code": m.get("exit_code"), "turns": m.get("num_turns"),
               "uncached_input_tokens": m.get("input_tokens"), "cache_write_tokens": m.get("cache_creation_tokens"),
               "cache_read_tokens": m.get("cache_read_tokens"), "output_tokens": m.get("output_tokens"),
               "price_weighted": price_weighted(m), "wall_s": m.get("wall_s"), "model": m.get("model"),
               "claude_version": version, "rate_limit": m.get("rate_limit"), "deps_changed": changed_deps,
               "fake": a.fake, **pack_summary(cur / "work")}
        run_id = f"replay_{a.repo}_{a.family}_s{s['n']}"
        raw = results / "raw" / run_id
        W.rmtree(raw)
        shutil.copytree(cur / "logs", raw)
        with open(results / "replay.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        W.rmtree(cur / "work")
        W.rmtree(root / "runs" / run_id)
        (root / "runs").mkdir(parents=True, exist_ok=True)
        os.replace(cur, root / "runs" / run_id)
        if changed_deps:        # the next session's work_copy() installs it fresh
            W.rmtree(deps_dir)
        print(f"replay {a.repo} {a.family} session {s['n']}: exit {row['exit_code']} turns={row['turns']}"
              f"{' HARNESS: ' + row['harness_failure'] if row['harness_failure'] else ''}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.gate.run", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run", "replay"):
        p = sub.add_parser(name)
        p.add_argument("--root", default=str(W.DEFAULT_ROOT))
        p.add_argument("--results", default=str(RESULTS))
        p.add_argument("--fake", action="store_true", help="use evals/gate/fake_claude.py instead of claude")
        p.add_argument("--claude", help="path of the claude program (default: the one on PATH)")
        p.add_argument("--any-version", action="store_true", help="do not refuse a claude other than runner.json's")
        p.add_argument("--confirm", type=int, default=-1, help="the number of sessions this starts (required)")
        if name == "replay":
            p.add_argument("--repo", required=True)
            p.add_argument("--family", required=True, choices=["S", "D"])
            p.add_argument("--again", action="store_true")
        else:
            p.add_argument("--stage", required=True, help="A, B, C, retry, extension, or another name with --arms")
            p.add_argument("--repo", action="append")
            p.add_argument("--repeats", type=int, default=2)
            p.add_argument("--arms", type=lambda s: s.split(","))
            p.add_argument("--kinds", type=lambda s: s.split(","))
            p.add_argument("--tasks", type=lambda s: s.split(","))
            p.add_argument("--unfrozen", action="store_true", help="allow a task set that is not frozen (rows say so)")
            p.add_argument("--keep-work", action="store_true")
            p.add_argument("--stop-weekly", type=float, default=None,
                           help="stop after a run whose rate_limit event reports this seven-day use or more (0.35)")
    a = ap.parse_args(argv)
    return {"plan": cmd_plan, "run": cmd_run, "replay": cmd_replay}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
