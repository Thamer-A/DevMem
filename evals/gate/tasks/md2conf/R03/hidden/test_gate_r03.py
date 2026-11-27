"""Hidden grading tests for gate task md2conf R03 (unique anchors for repeated headings)."""

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from md2conf.local import LocalConverter
from md2conf.metadata import ConfluenceSiteMetadata
from md2conf.options import ProcessorOptions
from md2conf.options_converter import ConverterOptions

_ANCHOR = re.compile(r'<ac:structured-macro[^>]*ac:name="anchor"[^>]*>\s*<ac:parameter ac:name="">([^<]*)</ac:parameter>')


def _anchors(files: dict[str, str]) -> dict[str, list[str]]:
    with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        source_dir = Path(temp_dir)
        for name, text in files.items():
            (source_dir / name).write_text(text, encoding="utf-8")
        site = ConfluenceSiteMetadata(domain="example.com", base_path="/wiki/", space_key="SPACE_KEY")
        options = ProcessorOptions(converter=ConverterOptions(heading_anchors=True))
        LocalConverter(options, site).process(source_dir)
        return {name: _ANCHOR.findall((source_dir / name).with_suffix(".csf").read_text(encoding="utf-8")) for name in files}


class TestGateR03(unittest.TestCase):
    def test_repeated_headings(self) -> None:
        page = "# Tools\n\n## Setup\n\nA.\n\n## Usage\n\nB.\n\n## Setup\n\nC.\n\n## Setup\n\nD.\n"
        anchors = _anchors({"index.md": page})["index.md"]
        self.assertEqual(anchors, ["tools", "setup", "usage", "setup-1", "setup-2"])

    def test_distinct_headings_unchanged(self) -> None:
        anchors = _anchors({"index.md": "# Tools\n\n## Install guide\n\n## Usage\n"})["index.md"]
        self.assertEqual(anchors, ["tools", "install-guide", "usage"])

    def test_numbering_is_per_page(self) -> None:
        anchors = _anchors({"index.md": "# One\n\n## Setup\n", "other.md": "# Two\n\n## Setup\n\n## Setup\n"})
        self.assertEqual(anchors["index.md"], ["one", "setup"])
        self.assertEqual(anchors["other.md"], ["two", "setup", "setup-1"])


if __name__ == "__main__":
    unittest.main()
