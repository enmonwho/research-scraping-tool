# Research Discovery CLI

A local, Python-first research discovery application for thesis work. It searches papers through OpenAlex by default or Crossref when selected; `--provider all` searches both providers and merges duplicate papers. Dataset searches use Harvard Dataverse. Results are normalized, saved in SQLite, displayed, available in search history, and exportable to CSV or JSON.

## Setup and use

Python 3.10 or newer is required. The application has no runtime dependencies. From the repository root:

```powershell
python -m app --help
python -m app "urban heat islands"
python -m app "urban heat islands" --limit 5 --timeout 8
python -m app search "flood prediction" --type papers
python -m app search "flood prediction" --type papers --provider all
python -m app search "machine learning for flood prediction" --expand
python -m app search "flood prediction" --provider all --expand --expand-limit 3
python -m app search "GIS for flood prediction" --type datasets --expand
python -m app search "flood prediction" --type datasets
python -m app search "flood prediction" --type datasets --limit 5 --timeout 8
python -m app "urban heat islands" --db "data/my-research.db"
python -m app history
python -m app show 1
python -m app export 1 --format csv --output exports/urban-heat.csv
python -m app export 1 --format json --output exports/urban-heat.json
python -m app download 3 2
python -m app download 3 2 --file 1
python -m app download 3 2 --file 1 --download-dir "D:\Research Files"
```

