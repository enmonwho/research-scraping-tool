"""Tests for the argument parser and CLI boundary."""

import contextlib
import io
import unittest
from unittest.mock import patch

from app.cli.main import build_parser, main
from app.models import ResearchResource, ResourceType
from app.providers.openalex import OpenAlexError, normalize_work
from app.storage import DEFAULT_DB_PATH, StorageError


class CliTests(unittest.TestCase):
    def test_parser_accepts_research_topic(self) -> None:
        args = build_parser().parse_args(["search", "machine learning in education"])
        self.assertEqual(args.topic, "machine learning in education")

    def test_help_is_available(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
            main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("usage:", output.getvalue())
        self.assertIn("research topic", output.getvalue())

    def test_missing_topic_has_usage_error_exit_code(self) -> None:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as result:
            main([])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("usage:", errors.getvalue())

    def test_blank_topic_has_clear_error_and_error_exit_code(self) -> None:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as result:
            main(["   "])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("topic cannot be blank", errors.getvalue())

    def test_oversized_topic_has_clear_error_and_error_exit_code(self) -> None:
        errors = io.StringIO()
        topic = "x" * 201
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as result:
            main([topic])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("200 characters or fewer", errors.getvalue())

    def test_valid_topic_is_trimmed_and_printed(self) -> None:
        output = io.StringIO()
        papers = [normalize_work({
            "display_name": "A returned paper", "publication_year": 2025,
            "primary_location": {"landing_page_url": "https://example.org/paper"},
        })]
        with (
            patch("app.cli.main.search_and_save", return_value=papers) as search,
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["  renewable energy  "])
        self.assertEqual(exit_code, 0)
        self.assertIn("A returned paper", output.getvalue())
        self.assertIn("2025", output.getvalue())
        self.assertIn("https://example.org/paper", output.getvalue())
        search.assert_called_once_with(
            "renewable energy", limit=20, timeout=10.0, db_path=str(DEFAULT_DB_PATH)
        )

    def test_valid_topic_with_no_results_reports_empty_search(self) -> None:
        output = io.StringIO()
        with patch("app.cli.main.search_and_save", return_value=[]), contextlib.redirect_stdout(output):
            exit_code = main(["renewable energy"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No papers found.", output.getvalue())

    def test_missing_paper_fields_are_displayed_as_unknown(self) -> None:
        output = io.StringIO()
        papers = [ResearchResource(
            resource_type=ResourceType.PAPER, title=None, description=None, authors=None,
            publication_year=None, source_provider="OpenAlex", external_id=None, doi=None,
            keywords=None, retrieved_at=normalize_work({}).retrieved_at,
        )]
        with (
            patch("app.cli.main.search_and_save", return_value=papers),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main(["topic"]), 0)
        self.assertIn("Unknown title", output.getvalue())
        self.assertIn("Unknown year", output.getvalue())
        self.assertIn("Unknown URL", output.getvalue())

    def test_non_url_or_malformed_external_ids_display_unknown_url(self) -> None:
        output = io.StringIO()
        papers = [
            normalize_work({"display_name": "Provider ID only", "id": "W123"}),
            normalize_work({"display_name": "Malformed URL ID", "id": "https://[invalid"}),
        ]
        with (
            patch("app.cli.main.search_and_save", return_value=papers),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main(["topic"]), 0)
        self.assertEqual(output.getvalue().count("Source URL: Unknown URL"), 2)
        self.assertNotIn("W123", output.getvalue())
        self.assertNotIn("https://[invalid", output.getvalue())

    def test_provider_failure_returns_one_and_preserves_message(self) -> None:
        errors = io.StringIO()
        with (
            patch(
                "app.cli.main.search_and_save",
                side_effect=OpenAlexError("OpenAlex unavailable"),
            ),
            contextlib.redirect_stderr(errors),
        ):
            exit_code = main(["topic"])
        self.assertEqual(exit_code, 1)
        self.assertIn("OpenAlex unavailable", errors.getvalue())

    def test_search_options_are_passed_to_provider(self) -> None:
        with (
            patch("app.cli.main.search_and_save", return_value=[]) as search,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["topic", "--limit", "7", "--timeout", "4.25"]), 0)
        search.assert_called_once_with(
            "topic", limit=7, timeout=4.25, db_path=str(DEFAULT_DB_PATH)
        )

    def test_custom_database_path_is_passed_to_search_service(self) -> None:
        with (
            patch("app.cli.main.search_and_save", return_value=[]) as search,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["topic", "--db", "custom/research.sqlite"]), 0)
        search.assert_called_once_with(
            "topic", limit=20, timeout=10.0, db_path="custom/research.sqlite"
        )

    def test_storage_failure_returns_one_and_preserves_message(self) -> None:
        errors = io.StringIO()
        with (
            patch("app.cli.main.search_and_save", side_effect=StorageError("database unavailable")),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(main(["topic"]), 1)
        self.assertIn("database unavailable", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
