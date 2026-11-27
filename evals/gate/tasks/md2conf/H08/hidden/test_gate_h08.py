"""Hidden grading tests for gate task md2conf H08 (warning for images without alt text)."""

import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from md2conf.local import LocalConverter
from md2conf.metadata import ConfluenceSiteMetadata
from md2conf.options import ProcessorOptions

_PNG = (Path(__file__).parent / "source" / "figure" / "raster.png").read_bytes()


def _convert(files: dict[str, str]) -> tuple[Path, list[logging.LogRecord]]:
    "Converts a directory with the given Markdown files (each folder gets a pic.png); returns the warnings logged."

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                records.append(record)

    handler = _Collect(level=logging.WARNING)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            source_dir = Path(temp_dir) / "docs"
            for name, text in files.items():
                path = source_dir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                (path.parent / "pic.png").write_bytes(_PNG)
            site = ConfluenceSiteMetadata(domain="example.com", base_path="/wiki/", space_key="SPACE_KEY")
            LocalConverter(ProcessorOptions(), site).process(source_dir)
            for name in files:
                if not (source_dir / name).with_suffix(".csf").is_file():
                    raise AssertionError(f"no output for {name}")
    finally:
        root.removeHandler(handler)
    return source_dir, records


class TestGateH08(unittest.TestCase):
    def test_missing_alt_text_warns_with_path_prefix(self) -> None:
        _, records = _convert(
            {
                "index.md": "# Home\n\n![Company logo](pic.png)\n",
                "guide/page.md": "# Setup guide\n\nFirst:\n\n![](pic.png)\n\nThen:\n\n![A chart of the steps](pic.png)\n\nLast:\n\n![   ](pic.png)\n",
            }
        )
        messages = [r.getMessage() for r in records]
        self.assertGreaterEqual(len(messages), 2, messages)
        for message in messages:
            self.assertTrue(message.startswith("guide/page.md: "), message)

    def test_alt_text_present_no_warning(self) -> None:
        _, records = _convert(
            {
                "index.md": "# Home\n\n![Company logo](pic.png)\n",
                "guide/page.md": "# Setup guide\n\n![A chart of the steps](pic.png)\n",
            }
        )
        self.assertEqual([r.getMessage() for r in records], [])


if __name__ == "__main__":
    unittest.main()
