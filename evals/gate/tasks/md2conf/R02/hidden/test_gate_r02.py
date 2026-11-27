"""Hidden grading tests for gate task md2conf R02 ([TOC] marker for a table of contents)."""

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from md2conf.local import LocalConverter
from md2conf.metadata import ConfluenceSiteMetadata
from md2conf.options import ProcessorOptions

_TOC_MACRO = re.compile(r'<ac:structured-macro[^>]*ac:name="toc"')


def _convert(markdown: str) -> str:
    with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        page = Path(temp_dir) / "page.md"
        page.write_text(markdown, encoding="utf-8")
        site = ConfluenceSiteMetadata(domain="example.com", base_path="/wiki/", space_key="SPACE_KEY")
        LocalConverter(ProcessorOptions(), site).process(page)
        return page.with_suffix(".csf").read_text(encoding="utf-8")


class TestGateR02(unittest.TestCase):
    def test_toc_marker(self) -> None:
        content = _convert("# Guide\n\n[TOC]\n\n## Install\n\nText.\n\n## Use\n\nText.\n")
        self.assertEqual(len(_TOC_MACRO.findall(content)), 1)
        self.assertNotIn("[TOC]", content)

    def test_same_macro_as_gitlab_marker(self) -> None:
        a = _convert("# Guide\n\n[TOC]\n\n## Install\n")
        b = _convert("# Guide\n\n[[_TOC_]]\n\n## Install\n")
        self.assertEqual(a, b)

    def test_marker_in_sentence_and_code_stays_text(self) -> None:
        content = _convert("# Guide\n\nIn MkDocs you write [TOC] on its own line.\n\n```\n[TOC]\n```\n\nAlso `[TOC]` inline.\n")
        self.assertEqual(len(_TOC_MACRO.findall(content)), 0)
        self.assertIn("In MkDocs you write [TOC] on its own line.", content)
        self.assertIn("<![CDATA[[TOC]]]>", content)
        self.assertIn("<code>[TOC]</code>", content)

    def test_gitlab_marker_still_works(self) -> None:
        content = _convert("# Guide\n\n[[_TOC_]]\n\n## Install\n")
        self.assertEqual(len(_TOC_MACRO.findall(content)), 1)


if __name__ == "__main__":
    unittest.main()
