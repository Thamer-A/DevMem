"""Helpers for the CLI and MCP tests: a tiny project, hand-written session records, subprocess runners."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = ROOT / "plugin"   # the folder that holds the devmem package


def make_project(root: Path, name: str = "proj") -> Path:
    project = root / name
    (project / "src").mkdir(parents=True)
    (project / "src" / "wallet.py").write_text(
        "def balance_label(amount):\n    return str(amount)\n\n\ndef negative_balance(amount):\n    return -abs(amount)\n",
        encoding="utf-8")
    (project / "README.md").write_text("# demo\n", encoding="utf-8")
    return project


def write_session(store: Path, sid: str, prompt: str, started_at: str = "2026-11-01T10:00:00Z",
                  files_edited: list[str] | None = None, claim: bool = True) -> Path:
    """A schema-1 session record (what older versions wrote); the loader upgrades it in memory. claim: the store is
    noted as this user's own (devmem.consent.claim_store), as devmem notes a store it creates; False for a store a
    repository shipped."""
    if claim:
        from devmem.consent import claim_store
        store.mkdir(parents=True, exist_ok=True)
        claim_store(store)
    sessions = store / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    stamp = started_at.replace("-", "").replace(":", "")[:13]
    base = sessions / f"{stamp}_{sid[:8]}"
    meta = {"session_id": sid, "provider": "claude-code", "capture_method": "stream-json", "started_at": started_at,
            "prompt": prompt, "result_text": "done", "result_source": "assistant-visible",
            "raw_record": base.name + ".jsonl", "tool_calls": [], "files_read": [],
            "files_edited": files_edited or [], "files_deleted": [], "file_hashes_at_capture": {}, "usage": {},
            "assistant_text": [], "errors": []}
    base.with_suffix(".jsonl").write_text('{"type":"system"}\n', encoding="utf-8")
    p = base.with_suffix(".meta.json")
    p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return p


def cli_env(**extra) -> dict:
    env = {**os.environ, "PYTHONPATH": str(PKG_ROOT),"PYTHONUTF8": "0"}
    env.pop("PYTHONIOENCODING", None)
    env.update(extra)
    return env


def run_cli(args: list[str], cwd: Path, stdin: str | bytes = "", env: dict | None = None, timeout: int = 120):
    """Run `python -m devmem ...` as a real subprocess; returns CompletedProcess with bytes output."""
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    return subprocess.run([sys.executable, "-m", "devmem", *args], cwd=cwd, input=data, capture_output=True,
                          env=env or cli_env(), timeout=timeout)
