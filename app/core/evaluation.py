"""Human-judgment evaluation metrics and portable annotation/report files.

Metrics operate on relevance grades 0..2. Unjudged results are omitted before
applying K; consequently K is counted in judged positions, and recall refers
only to the judged candidate pool. No relevance labels are inferred here.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
import io
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence


DATASET_VERSION = "research-relevance-v1"
EVALUATION_CODE_VERSION = "evaluation-v1"
RELEVANCE_LABELS = {0: "not relevant", 1: "partially relevant", 2: "highly relevant"}
JUDGMENT_FIELDS = (
    "dataset_version", "query_id", "topic", "search_id", "resource_id",
    "title", "abstract", "keywords", "resource_type", "source_provider",
    "source_links", "relevance", "annotator", "notes",
)


class EvaluationError(ValueError):
    """Invalid evaluation input, judgments, or report configuration."""


class MemoizingEmbeddingBackend:
    """Cache exact input strings so repeated ranking configurations reuse vectors."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self._vectors: dict[str, Sequence[float]] = {}

    def encode(self, texts: Sequence[str]) -> list[Sequence[float]]:
        missing = list(dict.fromkeys(text for text in texts if text not in self._vectors))
        if missing:
            vectors = self.backend.encode(missing)
            if len(vectors) != len(missing):
                raise EvaluationError("semantic backend returned an unexpected number of vectors")
            for text, vector in zip(missing, vectors):
                self._vectors[text] = vector.tolist() if hasattr(vector, "tolist") else vector
        return [self._vectors[text] for text in texts]


@dataclass(frozen=True)
class Judgment:
    query_id: str
    topic: str
    search_id: int
    resource_id: int
    title: str
    resource_type: str
    source_provider: str
    relevance: int | None
    annotator: str | None = None
    notes: str | None = None
    abstract: str = ""
    keywords: str = ""
    source_links: tuple[str, ...] = ()


def _validate_k(k: int) -> int:
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise EvaluationError("K must be a positive integer")
    return k


def _validate_threshold(threshold: int) -> int:
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold not in (1, 2):
        raise EvaluationError("relevant_threshold must be 1 or 2")
    return threshold


def _validate_grades(values: Sequence[int | None]) -> list[int | None]:
    grades: list[int | None] = []
    for value in values:
        if value is None:
            grades.append(None)
        elif isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1, 2):
            raise EvaluationError("relevance grades must be 0, 1, 2, or None for unjudged")
        else:
            grades.append(value)
    return grades


def _judged(values: Sequence[int | None], judged_only: bool = True) -> list[int]:
    grades = _validate_grades(values)
    if judged_only:
        return [grade for grade in grades if grade is not None]
    if any(grade is None for grade in grades):
        raise EvaluationError("unjudged resources must be omitted or judged_only must be true")
    return [int(grade) for grade in grades]


def precision_at_k(
    relevances: Sequence[int | None], k: int, *, relevant_threshold: int = 2,
    judged_only: bool = True,
) -> float:
    """Binary precision; K is the denominator even with fewer judged results."""
    k = _validate_k(k)
    threshold = _validate_threshold(relevant_threshold)
    values = _judged(relevances, judged_only)[:k]
    return sum(value >= threshold for value in values) / k


def recall_at_k(
    relevances: Sequence[int | None], k: int, *, relevant_threshold: int = 2,
    judged_only: bool = True,
) -> float | None:
    """Recall against all known relevant judgments in this query's judged pool."""
    k = _validate_k(k)
    threshold = _validate_threshold(relevant_threshold)
    all_values = _judged(relevances, judged_only)
    relevant_total = sum(value >= threshold for value in all_values)
    if relevant_total == 0:
        return None
    return sum(value >= threshold for value in all_values[:k]) / relevant_total