To install the command in a Python environment, use `python -m pip install -e .` and then run `research-discovery "urban heat islands"`. Packaging uses setuptools. Surround a multiword topic with quotes. Paper searches use the [official OpenAlex Works API](https://help.openalex.org/api/); dataset searches use the [Harvard Dataverse Search API](https://guides.dataverse.org/en/latest/api/search.html) and [Native API](https://guides.dataverse.org/en/latest/api/native-api.html). Both require internet access and use publicly accessible records without an API key. Dataverse rate limits are configurable by each installation and request type ([configuration docs](https://guides.dataverse.org/en/latest/installation/config.html)), so transient throttling receives bounded retries.

The CLI requests at most 20 results in one page (`--limit` accepts 1 through 20). It filters OpenAlex work types to articles, preprints, conference papers, reviews, data papers, software papers, and reports. `--timeout` sets a positive, finite per-attempt timeout in seconds (default 10). Transient HTTP 429/5xx and network failures receive at most two retries with bounded delays. Results show available normalized metadata and label unavailable values as unknown. The OpenAlex request follows its [search](https://help.openalex.org/api/searching/), [field selection](https://help.openalex.org/api/selecting-fields/), [work types](https://help.openalex.org/data/work-types/), and [authentication/rate limit](https://help.openalex.org/api/authentication/) documentation. Dataverse search reads public dataset records, then requests metadata by persistent ID; its returned fields vary with what creators deposited.

The explicit `search TOPIC` command and earlier positional topic form are equivalent; paper search remains the default. Paper searches default to OpenAlex; `--provider crossref` selects Crossref, and `--provider all` searches OpenAlex followed by Crossref. For combined searches, `--limit` is requested from each provider and then applied again to the final deduplicated list; the provider APIs are each capped at 20. Results are deterministic with OpenAlex first. Partial provider failures retain successful results, display a warning, and are saved with a PARTIAL status; if both fail, the command exits 1 and saves nothing. Add `--type datasets` for Harvard Dataverse datasets; combined providers apply only to papers. `history` lists saved-search IDs, timestamps, and result counts, marking partial searches. `show ID` displays a saved snapshot and its partial status without contacting a provider. `export ID --format csv|json --output PATH` writes only saved metadata; it does not fetch PDFs or datasets. Exports use UTF-8, preserve all source links, provider identifiers, provenance, and provider-reported file records, sanitize the output filename, and refuse to overwrite an existing file. CSV stores multi-valued fields as JSON text within a cell, including complete link and identifier lists. JSON keeps normalized fields, nested links/files, provenance, and combined-search status. Pass `--db PATH` to history, show, and export to select the same database used for searches.

The topic must contain 1 to 200 characters after surrounding whitespace is removed. A missing, blank, or oversized topic prints an error to stderr and exits with status 2. Invalid search options also exit with status 2. `--help` exits with status 0. A successful search, including zero results, exits with status 0; a provider, network, or storage failure exits with status 1.

The validator can also be called from Python without the CLI:

```python
from app.core.topic import validate_topic

topic = validate_topic("  urban heat islands  ")
```

## Normalized resources

`search_papers()` returns provider-independent `ResearchResource` objects without importing the CLI:

```python
from app.providers.openalex import search_papers

resources = search_papers("urban heat islands", limit=5, timeout=8)
for resource in resources:
    print(resource.title, resource.doi, resource.links)
```

The typed model in `app.models` supports paper and dataset resource types, `ResourceLink`, and `DownloadableFile`. A resource retains title, description, authors, year, keywords, provider ID, a normalized lowercase bare DOI, UTC retrieval time, original provider metadata, and known license/access fields. Missing authors or keywords are `None`; an explicit empty list becomes an empty tuple. A resource-level license and access value are set only when supported by provider metadata.

OpenAlex [location fields](https://help.openalex.org/data/locations/) determine link kinds: supplied landing pages, DOI URLs, publisher or repository pages, and reported open-access URLs remain distinct. Only an explicit `pdf_url` becomes a direct-file link and `DownloadableFile` (format PDF, MIME type `application/pdf`, unknown filename). Harvard Dataverse results retain dataset landing pages, DOI, citation metadata, reported license, and complete returned dataset/file metadata in provenance. File records include provider IDs, names, MIME types, filename-derived formats, documented file access endpoints, and known restricted/public status. Endpoints are shown as links only; Phase 7 does not download files. A file-page URL is not invented when the API does not supply one. Unknown license and access remain unknown.

## Local search history

Each successful CLI search is saved to `data/research.db` by default. Use `--db PATH` for another local SQLite file; missing parent directories are created. The `data/` directory is ignored by Git. Search and history operations are also available without the CLI:

```python
from app.core.search_service import search_and_save, list_history, get_history_resources

resources = search_and_save("urban heat islands", limit=5)
for search in list_history():
    print(search.id, search.topic, search.searched_at, search.result_count)
    past_resources = get_history_resources(search.id)
```

Schema version 3 uses `searches`, `resources`, `search_resources`, `resource_links`, `downloadable_files`, `resource_identifiers`, and `resource_provenance`. Version 2 added namespaced identity aliases and provider observations; version 3 adds optional search-level metadata used to retain combined-search status. Both migrations are transactional and leave existing search snapshots and their order unchanged. Each search keeps its own normalized snapshot, while resource-level records accumulate matching source links, files, identifiers, and provider metadata. Saving a search and its associations is one transaction; a failed or interrupted write rolls back. `PRAGMA user_version` tracks migrations. Unsupported newer schemas are rejected without changes.

## Deduplication

Search results are deduplicated in this order: normalized DOI; provider ID scoped by provider name; canonical HTTP(S) landing, publisher, repository, or open-access URL; then a strict fallback requiring the same normalized title, known publication year, and full author signature when neither resource has a stronger identifier. DOI URL, `doi:` prefix, and bare DOI forms normalize to the same lowercase DOI. A conflict between known DOI values, resource types, or similar-but-not-identical titles keeps records separate. URL normalization lowercases scheme/host, removes default ports, fragments, and trailing path slashes, and preserves query parameters.

Matched observations retain their distinct provider IDs and raw metadata in provenance. Links and provider-reported file records are accumulated; existing known scalar metadata is not replaced by conflicting or missing values. Current search snapshots capture that search's observation plus accumulated links/files/provenance. Previously saved snapshots, search IDs, dates, and ordering are not rewritten. The migration does not merge or delete preexisting resource rows; later observations resolve to an existing match while old history remains intact. Dataset type is kept separate from paper type.

Run the standard-library tests with `python -m unittest discover -s tests -v`. Tests use synthetic API fixtures and need no network access. Search requires network access. Work is gated one phase at a time under `AGENTS.md`; see [the phase tracker](docs/phase-status.md) for verified status.

Phase 7 verification: all 92 tests passed, including synthetic Dataverse API and SQLite/CLI/export integration tests. `python -m compileall -q app tests` and `python -m tabnanny app tests` passed. A live Harvard Dataverse CLI smoke search returned one dataset with two file records and successfully exercised history, show, CSV, and JSON export. Ruff is not installed in the approved environment and was not added.

## Phase 9: Safe, user-selected downloads

Downloads use only file URLs preserved from provider metadata. Inspect the saved result and select the file explicitly; no file is downloaded until `--file N` is supplied:

```powershell
python -m app download SEARCH_ID RESULT_NUMBER
python -m app download SEARCH_ID RESULT_NUMBER --file FILE_NUMBER
python -m app download SEARCH_ID RESULT_NUMBER --file FILE_NUMBER --download-dir PATH --timeout 15 --max-size 104857600
```

The command lists provider filename, format, MIME type, provider access indication, source provider, and URL. It reports a landing page when the resource has no supplied file endpoint. Files are placed under `data/downloads/papers/` or `data/downloads/datasets/` by default; choose another root with `--download-dir`. Existing names receive a numbered suffix. Each downloaded file has an adjacent `.source.json` sidecar recording its provider URL, provider, license, supplied/response MIME metadata, and byte count. Supported formats depend on provider file metadata and response (commonly PDF, CSV, and ZIP); there is no extension-based trust decision.

The downloader permits HTTP/HTTPS only, rejects credentials and non-public IP destinations, validates every redirect, resolves and pins a public address for each connection, uses finite timeouts, streams with a 100 MiB default cap, checks status and content type, rejects obvious HTML pages, and finalizes files atomically without overwriting. Temporary files are cleaned after failures. HTTP 401/403 and provider-marked restricted files are refused. Unknown access is shown as unknown and may be attempted; providers can still deny access. The tool does not bypass authentication or paywalls, inspect publisher pages for files, or execute downloaded content. No checksum verification is claimed.

Phase 9 verification: the full suite passed 161 tests with 0 failures/errors and 1 platform skip (Windows denied directory-symlink creation); compilation, indentation checks, and CLI help passed. The live OpenAlex + Crossref search saved three unique results in one search record. One provider-marked public 2,059,290-byte arXiv PDF was downloaded; its `%PDF-` signature, byte count, source URL, and sidecar metadata were checked. The initial live combined-search display hit a Windows CP1252 encoding error on a provider title after the search had been saved; its stored history was independently read to confirm one record. The download was performed from that saved search. Live checks are separate from mocked tests.

## Phase 10: Deterministic query expansion

Expansion is disabled by default. Add `--expand` to search the validated original topic plus transparent keyword variants:

```powershell
python -m app search "machine learning for flood prediction" --expand
python -m app search "flood prediction" --provider all --expand --expand-limit 3
python -m app search "GIS for flood prediction" --type datasets --expand
```

`--expand-limit` accepts 0 through 4 generated variations (default 4); the original is always included and is not counted against that limit. The deterministic `keyword-rules-v1` rules include prediction/forecasting and modeling/modelling substitutions, exact uppercase expansions for AI/GIS/ML/NLP, limited method-for-task rephrasing, and removal of framing prefixes `study of` and `research on`. Case-insensitive duplicate variations are removed. Topics without a documented match search only the original. No model or external expansion service is used.

Each provider is called once per query, in stable query/provider order; results are merged using existing deduplication and capped at `--limit`. A search makes at most 5 queries including the original and at most 10 provider search calls. Existing adapter retry bounds also apply; counting Dataverse's search plus up to 20 per-result metadata requests, three attempts per request, and urllib's maximum ten redirects per attempt gives a conservative HTTP-attempt ceiling of 3,465 across five queries. Search metadata stores the original topic, ordered queries, the named rule for every generated query, method/version, providers, failures, request counts, retrieved and unique counts, and duplicate count. This provenance is available in history/show and JSON/CSV exports; each operation remains one schema-v3 search record.

The CLI configures its output streams to escape characters unsupported by a restricted Windows console (for example, a CP1252 console renders an unencodable character as a `\\u....` escape) instead of aborting after saving results. SQLite and CSV/JSON exports retain the original Unicode text. Download destination validation still resolves the type subdirectory and rejects paths outside the selected root; the Windows symlink test may be unavailable where directory-symlink privileges are not granted.

Phase 10 verification: all 181 tests ran; 180 passed, 0 failed/errors, and 1 Windows symlink-privilege test was skipped. Compilation, indentation checks, and CLI help passed. Synthetic retrieval comparison: the original-only OpenAlex fixture used 1 provider request and returned 2 records / 2 unique resources / 0 duplicates removed. The expanded fixture used 2 requests and returned 4 records / 3 unique resources / 1 duplicate removed. This checks request and deduplication accounting only; no relevance gain is claimed. Tests also exercise Windows drive, UNC, parent traversal, and a simulated resolved-path escape. A real directory symlink remains untestable on this Windows host because the required privilege is unavailable. No live API request was run.

## Phase 11: Semantic similarity

Semantic similarity is optional and does not change provider retrieval. Install the CPU-capable optional dependencies, then request scoring explicitly:

```powershell
python -m pip install -e ".[semantic]"
python -m app search "machine learning flood prediction" --semantic
python -m app search "machine learning flood prediction" --semantic --semantic-sort
python -m app search "flood prediction" --type datasets --semantic --model-cache data/model-cache
```

The default model is `sentence-transformers/all-MiniLM-L6-v2`; `--model ID` selects another Sentence Transformers model. The optional `--model-cache PATH` uses the library's supported cache-folder setting. If no cache exists, the first semantic search needs internet access to fetch model files (the default cache is managed by Hugging Face). Subsequent searches reuse the cache. CPU inference is used. On Windows 11 x64 with Python 3.14.3, the optional dependency installation succeeded with sentence-transformers 6.1.0, transformers 5.19.0, and PyTorch 2.14.1. This install requires large ML packages; the PyTorch wheel was about 124 MB and MiniLM weights about 91 MB before unpacked cache/runtime storage.

The service compares the topic with text assembled from each resource's title, description, and keywords (each included at most once). Authors, DOI, identifiers, and URLs are excluded. Resource text is capped at 5,000 characters; the selected model can apply its own token-length limit as well. Scores are cosine similarity values in [-1, 1], not probabilities or correctness percentages. Scores display in original result order unless `--semantic-sort` is explicitly requested; that option affects display only, not saved search-result order. Resources lacking usable text receive an unavailable score. Model loading is lazy, so ordinary searches do not import or load ML packages. Model-load/inference failures produce an error and leave the already-saved search intact.

Derived scores are computed on demand and stored under the search's schema-v3 `semantic_similarity` metadata with the method/version, model ID, CPU device, runtime package versions, text limit, display-sort setting, and per-result provider identity/score. Normalized resource metadata and historical resource snapshots are not modified. Existing CSV/JSON exports include the search metadata. No schema migration is required.

Phase 11 verification: 200 tests ran, 199 passed, 0 failed/errors, and 1 Windows directory-symlink privilege test was skipped. The real model was loaded once from CPU cache on Python 3.14.3: initial load 18.311 seconds; four-text batch inference 0.036 seconds; 384-dimensional embeddings; process working set about 527.9 MB after load and 533.7 MB after inference. A separate real-service smoke scored two synthetic resources at 0.713401 and 0.389351; these values validate inference and cosine wiring only, not research relevance. Measurements are one-run results, not performance guarantees. Ruff is not installed. No research-provider API search was run in this phase.

## Phase 12: Hybrid research ranking

Hybrid ranking is optional. `--hybrid` computes and sorts the displayed result list by an experimental combined score; the saved search snapshots stay in their original provider/deduplication order. `--semantic` remains a separate mode with its existing behavior.

```powershell
python -m app search "machine learning flood prediction" --provider all --hybrid
python -m app search "flood prediction" --type datasets --hybrid --hybrid-weights 0.5 0.4 0.1
```

Default weights are semantic **0.60**, keyword **0.40**, and recency **0.00**. Supply `--hybrid-weights SEMANTIC KEYWORD RECENCY` to choose nonnegative values that sum to 1. The default turns recency off, so older studies are not downweighted automatically. If explicitly configured, recency is `max(0, 1 - age / 30 years)`, future-dated years are capped at 1, and a missing/invalid publication year is neutral at 0.5. This is a small exploratory signal, not research quality.

Semantic cosine in [-1, 1] is linearly normalized to [0, 1]. Keyword scoring uses NFKC-normalized, casefolded Unicode tokens, splits on punctuation/whitespace, drops a small English stopword list, and measures query-token coverage. Title, description, and keyword field coverage use 0.50/0.30/0.20 weights renormalized across fields with usable text. This lightweight baseline does not stem words, expand synonyms, or segment unspaced scripts. The final weighted score is in [0, 1]; if a resource has no usable semantic text, its remaining configured nonzero component weights are renormalized. If no weighted component is available, the final score is unavailable.

Output labels the result as an experimental relevance signal and displays semantic cosine, normalized semantic, keyword, and recency components. Search metadata stores the method/version, base weights, per-result component/final scores and effective weights, original order, sort setting, `as_of_year`, token/field settings, and semantic model/runtime details. The Hugging Face cache commit is recorded when available. `history` marks hybrid searches; `show` prints stored scores in original result order; CSV/JSON exports already carry complete search metadata. No schema migration or resource snapshot rewrite is used.

Similarity and lexical scores have not been evaluated against human relevance judgments. Weights are reproducible defaults for experimentation, not scientifically validated ranking settings. More details and current test/comparison evidence are in [the phase tracker](docs/phase-status.md).

## Phase 13: Thesis ranking evaluation

Phase 13 adds a human-judgment workflow and offline comparison of keyword, semantic, and hybrid rankings. Export candidates from existing saved searches, annotate them, then evaluate without another provider request:

```powershell
python -m app evaluate --template --search-id 3 --search-id 4 --output data/evaluation/judgments.csv
python -m app evaluate --judgments data/evaluation/judgments.csv --k 5 10 --output data/evaluation/report.json
python -m app evaluate --judgments data/evaluation/judgments.csv --k 5 --format csv --output data/evaluation/report.csv
python -m app evaluate --judgments data/evaluation/judgments.csv --hybrid-weights 0.5 0.4 0.1 --hybrid-weights 0.7 0.3 0
```

The template includes title, abstract/description, keywords, type, provider, source links, and stable SQLite resource IDs. Assign relevance manually as 0 (not relevant), 1 (partially relevant), or 2 (highly relevant); leave a blank label unjudged and supply an annotator ID for every assigned label. The default binary threshold is grade 2; use `--relevant-threshold 1` to count partial relevance as relevant. JSON judgment files with the same fields and `dataset_version` are also accepted. The evaluator checks each candidate row against the immutable saved snapshot, requires a label row for every candidate, and never creates judgments from ranking scores.

Unjudged resources are omitted before applying K. Recall describes the judged candidate pool only, not the scholarly literature. Queries without a threshold-relevant judgment are omitted from all aggregate metrics; per-query scores remain available. NDCG uses graded gain `2^grade - 1`. The report records the dataset and code versions, judgment-file and candidate-snapshot hashes, saved-search provenance, model ID/revision/runtime, ranking weights, K values, evaluation date, individual rankings and metrics, aggregate metrics, and limitations. Existing SQLite history is read-only; no migration is needed. Semantic comparisons require the model to be cached (first retrieval may need internet).

No genuine human-labeled research collection is included yet. The checked-in synthetic fixtures verify formulas and CLI wiring only; they are not empirical thesis findings. Follow [the evaluation methodology](docs/evaluation-methodology.md) and [phase tracker](docs/phase-status.md) for metric definitions, aggregation rules, and verified results.

Phase 13 verification: 21 focused evaluation tests passed. The complete suite ran 243 tests: 242 passed and 1 Windows directory-symlink privilege test was skipped. System Python and the optional semantic `.venv` produced the same result. Compilation, indentation checks, and `evaluate --help` passed. Tests use synthetic judgments/fake embeddings; no real human-labeled experiment or live provider call was run.

## Phase 14: Optional authenticated n8n bridge

The standalone CLI remains usable without a server or n8n. To let a local n8n workflow submit searches and poll their results, start the optional loopback-only bridge:

```powershell
$env:RESEARCH_DISCOVERY_TOKEN = "replace-with-a-random-secret-at-least-24-characters"
python -m app serve --host 127.0.0.1 --port 8765 --db data/research.db
```

The token is read from the environment and is never stored in the workflow or database. Requests use `Authorization: Bearer <token>`. `POST /jobs` accepts JSON with `topic` (required), and optional `provider` (`openalex`, `crossref`, or `all`), `limit` (1–20), `timeout` (up to 120 seconds), `expand` (boolean), and `expand_limit` (0–4). It returns HTTP 202 and a job ID. Poll `GET /jobs/{job_id}` until the status is `succeeded` or `failed`; a successful response includes the saved search ID, metadata, and normalized results. Job state is kept in memory for this server process (up to 256 recent jobs); saved results remain in SQLite. The worker queue is bounded and returns HTTP 503 when full.

Example n8n HTTP Request nodes: POST to `http://127.0.0.1:8765/jobs` with the token header and JSON body `{"topic":"urban heat islands","provider":"all","limit":10}`; then GET the returned `status_url` with the same header. Keep the token in n8n credentials. This address works when n8n runs on the same computer. A hosted or containerized n8n instance cannot reach a user's loopback address directly; use a private, authenticated network path or a local n8n instance. The bridge intentionally refuses non-loopback bind addresses and does not expose an unauthenticated endpoint. Do not publish it directly to the internet.

An importable manual-trigger workflow is included at [`app/integrations/n8n/workflows/research-discovery-search.json`](app/integrations/n8n/workflows/research-discovery-search.json). In n8n, choose **Workflows → Import from File**, import the JSON, create a generic **Bearer Auth** credential containing the same token used by the server, and select it in both HTTP Request nodes. This stores the secret in n8n credentials instead of the workflow export. Open **Edit search settings** to set the topic/provider/result limit, then run the workflow. It submits one search and checks status every two seconds, up to 30 checks; the final node output contains the successful or failed response, or the still-running job status if it reaches the polling cap. The workflow is inactive on import and contains no secret. n8n Cloud cannot call a server at `127.0.0.1`; the bridge would need a securely reachable private endpoint, and this local bridge deliberately does not provide one. This workflow package has not been imported into a live n8n instance in this session.

This workflow automates the paper-search path through the Python bridge. The Python application and its SQLite database still run on your machine; importing the workflow does not copy the application, ranking/evaluation models, dataset search, or download manager into your n8n account. The workflow itself has not been tested against a live n8n instance.

## Planned roadmap

| Phase | Scope |
| --- | --- |
| 0 | Repository inspection and planning (complete) |
| 1 | Minimal Python CLI foundation: topic input, validation, help, tests (complete) |
| 2 | OpenAlex paper search adapter (complete) |
| 3 | Canonical resources and source provenance (complete) |
| 4 | SQLite persistence and search history (complete) |
| 5 | Terminal presentation and CSV/JSON export (complete) |
| 6 | Conservative deduplication (complete) |
| 7 | Harvard Dataverse dataset provider (complete) |
| 8 | Crossref academic paper provider (complete) |
| 8B | Combined OpenAlex and Crossref paper search (complete; Phase 9 awaits approval) |
| 9 | User-selected safe downloads (complete) |
| 10 | Deterministic query expansion (complete) |
| 11 | Semantic similarity (complete) |
| 12 | Hybrid ranking (complete) |
| 13 | Thesis evaluation framework (complete; human-labeled study pending) |
| 14 | Optional n8n integration (complete; not required for standalone use) |
| 15 | PySide6 desktop GUI and Windows executable (implemented; clean-machine validation pending) |

The CLI remains independently usable alongside the optional desktop app. A website, Next.js app, and Supabase dependency are outside the project scope. Each later phase requires explicit approval before implementation.

## Desktop application (Phase 15)

The PySide6 app searches papers and Harvard Dataverse datasets, lets you inspect results and provenance, revisit saved searches, export CSV/JSON, and download a provider-supplied file only after selecting it. Paper details show a prominent provider-supplied paper page link; when no direct file is supplied, the page is offered as the alternative. Its dark research-console interface uses charcoal surfaces, high-contrast text, and a restrained lime accent. The first screen focuses on resource type and topic search; less-used provider, limit, timeout, expansion, and ranking controls are collapsed under Advanced search settings. Saved-search actions are grouped in an Actions menu. Search, model scoring, history loading, and downloads run in background workers so the window stays responsive. The app reuses the same Python services and SQLite database as the CLI; use **Choose database** to open an existing CLI history file.

On Windows, user data defaults to `%LOCALAPPDATA%\\ResearchDiscovery` (database, downloads, and model cache), outside the installation folder. Build the onedir executable from a Windows Python environment with the project's build extra installed:

```powershell
python -m pip install -e ".[build]"
.\\scripts\\build-gui.ps1
```

The resulting launcher is `dist/phase15-gui/ResearchDiscovery/ResearchDiscovery.exe`. Copy the entire `ResearchDiscovery` folder when sharing the application. The executable does not bundle databases, downloaded files, ML packages, or model weights. Semantic/hybrid scoring needs the optional `[semantic]` dependencies and a model cache; selecting scoring in a build without those dependencies reports the error while retaining retrieved results.

Phase 15 evidence: after the UI cleanup, the GUI-focused suite passed 21 tests and the full suite passed 268 tests with 1 Windows directory-symlink privilege skip; both processes exited 0. `compileall`, `tabnanny`, and `git diff --check` passed. The PyInstaller onedir build was refreshed with the paper-page fallback, and the executable stayed active for an 8-second startup smoke test. The executable's interactive search/download flow, live provider access, model inference from the bundle, and clean-Windows-machine behavior have not been verified. See [the phase tracker](docs/phase-status.md) for details.

## Phase 8: Crossref papers

Paper searches still default to OpenAlex. Choose Crossref explicitly with:

```powershell
python -m app search "flood prediction" --type papers --provider crossref
python -m app search "flood prediction" --type papers --provider openalex
```

Crossref uses the public [`/works` REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) with `query` and `rows`. It does not require a paid account, API key, or other credentials. Set `CROSSREF_MAILTO` in the environment to identify requests through Crossref's polite pool; when configured, the adapter sends it as `mailto` and includes it in the User-Agent. Per-attempt timeout uses `--timeout`; the result limit uses `--limit`. HTTP 429, 5xx, and network timeouts have at most two bounded retries; HTTP 400 and other non-transient HTTP errors return a clear provider error.

Crossref titles, author names, partial publication years, DOI, supplied landing/publisher links, supplied license URLs, and raw provider records are retained when present. Abstract markup is reduced to text. Crossref does not consistently deposit abstracts, publication dates, licenses, or full-text links. Publisher name and Crossref work type are retained in original metadata because the shared resource model has no dedicated fields. Only provider-supplied `application/pdf` link records become file records; no file URL is inferred. File access and license status remain unknown unless the metadata supports them. DOI normalization uses the existing shared identity matching, so Crossref and OpenAlex records with the same DOI retain both provider identifiers, links, and provenance in SQLite. Conflicting DOI values remain separate; saved historical search snapshots are not rewritten.

`--provider` applies only to `--type papers`; datasets continue to use Harvard Dataverse. Combined `all` search is Phase 8B, between Phase 8 and Phase 9. Phase 9 requires explicit approval before implementation.
