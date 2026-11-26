"""File listing (devmem.store): .gitignore with and without git, the secret-file floor."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import devmem.store as store_mod
from devmem.index import Index
from devmem.store import GitIgnore, iter_files, list_files

HAS_GIT = shutil.which("git") is not None

SECRETS = [".env", ".env.local", "config/credentials.json", "keys/server.pem", "id_rsa", "id_ed25519.pub",
           "app/secrets.py", "deploy/.npmrc", "SECRET_NOTES.md"]


def make_tree(root: Path) -> None:
    files = {
        "src/app.py": "def main():\n    return 1\n",
        "src/util.py": "def helper():\n    return 2\n",
        "src/generated/out.py": "X = 1\n",
        "logs/run.log": "log line\n",
        "notes.tmp": "scratch\n",
        "keep.tmp": "kept by negation\n",
        "docs/guide.md": "# Guide\n",
        "docs/build/page.html": "<p>built</p>\n",
        "node_modules/pkg/index.js": "module.exports = 1\n",
        "sub/.gitignore": "*.cache.txt\n",
        "sub/data.cache.txt": "cached\n",
        "sub/data.txt": "real\n",
        "Lib/main.py": "print(1)\n",
    }
    for s in SECRETS:
        files[s] = "SECRET_TOKEN=supersecretvalue123\n"
    files[".gitignore"] = "\n".join([
        "# generated output", "src/generated/", "*.log", "*.tmp", "!keep.tmp", "/docs/build/",
        # a .gitignore that tries to re-include secrets must not win over the floor
        "!.env", "!*.pem",
    ]) + "\n"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


EXPECTED = {".gitignore", "Lib/main.py", "docs/guide.md", "keep.tmp", "src/app.py", "src/util.py",
            "sub/.gitignore", "sub/data.txt"}


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})


def test_gitignore_without_git(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "_git_list", lambda project: None)
    make_tree(tmp_path)
    entries, method = list_files(tmp_path)
    assert method == "walk"
    assert {e[0] for e in entries} == EXPECTED
    assert list(iter_files(tmp_path)) == sorted(EXPECTED)


@pytest.mark.skipif(not HAS_GIT, reason="git not installed")
def test_gitignore_with_git(tmp_path):
    make_tree(tmp_path)
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "src/app.py")                  # one tracked file, the rest untracked
    git(tmp_path, "add", "-f", ".env", "keys/server.pem")  # secrets force-tracked: still excluded
    entries, method = list_files(tmp_path)
    assert method == "git"
    assert {e[0] for e in entries} == EXPECTED


@pytest.mark.skipif(not HAS_GIT, reason="git not installed")
def test_git_and_walk_agree(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    make_tree(a)
    make_tree(b)
    git(a, "init", "-q")
    via_git = {e[0] for e in list_files(a)[0]}
    old = store_mod._git_list
    try:
        store_mod._git_list = lambda project: None
        via_walk = {e[0] for e in list_files(b)[0]}
    finally:
        store_mod._git_list = old
    assert via_git == via_walk


@pytest.mark.skipif(not HAS_GIT, reason="git not installed")
def test_project_ignored_by_enclosing_repo_falls_back_to_walk(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("inner/\n", encoding="utf-8")
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "a.py").write_text("x = 1\n", encoding="utf-8")
    entries, method = list_files(inner)
    assert [e[0] for e in entries] == ["a.py"] and method == "walk"


@pytest.mark.parametrize("use_git", [False, True])
def test_secret_files_never_indexed(tmp_path, monkeypatch, use_git):
    if use_git and not HAS_GIT:
        pytest.skip("git not installed")
    proj = tmp_path / "proj"
    make_tree(proj)
    if use_git:
        git(proj, "init", "-q")
        git(proj, "add", "-f", *SECRETS)
    else:
        monkeypatch.setattr(store_mod, "_git_list", lambda project: None)
    with Index(tmp_path / "store") as idx:
        idx.sync(proj)
        indexed = set(idx.files())
        assert not indexed & set(SECRETS)
        assert idx.search("file", "supersecretvalue123 SECRET_TOKEN") == []
    db = (tmp_path / "store" / "index" / "index.db").read_bytes()
    assert b"supersecretvalue" not in db


def test_store_ignore_file_applies(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "_git_list", lambda project: None)
    make_tree(tmp_path)
    (tmp_path / ".devmemory").mkdir()
    (tmp_path / ".devmemory" / "ignore").write_text("docs\n# comment\n", encoding="utf-8")
    rels = set(iter_files(tmp_path))
    assert "docs/guide.md" not in rels and "src/app.py" in rels


def test_gitignore_rules():
    gi = GitIgnore("", ["*.log", "!important.log", "build/", "/root-only.txt", "a/**/z.txt", "doc/*.html"])
    assert gi.match("x/y.log", False) is True
    assert gi.match("important.log", False) is False
    assert gi.match("build", True) is True and gi.match("build", False) is None
    assert gi.match("root-only.txt", False) is True and gi.match("sub/root-only.txt", False) is None
    assert gi.match("a/b/c/z.txt", False) is True and gi.match("a/z.txt", False) is True
    assert gi.match("doc/x.html", False) is True and gi.match("doc/sub/x.html", False) is None
    nested = GitIgnore("pkg", ["*.tmp"])
    assert nested.match("pkg/a.tmp", False) is True and nested.match("other/a.tmp", False) is None
