"""Conservative identity and history-preservation tests for Phase 6."""

from __future__ import annotations

import tempfile
import json
import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from app.core.deduplication import deduplicate_resources, find_duplicate, normalize_doi
from app.models import (
    DownloadableFile,
    LinkType,
    ResearchResource,
    ResourceLink,
    ResourceType,
)
from app.storage.sqlite import ResearchStore


STAMP = datetime(2025, 1, 2, tzinfo=timezone.utc)


def paper(
    title: str = "A Study of Climate",
    *,
    provider: str = "OpenAlex",
    external_id: str | None = "W1",
    doi: str | None = "10.1234/climate",
    year: int | None = 2024,
    authors: tuple[str, ...] | None = ("Ada Author",),
    links: tuple[ResourceLink, ...] = (),
    files: tuple[DownloadableFile, ...] = (),
    description: str | None = "Abstract",
    resource_type: ResourceType = ResourceType.PAPER,
    metadata: dict | None = None,
) -> ResearchResource:
    return ResearchResource(
        resource_type=resource_type,
        title=title,
        description=description,
        authors=authors,
        publication_year=year,
        source_provider=provider,
        external_id=external_id,
        doi=doi,
        keywords=None,
        retrieved_at=STAMP,
        links=links,
        downloadable_files=files,
        original_metadata=metadata or {},
    )


class IdentityMatchingTests(unittest.TestCase):
    def test_normalizes_plain_prefixed_and_url_dois(self) -> None:
        normalized = "10.1234/climate"
        self.assertEqual(normalize_doi(normalized), normalized)
        self.assertEqual(normalize_doi("doi:" + normalized), normalized)
        self.assertEqual(normalize_doi("https://doi.org/" + normalized), normalized)
        self.assertEqual(normalize_doi("http://dx.doi.org/" + normalized), normalized)

    def test_exact_dois_match_despite_different_provider_ids(self) -> None:
        first = paper(provider="OpenAlex", external_id="W1")
        other = paper(provider="Crossref", external_id="10.1/other", doi="doi:10.1234/climate")
        self.assertEqual(find_duplicate(other, [first]), 0)

    def test_same_provider_external_id_matches_without_doi(self) -> None:
        first = paper(provider="OpenAlex", external_id="W-SAME", doi=None)
        second = paper("Renamed title", provider="OpenAlex", external_id="W-SAME", doi=None)
        self.assertEqual(find_duplicate(second, [first]), 0)

    def test_provider_ids_are_namespaced(self) -> None:
        first = paper(provider="OpenAlex", external_id="123", doi=None)
        other_provider = paper(provider="Crossref", external_id="123", doi=None)
        self.assertIsNone(find_duplicate(other_provider, [first]))

    def test_same_title_with_distinct_known_dois_stays_separate(self) -> None:
        first = paper()
        second = paper(doi="10.1234/different")
        self.assertIsNone(find_duplicate(second, [first]))

    def test_similar_titles_do_not_match(self) -> None:
        first = paper("Climate effects on urban health", doi=None, external_id=None)
        second = paper("Climate effects on urban heat", doi=None, external_id=None)
        self.assertIsNone(find_duplicate(second, [first]))

    def test_title_fallback_requires_year_and_full_author_signature(self) -> None:
        first = paper(doi=None, external_id=None)
        self.assertEqual(find_duplicate(replace(first, title="  A STUDY of climate "), [first]), 0)
        self.assertIsNone(find_duplicate(replace(first, publication_year=2023), [first]))
        self.assertIsNone(find_duplicate(replace(first, authors=("Different Author",)), [first]))

    def test_missing_strong_ids_and_incomplete_fallback_stay_separate(self) -> None:
        first = paper(doi=None, external_id=None, year=2024, authors=None)
        second = replace(first, retrieved_at=datetime(2025, 2, 2, tzinfo=timezone.utc))
        self.assertIsNone(find_duplicate(second, [first]))

    def test_canonical_resource_urls_match(self) -> None:
        first = paper(
            doi=None,
            external_id=None,
            links=(ResourceLink(LinkType.LANDING_PAGE, "HTTPS://Example.org:443/paper/", "OpenAlex"),),
        )
        other = paper(
            "Different title",
            provider="Crossref",
            external_id=None,
            doi=None,
            links=(ResourceLink(LinkType.LANDING_PAGE, "https://example.org/paper", "Crossref"),),
        )
        self.assertEqual(find_duplicate(other, [first]), 0)

    def test_different_resource_types_never_match(self) -> None:
        self.assertIsNone(find_duplicate(paper(resource_type=ResourceType.DATASET), [paper()]))

    def test_deduplication_collapses_with_deterministic_first_seen_order(self) -> None:
        first = paper()
        second = paper("DOI duplicate", provider="Crossref", external_id="C1")
        unique = paper("Unique", doi="10.1234/unique", external_id="W2")
        result = deduplicate_resources([first, second, unique])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].title, first.title)
        self.assertEqual(result[1].title, "Unique")

    def test_merge_preserves_links_files_and_provider_metadata(self) -> None:
        first_link = ResourceLink(LinkType.LANDING_PAGE, "https://openalex.org/W1", "OpenAlex")
        second_link = ResourceLink(LinkType.REPOSITORY, "https://repo.example/paper", "Crossref", "cc-by", True)
        file_one = DownloadableFile("https://repo.example/a.pdf", "pdf", "application/pdf", "a.pdf", "Crossref", True)
        file_two = DownloadableFile("https://repo.example/b.pdf", "pdf", "application/pdf", "b.pdf", "OpenAlex", True)
        first = paper(links=(first_link,), metadata={"openalex": {"id": "W1"}})
        second = paper(
            provider="Crossref",
            external_id="C1",
            doi="https://doi.org/10.1234/climate",
            authors=("Ada Author", "Bea Author"),
            description=None,
            links=(second_link,),
            files=(file_one, file_two),
            metadata={"crossref": {"id": "C1"}},
        )
        merged = deduplicate_resources([first, second])[0]
        self.assertEqual(set(merged.links), {first_link, second_link})
        self.assertEqual(set(merged.downloadable_files), {file_one, file_two})
        self.assertIn("Ada Author", merged.authors)
        self.assertIn("Bea Author", merged.authors)
        self.assertEqual(merged.description, "Abstract")
        self.assertTrue(any(item.source_provider == "OpenAlex" for item in merged.provenance))
        self.assertTrue(any(item.source_provider == "Crossref" for item in merged.provenance))
        self.assertTrue(any(item.original_metadata.get("crossref", {}).get("id") == "C1"
                            for item in merged.provenance))


class DeduplicationHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "dedupe.db"

    def test_same_search_duplicates_collapse_but_distinct_order_is_kept(self) -> None:
        first = paper()
        duplicate = paper("Alternate observation", provider="Crossref", external_id="C1")
        unique = paper("Unique", doi="10.1234/unique", external_id="W2")
        with ResearchStore(self.db) as store:
            search_id = store.save_search("topic", [first, duplicate, unique])
            saved = store.get_search_resources(search_id)
        self.assertEqual([item.title for item in saved], [first.title, "Unique"])

    def test_repeated_searches_preserve_history_and_immutable_snapshots(self) -> None:
        before = paper(description="First observation", metadata={"edition": 1})
        after = paper("Improved title", provider="Crossref", external_id="C1", description="Later observation",
                      metadata={"edition": 2})
        with ResearchStore(self.db) as store:
            first_id = store.save_search("first", [before])
            second_id = store.save_search("second", [after])
            history = store.list_searches()
            first_snapshot = store.get_search_resources(first_id)[0]
            second_snapshot = store.get_search_resources(second_id)[0]
        self.assertEqual([entry.id for entry in history], [second_id, first_id])
        self.assertEqual(first_snapshot.description, "First observation")
        self.assertEqual(first_snapshot.original_metadata, {"edition": 1})
        self.assertEqual(second_snapshot.description, "Later observation")

    def test_cross_provider_doi_merge_keeps_links_files_and_provenance(self) -> None:
        openalex_link = ResourceLink(
            LinkType.LANDING_PAGE, "https://openalex.org/W-CROSS", "OpenAlex"
        )
        crossref_link = ResourceLink(
            LinkType.PUBLISHER, "https://publisher.example/cross", "Crossref"
        )
        openalex_file = DownloadableFile(
            "https://repository.example/openalex.pdf", "pdf", "application/pdf",
            "openalex.pdf", "OpenAlex", True,
        )
        crossref_file = DownloadableFile(
            "https://repository.example/crossref.pdf", "pdf", "application/pdf",
            "crossref.pdf", "Crossref", True,
        )
        openalex = paper(
            provider="OpenAlex", external_id="W-CROSS", doi="10.1234/shared",
            links=(openalex_link,), files=(openalex_file,), metadata={"id": "W-CROSS"},
        )
        crossref = paper(
            provider="Crossref", external_id="C-CROSS", doi="https://doi.org/10.1234/shared",
            links=(crossref_link,), files=(crossref_file,), metadata={"id": "C-CROSS"},
        )
        with ResearchStore(self.db) as store:
            first_search_id = store.save_search("first provider", [openalex])
            second_search_id = store.save_search("second provider", [crossref])
            merged_snapshot = store.get_search_resources(second_search_id)[0]
            first_snapshot = store.get_search_resources(first_search_id)[0]
            resource_count = store._connection.execute("SELECT COUNT(*) FROM resources").fetchone()[0]

        self.assertEqual(resource_count, 1)
        self.assertEqual({link.url for link in merged_snapshot.links}, {
            openalex_link.url, crossref_link.url,
        })
        self.assertEqual({item.url for item in merged_snapshot.downloadable_files}, {
            openalex_file.url, crossref_file.url,
        })
        self.assertEqual({item.source_provider for item in merged_snapshot.provenance}, {
            "OpenAlex", "Crossref",
        })
        self.assertEqual({link.url for link in first_snapshot.links}, {openalex_link.url})
        self.assertEqual({item.url for item in first_snapshot.downloadable_files}, {openalex_file.url})

    def test_reopening_preserves_aliases_provenance_and_saved_order(self) -> None:
        first = paper(links=(ResourceLink(LinkType.LANDING_PAGE, "https://openalex.org/W1", "OpenAlex"),))
        second = paper(provider="Crossref", external_id="C1", links=(
            ResourceLink(LinkType.PUBLISHER, "https://publisher.example/item", "Crossref"),
        ))
        unique = paper("Third paper", doi="10.1234/third", external_id="W3")
        with ResearchStore(self.db) as store:
            search_id = store.save_search("topic", [first, second, unique])
        with ResearchStore(self.db) as reopened:
            resources = reopened.get_search_resources(search_id)
            history = reopened.list_searches()
        self.assertEqual([item.title for item in resources], [first.title, "Third paper"])
        self.assertEqual(history[0].result_count, 2)
        self.assertEqual(resources[0].source_provider, "OpenAlex")
        self.assertIn("https://publisher.example/item", {link.url for link in resources[0].links})

    def test_v1_migration_preserves_records_and_backfills_identity_and_provenance(self) -> None:
        observed_at = datetime(2024, 6, 7, 8, 9, tzinfo=timezone.utc)
        resource = replace(
            paper(
                external_id="https://openalex.org/W-MIGRATE",
                doi="https://doi.org/10.1234/migrate",
                metadata={"provider_payload": {"id": "W-MIGRATE"}},
                links=(ResourceLink(LinkType.LANDING_PAGE, "https://publisher.example/migrate", "OpenAlex"),),
            ),
            retrieved_at=observed_at,
        )
        searched_at = datetime(2024, 6, 8, 10, 11, tzinfo=timezone.utc)
        with ResearchStore(self.db) as store:
            search_id = store.save_search("migration topic", [resource], searched_at=searched_at)

        # Recreate the Phase 4 shape: retain original tables and rows, remove the
        # later additions and both optional fields introduced after Phase 4.
        connection = sqlite3.connect(self.db)
        try:
            snapshot_row = connection.execute(
                "SELECT snapshot_json FROM search_resources WHERE search_id = ?", (search_id,)
            ).fetchone()
            snapshot_data = json.loads(snapshot_row[0])
            snapshot_data.pop("provenance", None)
            phase4_snapshot = json.dumps(snapshot_data, ensure_ascii=False)
            connection.execute(
                "UPDATE search_resources SET snapshot_json = ? WHERE search_id = ?",
                (phase4_snapshot, search_id),
            )
            connection.execute("DROP TABLE resource_identifiers")
            connection.execute("DROP TABLE resource_provenance")
            connection.execute("ALTER TABLE searches DROP COLUMN metadata_json")
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        finally:
            connection.close()

        with ResearchStore(self.db) as upgraded:
            self.assertEqual(upgraded.schema_version, 3)
            history = upgraded.list_searches()
            restored = upgraded.get_search_resources(search_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].id, search_id)
        self.assertEqual(history[0].topic, "migration topic")
        self.assertEqual(history[0].searched_at, searched_at)
        self.assertEqual(history[0].result_count, 1)
        self.assertEqual(restored[0].title, resource.title)
        self.assertEqual(restored[0].original_metadata, resource.original_metadata)

        connection = sqlite3.connect(self.db)
        try:
            preserved_snapshot = connection.execute(
                "SELECT snapshot_json FROM search_resources WHERE search_id = ?", (search_id,)
            ).fetchone()[0]
            identifiers = connection.execute(
                "SELECT namespace, identifier, source_provider FROM resource_identifiers"
            ).fetchall()
            provenance = connection.execute(
                "SELECT source_provider, external_id, doi, retrieved_at, original_metadata_json "
                "FROM resource_provenance"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(preserved_snapshot, phase4_snapshot)
        self.assertIn(("doi", "10.1234/migrate", ""), identifiers)
        self.assertIn(("provider_id", "https://openalex.org/W-MIGRATE", "openalex"), identifiers)
        self.assertIn(("url", "https://publisher.example/migrate", ""), identifiers)
        self.assertEqual(len(provenance), 1)
        self.assertEqual(provenance[0][0:3], (
            "OpenAlex", "https://openalex.org/W-MIGRATE", "10.1234/migrate"
        ))
        self.assertEqual(provenance[0][3], observed_at.isoformat())
        self.assertEqual(json.loads(provenance[0][4]), resource.original_metadata)


if __name__ == "__main__":
    unittest.main()
