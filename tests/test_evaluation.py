"""Hand-calculated tests for retrieval evaluation metrics."""

import math
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
import io
from unittest.mock import patch

from app.core.evaluation import (
    DATASET_VERSION,
    EvaluationError,
    JUDGMENT_FIELDS,
    aggregate_query_metrics,
    dcg_at_k,
    evaluate_query,
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    load_judgments,
)
from app.cli.main import main
from app.models import ResearchResource, ResourceType
from app.storage import ResearchStore


def resource(title: str, *, key: str, year: int = 2024) -> ResearchResource:
    return ResearchResource(
        resource_type=ResourceType.PAPER,
        title=title,
        description=None,
        authors=None,
        publication_year=year,
        source_provider="Evaluation fixture",
        external_id=f"fixture:{key}",
        doi=f"10.1234/{key}",
        keywords=None,
        retrieved_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


class FakeEmbeddingBackend:
    """Maps titles to hand-selected cosine values for reproducible CLI metrics."""

    model_revision = "fixture-revision"

    def encode(self, texts):
        rows = []
        for text in texts:
            if text.startswith("flood"):
                rows.append([1.0, 0.0])
            elif "Solar" in text:
                rows.append([1.0, 0.0])
            elif "River" in text:
                rows.append([0.0, 1.0])
            else:
                rows.append([-1.0, 0.0])
        return rows


class BinaryMetricTests(unittest.TestCase):
    def test_precision_at_k_uses_fixed_k_denominator(self):
        relevances = [2, 0, 1, 2]
        self.assertAlmostEqual(precision_at_k(relevances, 3), 1 / 3)
        self.assertEqual(precision_at_k([0, 1, 0], 2), 0.0)

    def test_recall_at_k_uses_all_judged_relevant_resources_as_denominator(self):
        relevances = [2, 0, 1, 2]
        self.assertEqual(recall_at_k(relevances, 3), 1 / 2)
        self.assertEqual(recall_at_k(relevances, 4), 1.0)

    def test_reciprocal_rank_and_mrr_are_hand_calculated(self):
        self.assertEqual(reciprocal_rank([0, 2, 1]), 1 / 2)
        self.assertAlmostEqual(mean_reciprocal_rank([[0, 2], [2, 0], [0, 0]]),
                               (1 / 2 + 1) / 2)

    def test_threshold_can_be_configured(self):
        self.assertEqual(precision_at_k([1, 2, 0], 2, relevant_threshold=1), 1.0)
        self.assertEqual(recall_at_k([1, 2, 0], 2, relevant_threshold=1), 1.0)
        self.assertEqual(reciprocal_rank([1, 2, 0], relevant_threshold=1), 1.0)

    def test_unjudged_entries_are_removed_before_applying_k(self):
        relevances = [None, 0, 2, None, 2]
        self.assertEqual(precision_at_k(relevances, 2), 0.5)
        self.assertEqual(recall_at_k(relevances, 2), 0.5)
        self.assertEqual(reciprocal_rank(relevances), 1 / 2)

    def test_unjudged_entries_are_rejected_when_judged_only_is_disabled(self):
        relevances = [None, 2, 0]
        with self.assertRaises(EvaluationError):
            precision_at_k(relevances, 2, judged_only=False)

    def test_invalid_labels_thresholds_and_k_are_rejected(self):
        for grade in (-1, 3, True, 1.0, "2"):
            with self.subTest(grade=grade), self.assertRaises(EvaluationError):
                precision_at_k([grade], 1)
        for k in (0, -1, True, 1.5):
            with self.subTest(k=k), self.assertRaises(EvaluationError):
                precision_at_k([2], k)
        with self.assertRaises(EvaluationError):
            recall_at_k([2], 1, relevant_threshold=0)

    def test_empty_and_no_relevant_cases_are_explicit(self):
        for relevances in ([], [None, None], [0, 1, None]):
            with self.subTest(relevances=relevances):
                self.assertEqual(precision_at_k(relevances, 5), 0.0)
                self.assertIsNone(recall_at_k(relevances, 5))
                self.assertIsNone(reciprocal_rank(relevances))
        self.assertEqual(mean_reciprocal_rank([[], [0, 0]]), 0.0)


class GradedMetricTests(unittest.TestCase):
    def test_dcg_uses_exponential_gain_and_log_discount(self):
        expected = 3.0 + 1.0 / math.log2(3)
        self.assertAlmostEqual(dcg_at_k([2, 1, 0], 3), expected)
        self.assertAlmostEqual(dcg_at_k([0, 2, 1], 3),
                               3.0 / math.log2(3) + 1.0 / math.log2(4))

    def test_ndcg_is_one_for_ideal_order_and_bounded_for_reordered_results(self):
        self.assertEqual(ndcg_at_k([2, 1, 0], 3), 1.0)
        value = ndcg_at_k([0, 2, 1], 3)
        self.assertGreater(value, 0.0)
        self.assertLess(value, 1.0)

    def test_empty_or_no_relevant_graded_cases_return_none_ndcg(self):
        self.assertEqual(dcg_at_k([], 5), 0.0)
        self.assertEqual(dcg_at_k([0, 0], 2), 0.0)
        self.assertIsNone(ndcg_at_k([], 5))
        self.assertIsNone(ndcg_at_k([0, 0], 2))


class QueryEvaluationTests(unittest.TestCase):
    def test_evaluate_query_omits_unjudged_candidates_before_k(self):
        rankings = {
            "baseline": ["unjudged", "irrelevant", "relevant", "partial"],
        }
        judgments = {"unjudged": None, "irrelevant": 0, "relevant": 2, "partial": 1}
        scores = evaluate_query(rankings, judgments, [1, 2], relevant_threshold=2)["baseline"]
        self.assertEqual(scores["precision@1"], 0.0)
        self.assertEqual(scores["recall@1"], 0.0)
        self.assertEqual(scores["precision@2"], 0.5)
        self.assertEqual(scores["recall@2"], 1.0)
        self.assertEqual(scores["reciprocal_rank"], 0.5)

    def test_evaluate_query_rejects_duplicate_candidate_ids_and_invalid_labels(self):
        with self.assertRaisesRegex(EvaluationError, "duplicate resource ID"):
            evaluate_query({"method": ["a", "a"]}, {"a": 2}, [1])
        with self.assertRaisesRegex(EvaluationError, "relevance grades"):
            evaluate_query({"method": ["a"]}, {"a": 4}, [1])

    def test_evaluation_is_reproducible_for_identical_inputs(self):
        rankings = {"keyword": ["a", "b"], "semantic": ["b", "a"]}
        judgments = {"a": 2, "b": 0}
        first = evaluate_query(rankings, judgments, [1, 2])
        second = evaluate_query(rankings, judgments, [1, 2])
        self.assertEqual(first, second)

    def test_aggregate_macro_averages_defined_metrics_and_skips_ineligible_values(self):
        per_query = [
            {"keyword": {"precision@2": 0.5, "recall@2": 1.0, "reciprocal_rank": 1.0}},
            {"keyword": {"precision@2": 0.0, "recall@2": None, "reciprocal_rank": None}},
            {"semantic": {"precision@2": 1.0, "recall@2": 1.0, "reciprocal_rank": 0.5}},
        ]
        aggregated = aggregate_query_metrics(per_query)
        self.assertEqual(aggregated["keyword"]["precision@2"], 0.5)
        self.assertEqual(aggregated["keyword"]["recall@2"], 1.0)
        self.assertEqual(aggregated["keyword"]["mrr"], 1.0)
        self.assertEqual(aggregated["semantic"]["precision@2"], 1.0)


class EvaluationFileAndCliTests(unittest.TestCase):
    def _save_fixture(self, directory: Path):
        db_path = directory / "research.sqlite"
        candidates = [
            resource("Flood model", key="relevant"),
            resource("Solar panel analysis", key="irrelevant"),
            resource("River flood gauge", key="unjudged"),
        ]
        with ResearchStore(db_path) as store:
            search_id = store.save_search("flood", candidates)
            pairs = store.get_search_resources_with_ids(search_id)
        judgment_path = directory / "judgments.csv"
        rows = []
        grades = {"relevant": "2", "irrelevant": "0", "unjudged": ""}
        for resource_id, candidate in pairs:
            key = (candidate.external_id or "").split(":")[-1]
            relevance = grades[key]
            rows.append({
                "dataset_version": DATASET_VERSION,
                "query_id": f"search-{search_id}",
                "topic": "flood",
                "search_id": str(search_id),
                "resource_id": str(resource_id),
                "title": candidate.title or "",
                "abstract": candidate.description or "",
                "keywords": "",
                "resource_type": candidate.resource_type.value,
                "source_provider": candidate.source_provider,
                "source_links": "[]",
                "relevance": relevance,
                "annotator": "reviewer" if relevance else "",
                "notes": "",
            })
        with judgment_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=JUDGMENT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return db_path, search_id, judgment_path, pairs

    def _json_judgments(self, csv_path: Path, json_path: Path) -> None:
        with csv_path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            row["search_id"] = int(row["search_id"])
            row["resource_id"] = int(row["resource_id"])
            row["relevance"] = int(row["relevance"]) if row["relevance"] else None
            row["source_links"] = json.loads(row["source_links"])
        json_path.write_text(json.dumps({"dataset_version": DATASET_VERSION, "rows": rows}),
                             encoding="utf-8")

    def test_judgment_loader_rejects_invalid_labels_and_duplicate_resource_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            base = {
                "dataset_version": DATASET_VERSION, "query_id": "search-1", "topic": "flood",
                "search_id": "1", "resource_id": "10", "title": "Example",
                "abstract": "", "keywords": "", "resource_type": "paper",
                "source_provider": "Fixture", "source_links": "[]", "relevance": "3",
                "annotator": "reviewer", "notes": "",
            }
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=JUDGMENT_FIELDS)
                writer.writeheader()
                writer.writerow(base)
            with self.assertRaisesRegex(EvaluationError, "relevance must be"):
                load_judgments(path)

            base["relevance"] = "2"
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=JUDGMENT_FIELDS)
                writer.writeheader()
                writer.writerows((base, base))
            with self.assertRaisesRegex(EvaluationError, "duplicate resource judgment"):
                load_judgments(path)

    def test_cli_evaluates_saved_candidates_and_writes_reproducible_json_csv_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path, search_id, judgment_path, pairs = self._save_fixture(root)
            outputs = {}
            for file_format in ("json", "csv"):
                output_path = root / f"report.{file_format}"
                stdout = io.StringIO()
                with (
                    patch("app.cli.main.SentenceTransformerBackend", return_value=FakeEmbeddingBackend()),
                    redirect_stdout(stdout),
                ):
                    code = main(["evaluate", "--judgments", str(judgment_path),
                                 "--search-id", str(search_id), "--k", "1", "2",
                                 "--hybrid-weights", "1", "0", "0",
                                 "--hybrid-weights", "0", "1", "0",
                                 "--format", file_format, "--output", str(output_path),
                                 "--db", str(db_path)])
                self.assertEqual(code, 0)
                self.assertIn("queries=1", stdout.getvalue())
                outputs[file_format] = output_path

            json_report = json.loads(outputs["json"].read_text(encoding="utf-8"))
            self.assertEqual(json_report["dataset_version"], DATASET_VERSION)
            self.assertEqual(json_report["query_count"], 1)
            self.assertEqual(json_report["judged_resource_count"], 3)
            self.assertEqual(json_report["unjudged_resource_count"], 1)
            self.assertEqual(json_report["eligible_query_count"], 1)
            query = json_report["queries"][0]
            self.assertEqual(query["known_relevant_count"], 1)
            self.assertEqual(query["methods"]["keyword"]["precision@1"], 1.0)
            self.assertEqual(query["methods"]["semantic"]["precision@1"], 0.0)
            self.assertEqual(query["methods"]["semantic"]["reciprocal_rank"], 0.5)
            self.assertEqual(query["methods"]["hybrid_default"]["precision@1"], 0.0)
            self.assertEqual(query["methods"]["hybrid_config_1"]["precision@1"], 0.0)
            self.assertEqual(query["methods"]["hybrid_config_2"]["precision@1"], 1.0)
            self.assertEqual(json_report["ranking_configuration"]["hybrid_configurations"].keys(),
                             {"hybrid_default", "hybrid_config_1", "hybrid_config_2"})
            self.assertEqual(len(json_report["judgments_sha256"]), 64)
            self.assertEqual(len(json_report["candidate_snapshot_sha256"]), 64)

            with outputs["csv"].open(encoding="utf-8-sig", newline="") as stream:
                csv_rows = list(csv.DictReader(stream))
            self.assertTrue(any(row["query_id"] == "search-1" for row in csv_rows))
            self.assertTrue(any(row["query_id"] == "AGGREGATE" for row in csv_rows))
            self.assertTrue(any(row["method"] == "semantic" and row["metric"] == "precision@1"
                                and float(row["score"]) == 0.0 for row in csv_rows))

    def test_json_judgment_loading_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, csv_path, _ = self._save_fixture(root)
            json_path = root / "judgments.json"
            self._json_judgments(csv_path, json_path)
            version, judgments = load_judgments(json_path)
            self.assertEqual(version, DATASET_VERSION)
            self.assertEqual(len(judgments), 3)
            self.assertEqual(sum(item.relevance is None for item in judgments), 1)

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            payload["rows"][0]["relevance"] = 1.5
            json_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(EvaluationError, "invalid identifiers or label"):
                load_judgments(json_path)

    def test_cli_rejects_tampered_candidate_title_or_source_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path, search_id, judgment_path, _ = self._save_fixture(root)
            for field in ("title", "source_provider"):
                with self.subTest(field=field):
                    with judgment_path.open(encoding="utf-8-sig", newline="") as stream:
                        rows = list(csv.DictReader(stream))
                    rows[0][field] = "Edited after annotation"
                    with judgment_path.open("w", encoding="utf-8-sig", newline="") as stream:
                        writer = csv.DictWriter(stream, fieldnames=JUDGMENT_FIELDS)
                        writer.writeheader()
                        writer.writerows(rows)
                    errors = io.StringIO()
                    with redirect_stderr(errors):
                        code = main(["evaluate", "--judgments", str(judgment_path),
                                     "--search-id", str(search_id), "--db", str(db_path)])
                    self.assertEqual(code, 1)
                    self.assertIn("candidate metadata", errors.getvalue())

    def test_cli_returns_clear_errors_for_bad_k_and_missing_judgments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.csv"
            errors = io.StringIO()
            with redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
                main(["evaluate", "--judgments", str(path), "--k", "0"])
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("--k values must be positive integers", errors.getvalue())

            errors = io.StringIO()
            with redirect_stderr(errors):
                code = main(["evaluate", "--judgments", str(path)])
            self.assertEqual(code, 1)
            self.assertIn("could not read judgments", errors.getvalue())

    def test_cli_template_exports_candidate_rows_from_saved_search(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "template.sqlite"
            with ResearchStore(db_path) as store:
                search_id = store.save_search("flood", [resource("Flood model", key="one")])
            output = root / "template.csv"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["evaluate", "--template", "--search-id", str(search_id),
                             "--output", str(output), "--db", str(db_path)])
            self.assertEqual(code, 0)
            self.assertIn(DATASET_VERSION, stdout.getvalue())
            with output.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["query_id"], f"search-{search_id}")
            self.assertEqual(rows[0]["relevance"], "")
            self.assertEqual(rows[0]["annotator"], "")


if __name__ == "__main__":
    unittest.main()
