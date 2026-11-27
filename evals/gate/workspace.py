"""Working copies for the gate experiment (evals/gate/TASK-FORMAT.md).

Everything lives under one root folder outside this repository (default `<temp>/devmem-gate`, or `--root`):

    src/<name>-<commit12>/          the repository, cloned once and checked out at the pinned commit
    deps/<name>-<commit12>-<h>/     its dependencies, installed once (a venv, or node_modules)
    base/<name>-<commit12>-<h>/     the starting code: the pinned tree plus the history's reference patches, as a
                                    git repository with one commit per step
    runs/<run id>/                  one folder per run (runner): its working copy, configuration folder, logs

A working copy is a copy of the base (with its .git, so the grader and the report can diff against the starting
code) and, for Node, a `node_modules` link to the shared install.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GATE = Path(__file__).resolve().parent
TASKS = GATE / "tasks"
TUNING = GATE / "tuning"
DEFAULT_ROOT = Path(tempfile.gettempdir()) / "devmem-gate"


def git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", *args], cwd=cwd,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {p.stderr.strip()[-400:]}")
    return p.stdout


def commit_all(cwd: Path, message: str) -> None:
    git(cwd, "add", "-A")
    git(cwd, "-c", "user.name=gate", "-c", "user.email=gate@example.invalid", "commit", "-q", "--allow-empty",
        "-m", message)


def load_repo(name: str) -> dict:
    cfg = json.loads((TASKS / name / "repo.json").read_text(encoding="utf-8"))
    cfg["dir"] = TASKS / name
    return cfg


def load_history(name: str) -> list[dict]:
    return json.loads((TASKS / name / "history" / "history.json").read_text(encoding="utf-8"))["sessions"]


def task_dirs(name: str, tuning: bool = False) -> list[Path]:
    top = (TUNING if tuning else TASKS) / name
    return sorted(p.parent for p in top.glob("*/task.json"))


def load_task(d: Path) -> dict:
    t = json.loads((d / "task.json").read_text(encoding="utf-8"))
    t["dir"] = d
    return t


def _digest(*parts: bytes) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(hashlib.sha256(p).digest())
    return h.hexdigest()[:12]


def rmtree(path: Path) -> None:
    """Remove a folder; a link or junction inside it (node_modules) is removed as a link, never followed."""
    import stat
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return
    isjunction = getattr(os.path, "isjunction", None)
    for root, dirs, _files in os.walk(path):
        for d in list(dirs):
            p = os.path.join(root, d)
            if os.path.islink(p) or (isjunction is not None and isjunction(p)):
                _unlink_dir_link(p)
                dirs.remove(d)

    def retry(func, p, _exc):     # git leaves read-only object files on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def _unlink_dir_link(p: str) -> None:
    try:
        os.unlink(p)
    except OSError:
        os.rmdir(p)          # a Windows junction


def link_dir(target: Path, link: Path) -> None:
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def source(cfg: dict, root: Path) -> Path:
    """The repository at its pinned commit (cloned once)."""
    dest = root / "src" / f"{cfg['name']}-{cfg['commit'][:12]}"
    if not (dest / ".git").is_dir():
        rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "-q", "--no-checkout", cfg["url"], str(dest)],
                       check=True)
        git(dest, "checkout", "-q", "--detach", cfg["commit"])
    head = git(dest, "rev-parse", "HEAD").strip()
    if head != cfg["commit"]:
        raise RuntimeError(f"{dest} is at {head}, not the pinned {cfg['commit']}")
    return dest


def deps(cfg: dict, root: Path) -> Path:
    """The repository's dependencies, installed once outside every working copy."""
    spec = cfg["deps"]
    src = source(cfg, root)
    lock = b""
    lock_file = src / "package-lock.json"
    if spec["kind"] == "node":
        # A repository that ships no npm lock file (bonsai-js has only bun.lock) names one kept with its tasks
        # (`"deps": {"kind": "node", "lock": "package-lock.json"}`, made once from its package.json), frozen with them.
        if spec.get("lock"):
            lock_file = cfg["dir"] / spec["lock"]
        lock = lock_file.read_bytes()
    dest = root / "deps" / f"{cfg['name']}-{cfg['commit'][:12]}-{_digest(json.dumps(spec, sort_keys=True).encode(), lock)}"
    done = dest / ".installed"
    if done.exists():
        return dest
    rmtree(dest)
    dest.mkdir(parents=True)
    if spec["kind"] == "python":
        subprocess.run([sys.executable, "-m", "venv", str(dest / "venv")], check=True)
        py = venv_python(dest)
        if spec.get("requirements"):
            subprocess.run([str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                            *spec["requirements"]], check=True)
    elif spec["kind"] == "node":
        shutil.copyfile(src / "package.json", dest / "package.json")
        shutil.copyfile(lock_file, dest / "package-lock.json")
        subprocess.run(["npm", "ci", "--no-audit", "--no-fund", "--ignore-scripts"], cwd=dest, check=True,
                       shell=os.name == "nt")
    else:
        raise ValueError(f"unknown deps kind {spec['kind']!r}")
    done.write_text("ok\n", encoding="utf-8")
    return dest


