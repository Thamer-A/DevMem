"""Hidden grading tests for gate task md2conf H01 (long code blocks start collapsed)."""

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from md2conf.local import LocalConverter
from md2conf.metadata import ConfluenceSiteMetadata
from md2conf.options import ProcessorOptions

_CODE_MACRO = re.compile(r'<ac:structured-macro[^>]*ac:name="code"[^>]*>(.*?)</ac:structured-macro>', re.DOTALL)
_COLLAPSED = re.compile(r'<ac:parameter ac:name="collapse">\s*true\s*</ac:parameter>')


def _code(chars: int, line_length: int, tag: str) -> str:
    "Code text of exactly `chars` characters (newlines included), in lines of at most `line_length` characters."

    lines: list[str] = []
    remaining = chars
    while remaining > 0:
        if lines:
            remaining -= 1  # the newline before this line
        take = min(line_length, remaining)
        if take <= 0:
            break
        lines.append((tag + "x" * take)[:take])
        remaining -= take
    code = "\n".join(lines)
    assert len(code) == chars, (len(code), chars)
    return code


def _block(code: str) -> str:
    return f"```python\n{code}\n```\n"


def _convert(markdown: str) -> str:
    with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        source_dir = Path(temp_dir)
        (source_dir / "page.md").write_text(markdown, encoding="utf-8")
        site = ConfluenceSiteMetadata(domain="example.com", base_path="/wiki/", space_key="SPACE_KEY")
        LocalConverter(ProcessorOptions(), site).process(source_dir / "page.md")
        return (source_dir / "page.csf").read_text(encoding="utf-8")


class TestGateH01(unittest.TestCase):
    def _collapsed(self, *codes: str) -> list[bool]:
        markdown = "# Page\n\n" + "\nSome text.\n\n".join(_block(code) for code in codes)
        macros = _CODE_MACRO.findall(_convert(markdown))
        self.assertEqual(len(macros), len(codes))
        return [bool(_COLLAPSED.search(m)) for m in macros]

    def test_just_over_limit_collapsed(self) -> None:
        self.assertEqual(self._collapsed(_code(2530, 250, "a")), [True])

    def test_at_limit_not_collapsed(self) -> None:
        self.assertEqual(self._collapsed(_code(2500, 250, "b")), [False])

    def test_few_long_lines_collapsed(self) -> None:
        self.assertEqual(self._collapsed(_code(4003, 1000, "c")), [True])

    def test_many_short_lines_not_collapsed(self) -> None:
        self.assertEqual(self._collapsed(_code(1300, 20, "d")), [False])

    def test_short_block_not_collapsed(self) -> None:
        self.assertEqual(self._collapsed("print('hello')"), [False])

    def test_mixed_page(self) -> None:
        self.assertEqual(self._collapsed("x = 1", _code(3000, 80, "e"), _code(2400, 12, "f")), [False, True, False])


if __name__ == "__main__":
    unittest.main()
