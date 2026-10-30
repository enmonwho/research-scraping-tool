"""Offscreen Qt tests for the desktop research-discovery interface."""

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer, Qt, QUrl
from PySide6.QtWidgets import QApplication, QMessageBox

from app.gui.controller import ResearchController, SearchResult
from app.gui.window import ResearchWindow, safe_http_url
from app.models import (DownloadableFile, LinkType, ResearchResource,
                        ResourceLink, ResourceType)


def resource(title: str = "Urban heat study", *, provider: str = "OpenAlex",
             external_id: str = "W1", doi: str | None = "10.1000/example") -> ResearchResource:
    return ResearchResource(
        resource_type=ResourceType.PAPER, title=title, description="Study description",
        authors=("A. Researcher",), publication_year=2024, source_provider=provider,
        external_id=external_id, doi=doi, keywords=("urban heat",),
        retrieved_at=datetime.now(timezone.utc),
        links=(ResourceLink(LinkType.LANDING_PAGE, "https://example.org/paper", provider),),
        downloadable_files=(DownloadableFile("https://example.org/paper.pdf", "pdf",
                                              "application/pdf", "paper.pdf", provider, True),),
    )


class QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def wait_for(self, predicate, timeout_ms: int = 5000) -> None:
        if predicate():
            return
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: loop.quit() if predicate() else None)
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        timer.start()
        deadline.start(timeout_ms)
        loop.exec()
        timer.stop()
        deadline.stop()
        self.assertTrue(predicate(), "Qt worker did not finish before timeout")


