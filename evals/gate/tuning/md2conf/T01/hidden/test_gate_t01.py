"""Hidden grading tests for gate tuning task md2conf T01 (a box listing broken links)."""

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from md2conf.local import LocalConverter
from md2conf.metadata import ConfluenceSiteMetadata
from md2conf.options import ProcessorOptions
from md2conf.options_converter import ConverterOptions

_MACRO = re.compile(r'<ac:structured-macro[^>]*ac:name="([a-z]+)"[^>]*>(.*?)</ac:structured-macro>', re.DOTALL)


def _convert(page_text: str) -> str:
    with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        source_dir = Path(temp_dir)
        (source_dir / "index.md").write_text(page_text, encoding="utf-8")
        (source_dir / "present.md").write_text("# Present page\n\nHere.\n", encoding="utf-8")
        site = ConfluenceSiteMetadata(domain="example.com", base_path="/wiki/", space_key="SPACE_KEY")
        options = ProcessorOptions(converter=ConverterOptions(force_valid_url=False))
        LocalConverter(options, site).process(source_dir)
        return (source_dir / "index.csf").read_text(encoding="utf-8")


class TestGateT01(unittest.TestCase):
    def test_box_lists_broken_links(self) -> None:
        content = _convert("# Overview\n\nSee [setup](missing-setup.md), [the page](present.md) and [old notes](archive/old-notes.md).\n")
        boxes = [body for name, body in _MACRO.findall(content) if "missing-setup.md" in body]
        names = [name for name, body in _MACRO.findall(content) if "missing-setup.md" in body]
        self.assertEqual(names, ["note"], names)
        self.assertIn("old-notes.md", boxes[0])
        self.assertNotIn("present.md", boxes[0])

    def test_no_box_without_broken_links(self) -> None:
        content = _convert("# Overview\n\nSee [the page](present.md).\n")
        self.assertEqual([name for name, _ in _MACRO.findall(content) if name in ("note", "warning")], [])


if __name__ == "__main__":
    unittest.main()
