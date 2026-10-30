"""Synthetic integration coverage for merged academic provider observations."""

from __future__ import annotations

import csv
import contextlib
import io
import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace
from unittest.mock import patch

from app.core.deduplication import deduplicate_resources
from app.models import (
    DownloadableFile,
    LinkType,
    ResearchResource,
    ResourceLink,
    ResourceType,
)
from app.storage.sqlite import ResearchStore


STAMP = datetime(2025, 1, 2, tzinfo=timezone.utc)


def observation(
    provider: str,
    external_id: str,
    doi: str | None,
    *,
    title: str | None = "Flood prediction from community observations",
    authors: tuple[str, ...] | None = None,
    description: str | None = None,
    links: tuple[ResourceLink, ...] = (),
    files: tuple[DownloadableFile, ...] = (),
) -> ResearchResource:
    return ResearchResource(
        resource_type=ResourceType.PAPER,
        title=title,
        description=description,
        authors=authors,
        publication_year=2024,
        source_provider=provider,
        external_id=external_id,
        doi=doi,
        keywords=None,
        retrieved_at=STAMP,
        links=links,
        downloadable_files=files,
        original_metadata={"provider_record": provider.lower()},
    )


def fixture_observations(name: str) -> list[ResearchResource]:
    records = json.loads((Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8"))
    resources = []
    for record in records:
        links = [ResourceLink(LinkType.LANDING_PAGE, record["source_url"], record["provider"])]
        files = []
        if record.get("download_url"):
            files.append(DownloadableFile(record["download_url"], "pdf", "application/pdf",
                                           None, record["provider"], True))
        resources.append(observation(
            record["provider"], record["external_id"], record["doi"],
            title=record["title"], authors=tuple(record["authors"]),
            links=tuple(links), files=tuple(files),
        ))
    return resources


class CombinedResourceMergeTests(unittest.TestCase):
    def test_different_doi_representations_merge_and_keep_both_provenance(self) -> None:
        first = observation(
            "OpenAlex", "https://openalex.org/W1", "10.5555/FLOOD.2024",
            authors=("Ada Author",), links=(ResourceLink(
                LinkType.LANDING_PAGE, "https://openalex.example/work/1", "OpenAlex"
            ),),
        )
        second = observation(
            "Crossref", "https://api.crossref.org/works/10.5555/flood.2024",
            "https://doi.org/10.5555/flood.2024", authors=("Ben Scientist",),
            description="Provider supplied abstract.", links=(ResourceLink(
                LinkType.PUBLISHER, "https://publisher.example/paper", "Crossref"
            ),), files=(DownloadableFile(
                "https://publisher.example/paper.pdf", "pdf", "application/pdf", None,
                "Crossref", True,
            ),),
        )

        merged = deduplicate_resources([first, second])

        self.assertEqual(len(merged), 1)
        result = merged[0]
        self.assertEqual(result.doi, "10.5555/flood.2024")
        self.assertEqual({item.source_provider for item in result.provenance}, {"OpenAlex", "Crossref"})
        self.assertEqual({item.external_id for item in result.provenance}, {
            "https://openalex.org/W1", "https://api.crossref.org/works/10.5555/flood.2024",
        })
        self.assertEqual({link.url for link in result.links}, {
            "https://openalex.example/work/1", "https://publisher.example/paper",
        })
        self.assertEqual(result.authors, ("Ada Author", "Ben Scientist"))
        self.assertEqual(result.description, "Provider supplied abstract.")
        self.assertEqual(result.downloadable_files[0].url, "https://publisher.example/paper.pdf")

    def test_conflicting_dois_remain_distinct_even_with_same_title(self) -> None:
        first = observation("OpenAlex", "W1", "10.5555/first", authors=("Ada Author",))
        second = observation("Crossref", "10.5555/second", "10.5555/second", authors=("Ada Author",))
        self.assertEqual(len(deduplicate_resources([first, second])), 2)

    def test_missing_doi_requires_strict_fallback_evidence(self) -> None:
        first = observation("OpenAlex", "W1", None, authors=("Ada Author",))
        unrelated = observation("Crossref", "R2", None, title="A related but different flood paper",
                                authors=("Ada Author",))
        self.assertEqual(len(deduplicate_resources([first, unrelated])), 2)

    def test_deduplication_is_stable_and_keeps_distinct_papers(self) -> None:
        a = observation("OpenAlex", "W1", "10.5555/a")
        b = observation("Crossref", "10.5555/b", "10.5555/b", title="Another flood paper")
        a_copy = observation("Crossref", "10.5555/a", "doi:10.5555/a")
        result = deduplicate_resources([a, b, a_copy])
        self.assertEqual([item.title for item in result], [a.title, "Another flood paper"])
        self.assertEqual(len(result[0].provenance), 2)


class CombinedHistoryExportTests(unittest.TestCase):
    def test_combined_snapshot_is_one_history_entry_and_exports_all_sources(self) -> None:
        with ResearchStore(":memory:") as store:
            papers = deduplicate_resources([
                observation("OpenAlex", "https://openalex.org/W1", "10.5555/shared", links=(
                    ResourceLink(LinkType.LANDING_PAGE, "https://openalex.example/W1", "OpenAlex"),
                ), files=(DownloadableFile("https://openalex.example/shared.pdf", "pdf",
                                           "application/pdf", None, "OpenAlex", True),)),
                observation("Crossref", "https://api.crossref.org/works/10.5555/shared",
                            "10.5555/shared", links=(
                    ResourceLink(LinkType.PUBLISHER, "https://publisher.example/shared", "Crossref"),
                ), files=(DownloadableFile("https://publisher.example/shared.pdf", "pdf",
                                            "application/pdf", None, "Crossref", True),)),
            ])

            search_id = store.save_search("flood prediction", papers, searched_at=STAMP)
            summaries = store.list_searches()
            restored = store.get_search_resources(search_id)

            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].result_count, 1)
            self.assertEqual(summaries[0].searched_at, STAMP)
            self.assertEqual(len(restored), 1)
            self.assertEqual({item.source_provider for item in restored[0].provenance}, {"OpenAlex", "Crossref"})
            self.assertEqual({link.url for link in restored[0].links}, {
                "https://openalex.example/W1", "https://publisher.example/shared",
            })
            self.assertEqual({item.url for item in restored[0].downloadable_files}, {
                "https://openalex.example/shared.pdf", "https://publisher.example/shared.pdf",
            })

            from app.cli.main import write_export

            # The repository has write access; isolated per-test names avoid touching user files.
            csv_path = Path(__file__).parent / f"combined-{id(self)}.csv"
            json_path = Path(__file__).parent / f"combined-{id(self)}.json"
            try:
                metadata = {"status": "partial", "failed_providers": {"OpenAlex": "offline"}}
                write_export(restored, csv_path, "csv", search_metadata=metadata)
                write_export(restored, json_path, "json", search_metadata=metadata)
                with csv_path.open(encoding="utf-8", newline="") as stream:
                    csv_row = next(csv.DictReader(stream))
                json_row = json.loads(json_path.read_text(encoding="utf-8"))[0]
                self.assertIn("OpenAlex", csv_row["provenance"])
                self.assertIn("Crossref", csv_row["provenance"])
                self.assertIn("https://openalex.org/W1", csv_row["provider_identifiers"])
                self.assertIn("10.5555/shared", csv_row["provider_identifiers"])
                self.assertIn("openalex.example/W1", csv_row["links"])
                self.assertIn("publisher.example/shared", csv_row["links"])
                self.assertIn("shared.pdf", csv_row["download_links"])
                self.assertEqual(csv_row["search_status"], "partial")
                self.assertIn("offline", csv_row["failed_providers"])
                self.assertEqual(len(json_row["provenance"]["provider_observations"]), 2)
                self.assertIn("publisher.example/shared", json.dumps(json_row["links"]))
                self.assertIn("shared.pdf", json.dumps(json_row["downloadable_files"]))
                self.assertEqual(json_row["search_metadata"], metadata)
            finally:
                csv_path.unlink(missing_ok=True)
                json_path.unlink(missing_ok=True)


