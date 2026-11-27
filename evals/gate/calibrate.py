"""Calibration sessions for the gate experiment (PLAN.md section 4, Task 2; evals/gate/BASELINE-FACTS.md).

    python -m evals.gate.calibrate --fake --confirm 6            # every step with fake_claude (no model, no network)
    python -m evals.gate.calibrate --confirm 6                   # six real `claude -p` sessions (the owner's yes first)
    python -m evals.gate.calibrate --only a,b --confirm 2        # a subset

Each session runs with the runner's own isolation (evals/gate/run.py: session_env, run_session, install_devmem,
save_state/restore_state): CLAUDE_CONFIG_DIR, HOME, USERPROFILE and TEMP inside the run folder, no ANTHROPIC_*
variable, the sign-in token from run.oauth_token (`CLAUDE_CODE_OAUTH_TOKEN`, or the file `<root>/oauth-token`), the
pinned Claude Code version, model and headless flags of runner.json. The project is a small scratch repository made
fresh for every session at one fixed path, `<root>/calib/current/work` (automatic memory is kept per project path),
with a command hook in its `.claude/settings.json` that appends each UserPromptSubmit and PostToolUse event's name to
`logs/hook-events.txt`. A session that continues an earlier one gets that session's saved configuration folder (and
DevMem store) copied in, as the runner restores an arm's replayed state.

Everything is written under `<root>/calib/`: `current/` while a session runs, `runs/<id>/` afterwards, `state/<name>/`
the saved configurations, `results/<id>/` the stream, prompt, diff, hook lines, memory folder copy and pack log of each
session, and `results/summary.json`. Nothing goes into this repository.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from evals.gate import run as R
from evals.gate import workspace as W

RULE_PROMPT = ("From now on, every log file name in this project ends in .trail.log; please add a tiny write_log(name) "
               "helper in logs.py.")
NEED_PROMPT = "Add a helper that opens the audit log for today."
FUTURE_PROMPT = ("Please add a small greet(name) function to app.py that returns 'hello, <name>'. When we add an audit "
                 "log later, its file name must end in .trail.log.")
REMEMBER_PROMPT = ("Remember for future sessions in this project: every log file name ends in .trail.log. No code "
                   "change is needed now.")
MARKER = ".trail.log"
MAX_TURNS = 10

# One entry per planned session, run in this order. config: "new" (an empty configuration folder) or the name of a
# state an earlier session saved; save: the state name this session's configuration (and store) is kept under.
SESSIONS = [
    {"id": "a", "what": "rule stated, memory on: is a memory written, do hooks fire, does sign-in work",
     "prompt": RULE_PROMPT, "config": "new", "save": "mem", "memory": True, "devmem": False},
    {"id": "b", "what": "same configuration as a: does the rule come back without being stated",
     "prompt": NEED_PROMPT, "config": "mem", "save": None, "memory": True, "devmem": False},
    {"id": "c", "what": "fresh configuration, same path: nothing carried over from a",
     "prompt": NEED_PROMPT, "config": "new", "save": None, "memory": True, "devmem": False},
    {"id": "d", "what": "fresh configuration, CLAUDE_CODE_DISABLE_AUTO_MEMORY=1: no memory folder",
     "prompt": RULE_PROMPT, "config": "new", "save": None, "memory": False, "devmem": False},
    {"id": "e", "what": "DevMem installed as in arm D: the session is recorded and a pack line written",
     "prompt": RULE_PROMPT, "config": "new", "save": "devmem", "memory": True, "devmem": True},
    {"id": "f", "what": "DevMem state of e: does the pack carry e's request",
     "prompt": NEED_PROMPT, "config": "devmem", "save": None, "memory": True, "devmem": True},
    # Added after a-f, where the rule was implemented at once and no memory was saved: the history's requirements
    # are for later work, which the code does not show, so g states one that way.
    {"id": "g", "what": "a rule for later work, phrased like the history: is a memory written now",
     "prompt": FUTURE_PROMPT, "config": "new", "save": "mem2", "memory": True, "devmem": False},
    {"id": "h", "what": "same configuration as g: does that rule come back without being stated",
     "prompt": NEED_PROMPT, "config": "mem2", "save": None, "memory": True, "devmem": False},
    {"id": "i", "what": "an explicit request to remember: can a headless session save a memory at all",
     "prompt": REMEMBER_PROMPT, "config": "new", "save": None, "memory": True, "devmem": False},
]

# The scratch session's tools: none of a repository's. session_env takes a repository config; a "node" one only puts
# the (absent) work/node_modules/.bin first on PATH and sets no VIRTUAL_ENV.
CALIB_REPO = {"name": "calib", "deps": {"kind": "node"}}

HOOK_CODE = ("import sys,json;e=json.loads(sys.stdin.buffer.read() or b'{}').get('hook_event_name','?');"
             "open(sys.argv[1],'a',encoding='utf-8').write(e+chr(10))")

AUTH_WORDS = re.compile(r"(?i)\b(log ?in|logged in|auth\w*|oauth|credential\w*|api key|401|403|unauthori[sz]ed)\b")


def calib_dir(root: Path) -> Path:
    return Path(root) / "calib"


def select(only: list[str] | None) -> list[dict]:
    if not only:
        return list(SESSIONS)
    unknown = sorted(set(only) - {s["id"] for s in SESSIONS})
    if unknown:
        raise SystemExit(f"unknown session id(s): {', '.join(unknown)} (known: {', '.join(s['id'] for s in SESSIONS)})")
    return [s for s in SESSIONS if s["id"] in only]


def missing_states(chosen: list[dict], calib: Path) -> list[str]:
    """States a chosen session needs that neither an earlier chosen session saves nor a previous call left."""
    saved, missing = set(), []
    for s in chosen:
        if s["config"] != "new" and s["config"] not in saved and not (calib / "state" / s["config"] / "config").is_dir():
            missing.append(f"{s['id']} needs state '{s['config']}'")
        if s["save"]:
            saved.add(s["save"])
    return missing


def token_source(root: Path) -> str:
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "CLAUDE_CODE_OAUTH_TOKEN from the environment"
    if R.oauth_token(root):
        return f"the token file {Path(root) / R.TOKEN_FILE}"
    return "none (a real session is then expected to fail to sign in)"


def make_project(cur: Path) -> Path:
    """The scratch project at the fixed path: a git repository with a hook in its shared settings."""
    work = cur / "work"
    work.mkdir(parents=True)
    (work / "app.py").write_text('"""A tiny scratch program."""\n\n\ndef main():\n    print("hello")\n\n\n'
                                 'if __name__ == "__main__":\n    main()\n', encoding="utf-8", newline="\n")
    (work / "README.md").write_text("# scratch\n\nA tiny project for a calibration run.\n", encoding="utf-8",
                                    newline="\n")
    hook = {"type": "command", "command": sys.executable,
            "args": ["-c", HOOK_CODE, str(cur / "logs" / "hook-events.txt")], "timeout": 30}
    settings = {"hooks": {"UserPromptSubmit": [{"hooks": [hook]}],
                          "PostToolUse": [{"matcher": "*", "hooks": [hook]}]}}
    (work / ".claude").mkdir()
    (work / ".claude" / "settings.json").write_text(json.dumps(settings, indent=1), encoding="utf-8")
    W.git(work, "init", "-q")
    W.commit_all(work, "scratch project")
    return work


def calib_env(root: Path, cur: Path, spec: dict) -> dict:
    env = R.session_env(CALIB_REPO, cur / "work", cur, devmem=spec["devmem"])
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)        # session_env looks beside cur; the token lives beside calib/
    token = R.oauth_token(root)
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    if not spec["memory"]:
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


def _events(stream: Path) -> list[dict]:
    out = []
    if stream.is_file():
        for line in stream.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
    return out


def _texts(e: dict) -> list[str]:
    """The assistant-visible text and tool inputs of one stream event."""
    out = []
    content = (e.get("message") or {}).get("content") if isinstance(e.get("message"), dict) else None
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict):
            if b.get("type") == "text" and isinstance(b.get("text"), str):
                out.append(b["text"])
            elif b.get("type") == "tool_use":
                out.append(json.dumps(b.get("input"), ensure_ascii=False))
    if e.get("type") == "result" and isinstance(e.get("result"), str):
        out.append(e["result"])
    return out


def auth_problem(events: list[dict], stderr: str) -> str | None:
    """A sign-in failure as the stream shows it: an api_retry event with an authentication category, or an error
    result (or stderr) that speaks of logging in, credentials or 401/403."""
    for e in events:
        if e.get("type") == "system" and e.get("subtype") == "api_retry" and \
                e.get("error") in ("authentication_failed", "oauth_org_not_allowed", "account_on_hold"):
            return f"api_retry: {e.get('error')} (status {e.get('error_status')})"
    for e in events:
        if e.get("type") == "result" and e.get("is_error") and AUTH_WORDS.search(str(e.get("result") or "")):
            return f"error result: {str(e.get('result'))[:200]}"
    if AUTH_WORDS.search(stderr or ""):
        return f"stderr: {stderr.strip()[-200:]}"
    return None


def memory_evidence(events: list[dict]) -> dict:
    """What the stream says about automatic memory: the init event's keys, any init field that names memory, and the
    tool calls that touched a memory path. (The memory index itself is loaded into the system prompt, which the
    stream does not show.)"""
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    fields = {k: str(v)[:300] for k, v in init.items() if "memory" in k.lower() or "memory" in str(v).lower()}
    calls = []
    for e in events:
        if e.get("type") != "assistant":
            continue
        content = (e.get("message") or {}).get("content") if isinstance(e.get("message"), dict) else None
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and "memory" in json.dumps(b.get("input")).lower():
                calls.append(f"{b.get('name')}: {json.dumps(b.get('input'), ensure_ascii=False)[:200]}")
    return {"init_keys": sorted(init), "init_memory_fields": fields, "memory_tool_calls": calls}


def memory_folders(config: Path) -> list[dict]:
    """Every `<config>/projects/<project>/memory/` folder: its project name, and each file's name, size and text."""
    out = []
    for d in sorted(config.glob("projects/*/memory")):
        files = []
        for f in sorted(p for p in d.rglob("*") if p.is_file()):
            text = f.read_text(encoding="utf-8", errors="replace")[:20000] if f.suffix.lower() == ".md" else None
            files.append({"name": f.relative_to(d).as_posix(), "bytes": f.stat().st_size, "text": text})
        out.append({"project": d.parent.name, "files": files})
    return out


