"""Harvard Dataverse dataset search using its documented Search and Native APIs."""

from datetime import datetime, timezone
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

BASE_URL = "https://dataverse.harvard.edu"
SEARCH_URL = f"{BASE_URL}/api/search"
MAX_RESULTS = 20
PROVIDER = "Harvard Dataverse"


class DataverseError(Exception):
    """A failed Harvard Dataverse request or unusable response."""


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


def _field(fields: object, name: str) -> object:
    if not isinstance(fields, list):
        return None
    for field in fields:
        if isinstance(field, dict) and field.get("typeName") == name:
            return field.get("value")
    return None


def _citation(version: object) -> list:
    if not isinstance(version, dict):
        return []
    blocks = version.get("metadataBlocks")
    citation = blocks.get("citation") if isinstance(blocks, dict) else None
    fields = citation.get("fields") if isinstance(citation, dict) else None
    return fields if isinstance(fields, list) else []


def _compound_text(value: object, key: str) -> str | None:
    if not isinstance(value, list):
        return None
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        nested = item.get(key)
        if isinstance(nested, list):
            for part in nested:
                if isinstance(part, dict):
                    found = _text(part.get("value"))
                    if found:
                        result.append(found)
        elif isinstance(nested, dict):
            found = _text(nested.get("value"))
            if found:
                result.append(found)
    return "\n\n".join(result) or None


