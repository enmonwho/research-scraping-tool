# SKILLS.md — Research Discovery CLI Agent Playbook

This file documents **workflows and capabilities**, not a list of features to build immediately. `AGENTS.md` controls which phase is authorized. If the two conflict, obey the stricter phase boundary.

## Agent workflow — delegation, verification, escalation
**Follow `AGENTS.md` for authority and phase boundaries.** These steps describe how to carry out that policy; they do not authorize additional work or guarantee the environment can spawn agents.

### Sol — lead implementer and coordinator (default)
1. Read the phase tracker and inspect the existing implementation before planning.
2. Define bounded tasks, owned files, deliverables, and acceptance criteria.
3. Implement core logic and integration, while delegating **test design, test execution, linting, and regression checks to Luna** whenever Luna is available.
4. Send repetitive or long, well-specified implementation work to Luna only if ownership is clear and delegation is useful.
5. Review all Luna changes and raw test evidence; compare the evidence against acceptance criteria, request additional targeted tests when needed, and prepare the final phase report.
6. Escalate to Astra **only if a consequential decision is actually necessary**. Sol should continue routine work without Astra.
7. Stop and await user approval at the end of the phase.

### Luna — repetitive implementation and test owner
- Receive a task brief stating active phase, exact scope, inputs, files owned, expected output, and acceptance criteria.
- Handle repetitive implementation work, fixtures, and mechanical changes when delegated.
- **Own testing by default:** create/update focused tests, run unit/integration checks as appropriate, lint/static analysis, regression tests, and phase-specific smoke checks.
- Report exact commands, outcomes, pass/fail/skip totals, failing cases, environment blockers, and relevant logs. Do **not** turn an unrun test into PASS.
- Return reproducible results directly to Sol. Luna does not approve phases or make major architectural or security decisions.
- Avoid simultaneous edits to Sol-owned files unless explicitly coordinated.

### Sol's verification gate
After Luna reports, Sol must inspect the code changes and test evidence, identify coverage gaps and failing cases, and either request corrections/retests or prepare the phase report. The final report distinguishes **Luna's test execution** from **Sol's verification**. Sol should not accept PASS solely because Luna says PASS.

### Astra — conditional decision only
Invoke Astra only for architectural choices, significant requirement conflicts, security/data-integrity decisions, destructive changes, consequential blockers, or an explicit user request. Sol provides the verified problem, options, tradeoffs, and narrow question. Astra returns a recommendation; user approval remains mandatory for out-of-scope work and advancing phases. **Do not automatically forward every successful test report to Astra.**

### Required Luna → Sol evidence template
```text
PHASE:
TASK / ASSIGNED FILES:
IMPLEMENTATION WORK COMPLETED:
TEST COMMANDS:
PASS / FAIL / SKIP TOTALS:
FAILURES AND REPRODUCTION STEPS:
LINT / STATIC CHECKS:
LOGS OR OTHER EVIDENCE:
BLOCKERS / LIMITATIONS:
RETURNED TO: Sol
```

### Sol → Astra escalation brief (only when needed)
```text
APPROVED PHASE:
DECISION REQUIRED:
VERIFIED FINDINGS (including relevant Luna results):
OPTIONS AND TRADEOFFS:
SOL'S RECOMMENDATION:
QUESTION FOR ASTRA:
USER APPROVAL NEEDED?:
```

### Availability fallback
If the development environment does not offer actual Luna/Astra agents, do not simulate an agent handoff or falsely label testing as independently verified. Sol performs the available work and reports who actually executed each check. Where a major decision cannot be resolved, pause for user direction.

---

## Skill 1 — Repository reconnaissance
**Use in Phase 0 and at the start of every later phase.**
1. Read `AGENTS.md`, `SKILLS.md`, `README.md`, and `docs/phase-status.md` if present.
2. Inspect files, git status, Python version, installed dependencies, and available tests.
3. Identify existing functionality before creating replacements.
4. Summarize what exists, what is missing, and exactly what the active phase will change.
5. Do not advance phases without approval.

## Skill 2 — CLI design
**Use from Phase 1.**
- Build terminal commands with clear help, validation, exit codes, and progress/errors on stderr.
- Keep core functions independent of terminal printing.
- Support a research topic as an argument; interactive prompting may be added when approved.
- Keep commands predictable and testable with no network dependency for parser tests.