def file_list(top: Path, skip: tuple[str, ...] = (), limit: int = 200) -> list[str]:
    """Names (and sizes) of the files a session left under `top`, never their content."""
    out = []
    if top.is_dir():
        for p in sorted(top.rglob("*")):
            rel = p.relative_to(top).as_posix()
            if p.is_file() and not rel.startswith(skip):
                out.append(f"{rel} ({p.stat().st_size} B)")
                if len(out) >= limit:
                    out.append("...")
                    break
    return out


def devmem_record(cur: Path, env: dict) -> dict:
    store = cur / "work" / ".devmemory"
    if not store.is_dir():
        return {"devmem_store": False}
    e = dict(env, PYTHONPATH=str(R.PLUGIN))
    p = subprocess.run([sys.executable, "-m", "devmem", "sessions"], cwd=str(cur / "work"), env=e,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    (cur / "logs" / "devmem-sessions.txt").write_text((p.stdout or "") + (p.stderr or ""), encoding="utf-8")
    if (store / "packs.jsonl").is_file():
        shutil.copyfile(store / "packs.jsonl", cur / "logs" / "packs.jsonl")
    metas = sorted(store.glob("sessions/*.meta.json"))
    return {"devmem_store": True, "devmem_sessions": len(metas), "devmem_sessions_exit": p.returncode,
            **R.pack_summary(cur / "work")}


def scrub_token(token: str | None, *tops: Path) -> list[str]:
    """Remove the sign-in token from every file under `tops` (it should never be there); the paths it was in."""
    if not token:
        return []
    raw, found = token.encode("utf-8"), []
    for top in tops:
        for p in (top.rglob("*") if top.is_dir() else []):
            if p.is_file() and not p.is_symlink():
                b = p.read_bytes()
                if raw in b:
                    p.write_bytes(b.replace(raw, b"[token removed]"))
                    found.append(str(p))
    return found


def one_session(spec: dict, root: Path, cmd: list[str], version: str, fake: bool) -> dict:
    calib = calib_dir(root)
    cur = calib / "current"
    W.rmtree(cur)
    for sub in ("config", "home", "tmp", "userdata", "logs"):
        (cur / sub).mkdir(parents=True)
    make_project(cur)
    env = calib_env(root, cur, spec)
    family = "D" if spec["devmem"] else "S"
    restored = spec["config"] != "new" and R.restore_state(calib / "state" / spec["config"], cur, family)
    if spec["devmem"]:
        R.install_devmem(cur, env, "requests")
    (cur / "logs" / "prompt.txt").write_text(spec["prompt"], encoding="utf-8")
    m = R.run_session(cmd, spec["prompt"], cur, env, MAX_TURNS)
    diff = R.diff_against_start(cur / "work")
    (cur / "logs" / "diff.patch").write_text(diff, encoding="utf-8", newline="\n")
    events = _events(cur / "logs" / "stream.jsonl")
    stderr_file = cur / "logs" / "stderr.txt"
    stderr = stderr_file.read_text(encoding="utf-8", errors="replace") if stderr_file.is_file() else ""
    hook_file = cur / "logs" / "hook-events.txt"
    hook_lines = hook_file.read_text(encoding="utf-8").splitlines() if hook_file.is_file() else []
    reply = "\n".join(t for e in events for t in _texts(e))
    mem = memory_folders(cur / "config")
    row = {"id": spec["id"], "what": spec["what"], "ts": R.now(), "fake": fake, "claude_version": version,
           "model_requested": R.RUNNER["model"], "model": m.get("model"), "config": spec["config"],
           "restored_state": bool(restored), "auto_memory_env": "1" if not spec["memory"] else "0",
           "devmem": spec["devmem"], "project_path": str(cur / "work"),
           "exit_code": m.get("exit_code"), "timed_out": m.get("timed_out"), "subtype": m.get("subtype"),
           "is_error": m.get("is_error"), "harness_failure": R.harness_failure(m),
           "auth_problem": auth_problem(events, stderr), "result_text": str(m.get("result_text") or "")[:2000],
           "turns": m.get("num_turns"), "wall_s": m.get("wall_s"), "tool_calls": m.get("tool_calls"),
           "uncached_input_tokens": m.get("input_tokens"), "cache_write_tokens": m.get("cache_creation_tokens"),
           "cache_read_tokens": m.get("cache_read_tokens"), "output_tokens": m.get("output_tokens"),
           "price_weighted": R.price_weighted(m), "rate_limit": m.get("rate_limit"),
           "hook_lines": hook_lines, "memory_folders": mem,
           "memory_files": [f["name"] for d in mem for f in d["files"]],
           "marker_in_reply": MARKER in reply, "marker_in_diff": MARKER in diff,
           **memory_evidence(events),
           "config_files": file_list(cur / "config", skip=("projects/",)),
           "config_projects": sorted(p.name for p in (cur / "config" / "projects").glob("*")),
           "home_files": file_list(cur / "home"), "tmp_files": file_list(cur / "tmp", limit=50),
           "diff_lines": diff.count("\n")}
    if spec["devmem"]:
        row.update(devmem_record(cur, env))
    if spec["save"]:
        R.save_state(calib / "state" / spec["save"], cur, family)
    results = calib / "results" / spec["id"]
    W.rmtree(results)
    shutil.copytree(cur / "logs", results)
    for d in sorted((cur / "config").glob("projects/*/memory")):
        shutil.copytree(d, results / "memory" / d.parent.name)
    dest = calib / "runs" / spec["id"]
    W.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(cur, dest)
    row["token_found_in"] = scrub_token(R.oauth_token(root), results, dest, calib / "state")
    (results / "session.json").write_text(json.dumps(row, indent=1), encoding="utf-8")
    return row


def table(rows: list[dict]) -> str:
    head = ["id", "exit", "sign-in", "memory files", "hooks", "marker", "devmem", "in/cw/cr/out tokens"]
    lines = [head]
    for r in rows:
        hooks = ",".join(sorted(set(r["hook_lines"]))) or "-"
        marker = "reply" if r["marker_in_reply"] else ""
        marker += ("+" if marker else "") + "diff" if r["marker_in_diff"] else ""
        dm = f"{r.get('devmem_sessions')} sess/{r.get('pack_lines', 0)} packs" if r["devmem"] else "-"
        toks = "/".join(str(r.get(k)) for k in ("uncached_input_tokens", "cache_write_tokens", "cache_read_tokens",
                                                 "output_tokens"))
        lines.append([r["id"], str(r["exit_code"]), "FAILED" if r["auth_problem"] else "ok",
                      ",".join(r["memory_files"]) or "none", hooks, marker or "-", dm, toks])
    widths = [max(len(x[i]) for x in lines) for i in range(len(head))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(x, widths)) for x in lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.gate.calibrate", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(W.DEFAULT_ROOT))
    ap.add_argument("--fake", action="store_true", help="use evals/gate/fake_claude.py instead of claude")
    ap.add_argument("--claude", help="path of the claude program (default: the one on PATH)")
    ap.add_argument("--any-version", action="store_true", help="do not refuse a claude other than runner.json's")
    ap.add_argument("--only", type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                    help="a subset of the session ids, e.g. a,b")
    ap.add_argument("--confirm", type=int, default=-1, help="the number of sessions this starts (required)")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    if root == R.REPO_ROOT or R.REPO_ROOT in root.parents:
        print(f"--root {root} is inside this repository: refusing (calibration writes nothing here)")
        return 2
    chosen = select(a.only)
    calib = calib_dir(root)
    kind = "fake" if a.fake else "real"
    cmd = R.claude_cmd(a.fake, a.claude)
    version = R.claude_version(cmd)
    print(f"calibration in {calib / 'current' / 'work'} ({kind} claude {version}, model {R.RUNNER['model']}, "
          f"sign-in: {token_source(root)})")
    for s in chosen:
        flags = [f"config {s['config']}"] + (["auto memory OFF"] if not s["memory"] else []) + \
                (["DevMem"] if s["devmem"] else []) + ([f"saves '{s['save']}'"] if s["save"] else [])
        print(f"  {s['id']}  {s['what']}  [{', '.join(flags)}]")
    print(f"{len(chosen)} sessions planned")
    if version != R.RUNNER["claude_version"] and not a.any_version:
        print(f"claude is {version}, runner.json pins {R.RUNNER['claude_version']}: refusing (--any-version to override)")
        return 2
    missing = missing_states(chosen, calib)
    if missing:
        print("cannot start:", *missing, sep="\n  ")
        return 2
    if a.confirm != len(chosen):
        print(f"--confirm {len(chosen)} is needed to start them (each one is a {kind} session)")
        return 2
    rows = []
    for i, s in enumerate(chosen, 1):
        row = one_session(s, root, cmd, version, a.fake)
        rows.append(row)
        print(f"[{i}/{len(chosen)}] {s['id']} exit {row['exit_code']}"
              f"{' HARNESS: ' + row['harness_failure'] if row['harness_failure'] else ''}"
              f"{' SIGN-IN: ' + row['auth_problem'] if row['auth_problem'] else ''} {row['wall_s']}s", flush=True)
    summary_path = calib / "results" / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    summary.update({r["id"]: r for r in rows})
    summary_path.write_text(json.dumps(dict(sorted(summary.items())), indent=1), encoding="utf-8")
    print(table([summary[k] for k in sorted(summary)]))
    print(f"results: {calib / 'results'}")
    found = sorted({p for r in rows for p in r["token_found_in"]} | set(scrub_token(R.oauth_token(root), calib)))
    if found:
        print("WARNING: the sign-in token was found in saved files and removed from them:", *found, sep="\n  ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
