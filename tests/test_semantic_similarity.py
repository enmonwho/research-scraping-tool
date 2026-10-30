"""Deterministic coverage for optional semantic similarity scoring."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import math
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from app.cli.main import main
from app.core.semantic_similarity import (
    SemanticSimilarityError,
    SentenceTransformerBackend,
    cosine_similarity,
    resource_text,
    score_resources,
)
from app.models import ResearchResource, ResourceType
from app.storage import ResearchStore


def resource(
    title: str | None = "A research title",
    *,
    description: str | None = "A useful description",
    authors: tuple[str, ...] | None = ("Private Author",),
    doi: str | None = "10.1234/private-doi",
    keywords: tuple[str, ...] | None = ("hydrology", "forecasting"),
    resource_type: ResourceType = ResourceType.PAPER,
    external_id: str | None = "https://provider.example/resource/1",
) -> ResearchResource:
    return ResearchResource(
        resource_type=resource_type,
        title=title,
        description=description,
        authors=authors,
        publication_year=2025,
        source_provider="Fixture provider",
        external_id=external_id,
        doi=doi,
        keywords=keywords,
        retrieved_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


class FakeBackend:
    def __init__(self, vectors=None, error: Exception | None = None):
        self.vectors = vectors
        self.error = error
        self.calls: list[list[str]] = []

    def encode(self, texts):
        self.calls.append(list(texts))
        if self.error:
            raise self.error
        if callable(self.vectors):
            return self.vectors(texts)
        return self.vectors


class SemanticSimilarityTests(unittest.TestCase):
    def test_resource_text_uses_semantic_fields_only_and_deduplicates_repetition(self):
        item = resource(
            title="Flood modeling",
            description="FLOOD   modeling",
            authors=("SECRET AUTHOR",),
            doi="10.9999/SECRET-DOI",
            external_id="https://private.example/path",
            keywords=("flood modeling", "rainfall"),
        )
        text = resource_text(item)
        self.assertIn("Title: Flood modeling", text)
        self.assertIn("Keywords: flood modeling; rainfall", text)
        self.assertNotIn("Description:", text)
        for excluded in ("SECRET AUTHOR", "SECRET-DOI", "private.example"):
            self.assertNotIn(excluded, text)

    def test_resource_text_is_bounded_and_rejects_invalid_limit(self):
        item = resource(title="x" * 300, description="y" * 400)
        self.assertEqual(len(resource_text(item, max_chars=31)), 31)
        for invalid in (0, -1, True, 2.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                resource_text(item, max_chars=invalid)

    def test_cosine_similarity_calculations(self):
        self.assertEqual(cosine_similarity([1, 0], [1, 0]), 1.0)
        self.assertEqual(cosine_similarity([1, 0], [-1, 0]), -1.0)
        self.assertAlmostEqual(cosine_similarity([1, 0], [1, 1]), math.sqrt(0.5))

    def test_empty_resources_do_not_initialize_backend(self):
        backend = FakeBackend(error=AssertionError("must not encode"))
        self.assertEqual(score_resources("topic", [], backend), [])
        self.assertEqual(backend.calls, [])

    def test_missing_resource_text_is_returned_without_a_score(self):
        item = resource(title=None, description=None, keywords=None)
        backend = FakeBackend(error=AssertionError("must not encode"))
        scored = score_resources("topic", [item], backend)
        self.assertEqual(scored[0].resource, item)
        self.assertIsNone(scored[0].similarity)
        self.assertEqual(scored[0].original_position, 0)
        self.assertEqual(backend.calls, [])

    def test_partial_missing_fields_keep_other_available_text(self):
        items = [
            resource(title=None, description="Description remains", keywords=None),
            resource(title="Keyword-only title", description=None, keywords=("rainfall",)),
        ]
        backend = FakeBackend([[1, 0], [1, 0], [0, 1]])
        results = score_resources("topic", items, backend)
        self.assertEqual(len(results), 2)
        self.assertIn("Description remains", backend.calls[0][1])
        self.assertIn("Keyword-only title", backend.calls[0][2])
        self.assertIn("rainfall", backend.calls[0][2])

    def test_blank_or_malformed_topics_are_rejected(self):
        for topic in ("", "  \t", "x" * 201):
            with self.subTest(topic=topic[:10]), self.assertRaises(ValueError):
                score_resources(topic, [resource()], FakeBackend([[1, 0], [1, 0]]))

    def test_topic_and_resource_are_embedded_as_one_batch(self):
        item = resource()
        backend = FakeBackend([[1, 0], [0, 1]])
        result = score_resources("flood prediction", [item], backend)
        self.assertEqual(backend.calls, [["flood prediction", resource_text(item)]])
        self.assertEqual(result[0].similarity, 0.0)
        self.assertIs(result[0].resource, item)

    def test_dataset_text_is_supported_and_unicode_is_preserved(self):
        unicode_title = "Donn\u00e9es hydrologiques \u96e8\u91cf"
        unicode_topic = "pluie \u96e8\u91cf"
        dataset = resource(
            title=unicode_title,
            description="Observations de pluie",
            resource_type=ResourceType.DATASET,
        )
        backend = FakeBackend([[1, 0], [1, 0]])
        result = score_resources(unicode_topic, [dataset], backend)
        self.assertEqual(result[0].similarity, 1.0)
        self.assertIn(unicode_title, backend.calls[0][1])

    def test_default_order_is_preserved_and_sorting_is_stable_on_ties(self):
        resources = [resource("first"), resource("second"), resource("third")]
        vectors = [[1, 0], [-1, 0], [0.5, math.sqrt(0.75)], [0.5, math.sqrt(0.75)]]
        backend = FakeBackend(vectors)
        original_order = score_resources("topic", resources, backend)
        self.assertEqual([item.resource.title for item in original_order], ["first", "second", "third"])
        sorted_order = score_resources(
            "topic", resources,
            FakeBackend(vectors),
            sort_by_similarity=True,
        )
        self.assertEqual([item.resource.title for item in sorted_order], ["second", "third", "first"])
        self.assertEqual([item.original_position for item in sorted_order], [1, 2, 0])

    def test_invalid_embedding_outputs_raise_clear_similarity_error(self):
        item = resource()
        invalid_results = (
            [[1, 0], [float("nan"), 0]],
            [[1, 0], [0, 0]],
            [[1, 0]],
            [[1, 0], [1, 2, 3]],
            [[1, 0], ["not-a-number", 0]],
        )
        for vectors in invalid_results:
            with self.subTest(vectors=vectors), self.assertRaises(SemanticSimilarityError):
                score_resources("topic", [item], FakeBackend(vectors))
        for left, right in (([float("inf")], [1]), ([0], [0]), ([1, 0], [1])):
            with self.subTest(left=left, right=right), self.assertRaises(SemanticSimilarityError):
                cosine_similarity(left, right)

    def test_backend_inference_errors_are_wrapped(self):
        with self.assertRaisesRegex(SemanticSimilarityError, "inference failed"):
            score_resources("topic", [resource()], FakeBackend(error=RuntimeError("offline")))

    def test_sentence_transformer_backend_loads_lazily_and_reuses_model(self):
        model = unittest.mock.Mock()
        model.encode.return_value = [[0.1, 0.2]]
        module = types.SimpleNamespace(SentenceTransformer=unittest.mock.Mock(return_value=model))
        backend = SentenceTransformerBackend("custom/model", cache_dir="model-cache")
        with patch("app.core.semantic_similarity.importlib.import_module", return_value=module) as loader:
            self.assertEqual(loader.call_count, 0)
            self.assertEqual(backend.encode(["first"]), [[0.1, 0.2]])
            self.assertEqual(backend.encode(["second"]), [[0.1, 0.2]])
        module.SentenceTransformer.assert_called_once_with(
            "custom/model", device="cpu", cache_folder="model-cache"
        )
        self.assertEqual(model.encode.call_count, 2)

    def test_missing_sentence_transformers_dependency_is_reported(self):
        backend = SentenceTransformerBackend()
        with patch("app.core.semantic_similarity.importlib.import_module", side_effect=ImportError("missing")):
            with self.assertRaisesRegex(SemanticSimilarityError, "install the 'semantic' extra"):
                backend.encode(["topic"])


class SemanticCliTests(unittest.TestCase):
    def test_normal_cli_search_does_not_load_ml_dependencies(self):
        output = io.StringIO()
        with (
            patch("app.cli.main.search_and_save", return_value=[resource()]),
            patch("app.core.semantic_similarity.importlib.import_module",
                  side_effect=AssertionError("ML dependency should remain unloaded")) as loader,
            redirect_stdout(output),
        ):
            code = main(["search", "topic", "--db", "unused.sqlite"])
        self.assertEqual(code, 0)
        self.assertEqual(loader.call_count, 0)
        self.assertNotIn("Semantic similarity", output.getvalue())

    def test_optional_cli_mode_displays_scores_and_persists_model_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "history.sqlite"
            paper = resource("Scored paper")
            with ResearchStore(db_path) as store:
                search_id = store.save_search("topic", [paper], metadata={"status": "complete"})
            fake_backend = FakeBackend([[1, 0], [0.6, 0.8]])
            output = io.StringIO()
            with (
                patch("app.cli.main.search_and_save_with_id", return_value=([paper], search_id)),
                patch("app.cli.main.SentenceTransformerBackend", return_value=fake_backend) as make_backend,
                redirect_stdout(output),
            ):
                code = main(["search", "topic", "--semantic", "--model", "fixture/model",
                             "--db", str(db_path)])
            self.assertEqual(code, 0)
            make_backend.assert_called_once_with("fixture/model", cache_dir=None)
            self.assertIn("Semantic similarity (cosine): 0.6", output.getvalue())
            with ResearchStore(db_path) as store:
                metadata = next(item for item in store.list_searches() if item.id == search_id).metadata
            derived = metadata["semantic_similarity"]
            self.assertEqual(derived["model_id"], "fixture/model")
            self.assertEqual(derived["method"], "cosine_similarity")
            self.assertAlmostEqual(derived["scores"][0]["score"], 0.6)

    def test_semantic_sort_changes_display_order_when_requested(self):
        papers = [resource("lower score"), resource("higher score")]
        vectors = [[1, 0], [-1, 0], [1, 0]]
        output = io.StringIO()
        with (
            patch("app.cli.main.search_and_save_with_id", return_value=(papers, None)),
            patch("app.cli.main.SentenceTransformerBackend", return_value=FakeBackend(vectors)),
            redirect_stdout(output),
        ):
            code = main(["search", "topic", "--semantic", "--semantic-sort", "--db", "unused.sqlite"])
        self.assertEqual(code, 0)
        rendered = output.getvalue()
        self.assertLess(rendered.index("higher score"), rendered.index("lower score"))

    def test_semantic_failure_does_not_discard_saved_results(self):
        output = io.StringIO()
        errors = io.StringIO()
        with (
            patch("app.cli.main.search_and_save_with_id", return_value=([resource("Already saved")], 1)),
            patch("app.cli.main.SentenceTransformerBackend",
                  side_effect=SemanticSimilarityError("model unavailable")),
            redirect_stdout(output),
            unittest.mock.patch("sys.stderr", errors),
        ):
            code = main(["search", "topic", "--semantic", "--db", "unused.sqlite"])
        self.assertEqual(code, 1)
        self.assertIn("Already saved", output.getvalue())
        self.assertIn("Semantic scoring error", errors.getvalue())

    def test_semantic_sort_requires_semantic_mode(self):
        errors = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
            main(["search", "topic", "--semantic-sort"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--semantic-sort requires --semantic", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
