"""Hidden grading tests for gate task md2conf R01 (code block language aliases, case-insensitive names)."""

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from md2conf.local import LocalConverter
from md2conf.metadata import ConfluenceSiteMetadata
from md2conf.options import ProcessorOptions
from md2conf.options_converter import ConverterOptions

_LANGUAGE = re.compile(r'<ac:parameter ac:name="language">([^<]*)</ac:parameter>')


def _languages(names: list[str], *, force_valid_language: bool = True) -> list[str]:
    blocks = "\n".join(f"```{name}\nsome code\n```\n" for name in names)
    with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        page = Path(temp_dir) / "page.md"
        page.write_text("# Code\n\n" + blocks, encoding="utf-8")
        site = ConfluenceSiteMetadata(domain="example.com", base_path="/wiki/", space_key="SPACE_KEY")
        options = ProcessorOptions(converter=ConverterOptions(force_valid_language=force_valid_language))
        LocalConverter(options, site).process(page)
        return _LANGUAGE.findall(page.with_suffix(".csf").read_text(encoding="utf-8"))


class TestGateR01(unittest.TestCase):
    def test_new_aliases(self) -> None:
        self.assertEqual(
            _languages(["sh", "zsh", "console", "yml", "ps1", "ts"]),
            ["bash", "bash", "bash", "yaml", "powershell", "typescript"],
        )

    def test_case_insensitive(self) -> None:
        self.assertEqual(_languages(["Python", "YAML", "Bash", "SH", "Yml", "JavaScript"]), ["py", "yaml", "bash", "bash", "yaml", "js"])

    def test_existing_names_unchanged(self) -> None:
        self.assertEqual(_languages(["python", "shell", "bash", "json", "csharp"]), ["py", "shell", "bash", "json", "c#"])

    def test_unknown_language(self) -> None:
        self.assertEqual(_languages(["brainfork"]), ["none"])
        self.assertEqual(_languages(["Brainfork"], force_valid_language=False), ["Brainfork"])


if __name__ == "__main__":
    unittest.main()
