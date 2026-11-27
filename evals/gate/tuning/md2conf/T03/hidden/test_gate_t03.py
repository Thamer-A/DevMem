"""Hidden grading tests for gate tuning task md2conf T03 (--banner shows an announcement box on every page)."""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent

_MACRO = re.compile(r'<ac:structured-macro[^>]*ac:name="([a-z]+)"[^>]*>(.*?)</ac:structured-macro>', re.DOTALL)
_BANNER = "This space moves to DOCS2 on 1 March."


def _convert(*flags: str) -> dict[str, str]:
    with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        source_dir = Path(temp_dir) / "docs"
        source_dir.mkdir()
        (source_dir / "index.md").write_text("# Home\n\nWelcome.\n", encoding="utf-8")
        (source_dir / "faq.md").write_text("# FAQ\n\nAnswers.\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("MD2CONF_", "CONFLUENCE_"))}
        env["CONFLUENCE_DOMAIN"] = "example.com"
        env["CONFLUENCE_SPACE_KEY"] = "SPACE_KEY"
        proc = subprocess.run(
            [sys.executable, "-m", "md2conf", str(source_dir), "--local", *flags],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if proc.returncode != 0:
            raise AssertionError(f"md2conf exited with {proc.returncode}: {proc.stdout[-1500:]} {proc.stderr[-1500:]}")
        return {name: (source_dir / name).read_text(encoding="utf-8") for name in ("index.csf", "faq.csf")}


class TestGateT03(unittest.TestCase):
    def test_banner_on_every_page(self) -> None:
        for name, content in _convert("--banner", _BANNER).items():
            with self.subTest(page=name):
                names = [macro for macro, body in _MACRO.findall(content) if _BANNER in body]
                self.assertEqual(names, ["note"], content)

    def test_no_banner_by_default(self) -> None:
        for content in _convert().values():
            self.assertEqual([macro for macro, _ in _MACRO.findall(content) if macro != "info"], [])


if __name__ == "__main__":
    unittest.main()
