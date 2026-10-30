"""Temporary-database tests for Phase 4 persistence and search history."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from app.core.search_service import get_history_resources, list_history, search_and_save
from app.models import DownloadableFile, LinkType, ResearchResource, ResourceLink, ResourceType
from app.providers.openalex import normalize_work
from app.storage.sqlite import SCHEMA_VERSION, ResearchStore, StorageError


def scalar_from_database(path: Path, query: str) -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(query).fetchone()[0]
    finally:
        connection.close()


class ResearchStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "nested" / "research.db"

    def test_initializes_schema_version_and_creates_parent_directory(self) -> None:
        with ResearchStore(self.db_path) as store:
            self.assertGreaterEqual(store.schema_version, 1)
        self.assertTrue(self.db_path.is_file())

    def test_saves_search_and_returns_topic_timestamp_and_result_count(self) -> None:
        resource = normalize_work({"id": "https://openalex.org/W1", "display_name": "A paper"})
        timestamp = datetime(2025, 1, 2, 3, 4, tzinfo=timezone.utc)
        with ResearchStore(self.db_path) as store:
            search_id = store.save_search("  climate change ", [resource], searched_at=timestamp)
            summaries = store.list_searches()
            resources = store.get_search_resources(search_id)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].id, search_id)
        self.assertEqual(summaries[0].topic, "  climate change ")
        self.assertEqual(summaries[0].searched_at, timestamp)
        self.assertEqual(summaries[0].result_count, 1)
        self.assertEqual(resources[0].external_id, "https://openalex.org/W1")

    def test_same_provider_resource_is_reused_across_searches(self) -> None:
        resource = normalize_work({"id": "https://openalex.org/W-DUP", "display_name": "Shared"})
        second_observation = replace(
            resource, title="Shared, with updated metadata", original_metadata={"observation": 2}
        )
        with ResearchStore(self.db_path) as store:
            first = store.save_search("first topic", [resource])
            second = store.save_search("second topic", [second_observation])
            first_resource = store.get_search_resources(first)[0]
            second_resource = store.get_search_resources(second)[0]
        self.assertEqual(first_resource.external_id, second_resource.external_id)
        self.assertEqual(first_resource.title, "Shared")
        self.assertEqual(second_resource.title, "Shared, with updated metadata")
        self.assertEqual(second_resource.original_metadata, {"observation": 2})
        count = scalar_from_database(self.db_path, "SELECT COUNT(*) FROM resources")
        self.assertEqual(count, 1)

    def test_missing_optional_metadata_round_trips_as_unknown(self) -> None:
        resource = normalize_work({"id": "https://openalex.org/W-MIN"})
        with ResearchStore(self.db_path) as store:
            search_id = store.save_search("minimal", [resource])
            restored = store.get_search_resources(search_id)[0]
        self.assertIsNone(restored.title)
        self.assertIsNone(restored.description)
        self.assertIsNone(restored.authors)
        self.assertIsNone(restored.publication_year)
        self.assertIsNone(restored.doi)
        self.assertIsNone(restored.license)
        self.assertIsNone(restored.is_open_access)

    def test_links_files_and_provenance_round_trip(self) -> None:
        resource = ResearchResource(
            resource_type=ResourceType.PAPER, title="Persisted", description="Abstract",
            authors=("A. Author", "B. Author"), publication_year=2024,
            source_provider="OpenAlex", external_id="https://openalex.org/W-LINKS",
            doi="10.1234/example", keywords=("climate",),
            retrieved_at=datetime(2025, 2, 3, tzinfo=timezone.utc), license="cc-by",
            is_open_access=True,
            links=(
                ResourceLink(LinkType.LANDING_PAGE, "https://example.org/item", "OpenAlex"),
                ResourceLink(LinkType.REPOSITORY, "https://repo.example/item", "OpenAlex", "cc-by", True),
            ),
            downloadable_files=(DownloadableFile(
                "https://repo.example/file.pdf", "pdf", "application/pdf", "paper.pdf", "OpenAlex", True
            ),),
            original_metadata={"id": "https://openalex.org/W-LINKS", "nested": {"ok": True}},
        )
        with ResearchStore(self.db_path) as store:
            search_id = store.save_search("links", [resource])
            restored = store.get_search_resources(search_id)[0]
        self.assertEqual(restored.links, resource.links)
        self.assertEqual(restored.downloadable_files, resource.downloadable_files)
        self.assertEqual(restored.original_metadata, resource.original_metadata)
        self.assertEqual(restored.authors, resource.authors)

    def test_failed_save_rolls_back_search_and_resources(self) -> None:
        valid = normalize_work({"id": "https://openalex.org/W-ROLLBACK", "display_name": "Valid"})
        invalid = ResearchResource(
            resource_type=ResourceType.PAPER, title="Cannot serialize", description=None,
            authors=None, publication_year=None, source_provider="OpenAlex",
            external_id="https://openalex.org/W-BAD", doi=None, keywords=None,
            retrieved_at=datetime.now(timezone.utc), original_metadata={"bad": {1, 2}},
        )
        with ResearchStore(self.db_path) as store:
            with self.assertRaises(StorageError):
                store.save_search("must rollback", [valid, invalid])
            self.assertEqual(store.list_searches(), [])
        self.assertEqual(scalar_from_database(self.db_path, "SELECT COUNT(*) FROM resources"), 0)

    def test_interrupted_resource_iteration_rolls_back_entire_search(self) -> None:
        resource = normalize_work({"id": "https://openalex.org/W-INTERRUPTED"})

        def interrupted_resources():
            yield resource
            raise KeyboardInterrupt()

        with ResearchStore(self.db_path) as store:
            with self.assertRaises(KeyboardInterrupt):
                store.save_search("interrupted", interrupted_resources())
            self.assertEqual(store.list_searches(), [])

    def test_reopen_preserves_saved_searches(self) -> None:
        resource = normalize_work({"id": "https://openalex.org/W-REOPEN", "display_name": "Persisted"})
        with ResearchStore(self.db_path) as store:
            search_id = store.save_search("keep me", [resource])
        with ResearchStore(self.db_path) as reopened:
            self.assertEqual(reopened.schema_version, SCHEMA_VERSION)
            self.assertEqual(reopened.list_searches()[0].id, search_id)

    def test_invalid_database_path_is_reported(self) -> None:
        blocker = Path(self.temp.name) / "ordinary-file"
        blocker.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(StorageError):
            ResearchStore(blocker / "research.db")

    def test_newer_schema_is_rejected_without_modifying_database(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("CREATE TABLE sentinel (value TEXT)")
            connection.execute("INSERT INTO sentinel VALUES ('preserve')")
            connection.execute("PRAGMA user_version = 999")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            ResearchStore(self.db_path)
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 999)
            self.assertEqual(connection.execute("SELECT value FROM sentinel").fetchone()[0], "preserve")
        finally:
            connection.close()


class SearchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "service.db"

    def test_search_and_save_and_history_service_use_injected_provider(self) -> None:
        expected = [normalize_work({"id": "https://openalex.org/W-SVC", "display_name": "Service result"})]

        def provider(topic: str, *, limit: int, timeout: float):
            self.assertEqual(topic, "service topic")
            self.assertEqual(limit, 3)
            self.assertEqual(timeout, 2.5)
            return expected

        results = search_and_save(
            "service topic", limit=3, timeout=2.5, db_path=self.db_path, provider=provider
        )
        searches = list_history(self.db_path)
        restored = get_history_resources(searches[0].id, self.db_path)
        self.assertEqual(results, expected)
        self.assertEqual(searches[0].topic, "service topic")
        self.assertEqual(searches[0].result_count, 1)
        self.assertEqual(restored[0].external_id, expected[0].external_id)

    def test_empty_search_history_and_provider_failure_do_not_create_history(self) -> None:
        self.assertEqual(list_history(self.db_path), [])

        def failed_provider(topic: str, *, limit: int, timeout: float):
            raise RuntimeError("provider stopped")

        with self.assertRaisesRegex(RuntimeError, "provider stopped"):
            search_and_save("topic", db_path=self.db_path, provider=failed_provider)
        self.assertEqual(list_history(self.db_path), [])


if __name__ == "__main__":
    unittest.main()
