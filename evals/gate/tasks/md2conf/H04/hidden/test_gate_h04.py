"""Hidden grading tests for gate task md2conf H04 (--skip-drafts leaves draft pages out)."""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent

_FILES = {
    "index.md": "# Handbook\n\nWelcome.\n",
    "ready.md": "# Ready page\n\nPublished text.\n",
    "plans.draft.md": "# Plans\n\nNot ready yet.\n",
    "_sidebar.md": "# Sidebar\n\nLinks.\n",
    "guide/intro.md": "# Intro\n\nHello.\n",
    "guide/wip.draft.md": "# Work in progress\n\nHalf done.\n",
}


def _run(source_dir: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("MD2CONF_", "CONFLUENCE_"))}
    env["CONFLUENCE_DOMAIN"] = "example.com"
    env["CONFLUENCE_SPACE_KEY"] = "SPACE_KEY"
    return subprocess.run(
        [sys.executable, "-m", "md2conf", str(source_dir), "--local", *flags],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


class TestGateH04(unittest.TestCase):
    def _tree(self, temp_dir: str) -> Path:
        source_dir = Path(temp_dir) / "docs"
        for name, text in _FILES.items():
            path = source_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return source_dir

    def test_skip_drafts(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            source_dir = self._tree(temp_dir)
            proc = _run(source_dir, "--skip-drafts")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue((source_dir / "ready.csf").is_file())
            self.assertTrue((source_dir / "_sidebar.csf").is_file())
            self.assertTrue((source_dir / "guide" / "intro.csf").is_file())
            self.assertFalse((source_dir / "plans.draft.csf").exists())
            self.assertFalse((source_dir / "guide" / "wip.draft.csf").exists())

    def test_default_keeps_everything(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            source_dir = self._tree(temp_dir)
            proc = _run(source_dir)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue((source_dir / "ready.csf").is_file())
            self.assertTrue((source_dir / "plans.draft.csf").is_file())
            self.assertTrue((source_dir / "guide" / "wip.draft.csf").is_file())


if __name__ == "__main__":
    unittest.main()
