"""Conservative, provider-neutral research resource identity matching."""

from dataclasses import replace
import json
import re
import unicodedata
from urllib.parse import unquote, urlsplit, urlunsplit

from app.models import LinkType, ResearchResource, ResourceProvenance

_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)


def normalize_doi(value: object) -> str | None:
    """Normalize a bare DOI, doi: value, or DOI resolver URL."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text[:4].casefold() == "doi:":
        text = text[4:].strip()
    try:
        parsed = urlsplit(text)
        if parsed.scheme:
            if parsed.scheme.casefold() not in ("http", "https") or (parsed.hostname or "").casefold() not in {
                "doi.org", "dx.doi.org"
            }:
                return None
            text = unquote(parsed.path.lstrip("/"))
    except ValueError:
        return None
    text = unquote(text).strip().casefold()
    return text if _DOI_RE.fullmatch(text) else None


def canonical_url(value: object) -> str | None:
    """Canonicalize only safe, evident URL equivalents; preserve query semantics."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
        scheme = parsed.scheme.casefold()
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if scheme not in ("http", "https") or not host or host.casefold() in {
        "doi.org", "dx.doi.org"
    }:
        return None
    host = host.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, host, path, parsed.query, ""))


def _provider_id(resource: ResearchResource) -> tuple[str, str] | None:
    value = resource.external_id
    if not value:
        return None
    normalized = canonical_url(value) or value.strip()
    return (resource.source_provider.strip().casefold(), normalized)


def _urls(resource: ResearchResource) -> set[str]:
    urls = {
        canonical
        for link in resource.links
        if link.kind in (LinkType.LANDING_PAGE, LinkType.PUBLISHER, LinkType.REPOSITORY,
                         LinkType.OPEN_ACCESS)
        if (canonical := canonical_url(link.url)) is not None
    }
    external_url = canonical_url(resource.external_id)
    if external_url:
        urls.add(external_url)
    return urls


def _title_author_key(resource: ResearchResource) -> tuple[str, int, tuple[str, ...]] | None:
    if not resource.title or resource.publication_year is None or not resource.authors:
        return None
    title = " ".join(unicodedata.normalize("NFKC", resource.title).casefold().split())
    authors = tuple(sorted(" ".join(unicodedata.normalize("NFKC", name).casefold().split())
                           for name in resource.authors if name.strip()))
    return (title, resource.publication_year, authors) if title and authors else None


def find_duplicate(candidate: ResearchResource, resources: list[ResearchResource] | tuple[ResearchResource, ...]) -> int | None:
    """Return first matching index, following DOI, provider ID, URL, then strict title priority."""
    candidate_doi = normalize_doi(candidate.doi)
    candidate_id = _provider_id(candidate)
    candidate_urls = _urls(candidate)
    candidate_title = _title_author_key(candidate)
    for index, existing in enumerate(resources):
        if existing.resource_type != candidate.resource_type:
            continue
        existing_doi = normalize_doi(existing.doi)
        provenance_dois = {normalized for item in existing.provenance
                           if (normalized := normalize_doi(item.doi)) is not None}
        known_dois = provenance_dois | ({existing_doi} if existing_doi else set())
        # Contradictory DOI evidence is decisive; weaker keys cannot override it.
        if candidate_doi and known_dois:
            if known_dois == {candidate_doi}:
                return index
            continue
        existing_id = _provider_id(existing)
        if candidate_id and candidate_id == existing_id:
            return index
        existing_urls = _urls(existing)
        if candidate_urls and candidate_urls.intersection(existing_urls):
            return index
        # Title matching is allowed only when neither observation has any stronger identity.
        if (candidate_doi is None and existing_doi is None and candidate_id is None
                and existing_id is None and not candidate_urls and not existing_urls
                and candidate_title is not None and candidate_title == _title_author_key(existing)):
            return index
    return None


def _observation_provenance(resource: ResearchResource) -> tuple[ResourceProvenance, ...]:
    if resource.provenance:
        return resource.provenance
    return (ResourceProvenance(resource.source_provider, resource.external_id, resource.doi,
                               resource.retrieved_at, resource.original_metadata),)


def _merge_provenance(*groups: tuple[ResourceProvenance, ...]) -> tuple[ResourceProvenance, ...]:
    result = []
    seen = set()
    for group in groups:
        for item in group:
            key = (item.source_provider, item.external_id, item.doi, item.retrieved_at.isoformat(),
                   json.dumps(item.original_metadata, ensure_ascii=False, sort_keys=True, default=str))
            if key not in seen:
                seen.add(key)
                result.append(item)
    return tuple(result)


def merge_resources(existing: ResearchResource, incoming: ResearchResource) -> ResearchResource:
    """Merge complementary values without overwriting known conflicting values."""
    def known(first, second):
        return first if first is not None else second

    authors = existing.authors
    if authors is None:
        authors = incoming.authors
    elif incoming.authors:
        authors = tuple(dict.fromkeys((*authors, *incoming.authors)))
    keywords = existing.keywords
    if keywords is None:
        keywords = incoming.keywords
    elif incoming.keywords:
        keywords = tuple(dict.fromkeys((*keywords, *incoming.keywords)))
    provenance = _merge_provenance(_observation_provenance(existing),
                                   _observation_provenance(incoming))
    return replace(
        existing,
        title=known(existing.title, incoming.title),
        description=known(existing.description, incoming.description),
        authors=authors,
        publication_year=known(existing.publication_year, incoming.publication_year),
        doi=known(normalize_doi(existing.doi), normalize_doi(incoming.doi)),
        keywords=keywords,
        license=known(existing.license, incoming.license),
        is_open_access=known(existing.is_open_access, incoming.is_open_access),
        links=tuple(dict.fromkeys((*existing.links, *incoming.links))),
        downloadable_files=tuple(dict.fromkeys((*existing.downloadable_files,
                                                  *incoming.downloadable_files))),
        provenance=provenance,
    )


def deduplicate_resources(resources) -> list[ResearchResource]:
    """Stable within-batch deduplication, preserving first result position."""
    unique: list[ResearchResource] = []
    for resource in resources:
        match = find_duplicate(resource, unique)
        if match is None:
            unique.append(resource)
        else:
            unique[match] = merge_resources(unique[match], resource)
    return unique
