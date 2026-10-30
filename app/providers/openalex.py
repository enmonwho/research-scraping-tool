"""OpenAlex Works search and provider-specific resource normalization."""

from datetime import datetime, timezone
import json
import re
import socket
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

from app.core.topic import validate_topic
from app.models import DownloadableFile, LinkType, ResearchResource, ResourceLink, ResourceType

WORKS_URL = "https://api.openalex.org/works"
MAX_RESULTS = 20
PROVIDER = "OpenAlex"
FIELDS = (
    "id,type,display_name,publication_year,doi,abstract_inverted_index,"
    "authorships,keywords,primary_location,best_oa_location,locations,open_access"
)
PAPER_TYPES = "article|preprint|conference-paper|review|data-paper|software-paper|report"
_DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)


class OpenAlexError(Exception):
    """A failed OpenAlex request or unusable response."""


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _url(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = urlsplit(text)
        host = parsed.hostname
        parsed.port
    except ValueError:
        return None
    return text if parsed.scheme in ("http", "https") and host else None


def _doi(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    if text.lower().startswith("doi:"):
        text = text[4:]
    try:
        parsed = urlsplit(text)
        host = parsed.hostname
        parsed.port
    except ValueError:
        return None
    if parsed.scheme:
        if parsed.scheme not in ("http", "https") or host not in ("doi.org", "dx.doi.org"):
            return None
        text = unquote(parsed.path.lstrip("/"))
    text = text.strip().lower()
    return text if _DOI_PATTERN.fullmatch(text) else None


def _abstract(value: object) -> str | None:
    if not isinstance(value, dict) or not value:
        return None
    positions: dict[int, str] = {}
    for word, indices in value.items():
        if not isinstance(word, str) or not isinstance(indices, list):
            return None
        for index in indices:
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 10000:
                return None
            positions[index] = word
    if not positions or len(positions) != max(positions) + 1:
        return None
    return " ".join(positions[index] for index in range(len(positions)))


def _names(value: object, *, nested: bool) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    names = []
    for item in value:
        if not isinstance(item, dict):
            continue
        source = item.get("author") if nested else item
        if isinstance(source, dict):
            name = _text(source.get("display_name"))
            if name:
                names.append(name)
    return tuple(names)


def normalize_work(work: dict, *, retrieved_at: datetime | None = None) -> ResearchResource:
    """Map one OpenAlex work without inventing unavailable metadata or URLs."""
    if not isinstance(work, dict):
        raise ValueError("OpenAlex work must be an object")
    retrieved_at = retrieved_at or datetime.now(timezone.utc)
    links: list[ResourceLink] = []
    files: list[DownloadableFile] = []

    def add_link(kind: LinkType, url: str | None, *, license: str | None = None,
                 is_open_access: bool | None = None) -> None:
        if url is None:
            return
        link = ResourceLink(kind, url, PROVIDER, license, is_open_access)
        if link not in links:
            links.append(link)

    doi_value = work.get("doi")
    if doi_value is None and isinstance(work.get("ids"), dict):
        doi_value = work["ids"].get("doi")
    doi = _doi(doi_value)
    if doi:
        add_link(LinkType.DOI, _url(doi_value))

    locations: list[dict] = []
    primary = work.get("primary_location")
    if isinstance(primary, dict):
        locations.append(primary)
    best_oa = work.get("best_oa_location")
    if isinstance(best_oa, dict):
        locations.append(best_oa)
    if isinstance(work.get("locations"), list):
        locations.extend(item for item in work["locations"] if isinstance(item, dict))

    for index, location in enumerate(locations):
        landing = _url(location.get("landing_page_url"))
        pdf = _url(location.get("pdf_url"))
        license = _text(location.get("license"))
        is_oa = location.get("is_oa") if isinstance(location.get("is_oa"), bool) else None
        source = location.get("source")
        source_type = source.get("type") if isinstance(source, dict) else None
        if index == 0 and location is primary:
            add_link(LinkType.LANDING_PAGE, landing, license=license, is_open_access=is_oa)
        if source_type == "repository":
            add_link(LinkType.REPOSITORY, landing, license=license, is_open_access=is_oa)
        elif source_type in ("journal", "conference"):
            add_link(LinkType.PUBLISHER, landing, license=license, is_open_access=is_oa)
        add_link(LinkType.DIRECT_FILE, pdf, license=license, is_open_access=is_oa)
        if pdf:
            file = DownloadableFile(pdf, "pdf", "application/pdf", None, PROVIDER, is_oa)
            if file not in files:
                files.append(file)

    access = work.get("open_access")
    if isinstance(access, dict):
        is_oa = access.get("is_oa") if isinstance(access.get("is_oa"), bool) else None
        add_link(LinkType.OPEN_ACCESS, _url(access.get("oa_url")), is_open_access=is_oa)
    else:
        is_oa = None

    year = work.get("publication_year")
    return ResearchResource(
        resource_type=ResourceType.DATASET if work.get("type") == "dataset" else ResourceType.PAPER,
        title=_text(work.get("display_name")) or _text(work.get("title")),
        description=_abstract(work.get("abstract_inverted_index")),
        authors=_names(work.get("authorships"), nested=True),
        publication_year=year if isinstance(year, int) and not isinstance(year, bool) else None,
        source_provider=PROVIDER,
        external_id=_text(work.get("id")),
        doi=doi,
        keywords=_names(work.get("keywords"), nested=False),
        retrieved_at=retrieved_at,
        license=_text(primary.get("license")) if isinstance(primary, dict) else None,
        is_open_access=is_oa,
        links=tuple(links),
        downloadable_files=tuple(files),
        original_metadata=work.copy(),
    )


def _parse_resources(payload: object, limit: int, retrieved_at: datetime) -> list[ResearchResource]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise OpenAlexError("OpenAlex returned an invalid response")
    return [normalize_work(item, retrieved_at=retrieved_at)
            for item in payload["results"][:limit] if isinstance(item, dict)]


def search_papers(
    topic: str,
    *,
    limit: int = MAX_RESULTS,
    timeout: float = 10.0,
    max_retries: int = 2,
    opener: Callable | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[ResearchResource]:
    """Search OpenAlex and return normalized resources with bounded retries."""
    topic = validate_topic(topic)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f"limit must be between 1 and {MAX_RESULTS}")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
        raise ValueError("timeout must be a positive finite number")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("max_retries must be a nonnegative integer")

    query = urlencode({"search": topic, "filter": f"type:{PAPER_TYPES}",
                       "per_page": limit, "select": FIELDS})
    request = Request(f"{WORKS_URL}?{query}", headers={"User-Agent": "research-discovery-cli/0.1"})
    open_request = opener or urlopen
    pause = sleep or time.sleep

    for attempt in range(max_retries + 1):
        try:
            with open_request(request, timeout=timeout) as response:
                payload = json.load(response)
            return _parse_resources(payload, limit, datetime.now(timezone.utc))
        except HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                exc.close()
                pause(min(0.5 * (2 ** attempt), 4.0))
                continue
            if exc.code == 429:
                exc.close()
                raise OpenAlexError("OpenAlex rate limit reached (HTTP 429)") from exc
            exc.close()
            raise OpenAlexError(f"OpenAlex request failed (HTTP {exc.code})") from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            if attempt < max_retries:
                pause(min(0.5 * (2 ** attempt), 4.0))
                continue
            raise OpenAlexError(f"Could not reach OpenAlex: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OpenAlexError("OpenAlex returned malformed JSON") from exc

    raise AssertionError("retry loop exited unexpectedly")
