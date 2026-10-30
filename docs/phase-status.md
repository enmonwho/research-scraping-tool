# Phase status

## Phase 15 — Desktop GUI and Windows executable

**Status: PARTIAL — desktop implementation and local executable startup verified; clean-machine validation remains unavailable.**

### Initial inspection and plan

- Python 3.14.3 on Windows 11; repository and existing `.venv` initially lacked PySide6 and PyInstaller. The `.venv` already contains the optional semantic model runtime.
- Existing `build/` and `dist/` artifacts are present, so this phase uses isolated `build/phase15-gui` and `dist/phase15-gui` paths and will not clean or overwrite the existing wheel/output directory.
- Astra's plan: native PySide6 GUI around existing services; user-writable configuration/database/download/model paths; async workers; onedir PyInstaller build; preserve the CLI and avoid bundling user data or model weights.
- PySide6 Essentials 6.12.0 and PyInstaller 6.22.3 installed in ignored `.venv` only. The full PySide6 metapackage was cancelled before installation completed because it began downloading unused WebEngine/PDF add-ons.
- Added shared `app/core/export.py` and `app/core/app_paths.py`; added the PySide6 desktop workflow, UI, worker/controller layer, and onedir PyInstaller packaging. No system-wide installation or live provider API request was performed.
- The GUI supports paper and dataset searches, provider/result/timeout controls, optional query expansion and ranking, saved history and choosing an existing CLI database, resource/provenance review, CSV/JSON export, and explicit selection of a supplied downloadable file. Search, ranking, history, export, and download work runs off the UI thread. Data defaults to per-user application storage.
- Following the user's Oct 22 requests, the desktop UI uses a dark research-console theme and a simpler first-run layout. Resource type and topic search lead the screen; provider, limit, timeout, expansion, and ranking controls are collapsed under Advanced search settings. Saved-search selection and Load stay visible; refresh, database, export, and download actions are grouped under Actions. The first-run detail pane explains how to start. Paper details prominently link to a provider-supplied landing, publisher, DOI, repository, or open-access page; when there is no provider-supplied direct file, the UI points users to that page as the alternative. No URL is constructed when the provider did not supply one. Menu-backed helper buttons remain hidden. The refreshed 1180×780 preview was captured and inspected at `.impeccable/review/desktop-clean-preview.png`.

### Verification evidence

- Luna ran `.venv\Scripts\python.exe -m unittest tests.test_gui -v` with `QT_QPA_PLATFORM=offscreen`: **21 tests passed, process exit 0**. Coverage includes the collapsed-by-default advanced section, Actions menu contents, and helper buttons staying hidden after the window opens.
- Full regression after the UI cleanup: `.venv\Scripts\python.exe -m unittest discover -s tests -q` — **268 tests, OK (skipped=1), process exit 0**. The skipped test requires Windows directory-symlink privileges. Luna also ran `compileall -q app/gui tests`, `tabnanny app/gui tests`, and `git diff --check`; all exited 0.
- PyInstaller onedir build succeeded in the isolated `build/phase15-gui` and `dist/phase15-gui` folders. A packaging issue was found and corrected: a host Poppler directory supplied incompatible version-suffixed ICU DLLs, so the build script now excludes Poppler PATH entries while building and restores PATH afterward. The final bundle manifest contains no bundled ICU DLLs.
- After this UI update, the actual `ResearchDiscovery.exe` remained alive for an 8-second startup smoke under the Qt offscreen platform with empty stdout/stderr, confirming it reached the event loop. The fresh Analysis manifest contains no ICU DLLs. The executable was rebuilt after adding the paper-page fallback and again remained alive for an 8-second offscreen startup smoke with empty stdout/stderr. This does not verify interactive flows in the frozen executable.
- Relevant implementation and packaging files: `app/gui/{__init__,__main__,main,controller,window}.py`, `app/core/{app_paths,export}.py`, `tests/test_gui.py`, `packaging/research_discovery_gui.spec`, `scripts/build-gui.ps1`, `pyproject.toml`, `PRODUCT.md`, `DESIGN.md`, `.impeccable/config.json`, and this README/tracker.
- No clean Windows VM is available in the current workspace. Clean-machine startup, certificate/network behavior, live API search, model inference from the packaged executable, and an actual file download remain unverified.

### Limitations and next gate

Model weights and databases remain outside the packaged bundle. The bundled build excludes ML dependencies; optional semantic/hybrid scoring requires the `[semantic]` runtime and cached/downloadable model files. No live n8n integration is required for this milestone. Phase 15 is the final currently defined roadmap phase; stop after its report.

## Phase 14 — Optional n8n integration

**Status: PASS — optional local bridge implemented; Phase 15 is tracked above.**

### Implemented

- Added an optional standard-library HTTP bridge at `app/integrations/n8n/server.py`; the Python core and CLI searches continue to work without it.
- Added `research-discovery serve` / `python -m app serve`, with loopback-only binding, bearer token required (minimum 24 characters), configurable local DB and port.
- Added asynchronous `POST /jobs` and `GET /jobs/{job_id}` endpoints. Search work runs in a bounded two-worker pool with a bounded pending queue. Search results use existing OpenAlex, Crossref, combined search, deterministic expansion, persistence, and normalization services.
- Added strict body-size, topic, provider, limit, timeout, and expansion validation; authorization headers and request bodies are not logged. Job results are retained in memory for the most recent 256 completed jobs, while results are also saved in SQLite.
- Documented local n8n node setup, token handling, localhost limits for hosted/containerized n8n, and the intentional loopback restriction in README.
- Added an importable manual-trigger workflow at `app/integrations/n8n/workflows/research-discovery-search.json`. It submits configurable paper searches, polls at two-second intervals (up to 30 checks), uses a selectable n8n Bearer Auth credential, and includes no embedded secret. It returns success/failure or the still-running status at the poll cap.

### Verification evidence

- Focused bridge tests: **4 passed**. They cover missing/invalid tokens, invalid requests, unknown paths, loopback/token policy, and successful stubbed search submission, SQLite save, and job polling.
- `python -m compileall -q app tests` passed after focused tests.
- Full suite: **247 tests, 246 passed, 0 failed, 1 skipped** (existing Windows directory-symlink privilege limitation). Focused bridge suite: **4 passed** after final review.
- `python -m compileall -q app tests`, `python -m tabnanny app tests`, `python -m app serve --help`, and `python -m app --help` passed.
- Workflow JSON parsed and graph sanity was checked; actual import/execution in n8n remains unverified because no n8n account connection is available.
- No live provider request or real n8n instance was used.

### Limitations and next gate

The bridge is intended for a local n8n instance on the same machine. It does not provide TLS, persistence of job status across server restarts, cancellation, or connectivity to hosted n8n. Remote deployment requires an independently secured private network path; binding beyond loopback is rejected. The optional bridge is not required to use the CLI or desktop application.

