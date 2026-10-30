"""Crossref REST API works search and provider-specific normalization."""

from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import math
import socket
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from app.core.deduplication import normalize_doi
from app.core.topic import validate_topic
from app.models import (DownloadableFile, LinkType, ResearchResource, ResourceLink,
                        ResourceProvenance, ResourceType)

WORKS_URL = "https://api.crossref.org/works"
MAX_RESULTS = 20
PROVIDER = "Crossref"


class CrossrefError(Exception):
    """A failed Crossref request or unusable response."""


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _url(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = urlsplit(text)
        parsed.port
    except ValueError:
        return None
    return text if parsed.scheme in ("http", "https") and parsed.hostname else None


class _TextExtractor(HTMLParser):
    """Extract text only; never interpret markup from remote abstracts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in ("script", "style"):
            self._suppressed += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in ("script", "style") and self._suppressed:
            self._suppressed -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppressed and data.strip():
            self.parts.append(data.strip())


def _abstract(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    parser = _TextExtractor()
    try:
        parser.feed(text)
        parser.close()
    except (ValueError, AssertionError):
        return " ".join(text.split())
    return " ".join(" ".join(parser.parts).split()) or None


def _authors(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    names = []
    for author in value:
        if not isinstance(author, dict):
            continue
        name = _text(author.get("name"))
        if name is None:
            given, family = _text(author.get("given")), _text(author.get("family"))
            name = " ".join(part for part in (given, family) if part) or None
        if name:
            names.append(name)
    return tuple(names) if names else (() if not value else None)


def _year(item: dict) -> int | None:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        date_obj = item.get(key)
        parts = date_obj.get("date-parts") if isinstance(date_obj, dict) else None
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            year = parts[0][0]
            if isinstance(year, int) and not isinstance(year, bool) and 1 <= year <= 9999:
                return year
    return None


def normalize_work(item: dict, *, retrieved_at: datetime | None = None) -> ResearchResource:
    """Map one Crossref item, keeping absent or unsupported fields unknown."""
    if not isinstance(item, dict):
        raise ValueError("Crossref work must be an object")
    retrieved_at = retrieved_at or datetime.now(timezone.utc)
    titles = item.get("title")
    title = next((_text(value) for value in titles if _text(value)), None) if isinstance(titles, list) else None
    doi = normalize_doi(item.get("DOI"))
    doi_value = _url(item.get("URL"))
    links: list[ResourceLink] = []
    files: list[DownloadableFile] = []
    if doi:
        links.append(ResourceLink(LinkType.DOI, f"https://doi.org/{doi}", PROVIDER))
    if doi_value:
        links.append(ResourceLink(LinkType.LANDING_PAGE, doi_value, PROVIDER))
    license_name = None
    license_items = item.get("license")
    if isinstance(license_items, list):
        for license_item in license_items:
            if not isinstance(license_item, dict):
                continue
            license_url = _url(license_item.get("URL"))
            if license_url:
                license_name = license_url
                links.append(ResourceLink(LinkType.OPEN_ACCESS, license_url, PROVIDER, license_url))
                break
    link_items = item.get("link")
    if isinstance(link_items, list):
        for record in link_items:
            if not isinstance(record, dict):
                continue
            link_url = _url(record.get("URL"))
            if not link_url:
                continue
            content_type = _text(record.get("content-type"))
            if content_type and content_type.casefold() == "application/pdf":
                files.append(DownloadableFile(link_url, "pdf", content_type, None, PROVIDER))
                links.append(ResourceLink(LinkType.DIRECT_FILE, link_url, PROVIDER))
            else:
                links.append(ResourceLink(LinkType.PUBLISHER, link_url, PROVIDER))
    external_id = _text(item.get("DOI"))
    metadata = item.copy()
    return ResearchResource(
        resource_type=ResourceType.PAPER, title=title,
        description=_abstract(item.get("abstract")), authors=_authors(item.get("author")),
        publication_year=_year(item), source_provider=PROVIDER, external_id=external_id,
        doi=doi, keywords=None, retrieved_at=retrieved_at, license=license_name,
        links=tuple(dict.fromkeys(links)), downloadable_files=tuple(dict.fromkeys(files)),
        original_metadata=metadata,
        provenance=(ResourceProvenance(PROVIDER, external_id, doi, retrieved_at, metadata),),
    )


def search_papers(
    topic: str, *, limit: int = MAX_RESULTS, timeout: float = 10.0, max_retries: int = 2,
    contact_email: str | None = None, opener: Callable | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[ResearchResource]:
    """Search Crossref /works with a bounded result count and polite identification."""
    topic = validate_topic(topic)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f"limit must be between 1 and {MAX_RESULTS}")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("max_retries must be a nonnegative integer")
    email = _text(contact_email)
    params = {"query": topic, "rows": limit}
    if email:
        params["mailto"] = email
    headers = {"User-Agent": "Crossref-compatible ResearchDiscoveryCLI/0.1" +
               (f" (mailto:{email})" if email else "")}
    request = Request(f"{WORKS_URL}?{urlencode(params)}", headers=headers)
    open_request, pause = opener or urlopen, sleep or time.sleep
    for attempt in range(max_retries + 1):
        try:
            with open_request(request, timeout=timeout) as response:
                payload = json.load(response)
            message = payload.get("message") if isinstance(payload, dict) else None
            items = message.get("items") if isinstance(message, dict) else None
            if not isinstance(items, list):
                raise CrossrefError("Crossref returned an invalid response")
            timestamp = datetime.now(timezone.utc)
            return [normalize_work(item, retrieved_at=timestamp) for item in items[:limit]
                    if isinstance(item, dict)]
        except HTTPError as exc:
            retry = exc.code == 429 or 500 <= exc.code <= 599
            exc.close()
            if retry and attempt < max_retries:
                pause(min(0.5 * 2 ** attempt, 4.0))
                continue
            if exc.code == 429:
                raise CrossrefError("Crossref rate limit reached (HTTP 429)") from exc
            raise CrossrefError(f"Crossref request failed (HTTP {exc.code})") from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            if attempt < max_retries:
                pause(min(0.5 * 2 ** attempt, 4.0))
                continue
            raise CrossrefError(f"Could not reach Crossref: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CrossrefError("Crossref returned malformed JSON") from exc
    raise AssertionError("retry loop exited unexpectedly")
