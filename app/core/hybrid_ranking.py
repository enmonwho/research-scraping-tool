"""Explainable, deterministic hybrid scoring for normalized research resources."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from types import MappingProxyType
import unicodedata

from app.core.semantic_similarity import (
    EmbeddingBackend,
    ScoredResource,
    SemanticSimilarityError,
    score_resources,
)
from app.core.topic import validate_topic
from app.models import ResearchResource


HYBRID_METHOD = "weighted_semantic_keyword_metadata"
HYBRID_VERSION = "hybrid-ranking-v1"
RECENCY_HORIZON_YEARS = 30
FIELD_WEIGHTS = {"title": 0.50, "description": 0.30, "keywords": 0.20}
DEFAULT_WEIGHTS = MappingProxyType({"semantic": 0.60, "keyword": 0.40, "recency": 0.00})
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "into", "is", "of", "on", "or", "the", "to", "using", "via", "with",
})
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


class HybridRankingError(RuntimeError):
    """Hybrid ranking failed because its configuration or semantic backend is invalid."""


@dataclass(frozen=True)
class HybridScoredResource:
    """A resource with separate raw and normalized components; resource is unchanged."""

    resource: ResearchResource
    semantic_cosine: float | None
    semantic_component: float | None
    keyword_component: float
    metadata_component: float
    hybrid_score: float | None
    effective_weights: dict[str, float]
    original_position: int


def validate_weights(
    weights: Mapping[str, float] | Sequence[float] = DEFAULT_WEIGHTS,
) -> dict[str, float]:
    """Return validated semantic/keyword/recency weights summing to one."""
    names = ("semantic", "keyword", "recency")
    if isinstance(weights, Mapping):
        if set(weights) != set(names):
            raise HybridRankingError("weights must define semantic, keyword, and recency")
        raw = [weights[name] for name in names]
    else:
        if isinstance(weights, (str, bytes)):
            raise HybridRankingError("weights must be a mapping or three-number sequence")
        try:
            raw = list(weights)
        except TypeError as exc:
            raise HybridRankingError("weights must be a mapping or three-number sequence") from exc
        if len(raw) != 3:
            raise HybridRankingError("weights must contain semantic, keyword, and recency values")
    values: list[float] = []
    for value in raw:
        if isinstance(value, bool):
            raise HybridRankingError("weights must be finite nonnegative numbers")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise HybridRankingError("weights must be finite nonnegative numbers") from exc
        if not math.isfinite(number) or number < 0:
            raise HybridRankingError("weights must be finite nonnegative numbers")
        values.append(number)
    try:
        total = math.fsum(values)
    except OverflowError as exc:
        raise HybridRankingError("hybrid weights must sum to 1") from exc
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise HybridRankingError("hybrid weights must sum to 1")
    return dict(zip(names, values))


def tokenize(text: str) -> list[str]:
    """Casefold and split Unicode text at punctuation/whitespace; remove English stopwords."""
    if not isinstance(text, str):
        return []
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return [token for token in _TOKEN.findall(normalized) if token not in STOPWORDS]


def keyword_score(topic: str, resource: ResearchResource) -> float:
    """Topic-token coverage across title/description/keywords, normalized to [0, 1].

    Per-field coverage is weighted 0.50/0.30/0.20. Available text fields are
    renormalized so missing abstracts or keywords do not lower scores by default.
    """
    query_terms = set(tokenize(topic))
    if not query_terms:
        return 0.0
    fields = (
        ("title", resource.title),
        ("description", resource.description),
        ("keywords", " ".join(resource.keywords or ())),
    )
    present: list[tuple[float, float]] = []
    for name, value in fields:
        terms = set(tokenize(value or ""))
        if terms:
            coverage = len(query_terms & terms) / len(query_terms)
            present.append((FIELD_WEIGHTS[name], coverage))
    total_weight = math.fsum(weight for weight, _ in present)
    if not total_weight:
        return 0.0
    score = math.fsum(weight * coverage for weight, coverage in present) / total_weight
    return max(0.0, min(1.0, score))


def recency_score(
    publication_year: int | None,
    *,
    as_of_year: int,
    horizon_years: int = RECENCY_HORIZON_YEARS,
) -> float:
    """Return bounded recency in [0,1]; unknown years receive a neutral 0.5."""
    if isinstance(as_of_year, bool) or not isinstance(as_of_year, int):
        raise HybridRankingError("as_of_year must be an integer")
    if (isinstance(horizon_years, bool) or not isinstance(horizon_years, int)
            or horizon_years < 1):
        raise HybridRankingError("recency horizon must be a positive integer")
    if isinstance(publication_year, bool) or not isinstance(publication_year, int):
        return 0.5
    age = max(0, as_of_year - publication_year)
    return 0.0 if age >= horizon_years else 1.0 - age / horizon_years


def _hybrid_score(
    semantic: float | None,
    keyword: float,
    metadata: float,
    weights: Mapping[str, float],
) -> tuple[float | None, dict[str, float]]:
    components = {"semantic": semantic, "keyword": keyword, "recency": metadata}
    used = {name: weight for name, weight in weights.items()
            if weight > 0 and components[name] is not None}
    weight_total = math.fsum(used.values())
    if weight_total == 0:
        return None, {}
    effective = {name: weight / weight_total for name, weight in used.items()}
    final = math.fsum(effective[name] * components[name] for name in effective)
    return max(0.0, min(1.0, final)), effective


def rank_resources(
    topic: str,
    resources: Sequence[ResearchResource],
    semantic_backend: EmbeddingBackend | None,
    *,
    weights: Mapping[str, float] | Sequence[float] = DEFAULT_WEIGHTS,
    as_of_year: int | None = None,
    horizon_years: int = RECENCY_HORIZON_YEARS,
    sort_by_score: bool = False,
) -> list[HybridScoredResource]:
    """Compute components once, keeping input order unless explicit sorting is set.

    Missing semantic text is omitted from that resource's weighted sum, and
    the remaining nonzero weights are renormalized. Missing years use neutral
    recency 0.5. With no available nonzero components the final score is None.
    """
    topic = validate_topic(topic)
    normalized_weights = validate_weights(weights)
    if not isinstance(sort_by_score, bool):
        raise HybridRankingError("sort_by_score must be a boolean")
    as_of_year = as_of_year if as_of_year is not None else datetime.now(timezone.utc).year
    recency_score(None, as_of_year=as_of_year, horizon_years=horizon_years)
    if not resources:
        return []
    semantic_results: list[ScoredResource | None]
    if normalized_weights["semantic"] > 0:
        if semantic_backend is None:
            raise HybridRankingError("a semantic backend is required by the configured weights")
        try:
            semantic_scores = score_resources(topic, resources, semantic_backend)
        except SemanticSimilarityError as exc:
            raise HybridRankingError(str(exc)) from exc
        semantic_results = list(semantic_scores)
    else:
        semantic_results = [None] * len(resources)
    ranked: list[HybridScoredResource] = []
    for position, resource in enumerate(resources):
        semantic_cosine = (semantic_results[position].similarity
                           if semantic_results[position] is not None else None)
        semantic_component = (None if semantic_cosine is None
                              else max(0.0, min(1.0, (semantic_cosine + 1.0) / 2.0)))
        lexical = keyword_score(topic, resource)
        metadata = recency_score(resource.publication_year, as_of_year=as_of_year,
                                 horizon_years=horizon_years)
        final, effective = _hybrid_score(semantic_component, lexical, metadata,
                                         normalized_weights)
        ranked.append(HybridScoredResource(resource, semantic_cosine, semantic_component,
                                           lexical, metadata, final, effective, position))
    if sort_by_score:
        ranked.sort(key=lambda item: (item.hybrid_score is None,
                                      -(item.hybrid_score if item.hybrid_score is not None else 0.0),
                                      item.original_position))
    return ranked