class CombinedSearchServiceTests(unittest.TestCase):
    """Exercise orchestration with deterministic provider responses and failures."""

    def run_search(self, openalex, crossref, *, limit=20):
        from app.core.search_service import search_all_and_save

        db_path = Path(__file__).parent / f".combined-{id(self)}.sqlite"
        self.addCleanup(db_path.unlink, missing_ok=True)
        outcome = search_all_and_save(
            "flood prediction", limit=limit, timeout=3.5, db_path=db_path,
            openalex_provider=openalex, crossref_provider=crossref,
        )
        return outcome, db_path

    def test_success_deduplicates_and_reports_provider_counts(self) -> None:
        openalex = Mock(return_value=fixture_observations("combined_openalex.json"))
        crossref = Mock(return_value=fixture_observations("combined_crossref.json"))
        outcome, _ = self.run_search(openalex, crossref)
        self.assertEqual(outcome.provider_counts, {"OpenAlex": 2, "Crossref": 2})
        self.assertEqual(outcome.failed_providers, {})
        self.assertEqual(len(outcome.resources), 3)
        self.assertEqual({p.source_provider for p in outcome.resources[0].provenance},
                         {"OpenAlex", "Crossref"})
        self.assertEqual(len(outcome.resources[0].links), 2)
        self.assertEqual(len(outcome.resources[0].downloadable_files), 1)
        self.assertEqual(set(outcome.resources[0].authors), {"Ada Author", "Casey Author"})
        openalex.assert_called_once_with("flood prediction", limit=20, timeout=3.5)
        crossref.assert_called_once_with("flood prediction", limit=20, timeout=3.5)

    def test_final_limit_applies_after_merge_in_deterministic_order(self) -> None:
        openalex = lambda *_args, **_kwargs: [
            observation("OpenAlex", "W1", "10.5555/one", title="One"),
            observation("OpenAlex", "W2", "10.5555/two", title="Two"),
        ]
        crossref = lambda *_args, **_kwargs: [
            observation("Crossref", "C1", "10.5555/one", title="One"),
            observation("Crossref", "C3", "10.5555/three", title="Three"),
        ]
        first, _ = self.run_search(openalex, crossref, limit=2)
        second, _ = self.run_search(openalex, crossref, limit=2)
        self.assertEqual([item.title for item in first.resources], ["One", "Two"])
        self.assertEqual([item.title for item in first.resources], [item.title for item in second.resources])
        self.assertEqual(len(first.resources), 2)

    def test_one_provider_failure_returns_successful_results_and_failure_detail(self) -> None:
        from app.core.search_service import search_all_and_save

        def fail(*_args, **_kwargs):
            raise RuntimeError("OpenAlex offline")

        db_path = Path(__file__).parent / f".combined-one-failure-{id(self)}.sqlite"
        self.addCleanup(db_path.unlink, missing_ok=True)
        outcome = search_all_and_save(
            "flood prediction", db_path=db_path, openalex_provider=fail,
            crossref_provider=lambda *_args, **_kwargs: [
                observation("Crossref", "C1", "10.5555/crossref")
            ],
        )
        self.assertEqual(len(outcome.resources), 1)
        self.assertIn("OpenAlex", outcome.failed_providers)
        self.assertIn("offline", outcome.failed_providers["OpenAlex"])

    def test_crossref_failure_returns_openalex_results_and_failure_detail(self) -> None:
        from app.core.search_service import search_all_and_save

        def fail(*_args, **_kwargs):
            raise RuntimeError("Crossref unavailable")

        db_path = Path(__file__).parent / f".combined-crossref-failure-{id(self)}.sqlite"
        self.addCleanup(db_path.unlink, missing_ok=True)
        outcome = search_all_and_save(
            "flood prediction", db_path=db_path, crossref_provider=fail,
            openalex_provider=lambda *_args, **_kwargs: [
                observation("OpenAlex", "W1", "10.5555/openalex")
            ],
        )
        self.assertEqual(len(outcome.resources), 1)
        self.assertEqual(outcome.resources[0].source_provider, "OpenAlex")
        self.assertIn("Crossref", outcome.failed_providers)
        self.assertIn("unavailable", outcome.failed_providers["Crossref"])

    def test_both_provider_failures_raise(self) -> None:
        from app.core.search_service import search_all_and_save

        def fail(message):
            def provider(*_args, **_kwargs):
                raise RuntimeError(message)
            return provider

        db_path = Path(__file__).parent / f".combined-failed-{id(self)}.sqlite"
        self.addCleanup(db_path.unlink, missing_ok=True)
        from app.core.search_service import CombinedSearchError
        with self.assertRaises(CombinedSearchError):
            search_all_and_save(
                "flood prediction", db_path=db_path,
                openalex_provider=fail("OpenAlex offline"),
                crossref_provider=fail("Crossref offline"),
            )
        self.assertFalse(db_path.exists())

    def test_empty_responses_are_success_and_partial_empty_is_not_failure(self) -> None:
        outcome, _ = self.run_search(lambda *_a, **_k: [], lambda *_a, **_k: [])
        self.assertEqual(len(outcome.resources), 0)
        self.assertEqual(outcome.provider_counts, {"OpenAlex": 0, "Crossref": 0})
        self.assertEqual(outcome.failed_providers, {})
        outcome, _ = self.run_search(
            lambda *_a, **_k: [],
            lambda *_a, **_k: [observation("Crossref", "C1", "10.5555/crossref")],
        )
        self.assertEqual(len(outcome.resources), 1)
        self.assertEqual(outcome.failed_providers, {})

    def test_one_combined_history_entry_restores_snapshot_order(self) -> None:
        from app.core.search_service import list_history

        outcome, db_path = self.run_search(
            lambda *_a, **_k: [
                observation("OpenAlex", "W1", "10.5555/one", title="One"),
                observation("OpenAlex", "W2", "10.5555/two", title="Two"),
            ],
            lambda *_a, **_k: [observation("Crossref", "C1", "10.5555/one", title="One")],
        )
        history = list_history(db_path)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].result_count, 2)
        with ResearchStore(db_path) as store:
            restored = store.get_search_resources(history[0].id)
        self.assertEqual([item.title for item in restored], [item.title for item in outcome.resources])
        self.assertEqual({item.source_provider for item in restored[0].provenance},
                         {"OpenAlex", "Crossref"})

    def test_partial_provider_status_and_counts_are_saved_in_history_metadata(self) -> None:
        from app.core.search_service import list_history, search_all_and_save

        def failed(*_args, **_kwargs):
            raise RuntimeError("OpenAlex rate limited")

        db_path = Path(__file__).parent / f".combined-partial-{id(self)}.sqlite"
        self.addCleanup(db_path.unlink, missing_ok=True)
        search_all_and_save(
            "flood prediction", db_path=db_path, openalex_provider=failed,
            crossref_provider=lambda *_a, **_k: [
                observation("Crossref", "C1", "10.5555/partial")
            ],
        )
        history = list_history(db_path)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].metadata["status"], "partial")
        self.assertEqual(history[0].metadata["provider_counts"], {"OpenAlex": 0, "Crossref": 1})
        self.assertIn("rate limited", history[0].metadata["failed_providers"]["OpenAlex"])

    def test_v2_to_v3_migration_preserves_existing_search_snapshot(self) -> None:
        db_path = Path(__file__).parent / f".combined-v2-migration-{id(self)}.sqlite"
        self.addCleanup(db_path.unlink, missing_ok=True)
        original = observation("OpenAlex", "W-OLD", "10.5555/old", title="Historical snapshot")
        with ResearchStore(db_path) as store:
            search_id = store.save_search("historical topic", [original], searched_at=STAMP)
            before_migration = store.get_search_resources(search_id)

        connection = sqlite3.connect(db_path)
        try:
            connection.execute("ALTER TABLE searches DROP COLUMN metadata_json")
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        finally:
            connection.close()

        with ResearchStore(db_path) as migrated:
            history = migrated.list_searches()
            snapshot = migrated.get_search_resources(search_id)
            self.assertEqual(migrated.schema_version, 3)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].metadata, {})
        self.assertEqual(history[0].searched_at, STAMP)
        self.assertEqual(snapshot, before_migration)