## Phase 13 — Thesis Model Evaluation and Benchmarking

**Status: PASS — evaluation framework complete; real human-labeled evaluation pending. Phase 14 has not started.**

### Implemented

- Added reusable `app/core/evaluation.py` metrics and judgment/report utilities. Relevance grades are 0 (not relevant), 1 (partially relevant), and 2 (highly relevant); default binary threshold is 2, configurable to 1.
- Added `evaluate --template` to export title, description, keywords, resource type, provider, source links, query topic, and stable saved-resource IDs for manual annotation. Blank labels remain unjudged; a labeled row requires an annotator ID. CSV is the editable template and CSV/JSON judgments can be loaded.
- Added offline `evaluate --judgments` comparison for keyword-only, semantic-only, baseline hybrid (0.60/0.40/0.00), and any explicitly supplied hybrid weight configurations. All methods share the same saved search snapshots and labels. Semantic vectors are memoized across configurations during a run.
- Unjudged resources are removed before K is applied; precision denominator stays K. Recall denominator is relevant judgments inside the judged candidate pool, not all scholarly literature. Per-query recall/RR are undefined with no threshold-positive labels; NDCG uses graded labels and is undefined when ideal gain is zero. Aggregate metrics omit queries with no threshold-positive labels, with eligibility visible in each query/report.
- JSON/CSV reports include per-query and aggregate scores, ranked candidate IDs, dataset/code versions, evaluation timestamp, Python version, metric definition, model ID/revision/runtime, weights, saved-search provenance, annotator/notes, judgment-file SHA-256, full candidate snapshot, and candidate snapshot SHA-256. Existing saved history is read-only; no schema migration was required.
- Added [evaluation-methodology.md](evaluation-methodology.md) with the annotation procedure, formulas, unjudged handling, candidate-pool limits, synthetic arithmetic check, and empirical limitations.

### Verification evidence

- Phase 12 baseline confirmed before changes: **222 tests, 221 passed, 1 Windows directory-symlink privilege skip**. The host's default temp path caused access/cleanup errors on the first attempt; redirecting `TEMP` and `TMP` to a workspace-local directory reproduced the baseline successfully.
- Synthetic hand calculation for grades `[0, 2, 1]`, threshold 2: P@2 = **0.5**; judged-pool R@2 = **1.0**; RR = **0.5**; DCG@3 = **2.392789**; NDCG@3 = **0.659002** (ideal DCG = 3.630930). No synthetic label is presented as a human judgment.
- Synthetic CLI fixture exercises the same saved candidates across all three ranking modes, JSON/CSV reports, and metric differences. With the fake encoder, keyword-only yields P@1=1.0/RR=1.0, semantic-only P@1=0.0/RR=0.5, and default hybrid P@1=0.0/RR=0.5. It uses fabricated fixture labels solely to verify calculations and wiring; it is not an empirical experiment.
- Empirical evaluation is **pending**. No genuine human-labeled dataset was supplied; annotation templates are available for review. No provider API request or real model inference was used for this phase's benchmark tests.
- Luna: focused `python -m unittest tests.test_evaluation` — **21 passed**; full suite — **243 tests, 242 passed, 1 skipped**. `compileall` and `tabnanny` passed.
- Sol independently repeated the focused suite (**21 passed**) and complete suite in system Python and `.venv` (**243 tests, 242 passed, 1 skipped** in each). `python -m compileall -q app tests`, `python -m tabnanny app tests`, and `python -m app evaluate --help` passed. Full-suite runs used workspace-local `TEMP`/`TMP` because the default Windows temp location denied cleanup/access.

### Limitations and next gate

Metrics measure only the human-labeled saved candidate pool; recall cannot be generalized to the scholarly literature. Judgment quality, inter-annotator agreement, sample size, statistical uncertainty, and external validity remain unmeasured. Hybrid configurations can be compared but are not tuned or claimed optimal. Semantic evaluation requires local model weights; it does not make provider requests. **Phase 14 is not started; await explicit user approval.**

## Phase 12 — Hybrid Research Ranking

**Status: PASS** (Phase 12 complete; Phase 13 has not started and awaits explicit approval)

### Implemented

- Added the reusable, CLI-independent `app/core/hybrid_ranking.py` service. It reuses Phase 11's batched topic/resource encoder and raw cosine scores; embeddings are computed once per ranking invocation.
- Default experimental weights are semantic **0.60**, keyword **0.40**, and recency **0.00**. `--hybrid-weights SEMANTIC KEYWORD RECENCY` accepts finite nonnegative values summing to 1. Active weights are renormalized per result if a component is unavailable.
- Semantic cosine [-1,1] maps linearly to [0,1]. Lexical scoring measures topic-token coverage after Unicode NFKC normalization, casefolding, and punctuation/whitespace splitting; it removes a small English stopword list. Title, description, and keywords use 0.50/0.30/0.20 weights, renormalized over usable fields. No lexical dependency was added.
- Recency uses a 30-year horizon and scores in [0,1]; missing/invalid years receive neutral 0.5. Its default weight is zero, so older resources are not automatically penalized. Recency is not research quality.
- `--hybrid` explicitly requests sorted display and shows raw cosine, normalized semantic, keyword, recency, and final experimental scores. Saved SQLite resource ordering and provenance remain unchanged.
- Existing schema-v3 search metadata records ranking method/version, discovered model cache revision, model/runtime settings, weights, component ranges/settings, as-of year, original ordering, and per-resource scores. `history` marks hybrid searches; `show` displays scores by original position; existing CSV/JSON exports include ranking metadata. No migration was needed.

### Verification evidence

- Baseline confirmed before implementation: **200 tests, 199 passed, 1 Windows directory-symlink privilege skip**. The optional `.venv` and cached MiniLM model from Phase 11 were available.
- Luna added 22 deterministic ranking tests, including Unicode normalization, overflow-safe weight validation, and very old publication years. Focused suite: **22 passed**. Sol independently ran the complete suite under system Python and `.venv`: each ran **222 tests, 221 passed, 1 skipped**. `compileall`, `tabnanny`, and `python -m app search --help` passed.
- Real CPU model smoke test ran on the three-resource normalized fixture using cached `all-MiniLM-L6-v2` commit `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`. No research provider API search was run.
- Same-fixture comparison (topic `flood prediction`, as-of year 2026; semantic embeddings from the cached model):

  | Mode / weights (semantic, keyword, recency) | Order and scores |
  | --- | --- |
  | Keyword only (0, 1, 0) | “Flood prediction rainfall data” 0.6000; “Flood forecasting neural networks” 0.2500; “Coastal climate change” 0.0000 |
  | Semantic only (1, 0, 0) | Same order; normalized semantic 0.8488, 0.8164, 0.6384 (raw cosine 0.6975, 0.6328, 0.2768) |
  | Hybrid default (0.6, 0.4, 0) | Same order; final 0.7493, 0.5898, 0.3830 |

  Fixture fields: first resource description `River models`, keywords `hydrology; forecasting`, year 2024; second description `Rainfall gauges`, keywords `flood hazard`, year 2010; third description `Sea level observations`, keywords `ocean trends`, year 2020. The order remaining the same is the observed outcome, not evidence of improved accuracy or relevance.

