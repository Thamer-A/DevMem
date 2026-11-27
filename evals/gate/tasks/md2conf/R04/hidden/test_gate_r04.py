"""Hidden grading tests for gate task md2conf R04 (--skip-title-heading when the front-matter title repeats the heading)."""

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from md2conf.local import LocalConverter
from md2conf.metadata import ConfluenceSiteMetadata
from md2conf.options import ProcessorOptions
from md2conf.options_converter import ConverterOptions

_BODY = "# Install Guide\n\nHow to install.\n\n## Requirements\n\nPython.\n"
_HEADING = re.compile(r"<h1[^>]*>.*?Install Guide.*?</h1>", re.DOTALL)


def _convert(front_matter: str, *, skip: bool, body: str = _BODY) -> str:
    with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        page = Path(temp_dir) / "page.md"
        page.write_text(front_matter + body, encoding="utf-8")
        site = ConfluenceSiteMetadata(domain="example.com", base_path="/wiki/", space_key="SPACE_KEY")
        options = ProcessorOptions(converter=ConverterOptions(skip_title_heading=skip))
        LocalConverter(options, site).process(page)
        return page.with_suffix(".csf").read_text(encoding="utf-8")


class TestGateR04(unittest.TestCase):
    def test_same_title_removed(self) -> None:
        content = _convert("---\ntitle: Install Guide\n---\n\n", skip=True)
        self.assertNotRegex(content, _HEADING)
        self.assertIn("Requirements", content)
        self.assertIn("How to install.", content)

    def test_title_differs_in_case_and_spaces_removed(self) -> None:
        content = _convert('---\ntitle: "  install guide "\n---\n\n', skip=True)
        self.assertNotRegex(content, _HEADING)
        self.assertIn("Requirements", content)

    def test_different_title_kept(self) -> None:
        content = _convert("---\ntitle: Installing the tool\n---\n\n", skip=True)
        self.assertRegex(content, _HEADING)

    def test_option_off_kept(self) -> None:
        content = _convert("---\ntitle: Install Guide\n---\n\n", skip=False)
        self.assertRegex(content, _HEADING)

    def test_no_front_matter_still_removed(self) -> None:
        content = _convert("", skip=True)
        self.assertNotRegex(content, _HEADING)
        self.assertIn("Requirements", content)


if __name__ == "__main__":
    unittest.main()
