"""Offline retrieval evaluation for DevMem context packs.

    python -m evals.retrieval.run [--json out.json] [--md out.md] [--budget 1500]
                                  [--project NAME] [--kind KIND] [--case ID ...]

For every labelled case it calls ``devmem.pack.build_pack(project, store, task, budget=..., mode="full")``
and checks which past sessions, files and verbatim requirement strings ended up in the pack text.
No LLM, no network. Fixtures are copied to a temp directory first, so nothing under demo/ or bench/ is
written. See evals/README.md for what the numbers mean and do not mean.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CASES_DIR = HERE / "cases"
SCHEMA = 1
# 1: a leading backticked path anywhere in the text counted (inflated by the Conventions section);
# 2: Conventions items are not citations. Baselines with different values are not comparable on file recall.
FILE_RECALL_METRIC = 2

# Held-out material must never be opened by the evaluation harness (see docs/DESIGN-v2.md).
_FORBIDDEN = ("bench/projects/P2", "bench/tasks/P2", "bench/tasks/P1/H")


# ---------------------------------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------------------------------
def _check_allowed(rel: str) -> None:
    norm = rel.replace("\\", "/")
    if any(norm.startswith(f) for f in _FORBIDDEN):
        raise ValueError(f"fixture path {rel!r} is held-out material and must not be used")


def load_projects() -> dict[str, dict]:
    projects = json.loads((HERE / "projects.json").read_text(encoding="utf-8"))
    for spec in projects.values():
        _check_allowed(spec["project"])
        _check_allowed(spec["sessions"])
    return projects


def load_cases(projects: dict[str, dict] | None = None) -> list[dict]:
    """All cases from cases/*.json; each gets its ``project`` from the file name."""
    projects = projects or load_projects()
    cases: list[dict] = []
    for path in sorted(CASES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if path.stem not in projects:
            raise ValueError(f"{path.name}: no project {path.stem!r} in projects.json")
        for c in data:
            c.setdefault("project", path.stem)
            for key in ("sessions_required", "sessions_optional", "files_required", "files_optional", "verbatim"):
                c.setdefault(key, [])
            cases.append(c)
    ids = [c["id"] for c in cases]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"duplicate case ids: {sorted(dup)}")
    return cases


# ---------------------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------------------
class Fixture:
    def __init__(self, name: str, project: Path, store: Path, scan_seconds: float, files: list[str]):
        self.name, self.project, self.store = name, project, store
        self.scan_seconds, self.files = scan_seconds, files


def project_files(project: Path) -> list[str]:
    return sorted(p.relative_to(project).as_posix() for p in project.rglob("*") if p.is_file())


def prepare_fixture(name: str, spec: dict, workdir: Path) -> Fixture:
    """Copy the project (minus excluded entries and any store) and its sessions into workdir, then scan."""
    from devmem.scan import scan

    src = REPO / spec["project"]
    skip = set(spec.get("exclude", [])) | {".devmemory", "__pycache__", "node_modules", ".git"}
    dest = workdir / name / "project"
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*skip))
    store = workdir / name / "store"
    from devmem.store import ensure_store
    ensure_store(store)          # created by devmem, so noted as this user's own (devmem.consent.store_refusal)
    (store / "sessions").mkdir(parents=True)
    for f in (REPO / spec["sessions"]).iterdir():
        if f.is_file():
            shutil.copy2(f, store / "sessions" / f.name)
    t0 = time.perf_counter()
    scan(dest, store)
    return Fixture(name, dest, store, time.perf_counter() - t0, project_files(dest))


def session_index(fx: Fixture) -> dict[str, dict]:
    """8-char session id prefix -> meta dict (used by validation and by tests)."""
    out = {}
    for p in sorted((fx.store / "sessions").glob("*.meta.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        out[str(s.get("session_id"))[:8]] = s
    return out


# ---------------------------------------------------------------------------------------------------
# measuring one pack
# ---------------------------------------------------------------------------------------------------
_CITE = re.compile(r"\bsession[ :]+([0-9a-f]{8})", re.I)
_CONVENTIONS = re.compile(r"^## Conventions", re.M)  # file recall: anything after this heading is not a citation
_TICK = re.compile(r"^\s*[-*]\s+`([^`\n]+)`", re.M)  # a list item that leads with a backticked path


def norm(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def _as_path(x) -> str | None:
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return x.get("path") or x.get("file") or x.get("rel")
    return None


def pack_contents(text: str, info: dict, files: list[str]) -> tuple[set[str], set[str], set[str]]:
    """(sessions, files, files merely mentioned) present in a pack.

    A session counts if ``info["sessions"]`` lists it OR the pack text cites ``session <id8>`` (the
    verbatim-requirements block cites its source session that way). A file counts if ``info["files"]``
    lists it OR the text before its "Conventions" section has a list item that leads with the path in
    backticks. List items inside Conventions, and paths quoted inside a convention line, do not count
    (``FILE_RECALL_METRIC`` 2; metric 1 counted the Conventions items and inflated file recall).
    ``mentioned`` is any occurrence of the path in the text.
    """
    sessions = set()
    for s in info.get("sessions", []) or []:
        sid = s.get("session") if isinstance(s, dict) else s
        if sid:
            sessions.add(str(sid)[:8])
    sessions |= {m.lower() for m in _CITE.findall(text)}
    known = set(files)
    listed = {p for p in map(_as_path, info.get("files", []) or []) if p}
    # the Conventions section also has list items that start with a backticked path; those describe a
    # file, they are not a citation of it, so only the text before that section is searched
    cited_text = _CONVENTIONS.split(text, maxsplit=1)[0]
    ticked = {m for m in _TICK.findall(cited_text) if m in known}
    mentioned = {f for f in known if f in text}
    return sessions, listed | ticked, mentioned


def _frac(hit: int, total: int):
    return None if total == 0 else hit / total


def evaluate_case(case: dict, fx: Fixture, budget: int = 1500, mode: str = "full") -> tuple[dict, float]:
    from devmem.pack import build_pack

    t0 = time.perf_counter()
    try:
        text, info = build_pack(fx.project, fx.store, case["task"], budget=budget, mode=mode)
        err = None
    except Exception as exc:  # a crash is a result, not a reason to stop the run
        text, info, err = "", {}, f"{type(exc).__name__}: {exc}"
    latency = time.perf_counter() - t0

    sess, got_files, mentioned = pack_contents(text, info, fx.files)
    req_s, opt_s = set(case["sessions_required"]), set(case["sessions_optional"])
    req_f, opt_f = set(case["files_required"]), set(case["files_optional"])
    haystack = norm(text)
    verb_hits = [v for v in case["verbatim"] if norm(v) in haystack]
    off_s = sess - req_s - opt_s
    off_f = got_files - req_f - opt_f
    items = len(sess) + len(got_files)
    row = {
        "id": case["id"], "project": case["project"], "kind": case["kind"],
        "sessions_found": sorted(sess), "files_found": sorted(got_files),
        "session_hits": len(req_s & sess), "session_total": len(req_s),
        "file_hits": len(req_f & got_files), "file_total": len(req_f),
        "file_mention_hits": len(req_f & mentioned),
        "verbatim_hits": len(verb_hits), "verbatim_total": len(case["verbatim"]),
        "verbatim_missing": [v for v in case["verbatim"] if v not in verb_hits],
        "missing_sessions": sorted(req_s - sess), "missing_files": sorted(req_f - got_files),
        "offlabel_sessions": sorted(off_s), "offlabel_files": sorted(off_f),
        "items": items, "offlabel_items": len(off_s) + len(off_f),
        "expect_empty": bool(case.get("expect_empty")),
        "empty_ok": (items == 0) if case.get("expect_empty") else None,
        "tokens": info.get("tokens_est", 0) if info else 0,
        "decisions": info.get("decisions", 0) if info else 0,
        "error": err,
    }
    return row, latency


# ---------------------------------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------------------------------
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return None if not xs else sum(xs) / len(xs)


def _r(x, n=4):
    return None if x is None else round(x, n)


def summarize(rows: list[dict]) -> dict:
    """Macro means of per-case recall (only over cases that have that label type), pooled noise."""
    s_cases = [r for r in rows if r["session_total"]]
    f_cases = [r for r in rows if r["file_total"]]
    v_cases = [r for r in rows if r["verbatim_total"]]
    e_cases = [r for r in rows if r["expect_empty"]]
    items, off = sum(r["items"] for r in rows), sum(r["offlabel_items"] for r in rows)
    toks = sorted(r["tokens"] for r in rows)
    return {
        "cases": len(rows),
        "errors": sum(1 for r in rows if r["error"]),
        "session_recall": _r(_mean(r["session_hits"] / r["session_total"] for r in s_cases)),
        "session_recall_n": len(s_cases),
        "file_recall": _r(_mean(r["file_hits"] / r["file_total"] for r in f_cases)),
        "file_recall_n": len(f_cases),
        "file_mention_recall": _r(_mean(r["file_mention_hits"] / r["file_total"] for r in f_cases)),
        "verbatim_recall": _r(_mean(r["verbatim_hits"] / r["verbatim_total"] for r in v_cases)),
        "verbatim_recall_n": len(v_cases),
        "noise_rate": _r(off / items) if items else None,
        "items": items, "offlabel_items": off,
        "empty_pass_rate": _r(_mean(1.0 if r["empty_ok"] else 0.0 for r in e_cases)),
        "empty_n": len(e_cases),
        "tokens_mean": _r(statistics.mean(toks), 1) if toks else None,
        "tokens_median": statistics.median(toks) if toks else None,
        "tokens_max": toks[-1] if toks else None,
    }


def _pct(sorted_vals: list[float], p: float) -> float:
    return sorted_vals[min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1))))]


def timing_summary(latencies: list[float]) -> dict:
    ms = sorted(x * 1000 for x in latencies)
    if not ms:
        return {}
    return {"mean_ms": round(statistics.mean(ms), 1), "p50_ms": round(_pct(ms, .5), 1),
            "p95_ms": round(_pct(ms, .95), 1), "max_ms": round(ms[-1], 1)}


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=20)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _private_user_data() -> None:
    """devmem keeps notes about the stores it creates in its per-user folder. A measurement run must not leave any in
    the real one, so unless DEVMEM_USER_DATA is set the run gets a temporary folder of its own (removed at exit)."""
    import atexit
    import os
    if os.environ.get("DEVMEM_USER_DATA"):
        return
    d = tempfile.mkdtemp(prefix="devmem-eval-user-")
    os.environ["DEVMEM_USER_DATA"] = d
    atexit.register(shutil.rmtree, d, ignore_errors=True)


def run_cases(cases: list[dict], projects: dict[str, dict] | None = None, budget: int = 1500,
              mode: str = "full", workdir: Path | None = None) -> dict:
    """Run cases and return the result dict (importable; tests call this with a small subset)."""
    _private_user_data()
    projects = projects or load_projects()
    own = workdir is None
    workdir = Path(tempfile.mkdtemp(prefix="devmem-evals-")) if own else Path(workdir)
    try:
        fixtures: dict[str, Fixture] = {}
        rows, lats = [], []
        for case in cases:
            name = case["project"]
            if name not in fixtures:
                fixtures[name] = prepare_fixture(name, projects[name], workdir)
            row, lat = evaluate_case(case, fixtures[name], budget=budget, mode=mode)
            rows.append(row)
            lats.append((case["id"], lat))
        kinds = sorted({r["kind"] for r in rows})
        projs = sorted({r["project"] for r in rows})
        return {
            "schema": SCHEMA,
            "config": {"budget": budget, "mode": mode, "file_recall_metric": FILE_RECALL_METRIC},
            "summary": {
                "overall": summarize(rows),
                "by_kind": {k: summarize([r for r in rows if r["kind"] == k]) for k in kinds},
                "by_project": {p: summarize([r for r in rows if r["project"] == p]) for p in projs},
            },
            "cases": rows,
            # everything below varies between runs on identical code; ignore it when diffing quality
            "timing": {
                "pack": timing_summary([l for _, l in lats]),
                "per_case_ms": {i: round(l * 1000, 1) for i, l in lats},
                "fixture_scan_seconds": {n: round(f.scan_seconds, 3) for n, f in fixtures.items()},
            },
            "meta": {
                "date": _dt.date.today().isoformat(),
                "git_head": _git("rev-parse", "HEAD"),
                "git_dirty": bool(_git("status", "--porcelain", "--", "plugin/devmem", "pyproject.toml")),
                "python": sys.version.split()[0],
            },
        }
    finally:
        if own:
            shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------------------------------
def _f(x, pct=True):
    if x is None:
        return "-"
    return f"{x * 100:.0f}%" if pct else f"{x:g}"


def markdown(result: dict) -> str:
    cfg, meta = result["config"], result["meta"]
    lines = [f"# Retrieval eval ({meta['date']}, commit {(meta.get('git_head') or '?')[:10]}"
             f"{', dirty tree' if meta.get('git_dirty') else ''}, budget {cfg['budget']}, mode {cfg['mode']})", ""]
    head = "| group | cases | session recall | file recall | verbatim recall | noise | empty ok | tokens mean (max) |"
    sep = "|---|---|---|---|---|---|---|---|"

    def line(name, s):
        return (f"| {name} | {s['cases']} | {_f(s['session_recall'])} ({s['session_recall_n']}) | "
                f"{_f(s['file_recall'])} ({s['file_recall_n']}) | {_f(s['verbatim_recall'])} ({s['verbatim_recall_n']}) | "
                f"{_f(s['noise_rate'])} | {_f(s['empty_pass_rate'])} ({s['empty_n']}) | "
                f"{_f(s['tokens_mean'], False)} ({s['tokens_max']}) |")

    lines += [head, sep, line("**overall**", result["summary"]["overall"])]
    lines += [line(f"kind: {k}", s) for k, s in result["summary"]["by_kind"].items()]
    lines += [line(f"project: {p}", s) for p, s in result["summary"]["by_project"].items()]
    lines += ["", "Numbers in parentheses are the count of cases that carry that label type. Noise = share of "
              "retrieved sessions+files that no label asked for. Empty ok = trivial prompts that got no "
              "session or file.", "", "## Cases", "",
              "| id | kind | sess | file | verb | items/off-label | tokens | missing |", "|---|---|---|---|---|---|---|---|"]
    for r in result["cases"]:
        miss = ", ".join(r["missing_sessions"] + r["missing_files"] + [f'"{v}"' for v in r["verbatim_missing"]])
        if r["error"]:
            miss = "ERROR " + r["error"]
        sess = f"{r['session_hits']}/{r['session_total']}" if r["session_total"] else "-"
        fil = f"{r['file_hits']}/{r['file_total']}" if r["file_total"] else "-"
        ver = f"{r['verbatim_hits']}/{r['verbatim_total']}" if r["verbatim_total"] else "-"
        tag = " (empty expected)" if r["expect_empty"] else ""
        lines.append(f"| {r['id']} | {r['kind']}{tag} | {sess} | {fil} | {ver} | "
                     f"{r['items']}/{r['offlabel_items']} | {r['tokens']} | {miss.replace('|', '/')} |")
    t = result["timing"].get("pack", {})
    if t:
        lines += ["", f"Pack latency (varies run to run): mean {t['mean_ms']} ms, p50 {t['p50_ms']} ms, "
                      f"p95 {t['p95_ms']} ms, max {t['max_ms']} ms."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.retrieval.run", description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", help="write the full result as stable JSON to this path")
    ap.add_argument("--md", help="write the Markdown report to this path")
    ap.add_argument("--budget", type=int, default=1500)
    ap.add_argument("--mode", default="full", choices=["full", "static", "nofresh", "requests"])
    ap.add_argument("--project", action="append", help="only this project (repeatable)")
    ap.add_argument("--kind", action="append", help="only this case kind (repeatable)")
    ap.add_argument("--case", action="append", help="only this case id (repeatable)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    cases = load_cases()
    if args.project:
        cases = [c for c in cases if c["project"] in args.project]
    if args.kind:
        cases = [c for c in cases if c["kind"] in args.kind]
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2
    sys.path.insert(0, str(REPO / "plugin"))   # the devmem package of this working tree
    result = run_cases(cases, budget=args.budget, mode=args.mode)
    md = markdown(result)
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.md:
        Path(args.md).write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