class CombinedSearchCliTests(unittest.TestCase):
    def test_provider_all_is_available_and_renders_combined_counts_and_sources(self) -> None:
        from app.cli.main import build_parser, main

        args = build_parser().parse_args([
            "search", "flood prediction", "--type", "papers", "--provider", "all"
        ])
        self.assertEqual(args.provider, "all")
        merged = deduplicate_resources([
            observation("OpenAlex", "W1", "10.5555/combined"),
            observation("Crossref", "C1", "10.5555/combined"),
        ])
        outcome = SimpleNamespace(
            resources=merged, provider_counts={"OpenAlex": 15, "Crossref": 18},
            failed_providers={}, duplicates_merged=1,
        )
        output = io.StringIO()
        with (patch("app.cli.main.search_all_and_save", return_value=outcome) as search,
              contextlib.redirect_stdout(output)):
            self.assertEqual(main(["search", "flood prediction", "--provider", "all"]), 0)
        self.assertIn("OpenAlex: 15 papers", output.getvalue())
        self.assertIn("Crossref: 18 papers", output.getvalue())
        self.assertIn("Combined results: 1 unique papers", output.getvalue())
        search.assert_called_once()

    def test_partial_combined_cli_output_names_failed_provider(self) -> None:
        from app.cli.main import main

        outcome = SimpleNamespace(
            resources=[observation("Crossref", "C1", "10.5555/partial")],
            provider_counts={"OpenAlex": 0, "Crossref": 1},
            failed_providers={"OpenAlex": "temporarily unavailable"},
            duplicates_merged=0,
        )
        output = io.StringIO()
        errors = io.StringIO()
        with (patch("app.cli.main.search_all_and_save", return_value=outcome),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            self.assertEqual(main(["search", "flood prediction", "--provider", "all"]), 0)
        self.assertIn("OpenAlex", output.getvalue())
        self.assertIn("temporarily unavailable", errors.getvalue())
        self.assertIn("partial", output.getvalue().lower())

    def test_both_provider_failures_exit_one_and_do_not_save_history(self) -> None:
        from app.cli.main import main
        from app.core.search_service import CombinedSearchError

        db_path = Path(__file__).parent / f".combined-cli-failed-{id(self)}.sqlite"
        self.addCleanup(db_path.unlink, missing_ok=True)
        output = io.StringIO()
        errors = io.StringIO()
        failure = CombinedSearchError({"OpenAlex": "offline", "Crossref": "timeout"})
        with (patch("app.cli.main.search_all_and_save", side_effect=failure),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            status = main(["search", "flood prediction", "--provider", "all", "--db", str(db_path)])
        self.assertEqual(status, 1)
        self.assertIn("OpenAlex", errors.getvalue())
        self.assertIn("Crossref", errors.getvalue())
        self.assertFalse(db_path.exists())

    def test_history_show_and_exports_expose_persisted_partial_metadata(self) -> None:
        from app.cli.main import main

        db_path = Path(__file__).parent / f".combined-cli-partial-{id(self)}.sqlite"
        csv_path = Path(__file__).parent / f"combined-partial-{id(self)}.csv"
        json_path = Path(__file__).parent / f"combined-partial-{id(self)}.json"
        self.addCleanup(db_path.unlink, missing_ok=True)
        self.addCleanup(csv_path.unlink, missing_ok=True)
        self.addCleanup(json_path.unlink, missing_ok=True)

        def unavailable(*_args, **_kwargs):
            raise RuntimeError("OpenAlex offline")

        crossref_results = [observation(
            "Crossref", "https://api.crossref.org/works/10.5555/partial",
            "10.5555/partial", links=(ResourceLink(
                LinkType.PUBLISHER, "https://publisher.example/partial", "Crossref"
            ),),
        )]
        search_output = io.StringIO()
        search_errors = io.StringIO()
        with (patch("app.core.search_service.search_openalex", side_effect=unavailable),
              patch("app.cli.main.search_crossref", return_value=crossref_results),
              contextlib.redirect_stdout(search_output), contextlib.redirect_stderr(search_errors)):
            self.assertEqual(main([
                "search", "flood prediction", "--provider", "all", "--db", str(db_path)
            ]), 0)
        self.assertIn("Search status: PARTIAL", search_output.getvalue())
        self.assertIn("OpenAlex search failed", search_errors.getvalue())

        with ResearchStore(db_path) as store:
            search_id = store.list_searches()[0].id

        history_output = io.StringIO()
        with contextlib.redirect_stdout(history_output):
            self.assertEqual(main(["history", "--db", str(db_path)]), 0)
        self.assertIn("PARTIAL", history_output.getvalue())

        show_output = io.StringIO()
        with contextlib.redirect_stdout(show_output):
            self.assertEqual(main(["show", str(search_id), "--db", str(db_path)]), 0)
        self.assertIn("Saved search status: PARTIAL", show_output.getvalue())
        self.assertIn("OpenAlex failed: OpenAlex offline", show_output.getvalue())

        for export_path, output_format in ((csv_path, "csv"), (json_path, "json")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([
                    "export", str(search_id), "--format", output_format,
                    "--output", str(export_path), "--db", str(db_path),
                ]), 0)
        with csv_path.open(encoding="utf-8", newline="") as stream:
            csv_row = next(csv.DictReader(stream))
        json_row = json.loads(json_path.read_text(encoding="utf-8"))[0]
        self.assertEqual(csv_row["search_status"], "partial")
        self.assertIn("OpenAlex offline", csv_row["failed_providers"])
        self.assertEqual(json_row["search_metadata"]["status"], "partial")
        self.assertIn("OpenAlex offline", json.dumps(json_row["search_metadata"]["failed_providers"]))

    def test_all_providers_are_rejected_for_dataset_search(self) -> None:
        from app.cli.main import main

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as result:
            main(["search", "flood prediction", "--type", "datasets", "--provider", "all"])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("--provider applies only to paper searches", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
