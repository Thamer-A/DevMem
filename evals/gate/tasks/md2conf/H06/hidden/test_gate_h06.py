"""Hidden grading tests for gate task md2conf H06 (per-page code_line_numbers setting in the front matter)."""

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from md2conf.local import LocalConverter
from md2conf.metadata import ConfluenceSiteMetadata
from md2conf.options import ProcessorOptions
from md2conf.options_converter import ConverterOptions

_LINE_NUMBERS = re.compile(r'<ac:parameter ac:name="linenumbers">\s*true\s*</ac:parameter>')

_BODY = "# Sample\n\n```python\nprint('hello')\nprint('world')\n```\n"


def _convert(front_matter: str, *, site_wide: bool) -> str:
    with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        page = Path(temp_dir) / "page.md"
        page.write_text(front_matter + _BODY, encoding="utf-8")
        site = ConfluenceSiteMetadata(domain="example.com", base_path="/wiki/", space_key="SPACE_KEY")
        options = ProcessorOptions(converter=ConverterOptions(code_line_numbers=site_wide))
        LocalConverter(options, site).process(page)
        return page.with_suffix(".csf").read_text(encoding="utf-8")


class TestGateH06(unittest.TestCase):
    def test_page_turns_line_numbers_on(self) -> None:
        content = _convert("---\ntitle: Sample\nmd2conf:\n  code_line_numbers: true\n---\n\n", site_wide=False)
        self.assertRegex(content, _LINE_NUMBERS)
        self.assertIn("print('world')", content)

    def test_page_turns_line_numbers_off(self) -> None:
        content = _convert("---\nmd2conf:\n  code_line_numbers: false\n---\n\n", site_wide=True)
        self.assertNotRegex(content, _LINE_NUMBERS)

    def test_no_setting_follows_option(self) -> None:
        self.assertNotRegex(_convert("", site_wide=False), _LINE_NUMBERS)
        self.assertRegex(_convert("", site_wide=True), _LINE_NUMBERS)
        self.assertRegex(_convert("---\ntitle: Other\n---\n\n", site_wide=True), _LINE_NUMBERS)


if __name__ == "__main__":
    unittest.main()
