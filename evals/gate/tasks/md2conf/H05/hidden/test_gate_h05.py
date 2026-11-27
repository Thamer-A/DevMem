"""Hidden grading tests for gate task md2conf H05 (--folder-labels adds folder names as page labels)."""

import contextlib
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

import md2conf.__main__
from md2conf.__main__ import main
from tests.api import MockConfluenceSession


class _RecordingSession(MockConfluenceSession):
    "A mock Confluence session that remembers the labels assigned to each page."

    def __init__(self) -> None:
        super().__init__()
        self.recorded_labels: dict[str, set[str]] = {}

    def _record(self, page_id: str, labels: Any) -> None:
        self.recorded_labels.setdefault(page_id, set()).update(label.name for label in labels)

    def add_labels(self, page_id: str, labels: Any) -> None:  # type: ignore[override]
        self._record(page_id, labels)

    def update_labels(self, page_id: str, labels: Any, *, keep_existing: bool = False) -> None:  # type: ignore[override]
        self._record(page_id, labels)


class _FakeAPI:
    def __init__(self, session: _RecordingSession) -> None:
        self.session = session

    def __enter__(self) -> _RecordingSession:
        return self.session

    def __exit__(self, *args: object) -> None:
        pass


_FILES = {
    "index.md": "# Handbook home\n\nWelcome.\n",
    "User Guide/Getting Started/install.md": "---\ntags: [setup]\n---\n\n# Installing the tool\n\nSteps.\n",
    "User Guide/overview.md": "# Guide overview\n\nText.\n",
    "FAQ/questions.md": "# Common questions\n\nAnswers.\n",
}


def _publish(*flags: str) -> dict[str, set[str]]:
    "Publishes a small directory tree through the command-line entry point; returns labels by page title."

    session = _RecordingSession()
    with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        source_dir = Path(temp_dir) / "docs"
        for name, text in _FILES.items():
            path = source_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("MD2CONF_", "CONFLUENCE_"))}
        env.update({"CONFLUENCE_DOMAIN": "example.atlassian.net", "CONFLUENCE_API_KEY": "not-a-real-key", "CONFLUENCE_USER_NAME": "someone"})
        argv = ["md2conf", str(source_dir), "--skip-update", "--no-render-drawio", "--no-render-mermaid", "--no-render-plantuml", "--no-render-latex", *flags]
        fake = lambda *args, **kwargs: _FakeAPI(session)  # noqa: E731
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            stack.enter_context(mock.patch("md2conf.api.ConfluenceAPI", fake))
            if hasattr(md2conf.__main__, "ConfluenceAPI"):
                stack.enter_context(mock.patch.object(md2conf.__main__, "ConfluenceAPI", fake))
            main()
    by_title: dict[str, set[str]] = {}
    for title in ("Handbook home", "Installing the tool", "Guide overview", "Common questions"):
        page = session.get_page_properties_by_title(title)
        by_title[title] = session.recorded_labels.get(page.id, set())
    return by_title


class TestGateH05(unittest.TestCase):
    def test_folder_labels(self) -> None:
        labels = _publish("--folder-labels")
        self.assertEqual(labels["Installing the tool"], {"setup", "md-userguide", "md-gettingstarted"})
        self.assertEqual(labels["Guide overview"], {"md-userguide"})
        self.assertEqual(labels["Common questions"], {"md-faq"})
        self.assertEqual(labels["Handbook home"], set())

    def test_default_only_tags(self) -> None:
        labels = _publish()
        self.assertEqual(labels["Installing the tool"], {"setup"})
        self.assertEqual(labels["Guide overview"], set())
        self.assertEqual(labels["Common questions"], set())


if __name__ == "__main__":
    unittest.main()
