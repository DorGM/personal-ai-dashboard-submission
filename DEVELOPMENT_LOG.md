# Development & Evaluation Log

> **Public copy.** This is a sanitized copy of the private development record at private commit `90e1f34` (2026-10-18). Entry IDs, order and wording are unchanged except the redactions listed in [`docs/submission/redactions.md`](docs/submission/redactions.md). Commit SHAs refer to the private development repository; the public repository is a single-commit snapshot of the released code. Files cited here that are not in the public repository stay private: `RESUME_CHECKPOINT.md`, the owner directive and approval records under `docs/architecture/owner-*.md`, `docs/analytics/*` (the analytics worker's notes) and the submission drafts under `docs/submission/` other than the report. Engineering excerpts of the owner's directives are in [Appendix B](docs/submission/prompts/README.md).

Purpose: capture the **actual** Claude Code prompts, tool-assisted changes, design tradeoffs, checks, failures and improvements needed for the project presentation. Never invent transcripts, outputs, benchmarks, screenshots or dates.

## Entry format (duplicate per significant iteration)

### LOG-001 — [short event title]
- Date/time (Asia/Jerusalem): TBD
- Phase / task: TBD
- Actor: User / Main Claude / Architect / Coder / Verifier
- Prompt (exact or linked transcript excerpt): TBD
- Output / decision: TBD
- Human steering / refinement: TBD
- Evaluation / test and *observed* result: TBD
- Defect / limitation / accepted risk: TBD
- Evidence (commit, file, command, browser trace, screenshot): TBD

### LOG-002 — A-01 MVP scope approved (Option B, amended)
- Date/time (Asia/Jerusalem): 2026-10-16 13:47–13:54
- Phase / task: Phase A / A-01
- Actor: User, Architect (Main Claude)
- Prompt (exact): "Read CLAUDE.md, PRODUCT_SPEC.md and PROJECT_PLAN.md. Start Phase A with the Architect agent only. Propose the first architecture decision with alternatives and tradeoffs. Wait for my explicit approval before proceeding. Do not write application code."
- Output / decision: Architect proposed three scope options (A full spec, B full core with bounded agents + manual "Run now", C core first). Recommended B.
- Human steering / refinement: Owner approved B and amended it: each "Run now" click generates job and trend recommendations; allowed more than once a day, max 3 per day.
- Evaluation / test and *observed* result: N/A (design decision).
- Defect / limitation / accepted risk: Amendment changes the spec's one-report-per-day uniqueness for `job_reports` / `industry_trends`; to be resolved in A-03. 09:00 schedule: Owner chose "Drop 09:00" (13:56) — manual Run now only, max 3/day; this replaces spec §4's daily 09:00 runs.
- Evidence: PROJECT_PLAN.md A-01; docs/phase-a/A-01-mvp-scope-proposal.md

### LOG-003 — A-02 stack approved (Option A)
- Date/time (Asia/Jerusalem): 2026-10-16 13:55–13:57
- Phase / task: Phase A / A-02
- Actor: User, Architect (Main Claude)
- Output / decision: Architect proposed A (Python FastAPI + HTMX + SQLite), B (TypeScript Fastify + React + SQLite), C (Next.js + Prisma). Recommended A. After the 09:00 run was dropped, the scheduler library was removed from the proposal before approval.
- Human steering / refinement: Owner: "approve A".
- Evaluation / test and *observed* result: N/A (design decision).
- Evidence: PROJECT_PLAN.md A-02; docs/phase-a/A-02-stack-modules-api-proposal.md

### LOG-004 — A-03 schema approved with amendments
- Date/time (Asia/Jerusalem): 2026-10-16 13:58–14:06
- Phase / task: Phase A / A-03
- Actor: User, Architect (Main Claude)
- Output / decision: Architect proposed A (DB-enforced canonical keys), B (plain UNIQUE + app checks), C (expression indexes) and 12 edge-case defaults. Recommended A.
- Human steering / refinement: Owner approved A and amended E2 (LLM calorie estimate on intake and on manual meal edit), E5 (sleep any time, once by LLM, manual edits), E8 (3/day cap only for jobs/trends Run now).
- Defect / limitation / accepted risk: E2 adds an LLM call on manual meal edits — a scoped, user-initiated exception to the "LLM never writes existing records" rule; on LLM failure the meal saves with calorie_basis 'unknown'.
- Evidence: PROJECT_PLAN.md A-03; docs/phase-a/A-03-schema-proposal.md v2

### LOG-005 — E2 correction and A-04 approved
- Date/time (Asia/Jerusalem): 2026-10-16 14:07–14:13
- Phase / task: Phase A / A-03 (E2), A-04
- Actor: User, Architect (Main Claude)
- Output / decision: A-04 proposed D1 (A OpenAI web search / B jobs+news APIs / C search API), D2 (A named tunnel on own domain / B quick tunnel), S1–S12 defaults.
- Human steering / refinement: Owner corrected the Architect's framing of E2 as an exception: calorie changes must create new records, daily total = sum per date. Architect proposed append-only `meal_calorie_estimates` + `daily_calories` view; Owner confirmed "Estimates table" at 14:15. A-04: "D1 A, D2 No, approve S".
- Defect / limitation / accepted risk: Architect misframed E2 as an exception to the no-overwrite rule; corrected by user before any implementation. Quick tunnel means manual Google callback update per start and allowlist as sole gate.
- Evidence: PROJECT_PLAN.md A-03, A-04; docs/phase-a/A-03-schema-proposal.md v3; docs/phase-a/A-04-ai-sources-auth-proposal.md

### LOG-006 — A-05 UI choices approved; plan tasks aligned
- Date/time (Asia/Jerusalem): 2026-10-16 14:14–14:17
- Phase / task: Phase A / A-05
- Actor: User, Architect (Main Claude)
- Output / decision: Proposed V1 (English / Hebrew RTL / toggle), V2 (top tabs + mobile bottom bar / sidebar), V3 (light only / light+dark), plus tokens, test matrix, build order, storyboard.
- Human steering / refinement: Owner: "V1 i agree with English. V2 agreed. V3 agreed."
- Change: PROJECT_PLAN B-03, B-06, B-08, B-09 acceptance text updated to match approved A-01/A-03/A-04 decisions (Run now replaces 09:00 schedule; calorie estimates table).
- Evidence: PROJECT_PLAN.md A-05, B-03, B-06, B-08, B-09

### LOG-007 — GATE-A passed; Phase B authorized
- Date/time (Asia/Jerusalem): 2026-10-16 14:23
- Phase / task: GATE-A
- Actor: User
- Prompt (exact): "approve A-05, start Phase B"
- Output / decision: A-05 fully approved; implementation authorized starting with B-01 in the order approved in A-05.
- Evidence: PROJECT_PLAN.md GATE-A

### LOG-008 — B-01 first verification FAILED; defects fixed
- Date/time (Asia/Jerusalem): 2026-10-16 14:24–14:40
- Phase / task: Phase B / B-01 (and B-03 started)
- Actor: Coder (Main Claude), Verifier (independent subagent)
- Output / decision: Scaffold written (FastAPI factory, config, /healthz, base template + CSS tokens, start/stop/check scripts, .env.example, .gitignore, README). Coder's own run: ruff clean, mypy clean, 9 tests passed.
- Evaluation / test and *observed* result: Verifier FAIL. Real defects caught: (D1, High) quick-tunnel flow could not set PUBLIC_BASE_URL without a restart that changes the URL, which also let TEST_AUTH pass its localhost guard while publicly exposed; (D2) .env.example placeholder secret was exactly 32 chars and validated; (D4) start.ps1 reported success without a health check; (D5) pydantic errors echoed secret values; (D6) .gitignore gaps (.env.*, *.sqlite, *.docx, .playwright-mcp/, *.png); (D7–D10) minor. Verifier also flagged B-03 files appearing mid-run (process note; one B-03 test expected SQLite STRICT to reject '12' for INTEGER, which it losslessly converts — test corrected).
- Fix: tunnel-first start with PUBLIC_BASE_URL from the tunnel; health check and pid|start-time tracking with taskkill /T; placeholder/low-entropy secrets rejected; value-free SettingsError; .gitignore extended; mypy over app+tests+scripts; README execution-policy note. Coder rerun: ruff clean, mypy clean (15 files), 60 tests passed. Re-verification pending.
- Evidence: scripts/start.ps1, scripts/_proc.ps1, app/config.py, tests/unit/test_config.py, .gitignore

### LOG-009 — B-01 PASS on recheck; B-03 verification FAILED; fixes; tzdata approved
- Date/time (Asia/Jerusalem): 2026-10-16 14:30–14:40
- Phase / task: Phase B / B-01, B-03
- Actor: Verifier, Coder (Main Claude), User
- Evaluation / test and *observed* result: Verifier B-01 PASS (residual lows: PUBLIC_BASE_URL persisted in the PowerShell session; case-sensitive .doc/.docx ignore). B-03 FAIL: (1) daily_calories coverage required every meal to have calories, contradicting approved E3; (2) seed script crashed without tzdata on Windows (simulated) and left an empty DB; (3) canonical_url raised on malformed ports/IPv6; (4) '|' separator allowed key collisions; (5) job_reports slots could be freed by delete/re-date, estimates deletable; (6) migration runner race and COMMIT-in-file breaking atomicity.
- Human steering: Owner approved adding `tzdata` to the dependency budget (decision card "Add tzdata", 14:38).
- Fix: view matches E3; seed computes date before creating DB; canonical_url returns "" on malformed/non-http input and treats http/https alike; \x1f separator and invisible-char stripping; triggers make job_reports slots permanent/immutable (cascade from users still works) and estimates undeletable while the meal exists; CHECKs for non-empty canonical_key and calories required for known basis; migration runner rejects transaction statements and skips versions applied concurrently; start.ps1 restores PUBLIC_BASE_URL; .gitignore case-insensitive CV formats. Coder rerun: ruff clean, mypy clean, 75 tests passed. Re-verification pending.
- Evidence: migrations/001_init.sql, app/canonical.py, app/db.py, tests/

### LOG-010 — B-01 and B-03 PASS; session paused at safe checkpoint
- Date/time (Asia/Jerusalem): 2026-10-16 14:41–14:50
- Phase / task: Phase B / B-01, B-03; checkpoint
- Actor: User, Verifier, Main Claude
- Prompt (exact, abridged heading): "Safe Checkpoint & Session Handoff — I want to pause our work at the nearest safe checkpoint, strictly following all existing project rules…"
- Evaluation / test and *observed* result: Verifier round 3: B-01 PASS, B-03 PASS. ruff clean, mypy clean (15 files), pytest 75 passed. Residual lows L1–L4 recorded in PROJECT_PLAN B-03 and RESUME_CHECKPOINT.md; not fixed (no new work after pause request).
- Output / decision: Both tasks marked Done. No new task started. Phase A proposals copied into docs/phase-a/. RESUME_CHECKPOINT.md written. Files saved to the owner's folder; nothing committed or pushed to git (not authorized; folder is not a git repo).
- Evidence: PROJECT_PLAN.md B-01, B-03; RESUME_CHECKPOINT.md

### LOG-011 — Owner directive: data preservation, migration safety, URLs, GitHub checkpoints
- Date/time (Asia/Jerusalem): 2026-10-17 21:45 (request); work through the evening
- Phase / task: Phase B / F-01 (inserted before B-02)
- Actor: User, Main Claude (Architect + Coder), Verifier
- Prompt (exact opening): "I want you to begin working on the following requirements now. Before starting, read CLAUDE.md, RESUME_CHECKPOINT.md, PROJECT_PLAN.md, and DEVELOPMENT_LOG.md to restore the full project context and respect all previously approved architectural decisions. Continue using our established Architect → Coder → Independent Verifier workflow. 1. Data Integrity and Deletion Policy — Highest Priority …" Sections followed: 2 L2 mandatory verifier tests, 3 L1/L3 migration safety with genuine concurrency, 4 L4 URL validation and SSRF, 5 private repository with secret and personal-information hygiene, 6 commit/push checkpoints after each verifier PASS, 7 then B-02.
- Output / decision: ADR-004 written (supersedes A-03 E11 cascade). Defaults taken without escalation (no conflict with approved architecture): RESTRICT FKs + grant-guarded deletes; soft delete design deferred to B-04 for Owner approval; frontend URL validation deferred until forms exist; 001_init.sql amended in place because no database had ever been deployed.
- Human steering: Owner connected GitHub when the first repository attach failed ("GitHub connected").
- Evaluation: see LOG-012.
- Evidence: docs/architecture/ADR-004-data-preservation.md, PROJECT_PLAN.md F-01

### LOG-012 — F-01 verification: five FAIL rounds caught real bypasses; round 6 PASS
- Date/time (Asia/Jerusalem): 2026-10-17 evening
- Phase / task: Phase B / F-01
- Actor: Verifier, Main Claude (Coder)
- Evaluation / test and *observed* result:
  - Round 1 FAIL (208 tests; 1 flaky failure in 6 runs). B1: a migration line `;COMMIT;` evaded the keyword check and committed part of a migration. B2: `UPDATE meals SET rowid=77` renumbered a row (column-filtered triggers ignore rowid aliases). B3: concurrent first open crashed on `PRAGMA journal_mode=WAL` (database is locked), the flaky test. B4: a migration could drop guard triggers or rebuild a table without them. Plus 15 non-blocking findings (raw connections, SSRF edge cases, unicode hosts, config gaps, .gitignore gaps).
  - Round 2 FAIL (284 tests pass). Guard check matched trigger text: an extra auto-grant trigger, a `WHEN 0` guard, or a dropped identity trigger passed; app connections could CREATE TRIGGER.
  - Round 3 FAIL (306 tests pass). A guard whose WHEN clause was wrapped in double quotes normalized equal to the template, but SQLite reads it as a string constant, so the guard never fired.
  - Round 4 FAIL (313 tests pass). Regression from the round-3 fix: an extra trigger naming `"deletion_grants"` (any quoting) was not recognised as referencing the grants table.
  - Round 5 FAIL (320 tests pass). A migration could INSERT standing grants (or rebuild the grants table), making rows deletable by any later stray DELETE.
  - Round 6 PASS (2026-10-17): no remaining way found for a migration-created object or data to leave lasting power to delete or renumber guarded rows; R2–R5 repros (28) rejected; unicode-whitespace fuzz 0 bypasses; sanctioned permanently_delete works after valid migrations; app restart OK; startup refuses standing grants; 326 tests passed 5/5 runs.
- Fix: SQLite authorizers (app connections: no DDL, no guard-disabling PRAGMAs; migrations: no transaction control/PRAGMA/ATTACH/temp objects); identity triggers without column lists; WAL retry; guard registry with exact templates (quotes and literal case preserved); invariants for registration, FKs and REPLACE/IGNORE; raw case-insensitive check for any other trigger/view naming deletion_grants; grants table exact and empty before COMMIT and at every startup; DQS off on 3.12+; savepoint in permanently_delete; SSRF hardening (NAT64/6to4/Teredo, malformed redirects); IDNA canonical hosts; stricter config; static tests for raw connections and conflict clauses.
- Defect / limitation / accepted risk: listed in ADR-004 "Known limits" and the checkpoint report.
- Evidence: tests/integration/test_data_preservation.py, tests/integration/test_migrations.py; verifier probe scripts were kept outside the repository.

### LOG-013 — Owner answers: no company name in the log, keep GitHub username; B-02 directive
- Date/time (Asia/Jerusalem): 2026-10-17 22:59
- Phase / task: Phase B / F-01 follow-up, B-02
- Actor: User, Main Claude
- Prompt (exact, company name redacted at the Owner's request): "Great work. Regarding your two questions: 1. Remove the [company name] reference from DEVELOPMENT_LOG.md. [REDACTED: personal context] Keep the development history itself intact. 2. Keep my GitHub username in the README clone URL. That's fine. Continue with B-02 as planned, following our established Architect → Coder → Independent Verifier workflow. Make sure the Google OAuth implementation is fully configurable through environment variables, without hardcoded personal credentials or email addresses. The Verifier must explicitly test that repeated Google sign-ins preserve the existing user identity and all associated data, including relevant concurrent authentication scenarios. As agreed, after independent verification passes, update the documentation, perform security checks, commit, and push the verified checkpoint to GitHub. Please continue."
- Output / decision: The log's purpose line now says "the project presentation" (the only change to history; the company name is also redacted in the quote above). B-02 built within A-04 S7–S9: Authlib OIDC, allowlist binds `google_sub`, signed 8 h cookie session, CSRF middleware, localhost-only TEST_AUTH. Defaults taken without escalation: absolute (not sliding) 8 h session; CSRF token also accepted as a form field so sign-out works without JavaScript; TEST_AUTH users get a separate `test-auth:` identity so they can never act as a real Google user.
- Evidence: PROJECT_PLAN.md B-02; app/auth.py; app/main.py

### LOG-014 — B-02 verification: round 1 FAIL (test login trusted the Host header); rounds 2–3 PASS
- Date/time (Asia/Jerusalem): 2026-10-17 late evening
- Phase / task: Phase B / B-02
- Actor: Verifier, Main Claude (Coder)
- Evaluation / test and *observed* result:
  - Round 1 FAIL (372 tests pass). B1: `is_direct_local_request` read the hostname from the client-supplied Host header. With TEST_AUTH=1 and uvicorn bound to 0.0.0.0, a request from another machine sending `Host: localhost` received 303 and a `test-auth:` session. Non-blocking: `/login` returned a bare 500 when Google's discovery document was unreachable; the CSRF form replay answered later `receive()` calls with `http.disconnect`, emptying streamed responses under uvicorn; README said sign-out "ends the session" although a copied cookie stays valid; X-Real-IP / True-Client-IP / Via were not treated as proxy headers.
  - Data preservation held in round 1 already: repeated, changed-profile and concurrent sign-ins (threads, 6 OS processes x 4 threads on one database file, sign-ins during concurrent writes, two accounts at once) kept one user per Google ID with unchanged id and `created_at`, and every other table byte-identical.
  - Round 2 PASS (2026-10-17). New non-blocking finding, fixed before commit: when the client disconnected mid-form, the CSRF middleware passed the partial body on as complete (a raw-socket repro on uvicorn created a test user from a truncated form). Partial bodies are now dropped. Round 3 PASS: no route runs on a mid-body disconnect (raw socket on uvicorn), no regressions, 380 passed.
- Fix: TEST_AUTH now also requires a loopback peer address (`request.client`); more proxy headers refused; `/login` shows a 503 page and logs only the exception type; the replay hands later `receive()` calls to the server; README states the cookie-session limits; a form cut short by a disconnect never reaches the route. Five new tests, each confirmed to fail on the pre-fix code; 380 tests pass.
- Defect / limitation / accepted risk: see PROJECT_PLAN.md B-02 "Not completed / reason".
- Evidence: tests/integration/test_auth.py; verifier probe scripts kept outside the repository.

### LOG-015 — Owner directive: product direction change to Personal AI Analytics (planning-only session)
- Date/time (Asia/Jerusalem): 2026-10-18 00:22 (2026-10-17 21:22 UTC)
- Phase / task: Architecture re-scope (A-06, ADR-005); no implementation
- Actor: User, Main Claude, Architect
- Prompt (exact): recorded verbatim in `docs/architecture/owner-directive-2026-10-17-personal-ai-analytics.md` (opening: "I want to make a final architectural and product-scope update before pausing this session. This is an intentional product direction change …").
- Output / decision (Owner, binding):
  - Focus the product on analyzing the user's own life, activities, habits, progress and trends. This is a deliberate conceptual shift, explicitly *not* driven by tokens, cost or time.
  - External job discovery, recommendations and industry-trend retrieval are Deferred / out of MVP. History and tables are kept; manual job applications stay.
  - New core feature: a single runtime OpenAI function-calling analytics agent with read-only parameterized tools, backend-computed numbers, user isolation and limits, plus a deterministic Evidence Validation layer with the Owner's fallback message.
  - Already-approved constraints: no embeddings or vector DB, no LangGraph or multi-agent framework, end-to-end demo first.
  - Demo Mode: a committed synthetic fixture, a working copy on start/reset, a demo identity without Google, the verbatim no-key message, AI with the reviewer's own key.
  - Vertical slices in priority order 1–5.
  - Submission: public repo (later; no history rewrite or visibility change now), PDF report, recorded local video.
  - This session is architecture and planning only: no Coder or Verifier, no code, tests, schema, dependency or runtime-config changes.
- Human steering: the Owner asked for open decisions to be listed without blocking documentation of what is already decided.
- Evidence: owner directive file; ADR-005; PROJECT_PLAN A-06

### LOG-016 — Architect review: ADR-005 design and vertical-slice plan (proposed, not approved, not implemented)
- Date/time (Asia/Jerusalem): 2026-10-18 early morning
- Phase / task: A-06 / ADR-005
- Actor: Architect (read-only Claude Code agent), Main Claude (recorder)
- Output / decision:
  - **ADR-005 written:** supersession table, MVP and deferred scope, and a six-tool read-only catalogue. The tools are coverage, summarize, compare periods, daily series, relate metrics and list records, all on a `read_only` `app.db.connect` connection.
  - **Evidence ledger:** the model writes placeholders, never digits, and the backend renders every number.
  - **Validator:** rules V1–V14 with default thresholds, one repair turn then degrade, and the Owner's fallback message.
  - **Charts:** server-rendered SVG, with no new dependency.
  - **Demo Mode:** `APP_MODE=demo`, localhost-only, content-based mode exclusivity, per-mode cookies, a JSON fixture with SHA-256, date offsets shifted at import.
  - **Also:** synthetic dataset specification, security analysis, a cost formula with no hardcoded prices, an evaluation strategy (adversarial and golden sets, optional live run), and open decisions O-0…O-12.
  - **Plan:** new tasks B-13, B-14 and B-15; B-08/B-09 Deferred; slices S1 (B-04, B-14), S2 (B-05, B-07), S3 (B-13, B-15), S4 (B-06), S5 (B-10, B-11 optional, B-12).
- Human steering / refinement: none yet; the detailed design awaits Owner approval (O-0).
- Evaluation / test and *observed* result:
  - No tests run and no Verifier run, as planning only.
  - Main Claude checked one Architect claim against git history and corrected it. The claim was that commit metadata contains the Owner's email; all commits are authored `Claude <noreply@anthropic.com>`. ADR-005 O-12 now gives the real reasons for a fresh public repo: a company name in commit 42c21af, and private-session details in this log.
- Defect / limitation / accepted risk: `.env.example` keeps a stale "web search" comment because runtime configuration was off-limits this session; it is fixed in B-15. Lexical validator rules are English-only and best-effort.
- Evidence: docs/architecture/ADR-005-personal-ai-analytics.md; PRODUCT_SPEC.md v0.2; PROJECT_PLAN.md; supersession notes appended to docs/phase-a/A-01, A-04, A-05

### LOG-017 — Owner approves ADR-005 decisions; implementation session starts
- Date/time (Asia/Jerusalem): 2026-10-18 06:03
- Phase / task: ADR-005 approvals; Phase B resumes at B-04 (slice S1)
- Actor: User, Main Claude
- Prompt (exact, approvals part): recorded verbatim in `docs/architecture/owner-approvals-2026-10-18.md`. Opening of the message: "We are now resuming implementation from the planning checkpoint saved in the previous session. My objective for this session is to complete the revised Personal AI Dashboard MVP as far as possible, ideally through a fully working, independently verified end-to-end application ready for submission."
- Output / decision:
  - O-0 and O-1…O-8, O-10…O-12 approved as listed in ADR-005 §11.
  - O-9: offline mocked evaluations only; live paid calls need prior approval.
  - Work continues autonomously through S1–S5 with a Verifier PASS, commit and push per task.
- Evaluation: git state verified at e267617 (matches remote); 380 tests pass before any change.
- Evidence: owner-approvals-2026-10-18.md; ADR-005 §11; PROJECT_PLAN A-06, B-04

### LOG-018 — B-04 data operations: round 1 FAIL (empty CSRF token in macro forms), round 2 PASS
- Date/time (Asia/Jerusalem): 2026-10-18 morning
- Phase / task: Phase B / B-04 (slice S1)
- Actor: Coder, Verifier, Main Claude
- Output / decision:
  - Soft delete per O-1, repositories as the only write paths, history windows and a five-tab shell.
  - Coder's assumption accepted by the orchestrator: tasks accept past dates and up to today+14. Past tasks are needed for history and the completion rate.
- Evaluation / test and *observed* result:
  - **Round 1 FAIL.**
    - Real-browser forms could not add or edit: `{% from "macros.html" import ... %}` without `with context` left `csrf_token` empty, so the CSRF middleware returned 403.
    - The 607 tests missed it because they read the token from the meta tag instead of the form. This is a test-design blind spot found only by a black-box probe.
    - Playwright MCP could not start: there was no Chrome at its expected path, and Chromium refused to run as root without `--no-sandbox`. Main Claude added a wrapper script for the preinstalled Chromium; the environment only, not the repo.
  - **Fix:**
    - macros imported `with context`;
    - new `test_records_forms.py` submits each rendered form's own fields, and failed 13/13 with the fix reverted;
    - also fixed: 1-day out-of-range windows, invisible-only text, and 78 ResourceWarnings in tests.
  - **Round 2 PASS:** HTTP probes, concurrency, migration, and Playwright desktop and mobile journeys (see PROJECT_PLAN B-04). The Verifier also found a flaky pre-existing test that assumed one process applies every migration; Main Claude fixed the assertion and it passed 15/15.
- Evidence: PROJECT_PLAN B-04; tests/integration/test_records_forms.py; Verifier probes and screenshots kept outside the repository.

### LOG-019 — Owner re-prioritizes: B-06 deferred, 30-day fixture, parallel analytics workstream, submission as a workstream
- Date/time (Asia/Jerusalem): 2026-10-18 07:47 (04:47 UTC), during B-14
- Phase / task: Phase B / planning amendment; B-14 continues
- Actor: User, Main Claude (integration lead)
- Prompt (exact): recorded verbatim in `docs/architecture/owner-directive-2026-10-18-priorities.md`. Opening: "I want to update execution priorities and introduce controlled parallel development, while preserving the current B-14 progress."
- Output / decision:
  - **B-06 Deferred / Out of MVP:** manual entry (B-04) is sufficient. Runtime AI is concentrated on personal analytics and evidence-grounded insights. Recorded in PROJECT_PLAN, PRODUCT_SPEC §3, ADR-005 §2 and RESUME_CHECKPOINT; the text is kept and marked.
  - **Fixture spec:** ADR-005 §7 now specifies about 30 fictional days. The 90-day spec is kept, marked superseded. The fixture stays unapproved until the Owner's review. The running B-14 Coder was redirected; its other work is kept.
  - **Parallel workstream:** B-13/B-15 run on `feature/analytics-b13` in a separate checkout. Main Claude is the integration lead and the only worker that merges to `main`. File ownership, phases, the B-05 metrics contract and the merge protocol are in `docs/architecture/parallel-work-and-metrics-contract.md`.
  - **Submission:** treated as a first-class workstream. The final 45–60 minutes before the deadline are reserved for integration verification, packaging, documentation, report finalization and recording support.
- Evaluation: docs only. `main` at dc0cfa8 before this entry; B-14 work stays uncommitted and outside this commit.
- Evidence: owner-directive-2026-10-18-priorities.md; parallel-work-and-metrics-contract.md; ADR-005 §2, §7, §10; PROJECT_PLAN slice order, B-05, B-06, B-13, B-14, B-15

### LOG-020 — B-14 Demo Mode: Verifier PASS round 1
- Date/time (Asia/Jerusalem): 2026-10-18 morning
- Phase / task: Phase B / B-14 (slice S1)
- Actor: Coder, Verifier, Main Claude
- Output / decision:
  - Demo Mode per ADR-005 §6 with the Owner's 30-day fixture (§7, LOG-019). The Coder was redirected mid-task to the new spec; earlier work was kept.
  - Defect found by the Coder's own tests: the generator re-sampled missing days inside a list-comprehension condition; the ~10% missing-days test caught it.
  - Coder's notes for B-13: the pattern weeks are 7-day blocks from day −29, not ISO weeks; V7 "coverage" for event domains is measured as documented days in the domain.
- Evaluation / test and *observed* result: Verifier PASS with no blocking defects. 853 passed, 1 skipped ×3; black-box probes and Playwright at both viewports (see PROJECT_PLAN B-14). Nine polish items recorded for B-07.
- Evidence: PROJECT_PLAN B-14; tests/integration/test_demo*.py; screenshots kept outside the repository.

### LOG-021 — B-05 metric registry: Verifier PASS round 1
- Date/time (Asia/Jerusalem): 2026-10-18 morning
- Phase / task: Phase B / B-05 (critical dependency for the analytics workstream)
- Actor: Coder, Verifier, Main Claude
- Output / decision: contract §4 implemented exactly; four small deviations accepted by the integration lead (see PROJECT_PLAN B-05). The Coder was interrupted once by a usage limit and resumed from its saved state.
- Evaluation / test and *observed* result: PASS with no blocking defects; independent randomized calculator and mutation testing (see PROJECT_PLAN B-05).
- Note: the Owner reported the analytics branch is `feature/analytics-agent` (not `feature/analytics-b13` as first documented).
- Evidence: PROJECT_PLAN B-05; tests listed there.

### LOG-022 — Integrated MVP: B-07 + B-13/B-15 merged with B-05; integration Verifier PASS round 2; release 32d849a
- Date/time (Asia/Jerusalem): 2026-10-18 morning (Owner's delivery window)
- Phase / task: Phase B / integration (integration lead)
- Actor: Coder (B-07), analytics worker (B-13/B-15, separate checkout, branch `feature/analytics-agent`), integration Verifier, Main Claude
- Output / decision: B-07 built in a separate worktree on top of B-05 while B-05 was verified; analytics branch merged cleanly; handoff patches applied; integration fixes: tests adapted to `Settings.ai_enabled_flag` and demo startup rules, `create_app(analyst_router=)` injection (FastAPI's wrapped routers made route swapping impossible), fail-closed demo check in the Analyst connection, new demo-isolation regression tests including a reset; `.gitattributes` keeps the checksummed fixture bytes on Windows checkouts.
- Evaluation / test and *observed* result: round 1 FAIL (comparison and "Show data" tables overflowed at 390 px); fixed with scroll regions; round 2 PASS. Owner-required demo isolation PASS. Open design questions for the Owner: T5 filters (aerobic-only split) and zero-event comparisons (D-6). Not run: live OpenAI, golden set, PowerShell, tunnel.
- Evidence: PROJECT_PLAN B-07, B-13, B-15; screenshots kept outside the repository.

### LOG-023 — Submission package integrated
- Date/time (Asia/Jerusalem): 2026-10-18 10:05
- Phase / task: B-12 packaging (no new features)
- Actor: Submission worker (`feature/submission` f8b5c27), Main Claude
- Output / decision: submission branch merged (report HTML and PDF, public README draft, security/PII audit, release procedure, storyboard). Root README finalized from the draft. Report §1, §10, §15 and the cover filled from PROJECT_PLAN and Verifier results; PDF rebuilt with headless Chromium (17 pages, no placeholders). Windows findings from the submission worker's clean-clone run: W-1 (CRLF fixture checksum) fixed by `.gitattributes` in 32d849a; W-2 (`write_text` without encoding) and W-3 (symlink privilege) fixed in tests. Not re-run on Windows.
- Evaluation: full suite green, ruff and mypy --strict clean after the test fixes; secret/PII scan of the release tree; fresh single-commit snapshot built from `git archive` and started in Demo Mode (see the submission report).
- Evidence: docs/submission/; README.md.

### LOG-024 — Public release and submission upgrade
- Date/time (UTC): 2026-10-18 07:18–07:30 (release); from 08:22 (upgrade)
- Phase / task: B-12 submission
- Actor: User, Main Claude, Verifier
- Prompt (exact, abridged headings): "URGENT — FINAL PUBLIC RELEASE AND SUBMISSION …" (07:18 UTC) and "Mission: Finalize and Professionally Upgrade the […] AI Project Submission" (08:22 UTC), with a follow-up refining the prompt appendix and the video upload.
- Output / decision:
  - **Public repository** published with the Owner's explicit approval: a fresh single-commit snapshot of the release, authored by the Owner's GitHub noreply identity. This private history is unchanged.
  - **Upgrade:** sanitized copies of this log, the plan and the evidence index; a curated prompt appendix (10 interactions, labelled verbatim / excerpt / no original prompt); report v2.0 (restructured, stale statements corrected, explicit AI validation status); README top section.
  - **Video:** the Owner's recording, validated with ffprobe and a full decode, then re-encoded once to crop the browser top bar (the Owner chose "Crop top bar"; it showed personal bookmarks). Uploaded as a release asset, not committed. The original file is unchanged.
- Human steering: the Owner required approval before publishing private records or external assets, no force-push, and no history rewrite.
- Evaluation: Verifier round 1 FAIL (8 documentation defects), all fixed; Verifier fresh clone 1741 tests, 0 failures; Main Claude fresh clone 1740 passed, 1 skipped, ruff and mypy --strict clean. The Owner created release `submission-v1`; the asset was downloaded anonymously and its SHA-256 matched the reviewed copy. Live OpenAI and the golden set were not run.
- Evidence: public repository `docs/submission/`; release `submission-v1`.

## Decision register
| ID | Decision | Alternatives / tradeoff | User approval | Evidence |
|---|---|---|---|---|
| ADR-001 | Stack, data and auth architecture | Stack approved (A-02 Option A: FastAPI/HTMX/SQLite vs TS+React, Next.js); data approved (A-03 Option A, amended E2/E5/E8); auth approved (A-04 S7–S9) | APPROVED 2026-10-16 | docs/phase-a/A-02-stack-modules-api-proposal.md |
| ADR-002 | Runtime OpenAI agent architecture and scheduling | OpenAI web search (vs jobs/news APIs, search API); Run now only, 3/day, no scheduler | APPROVED 2026-10-16; **Deferred by ADR-005** (2026-10-17) | docs/phase-a/A-04-ai-sources-auth-proposal.md |
| ADR-004 | Data preservation and deletion policy | RESTRICT + grant-guarded deletes and migration invariants (vs cascades + convention); soft delete deferred to B-04 | Directed by Owner 2026-10-17 | docs/architecture/ADR-004-data-preservation.md |
| ADR-005 | Personal AI Analytics: scope, runtime analytics agent, Evidence Validation, Demo Mode | Single function-calling agent + deterministic validator (vs embeddings/RAG, multi-agent frameworks, LLM SQL); JSON fixture + local-only demo identity (vs committed .db, shared login); defers ADR-002 job/trend agents | Scope directed by Owner 2026-10-17; detailed design APPROVED 2026-10-18 (LOG-017); amended 2026-10-18 (LOG-019: B-06 deferred, 30-day fixture) | docs/architecture/ADR-005-personal-ai-analytics.md |
| ADR-003 | Temporary tunnel and demo security | Quick tunnel (vs named tunnel on own domain); S1–S12 | APPROVED 2026-10-16 | docs/phase-a/A-04-ai-sources-auth-proposal.md |

## Release evidence checklist
- [x] Architecture approvals and any reapprovals documented (A-01…A-05, GATE-A, tzdata amendment, ADR-004, ADR-005 scope; ADR-005 detailed design approved 2026-10-18, LOG-017; amendment LOG-019).
- [ ] Significant prompts and iterative corrections recorded, not reconstructed as fake verbatim output.
- [ ] Unit/integration/Playwright command results recorded with pass/fail details.
- [x] At least one real AI error/bug/regression documented if encountered (LOG-005 E2 misframing; LOG-008/LOG-009/LOG-012/LOG-014/LOG-018 verifier-caught defects).
- [ ] Explicit list of skipped tests and accepted risks.
- [ ] Recorded local demo video on synthetic data (dashboard, records, metrics, data entry, AI analysis, insufficient-evidence case; server and tunnel stopped afterwards).
- [ ] PDF technical report (ADR-005 B-12): concept and final scope (direction change as a deliberate shift), architecture and decisions, Claude Code methodology and Architect/Coder/Verifier roles, prompts and iterations, verifier-caught defects, testing and results, runtime AI and evidence validation, reflection, limitations, risks.
- [ ] Public repository (approach per ADR-005 O-12) with a clean secret and PII scan of tree and history.
- *Superseded (ADR-005):* "Single final PDF: executive summary; prompts/process; critical reflection; testing, omissions and risk; outcomes; demo."
