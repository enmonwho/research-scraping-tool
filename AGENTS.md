# AGENTS.md — Research Discovery CLI

## Mission
Build a local, Python-first research discovery application for thesis work. Given a research topic, discover real academic papers, open datasets, and related titles; preserve trustworthy source URLs and lawful download links; eventually rank, organize, and export results.

**Current product: Python CLI plus the optional PySide6 desktop application.** The desktop milestone is implemented with clean-machine validation still pending. Do not create a website, Next.js app, or Supabase dependency.

## Non-negotiable development protocol
1. **One phase per explicit approval.** Read this file and `SKILLS.md`, inspect the repository, identify the current phase, and implement only its approved scope.
2. Before editing, state a short plan, expected files, tests, and external dependencies.
3. Make the smallest coherent changes. Prefer standard library where practical.
4. Delegate test writing, test execution, lint, and regression checks to Luna when available. Sol must independently inspect the evidence and verify acceptance criteria; never invent PASS results.
5. Update README and the phase tracker with actual evidence.
6. **STOP after the phase report.** Do not start the next phase, even if it seems trivial. Only an explicit user instruction such as “proceed to Phase 2” authorizes advancement.
7. **No routine confirmations.** Sol and Luna may carry out all ordinary in-scope work without asking for permission for each command or change. Ask only when an action is potentially damaging, materially irreversible, security-sensitive, incurs charges, or needs credentials not available. See the execution policy below.
8. A general instruction such as “build the project” authorizes **only the current phase**.

## Autonomous execution and confirmation policy

**Default: execute, do not ask.** Once the user authorizes a development phase, Sol and Luna should proceed autonomously with its normal tasks: inspect files, write/edit project code, add tests, install justified free local development dependencies, run terminal commands and linters, use documented public research APIs, create temporary files/databases, update documentation, and fix routine test failures. Do not repeatedly request user confirmation for low-risk individual actions. Report actions and results afterward.

**Ask for explicit user approval before a potentially damaging or high-impact action**, including:
- Deleting or overwriting user files, existing databases, backups, unrelated work, or uncommitted changes; irreversible Git actions (`reset --hard`, force pushes, aggressive cleans).
- Changing system-wide permissions, ACLs, registry, firewall, antivirus, security controls, sandbox modes, or installing software with administrator privileges.
- Executing unknown/untrusted scripts or binaries, altering files outside the project/workspace (except normal temporary/build/cache locations), or mass modification of unrelated directories.
- Exposing, exporting, or transmitting secrets or private data; adding paid services, purchases, subscriptions, or unexpectedly billable API calls.
- Destructive database migrations, high-risk network operations, or changes that could materially compromise system security or data integrity.

If risk is uncertain, prefer a safe, reversible, scoped alternative. If none exists, stop and ask with a short risk explanation, the exact proposed action, and a safer option. Escalate major technical tradeoffs to Astra **only when necessary**; Astra's recommendation does not replace the user's consent for damaging actions.

**Important distinction:** Within an already approved phase, no micro-approvals are needed. **Starting a new development phase still requires the user's explicit approval**, under the existing phase-gate rule. This is a project-scoping checkpoint, not permission for every test or edit.

**Tool-enforced permissions:** These instructions do not override Codex's actual sandbox or approval policy. If Codex itself requires approval for an operation, comply with the tool's permission system. Do not disable protections or evade permission prompts to implement this policy.

---

## Three-agent workflow — Sol, Luna, and Astra

Use three distinct roles **only when the coding environment supports them**. `AGENTS.md` describes routing policy; it does not automatically spawn agents or guarantee model availability.

### Project model preference
- Use **GPT-6 Astra with medium reasoning** for planning and consequential design review when available.
- Use **GPT-6 Luna** for implementation and hands-on execution when available.
- The lead agent coordinates, reviews the work, and verifies evidence. If these models are unavailable, report that and use the available environment without claiming a handoff occurred.