def _creators(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    names = []
    for item in value:
        if isinstance(item, dict):
            name = item.get("authorName", item.get("creatorName"))
            if isinstance(name, dict):
                name = name.get("value")
            text = _text(name)
            if text:
                names.append(text)
    return tuple(names) if names else (() if value == [] else None)


def _keywords(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    result = []
    for item in value:
        text = _text(item.get("keywordValue")) if isinstance(item, dict) else None
        if text:
            result.append(text)
    return tuple(result) if result else (() if value == [] else None)


def _version(payload: dict) -> dict:
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    latest = data.get("latestVersion")
    return latest if isinstance(latest, dict) else data


def normalize_dataset(search_item: dict, detail_payload: dict | None = None,
                      *, retrieved_at: datetime | None = None,
                      base_url: str = BASE_URL) -> ResearchResource:
    """Normalize one search hit plus optional full dataset metadata."""
    if not isinstance(search_item, dict):
        raise ValueError("Dataverse search result must be an object")
    retrieved_at = retrieved_at or datetime.now(timezone.utc)
    detail = detail_payload if isinstance(detail_payload, dict) else {}
    data = detail.get("data") if isinstance(detail.get("data"), dict) else {}
    version = _version(detail)
    fields = _citation(version)
    search_fields = search_item.get("metadataFields")
    if not fields and isinstance(search_fields, list):
        fields = search_fields

    title = _text(_field(fields, "title")) or _text(search_item.get("name"))
    description = _compound_text(_field(fields, "dsDescription"), "dsDescriptionValue")
    description = description or _text(search_item.get("description"))
    authors = _creators(_field(fields, "author"))
    if authors is None and isinstance(search_item.get("authors"), list):
        names = tuple(text for author in search_item["authors"]
                      if isinstance(author, dict) and (text := _text(author.get("name"))))
        authors = names if names else (() if not search_item["authors"] else None)
    keywords = _keywords(_field(fields, "keyword"))

    pid = _text(data.get("persistentId")) or _text(search_item.get("global_id"))
    doi = normalize_doi(pid)
    external_id = _text(data.get("id"))
    if external_id is not None:
        external_id = f"dataset:{external_id}"
    else:
        external_id = pid or _text(search_item.get("entity_id"))

    landing = _url(search_item.get("url"))
    links: list[ResourceLink] = []
    if landing:
        links.append(ResourceLink(LinkType.LANDING_PAGE, landing, PROVIDER))
    if doi:
        links.append(ResourceLink(LinkType.DOI, f"https://doi.org/{doi}", PROVIDER))

    license_obj = version.get("license")
    license_name = _text(license_obj.get("name")) if isinstance(license_obj, dict) else None
    is_open_access = None
    files = []
    file_access: list[bool] = []
    file_records = version.get("files")
    if isinstance(file_records, list):
        for record in file_records:
            if not isinstance(record, dict):
                continue
            file_data = record.get("dataFile")
            if not isinstance(file_data, dict):
                continue
            file_id = file_data.get("id")
            if isinstance(file_id, bool) or not isinstance(file_id, int):
                continue
            filename = _text(file_data.get("filename"))
            mime = _text(file_data.get("contentType"))
            restricted = file_data.get("restricted")
            if not isinstance(restricted, bool):
                nested_restrict = file_data.get("restrict")
                record_restricted = record.get("restricted")
                if isinstance(nested_restrict, bool):
                    restricted = nested_restrict
                elif isinstance(record_restricted, bool):
                    restricted = record_restricted
                else:
                    restricted = None
            if restricted is not None:
                file_access.append(not restricted)
            download_url = f"{base_url}/api/access/datafile/{file_id}"
            files.append(DownloadableFile(
                download_url,
                filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else None,
                mime, filename, PROVIDER,
                None if restricted is None else not restricted,
            ))
    if file_access and all(file_access):
        is_open_access = True
    elif file_access and not any(file_access):
        is_open_access = False

    published = _text(version.get("releaseTime")) or _text(search_item.get("published_at"))
    if published is None:
        pub_date = _field(fields, "publicationDate")
        published = _text(pub_date)
    year = None
    if published and len(published) >= 4 and published[:4].isdigit():
        year = int(published[:4])

    metadata = {"search": search_item, "detail": detail, "provider": PROVIDER,
                "dataset_persistent_id": pid,
                "files": [{"id": item.get("dataFile", {}).get("id"),
                           "filename": item.get("dataFile", {}).get("filename"),
                           "restricted": item.get("restricted",
                               item.get("dataFile", {}).get("restricted",
                                   item.get("dataFile", {}).get("restrict")))}
                          for item in file_records if isinstance(item, dict)
                          and isinstance(item.get("dataFile"), dict)]
                if isinstance(file_records, list) else []}
    return ResearchResource(
        resource_type=ResourceType.DATASET, title=title, description=description,
        authors=authors, publication_year=year, source_provider=PROVIDER,
        external_id=external_id, doi=doi, keywords=keywords, retrieved_at=retrieved_at,
        license=license_name, is_open_access=is_open_access, links=tuple(links),
        downloadable_files=tuple(files), original_metadata=metadata,
        provenance=(ResourceProvenance(PROVIDER, external_id, doi, retrieved_at, metadata),),
    )


def _request_json(url: str, *, timeout: float, max_retries: int,
                  opener: Callable, pause: Callable[[float], None]) -> dict:
    request = Request(url, headers={"User-Agent": "research-discovery-cli/0.1"})
    for attempt in range(max_retries + 1):
        try:
            with opener(request, timeout=timeout) as response:
                payload = json.load(response)
            if not isinstance(payload, dict) or payload.get("status") not in ("OK", "SUCCESS"):
                raise DataverseError("Harvard Dataverse returned an invalid response")
            return payload
        except HTTPError as exc:
            retry = exc.code in (429, 500, 502, 503, 504)
            exc.close()
            if retry and attempt < max_retries:
                pause(min(0.5 * (2 ** attempt), 4.0))
                continue
            if exc.code == 429:
                raise DataverseError("Harvard Dataverse rate limit reached (HTTP 429)") from exc
            raise DataverseError(f"Harvard Dataverse request failed (HTTP {exc.code})") from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            if attempt < max_retries:
                pause(min(0.5 * (2 ** attempt), 4.0))
                continue
            raise DataverseError(f"Could not reach Harvard Dataverse: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DataverseError("Harvard Dataverse returned malformed JSON") from exc
    raise AssertionError("retry loop exited unexpectedly")


def search_datasets(topic: str, *, limit: int = 10, timeout: float = 10.0,
                    max_retries: int = 2, opener: Callable | None = None,
                    sleep: Callable[[float], None] | None = None) -> list[ResearchResource]:
    """Search Harvard Dataverse and retrieve each result's dataset metadata."""
    topic = validate_topic(topic)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f"limit must be between 1 and {MAX_RESULTS}")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("max_retries must be a nonnegative integer")
    open_request = opener or urlopen
    pause = sleep or time.sleep
    params = urlencode({"q": topic, "type": "dataset", "per_page": limit,
                       "metadata_fields": "citation:*", "show_entity_ids": "true"})
    search_payload = _request_json(f"{SEARCH_URL}?{params}", timeout=timeout,
                                   max_retries=max_retries, opener=open_request, pause=pause)
    data = search_payload.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise DataverseError("Harvard Dataverse returned an invalid search response")
    resources = []
    retrieved_at = datetime.now(timezone.utc)
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        pid = _text(item.get("global_id"))
        detail = None
        if pid:
            query = urlencode({"persistentId": pid})
            detail = _request_json(f"{BASE_URL}/api/datasets/:persistentId/?{query}",
                                   timeout=timeout, max_retries=max_retries,
                                   opener=open_request, pause=pause)
        resources.append(normalize_dataset(item, detail, retrieved_at=retrieved_at))
    return resources
