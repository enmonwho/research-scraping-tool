"""Offline tests for Crossref works search and resource normalization."""

from __future__ import annotations

import io
import json
import contextlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request
from unittest.mock import patch

from app.models import LinkType, ResourceType
from app.providers.crossref import CrossrefError, normalize_work, search_papers
from app.core.search_service import get_history_resources, list_history, search_and_save
from app.providers.openalex import normalize_work as normalize_openalex_work


FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class FakeOpener:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[Request, float]] = []

    def __call__(self, request: Request, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResponse(outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode("utf-8"))


def load_fixture() -> dict:
    return json.loads((FIXTURES / "crossref_success.json").read_text(encoding="utf-8"))


def http_error(status: int) -> HTTPError:
    return HTTPError("https://api.crossref.org/works", status, f"HTTP {status}", {}, io.BytesIO(b"error"))


class CrossrefNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = load_fixture()
        self.work = self.payload["message"]["items"][0]

    def test_search_retrieves_and_normalizes_real_work_records(self) -> None:
        opener = FakeOpener(self.payload)
        papers = search_papers("flood prediction", limit=2, timeout=3.5, contact_email="researcher@example.org", opener=opener)
        self.assertEqual(len(papers), 2)
        paper = papers[0]
        self.assertEqual(paper.resource_type, ResourceType.PAPER)
        self.assertEqual(paper.source_provider, "Crossref")
        self.assertEqual(paper.title, "Flood prediction with community observations")
        self.assertEqual(paper.authors, ("Amina Researcher", "Ben Scientist"))
        self.assertEqual(paper.publication_year, 2024)
        self.assertEqual(paper.doi, "10.5555/crossref.example.2024")
        self.assertEqual(paper.original_metadata, self.work)
        self.assertIsNotNone(paper.retrieved_at.tzinfo)
        request, timeout = opener.calls[0]
        self.assertEqual(timeout, 3.5)
        self.assertEqual(request.get_method(), "GET")
        params = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(params["query"], ["flood prediction"])
        self.assertEqual(params["rows"], ["2"])
        self.assertEqual(params["mailto"], ["researcher@example.org"])
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertIn("crossref", headers["user-agent"].lower())
        self.assertIn("researcher@example.org", headers["user-agent"])

    def test_empty_results_are_empty(self) -> None:
        self.assertEqual(search_papers("no match", opener=FakeOpener({"message": {"items": []}})), [])

    def test_missing_metadata_remains_unknown(self) -> None:
        resource = normalize_work({"DOI": "10.1234/sparse"})
        self.assertIsNone(resource.title)
        self.assertIsNone(resource.authors)
        self.assertIsNone(resource.description)
        self.assertIsNone(resource.publication_year)
        self.assertIsNone(resource.license)
        self.assertIsNone(resource.is_open_access)
        self.assertEqual(resource.downloadable_files, ())

    def test_incomplete_publication_date_uses_available_year(self) -> None:
        papers = search_papers("date", opener=FakeOpener(self.payload))
        self.assertEqual(papers[1].publication_year, 2021)
        self.assertIsNone(normalize_work({"published-online": {"date-parts": [[]]}}).publication_year)
        self.assertIsNone(normalize_work({"published-online": {"date-parts": [["2020", 3]]}}).publication_year)

    def test_markup_abstract_becomes_readable_plain_text(self) -> None:
        resource = normalize_work(self.work)
        self.assertEqual(resource.description, "Reliable flood forecasts & early warnings.")
        self.assertNotIn("<jats:", resource.description or "")

    def test_publisher_license_and_supplied_links_are_preserved(self) -> None:
        resource = normalize_work(self.work)
        self.assertEqual(resource.license, "https://creativecommons.org/licenses/by/4.0/")
        self.assertTrue(any(link.kind == LinkType.DOI and link.url == "https://doi.org/10.5555/crossref.example.2024" for link in resource.links))
        self.assertTrue(any(link.kind == LinkType.LANDING_PAGE and link.url == self.work["URL"] for link in resource.links))
        self.assertTrue(any(link.kind == LinkType.PUBLISHER and link.url == "https://publisher.example/article/123" for link in resource.links))
        self.assertEqual(len(resource.downloadable_files), 1)
        file = resource.downloadable_files[0]
        self.assertEqual(file.url, "https://publisher.example/files/123.pdf")
        self.assertEqual(file.mime_type, "application/pdf")
        self.assertEqual(file.file_format, "pdf")

    def test_authors_date_and_safe_metadata_handling(self) -> None:
        resource = normalize_work({
            "author": [{"given": "One", "family": "Author"}, {"name": "Consortium"}, {"given": "OnlyGiven"}],
            "published": {"date-parts": [[2020, 9, 1]]},
            "abstract": "<script>alert('x')</script><p>Safe <em>text</em></p>",
            "link": [{"URL": "javascript:alert(1)", "content-type": "application/pdf"}],
        })
        self.assertEqual(resource.authors, ("One Author", "Consortium", "OnlyGiven"))
        self.assertEqual(resource.publication_year, 2020)
        self.assertNotIn("<script", resource.description or "")
        self.assertEqual(resource.downloadable_files, ())

    def test_doi_normalization(self) -> None:
        for supplied in ("https://doi.org/10.1234/AbC.XY", "doi:10.1234/AbC.XY", "10.1234/AbC.XY"):
            with self.subTest(supplied=supplied):
                self.assertEqual(normalize_work({"DOI": supplied}).doi, "10.1234/abc.xy")
        self.assertIsNone(normalize_work({"DOI": "https://example.org/10.1234/wrong"}).doi)

    def test_retrieval_timestamp_can_be_injected(self) -> None:
        timestamp = datetime(2024, 1, 2, tzinfo=timezone.utc)
        self.assertEqual(normalize_work({}, retrieved_at=timestamp).retrieved_at, timestamp)


class CrossrefRequestTests(unittest.TestCase):
    def test_http_400_fails_without_retry(self) -> None:
        opener = FakeOpener(http_error(400), load_fixture())
        with self.assertRaises(CrossrefError):
            search_papers("invalid", opener=opener, sleep=lambda _: None)
        self.assertEqual(len(opener.calls), 1)

    def test_http_429_retries_then_succeeds(self) -> None:
        opener = FakeOpener(http_error(429), load_fixture())
        delays: list[float] = []
        self.assertEqual(len(search_papers("limited", opener=opener, sleep=delays.append)), 2)
        self.assertEqual((len(opener.calls), len(delays)), (2, 1))

    def test_http_500_retries_only_to_configured_bound(self) -> None:
        opener = FakeOpener(http_error(500), http_error(500), http_error(500), load_fixture())
        delays: list[float] = []
        with self.assertRaises(CrossrefError):
            search_papers("server error", max_retries=2, opener=opener, sleep=delays.append)
        self.assertEqual((len(opener.calls), len(delays)), (3, 2))

    def test_network_timeout_retries_within_bound(self) -> None:
        opener = FakeOpener(TimeoutError("timed out"), load_fixture())
        self.assertEqual(len(search_papers("timeout", opener=opener, sleep=lambda _: None)), 2)
        self.assertEqual(len(opener.calls), 2)

    def test_malformed_json_and_bad_response_shape_raise_provider_error(self) -> None:
        for payload in (b"{invalid", {"message": {"items": "not a list"}}, {"message": {}}):
            with self.subTest(payload=payload), self.assertRaises(CrossrefError):
                search_papers("malformed", opener=FakeOpener(payload), sleep=lambda _: None)

    def test_limit_and_retry_validation(self) -> None:
        for kwargs in ({"limit": 0}, {"limit": 21}, {"max_retries": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                search_papers("topic", opener=FakeOpener(), **kwargs)


class CrossrefIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "crossref.db"

    def test_crossref_and_openalex_same_doi_merge_without_losing_identity_or_links(self) -> None:
        crossref = normalize_work({
            "DOI": "https://doi.org/10.5555/shared.paper",
            "URL": "https://api.crossref.org/works/10.5555/shared.paper",
            "title": ["Shared DOI paper"],
            "link": [{"URL": "https://publisher.example/shared", "content-type": "text/html"}],
        })
        openalex = normalize_openalex_work({
            "id": "https://openalex.org/W-SHARED",
            "doi": "10.5555/SHARED.PAPER",
            "display_name": "Shared DOI paper",
            "primary_location": {"landing_page_url": "https://openalex.example/shared"},
        })
        merged = search_and_save("shared topic", db_path=self.db, provider=lambda *_a, **_kw: [crossref, openalex])
        self.assertEqual(len(merged), 1)
        result = merged[0]
        self.assertEqual({item.source_provider for item in result.provenance}, {"Crossref", "OpenAlex"})
        self.assertEqual({item.external_id for item in result.provenance}, {crossref.external_id, openalex.external_id})
        self.assertGreaterEqual(len(result.links), 3)
        self.assertTrue(any(link.url == "https://publisher.example/shared" for link in result.links))
        self.assertTrue(any(link.url == "https://openalex.example/shared" for link in result.links))

    def test_conflicting_dois_stay_separate(self) -> None:
        first = normalize_work({"DOI": "10.5555/first", "title": ["Same title"], "author": [{"name": "A"}]})
        second = normalize_work({"DOI": "10.5555/second", "title": ["Same title"], "author": [{"name": "A"}]})
        saved = search_and_save("conflicting", db_path=self.db, provider=lambda *_a, **_kw: [first, second])
        self.assertEqual(len(saved), 2)

    def test_crossref_search_saves_history_and_exports_csv_and_json(self) -> None:
        resource = normalize_work(load_fixture()["message"]["items"][0])
        search_and_save("flood prediction", db_path=self.db, provider=lambda *_a, **_kw: [resource])
        searches = list_history(self.db)
        self.assertEqual(len(searches), 1)
        self.assertEqual(searches[0].result_count, 1)
        restored = get_history_resources(searches[0].id, self.db)[0]
        self.assertEqual(restored.source_provider, "Crossref")
        self.assertEqual(restored.original_metadata, resource.original_metadata)

        from app.cli.main import main
        for file_format in ("csv", "json"):
            output = self.root / f"papers.{file_format}"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main(["export", str(searches[0].id), "--format", file_format, "--output", str(output), "--db", str(self.db)])
            self.assertEqual(code, 0)
            contents = output.read_text(encoding="utf-8")
            self.assertIn("Crossref", contents)
            self.assertIn("10.5555/crossref.example.2024", contents)
            self.assertIn("publisher.example/files/123.pdf", contents)

    def test_cli_provider_selection_default_and_dataset_rejection(self) -> None:
        from app.cli.main import main
        with patch("app.cli.main.search_and_save", return_value=[] ) as save, patch("app.cli.main.search_crossref", return_value=[]):
            out, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errors):
                self.assertEqual(main(["search", "topic", "--provider", "crossref", "--db", str(self.db)]), 0, errors.getvalue())
            self.assertEqual(save.call_args.kwargs["provider"]("topic", limit=3, timeout=4), [])
            self.assertEqual(save.call_args.kwargs["provider"].__name__, "<lambda>")

        with patch("app.cli.main.search_and_save", return_value=[]) as save:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["search", "topic", "--db", str(self.db)]), 0)
            self.assertNotIn("provider", save.call_args.kwargs)

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as result:
            main(["search", "topic", "--type", "datasets", "--provider", "crossref", "--db", str(self.db)])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("only to paper searches", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