def reciprocal_rank(
    relevances: Sequence[int | None], *, relevant_threshold: int = 2,
    judged_only: bool = True,
) -> float | None:
    """Reciprocal position of the first threshold-relevant judged resource."""
    threshold = _validate_threshold(relevant_threshold)
    for position, value in enumerate(_judged(relevances, judged_only), start=1):
        if value >= threshold:
            return 1.0 / position
    return None


def mean_reciprocal_rank(
    query_relevances: Sequence[Sequence[int | None]], *, relevant_threshold: int = 2,
    judged_only: bool = True,
) -> float:
    """Mean RR over queries that have at least one threshold-relevant judgment."""
    values = [reciprocal_rank(row, relevant_threshold=relevant_threshold,
                              judged_only=judged_only) for row in query_relevances]
    defined = [value for value in values if value is not None]
    return math.fsum(defined) / len(defined) if defined else 0.0


def _dcg(values: Sequence[int], k: int) -> float:
    return math.fsum((2**grade - 1) / math.log2(position + 1)
                     for position, grade in enumerate(values[:k], start=1))


def dcg_at_k(relevances: Sequence[int | None], k: int, *, judged_only: bool = True) -> float:
    """Discounted cumulative gain using gain 2**grade - 1."""
    k = _validate_k(k)
    return _dcg(_judged(relevances, judged_only), k)


def ndcg_at_k(relevances: Sequence[int | None], k: int, *, judged_only: bool = True) -> float | None:
    """NDCG in [0,1]; None when the judged query has no positive gain."""
    k = _validate_k(k)
    values = _judged(relevances, judged_only)
    actual = _dcg(values, k)
    ideal = _dcg(sorted(values, reverse=True), k)
    return actual / ideal if ideal else None


def evaluate_query(
    rankings: Mapping[str, Sequence[str]], judgments: Mapping[str, int | None],
    k_values: Sequence[int], *, relevant_threshold: int = 2,
) -> dict[str, dict[str, float | None]]:
    """Score ordered resource IDs for each method, omitting unjudged IDs before K."""
    threshold = _validate_threshold(relevant_threshold)
    ks = tuple(dict.fromkeys(_validate_k(value) for value in k_values))
    if not ks:
        raise EvaluationError("at least one K value is required")
    grades = _validate_grades(list(judgments.values()))
    if len(set(judgments)) != len(judgments):
        raise EvaluationError("duplicate judgment resource IDs")
    result: dict[str, dict[str, float | None]] = {}
    for method, ordered_ids in rankings.items():
        if len(set(ordered_ids)) != len(ordered_ids):
            raise EvaluationError(f"duplicate resource ID in {method} ranking")
        values = [judgments[item] for item in ordered_ids if item in judgments
                  and judgments[item] is not None]
        scores: dict[str, float | None] = {}
        for k in ks:
            scores[f"precision@{k}"] = precision_at_k(
                values, k, relevant_threshold=threshold)
            scores[f"recall@{k}"] = recall_at_k(
                values, k, relevant_threshold=threshold)
            scores[f"ndcg@{k}"] = ndcg_at_k(values, k)
        scores["reciprocal_rank"] = reciprocal_rank(values, relevant_threshold=threshold)
        result[method] = scores
    return result


def aggregate_query_metrics(
    query_metrics: Sequence[Mapping[str, Mapping[str, float | None]]],
) -> dict[str, dict[str, float | None]]:
    """Macro-average over queries with at least one threshold-relevant judged item."""
    methods = sorted({method for row in query_metrics for method in row})
    aggregate: dict[str, dict[str, float | None]] = {}
    for method in methods:
        keys = sorted({key for row in query_metrics for key in row.get(method, {})})
        aggregate[method] = {}
        for key in keys:
            values = [row[method][key] for row in query_metrics
                      if method in row
                      and row[method].get("reciprocal_rank") is not None
                      and row[method].get(key) is not None]
            aggregate_key = "mrr" if key == "reciprocal_rank" else key
            aggregate[method][aggregate_key] = (
                math.fsum(float(value) for value in values) / len(values) if values else None)
    return aggregate