def venv_python(deps_dir: Path) -> Path:
    v = deps_dir / "venv"
    return v / "Scripts" / "python.exe" if os.name == "nt" else v / "bin" / "python"


def tool_env(cfg: dict, deps_dir: Path, work: Path | None = None, base_env: dict | None = None) -> dict:
    """The environment a grader, the repository's tests and the agent's shell use: the repository's tools first on
    PATH, nothing of devmem's or Claude Code's own variables."""
    env = {k: v for k, v in (base_env if base_env is not None else os.environ).items()
           if not k.upper().startswith(("DEVMEM", "CLAUDE", "PYTHONPATH", "VIRTUAL_ENV", "NODE_PATH"))}
    if cfg["deps"]["kind"] == "python":
        first = [str(venv_python(deps_dir).parent)]
        env["VIRTUAL_ENV"] = str(deps_dir / "venv")
    else:
        first = [str((work or deps_dir) / "node_modules" / ".bin")]
    env["PATH"] = os.pathsep.join(first + [env.get("PATH", "")])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def apply_patch(work: Path, patch: Path) -> None:
    git(work, "apply", "--whitespace=nowarn", str(patch))


def base(cfg: dict, root: Path) -> Path:
    """The starting code: pinned tree + every history patch in order, one commit each."""
    hist_dir = cfg["dir"] / "history"
    sessions = load_history(cfg["name"])
    patches = [hist_dir / s["patch"] for s in sessions]
    dest = root / "base" / f"{cfg['name']}-{cfg['commit'][:12]}-{_digest(*(p.read_bytes() for p in patches))}"
    if (dest / ".gate-base").exists():
        return dest
    rmtree(dest)
    src = source(cfg, root)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))
    git(dest, "init", "-q")
    commit_all(dest, f"{cfg['name']} at {cfg['commit']}")
    for s, p in zip(sessions, patches):
        apply_patch(dest, p)
        commit_all(dest, f"history session {s['n']}")
    (dest / ".git" / "info" / "exclude").write_text("node_modules\n.gate-base\n", encoding="utf-8")
    (dest / ".gate-base").write_text("ok\n", encoding="utf-8")
    return dest


def work_copy(cfg: dict, root: Path, dest: Path) -> Path:
    """A fresh working copy of the starting code at `dest` (which must not exist)."""
    b = base(cfg, root)
    d = deps(cfg, root)
    shutil.copytree(b, dest, ignore=shutil.ignore_patterns(".gate-base"))
    if cfg["deps"]["kind"] == "node":
        link_dir(d / "node_modules", dest / "node_modules")
    return dest


def grade(task: dict, work: Path, env: dict) -> dict:
    """Run the task's grader; a grader that raises is a failed run."""
    try:
        spec = importlib.util.spec_from_file_location(f"grade_{task['repo']}_{task['id']}", task["dir"] / "grade.py")
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {task['dir'] / 'grade.py'}")
        mod = importlib.util.module_from_spec(spec)
        keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True   # no __pycache__ in the frozen task folder
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.dont_write_bytecode = keep
        res = mod.grade(Path(work), dict(env))
        if not isinstance(res, dict) or not isinstance(res.get("passed"), bool):
            raise TypeError(f"grader returned {type(res).__name__}, not a dict with a bool 'passed'")
    except Exception as exc:  # noqa: BLE001 - a crashing grader is a failed run, recorded as such
        res = {"passed": False, "checks": {"grader_exception": False}, "error": repr(exc)[:500]}
    return res