### Limitations and next gate

Weights are experimental and were not evaluated against human judgments at the Phase 12 milestone. Lexical scoring does not stem, expand synonyms, or segment unspaced scripts; its small stopword list is English-only. Recency affects scores only when explicitly given a nonzero weight. The model cache commit is recorded when found, but the CLI has no revision-pinning option. Phase 13 evaluation framework is recorded above; empirical review remains pending.

## Phase 11 — Semantic Similarity Model

**Status: PASS** (Phase 11 complete; Phase 12 is recorded above)

### Implemented

- Added a CLI-independent embedding and cosine-similarity service in `app/core/semantic_similarity.py`. Its injectable backend supports deterministic tests; the optional Sentence Transformers backend lazily initializes and reuses a CPU model without importing ML packages during ordinary searches.
- Resource text uses the title, description, and keywords at most once, capped at 5,000 characters. It excludes author names, DOI values, identifiers, and URLs. Empty or textless result sets avoid model loading; missing resource text receives no score. Invalid/nonfinite embeddings and inference failures return a clear semantic error.
- Added `--semantic`, configurable `--model` and `--model-cache`, and opt-in `--semantic-sort`. Similarity values are cosine scores in [-1, 1]. Display order remains the provider/deduplicated order unless semantic sorting is requested; saved result positions are never reordered.
- Semantic scores and reproducibility details (method/version, model ID, runtime package versions, device, text cap, sort choice, provider identifiers, and scores) are merged into existing schema-v3 search metadata. Resource rows and historical resource snapshots are unchanged; no migration was required. Existing exports include this search-level metadata.
- Added the optional `semantic` dependency extra: `sentence-transformers>=6.1.0,<7`. First-time model retrieval needs internet access; Hugging Face's supported cache is reused. No paid service is used.

### Verification evidence

- Phase 10 baseline: **181 tests** (180 passed, 0 failed/errors, 1 Windows symlink privilege skip). Python was 3.14.3 on Windows 11 x64; no ML packages or model cache were initially present; about 79.7 GB disk space was free.
- Luna added 19 deterministic fake-backend tests. Focused run: **19 passed**. Sol independently ran `python -m unittest discover -s tests -v`: **200 tests, 199 passed, 0 failed/errors, 1 skipped**. Sol repeated the full suite inside the optional `.venv` with the same result. `python -m compileall -q app tests`, `python -m tabnanny app tests`, and both CLI help commands passed. Ruff is not installed.
- The optional dependencies installed in ignored `.venv` on Python 3.14.3: sentence-transformers 6.1.0, transformers 5.19.0, and PyTorch 2.14.1 (Windows CPython 3.14 wheel). MiniLM's approximately **90.9 MB** model weights were downloaded to the ignored `data/model-cache` and a real CPU inference smoke test succeeded: initial load **18.311 s**, four-text batch **0.036 s**, **384 dimensions**, process working set **527.9 MB** after load and **533.7 MB** after inference, 6 PyTorch threads. The service scored two synthetic records at **0.713401** and **0.389351**; these demonstrate actual score generation, not relevance. Measurements are from one local run.
- No provider API search was run. Mocked tests used a fixed fake encoder; they are not presented as ML performance evidence. The Windows cache emitted a symlink privilege warning and fell back to ordinary cache files; no system settings were changed.

### Limitations and next gate

Model output is a similarity signal, not a probability or an evaluated relevance judgment. Different model IDs or model revisions can produce different scores. Scores are derived on demand and added to search-level metadata; provider resource snapshots stay unchanged. Phase 12 is complete; Phase 13 awaits explicit user approval.

## Phase 10 — Query Expansion and Search Reliability

**Status: PASS** (Phase 10 complete; Phase 11 is recorded above)

### Baseline and plan

- The Phase 9 baseline ran 161 tests: 160 passed and 1 skipped because Windows refused directory-symlink creation with WinError 1314.
- Implementation adds a standalone deterministic keyword-rule service, one-search orchestration for OpenAlex/Crossref/Dataverse, search-level query provenance in existing schema-v3 metadata, bounded expansion/provider calls, and CP1252-safe terminal escaping. No provider or runtime dependency is added.
- Download confinement keeps the existing resolved-path check; tests will exercise that boundary without requiring elevated symlink privileges.

### Implemented

- Added deterministic rules for prediction/forecasting, modeling/modelling, uppercase AI/GIS/ML/NLP expansion, bounded method-for-task phrasing, and removal of `study of` / `research on` framing. Original validated topic remains first; duplicate variations are removed.
- Added one-snapshot expanded search orchestration, retaining query order, provider list, partial failures, record counts and deduplication counts in the existing search metadata JSON.
- Every stored generated query includes the rule name that produced it; the search is bounded to at most 5 queries and 10 provider search calls. Existing bounded retries and Dataverse per-record detail calls give a conservative ceiling of 3,465 HTTP attempts including urllib redirect limits.
- Added `--expand` and bounded `--expand-limit`, query/provider output, history/show metadata display, and full search metadata in CSV exports. Combined papers and Harvard Dataverse datasets use existing adapters.
- CLI stdout/stderr now escape unsupported output characters when a stream supports reconfiguration, preventing CP1252 title printing from crashing the command. Database and UTF-8 exports are unchanged.

### Verification evidence

- Pre-change Phase 9 baseline: `python -m unittest discover -s tests -v` ran 161 tests, 160 passed, 0 failed/errors, and the symlink privilege test skipped.
- Luna's focused query-expansion suite: 18 passed, 0 failed/errors/skips. It covers deterministic rule mappings, original-topic preservation, bounds, provider routing, stable deduplication, partial failures, saved query provenance, history/show/export behavior, and CP1252 output with Unicode storage/export.
- Sol independently ran the download tests using a writable workspace temp root: 33 total, 32 passed and the actual directory-symlink test skipped (WinError 1314). Windows drive/UNC/parent/absolute filename confinement and a mocked resolved-path escape test passed without symlink privileges. The downloader check was not weakened.
- Retrieval comparison with synthetic OpenAlex fixtures: original-only search made 1 provider request, retrieved 2 records, kept 2 unique resources, and removed 0 duplicates. Expanded search made 2 requests, retrieved 4 records, kept 3 unique resources, and removed 1 duplicate. This demonstrates deduplication accounting, not improved relevance.
- Sol independently ran `python -m unittest discover -s tests -v`: 181 tests, 180 passed, 0 failures/errors, and 1 skip (WinError 1314 directory-symlink creation). `python -m compileall -q app tests/test_query_expansion.py tests/test_downloads.py`, `python -m tabnanny app tests/test_query_expansion.py tests/test_downloads.py`, `python -m app --help`, and `python -m app search --help` all passed. The test suite used a workspace-local temporary directory to avoid the restricted OS temp location.
- No live API request was run; retrieval comparison used deterministic synthetic provider responses. The roadmap mentions Phase 19 tests in the task request, but this checkout defines phases only through 15; all available regression tests were run.

