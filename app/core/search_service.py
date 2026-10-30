"""Application operations for searching and reviewing local history."""

from pathlib import Path
from dataclasses import dataclass
from typing import Callable

from app.core.deduplication import deduplicate_resources
from app.core.query_expansion import (EXPANSION_VERSION, MAX_EXPANSIONS,
                                     expand_query_details)
from app.core.topic import validate_topic
from app.models import ResearchResource
from app.providers.openalex import MAX_RESULTS as OPENALEX_MAX_RESULTS
from app.providers.openalex import search_papers as search_openalex
from app.providers.crossref import MAX_RESULTS as CROSSREF_MAX_RESULTS
from app.providers.crossref import search_papers as search_crossref
from app.storage import DEFAULT_DB_PATH, ResearchStore, SearchSummary


def search_and_save(
    topic: str,
    *,
    limit: int = 20,
    timeout: float = 10.0,
    db_path: str | Path = DEFAULT_DB_PATH,
    provider: Callable[..., list[ResearchResource]] | None = None,
) -> list[ResearchResource]:
    """Search once, then atomically save the returned resources and topic."""
    resources, _search_id = search_and_save_with_id(
        topic, limit=limit, timeout=timeout, db_path=db_path, provider=provider
    )
    return resources


def search_and_save_with_id(
    topic: str,
    *,
    limit: int = 20,
    timeout: float = 10.0,
    db_path: str | Path = DEFAULT_DB_PATH,
    provider: Callable[..., list[ResearchResource]] | None = None,
) -> tuple[list[ResearchResource], int]:
    """Search and save, returning the saved search ID for derived metadata."""
    topic = validate_topic(topic)
    search = provider or search_openalex
    resources = deduplicate_resources(search(topic, limit=limit, timeout=timeout))
    with ResearchStore(db_path) as store:
        search_id = store.save_search(topic, resources)
        return store.get_search_resources(search_id), search_id


@dataclass(frozen=True)
class CombinedSearchOutcome:
    """Combined normalized results and provider-level execution evidence."""

    resources: list[ResearchResource]
    provider_counts: dict[str, int]
    failed_providers: dict[str, str]
    total_unique_count: int
    duplicates_merged: int
    search_id: int


class CombinedSearchError(Exception):
    """Both academic providers failed; no search history entry was saved."""

    def __init__(self, failures: dict[str, str]) -> None:
        self.failures = failures
        detail = "; ".join(f"{name}: {message}" for name, message in failures.items())
        super().__init__(f"both academic providers failed ({detail})")


class ExpandedSearchError(Exception):
    """Every expanded provider query failed; no history was saved."""


@dataclass(frozen=True)
class ExpandedSearchOutcome:
    resources: list[ResearchResource]
    queries: list[str]
    providers: list[str]
    provider_counts: dict[str, int]
    failed_queries: dict[str, str]
    records_retrieved: int
    unique_resources: int
    duplicates_removed: int
    provider_requests: int
    search_id: int


MAX_EXPANDED_PROVIDER_REQUESTS = (MAX_EXPANSIONS + 1) * 2
# urllib's standard redirect handler permits at most ten redirects per attempt.
MAX_EXPANDED_HTTP_ATTEMPTS = (MAX_EXPANSIONS + 1) * (1 + OPENALEX_MAX_RESULTS) * 3 * 11


