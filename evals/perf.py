"""Performance probe for scan and pack.

    python -m evals.perf <project> [<project> ...] [--stdlib] [--prompt TEXT ...] [--json out.json]

For each project it uses a throw-away store in a temp directory (nothing is written into the project),
and reports:  cold scan (empty store), warm scan (store already built, nothing changed), pack time for a
few sample prompts (first call and best of the repeats), and the size of the map and PROJECT.md.

``--stdlib`` additionally copies the Python standard library (no site-packages, no __pycache__) into a temp
directory and measures that, as a large-ish real tree.

Calls only ``devmem.scan.scan(project, store)`` and ``devmem.pack.build_pack(project, store, task, ...)``.
Timings are wall-clock on whatever machine runs this; compare runs from the same machine only.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

DEFAULT_PROMPTS = [
    "fix the parsing bug when the input contains unicode characters",
    "add a timeout option to the network client and document it",
    "yes go ahead",
    "refactor the logging code so errors include the file name",
]


def _tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _find(store: Path, *names: str) -> Path | None:
    for n in names:
        p = store / n
        if p.exists():
            return p
    return None


def count_tree(project: Path) -> dict:
    """Rough size of the tree as the user sees it (all files, no ignores applied)."""
    files = lines = 0
    for root, dirs, names in os.walk(project):
        dirs[:] = [d for d in dirs if d not in (".git", ".devmemory", "__pycache__", "node_modules")]
        for n in names:
            files += 1
            try:
                with open(Path(root) / n, "rb") as fh:
                    lines += sum(1 for _ in fh)
            except OSError:
                pass
    return {"files_on_disk": files, "lines_on_disk": lines}


def probe(project: Path, prompts: list[str], repeats: int = 3, budget: int = 1500) -> dict:
    from devmem.pack import build_pack
    from devmem.scan import scan

    project = Path(project).resolve()
    had_store = (project / ".devmemory").exists()
    out: dict = {"project": str(project), **count_tree(project)}
    store_root = Path(tempfile.mkdtemp(prefix="devmem-perf-"))
    store = store_root / "store"
    try:
        t0 = time.perf_counter()
        scan(project, store)
        out["cold_scan_s"] = round(time.perf_counter() - t0, 3)

        warm = []
        for _ in range(max(1, repeats)):
            t0 = time.perf_counter()
            scan(project, store)
            warm.append(time.perf_counter() - t0)
        out["warm_scan_s"] = round(min(warm), 3)
        out["warm_scan_runs_s"] = [round(w, 3) for w in warm]

        m = _find(store, "index/map.json", "map.json")
        pm = _find(store, "PROJECT.md")
        out["map_json_bytes"] = m.stat().st_size if m else None
        out["project_md_bytes"] = pm.stat().st_size if pm else None
        out["project_md_tokens_est"] = _tokens(_read(pm)) if pm else None
        db = _find(store, "index/index.db")
        out["index_db_bytes"] = db.stat().st_size if db else None
        if m:
            try:
                out["files_in_map"] = len(json.loads(_read(m)).get("files", {}))
            except ValueError:
                out["files_in_map"] = None

        packs = []
        for p in prompts:
            times, tokens, err = [], None, None
            for _ in range(max(1, repeats)):
                t0 = time.perf_counter()
                try:
                    text, info = build_pack(project, store, p, budget=budget, mode="full")
                    tokens = info.get("tokens_est", _tokens(text))
                except Exception as exc:  # report, do not abort the probe
                    err = f"{type(exc).__name__}: {exc}"
                    break
                times.append(time.perf_counter() - t0)
            packs.append({"prompt": p, "first_s": round(times[0], 3) if times else None,
                          "best_s": round(min(times), 3) if times else None, "tokens_est": tokens, "error": err})
        out["pack"] = packs
        ok = [x["first_s"] for x in packs if x["first_s"] is not None]
        out["pack_first_mean_s"] = round(sum(ok) / len(ok), 3) if ok else None
    finally:
        shutil.rmtree(store_root, ignore_errors=True)
    out["target_got_a_store"] = (project / ".devmemory").exists() and not had_store  # must stay False
    return out


def copy_stdlib(dest: Path) -> Path:
    src = Path(sysconfig.get_paths()["stdlib"])
    target = dest / "stdlib"
    shutil.copytree(src, target, ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc"))
    return target


def table(results: list[dict]) -> str:
    rows = ["| project | files | lines | cold scan s | warm scan s | pack s (mean first) | map.json KB | PROJECT.md KB (~tokens) |",
            "|---|---|---|---|---|---|---|---|"]
    for r in results:
        kb = lambda b: "-" if b is None else f"{b / 1024:.0f}"
        rows.append(f"| {Path(r['project']).name} | {r['files_on_disk']} | {r['lines_on_disk']} | {r['cold_scan_s']} | "
                    f"{r['warm_scan_s']} | {r['pack_first_mean_s']} | {kb(r['map_json_bytes'])} | "
                    f"{kb(r['project_md_bytes'])} (~{r['project_md_tokens_est']}) |")
    return "\n".join(rows) + "\n"


def _git_head() -> str | None:
    try:
        o = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=20)
        return o.stdout.strip() or None
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


def main(argv: list[str] | None = None) -> int:
    _private_user_data()
    ap = argparse.ArgumentParser(prog="python -m evals.perf", description="scan/pack performance probe")
    ap.add_argument("projects", nargs="*", help="project directories to measure (never modified)")
    ap.add_argument("--stdlib", action="store_true", help="also measure a temp copy of the Python standard library")
    ap.add_argument("--prompt", action="append", help="sample prompt (repeatable); default: four generic prompts")
    ap.add_argument("--repeats", type=int, default=3, help="warm scans / pack repeats per prompt")
    ap.add_argument("--budget", type=int, default=1500)
    ap.add_argument("--json", help="write results as JSON to this path")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if not args.projects and not args.stdlib:
        ap.error("give at least one project path or --stdlib")
    sys.path.insert(0, str(REPO / "plugin"))   # the devmem package of this working tree
    prompts = args.prompt or DEFAULT_PROMPTS

    results, tmp = [], None
    try:
        for p in args.projects:
            if not Path(p).is_dir():
                ap.error(f"not a directory: {p}")
            posix = Path(p).resolve().as_posix()
            if (REPO / "bench" / "projects" / "P2").resolve().as_posix() in posix or posix == REPO.as_posix():
                ap.error("refusing to scan the repo root or held-out bench/projects/P2 (use a fixture directory)")
            print(f"measuring {p} ...", file=sys.stderr)
            results.append(probe(Path(p), prompts, args.repeats, args.budget))
        if args.stdlib:
            tmp = Path(tempfile.mkdtemp(prefix="devmem-stdlib-"))
            print(f"copying the standard library to {tmp} ...", file=sys.stderr)
            results.append(probe(copy_stdlib(tmp), prompts, args.repeats, args.budget))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    doc = {"schema": 1, "date": _dt.date.today().isoformat(), "git_head": _git_head(),
           "python": sys.version.split()[0], "platform": sys.platform, "prompts": prompts, "results": results}
    if args.json:
        Path(args.json).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(table(results))
    for r in results:
        for x in r["pack"]:
            print(f"  {Path(r['project']).name}: pack {x['first_s']}s first / {x['best_s']}s best, ~{x['tokens_est']} tokens"
                  f"{' ERROR ' + x['error'] if x['error'] else ''}  <- {x['prompt'][:50]!r}")
        if r["target_got_a_store"]:
            print(f"  WARNING: {r['project']} gained a .devmemory directory", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