### Limitations and next gate

Phase 10 itself added no embeddings, learned expansion, relevance ranking, or new providers. The current repository roadmap ends at Phase 15; no Phase 19 test suite/feature exists. The real directory-symlink test remains skipped due platform privilege; equivalent resolved-path escape and malicious filename confinement tests passed. Phases 11 and 12 are complete; Phase 13 awaits explicit approval.

## Phase 9 — Safe Research File Download Manager

**Status: PASS** (Phase 9 complete; Phase 10 has not started)

### Implemented so far

- Added an explicit `download SEARCH_ID RESULT_NUMBER` command. It lists retained provider file metadata, requires `--file N` to transfer a file, and supports a configurable download root, timeout, and size cap. Files are categorized as papers or datasets and get source metadata sidecars.
- Added a standard-library streaming downloader. It validates HTTP/HTTPS URLs, rejects non-public DNS results, pins a vetted address for connection and preserves HTTPS certificate/SNI checks, validates redirects, limits response size, checks HTTP status and MIME metadata, detects obvious HTML responses, sanitizes names, uses same-directory temporary files and no-overwrite atomic hard-link finalization, and cleans incomplete files.
- OpenAlex, Crossref, and Harvard Dataverse file metadata are reused as-is. No guessed URLs, provider additions, schema changes, or access bypasses were introduced.

### Verification evidence

- Baseline was verified before implementation: `python -m unittest discover -s tests -v` ran 130 tests and passed (0 failures/errors/skips) with `TEMP`, `TMP`, and `TMPDIR` redirected to a writable workspace temp directory. The default Windows temp location is inaccessible in this sandbox.
- Luna's focused suite: 30 tests, 29 passed and 1 skipped (directory symlink test; Windows returned WinError 1314 because symlink privilege is unavailable). Sol independently ran the complete suite after integration and the final extension assertion: 161 tests passed, 0 failures/errors, 1 skipped.
- `python -m compileall -q app tests`, `python -m tabnanny app tests`, `python -m app --help`, and `python -m app download --help` passed. No linter is installed; no dependency was added.
- Live combined OpenAlex + Crossref search for `flood prediction` returned three unique results (one duplicate merged) and created exactly one saved search. Reading the saved search confirmed all three resources. Initial result rendering hit a Windows CP1252 UnicodeEncodeError on a provider title after the database write; this terminal encoding limitation is recorded. From the saved record, an explicitly selected OpenAlex/arXiv PDF downloaded successfully: 2,059,290 bytes, `%PDF-` signature verified, sidecar source URL and size match the bytes. This live check is separate from mocked HTTP tests.

### Limitations and next gate

Provider metadata does not guarantee access; unknown access is attempted only after explicit selection and HTTP 401/403 are reported as restricted. No cryptographic checksums are provided by the current providers/model. Windows did not grant directory-symlink creation privileges in the test sandbox, so that test skipped; lexical and resolved-path confinement tests still passed. Schema remained v3 and no migration was needed. Astra was not consulted because no additional consequential decision was required. Phase 10 has not started; await explicit approval.

## Phase 8B — Combined Academic Provider Search

**Status: PASS** (Phase 8B complete; Phase 9 not started)

### Implemented

- Added the CLI command `python -m app search TOPIC --type papers --provider all`. The reusable `search_all_and_save()` service searches OpenAlex then Crossref sequentially, using each existing adapter and normalizer; each provider is requested with the configured limit (maximum 20), then the stable DOI/provider ID/URL/conservative fallback deduplication runs and the final list is capped at that limit. OpenAlex remains the default; single-provider, dataset, and positional-topic commands remain supported.
- Combined output reports actual per-provider counts, the final unique count, and merged duplicate count. Resource display includes contributing source providers and all known links. A provider error produces a named warning and partial results; empty results are successful, not failures. If both providers fail, `CombinedSearchError` is returned before any search record is written.
- Added optional search-level metadata to SQLite schema v3 through an additive transactional v2→v3 migration. Each combined search remains one atomic history entry and stores status, provider counts, failures, requested limit, unique count, and merge count. Existing schema-v2 searches receive empty metadata; old timestamps and snapshots remain unchanged. `history`, `show`, and CSV/JSON exports expose saved partial status. Exports preserve normalized metadata, all links, provider identifiers and provenance, file records, and combined-search status.
- Corrected the Phase 8 report's erroneous Phase 17 reference. The documented roadmap ends at Phase 15; this checkout has no Phase 17 or Phase 18 implementation or separate test target. No existing Phase 18 functionality was found to alter.

### Verification evidence

