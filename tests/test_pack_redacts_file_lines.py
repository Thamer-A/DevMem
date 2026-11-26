"""A pack quotes project file lines redacted (verifier round 5: `L1: GITHUB_TOKEN = "ghp_..."` from a project file
reached the pack, which is pasted into other tools and sent to models). Project files themselves are indexed as they
are; only what a pack quotes from them is redacted. The token is built at run time."""
from __future__ import annotations

from pack_helpers import make_project

from devmem.pack import build_pack
from devmem.scan import scan

TOKEN = "gh" + "p_" + "PACKLINE" + "Q" * 28


def test_quoted_file_lines_are_redacted(tmp_path):
    project, store = make_project(tmp_path, {
        "src/config.py": f'GITHUB_TOKEN = "{TOKEN}"\nDEPLOY_TARGET = "prod"\n\ndef deploy_token():\n    return GITHUB_TOKEN\n',
        "src/deploy.js": f"export const deployToken = '{TOKEN}';\nexport function deployNow() {{ return 1 }}\n",
    })
    scan(project, store)
    text, info = build_pack(project, store, "deploy token github config", budget=1500)
    assert info.get("files"), text           # the files were quoted
    assert TOKEN not in text and "PACKLINE" not in text, text
    assert "[REDACTED:github-token]" in text
