"""Hidden grading tests for gate task md2conf H07 (--toc-max-level option)."""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent

_PAGE = "# Guide\n\n[[_TOC_]]\n\n## Install\n\n### Windows\n\n#### Details\n\nText.\n\n## Use\n\nText.\n"
_TOC_MACRO = re.compile(r'<ac:structured-macro[^>]*ac:name="toc"[^>]*>(.*?)</ac:structured-macro>', re.DOTALL)


def _convert(flags: list[str], extra_env: dict[str, str]) -> str:
    with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        page = Path(temp_dir) / "guide.md"
        page.write_text(_PAGE, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("MD2CONF_", "CONFLUENCE_"))}
        env["CONFLUENCE_DOMAIN"] = "example.com"
        env["CONFLUENCE_SPACE_KEY"] = "SPACE_KEY"
        env.update(extra_env)
        proc = subprocess.run(
            [sys.executable, "-m", "md2conf", str(page), "--local", *flags],
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
        return page.with_suffix(".csf").read_text(encoding="utf-8")


def _max_level(content: str) -> str | None:
    macros = _TOC_MACRO.findall(content)
    if len(macros) != 1:
        raise AssertionError(f"expected one toc macro, found {len(macros)}")
    m = re.search(r'<ac:parameter ac:name="maxLevel">\s*([^<]*?)\s*</ac:parameter>', macros[0])
    return m.group(1) if m else None


class TestGateH07(unittest.TestCase):
    def test_default_has_no_max_level(self) -> None:
        self.assertIsNone(_max_level(_convert([], {})))

    def test_command_line(self) -> None:
        self.assertEqual(_max_level(_convert(["--toc-max-level", "3"], {})), "3")

    def test_environment_variable(self) -> None:
        self.assertEqual(_max_level(_convert([], {"MD2CONF_TOC_MAX_LEVEL": "2"})), "2")

    def test_command_line_wins(self) -> None:
        self.assertEqual(_max_level(_convert(["--toc-max-level", "3"], {"MD2CONF_TOC_MAX_LEVEL": "2"})), "3")


if __name__ == "__main__":
    unittest.main()
