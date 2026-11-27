"""The freeze of the gate's task set: every file under evals/gate/tasks/ and evals/gate/tuning/ with its SHA-256.

    python -m evals.gate.verify_freeze            # check: exit 1 when a file was added, removed or changed
    python -m evals.gate.verify_freeze --write    # write MANIFEST.sha256 (once, when the task set is final)

Hashes are of the file with CRLF turned into LF, so a checkout with either line ending verifies. The last line is
`TOTAL <sha256 of the listing>`; the runner refuses to start unless the check passes.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent
MANIFEST = GATE / "tasks" / "MANIFEST.sha256"      # where PREREGISTRATION.md section 2 puts it
FOLDERS = ("tasks", "tuning")


def _files() -> list[Path]:
    out = []
    for top in FOLDERS:
        base = GATE / top
        if base.is_dir():
            out += [p for p in base.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and p.resolve() != MANIFEST.resolve()]
    return sorted(out, key=lambda p: p.relative_to(GATE).as_posix())


CRLF, LF = b"\r\n", b"\n"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes().replace(CRLF, LF)).hexdigest()


def listing() -> list[str]:
    return [f"{_sha(p)}  {p.relative_to(GATE).as_posix()}" for p in _files()]


def total(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def check() -> tuple[bool, list[str]]:
    if not MANIFEST.is_file():
        return False, ["no MANIFEST.sha256: the task set is not frozen yet"]
    recorded = [ln for ln in MANIFEST.read_text(encoding="utf-8").splitlines() if ln.strip()]
    entries = [ln for ln in recorded if not ln.startswith("TOTAL ")]
    want_total = next((ln.split()[1] for ln in recorded if ln.startswith("TOTAL ")), "")
    now = listing()
    msgs = []
    rec = {ln.split("  ", 1)[1]: ln.split("  ", 1)[0] for ln in entries}
    cur = {ln.split("  ", 1)[1]: ln.split("  ", 1)[0] for ln in now}
    for rel in sorted(set(rec) | set(cur)):
        if rel not in cur:
            msgs.append(f"MISSING {rel}")
        elif rel not in rec:
            msgs.append(f"ADDED   {rel}")
        elif rec[rel] != cur[rel]:
            msgs.append(f"CHANGED {rel}")
    if total(entries) != want_total:
        msgs.append("TOTAL does not match the listing in MANIFEST.sha256")
    return not msgs, msgs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.gate.verify_freeze", description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    if a.write:
        lines = listing()
        MANIFEST.write_text("\n".join(lines + [f"TOTAL {total(lines)}"]) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {MANIFEST.name}: {len(lines)} files, TOTAL {total(lines)[:16]}")
        return 0
    ok, msgs = check()
    for m in msgs:
        print(m)
    print(f"{len(listing())} files, {'frozen and unchanged' if ok else 'NOT as frozen'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