def candidate_rows(searches: Sequence[tuple[int, str, Sequence[Any]]]) -> list[dict[str, str]]:
    """Build an annotation template; IDs are stable local SQLite resource IDs."""
    rows: list[dict[str, str]] = []
    for search_id, topic, pairs in searches:
        query_id = f"search-{search_id}"
        for resource_id, resource in pairs:
            rows.append({
                "dataset_version": DATASET_VERSION, "query_id": query_id, "topic": topic,
                "search_id": str(search_id), "resource_id": str(resource_id),
                "title": resource.title or "", "abstract": resource.description or "",
                "keywords": "; ".join(resource.keywords or ()),
                "resource_type": resource.resource_type.value,
                "source_provider": resource.source_provider,
                "source_links": json.dumps([link.url for link in resource.links], ensure_ascii=False),
                "relevance": "", "annotator": "", "notes": "",
            })
    return rows


def write_judgment_template(rows: Sequence[Mapping[str, str]], path: str | Path) -> Path:
    target = Path(path)
    if target.exists():
        raise EvaluationError(f"output file already exists: {target}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=JUDGMENT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    except OSError as exc:
        raise EvaluationError(f"could not write annotation template: {exc}") from exc
    return target


def load_judgments(path: str | Path) -> tuple[str, list[Judgment]]:
    """Load CSV or JSON annotation rows, preserving blank relevance as unjudged."""
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise EvaluationError(f"could not read judgments: {exc}") from exc
    try:
        if target.suffix.casefold() == ".json":
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
                raise EvaluationError("JSON judgments must contain a rows array")
            version = payload.get("dataset_version")
            records = payload["rows"]
        else:
            records = list(csv.DictReader(io.StringIO(raw, newline="")))
            versions = {row.get("dataset_version") for row in records}
            version = next(iter(versions)) if len(versions) == 1 else None
    except (json.JSONDecodeError, csv.Error, TypeError) as exc:
        raise EvaluationError(f"invalid judgments file: {exc}") from exc
    if version != DATASET_VERSION:
        raise EvaluationError(f"dataset_version must be {DATASET_VERSION}")
    rows: list[Judgment] = []
    seen: set[tuple[str, int]] = set()
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise EvaluationError(f"judgment row {index} must be an object")
        try:
            query_id = str(record["query_id"]).strip()
            topic = str(record["topic"]).strip()
            raw_search_id = record["search_id"]
            raw_resource_id = record["resource_id"]
            if isinstance(raw_search_id, bool) or isinstance(raw_resource_id, bool):
                raise ValueError("IDs must be integers")
            if isinstance(raw_search_id, float) and not raw_search_id.is_integer():
                raise ValueError("search_id must be an integer")
            if isinstance(raw_resource_id, float) and not raw_resource_id.is_integer():
                raise ValueError("resource_id must be an integer")
            search_id = int(raw_search_id)
            resource_id = int(raw_resource_id)
            relevance_raw = record.get("relevance")
            if isinstance(relevance_raw, bool):
                raise ValueError("relevance must be an integer grade")
            if isinstance(relevance_raw, float) and not relevance_raw.is_integer():
                raise ValueError("relevance must be an integer grade")
            relevance = None if relevance_raw is None or str(relevance_raw).strip() == "" else int(relevance_raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise EvaluationError(f"judgment row {index} has invalid identifiers or label") from exc
        if not query_id or not topic or search_id < 1 or resource_id < 1:
            raise EvaluationError(f"judgment row {index} requires query/topic and positive IDs")
        if relevance is not None and (isinstance(relevance_raw, bool) or relevance not in (0, 1, 2)):
            raise EvaluationError(f"judgment row {index} relevance must be blank, 0, 1, or 2")
        annotator = str(record.get("annotator") or "").strip() or None
        if relevance is not None and annotator is None:
            raise EvaluationError(f"judgment row {index} with a label requires annotator")
        source_links_raw = record.get("source_links") or "[]"
        try:
            source_links = json.loads(source_links_raw) if isinstance(source_links_raw, str) else source_links_raw
        except json.JSONDecodeError as exc:
            raise EvaluationError(f"judgment row {index} source_links must be a JSON array") from exc
        if not isinstance(source_links, list) or any(not isinstance(url, str) for url in source_links):
            raise EvaluationError(f"judgment row {index} source_links must be a JSON array of strings")
        key = (query_id, resource_id)
        if key in seen:
            raise EvaluationError(f"duplicate resource judgment for {query_id}/{resource_id}")
        seen.add(key)
        rows.append(Judgment(query_id, topic, search_id, resource_id,
                             str(record.get("title") or ""),
                             str(record.get("resource_type") or ""),
                             str(record.get("source_provider") or ""), relevance,
                             annotator, str(record.get("notes") or "") or None,
                             str(record.get("abstract") or ""),
                             str(record.get("keywords") or ""), tuple(source_links)))
    if not rows:
        raise EvaluationError("judgments file contains no candidate rows")
    return str(version), rows


def write_report(report: Mapping[str, Any], path: str | Path, file_format: str) -> Path:
    target = Path(path)
    if file_format not in ("json", "csv"):
        raise EvaluationError("report format must be csv or json")
    if target.exists():
        raise EvaluationError(f"output file already exists: {target}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if file_format == "json":
            with target.open("x", encoding="utf-8", newline="") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
        else:
            with target.open("x", encoding="utf-8-sig", newline="") as stream:
                ranking_config = report.get("ranking_configuration", {})
                semantic = ranking_config.get("semantic", {})
                metadata = {
                    "dataset_version": report.get("dataset_version"),
                    "evaluation_code_version": report.get("evaluation_code_version"),
                    "evaluation_date": report.get("evaluation_date"),
                    "python_version": report.get("python_version"),
                    "query_count": report.get("query_count"),
                    "eligible_query_count": report.get("eligible_query_count"),
                    "judged_resource_count": report.get("judged_resource_count"),
                    "relevant_threshold": report.get("relevant_threshold"),
                    "model_id": semantic.get("model_id"),
                    "model_revision": semantic.get("model_revision"),
                    "ranking_configuration": json.dumps(ranking_config, ensure_ascii=False,
                                                         sort_keys=True),
                    "k_values": json.dumps(report.get("k_values", [])),
                    "limitations": json.dumps(report.get("limitations", []), ensure_ascii=False),
                    "judgments_sha256": report.get("judgments_sha256"),
                    "candidate_snapshot_sha256": report.get("candidate_snapshot_sha256"),
                    "saved_search_provenance": json.dumps(
                        report.get("saved_search_provenance", []), ensure_ascii=False,
                        sort_keys=True),
                }
                writer = csv.DictWriter(stream, fieldnames=(
                    "query_id", "method", "metric", "score", "dataset_version",
                    "evaluation_code_version", "evaluation_date", "python_version",
                    "query_count", "eligible_query_count", "judged_resource_count",
                    "relevant_threshold",
                    "model_id", "model_revision", "ranking_configuration", "k_values",
                    "limitations", "judgments_sha256", "candidate_snapshot_sha256",
                    "saved_search_provenance",
                ))
                writer.writeheader()
                for query in report.get("queries", []):
                    for method, metrics in query.get("methods", {}).items():
                        for metric, score in metrics.items():
                            writer.writerow({**metadata, "query_id": query.get("query_id"),
                                             "method": method, "metric": metric, "score": score})
                for method, metrics in report.get("aggregate", {}).items():
                    for metric, score in metrics.items():
                        writer.writerow({**metadata, "query_id": "AGGREGATE", "method": method,
                                         "metric": metric, "score": score})
    except OSError as exc:
        raise EvaluationError(f"could not write evaluation report: {exc}") from exc
    return target
