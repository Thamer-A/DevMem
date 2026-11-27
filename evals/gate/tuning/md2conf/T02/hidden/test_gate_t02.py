"""Hidden grading tests for gate tuning task md2conf T02 (--reading-time shows an estimated reading time)."""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent

_WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliett"]


def _prose(count: int) -> str:
    return " ".join(_WORDS[i % len(_WORDS)] for i in range(count)) + "."


def _convert(word_count: int, *flags: str) -> str:
    with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        page = Path(temp_dir) / "notes.md"
        page.write_text("# Notes\n\n" + _prose(word_count) + "\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("MD2CONF_", "CONFLUENCE_"))}
        env["CONFLUENCE_DOMAIN"] = "example.com"
        env["CONFLUENCE_SPACE_KEY"] = "SPACE_KEY"
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


def _minutes(content: str) -> list[int]:
    return [int(m) for m in re.findall(r"(\d+)\s*min", content)]


class TestGateT02(unittest.TestCase):
    def test_450_words(self) -> None:
        self.assertEqual(_minutes(_convert(450, "--reading-time")), [3])

    def test_1050_words(self) -> None:
        self.assertEqual(_minutes(_convert(1050, "--reading-time")), [6])

    def test_off_by_default(self) -> None:
        self.assertEqual(_minutes(_convert(450)), [])


if __name__ == "__main__":
    unittest.main()
