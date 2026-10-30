"""Offline tests for the OpenAlex provider using synthetic API responses."""

from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

from app.models import LinkType, ResourceType
from app.providers.openalex import OpenAlexError, normalize_work, search_papers


FIXTURE = Path(__file__).parent / "fixtures" / "openalex_success.json"
COMPLETE_FIXTURE = Path(__file__).parent / "fixtures" / "openalex_complete.json"
INCOMPLETE_FIXTURE = Path(__file__).parent / "fixtures" / "openalex_incomplete.json"
MALFORMED_ITEM_FIXTURE = Path(__file__).parent / "fixtures" / "openalex_malformed_item.json"


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
        return FakeResponse(outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode())


def http_error(status: int) -> HTTPError:
    return HTTPError(
        "https://api.openalex.org/works",
        status,
        f"HTTP {status}",
        {},
        io.BytesIO(b"error"),
    )


class OpenAlexProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_success_maps_normalized_resources_and_builds_work_search_request(self) -> None:
        opener = FakeOpener(self.fixture)
        papers = search_papers("urban heat islands", opener=opener)

        self.assertEqual(len(papers), 2)
        self.assertEqual(papers[0].title, "Urban heat exposure and public health")
        self.assertEqual(papers[0].publication_year, 2024)
        self.assertEqual(papers[0].resource_type, ResourceType.PAPER)
        self.assertEqual(papers[0].source_provider, "OpenAlex")
        self.assertEqual(papers[0].links[0].url, "https://doi.org/10.5555/example.2024.1")
        request, timeout = opener.calls[0]
        self.assertEqual(timeout, 10.0)
        self.assertEqual(request.get_method(), "GET")
        query = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(query["search"], ["urban heat islands"])
        self.assertEqual(query["filter"], ["type:article|preprint|conference-paper|review|data-paper|software-paper|report"])
        self.assertEqual(query["per_page"], ["20"])
        self.assertIn("abstract_inverted_index", query["select"][0])
        self.assertIn("type", query["select"][0].split(","))

    def test_maximum_limit_and_configurable_timeout_are_sent(self) -> None:
        opener = FakeOpener({"results": []})
        self.assertEqual(search_papers("topic", limit=20, timeout=3.5, opener=opener), [])
        request, timeout = opener.calls[0]
        self.assertEqual(parse_qs(urlparse(request.full_url).query)["per_page"], ["20"])
        self.assertEqual(timeout, 3.5)

    def test_empty_results_return_empty_list(self) -> None:
        self.assertEqual(search_papers("no match", opener=FakeOpener({"results": []})), [])

    def test_missing_metadata_is_preserved_as_none(self) -> None:
        result = {"results": [{}, {"display_name": "Only title"}, {"publication_year": 2020}]}
        papers = search_papers("partial metadata", opener=FakeOpener(result))
        self.assertEqual(
            [(paper.title, paper.publication_year, paper.external_id, paper.doi) for paper in papers],
            [(None, None, None, None), ("Only title", None, None, None), (None, 2020, None, None)],
        )
        self.assertIsNone(papers[0].authors)
        self.assertIsNone(papers[0].keywords)
        self.assertIsNone(papers[0].license)
        self.assertIsNone(papers[0].is_open_access)
        self.assertEqual(papers[0].links, ())
        self.assertEqual(papers[0].downloadable_files, ())

    def test_openalex_id_is_used_when_landing_page_url_is_missing(self) -> None:
        result = {"results": [{"id": "https://openalex.org/W123", "display_name": "Paper"}]}
        papers = search_papers("fallback URL", opener=FakeOpener(result))
        self.assertEqual(papers[0].external_id, "https://openalex.org/W123")

    def test_limit_outside_one_through_twenty_is_rejected(self) -> None:
        for limit in (0, 21):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "between 1 and 20"):
                search_papers("topic", limit=limit, opener=FakeOpener({"results": []}))

    def test_http_400_fails_without_retry(self) -> None:
        error = http_error(400)
        opener = FakeOpener(error, self.fixture)
        with self.assertRaises(OpenAlexError):
            search_papers("invalid", opener=opener, sleep=lambda _: None)
        self.assertEqual(len(opener.calls), 1)
        self.assertTrue(error.fp.closed)

    def test_complete_metadata_normalizes_authors_abstract_keywords_and_provenance(self) -> None:
        payload = json.loads(COMPLETE_FIXTURE.read_text(encoding="utf-8"))
        work = payload["results"][0]
        papers = search_papers("topic", opener=FakeOpener(payload))
        resource = papers[0]

        self.assertEqual(resource.title, "Urban heat exposure and public health")
        self.assertEqual(resource.description, "Urban heat exposure affects health.")
        self.assertEqual(resource.authors, ("A. Researcher", "B. Scientist"))
        self.assertEqual(resource.keywords, ("urban heat", "public health"))
        self.assertEqual(resource.external_id, "https://openalex.org/W1234567890")
        self.assertEqual(resource.doi, "10.1234/example.paper")
        self.assertEqual(resource.is_open_access, True)
        self.assertEqual(resource.license, None)  # primary location supplied no license
        self.assertIsNotNone(resource.retrieved_at.tzinfo)
        self.assertEqual(resource.original_metadata, work)
        self.assertTrue(any(link.kind == LinkType.DOI and link.url == work["doi"] for link in resource.links))
        self.assertTrue(any(link.kind == LinkType.LANDING_PAGE for link in resource.links))
        self.assertTrue(any(link.kind == LinkType.PUBLISHER for link in resource.links))
        self.assertTrue(any(link.kind == LinkType.REPOSITORY for link in resource.links))
        self.assertTrue(any(link.kind == LinkType.DIRECT_FILE for link in resource.links))
        self.assertEqual(len(resource.downloadable_files), 1)
        file = resource.downloadable_files[0]
        self.assertEqual(file.url, "https://repository.example.org/files/123.pdf")
        self.assertEqual(file.file_format, "pdf")
        self.assertEqual(file.mime_type, "application/pdf")
        self.assertIsNone(file.filename)
        self.assertEqual(file.is_open_access, True)

    def test_doi_inputs_normalize_to_bare_lowercase_identifier(self) -> None:
        for supplied in (
            "https://doi.org/10.1234/AbC.XY",
            "http://dx.doi.org/10.1234/AbC.XY",
            "doi:10.1234/AbC.XY",
            "10.1234/AbC.XY",
        ):
            with self.subTest(supplied=supplied):
                resource = normalize_work({"doi": supplied})
                self.assertEqual(resource.doi, "10.1234/abc.xy")
        self.assertIsNone(normalize_work({"doi": "https://example.org/10.1234/wrong"}).doi)

    def test_explicit_dataset_type_maps_to_dataset_resource_without_searching_datasets(self) -> None:
        resource = normalize_work({"type": "dataset", "display_name": "A described dataset"})
        self.assertEqual(resource.resource_type, ResourceType.DATASET)
        self.assertEqual(resource.title, "A described dataset")
        # The CLI search request still applies the paper-type filter (asserted above).

    def test_malformed_urls_are_ignored_without_losing_other_metadata(self) -> None:
        resource = normalize_work({
            "doi": "https://doi.org:bad/10.1234/example",
            "primary_location": {"landing_page_url": "http://[invalid", "pdf_url": "https://[invalid"},
            "open_access": {"oa_url": "http://[invalid", "is_oa": True},
            "display_name": "Metadata survives bad links",
        })
        self.assertEqual(resource.title, "Metadata survives bad links")
        self.assertIsNone(resource.doi)
        self.assertEqual(resource.links, ())
        self.assertEqual(resource.downloadable_files, ())

    def test_explicit_empty_authors_and_keywords_differ_from_missing(self) -> None:
        resource = normalize_work({"authorships": [], "keywords": []})
        self.assertEqual(resource.authors, ())
        self.assertEqual(resource.keywords, ())
        missing = normalize_work({})
        self.assertIsNone(missing.authors)
        self.assertIsNone(missing.keywords)

    def test_unknown_license_and_access_remain_unknown(self) -> None:
        resource = normalize_work({"primary_location": {"landing_page_url": "https://example.org/p"}})
        self.assertIsNone(resource.license)
        self.assertIsNone(resource.is_open_access)
        self.assertIsNone(resource.links[0].license)
        self.assertIsNone(resource.links[0].is_open_access)

    def test_incomplete_and_malformed_work_shapes_are_safe_and_do_not_invent_files(self) -> None:
        incomplete = json.loads(INCOMPLETE_FIXTURE.read_text(encoding="utf-8"))
        resources = search_papers("topic", opener=FakeOpener(incomplete))
        self.assertEqual(len(resources), 2)
        self.assertTrue(all(resource.downloadable_files == () for resource in resources))
        self.assertIsNone(resources[0].publication_year)
        self.assertEqual(resources[0].authors, ())
        self.assertEqual(resources[0].keywords, ())
        self.assertIsNone(resources[1].title)
        self.assertIsNone(resources[1].publication_year)

        malformed = json.loads(MALFORMED_ITEM_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(search_papers("topic", opener=FakeOpener(malformed)), [])

    def test_location_links_preserve_multiple_provider_urls_and_direct_file_evidence(self) -> None:
        resource = normalize_work({
            "primary_location": {
                "landing_page_url": "https://publisher.example/article",
                "source": {"type": "journal"}, "license": "cc-by"
            },
            "locations": [{
                "landing_page_url": "https://repo.example/item",
                "source": {"type": "repository"}, "license": "cc0"
            }],
            "open_access": {"oa_url": "https://oa.example/version", "is_oa": True},
        })
        self.assertEqual(
            [(link.kind, link.url) for link in resource.links],
            [
                (LinkType.LANDING_PAGE, "https://publisher.example/article"),
                (LinkType.PUBLISHER, "https://publisher.example/article"),
                (LinkType.REPOSITORY, "https://repo.example/item"),
                (LinkType.OPEN_ACCESS, "https://oa.example/version"),
            ],
        )
        self.assertEqual(resource.downloadable_files, ())
        self.assertEqual(resource.links[1].license, "cc-by")
        self.assertEqual(resource.links[2].license, "cc0")

    def test_http_429_retries_then_succeeds(self) -> None:
        error = http_error(429)
        opener = FakeOpener(error, self.fixture)
        delays: list[float] = []
        papers = search_papers("rate limited", opener=opener, sleep=delays.append)
        self.assertEqual(len(papers), 2)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(len(delays), 1)
        self.assertTrue(error.fp.closed)

    def test_http_429_exhaustion_reports_rate_limit_error(self) -> None:
        opener = FakeOpener(http_error(429), http_error(429), http_error(429))
        delays: list[float] = []
        with self.assertRaisesRegex(OpenAlexError, "rate limit.*429"):
            search_papers("rate limited", max_retries=2, opener=opener, sleep=delays.append)
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(len(delays), 2)

    def test_http_500_uses_bounded_retries(self) -> None:
        opener = FakeOpener(http_error(500), http_error(500), http_error(500), self.fixture)
        delays: list[float] = []
        with self.assertRaises(OpenAlexError):
            search_papers("server error", max_retries=2, opener=opener, sleep=delays.append)
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(len(delays), 2)

    def test_timeout_retries_only_up_to_configured_bound(self) -> None:
        opener = FakeOpener(TimeoutError("timed out"), TimeoutError("timed out"), self.fixture)
        with self.assertRaises(OpenAlexError):
            search_papers("network issue", max_retries=1, opener=opener, sleep=lambda _: None)
        self.assertEqual(len(opener.calls), 2)

    def test_malformed_json_raises_provider_error_without_retry(self) -> None:
        opener = FakeOpener(b"{not valid json")
        with self.assertRaises(OpenAlexError):
            search_papers("bad response", opener=opener)
        self.assertEqual(len(opener.calls), 1)


if __name__ == "__main__":
    unittest.main()