def expanded_search_and_save(
    topic: str,
    *,
    limit: int = 20,
    timeout: float = 10.0,
    db_path: str | Path = DEFAULT_DB_PATH,
    expansion_limit: int = MAX_EXPANSIONS,
    providers: tuple[tuple[str, Callable[..., list[ResearchResource]]], ...],
) -> ExpandedSearchOutcome:
    """Search the original and deterministic variations, then save one snapshot.

    At most five query strings are generated. Existing provider adapters retain
    their own bounded retry policies; at most two providers are allowed, so this
    orchestrator makes no more than ten adapter search calls.
    """
    topic = validate_topic(topic)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= OPENALEX_MAX_RESULTS:
        raise ValueError(f"limit must be between 1 and {OPENALEX_MAX_RESULTS}")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
        raise ValueError("timeout must be a positive finite number")
    if not providers or len(providers) > 2:
        raise ValueError("expanded search requires one or two configured providers")
    if len({name for name, _ in providers}) != len(providers):
        raise ValueError("expanded search provider names must be unique")
    provider_set = {name for name, _ in providers}
    supported_sets = ({"OpenAlex"}, {"Crossref"}, {"Harvard Dataverse"},
                      {"OpenAlex", "Crossref"})
    if provider_set not in supported_sets:
        raise ValueError("expanded search supports OpenAlex, Crossref, their combination, or Harvard Dataverse")
    query_details = expand_query_details(topic, limit=expansion_limit)
    queries = [item["query"] for item in query_details]
    provider_names = [name for name, _ in providers]
    counts = dict.fromkeys(provider_names, 0)
    failures: dict[str, str] = {}
    gathered: list[ResearchResource] = []
    provider_requests = 0
    completed_requests = 0
    for query_index, query in enumerate(queries):
        for provider_name, provider_search in providers:
            provider_requests += 1
            try:
                results = provider_search(query, limit=limit, timeout=timeout)
                completed_requests += 1
                counts[provider_name] += len(results)
                gathered.extend(results)
            except Exception as exc:
                failures[f"{query_index + 1}:{provider_name}"] = str(exc) or exc.__class__.__name__
    if completed_requests == 0 and failures:
        details = "; ".join(f"{key} {message}" for key, message in failures.items())
        raise ExpandedSearchError(f"all expanded provider queries failed ({details})")

    resources = deduplicate_resources(gathered)
    records_retrieved = len(gathered)
    unique_count = len(resources)
    duplicates_removed = records_retrieved - unique_count
    resources = resources[:limit]
    metadata = {
        "status": "partial" if failures else "complete",
        "providers": provider_names,
        "provider_counts": counts,
        "failed_providers": failures,
        "result_limit": limit,
        "total_unique_count": unique_count,
        "duplicates_merged": duplicates_removed,
        "query_expansion": {
            "method": "deterministic_keyword_rules",
            "version": EXPANSION_VERSION,
            "original_topic": topic,
            "queries": queries,
            "generated_queries": queries[1:],
            "query_rules": query_details,
            "expansion_limit": expansion_limit,
            "provider_search_requests": provider_requests,
            "provider_search_request_limit": MAX_EXPANDED_PROVIDER_REQUESTS,
            "worst_case_http_attempt_limit": MAX_EXPANDED_HTTP_ATTEMPTS,
            "records_retrieved": records_retrieved,
            "unique_resources": unique_count,
            "duplicates_removed": duplicates_removed,
        },
    }
    with ResearchStore(db_path) as store:
        search_id = store.save_search(topic, resources, metadata=metadata)
        saved = store.get_search_resources(search_id)
    return ExpandedSearchOutcome(saved, queries, provider_names, counts, failures,
                                records_retrieved, unique_count, duplicates_removed, provider_requests,
                                search_id)


def search_all_and_save(
    topic: str,
    *,
    limit: int = 20,
    timeout: float = 10.0,
    db_path: str | Path = DEFAULT_DB_PATH,
    openalex_provider: Callable[..., list[ResearchResource]] | None = None,
    crossref_provider: Callable[..., list[ResearchResource]] | None = None,
) -> CombinedSearchOutcome:
    """Search OpenAlex then Crossref, merge deterministically, and save one search.

    Each provider is bounded by ``limit`` (and its own API maximum); the final
    deduplicated result list is then capped at ``limit``.
    """
    topic = validate_topic(topic)
    maximum = min(OPENALEX_MAX_RESULTS, CROSSREF_MAX_RESULTS)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    if not 0 < timeout < float("inf"):
        raise ValueError("timeout must be a positive finite number")
    providers = (("OpenAlex", openalex_provider or search_openalex),
                 ("Crossref", crossref_provider or search_crossref))
    gathered: list[ResearchResource] = []
    counts: dict[str, int] = {}
    failures: dict[str, str] = {}
    for name, provider_search in providers:
        try:
            records = provider_search(topic, limit=limit, timeout=timeout)
            counts[name] = len(records)
            gathered.extend(records)
        except Exception as exc:
            # Adapter-specific exceptions are expected; retain unexpected provider
            # failures too so a bad response cannot mask the other provider's results.
            counts[name] = 0
            failures[name] = str(exc) or exc.__class__.__name__
    if len(failures) == len(providers):
        raise CombinedSearchError(failures)

    merged = deduplicate_resources(gathered)
    unique_count = len(merged)
    duplicates_merged = sum(counts.values()) - unique_count
    limited = merged[:limit]
    metadata = {
        "status": "partial" if failures else "complete",
        "providers": [name for name, _ in providers],
        "provider_counts": counts,
        "failed_providers": failures,
        "result_limit": limit,
        "total_unique_count": unique_count,
        "duplicates_merged": duplicates_merged,
    }
    with ResearchStore(db_path) as store:
        search_id = store.save_search(topic, limited, metadata=metadata)
        saved = store.get_search_resources(search_id)
    return CombinedSearchOutcome(saved, counts, failures, unique_count, duplicates_merged, search_id)


def list_history(db_path: str | Path = DEFAULT_DB_PATH) -> list[SearchSummary]:
    with ResearchStore(db_path) as store:
        return store.list_searches()


def get_history_resources(
    search_id: int, db_path: str | Path = DEFAULT_DB_PATH
) -> list[ResearchResource]:
    with ResearchStore(db_path) as store:
        return store.get_search_resources(search_id)