class ResearchControllerTests(QtTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "research.db"
        self.openalex_calls: list[tuple[str, dict]] = []

        def openalex(topic: str, **options):
            self.openalex_calls.append((topic, options))
            return [resource()]

        self.controller = ResearchController(self.db, openalex_provider=openalex)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_search_saves_result_and_history_show_reads_same_snapshot(self) -> None:
        result = self.controller.search(topic="  urban heat  ", limit=3, timeout=4)
        self.assertEqual(result.topic, "urban heat")
        self.assertEqual(result.resources[0].title, "Urban heat study")
        self.assertEqual(self.openalex_calls[0], ("urban heat", {"limit": 3, "timeout": 4}))
        summaries = self.controller.history()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].id, result.search_id)
        shown = self.controller.show(result.search_id)
        self.assertEqual(shown.resources, result.resources)

    def test_validates_topic_and_search_bounds(self) -> None:
        for options in ({"topic": "   "}, {"topic": "x" * 201},
                        {"topic": "valid", "limit": 0}, {"topic": "valid", "timeout": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.controller.search(**options)
        self.assertEqual(self.openalex_calls, [])

    def test_combined_provider_search_uses_both_injected_adapters(self) -> None:
        crossref_calls = []

        def crossref(topic: str, **options):
            crossref_calls.append((topic, options))
            return [resource("Same paper", provider="Crossref", external_id="10.1000/example")]

        controller = ResearchController(self.db, openalex_provider=lambda *_args, **_kwargs: [resource("Same paper")],
                                        crossref_provider=crossref)
        result = controller.search(topic="urban heat", provider="all", limit=2)
        self.assertEqual(len(result.resources), 1)
        self.assertEqual(result.metadata["providers"], ["OpenAlex", "Crossref"])
        self.assertEqual(crossref_calls[0][0], "urban heat")
        self.assertEqual(crossref_calls[0][1]["limit"], 2)

    def test_dataset_search_uses_dataverse_injected_provider(self) -> None:
        dataset = resource("Urban heat dataset")
        dataset = ResearchResource(**{**dataset.__dict__, "resource_type": ResourceType.DATASET,
                                      "source_provider": "Harvard Dataverse"})
        dataset_calls = []

        def dataverse(topic: str, **options):
            dataset_calls.append((topic, options))
            return [dataset]

        controller = ResearchController(self.db, dataset_provider=dataverse)
        result = controller.search(topic="heat dataset", resource_type="datasets")
        self.assertEqual(result.resources[0].resource_type, ResourceType.DATASET)
        self.assertEqual(dataset_calls[0][0], "heat dataset")

    def test_model_failure_returns_saved_unranked_search_results(self) -> None:
        self.controller._rank = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("model weights are unavailable"))
        result = self.controller.search(topic="urban heat", ranking="semantic")
        self.assertEqual(len(result.resources), 1)
        self.assertEqual(result.resources[0].title, "Urban heat study")
        self.assertEqual(result.ranking_error, "model weights are unavailable")
        restored = self.controller.show(result.search_id)
        self.assertEqual(restored.resources[0].title, "Urban heat study")

    def test_package_import_does_not_import_qt(self) -> None:
        code = "import app.gui, sys; assert not any(name == 'PySide6' or name.startswith('PySide6.') for name in sys.modules)"
        subprocess.run([sys.executable, "-c", code], check=True)


class ResearchWindowTests(QtTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "research.db"
        self.controller = ResearchController(self.db, openalex_provider=lambda topic, **kwargs: [resource()])
        self.window = ResearchWindow(controller=self.controller)

    def tearDown(self) -> None:
        if getattr(self, "window", None) is not None:
            self.wait_for(lambda: not self.window._workers)
        self.window.close()
        self.temp.cleanup()

    def test_topic_validation_happens_before_worker_submission(self) -> None:
        self.window.topic_edit.setText("   ")
        with patch.object(QMessageBox, "warning") as warning:
            self.window._search()
        warning.assert_called_once()
        self.assertEqual(self.window.status_label.text(), "topic cannot be blank")
        self.assertFalse(self.window._workers)

    def test_grid_surface_and_narrow_window_stack_detail_pane(self) -> None:
        self.assertEqual(self.window.results_table.columnCount(), 6)
        self.assertTrue(self.window.results_table.showGrid())
        self.window.show()
        self.app.processEvents()
        self.window.resize(700, 700)
        self.app.processEvents()
        self.assertEqual(self.window._splitter.orientation(), Qt.Orientation.Vertical)
        self.window.resize(1100, 700)
        self.app.processEvents()
        self.assertEqual(self.window._splitter.orientation(), Qt.Orientation.Horizontal)

    def test_advanced_search_settings_are_collapsed_until_requested(self) -> None:
        self.assertEqual(self.window.type_combo.currentData(), "papers")
        self.assertTrue(self.window.advanced_panel.isHidden())
        self.window.advanced_toggle.setChecked(True)
        self.app.processEvents()
        self.assertFalse(self.window.advanced_panel.isHidden())
        self.assertEqual(self.window.advanced_toggle.text(), "Advanced search settings  −")

    def test_saved_search_actions_are_grouped_in_actions_menu(self) -> None:
        labels = {action.text() for action in self.window.actions_menu.actions()}
        self.assertEqual(labels, {"Refresh history", "Export CSV…", "Export JSON…",
                                  "Download selected file…", "Choose database…"})
        self.assertIn("Start with a topic", self.window.detail_view.toHtml())

    def test_menu_helper_buttons_stay_hidden_when_window_is_shown(self) -> None:
        self.window.show()
        self.app.processEvents()
        for button in (self.window.database_button, self.window.refresh_history_button,
                       self.window.export_csv_button, self.window.export_json_button,
                       self.window.download_button):
            with self.subTest(button=button.text()):
                self.assertTrue(button.isHidden())
                self.assertFalse(button.isVisible())

    def test_user_can_choose_existing_cli_database(self) -> None:
        selected_db = self.db.parent / "existing-cli.db"
        ResearchController(selected_db, openalex_provider=lambda topic, **kwargs: [resource()]).search(
            topic="existing cli history")
        with patch("app.gui.window.QFileDialog.getOpenFileName", return_value=(str(selected_db), "Research databases")):
            self.window._choose_database()
        self.assertEqual(self.window.controller.db_path, selected_db)
        self.wait_for(lambda: not self.window._workers)
        self.assertEqual(self.window.history_combo.count(), 1)
        self.assertIn("existing cli history", self.window.history_combo.itemText(0))

    def test_dataset_mode_uses_dataverse_even_if_paper_provider_was_selected(self) -> None:
        dataset = ResearchResource(
            resource_type=ResourceType.DATASET, title="Heat dataset", description=None,
            authors=None, publication_year=None, source_provider="Harvard Dataverse",
            external_id="doi:10.1000/dataset", doi="10.1000/dataset", keywords=None,
            retrieved_at=datetime.now(timezone.utc),
        )
        self.controller.dataset_provider = lambda topic, **kwargs: [dataset]
        seen: list[dict] = []
        original_search = self.controller.search

        def track_search(**kwargs):
            seen.append(kwargs)
            return original_search(**kwargs)

        self.controller.search = track_search
        self.window.topic_edit.setText("heat dataset")
        self.window.type_combo.setCurrentIndex(1)
        self.window.provider_combo.setCurrentIndex(2)
        self.window._search()
        self.wait_for(lambda: self.window.current_result is not None)
        self.assertEqual(seen[0]["provider"], "openalex")
        self.assertEqual(self.window.current_result.resources[0].resource_type, ResourceType.DATASET)

    def test_search_runs_in_background_and_renders_numbered_details_and_links(self) -> None:
        main_thread = threading.get_ident()
        worker_thread_ids: list[int] = []
        original_search = self.controller.search

        def track_search(**kwargs):
            worker_thread_ids.append(threading.get_ident())
            return original_search(**kwargs)

        self.controller.search = track_search
        rank_thread_ids: list[int] = []

        def fake_rank(topic, resources, search_id, metadata, ranking, model_id, model_cache):
            rank_thread_ids.append(threading.get_ident())
            return resources, metadata, {}

        self.controller._rank = fake_rank
        self.window.topic_edit.setText("urban heat")
        self.window.ranking_combo.setCurrentIndex(1)
        self.window._search()
        self.wait_for(lambda: self.window.current_result is not None)
        self.wait_for(lambda: self.window.history_combo.count() == 1)
        self.assertEqual(self.window.results_table.rowCount(), 1)
        self.assertEqual(self.window.results_table.item(0, 0).text(), "1")
        self.assertIn("Urban heat study", self.window.results_table.item(0, 1).text())
        self.assertIn("https://example.org/paper", self.window.detail_view.toHtml())
        self.assertTrue(worker_thread_ids)
        self.assertNotEqual(worker_thread_ids[0], main_thread)
        self.assertTrue(rank_thread_ids)
        self.assertNotEqual(rank_thread_ids[0], main_thread)

    def test_download_operation_runs_off_ui_thread_after_explicit_file_choice(self) -> None:
        result = self.controller.search(topic="urban heat")
        self.window._accept_search(result)
        self.window.results_table.setCurrentCell(0, 0)
        main_thread = threading.get_ident()
        observed: list[int] = []

        def fake_download(*args, **kwargs):
            observed.append(threading.get_ident())
            return self.db.parent / "paper.pdf"

        self.controller.download = fake_download
        with patch("app.gui.window.QInputDialog.getItem", return_value=(
                "1. paper.pdf · pdf · provider-marked public", True)):
            self.window._download()
        self.wait_for(lambda: bool(observed))
        self.assertNotEqual(observed[0], main_thread)

    def test_ranking_failure_keeps_search_results_visible_and_status_is_explicit(self) -> None:
        saved = self.controller.search(topic="urban heat")
        errored = SearchResult(saved.search_id, saved.topic, saved.resources, saved.metadata, {},
                              "No sentence-transformers installation")
        self.window._accept_search(errored)
        self.assertEqual(self.window.results_table.rowCount(), 1)
        self.assertIn("Ranking failed: No sentence-transformers installation", self.window.status_label.text())
        self.assertIn("Saved search results are preserved", self.window.detail_view.toHtml())

    def test_only_http_urls_are_rendered_and_opened(self) -> None:
        for url in ("https://example.org/paper", "http://example.org/a"):
            self.assertTrue(safe_http_url(url))
        for url in ("javascript:alert(1)", "file:///C:/secret", "data:text/html,hi",
                    "https://user:pass@example.org/", "https://example.org:bad/"):
            self.assertFalse(safe_http_url(url))
        self.window._open_external_url(QUrl("file:///C:/secret"))
        self.assertIn("Blocked a source link", self.window.status_label.text())
        with patch("app.gui.window.QDesktopServices.openUrl") as open_url:
            self.window._open_external_url(QUrl("https://example.org/paper"))
        open_url.assert_called_once()

    def test_unsupported_provider_url_is_not_made_clickable(self) -> None:
        unsafe = ResearchResource(
            resource_type=ResourceType.PAPER, title="Unsafe URL paper", description=None,
            authors=None, publication_year=None, source_provider="fixture", external_id="id",
            doi=None, keywords=None, retrieved_at=datetime.now(timezone.utc),
            links=(ResourceLink(LinkType.LANDING_PAGE, "file:///C:/secret", "fixture"),),
        )
        result = SearchResult(8, "topic", [unsafe], {}, {})
        self.window._accept_search(result)
        self.assertIn("unsupported URL scheme", self.window.detail_view.toHtml())
        self.assertNotIn('href="file:///C:/secret"', self.window.detail_view.toHtml())

    def test_paper_page_fallback_is_prominent_when_no_direct_file_is_available(self) -> None:
        page_url = "https://journal.example.org/articles/heat-study"
        paper = ResearchResource(
            resource_type=ResourceType.PAPER, title="Paper without direct file",
            description=None, authors=None, publication_year=2024,
            source_provider="Crossref", external_id="10.1000/paper-page", doi="10.1000/paper-page",
            keywords=None, retrieved_at=datetime.now(timezone.utc),
            links=(ResourceLink(LinkType.PUBLISHER, page_url, "Crossref"),),
            downloadable_files=(),
        )
        self.window._accept_search(SearchResult(21, "paper page", [paper], {}, {}))
        rendered = self.window.detail_view.toHtml()
        self.assertIn('href="https://journal.example.org/articles/heat-study"', rendered)
        self.assertIn("Open paper page", rendered)
        self.assertIn("No direct file was supplied by the provider", rendered)
        self.assertTrue(safe_http_url(page_url))

    def test_paper_without_page_links_shows_no_page_message_without_inventing_url(self) -> None:
        paper = ResearchResource(
            resource_type=ResourceType.PAPER, title="Paper without page link",
            description=None, authors=None, publication_year=None,
            source_provider="OpenAlex", external_id="W-no-page", doi=None,
            keywords=None, retrieved_at=datetime.now(timezone.utc), links=(), downloadable_files=(),
        )
        self.window._accept_search(SearchResult(22, "no page link", [paper], {}, {}))
        rendered = self.window.detail_view.toHtml()
        self.assertIn("No source links supplied", rendered)
        self.assertNotIn("Open paper page", rendered)
        self.assertNotIn('href="https://', rendered)

    def test_export_operation_runs_in_worker_thread(self) -> None:
        result = self.controller.search(topic="urban heat")
        self.window._accept_search(result)
        main_thread = threading.get_ident()
        observed: list[int] = []

        def fake_export(*args, **kwargs):
            observed.append(threading.get_ident())
            return self.db.parent / "export.csv"

        self.controller.export = fake_export
        with patch("app.gui.window.QFileDialog.getSaveFileName", return_value=("unused.csv", "CSV")):
            self.window._export("csv")
        self.wait_for(lambda: bool(observed))
        self.assertNotEqual(observed[0], main_thread)


if __name__ == "__main__":
    unittest.main()
