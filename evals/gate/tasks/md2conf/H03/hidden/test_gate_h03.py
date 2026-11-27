"""Hidden grading tests for gate task md2conf H03 (%{modified} placeholder in the generated-by text)."""

import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from md2conf.local import LocalConverter
from md2conf.metadata import ConfluenceSiteMetadata
from md2conf.options import Markdown, ProcessorOptions


def _convert(modified: datetime, template: str, *, sub_dir: str = "") -> str:
    with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        source_dir = Path(temp_dir)
        page_dir = source_dir / sub_dir if sub_dir else source_dir
        page_dir.mkdir(parents=True, exist_ok=True)
        page = page_dir / "page.md"
        page.write_text("# Release notes\n\nWhat changed.\n", encoding="utf-8")
        stamp = modified.timestamp()
        os.utime(page, (stamp, stamp))
        site = ConfluenceSiteMetadata(domain="example.com", base_path="/wiki/", space_key="SPACE_KEY")
        LocalConverter(ProcessorOptions(generated_by=Markdown(template)), site).process(source_dir)
        return page.with_suffix(".csf").read_text(encoding="utf-8")


class TestGateH03(unittest.TestCase):
    def test_single_digit_day(self) -> None:
        content = _convert(datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc), "Last changed: %{modified}")
        self.assertIn("Last changed: 5 Mar 2026", content)
        self.assertNotIn("%{modified}", content)

    def test_two_digit_day(self) -> None:
        content = _convert(datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc), "Last changed: %{modified}")
        self.assertIn("Last changed: 26 Nov 2026", content)

    def test_with_other_placeholders(self) -> None:
        content = _convert(datetime(2025, 9, 1, 12, 0, tzinfo=timezone.utc), "%{filename} changed %{modified}", sub_dir="notes")
        self.assertIn("page.md changed 1 Sep 2025", content)


if __name__ == "__main__":
    unittest.main()