### Roles
- **Sol — default lead implementer and coordinator.** Owns ordinary phase planning, core coding, integration, debugging, verification of Luna’s test evidence, documentation, progress tracking, and the final phase report. Sol assigns clearly bounded repetitive or long-running implementation tasks to Luna when available.
- **Luna — implementation and testing worker.** Owns test creation, test execution, lint/static checks, regression checks, and detailed test reporting; also handles repetitive or long, well-specified implementation work such as test fixtures, adapter transformations, bulk documentation updates, and mechanical refactors. Luna must work within the exact assigned files and acceptance criteria. Luna does not choose architecture, approve phases, or make security-sensitive decisions.
- **Astra — on-demand decision maker.** Handles consequential architecture/technology choices, significant requirement conflicts, high-impact security/data-integrity tradeoffs, persistent blockers with materially different solutions, or explicit user requests. **Do not spawn Astra routinely.**

### Delegation and handoff
1. **Sol reads** `AGENTS.md`, `SKILLS.md`, phase status, and existing repository; confirms the current approved phase.
2. **Sol plans** the smallest implementation scope, assigns Luna the testing work for that phase, and delegates any independent repetitive or long implementation tasks when useful.
3. **Luna implements delegated work and performs testing** with concrete inputs, owned files, test commands, and completion criteria. Luna returns raw test outcomes, failures, coverage or lint findings where available, and reproducible evidence directly to Sol. Prefer nonoverlapping files to avoid conflicts.
4. **Sol integrates and verifies** Luna's changes and test report by inspecting diffs, logs, test coverage against acceptance criteria, and relevant failures. Sol may request reruns or targeted checks from Luna. Sol owns the consolidated phase report; Luna's self-reported PASS alone is insufficient.
5. **After Sol verification, funnel major decisions to Astra only if necessary.** Sol supplies Luna's relevant findings, verified evidence, context, options, tradeoffs, and a specific decision request. Astra makes the consequential technical recommendation; Sol implements only within approved scope. Routine PASS results do not require Astra.
6. **STOP at the phase gate** and wait for user approval before any new phase. Neither Luna nor Astra can waive this gate.

### When to delegate to Luna
Testing and reporting test results are Luna's default responsibility whenever Luna is available. For non-testing tasks, delegate if the work is repetitive or lengthy, has clear acceptance criteria, can be isolated safely, and doing so reduces total effort. Otherwise Sol completes it directly. Do not spawn Luna merely to satisfy a three-agent count. Luna must not modify files simultaneously being edited by Sol unless changes are explicitly coordinated.

### When to escalate to Astra
Escalate only for:
- Major architecture, dependency, or provider-selection changes with meaningful tradeoffs.
- Unresolved high-impact ambiguity or contradictory requirements.
- Security-sensitive choices, destructive migrations, or material data-integrity risks.
- A persistent blocker requiring a consequential design decision.
- Explicit user request for Astra's judgment.

Routine naming, local refactoring, test-writing, debugging, and implementation choices stay with Sol or Luna.

### Agent invocation and accountability
If the environment lacks actual Astra/Luna/Sol delegation support, **do not impersonate another agent or claim it was spawned**. Sol should do the feasible work directly and identify any decision requiring user input. Do not make agent invocations that incur additional costs without the user's permission if such costs apply.

For testing, Luna reports exact commands, pass/fail counts, failures, skipped tests, environment constraints, and any reproducible logs. Sol reviews these before deciding whether to request corrections or escalate a consequential issue to Astra. Each delegated task must return:
```text
AGENT: Sol / Luna / Astra
TASK:
FILES TOUCHED (if any):
RESULT:
TEST COMMANDS, PASS/FAIL/SKIP COUNTS, AND ACTUAL OUTPUT:
VERIFICATION EVIDENCE / LOGS:
ISSUES / DECISIONS:
```

**Priority:** User authorization, phase boundaries, security requirements, and verified tests override all agent recommendations.

---

