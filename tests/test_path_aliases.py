"""A name that is another spelling of a file (a Windows 8.3 short name, a trailing dot or space) is refused.

`MY_SEC~1.TXT` written into git's index for a gitignored `my_secret_notes.txt` passed the secret-name and ignore
rules (they ran on the name git printed), was indexed, and its line appeared in a pack; the viewer's /api/file served
it too. project_path now requires that the file's real path is the path asked for (case aside on Windows).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from devmem import store as S
from devmem.store import list_files, project_path


def test_an_alias_whose_real_name_differs_is_refused(tmp_path, monkeypatch):
    (tmp_path / "my_secret_notes.txt").write_text("k\n", encoding="utf-8")
    real = os.path.realpath

    def fake(p, *a, **k):
        p = os.fspath(p)
        if p.upper().endswith("MY_SEC~1.TXT"):
            return str(tmp_path / "my_secret_notes.txt")
        return real(p, *a, **k)
    monkeypatch.setattr(S.os.path, "realpath", fake)
    assert project_path(tmp_path, "MY_SEC~1.TXT") is None
    assert project_path(tmp_path, "my_secret_notes.txt") is not None   # the real name itself is fine


@pytest.mark.skipif(os.name != "nt", reason="Windows file names")
@pytest.mark.parametrize("alias", ["notes.txt.", "notes.txt ", "notes.txt. ."])
def test_trailing_dot_or_space_spellings_are_refused(tmp_path, alias):
    (tmp_path / "notes.txt").write_text("x\n", encoding="utf-8")
    assert project_path(tmp_path, alias) is None
    assert project_path(tmp_path, "NOTES.TXT") is not None           # the same name in another case is the same file


def _short_name(p: Path) -> str | None:
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024):
        return None
    short = Path(buf.value).name
    return short if short.lower() != p.name.lower() else None


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names exist on Windows volumes that generate them")
def test_a_real_short_name_in_gits_index_is_not_listed(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    secret = project / "my_secret_notes.txt"
    secret.write_text("DB pass is hunter2-planted\n", encoding="utf-8")
    short = _short_name(secret)
    if not short:
        pytest.skip("this volume does not generate 8.3 short names")
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")

    def git(*args):
        return subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, check=True).stdout
    git("init", "-q")
    git("add", "a.py")
    sha = git("hash-object", "-w", str(secret)).strip()
    git("update-index", "--add", "--cacheinfo", f"100644,{sha},{short}")
    assert short in git("ls-files")
    names = [rel for rel, _size, _mtime in list_files(project)[0]]
    assert "a.py" in names and short not in names and not any("secret" in n.lower() for n in names), names
    assert project_path(project, short) is None
