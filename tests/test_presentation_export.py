"""Phase 5 presentation, search-history, and export acceptance tests."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.cli.main import main
from app.core.search_service import list_history
from app.models import DownloadableFile, LinkType, ResearchResource, ResourceLink, ResourceType
from app.storage import ResearchStore


def resource(*, complete: bool = True, title: str = "Café研究") -> ResearchResource:
    return ResearchResource(
        resource_type=ResourceType.PAPER,
        title=title if complete else None,
        description="Résumé abstract" if complete else None,
        authors=("Zoë Author", "李明") if complete else None,
        publication_year=2024 if complete else None,
        source_provider="OpenAlex",
        external_id="https://openalex.org/W-PHASE5" if complete else None,
        doi="10.1234/example" if complete else None,
        keywords=("climate", "研究") if complete else None,
        retrieved_at=datetime(2025, 3, 4, tzinfo=timezone.utc),
        license="CC-BY-4.0" if complete else None,
        is_open_access=True if complete else None,
        links=(
            ResourceLink(LinkType.LANDING_PAGE, "https://example.org/paper", "OpenAlex"),
            ResourceLink(LinkType.OPEN_ACCESS, "https://repo.example/paper", "Repository", "CC-BY-4.0", True),
        ) if complete else (),
        downloadable_files=(
            DownloadableFile("https://repo.example/a.pdf", "pdf", "application/pdf", "研究.pdf", "Repository", True),
            DownloadableFile("https://repo.example/b.xml", "xml", "application/xml", "paper.xml", "Repository", True),
        ) if complete else (),
        original_metadata={"provider": {"work_id": "W-PHASE5"}, "unicode": "研究"} if complete else {},
    )


class PresentationTests(unittest.TestCase):
    def test_terminal_output_includes_available_fields_and_multiple_links(self) -> None:
        output = io.StringIO()
        with patch("app.cli.main.search_and_save", return_value=[resource()]), contextlib.redirect_stdout(output):
            self.assertEqual(main(["renewable energy"]), 0)
        rendered = output.getvalue()
        for expected in (
            "1.", "Café研究", "Zoë Author", "李明", "2024", "Résumé abstract",
            "10.1234/example", "OpenAlex", "https://example.org/paper",
            "https://repo.example/paper", "https://repo.example/a.pdf",
            "https://repo.example/b.xml", "CC-BY-4.0",
        ):
            self.assertIn(expected, rendered)

    def test_terminal_output_marks_missing_metadata_unknown(self) -> None:
        output = io.StringIO()
        with patch("app.cli.main.search_and_save", return_value=[resource(complete=False)]), contextlib.redirect_stdout(output):
            self.assertEqual(main(["topic"]), 0)
        rendered = output.getvalue()
        self.assertIn("Unknown", rendered)
        self.assertNotIn("DOI: None", rendered)
        self.assertNotIn("Year: None", rendered)


class HistoryCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "history.db"

    def test_history_lists_date_and_result_count(self) -> None:
        with ResearchStore(self.db) as store:
            store.save_search("climate", [resource()])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["history", "--db", str(self.db)]), 0)
        text = output.getvalue()
        self.assertIn("climate", text)
        self.assertIn("1", text)
        self.assertIn("202", text)

    def test_show_reads_saved_results_without_searching(self) -> None:
        with ResearchStore(self.db) as store:
            search_id = store.save_search("climate", [resource()])
        output = io.StringIO()
        with patch("app.cli.main.search_and_save", side_effect=AssertionError("must not search")), contextlib.redirect_stdout(output):
            self.assertEqual(main(["show", str(search_id), "--db", str(self.db)]), 0)
        self.assertIn("Café研究", output.getvalue())

    def test_empty_history_is_reported(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["history", "--db", str(self.db)]), 0)
        self.assertRegex(output.getvalue(), r"(?i)(no|empty).*search")

    def test_invalid_search_id_returns_clear_cli_error(self) -> None:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as result:
            main(["show", "not-an-id", "--db", str(self.db)])
        self.assertEqual(result.exception.code, 2)
        self.assertTrue(errors.getvalue())

    def test_unknown_search_id_returns_clear_cli_error(self) -> None:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as result:
            main(["show", "999999", "--db", str(self.db)])
        self.assertEqual(result.exception.code, 2)
        self.assertTrue(errors.getvalue())


class ExportCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "exports.db"
        with ResearchStore(self.db) as store:
            self.search_id = store.save_search("climate", [resource()])

    def invoke_export(self, fmt: str, path: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["export", str(self.search_id), "--format", fmt, "--output", str(path), "--db", str(self.db)])
        return code, out.getvalue(), err.getvalue()

    def test_csv_export_preserves_unicode_and_all_links_and_files(self) -> None:
        path = self.root / "papers.csv"
        code, _, err = self.invoke_export("csv", path)
        self.assertEqual((code, err), (0, ""))
        with path.open(encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        for expected in ("Café研究", "Zoë Author", "李明", "https://example.org/paper",
                         "https://repo.example/paper", "https://repo.example/a.pdf",
                         "https://repo.example/b.xml", "CC-BY-4.0"):
            self.assertIn(expected, " ".join(row.values()))

    def test_json_export_round_trips_structured_metadata(self) -> None:
        path = self.root / "papers.json"
        code, _, err = self.invoke_export("json", path)
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(path.read_text(encoding="utf-8"))
        encoded = json.dumps(payload, ensure_ascii=False)
        for expected in ("Café研究", "李明", "landing_page", "open_access", "downloadable_files",
                         "https://repo.example/a.pdf", "https://repo.example/b.xml", "original_metadata", "CC-BY-4.0"):
            self.assertIn(expected, encoded)

    def test_export_does_not_overwrite_existing_file(self) -> None:
        path = self.root / "existing.json"
        path.write_text("keep", encoding="utf-8")
        code, _, err = self.invoke_export("json", path)
        self.assertNotEqual(code, 0)
        self.assertTrue(err)
        self.assertEqual(path.read_text(encoding="utf-8"), "keep")

    def test_invalid_export_destination_is_reported_without_traceback(self) -> None:
        path = self.root / "missing" / "nested" / "papers.csv"
        code, _, err = self.invoke_export("csv", path)
        self.assertNotEqual(code, 0)
        self.assertTrue(err)
        self.assertNotIn("Traceback", err)

    def test_empty_saved_result_export_is_valid(self) -> None:
        with ResearchStore(self.db) as store:
            empty_id = store.save_search("empty", [])
        path = self.root / "empty.json"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["export", str(empty_id), "--format", "json", "--output", str(path), "--db", str(self.db)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