- Pre-change baseline: the current checkout ran **116 tests**, all passing outside the sandbox (the Phase 8 report's **111** count is the earlier historical total). The sandbox attempt failed because Windows denied SQLite writes in test temporary directories; the elevated baseline run passed.
- Luna added synthetic combined-provider fixtures and integration/regression checks. Final `python -m unittest discover -s tests -v`: **130 passed**, 0 failures/errors/skips, 1.784 seconds. Focused `python -m unittest tests.test_combined_search -v`: **19 passed**, 0 failures/errors/skips. Coverage includes DOI variants/conflicts, nonduplicates and missing DOI, provenance, links/files, deterministic order and final limit, both directions of partial failure, both failures/no history, empty results, schema migration/snapshot preservation, one history record, CLI, and exports.
- Sol independently ran the final `python -m unittest discover -s tests -v`: **130 passed**, 0 failures/errors/skips, 1.017 seconds. Sol independently ran scoped `compileall`, `tabnanny`, `python -m app --help`, and `python -m app search --help`; all exited 0. `git diff --check` also exited 0, but the repository files are untracked, so it does not inspect their contents.
- No live API request was run for Phase 8B. Combined provider tests use synthetic normalized resources and mocked adapters. No dependency was added; no third-party linter was installed.
- Astra was not consulted; no consequential architecture or data-integrity decision required escalation.

### Limitations and next gate

Combined searches retrieve at most the requested number from each provider, then cap after deduplication; they may therefore return fewer than the requested number when the retrieved page contains duplicates. API metadata quality remains provider-dependent. Dataset search is still single-provider. Phase 9 safe file downloading has **not** started. Await explicit user approval.

## Phase 8 — Crossref Academic Paper Provider Integration

**Status: PASS** (Crossref integration complete; Phase 8B is documented above)

### Implemented

- Added a standard-library Crossref `/works` search adapter using `query` and `rows`, configurable result limit, positive finite timeout, and at most two bounded retries for HTTP 429/5xx and network failures. HTTP 400 and other non-transient HTTP errors produce provider errors. Malformed JSON and invalid response shapes are rejected clearly.
- Added `search --provider openalex|crossref`; OpenAlex remains the default, the original positional topic form remains available, and Harvard Dataverse dataset searches remain unchanged. Selecting a paper provider for a dataset search exits with a clear usage error (status 2). Crossref results use the existing presentation, SQLite save/history/show, and CSV/JSON export paths.
- Crossref normalization retains provider DOI identity, normalized DOI, supplied URL, authors, abstract text, available publication year (including partial date parts), supplied license URL, provider-supplied PDF link metadata, retrieval timestamp, complete original item metadata, and Crossref provenance. Abstract markup is parsed as text; script/style content is suppressed. Unsupported or absent metadata remains unknown. Publisher name and Crossref work type remain available in original provider metadata because the shared resource model has no dedicated properties for them. No DOI-derived landing page or PDF URL is fabricated.
- When `CROSSREF_MAILTO` is configured, requests include `mailto` and contact information in the User-Agent. The public REST API requires no paid access or key. No application dependency was added.
- Reused SQLite schema v2 and the existing DOI alias/provenance system; no migration was needed. The deduplication algorithm was not changed. Matching DOI observations keep both source provider identities, provenance records, and distinct supplied links. Conflicting DOI values remain separate. Historical snapshots remain unchanged.
- README documents provider configuration, commands, rate/retry behavior, metadata limits, and deduplication.

### Verification evidence

- Pre-change Phase 7 baseline: Sol ran `python -m unittest discover -s tests -v`: **92 passed**, 0 failures/errors/skips.
- Luna added `tests/test_crossref.py` and `tests/fixtures/crossref_success.json`. Focused command `python -m unittest discover -s tests -p test_crossref.py -v`: **19 passed**. Coverage includes successful/empty/missing metadata, multiple authors, incomplete dates, DOI normalization, markup, license and links/files, HTTP 400/429/500, timeout, malformed JSON, persistence, history, exports, provider selection, and Crossref/OpenAlex identity matching.
- Luna ran the full suite: **111 passed**, 0 failures/errors/skips. Sol independently reran `python -m unittest discover -s tests -v`: **111 passed**, 0 failures/errors/skips.
- `python -m compileall -q app tests`, `python -m tabnanny app tests`, `python -m app --help`, and `python -m app search --help` passed. CLI help lists the provider option and retains existing commands.
- Live Crossref smoke used the public API with topic `flood prediction`, limit 1, timeout 15 seconds. It returned “Rational Flood Methodologies” (DOI `10.1007/978-3-319-52356-9_5`). The sandboxed request was connection-refused; the approved unsandboxed read-only retry succeeded. This was a direct adapter smoke, separate from deterministic mocked test results.
- The previous report's reference to Phase 17 was erroneous. The documented roadmap ends at Phase 15; no Phase 17 or Phase 18 implementation or test target exists in this checkout. Phase 8 verification ran all 111 tests then present (the 92-test Phase 7 baseline plus Crossref coverage). Ruff remains unavailable and was not installed. Astra was not consulted because no consequential design decision arose.

### Limitations and next gate

Crossref metadata varies by publisher deposit. Abstract, publication dates, licenses, and full-text links may be absent or incomplete. `application/pdf` identifies a supplied link's media type; the application does not fetch or verify its access. Publisher and work-type values are retained in original metadata rather than dedicated fields. Provider aggregation had not been implemented at the end of Phase 8 and is completed in Phase 8B above. No further provider, query expansion, or ranking phase has started.

Phase 8 was complete before the separately approved Phase 8B milestone. Phase 8B completion and the Phase 9 approval gate are recorded at the top of this tracker.

## Phase 0 — Repository inspection and planning

**Status: PASS** (documentation and inventory complete; Phase 1 not started)

### Verified inventory

- This is a new Git repository on unborn `master`: `git status --short --branch` reported `## No commits yet on master` with `AGENTS.md` and `SKILLS.md` untracked before the Phase 0 documentation was added. No user work was overwritten.
- Before documentation, the only project files were `AGENTS.md` (14,689 bytes) and `SKILLS.md` (9,908 bytes), plus `.git`. There was no README, `docs/`, application code, dependency manifest, or test suite.
- `python --version` and `py --version` reported Python 3.14.3. `python -m pip --version` reported pip 25.3 from `C:\Python314\Lib\site-packages\pip`. A global package listing had 125 lines; it does not define project dependencies.
- `python -m unittest discover -v` reported `Ran 0 tests in 0.000s` and `NO TESTS RAN` (exit code 1). This confirms tests are absent; it is not a passing product test.
- No research API check, dependency installation, or project test was run in Phase 0.

### Verification and environment issue

- Sol read the actual `AGENTS.md` and `SKILLS.md`, independently checked repository state, Python/pip, and test discovery. Luna independently read the files and repeated the inventory and test discovery without editing files.
- Ordinary sandboxed `exec_command` intermittently failed before process creation with `Failed to create unified exec process: helper_unknown_error: setup refresh had errors`, including for trivial commands in both PowerShell and cmd. Read-only outside-sandbox commands succeeded. The evidence points to the command setup/helper path, but its root cause has not been established. If it recurs, retry a fresh terminal/runtime and report the exact error; do not treat it as a repository or Python test failure.

### Phase 1 plan (not started)

1. Add project packaging/configuration and a minimal CLI entry point, preferring standard-library `argparse` unless evidence calls for another dependency.
2. Accept a research topic; reject blank and oversized topics; provide `--help`, useful errors, and predictable exit status.
3. Add unit tests for parsing and validation. Run available lint/static checks and smoke-test the entry point. Record exact results and stop at the Phase 1 gate.

No API, database, n8n, GUI, ML, or download logic belongs to Phase 1.

**Next gate:** Phase 1 was explicitly approved by the user and is recorded below.

## Phase 1 — Minimal Python CLI foundation

**Status: PASS** (Phase 1 complete; Phase 2 not started)

### Implemented

- Added setuptools packaging in `pyproject.toml` for Python 3.10+, with a `research-discovery` console script and no runtime dependencies.
- Added an `argparse` CLI runnable with `python -m app`. It accepts one topic, shows `--help`, confirms valid input, and uses exit status 2 with a clear stderr message for missing, blank, or oversized topics.
- Added a CLI-independent `app.core.topic.validate_topic` function. It strips surrounding whitespace, rejects blank topics, and limits normalized topics to 200 characters.
- Added focused standard-library unit tests and documented setup, use, behavior, and the phase gate in README.

### Verification evidence

- Luna created `tests/test_cli.py` and `tests/test_topic.py`. `python -m unittest discover -s tests -v` reported `Ran 12 tests in 2.493s` and `OK`: 12 passed, 0 failed, 0 skipped. Luna's CLI smoke checks showed exit 0 for help and valid input, exit 2 for blank and 201-character input, and the expected diagnostics. `python -m compileall -q app tests` and `git diff --check -- tests` exited 0.
- Sol inspected both test files and the implementation, then independently ran `python -m unittest discover -s tests -v`: `Ran 12 tests in 0.526s`, `OK` (12 passed, 0 failed, 0 skipped). Sol also ran `python -m compileall -q app tests` and `python -m tabnanny app tests`, both exit 0; `python -m app --help` and a valid-topic call exited 0 with expected output. `git diff --check` exited 0, although this new repository's files are still untracked, so that check does not cover them.
- Sol built the local wheel with `python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist`: exit 0, `Successfully built research-discovery-cli`. Inspection of the wheel confirmed it includes the `app` modules and maps `research-discovery` to `app.cli.main:main`. The first sandboxed attempt failed because pip could not write its temporary build tracker; the approved retry outside that sandbox restriction succeeded. The generated `dist/` and build metadata are ignored by Git.
- Ruff, Flake8, pycodestyle, and Pylint are not installed; no third-party linter was run or installed. Standard-library compilation and indentation checks passed.
- No live research API check was run. No API credentials, database, n8n service, ML component, or network integration was used.

### Limitations and next gate

The CLI confirms and validates a topic but does not retrieve research results. Third-party lint coverage remains unavailable in the current environment. Phase 2 is the OpenAlex provider and has **not** started. Await explicit user approval to proceed.

## Phase 2 — OpenAlex Provider Integration

**Status: PASS** (Phase 2 complete; Phase 3 not started)

### Implemented

- Added a standard-library OpenAlex Works adapter, independently callable from Python. It requests one page of up to 20 works, keeps presentation out of the adapter, and returns a small Phase 2 paper structure with title, publication year, and an API-supplied landing page or OpenAlex ID URL.
- Added configurable `--limit` (1–20) and `--timeout` (positive finite seconds) CLI options. Existing topic validation, help, and their exit codes remain. The CLI prints results, handles missing fields and empty searches, and exits 1 with a clear stderr message on provider failure.
- Added a maximum of two retries by default for HTTP 429/500/502/503/504 and network/timeouts, with bounded exponential backoff. HTTP 400 and malformed JSON fail without retry. No new runtime dependency was added.

### Verification evidence

- Luna added `tests/fixtures/openalex_success.json`, `tests/test_openalex.py`, and expanded `tests/test_cli.py`. Luna's final `python -m unittest discover -s tests -v` exited 0: `Ran 28 tests in 0.265s`, `OK` (28 pass, 0 fail/error/skip). Tests cover success/query parameters, empty results, missing metadata and URL fallback, 400, 429 retry and exhaustion, 500 retry bounds, timeout, malformed JSON, limit validation, CLI output/options/errors, and Phase 1 validation.
- Luna ran `python -m compileall -q app tests`, `python -m tabnanny app tests`, and `python -m app --help`; each exited 0. Ruff, Flake8, pycodestyle, and Pylint were not installed, so third-party lint was not run. `git diff --check` exited 0 but did not inspect the untracked files in this new repository.
- Sol inspected Luna's fixtures/tests and the implementation, then independently reran the full unittest suite (25 tests at the first review, all passed), compilation, and tabnanny checks. Sol reviewed the three later targeted tests and reran the final 28-test suite before the phase report.
- Live check: the sandboxed `python -m app --limit 2 --timeout 8 "urban heat islands"` attempt exited 1 due to a connection refusal. An approved outside-sandbox retry exited 0 and returned two OpenAlex results: “Urban heat island” (1992), source `https://doi.org/10.1080/01431169208904271`, and “City size and the urban heat island” (1973), source `https://doi.org/10.1016/0004-6981(73)90140-6`. Live results are separate from the synthetic unit tests.

### Limitations and next gate

The CLI displays one page only and does not yet retain full metadata, provenance, history, or exports. Network availability and OpenAlex rate limits can affect live searches. Phase 3 will introduce canonical resource models and provenance, but **has not started**. Await explicit user approval.

## Phase 3 — Resource Normalization and Provenance

**Status: PASS** (Phase 3 complete; Phase 4 not started)

### Implemented

- Added provider-independent typed `ResearchResource`, `ResourceLink`, and `DownloadableFile` structures with paper/dataset and link kinds. Fields retain title, reconstructed abstract, authors, publication year, provider, external ID, normalized DOI, keywords, UTC retrieval time, reported license/access, links, direct-file metadata, and selected original OpenAlex metadata. Missing values remain `None`; explicit empty author/keyword arrays remain empty tuples.
- OpenAlex `search_papers()` now returns normalized resources. It requests the needed documented fields and filters to paper-related work types. Provider-supplied landing, DOI, publisher, repository, OA, and explicit `pdf_url` links are classified separately; the raw URLs remain unchanged. An explicit `pdf_url` yields PDF metadata but is not independently opened or downloaded. A location's own reported license/access is retained on its links; the resource-level license comes only from the primary location.
- The CLI still shows title, year, and a returned landing-page or OpenAlex ID URL. Existing topic validation, options, empty/error handling, and exit codes remain. No new dependency or future-phase service was added.

### Verification evidence

- Luna expanded synthetic fixtures and updated provider/CLI tests. The final `python -m unittest discover -s tests -v` exited 0: `Ran 37 tests`, `OK` (37 passed, 0 failed/errors/skipped). Coverage includes complete and missing metadata, multiple/empty authors, abstract, keywords, DOI normalization, multiple classified URLs, provider-supplied PDF metadata, unknown licensing/access, malformed inputs/URLs, paper filtering, dataset type mapping in the model, and Phase 1/2 CLI and retry behavior. A targeted malformed DOI URL test initially failed on an invalid port; Sol fixed URL parsing and Luna reran the suite successfully.
- Luna's `python -m compileall -q app tests`, `python -m tabnanny app tests`, and `python -m app --help` exited 0. Ruff, Flake8, pycodestyle, and Pylint were unavailable and were not installed.
- Sol inspected the tests, fixtures, model, adapter, and CLI, then independently reran the final full suite: `Ran 37 tests in 1.066s`, `OK` (37 passed, 0 failed/errors/skipped). Compilation, tabnanny, and CLI help also exited 0. The repository remains untracked, so Git diff checks cannot validate these files.
- Live check: a sandboxed `python -m app --limit 2 --timeout 8 "urban heat islands"` attempt failed with a network connection refusal. An approved outside-sandbox retry of the final paper-filtered request exited 0 and returned “Urban heat island” (1992), `https://doi.org/10.1080/01431169208904271`, and “City size and the urban heat island” (1973), `https://doi.org/10.1016/0004-6981(73)90140-6`. This was a live CLI check; the unit tests used synthetic fixtures.

### Limitations and next gate

OpenAlex metadata may be incomplete or inaccurate. The CLI still shows only title, year, and one source URL; it does not yet display all normalized provenance. Provider-supplied `pdf_url` values are classified but not checked for availability or downloaded. Only one page of up to 20 papers is retrieved. Dataset search, persistence, and export remain future phases. Phase 4 is local persistence and search history and **has not started**. Await explicit user approval.

## Phase 4 — Local Persistence and Search History

**Status: PASS** (Phase 4 complete; Phase 5 not started)

### Implemented

- Added built-in `sqlite3` storage with configurable path (`--db PATH`, default `data/research.db`). Parent directories are created when needed; `data/` remains Git-ignored. `PRAGMA user_version` starts at schema 1, with an ordered migration dispatcher and rejection of unsupported newer versions. Initial schema creation is transactional.
- Schema 1 has `searches`, `resources`, `search_resources`, `resource_links`, and `downloadable_files`. Searches retain normalized topic and timestamp. Provider ID, or DOI when ID is absent, is the stable within-provider resource key. Search-resource rows retain result position and a full normalized snapshot, including original provider metadata; later observations cannot erase earlier ones. Link and file metadata are also kept in dedicated tables.
- Added `app.core.search_service` operations `search_and_save`, `list_history`, and `get_history_resources`, callable without the CLI. Saving a search, resources, and associations is one transaction. SQLite/serialization failures roll back with a `StorageError`; an interrupted write rolls back and re-raises the interruption.
- The CLI still validates and displays results as before, now saving successful searches. Provider or storage failures exit 1; input validation retains exit 2. No external database or runtime dependency was added.

### Verification evidence

- Luna added `tests/test_storage.py` and updated `tests/test_cli.py`. The final escalated `python -m unittest discover -s tests -v` exited 0: `Ran 51 tests in 0.483s`, `OK` (51 passed, 0 failed/errors/skipped). Tests cover initialization/versioning, saving and reading searches/resources, provider-key reuse across searches with separate snapshots, missing metadata, links and file round trips, rollback on serialization failure and `KeyboardInterrupt`, reopening, invalid path, newer-version rejection without data loss, empty/provider-failure history, and Phase 1–3 CLI/provider regressions. The sandboxed test attempt failed because Windows denied temporary SQLite writes; the approved outside-sandbox run passed. Temporary test directories were cleaned.
- Luna ran `python -m compileall -q app tests`, `python -m tabnanny app tests`, and `python -m app --help`; all exited 0. Ruff, Flake8, pycodestyle, and Pylint were unavailable and were not installed.
- Sol inspected the new tests, schema, service, and CLI. Sol independently reran the full suite outside the write sandbox: `Ran 51 tests in 0.570s`, `OK` (51 passed, 0 failed/errors/skipped), and independently ran compilation, tabnanny, and CLI help (all exit 0). A temporary SQLite service smoke test saved and reloaded a normalized resource from a nested path.
- Live end-to-end check: Sol ran an actual OpenAlex search for “urban heat islands” (limit 2) through `search_and_save` into a temporary workspace database. The command exited 0: `results=2 history=2 restored=2 title=Urban heat island schema=1 foreign_keys_ok=True`. The temporary database was removed by its `TemporaryDirectory` cleanup. This check is separate from the synthetic unit tests.

### Limitations and next gate

History is available through the Python service layer; there is no CLI history command yet. A resource row keeps first-seen fields while per-search snapshots preserve later observations. Cross-provider deduplication and metadata merging are planned for Phase 6. The SQLite file is local to the configured path and is not encrypted. Phase 5 began only after the user's explicit approval below.

## Phase 5 — CLI Presentation and Export

**Status: PASS** (Phase 5 complete; Phase 6 not started)

### Implemented

- Expanded the CLI with `search TOPIC`, `history`, `show SEARCH_ID`, and `export SEARCH_ID --format csv|json --output PATH`. The original positional topic form remains supported.
- Search and saved-result views display numbered resources with available authors, year, abstract, DOI, provider, source and open-access URLs, reported downloadable files, license, and access status. Unknown values are labeled; absent data is not inferred.
- History lists saved search IDs, timestamps, and result counts. `show` retrieves the selected snapshot through the existing SQLite search service without a provider request. Missing/invalid IDs receive CLI errors; empty history/results are handled.
- CSV exports include normalized paper fields, provider IDs as resource IDs, multi-valued author/link/file/provenance cells encoded as JSON text, and UTF-8 Unicode. JSON exports retain normalized fields, nested links/files, retrieval metadata, and original provider metadata.
- Export names are sanitized, paths containing parent traversal are rejected, files are created exclusively (no silent overwrite), and filesystem errors are reported. Export only reads saved metadata; it does not download files. SQLite schema remains version 1; no migration or dependency was added.
- README now documents commands, exports, roadmap state, and limitations.

### Verification evidence

- Phase 4 baseline before edits: `python -m unittest discover -s tests -v` passed 51 tests when run outside the filesystem sandbox; the sandboxed baseline could not create temporary SQLite databases.
- Luna added 12 Phase 5 acceptance tests covering full/missing metadata, multi-link/file output, history, saved-result retrieval without searching, empty histories/results, invalid IDs/destinations, overwrite refusal, Unicode, and CSV/JSON contents. Luna ran the targeted Phase 5 tests (12 passed) and full suite (63 passed, 0 failures/errors/skips).
- Sol independently ran `python -m unittest discover -s tests -v`: `Ran 63 tests`, `OK` (63 passed, 0 failures/errors/skips), including the Phase 4 regressions. Three `ResourceWarning`s came from existing HTTP fixture cleanup; they did not affect test results.
- Sol ran `python -m compileall -q app tests`, `python -m tabnanny app tests`, and `python -m app --help`; each exited 0. The CLI help lists all commands and documents the positional topic compatibility.
- Sol inspected the acceptance tests and CLI code. CSV/JSON Unicode and nested/multiple link/file behavior are verified by the tests. Export safety checks cover invalid/unwritable destinations and refusing to overwrite existing files.
- Ruff is unavailable (`python -m ruff --version`: `No module named ruff`). It was not installed. No live OpenAlex API check was run for Phase 5; export/history use saved synthetic data.

### Limitations and next gate

CSV represents nested or multi-valued data (authors, access URLs, files, provenance) as JSON text within cells. `resource_id` uses the provider-supplied external ID when available; otherwise it remains blank rather than inventing an identifier. Export parent directories must already exist. Phase 6 began only after the user's explicit approval below.

## Phase 6 — Research Resource Deduplication

**Status: PASS** (Phase 6 complete; Phase 7 not started)

### Implemented

- Added provider-neutral identity matching in `app.core.deduplication`: normalize DOI URL/prefix/plain forms; match DOI first; then provider-namespaced external IDs; then canonical HTTP(S) resource URLs; finally, only exact normalized title + same known year + identical full author signature when neither observation has stronger identity. Similar titles, conflicting known DOIs, and different resource types remain separate.
- Canonical URL normalization lowercases scheme/host, removes default ports, fragments, and trailing path slashes, while keeping path case and query parameters intact.
- Duplicate resources retain first-seen ordering. Distinct links and downloadable-file records are accumulated. Complementary known metadata is filled, conflicting scalar metadata remains conservative, authors/keywords are unioned, and each provider observation retains its own identifier, retrieval timestamp, and original metadata through `ResourceProvenance`.
- Repeated searches use a schema v2 migration that adds indexed identity aliases and provider provenance. Existing search IDs, timestamps, snapshots, result order, and CSV/JSON exports are not rewritten. New observations link to an existing canonical resource ID, while current-search snapshots retain that search's observed fields and add accumulated links/files/provenance.
- Search results returned to the CLI are deduplicated before display. Existing search/history/show/export commands and positional topic search remain available. No provider, dependency, download logic, or ranking was added.

### Verification evidence

- Pre-change Phase 5 baseline: `python -m unittest discover -s tests -v` passed 63 tests (0 failures/errors/skips), including three existing HTTP fixture cleanup `ResourceWarning`s.
- Luna added 17 Phase 6 matching/history tests and a schema migration regression. The targeted deduplication suite passed all 17 tests. The migration test verifies that a seeded schema v1 database upgrades to v2 without changing saved snapshot JSON, search IDs/topic/timestamps/order/counts, and that DOI/provider/URL identities and provenance are backfilled.
- Investigation confirmed those warnings came from unclosed `urllib.error.HTTPError` response wrappers in final and retried error paths. `search_papers()` now closes each response before retrying or raising. Luna verified response handles close; the affected provider suite passed 20 tests without warnings.
- Sol independently ran the final full suite: `Ran 80 tests`, `OK` (80 passed, 0 failures/errors/skips), with no HTTP fixture cleanup warnings. `python -m compileall -q app tests`, `python -m tabnanny app tests`, and `python -m app --help` all passed. The v1-to-v2 migration is transactional and nondestructive for snapshots.
- Ruff is unavailable (`python -m ruff --version`: `No module named ruff`) and was not installed. No live provider check was needed; deduplication tests use synthetic resources and temporary SQLite databases.

### Limitations and next gate

- The migration does not consolidate legacy duplicate resource rows or alter old search associations; it backfills aliases and future observations resolve to a deterministic existing row. This preserves history exactly, though prior duplicates can remain visible in their original saved searches.
- URL matching intentionally preserves query parameters and path case to avoid risky equivalence assumptions. Title fallback requires complete year and author evidence; sparse records remain separate.
- Title fallback scans identifier-free local resource records and is suitable for the current small local database, but may need indexing if database size grows substantially.

Phase 7 — complete. Phase 8 has not started; await explicit user approval to proceed.

## Phase 7 — First Dataset Provider Integration

**Status: PASS** (Phase 7 complete; Phase 8 not started)

### Implemented

- Added a standard-library Harvard Dataverse provider using the documented Search API followed by Native API dataset lookup by persistent ID. The provider searches public datasets, caps each search at 20 results, accepts a configurable timeout/result limit, and retries transient network/HTTP 429/5xx failures at most twice.
- Normalized dataset titles, descriptions, creators, year, keywords, DOI, license, landing page, retrieval time, provider ID, links, files, and original API responses into the existing resource model. Missing provider data remains unknown. Provider observations are retained as provenance.
- Preserved returned file IDs, names, MIME types, filename-derived formats, restriction metadata, and documented `/api/access/datafile/{id}` endpoints. Restricted/public status is shown when the API reports it. The CLI does not request or download file contents and does not invent file landing pages.
- Added `search TOPIC --type papers|datasets`; papers remain the default, and positional topic searches still work. Dataset results use SQLite schema v2, deduplication, history, show, CSV, and JSON. No migration, runtime dependency, or combined-provider search was added.
- Updated README examples, provider metadata coverage, and limitations.

### Verification evidence

- Phase 6 baseline before edits: `python -m unittest discover -s tests -v` passed 80 tests (0 failures/errors/skips).
- Luna added synthetic search/detail/empty fixtures and provider/integration tests for normalization, DOI, missing fields, creators, keywords, file formats/restrictions, HTTP 400/429/500, bounded timeout retries, malformed responses, persistence/reopening, history, export, deduplication, and CLI routing. Luna's elevated full-suite run passed: `Ran 92 tests`, `OK` (92 passed, 0 failures/errors/skips). The focused Native API wrapper-level restricted-file test also passed.
- Sol independently ran the full suite: `Ran 92 tests in 1.234s`, `OK` (92 passed, 0 failures/errors/skips). `python -m compileall -q app` passed. Ruff is unavailable (`No module named ruff`); no dependency was installed.
- Live provider smoke: a real public Harvard Dataverse search for `climate change`, limit 1, returned “Climate Change Helplessness, Pilot Study”, DOI `10.7910/dvn/haep2k`, with two file records. The CLI saved and displayed the live dataset; `history` and `show` retrieved it from SQLite. JSON and CSV exports each contained the dataset and two file records; JSON retained the file ID and Harvard Dataverse provenance. Temporary smoke files were removed.
- Schema remained version 2. No migration was required. Astra was not consulted because no consequential decision arose.

### Limitations and next gate

Dataverse metadata completeness varies by record. File restriction/access flags are displayed only when present; otherwise they remain unknown. Documented file-access endpoints are links only and may reject restricted files. Dataverse instances configure rate limits individually, so throttling can still occur despite bounded retries. This provider targets the Harvard Dataverse instance and does not query other Dataverse installations. Combined paper/dataset searches and Phase 8 providers are not implemented.

Phase 8 authorization was granted; see the completed Phase 8 status at the top of this tracker.
