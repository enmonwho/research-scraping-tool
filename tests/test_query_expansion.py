"""Deterministic query expansion and expanded-search regression tests."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.cli.main import main, write_export
from app.core.deduplication import deduplicate_resources
from app.core.query_expansion import expand_queries, expand_query_details
from app.core.search_service import expanded_search_and_save, search_and_save
from app.models import ResearchResource, ResourceType
from app.storage.sqlite import ResearchStore


STAMP = datetime(2025, 1, 1, tzinfo=timezone.utc)


def paper(provider: str, external_id: str, doi: str, title: str) -> ResearchResource:
    return ResearchResource(
        resource_type=ResourceType.PAPER, title=title, description=None,
        authors=None, publication_year=2025, source_provider=provider,
        external_id=external_id, doi=doi, keywords=None, retrieved_at=STAMP,
        license=None, is_open_access=None,
    )


class QueryExpansionTests(unittest.TestCase):
    def test_deterministic_and_preserves_original_as_first_query(self):
        original = "machine learning for flood prediction"
        expected = expand_queries(original)
        self.assertEqual(expected[0], original)
        self.assertEqual(expand_queries(original), expected)
        self.assertGreaterEqual(len(expected), 2)

    def test_rules_produce_a_conservative_academic_variant(self):
        expanded = expand_queries("machine learning for flood prediction")
        self.assertIn("machine learning for flood prediction", expanded)
        self.assertTrue(any("flood forecasting" in value for value in expanded[1:]))
        self.assertTrue(any("using machine learning" in value for value in expanded[1:]))

    def test_query_rule_details_are_stable_and_explain_every_variation(self):
        topic = "machine learning for flood prediction"
        details = expand_query_details(topic, limit=4)
        self.assertEqual(details, expand_query_details(topic, limit=4))
        self.assertEqual(details[0], {"query": topic, "rule": "original topic"})
        self.assertTrue(all(item["rule"].strip() for item in details[1:]))
        self.assertTrue(all(item["query"] for item in details))

    def test_duplicate_variations_are_removed_case_insensitively(self):
        result = expand_queries("AI for AI")
        self.assertEqual(len(result), len({item.casefold() for item in result}))

    def test_limit_counts_generated_variations_and_is_bounded(self):
        self.assertEqual(len(expand_queries("machine learning for flood prediction", limit=2)), 3)
        self.assertEqual(expand_queries("flood prediction", limit=0), ["flood prediction"])
        with self.assertRaises(ValueError):
            expand_queries("flood prediction", limit=5)

    def test_unknown_terms_remain_intact_without_speculative_expansion(self):
        topic = "xylophonic quasar telemetry"
        self.assertEqual(expand_queries(topic), [topic])

    def test_invalid_topics_are_rejected(self):
        for topic in ("", "   ", "x" * 201, None):
            with self.subTest(topic=topic), self.assertRaises(ValueError):
                expand_queries(topic)


class ExpandedSearchIntegrationTests(unittest.TestCase):
    def run_expanded(self, providers, *, topic="flood prediction", expansion_limit=2):
        database = Path(__file__).parent / f".expanded-{id(self)}-{len(self._cleanups)}.sqlite"
        self.addCleanup(database.unlink, missing_ok=True)
        outcome = expanded_search_and_save(
            topic, providers=providers, expansion_limit=expansion_limit,
            limit=10, timeout=2.0, db_path=database,
        )
        return outcome, database

    def test_single_provider_expands_once_and_preserves_search_provenance(self):
        provider = Mock(return_value=[paper("OpenAlex", "W1", "10.5555/shared", "Flood paper")])
        outcome, database = self.run_expanded((("OpenAlex", provider),))
        self.assertEqual(provider.call_count, len(outcome.queries))
        self.assertEqual(outcome.provider_requests, len(outcome.queries))
        self.assertEqual(outcome.records_retrieved, len(outcome.queries))
        self.assertEqual(outcome.duplicates_removed, len(outcome.queries) - 1)
        self.assertEqual(len(outcome.resources), 1)
        with ResearchStore(database) as store:
            searches = store.list_searches()
            restored = store.get_search_resources(outcome.search_id)
        self.assertEqual(len(searches), 1)
        saved_metadata = searches[0].metadata
        metadata = saved_metadata["query_expansion"]
        self.assertEqual(searches[0].topic, "flood prediction")
        self.assertEqual(metadata["queries"], outcome.queries)
        self.assertEqual(metadata["original_topic"], "flood prediction")
        self.assertEqual(metadata["provider_search_requests"], len(outcome.queries))
        self.assertEqual(metadata["query_rules"], expand_query_details("flood prediction", limit=2))
        self.assertEqual(restored[0].title, "Flood paper")

    def test_combined_providers_run_per_query_and_merge_stably_with_request_cap(self):
        first = paper("OpenAlex", "W1", "10.5555/shared", "Shared paper")
        second = paper("Crossref", "10.5555/shared", "10.5555/shared", "Duplicate paper")
        distinct = paper("OpenAlex", "W2", "10.5555/other", "Second paper")
        openalex = Mock(side_effect=lambda query, **kwargs: [first, distinct])
        crossref = Mock(side_effect=lambda query, **kwargs: [second])
        outcome, database = self.run_expanded(
            (("OpenAlex", openalex), ("Crossref", crossref)), expansion_limit=1
        )
        self.assertLessEqual(outcome.provider_requests, 10)
        self.assertEqual(outcome.provider_requests, len(outcome.queries) * 2)
        self.assertEqual([item.title for item in outcome.resources], ["Shared paper", "Second paper"])
        self.assertEqual(outcome.duplicates_removed, len(outcome.queries) * 3 - 2)
        with ResearchStore(database) as store:
            saved_metadata = store.list_searches()[0].metadata
            metadata = saved_metadata["query_expansion"]
        self.assertEqual(saved_metadata["providers"], ["OpenAlex", "Crossref"])
        self.assertEqual(metadata["provider_search_requests"], outcome.provider_requests)

    def test_original_only_vs_expanded_retrieval_comparison_is_reproducible(self):
        first = paper("OpenAlex", "W1", "10.5555/shared", "First")
        second = paper("OpenAlex", "W2", "10.5555/second", "Second")
        duplicate = paper("OpenAlex", "W1-copy", "10.5555/shared", "First duplicate")
        third = paper("OpenAlex", "W3", "10.5555/third", "Third")
        original_db = Path(__file__).parent / f".comparison-original-{id(self)}.sqlite"
        expanded_db = Path(__file__).parent / f".comparison-expanded-{id(self)}.sqlite"
        self.addCleanup(original_db.unlink, missing_ok=True)
        self.addCleanup(expanded_db.unlink, missing_ok=True)
        original_provider = Mock(return_value=[first, second])
        original_unique = search_and_save(
            "flood prediction", db_path=original_db, provider=original_provider
        )

        responses = [[first, second], [duplicate, third]]
        expanded_provider = Mock(side_effect=responses)
        expanded = expanded_search_and_save(
            "flood prediction", db_path=expanded_db, expansion_limit=1,
            providers=(("OpenAlex", expanded_provider),),
        )

        comparison = {
            "original_only": {
                "provider_requests": original_provider.call_count,
                "records_retrieved": 2,
                "unique_resources": len(original_unique),
                "duplicates_removed": 2 - len(original_unique),
            },
            "expanded": {
                "provider_requests": expanded.provider_requests,
                "records_retrieved": expanded.records_retrieved,
                "unique_resources": expanded.unique_resources,
                "duplicates_removed": expanded.duplicates_removed,
            },
        }
        self.assertEqual(comparison["original_only"], {
            "provider_requests": 1, "records_retrieved": 2,
            "unique_resources": 2, "duplicates_removed": 0,
        })
        self.assertEqual(comparison["expanded"], {
            "provider_requests": 2, "records_retrieved": 4,
            "unique_resources": 3, "duplicates_removed": 1,
        })

    def test_dataset_provider_expands_and_preserves_dataset_type(self):
        dataset = ResearchResource(
            resource_type=ResourceType.DATASET, title="Flood observations", description=None,
            authors=None, publication_year=2024, source_provider="Harvard Dataverse",
            external_id="doi:10.7910/test", doi="10.7910/test", keywords=None,
            retrieved_at=STAMP, license="CC0", is_open_access=True,
        )
        provider = Mock(return_value=[dataset])
        outcome, _ = self.run_expanded((("Harvard Dataverse", provider),), expansion_limit=1)
        self.assertEqual(provider.call_count, len(outcome.queries))
        self.assertTrue(all(item.resource_type == ResourceType.DATASET for item in outcome.resources))

    def test_cli_routes_expanded_search_to_each_existing_provider_configuration(self):
        expected = {
            (): ["OpenAlex"],
            ("--provider", "crossref"): ["Crossref"],
            ("--provider", "all"): ["OpenAlex", "Crossref"],
            ("--type", "datasets"): ["Harvard Dataverse"],
        }
        for options, names in expected.items():
            with self.subTest(options=options):
                outcome = SimpleNamespace(
                    resources=[], queries=["flood prediction", "flood forecasting"],
                    providers=names, provider_requests=len(names) * 2,
                    records_retrieved=0, unique_resources=0, duplicates_removed=0,
                    failed_queries={},
                )
                output = io.StringIO()
                with patch("app.cli.main.expanded_search_and_save", return_value=outcome) as search, \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(main(["search", "flood prediction", "--expand",
                                           *options, "--db", "ignored.sqlite"]), 0)
                actual_providers = search.call_args.kwargs["providers"]
                self.assertEqual([name for name, _provider in actual_providers], names)
                self.assertIn("Query 2 (expanded): flood forecasting", output.getvalue())

    def test_cli_rejects_expand_limit_without_expansion_and_out_of_range(self):
        for arguments in (("--expand-limit", "1"), ("--expand", "--expand-limit", "99")):
            errors = io.StringIO()
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(errors), \
                    self.assertRaises(SystemExit) as result:
                main(["search", "flood prediction", *arguments])
            self.assertEqual(result.exception.code, 2)

    def test_show_displays_saved_queries_method_and_provider_metadata(self):
        metadata = {
            "providers": ["OpenAlex", "Crossref"],
            "query_expansion": {
                "method": "deterministic_keyword_rules", "version": "keyword-rules-v1",
                "queries": ["flood prediction", "flood forecasting"],
            },
        }
        output = io.StringIO()
        with patch("app.cli.main._resolve_search", return_value=("flood prediction", [], metadata)), \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(["show", "7"]), 0)
        rendered = output.getvalue()
        self.assertIn("deterministic_keyword_rules (keyword-rules-v1)", rendered)
        self.assertIn("Providers: OpenAlex, Crossref", rendered)
        self.assertIn("Query 2: flood forecasting", rendered)

    def test_partial_query_failure_is_recorded_and_successes_are_saved(self):
        calls = 0

        def flaky(query, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary provider failure")
            return [paper("OpenAlex", "W1", "10.5555/shared", "Flood paper")]

        outcome, database = self.run_expanded((("OpenAlex", flaky),), expansion_limit=1)
        self.assertEqual(len(outcome.failed_queries), 1)
        with ResearchStore(database) as store:
            metadata = store.list_searches()[0].metadata
        self.assertEqual(metadata["status"], "partial")
        self.assertIn("temporary provider failure", json.dumps(metadata["failed_providers"]))

    def test_deduplication_keeps_first_position_and_merges_provider_provenance(self):
        first = paper("OpenAlex", "W1", "10.5555/shared", "Paper about floods")
        second = paper("Crossref", "10.5555/shared", "10.5555/shared", "Paper about floods")
        distinct = paper("OpenAlex", "W2", "10.5555/other", "Other paper")
        merged = deduplicate_resources([first, second, distinct])
        self.assertEqual([item.title for item in merged], [first.title, distinct.title])
        self.assertEqual({item.source_provider for item in merged[0].provenance}, {"OpenAlex", "Crossref"})

    def test_unicode_titles_survive_storage_and_json_export(self):
        record = paper("OpenAlex", "W-unicode", "10.5555/unicode", "Floods — études naïve 雪")
        database = Path(__file__).parent / f".unicode-{id(self)}.sqlite"
        output = Path(__file__).parent / f"unicode-{id(self)}.json"
        self.addCleanup(database.unlink, missing_ok=True)
        self.addCleanup(output.unlink, missing_ok=True)
        with ResearchStore(database) as store:
            metadata = {
                "status": "complete", "providers": ["OpenAlex"],
                "query_expansion": {
                    "original_topic": "flood prediction",
                    "queries": ["flood prediction", "flood forecasting"],
                    "method": "deterministic_keyword_rules",
                    "version": "keyword-rules-v1",
                },
            }
            search_id = store.save_search("flood prediction", [record], metadata=metadata)
            restored = store.get_search_resources(search_id)
            stored_metadata = store.list_searches()[0].metadata
        self.assertEqual(restored[0].title, record.title)
        self.assertEqual(stored_metadata["query_expansion"]["queries"],
                         ["flood prediction", "flood forecasting"])
        write_export(restored, output, "json", search_metadata=stored_metadata)
        self.assertIn("雪", output.read_text(encoding="utf-8"))
        csv_output = Path(__file__).parent / f"unicode-{id(self)}.csv"
        self.addCleanup(csv_output.unlink, missing_ok=True)
        write_export(restored, csv_output, "csv", search_metadata=stored_metadata)
        with csv_output.open(encoding="utf-8", newline="") as stream:
            row = next(csv.DictReader(stream))
        csv_search_metadata = json.loads(row["search_metadata"])
        self.assertEqual(csv_search_metadata["query_expansion"]["queries"],
                         ["flood prediction", "flood forecasting"])
        self.assertEqual(csv_search_metadata["providers"], ["OpenAlex"])
        self.assertEqual(csv_search_metadata["query_expansion"]["method"],
                         "deterministic_keyword_rules")

    def test_restricted_cp1252_console_does_not_raise_or_change_saved_unicode(self):
        title = "Étude des crues 雪"
        record = paper("OpenAlex", "W-unicode", "10.5555/unicode", title)
        # TextIOWrapper with strict cp1252 reproduces the Windows console failure.
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        try:
            with patch("sys.stdout", stream), \
                    patch("app.cli.main._resolve_search", return_value=("flood prediction", [record], {})):
                self.assertEqual(main(["show", "1"]), 0)
                stream.flush()
                rendered = stream.buffer.getvalue().decode("cp1252")
                self.assertIn(r"\u96ea", rendered)
        finally:
            stream.detach()
        self.assertEqual(record.title, title)


if __name__ == "__main__":
    unittest.main()
