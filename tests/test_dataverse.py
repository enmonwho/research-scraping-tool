"""Synthetic Harvard Dataverse API and integration acceptance tests."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

from app.core.search_service import get_history_resources, list_history, search_and_save
from app.models import LinkType, ResourceType
from app.providers.dataverse import DataverseError, search_datasets
from app.storage import ResearchStore


FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class FakeOpener:
    """Return queued outcomes; successful calls are JSON API documents."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[Request, float]] = []

    def __call__(self, request: Request, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        payload = outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode("utf-8")
        return FakeResponse(payload)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def http_error(status: int) -> HTTPError:
    return HTTPError("https://dataverse.harvard.edu/api/search", status, f"HTTP {status}", {}, io.BytesIO(b"error"))


def successful_opener() -> FakeOpener:
    return FakeOpener(
        load_fixture("dataverse_search_success.json"),
        load_fixture("dataverse_dataset_detail.json"),
        {"status": "OK", "data": {"id": 654, "persistentId": "doi:10.7910/DVN/XYZ987", "latestVersion": {"metadataBlocks": {}}}},
    )


class DataverseProviderTests(unittest.TestCase):
    def test_search_and_dataset_details_are_requested_and_normalized(self) -> None:
        opener = successful_opener()
        resources = search_datasets("urban flooding", limit=2, timeout=3.25, opener=opener, sleep=lambda _: None)

        self.assertEqual(len(resources), 2)
        dataset = resources[0]
        self.assertEqual(dataset.resource_type, ResourceType.DATASET)
        self.assertEqual(dataset.title, "Urban Flood Observations")
        self.assertEqual(dataset.description, "Measurements collected during flood events.")
        self.assertEqual(dataset.authors, ("A. Researcher", "B. Scientist"))
        self.assertEqual(dataset.publication_year, 2024)
        self.assertEqual(dataset.keywords, ("flooding", "urban hydrology"))
        self.assertEqual(dataset.license, "CC0 1.0")
        self.assertEqual(dataset.source_provider, "Harvard Dataverse")
        self.assertEqual(dataset.doi, "10.7910/dvn/abc123")
        self.assertTrue(dataset.external_id)
        self.assertEqual(len(dataset.provenance), 1)
        self.assertEqual(dataset.provenance[0].source_provider, "Harvard Dataverse")
        self.assertIsNotNone(dataset.provenance[0].external_id)
        self.assertTrue(dataset.provenance[0].original_metadata)
        self.assertTrue(any(link.kind == LinkType.LANDING_PAGE for link in dataset.links))
        self.assertEqual(len(dataset.downloadable_files), 2)
        self.assertEqual(dataset.downloadable_files[0].filename, "rainfall.csv")
        self.assertEqual(dataset.downloadable_files[0].file_format, "csv")
        self.assertEqual(dataset.downloadable_files[0].mime_type, "text/csv")
        self.assertTrue(dataset.downloadable_files[0].is_open_access)
        self.assertFalse(dataset.downloadable_files[1].is_open_access)
        # File IDs and restriction details remain available in the provider snapshot.
        self.assertIn("files", json.dumps(dataset.original_metadata))
        self.assertIn("9001", json.dumps(dataset.original_metadata))
        self.assertIn("9002", json.dumps(dataset.original_metadata))
        self.assertEqual(
            [(item["id"], item["restricted"]) for item in dataset.original_metadata["files"]],
            [(9001, False), (9002, True)],
        )

        search_request, timeout = opener.calls[0]
        params = parse_qs(urlparse(search_request.full_url).query)
        self.assertEqual(timeout, 3.25)
        self.assertEqual(params["q"], ["urban flooding"])
        self.assertEqual(params["type"], ["dataset"])
        self.assertEqual(params["per_page"], ["2"])
        self.assertEqual(params["metadata_fields"], ["citation:*"])
        self.assertEqual(params["show_entity_ids"], ["true"])
        self.assertIn("persistentId=doi%3A10.7910%2FDVN%2FABC123", opener.calls[1][0].full_url)
        self.assertEqual(len(opener.calls), 3)

    def test_empty_results_and_missing_metadata_stay_empty_or_unknown(self) -> None:
        empty = search_datasets("no match", opener=FakeOpener(load_fixture("dataverse_search_empty.json")))
        self.assertEqual(empty, [])

        minimal = {"status": "OK", "data": {"count": 1, "items": [{"dataset_id": 77}]}}
        detail = {"status": "OK", "data": {"id": 77, "latestVersion": {"metadataBlocks": {}, "files": []}}}
        [resource] = search_datasets("sparse", opener=FakeOpener(minimal, detail))
        self.assertIsNone(resource.title)
        self.assertIsNone(resource.description)
        self.assertIsNone(resource.authors)
        self.assertIsNone(resource.publication_year)
        self.assertIsNone(resource.doi)
        self.assertIsNone(resource.keywords)
        self.assertIsNone(resource.license)
        self.assertEqual(resource.downloadable_files, ())

    def test_explicit_empty_creators_and_keywords_are_distinct_from_missing(self) -> None:
        item = {"status": "OK", "data": {"count": 1, "items": [{"global_id": "hdl:1"}]}}
        detail = {"status": "OK", "data": {"latestVersion": {"metadataBlocks": {"citation": {"fields": [
            {"typeName": "author", "value": []}, {"typeName": "keyword", "value": []}
        ]}}}}}
        [resource] = search_datasets("empty values", opener=FakeOpener(item, detail))
        self.assertEqual(resource.authors, ())
        self.assertEqual(resource.keywords, ())

    def test_dataset_doi_representation_normalizes(self) -> None:
        item = {"status": "OK", "data": {"items": [{"global_id": "https://doi.org/10.7910/DVN/CaseSensitive"}]}}
        detail = {"status": "OK", "data": {"persistentId": "doi:10.7910/DVN/CaseSensitive", "latestVersion": {}}}
        [resource] = search_datasets("DOI", opener=FakeOpener(item, detail))
        self.assertEqual(resource.doi, "10.7910/dvn/casesensitive")

    def test_http_400_is_not_retried(self) -> None:
        opener = FakeOpener(http_error(400), load_fixture("dataverse_search_empty.json"))
        with self.assertRaises(DataverseError):
            search_datasets("bad request", opener=opener, sleep=lambda _: None)
        self.assertEqual(len(opener.calls), 1)

    def test_http_429_retries_then_succeeds_and_exhaustion_is_bounded(self) -> None:
        delays: list[float] = []
        opener = FakeOpener(http_error(429), load_fixture("dataverse_search_empty.json"))
        self.assertEqual(search_datasets("limited", opener=opener, sleep=delays.append), [])
        self.assertEqual((len(opener.calls), len(delays)), (2, 1))

        exhausted = FakeOpener(http_error(429), http_error(429), http_error(429), load_fixture("dataverse_search_empty.json"))
        delays.clear()
        with self.assertRaises(DataverseError):
            search_datasets("limited", max_retries=2, opener=exhausted, sleep=delays.append)
        self.assertEqual((len(exhausted.calls), len(delays)), (3, 2))

    def test_http_500_and_timeout_retries_are_bounded(self) -> None:
        server = FakeOpener(http_error(500), http_error(500), http_error(500), load_fixture("dataverse_search_empty.json"))
        with self.assertRaises(DataverseError):
            search_datasets("server error", max_retries=2, opener=server, sleep=lambda _: None)
        self.assertEqual(len(server.calls), 3)

        timeout = FakeOpener(TimeoutError("timed out"), TimeoutError("timed out"), load_fixture("dataverse_search_empty.json"))
        with self.assertRaises(DataverseError):
            search_datasets("timeout", max_retries=1, opener=timeout, sleep=lambda _: None)
        self.assertEqual(len(timeout.calls), 2)

    def test_malformed_json_or_response_shape_raises_provider_error(self) -> None:
        for bad in (b"{invalid", {"status": "ERROR"}, {"status": "OK", "data": {"items": "not a list"}}):
            with self.subTest(response=bad), self.assertRaises(DataverseError):
                search_datasets("bad response", opener=FakeOpener(bad), sleep=lambda _: None)

    def test_request_limit_validation(self) -> None:
        for limit in (0, -1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                search_datasets("topic", limit=limit, opener=FakeOpener())


class DataverseIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "dataverse.db"

    def test_dataset_history_persistence_reopen_and_json_export(self) -> None:
        opener = successful_opener()
        results = search_and_save(
            "urban flooding", limit=2, timeout=4.0, db_path=self.db,
            provider=lambda topic, *, limit, timeout: search_datasets(
                topic, limit=limit, timeout=timeout, opener=opener, sleep=lambda _: None
            ),
        )
        self.assertEqual(results[0].resource_type, ResourceType.DATASET)
        summaries = list_history(self.db)
        self.assertEqual(summaries[0].result_count, 2)
        restored = get_history_resources(summaries[0].id, self.db)
        self.assertEqual(restored[0].downloadable_files, results[0].downloadable_files)
        self.assertEqual(restored[0].provenance, results[0].provenance)
        self.assertTrue(restored[0].provenance)
        self.assertEqual(restored[0].provenance[0].source_provider, "Harvard Dataverse")
        with ResearchStore(self.db) as reopened:
            self.assertEqual(reopened.get_search_resources(summaries[0].id), restored)

        from app.cli.main import main
        output = Path(self.temp.name) / "datasets.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(["export", str(summaries[0].id), "--format", "json", "--output", str(output), "--db", str(self.db)])
        self.assertEqual(code, 0)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload[0]["resource_type"], "dataset")
        self.assertIn("downloadable_files", payload[0])
        self.assertIn("original_metadata", payload[0])

    def test_cli_dataset_mode_routes_provider_and_displays_dataset_links(self) -> None:
        from app.cli.main import main
        from app.providers.dataverse import search_datasets as find_datasets

        opener = successful_opener()
        found = find_datasets("urban flooding", limit=2, opener=opener, sleep=lambda _: None)
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with patch("app.cli.main.search_datasets", return_value=found) as provider:
                code = main(["search", "urban flooding", "--type", "datasets", "--limit", "2", "--db", str(self.db)])
        self.assertEqual(code, 0, errors.getvalue())
        provider.assert_called_once()
        self.assertIn("Urban Flood Observations", output.getvalue())
        self.assertIn("/api/access/datafile/9001", output.getvalue())

    def test_same_dataset_from_repeated_searches_is_deduplicated(self) -> None:
        from app.models import ResearchResource, ResourceProvenance
        from app.providers.dataverse import search_datasets as find_datasets

        # Repeated DOI representation from one provider must converge in storage.
        resource = find_datasets(
            "urban", opener=FakeOpener(
                {"status": "OK", "data": {"items": [{"global_id": "doi:10.7910/DVN/SAME", "dataset_id": 2}]}},
                {"status": "OK", "data": {"persistentId": "doi:10.7910/DVN/SAME", "latestVersion": {}}},
            ), sleep=lambda _: None,
        )[0]
        search_and_save("urban", db_path=self.db, provider=lambda *_args, **_kwargs: [resource])
        search_and_save("flood", db_path=self.db, provider=lambda *_args, **_kwargs: [resource])
        self.assertEqual(len(list_history(self.db)), 2)
        self.assertEqual(len(get_history_resources(1, self.db)), 1)
        self.assertEqual(len(get_history_resources(2, self.db)), 1)


if __name__ == "__main__":
    unittest.main()