## Skill 3 — Provider adapters
**Use from Phase 2; one provider at a time.**
- Read current official API docs and limits before implementation.
- Use HTTPS, timeouts, reasonable user agent/identification where required, and bounded retries.
- Keep raw API payload parsing inside the provider adapter.
- Return consistent domain objects; retain provider IDs and source URLs.
- Test 200, empty, 400, 429, 500, timeout, malformed and incomplete data.
- Clearly separate mocked tests from live integration tests.

## Skill 4 — Normalization and provenance
**Use from Phase 3.**
- Distinguish paper and dataset; retain title, authors, abstract/description, year, DOI, source, identifiers, license, retrieval timestamp.
- Represent links with a type: landing page, DOI, publisher, repository, open access, direct file.
- Track direct files with URL, format, MIME type, filename, and access status where verified.
- Missing metadata stays `None`/unknown; never fill it with plausible-looking inventions.
- Store origin and retrieval evidence for merged metadata.

## Skill 5 — Local storage and export
**Use from Phases 4–5.**
- Use SQLite transactions, migrations, uniqueness constraints where justified.
- Preserve search-to-resource associations and source provenance.
- Write CSV/JSON with stable column/field names; include original links.
- Support UTF-8 and escaping; test repeated searches and export round trips.
- Keep generated data in ignored runtime folders.

## Skill 6 — Deduplication
**Use from Phase 6.**
- Normalize DOI, then compare provider IDs and canonical URLs.
- Use cautious title similarity only when strong identifiers are absent.
- Preserve all valid source links and provider metadata.
- Test both duplicate and near-title-but-different-paper cases.

## Skill 7 — Dataset discovery
**Use from Phase 7.**
- Prefer documented government, institutional, and open-data APIs.
- Capture dataset landing page, license, file format, update date, and direct download URLs only if actually supplied.
- Do not confuse metadata pages with downloadable datasets.
- Add only one new provider per approved milestone.

## Skill 8 — Safe file downloading
**Use from Phase 9.**
- Require explicit user selection.
- Accept HTTPS/HTTP only; protect against SSRF including redirects to private, loopback, and link-local addresses.
- Validate filename/path; use size caps, streaming, timeout, temporary file, atomic move, and collision handling.
- Verify content type/response status and clean partial files on failure.
- Respect license and access permissions; do not bypass restrictions.

## Skill 9 — Query expansion and ML
**Use from Phases 10–13.**
- Keep a reproducible baseline before introducing embeddings.
- Log the original topic, expanded queries, model version, scoring features, and settings.
- Keep model inference separate from retrieval and presentation.
- Evaluate on real human-labeled examples; report metrics and limitations without exaggeration.
- Never describe an unvalidated score as a probability of correctness.

## Skill 10 — n8n automation
**Use only in Phase 14.**
- Keep Python CLI independently runnable.
- n8n invokes a thin API/worker rather than containing core algorithms.
- Store n8n workflow exports without secrets.
- Handle retries, job IDs, idempotency, and status; document local versus hosted connectivity.

## Skill 11 — Desktop packaging
**Use only in Phase 15.**
- Build PySide6 as an interface around the same Python core.
- Keep long searches off the GUI main thread.
- Package with PyInstaller on Windows and validate on a clean Windows environment.
- Test file paths, writable user data directory, model weights, certificates, API connectivity, and optional n8n integration.
- Do not claim the .exe works merely because the packaging command succeeds.

## Skill 12 — Testing, verification, and reporting
**Use every phase.**
- Luna designs/updates tests and runs actual unit, integration (if available), lint/static, regression, and phase-specific smoke checks.
- Luna includes exact commands, raw results, pass/fail/skip counts, relevant failures, and evidence in the handoff to Sol.
- Sol reviews the implementation and Luna's evidence against approved acceptance criteria, asks for targeted retests where necessary, and consolidates the phase report.
- Only involve Astra if Sol identifies a qualifying consequential decision; do not invoke Astra for routine testing or ordinary fixes.
- If a tool/agent/test is unavailable, label the check not run or blocked rather than claiming success.
- STOP after the report; only the user may approve another phase.

## Operating checklist
- [ ] Active phase explicitly authorized
- [ ] Existing files inspected
- [ ] Small plan stated
- [ ] No future-phase features introduced
- [ ] Links and source provenance preserved where applicable
- [ ] Security and error cases tested
- [ ] README/phase tracker updated
- [ ] Luna ran tests and returned actual evidence (or unavailability recorded)
- [ ] Sol inspected Luna’s evidence and verified acceptance criteria
- [ ] Astra invoked only for a qualifying decision (if any)
- [ ] Stopped for user approval
