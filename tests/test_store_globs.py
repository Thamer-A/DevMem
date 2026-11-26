"""devmem.store._Globs answers as the one regex of its globs would, with a cheap test in front of it (a glob can only
match a name that starts with its text before the first wildcard and ends with its text after the last one), and
the file listing spends nothing on index entries whose files are not there."""
from __future__ import annotations

import fnmatch
import random
import re
import subprocess
import shutil

import pytest
from hooks_helpers import make_project

from devmem import store


def _plain(patterns):
    return re.compile("|".join(f"(?:{fnmatch.translate(p)})" for p in patterns) or r"(?!)", re.I)


ALPHABET = "aAbBkKsSzZ019._-~/ []!*?" + "ſKİßéÉ"


def _rand(rng, n, chars=ALPHABET):
    return "".join(rng.choice(chars) for _ in range(rng.randint(0, n)))


def test_the_same_answers_as_the_regex_on_the_default_globs(tmp_path):
    pats = tuple(store.load_ignores(tmp_path, None))
    g, rx = store._Globs(pats), _plain(pats)
    rng = random.Random(7)
    names = [".env", ".ENV.local", "id_rsa.pub", "My-Secret.txt", "x/CREDENTIALS.json", "node_modules", "a.pyc",
             "A.PYC", "ſecret", "sKey.pem", "Thumbs.db", "build", "x" * 5000 + ".pem"]
    for _ in range(20000):
        base = rng.choice(pats).replace("*", _rand(rng, 6)).replace("?", rng.choice("aZ._é"))
        names += [base, base.upper(), _rand(rng, 12) + base, base + _rand(rng, 3), _rand(rng, 20)]
    for n in names:
        assert g.match(n) == bool(rx.match(n)), (n, [p for p in pats if re.match(fnmatch.translate(p), n, re.I)])


def test_the_same_answers_as_the_regex_on_random_globs():
    rng = random.Random(11)
    for _ in range(400):
        pats = tuple(_rand(rng, 8) for _ in range(rng.randint(0, 6)))
        try:
            rx = _plain(pats)
        except re.error:
            continue
        g = store._Globs(pats)
        for _ in range(200):
            n = _rand(rng, 30)
            assert g.match(n) == bool(rx.match(n)), (pats, n)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_index_entries_without_files_are_skipped_without_looking_at_their_folders(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    blob = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD:greet.py"], capture_output=True, text=True,
                          check=True).stdout.strip()
    subprocess.run(["git", "-C", str(project), "update-index", "--add", "--index-info"], check=True,
                   input="".join(f"100644 {blob}\tgone{i}/a/b/c.py\n" for i in range(200)).encode())
    calls = {"n": 0}
    real = store.project_path

    def counted(*a, **k):
        calls["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(store, "project_path", counted)
    files, how = store.list_files(project, project / ".devmemory")
    assert how == "git" and not any(f[0].startswith("gone") for f in files)
    assert calls["n"] == len(files) <= 20           # only the files that are there
