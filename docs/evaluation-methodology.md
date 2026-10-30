# Ranking Evaluation Methodology

## Purpose and scope

This framework compares the existing keyword-only, semantic-only, and hybrid rankings against human-assigned judgments for academic papers and datasets. It evaluates only a fixed set of saved search snapshots and does not make live provider requests. Ranking scores never create labels. Evaluation reports are descriptive and do not claim statistical significance or generalization.

## Candidate and judgment workflow

Export candidates from saved search IDs (repeat `--search-id` to create a multi-query file):

```powershell
python -m app evaluate --template --search-id 3 --search-id 4 --output data/evaluation/judgments.csv
```

Each row contains a query ID and topic, stable local SQLite resource ID, saved title and abstract/description, keywords, resource type, source provider, and source links. An annotator assigns `0` (not relevant), `1` (partially relevant), or `2` (highly relevant), records an annotator identifier, and may add notes. A blank relevance cell means **unjudged**, not irrelevant. Preserve the original annotation file; the evaluation command reads it without modifying it. CSV is the editable template; JSON judgments are also accepted with a top-level `dataset_version` and `rows` array using the same fields. Dataset version is `research-relevance-v1`.

For a consistent manual pass, review each candidate against the original topic and the provided title, abstract/description, and keywords. Assign 0 when no substantive topic relationship is present, 1 when there is a plausible but partial relationship, and 2 when the resource directly addresses the topic. Do not use model scores or ranking position to assign the label. Record uncertainty or the basis for a borderline judgment in `notes`; leave the grade blank if the candidate cannot be assessed. Preserve a separate copy of the original file for each annotation round and record the annotator ID. This project does not prescribe or claim inter-annotator agreement or adjudication results.

To evaluate the annotated candidate pool offline:

```powershell
python -m app evaluate --judgments data/evaluation/judgments.csv --k 5 10 --output data/evaluation/report.json
python -m app evaluate --judgments data/evaluation/judgments.csv --k 5 --format csv --output data/evaluation/report.csv
python -m app evaluate --judgments data/evaluation/judgments.csv --hybrid-weights 0.5 0.4 0.1 --hybrid-weights 0.7 0.3 0
```

The saved search IDs in the label file select the candidate snapshots; `--search-id` may be used to validate/select the same set. The resource IDs and query topics must match saved records, and every candidate in each snapshot must occur exactly once. Run from the project root with the same SQLite database. Semantic and hybrid comparisons need the configured model available in the local cache (or network access for its first download); there is no provider/API request during evaluation.

## Metrics

Binary relevance defaults to grade 2; `--relevant-threshold 1` includes partially relevant resources. For each query and method:

- Precision@K = relevant judged results in the first K judged positions / K. K remains the denominator when fewer than K judged results exist.
- Recall@K = relevant results in the first K judged positions / all relevant judgments for that query.
- Reciprocal rank = 1 / position of the first threshold-relevant judged result. MRR is the macro mean across eligible queries.
- DCG@K = sum of `(2^grade - 1) / log2(position + 1)`; NDCG@K divides DCG by the ideal DCG for the same judged grades.

Unjudged resources are removed before positions and K are applied. Metrics therefore compare only judged candidates. Recall is recall within the judged candidate pool; it is not recall over the entire scholarly literature. A query without any threshold-relevant judgments has undefined recall and reciprocal rank. NDCG uses the full graded labels, so a query with only grade-1 partial judgments can have a defined per-query NDCG at threshold 2. For aggregate comparisons, queries without any threshold-relevant judgment are omitted consistently from **all** metrics; per-query scores and relevant counts remain in the report. Aggregate values are macro means across eligible queries; the aggregate reciprocal-rank mean is labeled MRR. An empty ranking has precision 0 and undefined recall/RR/NDCG when it contains no positive judged gains.

## Ranking comparison and reproducibility

All methods use the same saved resource snapshots, query topic, and human labels. Keyword ordering reuses Phase 12's lexical score and stable original-order tie breaking. Semantic ordering reuses the configured Sentence Transformers model and cosine similarity. Hybrid comparisons include the default 0.60 semantic / 0.40 keyword / 0.00 recency weights plus each explicitly supplied configuration. Weight configurations are compared, not tuned on the reported results. There is no train/test split or claim that a configuration is optimal.

JSON reports preserve per-query order and metrics, aggregate macro means, the candidate snapshot and hash, judgment file hash, dataset/code versions, evaluation time, Python version, K and relevance threshold, model identifier/revision/runtime, hybrid weights, metric definitions, annotator IDs/notes, and limitations. CSV reports flatten per-query and aggregate metrics and repeat the main model/configuration provenance. Existing SQLite history is read-only during evaluation; no migration or history rewrite occurs.

## Verified synthetic calculation and empirical status

The metric implementation is checked with the explicit synthetic grade order `[0, 2, 1]` at relevance threshold 2. Hand calculation gives P@2 = 0.5, judged-pool R@2 = 1.0, RR = 0.5, DCG@3 = 2.392789, and NDCG@3 = 0.659002 (ideal DCG@3 = 3.630930). These values verify arithmetic only; the fixture is not a research evaluation and must not be described as empirical thesis findings.

The CLI fixture also compares the same three synthetic candidates with grades `[2, 0, unjudged]`. With its deterministic fake encoder, keyword-only yields P@1=1.0 and RR=1.0; semantic-only yields P@1=0.0 and RR=0.5; default hybrid yields P@1=0.0 and RR=0.5. Pure semantic and pure keyword hybrid configurations are also exercised. These fabricated labels and fake vectors validate that methods use a common candidate pool and that scores flow into the report; they are not research results and say nothing about model quality.

No genuinely human-labeled dataset is currently supplied. Empirical comparison is pending annotation of exported candidate files. Do not report synthetic fixture metrics as evidence that a ranking approach is more relevant or accurate.