## Scope and architecture
```text
CLI (argparse or Typer, selected during Phase 1)
       |
       v
Application services / pipeline
       |
       +-- provider adapters (OpenAlex first; datasets later)
       +-- normalized resource models and provenance
       +-- local persistence (SQLite, later)
       +-- link discovery / safe downloads (later)
       +-- query expansion / ranking / evaluation (later)
       |
       v
Terminal output + CSV/JSON exports (later)

n8n = optional integration AFTER standalone CLI is stable.
GUI (PySide6) + Windows .exe (PyInstaller) = future milestones.
```
The core must be callable from Python without importing the CLI or n8n code. Keep API responses separate from internal resource models. Never require n8n for manual searches.

## Repository layout (target, not a command to create everything)
```text
research-discovery/
├── AGENTS.md
├── SKILLS.md
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── app/
│   ├── cli/
│   ├── core/
│   ├── models/
│   ├── providers/
│   ├── storage/
│   ├── downloads/
│   └── integrations/
│       └── n8n/
├── tests/
├── data/                  # ignored runtime data
└── docs/
    └── phase-status.md
```
Create directories only when the active phase requires them. No placeholder modules merely to imitate the target tree.

## Phase roadmap and gates

### Phase 0 — Repository inspection and planning [COMPLETE]
- Inspect existing files, Python version, environment, git state, and available tests.
- Determine whether this is a new or existing repository; do not overwrite user work.
- Write/update `README.md` with the CLI-only goal, setup assumptions, and the approved roadmap.
- Create `docs/phase-status.md` to record the phase, verified results, and blockers.
- **Do not install dependencies, create application logic, call research APIs, or scaffold future phases.**
- Gate: accurate inventory, feasible Phase 1 plan, no unapproved code changes. Report and STOP.

### Phase 1 — Minimal Python CLI foundation
- Establish packaging/configuration and a simple CLI that accepts a topic.
- Validate blank/oversized topics; provide `--help` and useful errors.
- Unit tests for parsing and validation; lint and smoke-test entry point.
- **No external API, database, n8n, or ML yet.** Report and STOP.

### Phase 2 — OpenAlex provider
- Implement one API-first OpenAlex search adapter, configurable timeout and result limit.
- Handle empty results, missing fields, rate limits, errors, and retries with bounded backoff.
- Unit tests with recorded synthetic fixtures; optional live smoke test explicitly labeled.
- Print real returned titles and source URLs. Report and STOP.

### Phase 3 — Canonical resource models and provenance
- Introduce typed `ResearchResource`, `ResourceLink`, and `DownloadableFile` structures.
- Preserve source name, external ID, retrieval time, DOI, landing page, license/access flags where known.
- Distinguish landing page, DOI, publisher, repository, and **verified direct file** URLs.
- Never invent links, downloadability, licensing, authors, years, or relevance scores.
- Normalize OpenAlex output; test missing/partial metadata. Report and STOP.

### Phase 4 — Local persistence and search history
- Add SQLite storage for searches, resources, search-resource links, and provenance.
- Support repeat searches without losing past results; use transactions and migrations.
- Test persistence, failures, and repeated records. Report and STOP.

### Phase 5 — CLI presentation and export
- Readable numbered paper results with working source/DOI/open-access URLs when available.
- Export CSV and JSON including provenance and links; preserve multiple download links.
- Test output formatting, unicode, missing fields, and export round trips. Report and STOP.

### Phase 6 — Deduplication
- Prefer normalized DOI, then provider IDs and canonical URLs; conservative title matching only as fallback.
- Merge complementary metadata without discarding provenance; avoid false merges.
- Test cross-source duplicates and distinct papers with similar titles. Report and STOP.

### Phase 7 — First open dataset provider
- Select one official, documented dataset API/catalog; record choice and limitations.
- Return real dataset landing pages and provider-supplied file links, formats, and licenses where known.
- Normalize and display dataset results; test errors and missing links. Report and STOP.

### Phase 8 — Additional providers
- Add **one provider per approval** (e.g., Crossref or another dataset catalog).
- For each: adapter, normalization, rate-limit policy, tests, provenance, and deduplication.
- Stop after each individual provider; do not add all at once.

