"""Sanity checks on the retrieval eval labels: they must refer to things that exist, so a typo in a case
cannot silently lower (or raise) a score. These tests read fixtures but never write into them."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from evals.retrieval import run as R  # noqa: E402

PROJECTS = R.load_projects()
CASES = R.load_cases(PROJECTS)

KINDS = {"followup_early_req", "terse_bug", "untouched_file", "trivial", "paraphrase", "non_english",
         "direct", "stale_memory"}


def _sessions(name):
    out = {}
    for p in sorted((REPO / PROJECTS[name]["sessions"]).glob("*.meta.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        out[str(s["session_id"])[:8]] = s
    return out


def _tree(name):
    root = REPO / PROJECTS[name]["project"]
    skip = set(PROJECTS[name].get("exclude", [])) | {".devmemory"}
    return {p.relative_to(root).as_posix() for p in root.rglob("*")
            if p.is_file() and not (skip & set(p.relative_to(root).parts))}


def test_enough_cases_and_every_kind_present():
    assert len(CASES) >= 40
    assert {c["kind"] for c in CASES} == KINDS
    assert sum(1 for c in CASES if c["kind"] == "non_english") >= 4


def test_every_case_has_a_justification_and_unique_id():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))
    for c in CASES:
        assert c["why"].strip(), c["id"]
        assert c["kind"] in KINDS, c["id"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_labels_refer_to_real_sessions_and_files(case):
    sessions, tree = _sessions(case["project"]), _tree(case["project"])
    for sid in case["sessions_required"] + case["sessions_optional"]:
        assert sid in sessions, f"{case['id']}: unknown session {sid}"
    for f in case["files_required"] + case["files_optional"]:
        assert f in tree, f"{case['id']}: unknown file {f}"
    assert not set(case["sessions_required"]) & set(case["sessions_optional"])
    assert not set(case["files_required"]) & set(case["files_optional"])


@pytest.mark.parametrize("case", [c for c in CASES if c["verbatim"]], ids=lambda c: c["id"])
def test_verbatim_strings_really_occur_in_a_required_session_prompt(case):
    prompts = " ".join(R.norm(_sessions(case["project"])[sid]["prompt"]) for sid in case["sessions_required"])
    assert case["sessions_required"], f"{case['id']}: verbatim label without a required session"
    for v in case["verbatim"]:
        assert R.norm(v) in prompts, f"{case['id']}: {v!r} not found in required session prompts"


def test_trivial_cases_expect_nothing():
    for c in CASES:
        if c["kind"] == "trivial":
            assert c.get("expect_empty") is True
            assert not (c["sessions_required"] or c["files_required"] or c["verbatim"])


def test_no_held_out_material_is_referenced():
    for spec in PROJECTS.values():
        for key in ("project", "sessions"):
            norm = spec[key].replace("\\", "/")
            assert "projects/P2" not in norm and "tasks/P2" not in norm and "tasks/P1/H" not in norm
    with pytest.raises(ValueError):
        R._check_allowed("bench/projects/P2/game")
    with pytest.raises(ValueError):
        R._check_allowed("bench/tasks/P1/H3")

