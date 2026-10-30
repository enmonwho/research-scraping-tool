"""Qt-independent operations used by the desktop interface."""

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Callable, Sequence

from app.core.query_expansion import MAX_EXPANSIONS
from app.core.search_service import (CombinedSearchOutcome, ExpandedSearchOutcome,
                                    expanded_search_and_save, get_history_resources,
                                    list_history, search_all_and_save,
                                    search_and_save_with_id)
from app.core.topic import validate_topic
from app.models import DownloadableFile, ResearchResource
from app.providers.crossref import search_papers as search_crossref
from app.providers.dataverse import search_datasets
from app.providers.openalex import MAX_RESULTS, search_papers as search_openalex
from app.storage import ResearchStore, SearchSummary


@dataclass(frozen=True)
class SearchResult:
    search_id: int
    topic: str
    resources: list[ResearchResource]
    metadata: dict[str, Any]
    display_scores: dict[str, dict[str, Any]]
    ranking_error: str | None = None


class ResearchController:
    """Coordinate searches and saved-result operations without Qt dependencies."""

    def __init__(self, db_path: str | Path | None = None, *,
                 default_db: Callable[[], Path] | None = None,
                 openalex_provider: Callable[..., list[ResearchResource]] = search_openalex,
                 crossref_provider: Callable[..., list[ResearchResource]] = search_crossref,
                 dataset_provider: Callable[..., list[ResearchResource]] = search_datasets) -> None:
        if db_path is None:
            if default_db is None:
                from app.core.app_paths import default_database_path
                default_db = default_database_path
            db_path = default_db()
        self.db_path = Path(db_path)
        self.openalex_provider = openalex_provider
        self.crossref_provider = crossref_provider
        self.dataset_provider = dataset_provider

    def search(self, *, topic: str, resource_type: str = "papers", provider: str = "openalex",
               limit: int = MAX_RESULTS, timeout: float = 10.0,
               expand: bool = False, expansion_limit: int = MAX_EXPANSIONS,
               ranking: str = "none", model_id: str | None = None,
               model_cache: str | None = None) -> SearchResult:
        topic = validate_topic(topic)
        if resource_type not in {"papers", "datasets"}:
            raise ValueError("resource_type must be papers or datasets")
        if provider not in {"openalex", "crossref", "all"}:
            raise ValueError("provider must be openalex, crossref, or all")
        if resource_type == "datasets" and provider != "openalex":
            raise ValueError("paper provider selection is unavailable for dataset searches")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
            raise ValueError(f"limit must be between 1 and {MAX_RESULTS}")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
            raise ValueError("timeout must be a positive finite number")
        if isinstance(expansion_limit, bool) or not isinstance(expansion_limit, int) or not 0 <= expansion_limit <= MAX_EXPANSIONS:
            raise ValueError(f"expansion_limit must be between 0 and {MAX_EXPANSIONS}")
        if ranking not in {"none", "semantic", "hybrid"}:
            raise ValueError("ranking must be none, semantic, or hybrid")

        if expand:
            if resource_type == "datasets":
                providers = (("Harvard Dataverse", self.dataset_provider),)
            elif provider == "all":
                providers = (("OpenAlex", self.openalex_provider),
                             ("Crossref", self._crossref_with_mailto))
            elif provider == "crossref":
                providers = (("Crossref", self._crossref_with_mailto),)
            else:
                providers = (("OpenAlex", self.openalex_provider),)
            outcome: ExpandedSearchOutcome = expanded_search_and_save(
                topic, limit=limit, timeout=timeout, db_path=self.db_path,
                expansion_limit=expansion_limit, providers=providers,
            )
            search_id, resources = outcome.search_id, outcome.resources
        elif resource_type == "datasets":
            resources, search_id = search_and_save_with_id(
                topic, limit=limit, timeout=timeout, db_path=self.db_path, provider=self.dataset_provider
            )
        elif provider == "all":
            outcome: CombinedSearchOutcome = search_all_and_save(
                topic, limit=limit, timeout=timeout, db_path=self.db_path,
                openalex_provider=self.openalex_provider,
                crossref_provider=self._crossref_with_mailto,
            )
            search_id, resources = outcome.search_id, outcome.resources
        else:
            selected = self._crossref_with_mailto if provider == "crossref" else self.openalex_provider
            resources, search_id = search_and_save_with_id(
                topic, limit=limit, timeout=timeout, db_path=self.db_path, provider=selected
            )
        with ResearchStore(self.db_path) as store:
            summary = next((item for item in store.list_searches() if item.id == search_id), None)
            metadata = summary.metadata or {} if summary else {}
        resources = get_history_resources(search_id, self.db_path)
        if ranking != "none":
            saved_metadata = dict(metadata)
            try:
                resources, metadata, scores = self._rank(
                    topic, resources, search_id, metadata, ranking, model_id, model_cache
                )
                ranking_error = None
            except Exception as exc:
                # Search persistence is already complete. Preserve and return the saved
                # provider results if optional model loading or inference fails.
                ranking_error = str(exc) or exc.__class__.__name__
                metadata = saved_metadata
                scores = {}
        else:
            scores = {}
            ranking_error = None
        return SearchResult(search_id, topic, resources, metadata, scores, ranking_error)

    def _crossref_with_mailto(self, topic: str, **options: Any) -> list[ResearchResource]:
        return self.crossref_provider(topic, **options, contact_email=os.environ.get("CROSSREF_MAILTO"))

    def _rank(self, topic: str, resources: list[ResearchResource], search_id: int,
              metadata: dict[str, Any], ranking: str, model_id: str | None,
              model_cache: str | None) -> tuple[list[ResearchResource], dict[str, Any], dict[str, dict[str, Any]]]:
        from app.core.semantic_similarity import (DEFAULT_MODEL_ID, SentenceTransformerBackend,
                                                  score_resources, runtime_versions,
                                                  SIMILARITY_METHOD, SIMILARITY_VERSION,
                                                  MAX_RESOURCE_TEXT_CHARS)
        selected_model = model_id or DEFAULT_MODEL_ID
        if model_cache is None:
            from app.core.app_paths import default_model_cache_dir
            model_cache = str(default_model_cache_dir())
        backend = SentenceTransformerBackend(model_id=selected_model, cache_dir=model_cache)
        scores: dict[str, dict[str, Any]] = {}
        if ranking == "semantic":
            scored = score_resources(topic, resources, backend, sort_by_similarity=True)
            by_key = {self._resource_key(item.resource): {
                "semantic": item.similarity,
            } for item in scored}
            reordered = [item.resource for item in scored]
            metadata["semantic_similarity"] = {
                "method": SIMILARITY_METHOD, "version": SIMILARITY_VERSION,
                "model_id": selected_model, "runtime_versions": runtime_versions(),
                "device": "cpu", "resource_text_max_chars": MAX_RESOURCE_TEXT_CHARS,
                "sorted_for_display": True,
                "scores": [{"position": item.original_position + 1,
                            "source_provider": item.resource.source_provider,
                            "external_id": item.resource.external_id,
                            "score": item.similarity} for item in scored],
            }
            scores = by_key
        else:
            from app.core.hybrid_ranking import (DEFAULT_WEIGHTS, FIELD_WEIGHTS, HYBRID_METHOD,
                                                 HYBRID_VERSION, RECENCY_HORIZON_YEARS,
                                                 rank_resources, validate_weights)
            weights = validate_weights(DEFAULT_WEIGHTS)
            ranked = rank_resources(topic, resources, backend, weights=weights,
                                     as_of_year=datetime.now(timezone.utc).year,
                                     sort_by_score=True)
            reordered = [item.resource for item in ranked]
            scores = {self._resource_key(item.resource): {
                "semantic": item.semantic_cosine, "keyword": item.keyword_component,
                "recency": item.metadata_component, "hybrid": item.hybrid_score,
            } for item in ranked}
            metadata["hybrid_ranking"] = {
                "method": HYBRID_METHOD, "version": HYBRID_VERSION,
                "model_id": selected_model, "weights": weights,
                "field_weights": FIELD_WEIGHTS, "device": "cpu",
                "as_of_year": datetime.now(timezone.utc).year,
                "recency_horizon_years": RECENCY_HORIZON_YEARS,
                "sorted_for_display": True,
                "scores": [{"original_position": item.original_position + 1,
                            "source_provider": item.resource.source_provider,
                            "external_id": item.resource.external_id,
                            "semantic_cosine": item.semantic_cosine,
                            "semantic_normalized": item.semantic_component,
                            "keyword": item.keyword_component,
                            "recency": item.metadata_component,
                            "hybrid": item.hybrid_score,
                            "effective_weights": item.effective_weights} for item in ranked],
            }
        with ResearchStore(self.db_path) as store:
            store.merge_search_metadata(search_id, {key: value for key, value in metadata.items()
                                                    if key in {"semantic_similarity", "hybrid_ranking"}})
        return reordered, metadata, scores

    @staticmethod
    def _resource_key(resource: ResearchResource) -> str:
        return f"{resource.source_provider}\0{resource.external_id or ''}\0{resource.title or ''}"

    def history(self) -> list[SearchSummary]:
        return list_history(self.db_path)

    def show(self, search_id: int) -> SearchResult:
        summaries = list_history(self.db_path)
        summary = next((item for item in summaries if item.id == search_id), None)
        if summary is None:
            raise ValueError(f"saved search ID {search_id} was not found")
        resources = get_history_resources(search_id, self.db_path)
        metadata = summary.metadata or {}
        scores: dict[str, dict[str, Any]] = {}
        ranking_info = metadata.get("hybrid_ranking") or metadata.get("semantic_similarity") or {}
        ranking_scores = ranking_info.get("scores", [])
        score_by_identity = {}
        position_field = "original_position" if "hybrid_ranking" in metadata else "position"
        for score in ranking_scores:
            identity = (score.get("source_provider"), score.get("external_id"))
            score_by_identity[identity] = score
        if score_by_identity:
            paired = []
            for index, resource in enumerate(resources, 1):
                identity = (resource.source_provider, resource.external_id)
                score = score_by_identity.get(identity, {})
                score_value = score.get("hybrid") if position_field == "original_position" else score.get("score")
                score_map = ({"semantic": score.get("semantic_cosine"),
                              "keyword": score.get("keyword"), "recency": score.get("recency"),
                              "hybrid": score.get("hybrid")} if position_field == "original_position"
                             else {"semantic": score.get("score")})
                scores[self._resource_key(resource)] = score_map
                paired.append((index, resource, score_value))
            if ranking_info.get("sorted_for_display"):
                paired.sort(key=lambda item: (item[2] is None,
                                              -(item[2] if isinstance(item[2], (int, float)) else 0),
                                              item[0]))
                resources = [resource for _, resource, _ in paired]
        return SearchResult(search_id, summary.topic, resources, metadata, scores)

    def export(self, search_id: int, output: str | Path, file_format: str) -> Path:
        result = self.show(search_id)
        from app.core.export import write_export
        return write_export(result.resources, output, file_format, search_metadata=result.metadata)

    def download(self, search_id: int, resource_number: int, file_number: int, *,
                 download_dir: str | Path | None = None, timeout: float = 15.0,
                 max_bytes: int = 100 * 1024 * 1024) -> Path:
        result = self.show(search_id)
        if not 1 <= resource_number <= len(result.resources):
            raise ValueError(f"result number must be from 1 to {len(result.resources)}")
        resource = result.resources[resource_number - 1]
        if not 1 <= file_number <= len(resource.downloadable_files):
            raise ValueError(f"file number must be from 1 to {len(resource.downloadable_files)}")
        if download_dir is None:
            from app.core.app_paths import default_download_dir
            download_dir = default_download_dir()
        from app.downloads import download_file
        return download_file(resource.downloadable_files[file_number - 1], resource,
                             download_dir=download_dir, timeout=timeout, max_bytes=max_bytes)
