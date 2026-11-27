"""Hidden grading tests for gate task md2conf H02 (size check for attached images)."""

import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from md2conf.local import LocalConverter
from md2conf.metadata import ConfluenceSiteMetadata
from md2conf.options import ConfluencePageID, ProcessorOptions
from md2conf.options_converter import ConverterOptions
from md2conf.publisher import Publisher
from tests.api import MockConfluenceAPI

_PNG = (Path(__file__).parent / "source" / "figure" / "raster.png").read_bytes()
_MIB = 1024 * 1024
_PAGE = "# Screenshots\n\nThe dialog:\n\n![dialog](shot.png)\n"


def _write(source_dir: Path, image_size: int) -> None:
    (source_dir / "shot.png").write_bytes(_PNG + bytes(image_size - len(_PNG)))
    (source_dir / "index.md").write_text(_PAGE, encoding="utf-8")


def _convert_local(image_size: int) -> str:
    "Converts the page with --local; returns the CSF output."

    with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        source_dir = Path(temp_dir)
        _write(source_dir, image_size)
        site = ConfluenceSiteMetadata(domain="example.com", base_path="/wiki/", space_key="SPACE_KEY")
        LocalConverter(ProcessorOptions(), site).process(source_dir)
        return (source_dir / "index.csf").read_text(encoding="utf-8")


def _publish(image_size: int) -> str:
    "Publishes the page to a mock Confluence; returns the stored page body."

    with MockConfluenceAPI() as api, TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        source_dir = Path(temp_dir)
        _write(source_dir, image_size)
        options = ProcessorOptions(
            root_page=ConfluencePageID(api.get_homepage_id("SPACE_ID")),
            skip_update=True,
            converter=ConverterOptions(render_drawio=False, render_mermaid=False, render_plantuml=False, render_latex=False),
        )
        Publisher(api, options).process_directory(source_dir)
        page = api.get_page_properties_by_title("Screenshots")
        return api.get_page(page.id).content


def _warnings(action) -> tuple[list[logging.LogRecord], object]:
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                records.append(record)

    handler = _Collect(level=logging.WARNING)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        result = action()
    finally:
        root.removeHandler(handler)
    return records, result


class TestGateH02(unittest.TestCase):
    def _check_flagged(self, image_size: int) -> None:
        "Either the conversion or the upload must log a warning, and neither may fail or drop the image."

        local_warnings, content = _warnings(lambda: _convert_local(image_size))
        self.assertIn('ri:filename="shot.png"', content)
        publish_warnings, body = _warnings(lambda: _publish(image_size))
        self.assertIn('ri:filename="shot.png"', body)
        self.assertTrue(any(r.levelno == logging.WARNING for r in local_warnings + publish_warnings))

    def _check_quiet(self, image_size: int) -> None:
        local_warnings, _ = _warnings(lambda: _convert_local(image_size))
        publish_warnings, _ = _warnings(lambda: _publish(image_size))
        self.assertEqual([r.getMessage() for r in local_warnings + publish_warnings], [])

    def test_over_limit_warns(self) -> None:
        self._check_flagged(7 * _MIB + 1)

    def test_far_over_limit_warns(self) -> None:
        self._check_flagged(12 * _MIB)

    def test_at_limit_no_warning(self) -> None:
        self._check_quiet(7 * _MIB)

    def test_seven_decimal_megabytes_no_warning(self) -> None:
        self._check_quiet(7_000_001)

    def test_six_mebibytes_no_warning(self) -> None:
        self._check_quiet(6 * _MIB)

    def test_small_image_no_warning(self) -> None:
        self._check_quiet(len(_PNG))


if __name__ == "__main__":
    unittest.main()