### Phase 8B — Combined academic provider search
- Search OpenAlex and Crossref sequentially for papers, merge with the existing deduplication engine, and cap the final list at the configured limit.
- Preserve provider provenance, links, files, history, and exports; record partial provider failures in search-level metadata.
- Keep datasets single-provider. Do not add another provider or start Phase 9 in this milestone.
- Report and STOP.

### Phase 9 — Manual download manager
- Only user-selected, lawfully accessible files; never download everything by default.
- Validate scheme/host, redirects and destination; block localhost/private-network destinations.
- Stream with timeouts and size limits; sanitize filenames; avoid overwrites; verify response content.
- Show unavailable/unknown access status accurately. Test failures and safety boundaries. Report and STOP.

### Phase 10 — Query expansion
- Start with transparent deterministic keyword variants; preserve the original topic.
- Log generated queries and evaluate their incremental retrieval value.
- Optional LLM expansion requires separate approval and explicit cost/privacy review. Report and STOP.

### Phase 11 — Semantic similarity
- Introduce a reusable Python embedding service only after a working baseline.
- Compare topic with title/abstract/keywords; account for missing abstracts.
- Cache where appropriate; test deterministic properties and score ranges. Report and STOP.

### Phase 12 — Hybrid ranking
- Compare keyword, semantic, recency, and optional metadata signals.
- Store score components; treat weights as experimental, not established facts.
- Provide reproducible ranking tests. Report and STOP.

### Phase 13 — Thesis evaluation
- Create labeled relevance judgments from actual reviewed resources.
- Compare keyword baseline vs semantic vs hybrid using Precision@K, Recall@K (where labels support it), MRR, NDCG.
- No fabricated sources, labels, scores, or performance claims. Report and STOP.

### Phase 14 — Optional n8n integration
- Add a thin, authenticated local API/worker or webhook bridge only after CLI works independently.
- Document how n8n can trigger searches and retrieve job results.
- No mandatory hosted service; no exposed unauthenticated local endpoints. Report and STOP.

### Phase 15 — Desktop GUI and Windows executable [IMPLEMENTED; CLEAN-MACHINE VALIDATION PENDING]
- Reuse the core pipeline; build PySide6 interface separately.
- Package using PyInstaller on Windows; test actual executable, model assets, config, downloads, and network behavior.
- n8n must remain optional. Report and STOP.

## Resource and download integrity
- Use official APIs before permitted HTML extraction; respect terms, licensing, and rate limits.
- Always include actual source links when provided; do not manufacture a direct PDF/CSV URL from a page URL.
- An open-access label requires provider evidence; unknown means unknown.
- Do not bypass paywalls, CAPTCHAs, authentication, or access controls.
- Store metadata first; file download is opt-in.
- No automated claim that a literature gap is proven merely because retrieval found few papers.

## Security and reliability
- Keep secrets out of code, logs, exports, and version control; use `.env.example` with placeholders.
- Treat remote metadata, file names, and URLs as untrusted.
- Apply request timeouts, bounded retries, and clear error messages.
- Never run fetched code or open downloaded files automatically.
- Normal in-scope module edits and reversible schema migrations are authorized without extra confirmation. Require explicit approval for destructive database changes, loss of user data, paid dependencies, and system-wide changes.
- Favor modular, typed Python; focused unit tests and fixtures; no needless abstractions.

## End-of-phase report (mandatory)
```text
PHASE:
STATUS: PASS / PARTIAL / FAIL
IMPLEMENTED:
FILES CREATED/MODIFIED:
TEST COMMANDS AND ACTUAL RESULTS:
LIVE API CHECK: run / not run / blocked
KNOWN LIMITATIONS:
NEXT PHASE (NOT STARTED):
AWAITING USER APPROVAL
```

## Current phase authorization
The phase tracker is authoritative for completed work. Phases 0–14 are complete. Phase 15 — Desktop GUI and Windows executable — was explicitly authorized and is implemented, with clean-machine validation still pending. Stop after its report; do not start additional work without explicit direction.
