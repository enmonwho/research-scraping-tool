"""Command-line entry point for search, history, and exports."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
from collections.abc import Sequence
import sys
from urllib.parse import urlsplit

from app.core.search_service import (CombinedSearchError, get_history_resources,
                                    ExpandedSearchError, expanded_search_and_save,
                                    MAX_EXPANDED_HTTP_ATTEMPTS,
                                    MAX_EXPANDED_PROVIDER_REQUESTS, list_history,
                                    search_all_and_save, search_and_save, search_and_save_with_id)
from app.core.export import CSV_FIELDS, csv_row as _csv_row, resource_json as _resource_json, write_export
from app.core.query_expansion import MAX_EXPANSIONS
from app.core.evaluation import (DATASET_VERSION, EVALUATION_CODE_VERSION,
                                 EvaluationError, aggregate_query_metrics,
                                 MemoizingEmbeddingBackend, candidate_rows,
                                 evaluate_query, load_judgments,
                                 write_judgment_template, write_report,
                                 RELEVANCE_LABELS)
from app.core.hybrid_ranking import (DEFAULT_WEIGHTS, FIELD_WEIGHTS,
                                     HYBRID_METHOD, HYBRID_VERSION,
                                     RECENCY_HORIZON_YEARS, HybridRankingError,
                                     HybridScoredResource, rank_resources,
                                     validate_weights, keyword_score)
from app.core.semantic_similarity import (DEFAULT_MODEL_ID, SIMILARITY_METHOD,
                                          SIMILARITY_VERSION, MAX_RESOURCE_TEXT_CHARS,
                                          ScoredResource, SemanticSimilarityError,
                                          SentenceTransformerBackend, score_resources,
                                          runtime_versions)
from app.core.topic import MAX_TOPIC_LENGTH, validate_topic
from app.downloads import DownloadError, download_file
from app.models import LinkType, ResearchResource
from app.providers.openalex import MAX_RESULTS, OpenAlexError, search_papers as search_openalex
from app.providers.dataverse import DataverseError, search_datasets
from app.providers.crossref import CrossrefError, search_papers as search_crossref
from app.storage import DEFAULT_DB_PATH, ResearchStore, StorageError


def _add_database_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), metavar="PATH",
                        help=f"local SQLite database path (default: {DEFAULT_DB_PATH})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research-discovery",
                                     description=("Find and review academic papers or research datasets. Search with `search TOPIC` "
                                                  "or the backward-compatible positional research topic."))
    commands = parser.add_subparsers(dest="command")

    search = commands.add_parser("search", help="search for papers or datasets")
    search.add_argument("topic", help=f"research topic (1 to {MAX_TOPIC_LENGTH} characters)")
    search.add_argument("--type", choices=("papers", "datasets"), default="papers",
                        help="resource type to search (default: papers)")
    search.add_argument("--provider", choices=("openalex", "crossref", "all"), default=None,
                        help="paper provider (default: openalex; use all for OpenAlex and Crossref); datasets use Harvard Dataverse")
    search.add_argument("--limit", type=int, default=MAX_RESULTS, metavar="N",
                        help=f"maximum results to retrieve (1 to {MAX_RESULTS})")
    search.add_argument("--timeout", type=float, default=10.0, metavar="SECONDS",
                        help="request timeout in seconds (default: 10)")
    search.add_argument("--expand", action="store_true",
                        help="search the original topic and deterministic keyword variations")
    search.add_argument("--expand-limit", type=int, default=None, metavar="N",
                        help=f"maximum generated query variations when --expand is used (0 to {MAX_EXPANSIONS})")
    search.add_argument("--semantic", action="store_true",
                        help="compute optional sentence-embedding similarity scores")
    search.add_argument("--semantic-sort", action="store_true",
                        help="sort displayed results by semantic similarity (requires --semantic)")
    search.add_argument("--model", default=DEFAULT_MODEL_ID, metavar="ID",
                        help=f"sentence-transformer model identifier (default: {DEFAULT_MODEL_ID})")
    search.add_argument("--model-cache", default=None, metavar="PATH",
                        help="optional local model cache directory")
    search.add_argument("--hybrid", action="store_true",
                        help="compute and display the experimental hybrid ranking")
    search.add_argument("--hybrid-weights", nargs=3, type=float,
                        metavar=("SEMANTIC", "KEYWORD", "RECENCY"),
                        help="hybrid weights (nonnegative values summing to 1)")
    _add_database_option(search)

    history = commands.add_parser("history", help="list saved searches")
    _add_database_option(history)

    show = commands.add_parser("show", help="display results from a saved search")
    show.add_argument("search_id", type=int, help="saved search ID")
    _add_database_option(show)

    export = commands.add_parser("export", help="export saved results as CSV or JSON")
    export.add_argument("search_id", type=int, help="saved search ID")
    export.add_argument("--format", choices=("csv", "json"), required=True)
    export.add_argument("--output", required=True, metavar="PATH", help="new output file path")
    _add_database_option(export)

    download = commands.add_parser("download", help="download one selected provider-supplied file")
    download.add_argument("search_id", type=int, help="saved search ID")
    download.add_argument("result_number", type=int, help="1-based result number from that search")
    download.add_argument("--file", type=int, dest="file_number", metavar="N",
                          help="1-based downloadable file selection (required to download)")
    download.add_argument("--download-dir", default="data/downloads", metavar="PATH",
                          help="download directory (default: data/downloads)")
    download.add_argument("--timeout", type=float, default=15.0, metavar="SECONDS",
                          help="connection/read timeout (default: 15)")
    download.add_argument("--max-size", type=int, default=100 * 1024 * 1024, metavar="BYTES",
                          help="maximum file size (default: 104857600)")
    _add_database_option(download)

    evaluate = commands.add_parser("evaluate", help="evaluate rankings against human relevance judgments")
    evaluation_mode = evaluate.add_mutually_exclusive_group(required=True)
    evaluation_mode.add_argument("--judgments", metavar="PATH",
                                 help="CSV or JSON human relevance judgments")
    evaluation_mode.add_argument("--template", action="store_true",
                                 help="export a blank annotation template from saved candidate searches")
    evaluate.add_argument("--search-id", type=int, action="append", metavar="ID",
                          help="saved search ID (repeat for multiple queries)")
    evaluate.add_argument("--k", type=int, nargs="+", default=[5], metavar="N",
                          help="one or more positive cutoff positions (default: 5)")
    evaluate.add_argument("--relevant-threshold", type=int, choices=(1, 2), default=2,
                          help="binary relevance cutoff for P/R/MRR (default: 2)")
    evaluate.add_argument("--hybrid-weights", type=float, nargs=3, action="append",
                          metavar=("SEMANTIC", "KEYWORD", "RECENCY"),
                          help="additional hybrid weights; repeat to compare configurations")
    evaluate.add_argument("--model", default=DEFAULT_MODEL_ID, metavar="ID",
                          help="sentence-transformer model identifier")
    evaluate.add_argument("--model-cache", default=None, metavar="PATH",
                          help="optional local model cache directory")
    evaluate.add_argument("--format", choices=("json", "csv"), default="json")
    evaluate.add_argument("--output", metavar="PATH",
                          help="write report or annotation template; required for --template")
    _add_database_option(evaluate)

    server = commands.add_parser("serve", help="start the optional authenticated local HTTP bridge for n8n")
    server.add_argument("--host", default="127.0.0.1", help="loopback address to bind (default: 127.0.0.1)")
    server.add_argument("--port", type=int, default=8765, help="local API port (default: 8765)")
    _add_database_option(server)
    return parser


def _unknown(value: object, label: str) -> str:
    return str(value) if value is not None else f"Unknown {label}"


def format_resource(index: int, resource: ResearchResource) -> str:
    """Render every known normalized field without inferring absent metadata."""
    landing = next((link.url for link in resource.links if link.kind == LinkType.LANDING_PAGE), None)
    if landing is None and resource.external_id:
        try:
            parsed = urlsplit(resource.external_id)
            parsed.port
            if parsed.scheme in ("http", "https") and parsed.hostname:
                landing = resource.external_id
        except ValueError:
            pass
    lines = [f"{index}. {_unknown(resource.title, 'title')}"]
    sources = tuple(dict.fromkeys(
        item.source_provider for item in resource.provenance if item.source_provider
    )) or (resource.source_provider,)
    lines.extend((
        f"   Authors: {', '.join(resource.authors) if resource.authors is not None else 'Unknown authors'}",
        f"   Publication year: {_unknown(resource.publication_year, 'year')}",
        f"   Abstract: {_unknown(resource.description, 'abstract')}",
        f"   DOI: {_unknown(resource.doi, 'DOI')}",
        f"   Keywords: {', '.join(resource.keywords) if resource.keywords is not None else 'Unknown keywords'}",
        f"   Provider: {resource.source_provider or 'Unknown provider'}",
        f"   Sources: {', '.join(sources) if sources else 'Unknown provider'}",
        f"   Source URL: {landing or 'Unknown URL'}",
        f"   Open-access URL: {', '.join(link.url for link in resource.links if link.kind == LinkType.OPEN_ACCESS) or 'Unknown URL'}",
        f"   License: {_unknown(resource.license, 'license')}",
        f"   Open access: {_unknown(resource.is_open_access, 'access status')}",
    ))
    if resource.links:
        lines.append("   Links:")
        lines.extend(f"     - {link.kind.value}: {link.url} ({link.source_provider})"
                     for link in resource.links)
    if resource.downloadable_files:
        lines.append("   Downloadable files:")
        for file in resource.downloadable_files:
            detail = ", ".join(value for value in (file.file_format, file.mime_type, file.filename) if value)
            access = "restricted" if file.is_open_access is False else (
                "public" if file.is_open_access is True else "access unknown"
            )
            lines.append(f"     - Direct file endpoint: {file.url} ({access})" + (f"; {detail}" if detail else ""))
    else:
        lines.append("   Downloadable files: None reported")
    return "\n".join(lines)


def display_resources(resources: Sequence[ResearchResource], empty_label: str = "papers",
                      scores: Sequence[ScoredResource] | None = None) -> None:
    if not resources:
        print(f"No {empty_label} found.")
        return
    for index, resource in enumerate(resources, 1):
        print(format_resource(index, resource))
        if scores is not None:
            score = scores[index - 1].similarity
            rendered = "unavailable" if score is None else f"{score:.4f}"
            print(f"   Semantic similarity (cosine): {rendered}")


def display_hybrid_resources(results: Sequence[HybridScoredResource], empty_label: str = "papers") -> None:
    """Display hybrid component values without interpreting them as probabilities."""
    if not results:
        print(f"No {empty_label} found.")
        return
    for index, item in enumerate(results, 1):
        print(format_resource(index, item.resource))
        final = "unavailable" if item.hybrid_score is None else f"{item.hybrid_score:.4f}"
        cosine = "unavailable" if item.semantic_cosine is None else f"{item.semantic_cosine:.4f}"
        semantic = ("unavailable" if item.semantic_component is None
                    else f"{item.semantic_component:.4f}")
        print(f"   Hybrid relevance signal (experimental): {final}")
        print(f"   Components: semantic cosine={cosine}, normalized semantic={semantic}, "
              f"keyword={item.keyword_component:.4f}, recency={item.metadata_component:.4f}")


def _resolve_search(search_id: int, db_path: str) -> tuple[str, list[ResearchResource], dict]:
    summaries = list_history(db_path)
    summary = next((item for item in summaries if item.id == search_id), None)
    if summary is None:
        raise ValueError(f"saved search ID {search_id} was not found")
    return summary.topic, get_history_resources(search_id, db_path), summary.metadata or {}


def _run_search(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    semantic_enabled = getattr(args, "semantic", False)
    hybrid_enabled = getattr(args, "hybrid", False)
    semantic_sort = getattr(args, "semantic_sort", False)
    model_id = getattr(args, "model", DEFAULT_MODEL_ID)
    model_cache = getattr(args, "model_cache", None)
    try:
        topic = validate_topic(args.topic)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.limit <= MAX_RESULTS:
        parser.error(f"limit must be between 1 and {MAX_RESULTS}")
    if not 0 < args.timeout < float("inf"):
        parser.error("timeout must be a positive finite number")
    expansion_limit = MAX_EXPANSIONS if args.expand_limit is None else args.expand_limit
    if not 0 <= expansion_limit <= MAX_EXPANSIONS:
        parser.error(f"--expand-limit must be between 0 and {MAX_EXPANSIONS}")
    if args.expand_limit is not None and not args.expand:
        parser.error("--expand-limit requires --expand")
    if semantic_sort and not semantic_enabled:
        parser.error("--semantic-sort requires --semantic")
    if hybrid_enabled and semantic_enabled:
        parser.error("--hybrid already includes semantic scoring; omit --semantic")
    if getattr(args, "hybrid_weights", None) is not None and not hybrid_enabled:
        parser.error("--hybrid-weights requires --hybrid")
    try:
        hybrid_weights = validate_weights(
            args.hybrid_weights if getattr(args, "hybrid_weights", None) is not None
            else DEFAULT_WEIGHTS
        )
    except HybridRankingError as exc:
        parser.error(str(exc))
    derived_enabled = semantic_enabled or hybrid_enabled
    if not isinstance(model_id, str) or not model_id.strip():
        parser.error("--model must not be blank")
    search_id = None
    try:
        if args.expand:
            if args.type == "datasets":
                if args.provider is not None:
                    parser.error("--provider applies only to paper searches; datasets use Harvard Dataverse")
                providers = (("Harvard Dataverse", search_datasets),)
            elif args.provider == "crossref":
                providers = (("Crossref", lambda value, **options: search_crossref(
                    value, **options, contact_email=os.environ.get("CROSSREF_MAILTO")
                )),)
            elif args.provider == "all":
                providers = (("OpenAlex", search_openalex),
                             ("Crossref", lambda value, **options: search_crossref(
                                 value, **options, contact_email=os.environ.get("CROSSREF_MAILTO")
                             )))
            else:
                providers = (("OpenAlex", search_openalex),)
            outcome = expanded_search_and_save(
                topic, limit=args.limit, timeout=args.timeout, db_path=args.db,
                expansion_limit=expansion_limit, providers=providers,
            )
            resources = outcome.resources
            search_id = getattr(outcome, "search_id", None)
            for index, query in enumerate(outcome.queries, 1):
                label = "original" if index == 1 else "expanded"
                print(f"Query {index} ({label}): {query}")
            print(f"Providers: {', '.join(outcome.providers)}")
            print(f"Provider search requests: {outcome.provider_requests}/"
                  f"{MAX_EXPANDED_PROVIDER_REQUESTS} maximum")
            print(f"Worst-case HTTP attempt bound: {MAX_EXPANDED_HTTP_ATTEMPTS}")
            print(f"Records retrieved: {outcome.records_retrieved}")
            print(f"Unique resources: {outcome.unique_resources}")
            print(f"Duplicates removed: {outcome.duplicates_removed}")
            if outcome.failed_queries:
                print("Search status: PARTIAL")
                for failed_query, message in outcome.failed_queries.items():
                    print(f"Warning: {failed_query} failed: {message}", file=sys.stderr)
            # Scoring and the common display path below apply to expanded searches too.
        if not args.expand and args.type == "datasets":
            if args.provider is not None:
                parser.error("--provider applies only to paper searches; datasets use Harvard Dataverse")
            if derived_enabled:
                resources, search_id = search_and_save_with_id(
                    topic, limit=args.limit, timeout=args.timeout,
                    db_path=args.db, provider=search_datasets)
            else:
                resources = search_and_save(topic, limit=args.limit, timeout=args.timeout,
                                            db_path=args.db, provider=search_datasets)
        elif not args.expand and args.provider == "crossref":
            crossref_adapter = lambda value, **options: search_crossref(
                value, **options, contact_email=os.environ.get("CROSSREF_MAILTO")
            )
            if derived_enabled:
                resources, search_id = search_and_save_with_id(
                    topic, limit=args.limit, timeout=args.timeout, db_path=args.db,
                    provider=crossref_adapter)
            else:
                resources = search_and_save(
                    topic, limit=args.limit, timeout=args.timeout, db_path=args.db,
                    provider=crossref_adapter)
        elif not args.expand and args.provider == "all":
            print("Searching academic providers...")
            outcome = search_all_and_save(
                topic, limit=args.limit, timeout=args.timeout, db_path=args.db,
                crossref_provider=lambda value, **options: search_crossref(
                    value, **options, contact_email=os.environ.get("CROSSREF_MAILTO")
                ),
            )
            resources = outcome.resources
            search_id = getattr(outcome, "search_id", None)
            print(f"OpenAlex: {outcome.provider_counts.get('OpenAlex', 0)} papers")
            print(f"Crossref: {outcome.provider_counts.get('Crossref', 0)} papers")
            print(f"Combined results: {len(resources)} unique papers")
            print(f"Duplicates merged: {outcome.duplicates_merged}")
            if outcome.failed_providers:
                print("Search status: PARTIAL")
            for provider_name, message in outcome.failed_providers.items():
                print(f"Warning: {provider_name} search failed: {message}; showing partial results.",
                      file=sys.stderr)
        elif not args.expand:
            if derived_enabled:
                resources, search_id = search_and_save_with_id(
                    topic, limit=args.limit, timeout=args.timeout, db_path=args.db)
            else:
                resources = search_and_save(topic, limit=args.limit, timeout=args.timeout,
                                            db_path=args.db)
    except (OpenAlexError, DataverseError, CrossrefError, CombinedSearchError,
            ExpandedSearchError, StorageError) as exc:
        print(f"Search error: {exc}", file=sys.stderr)
        return 1
    semantic_scores = None
    if hybrid_enabled:
        try:
            ranking_as_of_year = datetime.now(timezone.utc).year
            backend = (SentenceTransformerBackend(model_id, cache_dir=model_cache)
                       if hybrid_weights["semantic"] > 0 else None)
            hybrid_results = rank_resources(
                topic, resources, backend, weights=hybrid_weights,
                as_of_year=ranking_as_of_year,
                sort_by_score=True,
            )
            if search_id is not None:
                with ResearchStore(args.db) as store:
                    store.merge_search_metadata(search_id, {"hybrid_ranking": {
                        "method": HYBRID_METHOD,
                        "version": HYBRID_VERSION,
                        "model_id": model_id if hybrid_weights["semantic"] > 0 else None,
                        "model_revision": backend.model_revision if backend is not None else None,
                        "runtime_versions": runtime_versions() if hybrid_weights["semantic"] > 0 else {},
                        "device": "cpu" if hybrid_weights["semantic"] > 0 else None,
                        "weights": hybrid_weights,
                        "component_ranges": {"semantic_cosine": [-1.0, 1.0],
                                             "semantic_normalized": [0.0, 1.0],
                                             "keyword": [0.0, 1.0], "recency": [0.0, 1.0],
                                             "hybrid": [0.0, 1.0]},
                        "keyword_method": "topic_token_coverage_v1",
                        "tokenization": "Unicode NFKC normalization and casefold; punctuation and whitespace split",
                        "stopword_policy": "small English-only list; other languages retained",
                        "field_weights": FIELD_WEIGHTS,
                        "resource_text_max_chars": MAX_RESOURCE_TEXT_CHARS,
                        "as_of_year": ranking_as_of_year,
                        "recency_horizon_years": RECENCY_HORIZON_YEARS,
                        "missing_year_score": 0.5,
                        "sorted_for_display": True,
                        "original_order": [{"position": index + 1,
                                            "source_provider": item.source_provider,
                                            "external_id": item.external_id}
                                           for index, item in enumerate(resources)],
                        "scores": [{"original_position": item.original_position + 1,
                                    "source_provider": item.resource.source_provider,
                                    "external_id": item.resource.external_id,
                                    "semantic_cosine": item.semantic_cosine,
                                    "semantic_normalized": item.semantic_component,
                                    "keyword": item.keyword_component,
                                    "recency": item.metadata_component,
                                    "hybrid": item.hybrid_score,
                                    "effective_weights": item.effective_weights}
                                   for item in hybrid_results],
                    }})
            print("Hybrid ranking: experimental relevance signal; provider order is retained in saved history.")
            print("Weights: " + ", ".join(f"{name}={value:g}" for name, value in hybrid_weights.items()))
            print(f"Search saved: {topic}")
            display_hybrid_resources(hybrid_results,
                                     "datasets" if args.type == "datasets" else "papers")
            return 0
        except (HybridRankingError, SemanticSimilarityError, StorageError) as exc:
            print(f"Hybrid ranking error: {exc}", file=sys.stderr)
            print(f"Search saved: {topic}")
            display_resources(resources, "datasets" if args.type == "datasets" else "papers")
            return 1
    if semantic_enabled:
        try:
            backend = SentenceTransformerBackend(model_id, cache_dir=model_cache)
            semantic_scores = score_resources(topic, resources, backend,
                                              sort_by_similarity=semantic_sort)
            if search_id is not None:
                with ResearchStore(args.db) as store:
                    store.merge_search_metadata(search_id, {"semantic_similarity": {
                        "method": SIMILARITY_METHOD,
                        "version": SIMILARITY_VERSION,
                        "model_id": model_id,
                        "runtime_versions": runtime_versions(),
                        "device": "cpu",
                        "resource_text_max_chars": MAX_RESOURCE_TEXT_CHARS,
                        "sorted_for_display": semantic_sort,
                        "scores": [{"position": item.original_position + 1,
                                    "source_provider": item.resource.source_provider,
                                    "external_id": item.resource.external_id,
                                    "score": item.similarity}
                                   for item in semantic_scores],
                    }})
            if semantic_sort:
                resources = [item.resource for item in semantic_scores]
        except (SemanticSimilarityError, StorageError) as exc:
            print(f"Semantic scoring error: {exc}", file=sys.stderr)
            print(f"Search saved: {topic}")
            display_resources(resources, "datasets" if args.type == "datasets" else "papers")
            return 1
    print(f"Search saved: {topic}")
    display_resources(resources, "datasets" if args.type == "datasets" else "papers",
                       semantic_scores)
    return 0


def _configure_console_encoding() -> None:
    """Escape unsupported console characters instead of aborting after saving."""
    for stream in (sys.stdout, sys.stderr):
        configure = getattr(stream, "reconfigure", None)
        if callable(configure):
            try:
                configure(errors="backslashreplace")
            except (OSError, ValueError):
                # Embedded callers may expose a stream that cannot be reconfigured.
                # Their own stream policy remains in control; stored text is untouched.
                pass


def _run_evaluation(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Export annotation templates or evaluate saved candidate snapshots offline."""
    if args.template:
        if not args.search_id:
            parser.error("--template requires at least one --search-id")
        if not args.output:
            parser.error("--template requires --output")
        try:
            with ResearchStore(args.db) as store:
                summaries = {item.id: item for item in store.list_searches()}
                missing = [item for item in args.search_id if item not in summaries]
                if missing:
                    raise EvaluationError("saved search ID(s) not found: " + ", ".join(map(str, missing)))
                if len(set(args.search_id)) != len(args.search_id):
                    raise EvaluationError("duplicate --search-id values are not allowed")
                searches = [(search_id, summaries[search_id].topic,
                             store.get_search_resources_with_ids(search_id))
                            for search_id in args.search_id]
            output = write_judgment_template(candidate_rows(searches), args.output)
            print(f"Annotation template ({DATASET_VERSION}): {output}")
            print("Assign relevance 0=not relevant, 1=partially relevant, 2=highly relevant; "
                  "leave relevance blank to mark a candidate unjudged. Fill annotator for each label.")
            return 0
        except (EvaluationError, StorageError, OSError) as exc:
            print(f"Evaluation error: {exc}", file=sys.stderr)
            return 1

    if not args.judgments:
        parser.error("provide --judgments PATH or use --template")
    if not args.output and args.format != "json":
        parser.error("--format csv requires --output")
    if any(isinstance(k, bool) or k < 1 for k in args.k):
        parser.error("--k values must be positive integers")
    args.k = list(dict.fromkeys(args.k))
    try:
        dataset_version, rows = load_judgments(args.judgments)
        search_ids_in_file = list(dict.fromkeys(row.search_id for row in rows))
        selected_ids = args.search_id or search_ids_in_file
        if set(selected_ids) != set(search_ids_in_file):
            raise EvaluationError("--search-id values must match the search IDs in the judgments file")
        if len(set(selected_ids)) != len(selected_ids):
            raise EvaluationError("duplicate --search-id values are not allowed")
        with ResearchStore(args.db) as store:
            summaries = {item.id: item for item in store.list_searches()}
            candidate_searches = {}
            for search_id in selected_ids:
                if search_id not in summaries:
                    raise EvaluationError(f"saved search ID {search_id} was not found")
                summary = summaries[search_id]
                candidate_searches[search_id] = (summary.topic,
                    store.get_search_resources_with_ids(search_id))
        labels: dict[str, dict[str, int | None]] = {}
        for row in rows:
            topic, candidates = candidate_searches[row.search_id]
            expected_qid = f"search-{row.search_id}"
            if row.query_id != expected_qid or row.topic != topic:
                raise EvaluationError(f"judgment row for search {row.search_id} does not match its saved topic/query ID")
            candidate = dict(candidates).get(row.resource_id)
            if candidate is None:
                raise EvaluationError(f"resource ID {row.resource_id} is not in saved search {row.search_id}")
            expected_candidate = (
                candidate.title or "", candidate.description or "",
                "; ".join(candidate.keywords or ()), candidate.resource_type.value,
                candidate.source_provider, tuple(link.url for link in candidate.links),
            )
            observed_candidate = (row.title, row.abstract, row.keywords, row.resource_type,
                                  row.source_provider, row.source_links)
            if observed_candidate != expected_candidate:
                raise EvaluationError(f"candidate metadata for resource {row.resource_id} was changed from the saved snapshot")
            labels.setdefault(row.query_id, {})[str(row.resource_id)] = row.relevance
        candidate_ids = {search_id: {resource_id for resource_id, _ in record[1]}
                         for search_id, record in candidate_searches.items()}
        for search_id in selected_ids:
            query_id = f"search-{search_id}"
            judged_ids = {row.resource_id for row in rows if row.search_id == search_id}
            if judged_ids != candidate_ids[search_id]:
                raise EvaluationError(f"judgment rows for {query_id} must contain every saved candidate exactly once")
            if not any(row.search_id == search_id for row in rows):
                raise EvaluationError(f"no judgment rows for search {search_id}")
        all_rows: list[dict[str, Any]] = []
        query_results: list[dict[str, Any]] = []
        semantic_backend = MemoizingEmbeddingBackend(
            SentenceTransformerBackend(args.model, cache_dir=args.model_cache))
        configurations = [("hybrid_default", dict(DEFAULT_WEIGHTS))]
        evaluation_year = datetime.now(timezone.utc).year
        for index, weights in enumerate(args.hybrid_weights or (), start=1):
            try:
                validated = validate_weights(weights)
            except HybridRankingError as exc:
                raise EvaluationError(str(exc)) from exc
            configurations.append((f"hybrid_config_{index}", validated))
        for search_id in selected_ids:
            query_id = f"search-{search_id}"
            topic, pairs = candidate_searches[search_id]
            resource_ids = [str(resource_id) for resource_id, _ in pairs]
            resources = [resource for _, resource in pairs]
            rankings: dict[str, list[str]] = {}
            lexical_order = sorted(enumerate(pairs),
                                   key=lambda entry: (-keyword_score(topic, entry[1][1]), entry[0]))
            rankings["keyword"] = [str(pair[0]) for _, pair in lexical_order]
            semantic_results = score_resources(topic, resources, semantic_backend, sort_by_similarity=True)
            rankings["semantic"] = [resource_ids[item.original_position]
                                     for item in semantic_results]
            methods: dict[str, dict[str, float | None]] = {}
            for name, weights in configurations:
                ranked = rank_resources(topic, resources, semantic_backend, weights=weights,
                                         as_of_year=evaluation_year, sort_by_score=True)
                rankings[name] = [str(pairs[item.original_position][0]) for item in ranked]
            query_labels = labels[query_id]
            methods = evaluate_query(rankings, query_labels, args.k,
                                     relevant_threshold=args.relevant_threshold)
            provenance = [{"resource_id": row.resource_id, "relevance": row.relevance,
                           "annotator": row.annotator, "notes": row.notes}
                          for row in rows if row.query_id == query_id]
            query_results.append({"query_id": query_id, "topic": topic,
                                  "judged_resource_count": len(query_labels),
                                  "unjudged_resource_count": sum(value is None for value in query_labels.values()),
                                  "known_relevant_count": sum(value is not None and value >= args.relevant_threshold
                                                               for value in query_labels.values()),
                                  "judgment_provenance": provenance,
                                  "methods": methods,
                                  "rankings": rankings})
            for resource_id, resource in pairs:
                all_rows.append({"query_id": query_id, "resource_id": resource_id,
                                 "title": resource.title, "description": resource.description,
                                 "keywords": resource.keywords,
                                 "resource_type": resource.resource_type.value,
                                 "source_provider": resource.source_provider,
                                 "external_id": resource.external_id, "doi": resource.doi,
                                 "publication_year": resource.publication_year,
                                 "source_links": [link.url for link in resource.links]})
        aggregates = aggregate_query_metrics([item["methods"] for item in query_results])
        eligible_query_count = sum(item["known_relevant_count"] > 0 for item in query_results)
        snapshot_json = json.dumps(all_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        judgments_bytes = Path(args.judgments).read_bytes()
        report = {
            "dataset_version": dataset_version,
            "evaluation_code_version": EVALUATION_CODE_VERSION,
            "evaluation_date": datetime.now(timezone.utc).isoformat(),
            "python_version": platform.python_version(),
            "judgments_file": str(Path(args.judgments)),
            "judgments_sha256": hashlib.sha256(judgments_bytes).hexdigest(),
            "candidate_snapshot_sha256": hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
            "query_count": len(query_results),
            "eligible_query_count": eligible_query_count,
            "judged_resource_count": sum(item["judged_resource_count"] for item in query_results),
            "unjudged_resource_count": sum(item["unjudged_resource_count"] for item in query_results),
            "relevant_threshold": args.relevant_threshold,
            "k_values": args.k,
            "metric_definitions": {
                "binary_relevance": "grade >= relevant_threshold",
                "grades": RELEVANCE_LABELS,
                "unjudged": "omitted before applying K; metrics are over judged candidates only",
                "recall": "recall within the judged candidate pool, not scholarly literature recall",
                "aggregation": "macro mean over queries with at least one threshold-relevant judged resource; ineligible queries are omitted for every aggregate metric",
                "ndcg_gain": "2^grade - 1; log2(position + 1) discount",
            },
            "ranking_configuration": {
                "keyword": {"method": "hybrid keyword_score", "version": "hybrid-ranking-v1"},
                "semantic": {"method": SIMILARITY_METHOD, "model_id": args.model,
                             "model_revision": semantic_backend.backend.model_revision,
                             "runtime_versions": runtime_versions(), "device": "cpu"},
                "hybrid_configurations": {name: weights for name, weights in configurations},
                "hybrid_as_of_year": evaluation_year,
                "hybrid_recency_horizon_years": RECENCY_HORIZON_YEARS,
            },
            "limitations": [
                "Metrics assess only the supplied human-labeled candidate pool.",
                "No relevance labels are generated by ranking scores.",
                "Aggregate metrics omit queries without any threshold-relevant judgments.",
                "Results are descriptive and do not establish statistical significance or generalization.",
            ],
            "queries": query_results,
            "aggregate": aggregates,
            "saved_search_provenance": [
                {"search_id": search_id,
                 "topic": summaries[search_id].topic,
                 "searched_at": summaries[search_id].searched_at.isoformat(),
                 "result_count": summaries[search_id].result_count,
                 "search_metadata": summaries[search_id].metadata}
                for search_id in selected_ids
            ],
            "candidate_snapshot": all_rows,
        }
        if args.output:
            output = write_report(report, args.output, args.format)
            print(f"Evaluation report written: {output}")
        print(f"Dataset {dataset_version} | queries={len(query_results)} | "
              f"judged={report['judged_resource_count']} | unjudged={report['unjudged_resource_count']}")
        for method, metrics in aggregates.items():
            print(method + " | " + " | ".join(
                f"{metric}={value:.4f}" if value is not None else f"{metric}=N/A"
                for metric, value in metrics.items()))
        return 0
    except (EvaluationError, HybridRankingError, SemanticSimilarityError, StorageError,
            OSError, ValueError) as exc:
        print(f"Evaluation error: {exc}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console_encoding()
    parser = build_parser()
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] not in {"search", "history", "show", "export", "download", "evaluate", "serve", "-h", "--help"}:
        args_list.insert(0, "search")
    args = parser.parse_args(args_list)
    if args.command is None:
        parser.error("a research topic or command is required")
    if args.command == "search":
        return _run_search(args, parser)
    if args.command == "evaluate":
        return _run_evaluation(args, parser)
    if args.command == "serve":
        from app.integrations.n8n.server import serve
        try:
            serve(host=args.host, port=args.port, db_path=args.db)
        except (OSError, ValueError) as exc:
            print(f"Server error: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        if args.command == "history":
            searches = list_history(args.db)
            if not searches:
                print("No saved searches.")
            for item in searches:
                suffix = " | PARTIAL" if (item.metadata or {}).get("status") == "partial" else ""
                expansion = (item.metadata or {}).get("query_expansion", {})
                if expansion:
                    suffix += f" | expanded ({len(expansion.get('queries', []))} queries)"
                if (item.metadata or {}).get("hybrid_ranking"):
                    suffix += " | hybrid ranked"
                print(f"{item.id}. {item.topic} | {item.searched_at.isoformat()} | {item.result_count} results{suffix}")
            return 0
        topic, resources, metadata = _resolve_search(args.search_id, args.db)
        if args.command == "download":
            if args.result_number < 1 or args.result_number > len(resources):
                raise ValueError(f"result number must be between 1 and {len(resources)}")
            resource = resources[args.result_number - 1]
            print(f"Saved search {args.search_id}: {topic}")
            print(f"{args.result_number}. {resource.title or 'Unknown title'}")
            if resource.license:
                print(f"License: {resource.license}")
            if not resource.downloadable_files:
                print("No provider-supplied downloadable files are available.")
                pages = [link.url for link in resource.links if link.kind == LinkType.LANDING_PAGE]
                if pages:
                    print("Landing page: " + ", ".join(pages))
                return 1
            for index, item in enumerate(resource.downloadable_files, 1):
                state = "restricted" if item.is_open_access is False else (
                    "accessible status unknown" if item.is_open_access is None else "provider-marked public")
                print(f"File {index}: {item.filename or 'Unknown filename'} | "
                      f"{item.file_format or 'Unknown format'} | {item.mime_type or 'Unknown MIME type'} | "
                      f"{state} | {item.source_provider} | {item.url}")
            if args.file_number is None:
                print("Select a file explicitly with --file N; no download was started.")
                return 0
            if args.file_number < 1 or args.file_number > len(resource.downloadable_files):
                raise ValueError(f"file number must be between 1 and {len(resource.downloadable_files)}")
            if not math.isfinite(args.timeout) or args.timeout <= 0 or args.max_size <= 0:
                raise ValueError("--timeout must be positive and finite; --max-size must be positive")
            selected = resource.downloadable_files[args.file_number - 1]
            path = download_file(selected, resource, download_dir=args.download_dir,
                                 timeout=args.timeout, max_bytes=args.max_size)
            print(f"Download status: complete ({path.stat().st_size} bytes)")
            print(f"Source URL: {selected.url}")
            print(f"Saved to: {path}")
            return 0
        if args.command == "show":
            print(f"Saved search {args.search_id}: {topic}")
            ranking = metadata.get("hybrid_ranking")
            if isinstance(ranking, dict):
                print(f"Hybrid ranking: {ranking.get('method')} ({ranking.get('version')})")
                print("Weights: " + ", ".join(
                    f"{name}={value:g}" for name, value in ranking.get("weights", {}).items()
                ))
                if ranking.get("model_id"):
                    print(f"Semantic model: {ranking['model_id']} "
                          f"(revision: {ranking.get('model_revision') or 'unavailable'})")
                by_position = {index + 1: resource for index, resource in enumerate(resources)}
                for result in sorted(ranking.get("scores", []),
                                     key=lambda item: item.get("original_position", 0)):
                    position = result.get("original_position")
                    resource = by_position.get(position)
                    title = resource.title if resource is not None else "Unknown title"
                    final = result.get("hybrid")
                    rendered = "unavailable" if final is None else f"{final:.4f}"
                    print(f"Original result {position}: {title} | hybrid={rendered} | "
                          f"semantic={result.get('semantic_normalized')} | "
                          f"keyword={result.get('keyword')} | recency={result.get('recency')}")
            expansion = metadata.get("query_expansion")
            if isinstance(expansion, dict):
                print(f"Query expansion: {expansion.get('method')} ({expansion.get('version')})")
                print(f"Providers: {', '.join(metadata.get('providers', []))}")
                for index, query in enumerate(expansion.get("queries", []), 1):
                    print(f"Query {index}: {query}")
            if metadata.get("status") == "partial":
                print("Saved search status: PARTIAL")
                for provider_name, message in metadata.get("failed_providers", {}).items():
                    print(f"Warning: {provider_name} failed: {message}")
            display_resources(resources)
            return 0
        output = write_export(resources, args.output, args.format, metadata)
        print(f"Exported {len(resources)} results to {output}")
        return 0
    except ValueError as exc:
        parser.error(str(exc))
    except DownloadError as exc:
        print(f"Download error: {exc}", file=sys.stderr)
        return 1
    except (StorageError, OSError) as exc:
        print(f"Command error: {exc}", file=sys.stderr)
        return 1
    return 0
