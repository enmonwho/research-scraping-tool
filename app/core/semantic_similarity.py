"""Optional semantic similarity scoring for normalized research resources.

The service has no import-time dependency on PyTorch or sentence-transformers.
It accepts an injectable encoder so callers can use another compatible backend.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from importlib.metadata import PackageNotFoundError, version
import math
import os
from pathlib import Path
import re
from typing import Protocol, Sequence

from app.models import ResearchResource
from app.core.topic import validate_topic


DEFAULT_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
SIMILARITY_METHOD = "cosine_similarity"
SIMILARITY_VERSION = "semantic-similarity-v1"
MAX_RESOURCE_TEXT_CHARS = 5_000


class EmbeddingBackend(Protocol):
    """Backend that maps a batch of strings to same-width numeric vectors."""

    def encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class SemanticSimilarityError(RuntimeError):
    """An embedding backend failed or returned invalid output."""


@dataclass(frozen=True)
class ScoredResource:
    """A resource and its derived score, without modifying the resource."""

    resource: ResearchResource
    similarity: float | None
    original_position: int


def resource_text(resource: ResearchResource, *, max_chars: int = MAX_RESOURCE_TEXT_CHARS) -> str:
    """Build bounded text from title, description, and keywords only.

    Each field is included once, with its label. Authors, DOI, IDs, and URLs
    are intentionally excluded. The result is capped at ``max_chars``.
    """
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    parts: list[str] = []
    fields = (
        ("Title", resource.title),
        ("Description", resource.description),
        ("Keywords", "; ".join(resource.keywords or ())),
    )
    seen: set[str] = set()
    for label, value in fields:
        if not isinstance(value, str):
            continue
        value = " ".join(value.split())
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        parts.append(f"{label}: {value}")
    return "\n".join(parts)[:max_chars]


class SentenceTransformerBackend:
    """Lazily load and reuse one CPU sentence-transformer instance."""

    def __init__(self, model_id: str = DEFAULT_MODEL_ID, *, cache_dir: str | None = None) -> None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must be a non-empty string")
        self.model_id = model_id.strip()
        self.cache_dir = cache_dir
        self._model = None

    @property
    def model_revision(self) -> str | None:
        """Return the resolved Hugging Face cache commit when it is discoverable."""
        if self._model is None or Path(self.model_id).is_dir():
            return None
        if self.cache_dir:
            cache_root = Path(self.cache_dir)
        elif os.environ.get("HF_HUB_CACHE"):
            cache_root = Path(os.environ["HF_HUB_CACHE"])
        elif os.environ.get("HF_HOME"):
            cache_root = Path(os.environ["HF_HOME"]) / "hub"
        else:
            cache_root = Path.home() / ".cache" / "huggingface" / "hub"
        model_folder = "models--" + self.model_id.strip("/").replace("/", "--")
        ref = cache_root / model_folder / "refs" / "main"
        try:
            revision = ref.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if re.fullmatch(r"[0-9a-f]{40}", revision) and (
            cache_root / model_folder / "snapshots" / revision
        ).is_dir():
            return revision
        return None

    def _get_model(self):
        if self._model is None:
            try:
                module = importlib.import_module("sentence_transformers")
                self._model = module.SentenceTransformer(
                    self.model_id, device="cpu", cache_folder=self.cache_dir
                )
            except Exception as exc:
                raise SemanticSimilarityError(
                    "could not load the semantic model; install the 'semantic' extra and "
                    "check model-cache/network availability"
                ) from exc
        return self._model

    def encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        try:
            result = self._get_model().encode(list(texts), convert_to_numpy=True,
                                              show_progress_bar=False)
            return result.tolist() if hasattr(result, "tolist") else result
        except SemanticSimilarityError:
            raise
        except Exception as exc:
            raise SemanticSimilarityError(f"semantic model inference failed: {exc}") from exc


def _validated_vectors(vectors: Sequence[Sequence[float]], expected: int) -> list[list[float]]:
    if hasattr(vectors, "tolist"):
        vectors = vectors.tolist()
    try:
        materialized = list(vectors)
    except (TypeError, ValueError) as exc:
        raise SemanticSimilarityError("embedding backend returned invalid vectors") from exc
    if len(materialized) != expected:
        raise SemanticSimilarityError(
            f"embedding backend returned {len(materialized)} vectors for {expected} texts"
        )
    rows: list[list[float]] = []
    width = None
    for vector in materialized:
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        try:
            row = [float(value) for value in vector]
        except (TypeError, ValueError, OverflowError) as exc:
            raise SemanticSimilarityError("embedding backend returned a non-numeric vector") from exc
        if not row or any(not math.isfinite(value) for value in row):
            raise SemanticSimilarityError("embedding backend returned an empty or non-finite vector")
        if width is None:
            width = len(row)
        elif len(row) != width:
            raise SemanticSimilarityError("embedding backend returned vectors with inconsistent dimensions")
        if not any(value != 0 for value in row):
            raise SemanticSimilarityError("embedding backend returned a zero-magnitude vector")
        rows.append(row)
    return rows


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return cosine similarity in [-1, 1] for finite, nonzero vectors."""
    a = _validated_vectors([left], 1)[0]
    b = _validated_vectors([right], 1)[0]
    if len(a) != len(b):
        raise SemanticSimilarityError("embedding vectors have inconsistent dimensions")
    def unit(vector: list[float]) -> list[float]:
        scale = max(abs(value) for value in vector)
        scaled = [value / scale for value in vector]
        norm = math.sqrt(math.fsum(value * value for value in scaled))
        return [value / norm for value in scaled]

    unit_a, unit_b = unit(a), unit(b)
    dot = math.fsum(x * y for x, y in zip(unit_a, unit_b))
    return max(-1.0, min(1.0, dot))


def runtime_versions() -> dict[str, str | None]:
    """Report installed inference package versions without importing them."""
    result: dict[str, str | None] = {}
    for package in ("sentence-transformers", "transformers", "torch", "numpy"):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = None
    return result


def score_resources(
    topic: str,
    resources: Sequence[ResearchResource],
    backend: EmbeddingBackend,
    *,
    sort_by_similarity: bool = False,
) -> list[ScoredResource]:
    """Score resources while retaining input order unless sorting is requested."""
    topic = validate_topic(topic)
    if not resources:
        return []
    texts = [resource_text(resource) for resource in resources]
    available = [(index, text) for index, text in enumerate(texts) if text]
    if not available:
        scores = [ScoredResource(item, None, i) for i, item in enumerate(resources)]
    else:
        inputs = [topic, *(text for _, text in available)]
        try:
            vectors = _validated_vectors(backend.encode(inputs), len(inputs))
        except SemanticSimilarityError:
            raise
        except Exception as exc:
            raise SemanticSimilarityError(f"semantic model inference failed: {exc}") from exc
        computed = {index: cosine_similarity(vectors[0], vectors[offset + 1])
                    for offset, (index, _) in enumerate(available)}
        scores = [ScoredResource(item, computed.get(i), i)
                  for i, item in enumerate(resources)]
    if sort_by_similarity:
        scores.sort(key=lambda item: (item.similarity is None,
                                      -(item.similarity if item.similarity is not None else 0.0),
                                      item.original_position))
    return scores
