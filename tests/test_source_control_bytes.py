"""No source file of the package holds a control byte other than tab, line feed and carriage return (a NUL or an escape
written into a file by a tool that turned `\\x00` into the byte itself would change what a pattern matches)."""
from __future__ import annotations

import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "plugin" / "devmem"
CONTROL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def test_no_control_bytes_in_the_package_sources():
    files = [f for f in PACKAGE.rglob("*") if f.is_file() and "__pycache__" not in f.parts]
    assert any(f.name == "redact.py" for f in files)
    bad = {}
    for f in files:
        hits = [m.start() for m in CONTROL.finditer(f.read_bytes())]
        if hits:
            bad[str(f.relative_to(PACKAGE))] = hits[:5]
    assert not bad, bad
