"""Explainable hybrid-ranking tests using deterministic fake embeddings."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.cli.main import main
from app.core.hybrid_ranking import (
    DEFAULT_WEIGHTS,
    HybridRankingError,
    keyword_score,
    rank_resources,
    recency_score,
    tokenize,
    validate_weights,
)
from app.models import ResearchResource, ResourceType
from app.storage import ResearchStore


def resource(
    title: str | None = "Flood prediction",
    *,
    description: str | None = "River flow forecasting",
    keywords: tuple[str, ...] | None = ("rainfall", "hydrology"),
    year: int | None = 2020,
    external_id: str = "fixture:resource",
    doi: str = "10.1234/fixture",
) -> ResearchResource:
    return ResearchResource(
        resource_type=ResourceType.PAPER,
        title=title,
        description=description,
        authors=("An Author",),
        publication_year=year,
        source_provider="Fixture",
        external_id=external_id,
        doi=doi,
        keywords=keywords,
        retrieved_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


class FakeBackend:
    def __init__(self, vectors=None, error=None):
        self.vectors = vectors
        self.error = error
        self.calls = []
        self.model_revision = None

    def encode(self, texts):
        self.calls.append(list(texts))
        if self.error is not None:
            raise self.error
        if callable(self.vectors):
            return self.vectors(texts)
        return self.vectors


class HybridWeightTests(unittest.TestCase):
    def test_default_and_sequence_weights_are_normalized_to_named_mapping(self):
        self.assertEqual(validate_weights(DEFAULT_WEIGHTS), DEFAULT_WEIGHTS)
        self.assertEqual(validate_weights((0.2, 0.3, 0.5)),
                         {"semantic": 0.2, "keyword": 0.3, "recency": 0.5})

    def test_weight_validation_rejects_missing_unknown_bad_or_nonfinite_values(self):
        invalid = (
            {"semantic": 1.0},
            {"semantic": 0.5, "keyword": 0.5, "recency": 0.0, "extra": 0.0},
            (0.4, 0.4, 0.4),
            (-0.1, 0.6, 0.5),
            (float("nan"), 0.5, 0.5),
            (float("inf"), 0.0, 0.0),
            (True, 0.0, 0.0),
            (1.0, 0.0),
            "0.6,0.4,0.0",
        )
        for weights in invalid:
            with self.subTest(weights=weights), self.assertRaises(HybridRankingError):
                validate_weights(weights)

    def test_extremely_large_finite_weights_raise_domain_error(self):
        with self.assertRaises(HybridRankingError):
            validate_weights((1e308, 1e308, 0.0))


class HybridComponentTests(unittest.TestCase):
    def test_unicode_tokenization_casefolds_splits_punctuation_and_removes_stopwords(self):
        text = "FLOOD, prediction using données 雨量 and hydrology"
        tokens = tokenize(text)
        self.assertEqual(tokens, ["flood", "prediction", "données", "雨量", "hydrology"])

    def test_nfkc_equivalent_unicode_tokens_match_ascii_forms(self):
        self.assertEqual(tokenize("Ｆｌｏｏｄ prediction"), tokenize("flood prediction"))

    def test_keyword_score_combines_weighted_field_coverage(self):
        item = resource(title="Flood", description="prediction", keywords=("flood", "prediction"))
        # Title and description each cover half the query; keywords cover both.
        self.assertAlmostEqual(keyword_score("flood prediction", item), 0.6)

    def test_keyword_score_renormalizes_for_missing_fields_and_handles_empty_text(self):
        item = resource(title=None, description="flood", keywords=("prediction",))
        # The present fields have weights 0.30 and 0.20, each covering half.
        self.assertAlmostEqual(keyword_score("flood prediction", item), 0.5)
        self.assertEqual(keyword_score("the and using", resource()), 0.0)
        self.assertEqual(keyword_score("flood", resource(None, description=None, keywords=None)), 0.0)

    def test_recency_is_bounded_and_unknown_year_is_neutral(self):
        self.assertEqual(recency_score(2025, as_of_year=2025), 1.0)
        self.assertEqual(recency_score(2010, as_of_year=2025), 0.5)
        self.assertEqual(recency_score(1995, as_of_year=2025), 0.0)
        self.assertEqual(recency_score(2030, as_of_year=2025), 1.0)
        self.assertEqual(recency_score(None, as_of_year=2025), 0.5)
        self.assertEqual(recency_score("unknown", as_of_year=2025), 0.5)
        self.assertEqual(recency_score(-(10 ** 10_000), as_of_year=2025), 0.0)

    def test_recency_rejects_invalid_year_configuration(self):
        with self.assertRaises(HybridRankingError):
            recency_score(2020, as_of_year=True)
        for horizon in (0, -1, True, 1.5):
            with self.subTest(horizon=horizon), self.assertRaises(HybridRankingError):
                recency_score(2020, as_of_year=2025, horizon_years=horizon)


class HybridRankingServiceTests(unittest.TestCase):
    def test_empty_resource_list_needs_no_semantic_backend(self):
        self.assertEqual(rank_resources("flood", [], None, as_of_year=2025), [])

    def test_semantic_cosine_normalization_and_hybrid_formula(self):
        item = resource()
        backend = FakeBackend([[1, 0], [0, 1]])
        result = rank_resources("flood prediction", [item], backend, as_of_year=2025)[0]
        self.assertAlmostEqual(result.semantic_cosine, 0.0)
        self.assertAlmostEqual(result.semantic_component, 0.5)
        self.assertAlmostEqual(result.keyword_component, 0.5)
        self.assertAlmostEqual(result.metadata_component, 5 / 6)
        self.assertAlmostEqual(result.hybrid_score, 0.5)
        self.assertEqual(result.effective_weights, {"semantic": 0.6, "keyword": 0.4})
        self.assertIs(result.resource, item)
        self.assertEqual(result.original_position, 0)

    def test_recency_only_weighting_handles_missing_year_as_neutral(self):
        item = resource(year=None)
        result = rank_resources(
            "flood", [item], None, weights=(0, 0, 1), as_of_year=2025
        )[0]
        self.assertIsNone(result.semantic_cosine)
        self.assertIsNone(result.semantic_component)
        self.assertEqual(result.metadata_component, 0.5)
        self.assertEqual(result.hybrid_score, 0.5)
        self.assertEqual(result.effective_weights, {"recency": 1.0})

    def test_missing_semantic_text_renormalizes_remaining_available_components(self):
        item = resource(title=None, description=None, keywords=None)
        result = rank_resources(
            "flood", [item], FakeBackend(error=AssertionError("no text to encode")),
            weights=(0.5, 0.5, 0), as_of_year=2025,
        )[0]
        self.assertIsNone(result.semantic_cosine)
        self.assertEqual(result.keyword_component, 0.0)
        self.assertEqual(result.hybrid_score, 0.0)
        self.assertEqual(result.effective_weights, {"keyword": 1.0})

    def test_missing_semantic_backend_is_rejected_only_when_weight_is_positive(self):
        with self.assertRaisesRegex(HybridRankingError, "semantic backend is required"):
            rank_resources("topic", [resource()], None, as_of_year=2025)
        result = rank_resources("flood", [resource()], None,
                                weights=(0, 1, 0), as_of_year=2025)
        self.assertEqual(result[0].effective_weights, {"keyword": 1.0})

    def test_backend_failures_are_reported_as_hybrid_errors(self):
        with self.assertRaisesRegex(HybridRankingError, "inference failed"):
            rank_resources("topic", [resource()], FakeBackend(error=RuntimeError("offline")),
                           as_of_year=2025)

    def test_default_order_is_preserved_and_equal_score_sort_is_stable(self):
        items = [resource("first"), resource("second"), resource("third")]
        vectors = [[1, 0], [1, 0], [1, 0], [1, 0]]
        unsorted = rank_resources("topic", items, FakeBackend(vectors),
                                  weights=(1, 0, 0), as_of_year=2025)
        self.assertEqual([result.resource.title for result in unsorted], ["first", "second", "third"])
        sorted_results = rank_resources("topic", items, FakeBackend(vectors),
                                        weights=(1, 0, 0), as_of_year=2025,
                                        sort_by_score=True)
        self.assertEqual([result.resource.title for result in sorted_results],
                         ["first", "second", "third"])
        self.assertEqual([result.original_position for result in sorted_results], [0, 1, 2])

    def test_sort_by_score_orders_descending_with_original_position_as_tie_breaker(self):
        items = [resource("lower"), resource("tie one"), resource("tie two")]
        vectors = [[1, 0], [-1, 0], [1, 0], [1, 0]]
        ranked = rank_resources("topic", items, FakeBackend(vectors),
                                weights=(1, 0, 0), as_of_year=2025,
                                sort_by_score=True)
        self.assertEqual([item.resource.title for item in ranked], ["tie one", "tie two", "lower"])
        self.assertEqual([item.original_position for item in ranked], [1, 2, 0])

    def test_score_ranges_are_bounded_and_nonfinite_vector_errors_propagate(self):
        result = rank_resources("topic", [resource()],
                                FakeBackend([[1, 0], [1, 0]]), as_of_year=2025)[0]
        self.assertTrue(0 <= result.semantic_component <= 1)
        self.assertTrue(0 <= result.keyword_component <= 1)
        self.assertTrue(0 <= result.metadata_component <= 1)
        self.assertTrue(0 <= result.hybrid_score <= 1)
        with self.assertRaises(HybridRankingError):
            rank_resources("topic", [resource()],
                           FakeBackend([[1, 0], [float("nan"), 0]]), as_of_year=2025)


class HybridCliTests(unittest.TestCase):
    def test_cli_sorts_display_persists_provenance_and_exports_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "history.sqlite"
            lower = resource("lower result", external_id="fixture:lower", doi="10.1234/lower")
            higher = resource("higher result", external_id="fixture:higher", doi="10.1234/higher")
            with ResearchStore(db_path) as store:
                search_id = store.save_search("topic", [lower, higher], metadata={"status": "complete"})
            backend = FakeBackend([[1, 0], [-1, 0], [1, 0]])
            output = io.StringIO()
            with (
                patch("app.cli.main.search_and_save_with_id", return_value=([lower, higher], search_id)),
                patch("app.cli.main.SentenceTransformerBackend", return_value=backend),
                redirect_stdout(output),
            ):
                code = main(["search", "topic", "--hybrid", "--hybrid-weights", "1", "0", "0",
                             "--model", "fixture/model", "--db", str(db_path)])
            self.assertEqual(code, 0)
            rendered = output.getvalue()
            self.assertLess(rendered.index("higher result"), rendered.index("lower result"))
            self.assertIn("Hybrid relevance signal (experimental)", rendered)
            self.assertIn("semantic cosine=1.0000", rendered)

            with ResearchStore(db_path) as store:
                saved_resources = store.get_search_resources(search_id)
                summary = next(item for item in store.list_searches() if item.id == search_id)
            self.assertEqual([item.title for item in saved_resources], ["lower result", "higher result"])
            metadata = summary.metadata["hybrid_ranking"]
            self.assertEqual(metadata["model_id"], "fixture/model")
            self.assertEqual(metadata["weights"], {"semantic": 1.0, "keyword": 0.0, "recency": 0.0})
            self.assertEqual([item["original_position"] for item in metadata["scores"]], [2, 1])
            self.assertEqual(metadata["scores"][0]["semantic_cosine"], 1.0)

            history = io.StringIO()
            with redirect_stdout(history):
                self.assertEqual(main(["history", "--db", str(db_path)]), 0)
            self.assertIn("hybrid ranked", history.getvalue())

            shown = io.StringIO()
            with redirect_stdout(shown):
                self.assertEqual(main(["show", str(search_id), "--db", str(db_path)]), 0)
            self.assertIn("Hybrid ranking: weighted_semantic_keyword_metadata (hybrid-ranking-v1)",
                          shown.getvalue())
            self.assertIn("Original result 1: lower result", shown.getvalue())
            self.assertIn("Original result 2: higher result", shown.getvalue())

            json_path = root / "ranking.json"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["export", str(search_id), "--format", "json",
                                       "--output", str(json_path), "--db", str(db_path)]), 0)
            json_rows = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(json_rows[0]["search_metadata"]["hybrid_ranking"]["method"],
                             "weighted_semantic_keyword_metadata")

            csv_path = root / "ranking.csv"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["export", str(search_id), "--format", "csv",
                                       "--output", str(csv_path), "--db", str(db_path)]), 0)
            import csv
            with csv_path.open(encoding="utf-8", newline="") as stream:
                csv_row = next(csv.DictReader(stream))
            csv_metadata = json.loads(csv_row["search_metadata"])
            self.assertEqual(csv_metadata["hybrid_ranking"]["scores"][0]["hybrid"], 1.0)

    def test_keyword_only_mode_does_not_initialize_sentence_model(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "history.sqlite"
            item = resource("flood prediction", external_id="fixture:one", doi="10.1234/one")
            with ResearchStore(db_path) as store:
                search_id = store.save_search("flood", [item])
            output = io.StringIO()
            with (
                patch("app.cli.main.search_and_save_with_id", return_value=([item], search_id)),
                patch("app.cli.main.SentenceTransformerBackend",
                      side_effect=AssertionError("keyword-only mode must not load a model")) as model,
                redirect_stdout(output),
            ):
                code = main(["search", "flood", "--hybrid", "--hybrid-weights", "0", "1", "0",
                             "--db", str(db_path)])
            self.assertEqual(code, 0)
            model.assert_not_called()
            self.assertIn("Hybrid relevance signal (experimental)", output.getvalue())
            with ResearchStore(db_path) as store:
                summary = next(item for item in store.list_searches() if item.id == search_id)
            ranking = summary.metadata["hybrid_ranking"]
            self.assertIsNone(ranking["model_id"])
            self.assertEqual(ranking["weights"], {"semantic": 0.0, "keyword": 1.0, "recency": 0.0})

    def test_invalid_hybrid_weight_sum_is_a_usage_error(self):
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
            main(["search", "topic", "--hybrid", "--hybrid-weights", "0.4", "0.4", "0.4"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("hybrid weights must sum to 1", errors.getvalue())

    def test_hybrid_weights_require_hybrid_mode(self):
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
            main(["search", "topic", "--hybrid-weights", "0.6", "0.4", "0"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--hybrid-weights requires --hybrid", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
