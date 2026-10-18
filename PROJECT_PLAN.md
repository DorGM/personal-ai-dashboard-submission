# Personal AI Dashboard — Execution Plan

> **Public copy.** This is a sanitized copy of the private development record at private commit `f511f53` (2026-10-18). Entry IDs, order and wording are unchanged except the redactions listed in [`docs/submission/redactions.md`](docs/submission/redactions.md). Commit SHAs refer to the private development repository; the public repository is a single-commit snapshot of the released code. Files cited here that are not in the public repository stay private: `RESUME_CHECKPOINT.md`, the owner directive and approval records under `docs/architecture/owner-*.md`, `docs/analytics/*` (the analytics worker's notes) and the submission drafts under `docs/submission/` other than the report. Engineering excerpts of the owner's directives are in [Appendix B](docs/submission/prompts/README.md).

**Goal:** Small, polished, responsive local **Personal AI Dashboard with agentic data analytics**: SQL-backed history, aggregates and charts over the user's own records, Google identity, manual structured entry, and a natural-language analytics agent whose answers are checked by deterministic Evidence Validation; a synthetic Demo Mode for reviewers; delivered using Claude Code Architect → Coder → Verifier and a documented evaluation process (ADR-005).
*Superseded (ADR-005), original goal:* Small, polished, responsive local personal dashboard with SQL-backed history/aggregates, Google identity, OpenAI-powered structured entry and scheduled insights; delivered using Claude Code Architect → Coder → Verifier and a documented evaluation process.

**Current state:** PHASE B — Done: B-01, B-03, F-01 (foundational safeguards), B-02 (Google sign-in), all 2026-10-17. **Planning checkpoint 2026-10-18:** product re-scoped to Personal AI Analytics (Owner directive, ADR-005, A-06); B-08 and B-09 Deferred; new tasks B-13, B-14, B-15; vertical-slice order S1–S5 below. **ADR-005 approved 2026-10-18 (LOG-017). B-04 Done (dc0cfa8). B-14 Done (slice S1 complete); B-05 Done; B-07 Done; B-13/B-15 integrated; integrated MVP released on `main` at 32d849a (LOG-022). Owner directive 2026-10-18 04:47 UTC (LOG-019): B-06 Deferred / Out of MVP; 30-day synthetic fixture; parallel B-13/B-15 workstream on `feature/analytics-b13` under `docs/architecture/parallel-work-and-metrics-contract.md`, integrated into `main` only by the integration lead; submission is a first-class workstream.** Repository: private GitHub repo (see RESUME_CHECKPOINT.md); every verified task ends with a commit and push. IMPLEMENTATION AUTHORIZED 2026-10-16 14:23 Asia/Jerusalem (GATE-A). Architecture version: phase-a A-01 v1, A-02 v1, A-03 v3, A-04 v1, A-05 v1 (as amended and approved), ADR-004, ADR-005 (approved 2026-10-18; amended by LOG-019).

## Governance
- **Phase A (approval for every meaningful design decision):** Architect proposes; user explicitly approves/rejects; main Claude records exact decision and approved scope. No code, migrations, dependency installs or tunnel until user authorizes Phase B.
- **Phase B (autonomous task execution):** For each bounded task, Coder implements, Verifier independently tests, Coder fixes, Verifier reruns; main Claude records progress. Architecture/scope/security/data model/external service changes require human reapproval before proceeding.
- `Done` requires tangible acceptance evidence. Mark partial work `Blocked` or `Failed`, not `Done`. Never fake execution outcomes. Phase B must be gated on explicit written user approval of architect-produced decisions.
- For **every** task, fill in the record shown below. Add a `DEVELOPMENT_LOG.md` entry for meaningful prompts, decisions, regressions, or verification results.
- **Statuses:** Pending | In Progress | Blocked | Failed | Done | Deferred (out of MVP by Owner decision; history kept). A Verifier `PASS` is required before Phase B implementation task `Done`.

## PHASE A — Architect + human approvals

### A-01 — Confirm product boundaries, MVP acceptance criteria and UI journeys
- Owner: Architect; Reviewer: User
- Deliverable: Feature inventory, desktop/mobile journeys, explicit MVP exclusions, definition of done; no implementation.
- Acceptance: User approves scope and all material tradeoffs.
- Status: Done
- Completed: Option B approved (full core; job/trend agents bounded to one approved source each; manual "Run now"; explicit exclusion list). User amendment: "Run now" produces fresh job and trend recommendations on each click, max 3 runs per user per day.
- Scheduling decision: 2026-10-16 13:56 Asia/Jerusalem, Owner chose "Drop 09:00": no automatic scheduler; job and trend recommendations run only on "Run now", max 3 per day.
- Not completed / reason: The per-day uniqueness of `job_reports` and `industry_trends` snapshots must change to per-run (to be finalized in A-03).
- Verification / user approval: 2026-10-16 13:54 Asia/Jerusalem, Owner: "approve B. i want it to be in a way that the Jobs and trends are recommended each time the user clicked the button "Run now". its possible to do it more than once a day, but it can be up to 3 times a day."
- Evidence / files: docs/phase-a/A-01-mvp-scope-proposal.md (docs/phase-a/)

### A-02 — Propose stack, modules and API contracts
- Owner: Architect; Reviewer: User
- Deliverable: Justified frontend/backend stack; module boundaries; typed routes; repo structure; dependency budget.
- Acceptance: Explicit approval; no added services without justification.
- Status: Done
- Completed: Option A approved: Python, FastAPI, Pydantic v2, stdlib sqlite3 with hand-written parameterized SQL and numbered .sql migrations, Jinja2 + HTMX, Authlib Google OIDC, FastAPI background tasks for Run now (no scheduler library), pytest + Playwright MCP. SQLite as DB engine. Module boundaries, typed routes, repo structure and runtime dependency budget as in the proposal.
- Not completed / reason: —
- Verification / user approval: 2026-10-16 13:57 Asia/Jerusalem, Owner: "approve A"
- Evidence / files: docs/phase-a/A-02-stack-modules-api-proposal.md (docs/phase-a/)

### A-03 — Confirm SQL schema, uniqueness, aggregates and lifecycle
- Owner: Architect; Reviewer: User
- Deliverable: PK/FK/UNIQUE constraints, daily data semantics, manual edit/delete flows, SQL queries/indices, calibration of calorie estimates.
- Acceptance: User approves finalized schema and handling of edge cases/ambiguous records.
- Status: Done
- Completed: Option A (DB-enforced canonical keys, STRICT tables, CHECKs, FK on) with defaults E1, E3, E4, E6, E7, E9–E12 as proposed. Amended: E2 — LLM estimates meal calories at free-text intake and re-estimates on manual meal edit (calorie fields of that meal only, user-typed kcal wins); daily total recomputed by deterministic SQL on every meal add/edit/delete. E5 — sleep entered any time, once per date via LLM, unlimited manual edits; stored under wake-up date. E8 — 3/day cap applies only to Run now (jobs/trends); manual and LLM intake uncapped beyond uniqueness rules.
- Not completed / reason: —
- Verification / user approval: 2026-10-16 14:06 Asia/Jerusalem, Owner: "approve A, but E2 needed to be treated more deeply - i want the LLM to calculate the calorie each time a meal is added, edited or deleted on fly, which means that the LLM gets the prompt, extract items, and if it found a meal within the prompt, it calculate the calories and it will be added to the relevant field. also, E5 can be added anytime, the same as the other fields, and also, it can be added once by the LLM or can be edited manually. in E8 make sure that the database cannot be changed more than 3 times a day in the Jobs or trends only! it can be changed many times manually or multiple times by adding with the LLM (under the specifications of only one time per category in each item)"
- E2 correction: 2026-10-16 14:13 Asia/Jerusalem, Owner: "E2 isn't deliberate exception to the "AI never edits a saved record" rule, because that if a meal is added/changed or whatever, the llm opens a new record with the changed calories, which mean, each day's total calorie is represented by sum of calories per date." Proposed shape (append-only `meal_calorie_estimates`; `daily_calories` as SQL view) confirmed by Owner 2026-10-16 14:15 Asia/Jerusalem (chose "Estimates table" on decision card).
- Evidence / files: docs/phase-a/A-03-schema-proposal.md v3 (docs/phase-a/)
- Superseded: E11 (hard delete with ON DELETE CASCADE) is replaced by ADR-004 data preservation (Owner directive 2026-10-17): RESTRICT foreign keys, grant-guarded deletes, soft delete decided in B-04.

### A-04 — Approve runtime AI, external retrieval, scheduler and auth/security
- Owner: Architect; Reviewer: User
- Deliverable: OpenAI extraction/clarification interfaces, news/jobs sourcing and duplicate rules, scheduler offline behavior, Google OAuth/allowlist/session, stable tunnel callback, demo privacy, costs.
- Acceptance: Human approval of all external-access, security and automation decisions.
- Status: Done
- Completed: D1 Option A — OpenAI Responses API web search for jobs and trends, schema-validated, every URL checked, deduped by canonical key. D2 Option B — free Cloudflare quick tunnel (random URL each start; Google callback added manually each time; no Cloudflare Access). S1–S12 approved as written (model in env, structured intake, server-side clarification state, injection handling, limits, OIDC with state/nonce/PKCE, email allowlist binding google_sub, secure session + CSRF, .env secrets, privacy-safe logs, synthetic demo DB, start/stop scripts with manual key revocation).
- Not completed / reason: —
- Verification / user approval: 2026-10-16 14:13 Asia/Jerusalem, Owner: "for phase A-04: D1 A, D2 No, approve S"
- Evidence / files: docs/phase-a/A-04-ai-sources-auth-proposal.md (docs/phase-a/)

### A-05 — Approve visual system, testing matrix and implementation order
- Owner: Architect; Reviewer: User
- Deliverable: Mobile/desktop wireframes, design tokens, responsive/accessibility states, realistic Playwright journeys, test/skip plan, demo storyboard.
- Acceptance: User approves UI direction and ordered milestones.
- Status: Done
- Completed: V1 A — English UI, spec Hebrew messages verbatim, inputs/record text dir="auto". V2 A — desktop top tab bar, mobile bottom tab bar, shared "Add update" panel. V3 A — light theme only.
- Not completed / reason: —
- Remaining sections (tokens, testing matrix, implementation order, storyboard): 2026-10-16 14:23 Asia/Jerusalem, Owner: "approve A-05, start Phase B"
- Verification / user approval: 2026-10-16 14:17 Asia/Jerusalem, Owner: "V1 i agree with English. V2 agreed. V3 agreed."
- Evidence / files: docs/phase-a/A-05-ui-tests-order-proposal.md (docs/phase-a/)

### GATE-A — Explicit authorization to implement
- Owner: User; Recorder: Main Claude
- Requirement: A-01 through A-05 explicitly approved; record architecture version and user's exact approval before changing Phase B status.
- Status: Done
- Completed: A-01 through A-05 explicitly approved; Phase B authorized.
- Not completed / reason: —
- Verification / user approval: 2026-10-16 14:23 Asia/Jerusalem, Owner: "approve A-05, start Phase B"
- Evidence / files: docs/phase-a/A-01…A-05 proposals (docs/phase-a/); DEVELOPMENT_LOG LOG-002…LOG-007

### A-06 — Product direction: Personal AI Analytics (ADR-005)
- Owner: Architect; Reviewer: User
- Deliverable: Re-scope to analysis of the user's own data; runtime analytics agent with deterministic Evidence Validation; Demo Mode with synthetic fixture; vertical-slice plan; new submission format.
- Acceptance: Owner directive recorded verbatim; ADR-005 written; spec, plan, log, resume checkpoint and README consistent; open decisions listed; no code changed.
- Status: Done for scope and planning documents (2026-10-18, planning-only session, no Coder or Verifier run). Detailed design **approved 2026-10-18** (O-0…O-12 except O-9 live, see owner-approvals-2026-10-18.md).
- Completed: Owner directive saved verbatim (docs/architecture/owner-directive-2026-10-17-personal-ai-analytics.md). Architect review produced ADR-005 (supersession table, scope, six read-only tools, evidence ledger, validator rules V1–V14, final-answer schema, repair/degrade, SVG chart catalogue, Demo Mode isolation and identity, synthetic dataset specification, security analysis, cost/config, evaluation strategy, open decisions). PRODUCT_SPEC v0.2. Tasks B-04…B-15 re-specified; B-08/B-09 Deferred.
- Not completed / reason: Nothing implemented by design (Owner: "architecture and planning only"). The synthetic dataset content is not created (Owner supplies or approves it, O-8).
- Verification / user approval: Scope approved in the Owner's directive 2026-10-17 21:22 UTC. The Architect's detailed design is not yet approved; no Verifier run (planning only).
- Evidence / files: docs/architecture/ADR-005-personal-ai-analytics.md, owner directive file, PRODUCT_SPEC.md v0.2, DEVELOPMENT_LOG LOG-015/LOG-016

## PHASE B — Coder + independent Verifier (only after GATE-A)

### Vertical-slice execution order (ADR-005; Owner priorities 1–5, optimized by the Architect)
One task at a time; each task ends Coder → independent Verifier PASS → plan/log/resume updates → secret and PII scan → commit → push → SHA in the checkpoint report.

| Slice | Tasks | User journey delivered | Acceptance (summary) | Verification | Approvals | Exit evidence |
|---|---|---|---|---|---|---|
| S1 Data ops and safe demo access | B-04, B-14 | Reviewer runs `start.ps1 -Demo`, clicks "Explore demo data", sees synthetic 15-day history, adds and edits a record (and deletes one after O-1), resets demo data. Owner signs in with Google to an empty personal DB | per task texts | unit, integration (2-user isolation, mode exclusivity, fixture checksum), Playwright 1440×900 and 390×844 | O-0, O-1 (blocks delete), O-2, O-4, O-8 draft | Verifier PASS ×2, commit and push, plan and log |
| S2 Dashboard | B-05, B-07 | Browse four tabs, toggle History/Aggregates, change ranges, see charts with gaps, highlighted open applications, task colours | per task | unit (reference calculator), integration, Playwright both viewports, keyboard, states | O-3 | PASS, screenshots (synthetic only) |
| S3 Analytics agent | B-13, B-15 | Ask "Did I sleep more in the last month?" and get a grounded answer with chart and evidence. Ask about mood and get the fallback. Without a key, get the verbatim message | per task | unit, scripted-model integration, adversarial suite, golden offline set, Playwright; live only with O-9 | O-5, O-6, O-7, O-10; O-9 for live | PASS, golden report |
| ~~S4 Free-text intake~~ **Deferred (Owner, 2026-10-18, LOG-019)** | ~~B-06~~ | ~~Dated free text → receipt → clarification → duplicate message → meal edit re-estimate; the new records appear in the dashboard and in agent answers | per task | mocked API tests, Playwright; live smoke only with O-9~~ | — | — |
| S5 Integration and submission | B-10, B-11 (optional), B-12 | Full storyboard on demo data; packaging; report; video | per task | full Playwright pass, clean-clone install test, secret and PII scan | O-8 final, O-11, O-12, PDF review | PASS, public repo URL (once Owner publishes), PDF, video |

**Order (Owner, 2026-10-18 04:47 UTC, LOG-019):** main workstream **S1** B-04 → B-14 · **S2** B-05 → B-07; analytics workstream in parallel **S3** B-13 (schemas, ledger, policy, validator now; tools after B-05 is merged) → B-15; then **S5** integration verification, B-10 → B-11 (optional) → B-12. S4 (B-06) is Deferred / Out of MVP. Submission material (report outline, evidence index, README, PII audit, storyboard) is built alongside, and the final 45–60 minutes before the deadline are reserved for integration verification, packaging, documentation, report finalization and recording support only.
*Superseded order (2026-10-18 03:03):* **S1** B-04 → B-14 · **S2** B-05 → B-07 · **S3** B-13 → B-15 · **S4** B-06 · **S5** B-10 → B-11 (optional) → B-12. Rationale: the demo importer reuses B-04 write paths; Playwright and the dashboard need demo data; B-05's metric registry is shared by the dashboard and the analytics tools; the read-only agent (tested with a scripted model) is demonstrable before intake.


### B-01 — Minimal scaffold and configuration
- Owner: Coder; Reviewer: Verifier
- Acceptance: Approved structure, example env only, safe .gitignore, repeatable local startup, lint/type/test scripts.
- Status: Done
- Completed: FastAPI app factory, settings loader (.env + env vars; placeholder/low-entropy secrets rejected; value-free SettingsError; TEST_AUTH refused unless localhost), /healthz, base template + light-theme CSS tokens, scripts/start.ps1 (quick tunnel first, PUBLIC_BASE_URL passed to app, health check, pid|start-time tracking), stop.ps1, _proc.ps1, check.ps1 (ruff, mypy, pytest), .env.example (placeholders only), .gitignore (secrets, DBs, CVs in any case, screenshots, logs), README.
- Not completed / reason: —
- Verification: Verifier round 1 FAIL (10 defects, LOG-008), round 2 PASS with 2 lows, round 3 PASS (2026-10-16). ruff clean; mypy --strict clean (15 files); pytest 75 passed. Server started and /healthz, /, /static/app.css → 200, /docs → 404. NOT RUN: any PowerShell script on real Windows; Python 3.11.
- Evidence / files: app/config.py, app/main.py, app/web/, scripts/*.ps1, .env.example, .gitignore, README.md, tests/unit/test_config.py, tests/unit/test_app.py

### F-01 — Foundational safeguards: data preservation, migrations, URLs, repository
- Owner: Coder; Reviewer: Verifier. Directed by the Owner 2026-10-17 (before B-02).
- Acceptance: No automatic or unintended deletion (sign-in, updates, migrations, errors); no destructive conflict handling; deletion only through an explicit authorized path, atomic and scoped; migrations atomic, data- and guard-preserving and safe under real concurrency; central URL validation and SSRF protection; env-based config with no personal or secret defaults; private GitHub repository with verified commit/push.
- Status: Done
- Completed: docs/architecture/ADR-004-data-preservation.md (D1–D7, known limits). Schema: all FKs ON DELETE RESTRICT; `deletion_grants` + BEFORE DELETE guard on every user-owned table and users; identity triggers on every UPDATE (rowid aliases included). app/guards.py registry + exact trigger templates. app/db.py: connections enable foreign_keys + recursive_triggers, disable double-quoted strings (3.12+), retry WAL switch, and install an authorizer denying schema changes and guard-disabling PRAGMAs; migration runner rejects transaction control/PRAGMA/ATTACH (keyword check + authorizer), refuses temp objects, checks row counts, schema_migrations, guard invariants, integrity and FKs before COMMIT. app/deletion.py (only deletion path; savepoint). app/users.py `get_or_create_user` (ON CONFLICT DO NOTHING + SELECT + targeted UPDATE). app/urls.py (central syntax validation, Pydantic `HttpUrl`), app/net.py (SSRF: public-address check on every hop, max 3 redirects). app/canonical.py uses the validator and IDNA hosts. Config: stricter PUBLIC_BASE_URL, DATABASE_PATH, TEST_AUTH. Static tests ban OR REPLACE/OR IGNORE/REPLACE INTO/ON CONFLICT REPLACE|IGNORE|DO UPDATE and raw `sqlite3.connect` outside app/db.py. README rewritten; .env.example placeholders only; .gitignore extended.
- Not completed / reason: Soft delete and its UX (B-04, needs Owner approval of the design); confirmation dialogs and frontend URL validation (`<input type="url">`, server messages shown) when forms exist (B-04/B-07). Residual risks in ADR-004 "Known limits" and the checkpoint report (DNS rebinding; raw/external connections; row counts not contents).
- Verification: Independent verifier, 6 rounds. Round 1 FAIL (B1 `;COMMIT;` partial commit, B2 rowid-alias renumbering, B3 concurrent first-open crash, B4 migration could drop guards). Round 2 FAIL (text-matched guards could be weakened or supplemented; app connections could create triggers). Round 3 FAIL (double-quoted WHEN clause passed the template check). Round 4 FAIL (quoted table name in an extra trigger passed the deletion_grants reference check). Round 5 FAIL (a migration could leave standing rows in deletion_grants). Round 6 PASS (2026-10-17): no remaining way found for a migration-created object or data to leave lasting power to delete or renumber guarded rows; R2–R5 repros (28) rejected; unicode-whitespace fuzz 0 bypasses; sanctioned permanently_delete works after valid migrations; app restart OK; startup refuses standing grants; 326 tests passed 5/5 runs. Suite: 313 tests, ruff and mypy clean (Python 3.13.16, SQLite 3.45.1). Concurrency verified with independent processes (sign-in, migrations, first open, SIGKILL mid-migration). NOT RUN: full suite on Python 3.11 (stdlib DB probes only), PowerShell on Windows, browser checks (no UI yet), live network SSRF.
- Evidence / files: ADR-004, migrations/001_init.sql, app/{db,guards,deletion,users,urls,net,canonical,config}.py, tests/integration/test_data_preservation.py, tests/integration/test_migrations.py, tests/unit/test_{urls,net,config,canonical}.py

### B-02 — Users, Google OAuth and row-level authorization
- Owner: Coder; Reviewer: Verifier
- Acceptance: Google `sub` maps to users.id; sessions and allowed identity secured; all data belongs to authenticated `user_id`; unauthorized access rejected.
- Status: Done
- Completed: app/auth.py: Google OpenID Connect via Authlib (state, nonce, PKCE S256; ID token signature, issuer, audience, expiry and nonce checked); `admit` (a known `google_sub` is the identity; a new account needs a Google-verified email in `ALLOWED_EMAILS`, otherwise 403 and no row; users only via `get_or_create_user`); signed session cookie `dashboard_session` (HttpOnly, SameSite=Lax, Secure on https, absolute 8 h from sign-in, replaced at sign-in, cleared at sign-out); CSRF middleware for every POST/PUT/PATCH/DELETE (header or form field, 64 KB form limit, body replayed unchanged, a form cut short by a disconnect is dropped). app/main.py: `/`, `/login`, `/auth/callback`, `POST /logout`, and `POST /auth/test-login` only when TEST_AUTH=1, only from a loopback peer addressed to localhost with no proxy headers, allowlisted emails only, separate `test-auth:` identity. Clear pages for not set up, Google unreachable, cancelled, failed and denied sign-ins; logs carry no email, name or token. All Google settings come from environment variables; no credential, client id or personal email is in the code. Templates base/signin/home/message. DEVELOPMENT_LOG line 3 no longer names a company (Owner, 2026-10-17).
- Not completed / reason: Real Google sign-in not tried (needs the Owner's Google Cloud client; tests use a simulated Google with real Authlib and real signed tokens). Row-level authorization of data routes is applied with the first data routes in B-04 (no data route exists yet; `auth.current_user` is the dependency to use). Known limits (accepted for a single-owner MVP, documented in README): sign-out cannot revoke a copied cookie before the 8 h end; a new Google ID with an allowed email becomes a separate user; the server access log records the single-use `code`/`state` query string; Authlib logs the PKCE verifier at DEBUG (off by default); `httpx2`, which Authlib prefers, is installed but not declared (adding it needs Owner approval; Authlib falls back to `httpx`); SQLite runs on the event loop in `/auth/callback`; no anti-framing header yet (B-07/B-11).
- Verification: Independent verifier, 3 rounds. Round 1 FAIL (2026-10-17): B1 TEST_AUTH trusted the client-supplied Host header, so a remote client sending `Host: localhost` to a server bound to all interfaces got a test session; non-blocking: `/login` gave a bare 500 when Google was unreachable, the CSRF form replay cut streamed responses short, README overstated sign-out. Round 2 PASS (2026-10-17), with one new non-blocking finding fixed before commit: a form cut short by a client disconnect was passed on as complete (now dropped; round 3 PASS on real uvicorn). Evidence across rounds: 61 ID-token/flow probes (wrong aud/azp/iss/exp/iat/nonce, wrong key, unknown kid, alg=none, HS256 key confusion, tampering, state/code replay, cross-browser callback, open-redirect) all rejected; 52 session/CSRF probes; 61 data-preservation probes comparing every table byte-for-byte after 25 repeated sign-ins, profile changes, 24 concurrent threads, 6 processes x 4 threads on one database file, sign-ins during concurrent writes, two accounts signing in concurrently and 16 conflicting profile updates: one user per Google ID, ids and `created_at` unchanged, no row lost or altered, integrity and FK checks clean. Suite: 380 passed (3 consecutive runs), ruff and mypy --strict clean (26 files). NOT RUN: live Google, browser (Playwright), Windows/PowerShell, Python 3.11, real Cloudflare headers.
- Evidence / files: app/auth.py, app/main.py, app/web/templates/{base,signin,home,message}.html, app/web/static/app.css, tests/integration/test_auth.py, README.md ("Google OAuth" and "How sign-in behaves"), .env.example

### B-03 — SQL schema and constraints
- Owner: Coder; Reviewer: Verifier
- Acceptance: Domain tables per A-03 v3 (canonical_key UNIQUEs, STRICT, CHECKs, FK on; append-only `meal_calorie_estimates`; `daily_calories` as SQL view; `job_reports.run_number` 1–3), migrations, indexes; repeat inserts fail safely; migrations tested; synthetic fixtures available.
- Status: Done
- Completed: migrations/001_init.sql (17 STRICT tables + daily_calories view per A-03 v3; composite (parent, user) FKs; canonical_key UNIQUEs with non-empty CHECK; append-only meal_calorie_estimates via triggers; job_reports run_number 1–3 with permanent/immutable slot triggers; indexes), app/db.py (connect with FK/WAL, transaction helper, atomic migration runner), app/canonical.py (deterministic keys), app/demo_seed.py + scripts/seed_synthetic.py (synthetic data only). tzdata added to dependencies (Owner approved 2026-10-16).
- Not completed / reason: Known low-severity issues L1–L4 were resolved by F-01 (2026-10-17).
- Verification: Verifier round 1 FAIL (6 defects, LOG-009), round 2 PASS (2026-10-16). pytest 75 passed (41 schema integration, 20 canonical unit). Seed script run OK, including with only tzdata zones available; integrity_check ok; foreign_key_check empty; cascades verified with recursive_triggers on/off; concurrent migration probe 5/5 clean. NOT RUN: real Windows.
- Evidence / files: migrations/001_init.sql, app/db.py, app/canonical.py, app/demo_seed.py, scripts/seed_synthetic.py, tests/integration/test_schema.py, tests/unit/test_canonical.py

### B-04 — Data operations: manual create/edit/delete, history, minimal UI
- *Original title (before ADR-005):* Data APIs, manual edit/delete and history
- Owner: Coder; Reviewer: Verifier
- Slice: see vertical-slice order
- Acceptance:
  - **Write and read paths:**
    - Authenticated, user-scoped create, read and manual edit for weight, meals (+ `meal_calorie_estimates`), workouts, sleep, steps, articles, podcasts, movies, series, job_applications and tasks.
    - Writes go through Pydantic-validated repository functions with parameterized SQL, `WHERE id = ? AND user_id = ?` and recomputed canonical keys. These are the only write paths, reused by the B-14 importer and B-06.
    - Another user's id returns 404.
  - **Validation rules:**
    - Duplicates are rejected with the spec message.
    - Date rules follow A-03 E7.
  - **Meal calories:**
    - Manual kcal adds a new `user_provided` estimate row.
    - A description edit without kcal adds an `unknown` row (re-estimate arrives with B-06).
  - **History:** per domain, ≤ 15 days, default trailing 15 clipped to the earliest record. The undocumented-day placeholder is UI only, and every value shows its record date.
  - **Delete:** per the approved soft-delete design (O-1) through `app/deletion.py`, with confirmation.
  - **UI:** accessible server-rendered forms and lists inside the four-tab shell, functional on desktop and mobile.
  - **Scope limit:** no deferred-table routes.
  - **Tests:** unit and integration (isolation, CSRF, duplicates, date edges, ADR-004 suite green); Playwright 1440×900 and 390×844 for create, edit and history.
- Status: Done (2026-10-18)
- Completed:
  - **Migration 002:** `deleted_at` on the 11 MVP tables (ADD COLUMN only); the `daily_calories` view now excludes soft-deleted meals.
  - **`app/records/`:** a generic repository plus one module per domain, all Pydantic-validated. These are the only write paths, reusable by B-14 and B-06.
    - SQL is parameterized; every query has `user_id = ?`, and edits use `WHERE id AND user_id AND deleted_at IS NULL`.
    - Canonical keys are recomputed; duplicates show the Hebrew message.
    - A key held by a soft-deleted record offers Restore or Delete permanently.
    - Text that is only invisible characters is rejected.
    - Dates: Asia/Jerusalem today, no future dates; tasks allow past dates and up to today+14.
    - Meal estimates are append-only (manual kcal → `user_provided`; description edit → `unknown`).
  - **Deletion:**
    - Soft delete needs confirmation.
    - Recently Deleted page with Restore.
    - Permanent delete only after soft delete, on a separate confirm page with a required acknowledgement, through `app/deletion.py`.
  - **History:** a window of ≤15 days, defaulting to the trailing 15 clipped to the earliest record, with Earlier/Later and custom ranges. Undocumented days show the UI placeholder; values show their record date.
  - **`app/web/`:** routes and templates for a five-tab shell (Food & Health, Content, Jobs & Career, Tasks, Analyst placeholder):
    - open applications highlighted;
    - task colours;
    - top bar on desktop, bottom bar on mobile.
- Not completed / reason:
  - Visual polish, charts and aggregates are B-05/B-07 by design.
  - B-07 polish notes: favicon, RTL alignment of the placeholder, wrapping of the mobile Earlier/Later buttons, full window for an end near the lower bound.
  - Real Google sign-in is still untried (TEST_AUTH only).
- Verification:
  - **Independent Verifier, 2 rounds.**
    - Round 1 FAIL: D-1, the add/edit/status forms rendered an empty CSRF token, because the Jinja macros were imported without context, so every add and edit returned 403 in a browser. The tests had read the token from the meta tag.
    - Round 2 PASS (2026-10-18). Every rendered form submits with its own fields (303). Two-user isolation holds across all 11 domains. The full lifecycle works, with forged confirms refused, other rows byte-identical, grants empty and integrity/FK clean. Estimates are append-only, and the view excludes soft-deleted meals.
  - **Dates:** 20 hostile date params handled. The Asia/Jerusalem midnight boundary was checked. 46 invalid inputs rejected; HTML, script and SQL-ish text is stored literally and escaped.
  - **Canonical duplicates** checked. Migration 001→002 keeps rows, is idempotent and keeps the guards.
  - **Concurrency:** 12 same-key creates give 1 × 303 and 11 × 409; delete, edit, restore and purge races give no 500s.
  - **Playwright MCP:** 1440×900 and 390×844. Covered: sign-in, all tabs, add, duplicate, edit, delete, Recently deleted, restore, permanent delete, window navigation, task colours, open-application highlight, keyboard-only form, mobile bottom bar, no horizontal overflow on 9 pages. 23 screenshots, synthetic data only.
  - **Suite:** 690 passed; ruff and mypy --strict clean (50 files).
  - Also fixed: a pre-existing flaky test assumption in `test_concurrent_migrations_from_separate_processes` (now 15/15 passes).
  - NOT RUN: real Google sign-in, screen reader, contrast measurement.
- Evidence / files: migrations/002_soft_delete.sql; app/records/*; app/web/{deps,forms,records}.py; app/web/templates/{tab,form,confirm,deleted,analyst,macros,base}.html; tests/integration/test_records_{repo,web,forms}.py; tests/unit/test_record_dates.py
- *Superseded (ADR-005), original acceptance:* Authenticated create/read/manual-update/delete; date validation; missing-day placeholders only in UI; SQL 15-day history and filtered queries.

### B-05 — Metric registry and deterministic aggregate service
- *Original title (before ADR-005):* SQL aggregate service
- Owner: Coder; Reviewer: Verifier
- **Contract (2026-10-18):** implements exactly the interface in `docs/architecture/parallel-work-and-metrics-contract.md` §4, plus `app.db.connect(..., read_only=True)` (ADR-005 §3.3). The analytics workstream builds T1–T6 on it after B-05 is merged.
- Slice: see vertical-slice order
- Acceptance:
  - **Registry:** `app/analytics/metrics.py` holds the closed registry (ADR-005 §3.3) with fixed parameterized SQL, kind, unit and `min_delta`.
  - **Aggregates:**
    - documented-day denominators;
    - means over documented values only;
    - content totals;
    - weight first, last and delta (≥ 2 points), plus the latest value overall with its date;
    - calories exclude unknown totals and report complete and partial counts;
    - tasks by category and completion rate;
    - applications by status.
  - **Ranges:** validated (start ≤ end ≤ today), clipped to the earliest record, ≤ 366 days.
  - **Edge cases:** no NaN or divide-by-zero; explicit zero ≠ missing; soft-deleted rows excluded.
  - **Tests:** results equal an independent reference calculator on test data.
- Status: Done (2026-10-18, Verifier PASS round 1)
- Completed: `app/analytics/metrics.py` (16-metric closed registry, `resolve_period`, `coverage`, `daily_points`, `summarize`, `MetricError`) exactly per contract §4; `app.db.connect(..., read_only=True)` (query_only, deny-by-default authorizer, 2 s statement abort, refuses missing files).
- Accepted deviations (integration lead): read-only connections may *read* PRAGMA `application_id` (B-14 demo checks); no `industry` filter (free text, filters are closed enums); `tasks.completion_rate` also caps at today; `summarize` does not clip (callers use `resolve_period`).
- Not completed / reason: non-blocking: SQL functions are all allowed on read-only connections (fixed SQL only); non-Mapping `filters` raises AttributeError; completion-rate cap uses the wall clock.
- Verification: ruff and mypy --strict clean; 1016 passed, 1 skipped ×3. Verifier probes: 56 forbidden statements refused with DB bytes unchanged; 2 s abort; independent calculator 119,256 randomized checks + 53,704 demo-fixture checks, 0 mismatches; 5 injected mutations all caught; 2-user isolation; 12 hostile inputs rejected without echo; dataclasses and signatures match the contract.
- Evidence / files: `app/analytics/metrics.py`, `app/db.py`, `tests/unit/test_metrics_registry.py`, `tests/integration/test_metrics{,_read_only,_fixture}.py`; DEVELOPMENT_LOG LOG-021.
- *Superseded (ADR-005), original acceptance:* 15-day bounded default, custom valid range; denominator excludes undocumented days; numeric averages, content counts, weight endpoints; edge-case tests.

### B-06 — OpenAI structured intake, calorie estimates and clarification — **Deferred / Out of MVP**
- **Deferred by the Owner, 2026-10-18 04:47 UTC (LOG-019):** "B-06 (AI-powered free-text data intake) is now Deferred / Out of MVP. Manual data entry from B-04 is sufficient for this submission." Rationale: concentrate the product's runtime AI on personal analytics and evidence-grounded insights. The text below is kept for history; nothing is built; tables and calorie-provenance fields stay unchanged.
- Owner: Coder; Reviewer: Verifier
- Acceptance: Explicit dated natural language → validated structured proposals; uncertainty loop; per-item (partial) outcomes with receipt; DB denies overwrite; LLM calorie estimate inserted as a new `meal_calorie_estimates` row on meal add and on manual meal edit (user-typed kcal wins); tests use mocked provider; estimated calorie provenance shown.
- ADR-005 additions:
  - Keep the current acceptance, plus:
    - uses the `app/ai_client.py` ModelClient with injected mocks;
    - inserts only through B-04 repositories;
    - with AI disabled, shows the O-5 notice, makes no client call, and manual forms keep working;
    - in demo mode, writes only to the working copy;
    - the model in env needs structured outputs only (no web search);
    - live smoke only with O-9.
- Status: Deferred / Out of MVP (2026-10-18)
- Completed: —
- Not completed / reason: deferred by the Owner (LOG-019); not part of the submission
- Verification: —
- Evidence / files: —

### B-07 — Polished responsive dashboard with charts
- *Original title (before ADR-005):* Polished responsive dashboard
- Owner: Coder; Reviewer: Verifier
- Slice: see vertical-slice order
- Acceptance:
  - **Layout:** four sections per A-05, plus the Analyst entry per O-3.
  - **Controls:** History/Aggregates toggle and validated range controls. Aggregates come from B-05 only.
  - **Charts:** SVG macros (line with gaps, bar, comparison) with "Show data" tables.
  - **Visual rules:**
    - open applications highlighted;
    - task colours including white "Other";
    - record dates shown;
    - loading, empty and error states;
    - demo banner;
    - confirmation dialogs.
  - **Headers:** CSP and anti-framing.
  - **Accessibility:** WCAG 2.2 AA (keyboard, focus, contrast, labels).
  - **Tests:** Playwright at both viewports on demo data.
- Status: Done (functional MVP, 2026-10-18; Integration Verifier PASS on `integration/analytics` (round 1 FAIL: mobile table overflow; fixed in b6ecca8; round 2 PASS), released to `main` at 32d849a.)
- Completed: Aggregates view on Health/Content/Career/Tasks with 7/14/30-day and custom validated ranges; tiles, previous-period comparison (min_delta, "not enough data"), SVG line charts with gaps, bar and comparison charts with "Show data" tables; CSP, X-Frame-Options, nosniff; 503 page for DemoError; B-14 polish (favicon, compact mobile banner, reset-page header); wide tables scroll inside their card.
- Not completed / reason: no screen-reader walkthrough or contrast audit; plural wording ("1 workouts"); CSP lacks form-action/object-src; no loading states (server-rendered).
- Verification: full suite green ×2 (~1740 tests); Playwright 1440×900 and 390×844 on demo data (all tabs, presets, custom range, gaps, Show data, comparison; scrollWidth equals the viewport on all 12 aggregates pages after the fix; keyboard reaches scroll regions; no CSP violations); two-user isolation on aggregates.
- Evidence / files: `app/web/aggregates.py`, `app/web/headers.py`, `app/web/templates/{aggregates,charts}.html`, `tests/integration/test_dashboard_aggregates.py`; LOG-022.
- *Superseded (ADR-005), original acceptance:* Four sections/tabs, dates and provenance, history/aggregate controls, open applications highlighted, tasks colored, accessible desktop and mobile layouts, loading/empty/error states.

### B-08 — Run now: job reports and deduplication (merged with B-09 per A-05 order)
- Owner: Coder; Reviewer: Verifier
- Acceptance: Search profile + CV, source-backed job retrieval via OpenAI web search with URL checks, triggered only by "Run now" (no scheduler), max 3 runs/user/day enforced in DB, persisted per-run reports, no repeated positions by canonical identity; failure/rate-limit cases explicit.
- Status: **Deferred / Out of MVP** (ADR-005, Owner directive 2026-10-17: deliberate focus on the user's own data). Original acceptance text kept; tables and triggers retained with no UI, routes or writes.
- Completed: —
- Not completed / reason: —
- Verification: —
- Evidence / files: —

### B-09 — Run now: Israel/global trends snapshots
- Owner: Coder; Reviewer: Verifier
- Acceptance: Source-backed trends generated in the same Run now (shares the 3/day cap), stored per run, region/company/topic filters, latest snapshot served without per-page LLM call, source and run timestamp displayed.
- Status: **Deferred / Out of MVP** (ADR-005, Owner directive 2026-10-17: deliberate focus on the user's own data). Original acceptance text kept; tables and triggers retained with no UI, routes or writes.
- Completed: —
- Not completed / reason: —
- Verification: —
- Evidence / files: —

### B-13 — Analytics tools, evidence ledger and Evidence Validation (no OpenAI)
- Owner: Coder; Reviewer: Verifier
- **Parallel workstream (2026-10-18, LOG-019):** built by the analytics worker on `feature/analytics-b13` in its own checkout, with its own Verifier PASS, then integrated into `main` by the integration lead after an integration Verifier run. File ownership and phases: `docs/architecture/parallel-work-and-metrics-contract.md`. Golden values use the 30-day fixture (ADR-005 §7) at anchor 2026-10-17.
- Slice: see vertical-slice order (ADR-005)
- Acceptance:
  - **Tools:** the 6 tools in ADR-005 §3.3 with strict argument models, a user-scoped read-only connection (`connect(..., read_only=True)`: query_only, authorizer, 2 s statement limit), and caps.
  - **Ledger:** the evidence ledger and the final-answer schema.
  - **Validator:** rules V1–V14 and the repair/degrade decision function.
  - **Rendering:** placeholder rendering, the fallback template (verbatim) and chart view-models.
  - **Tests:**
    - unit tests for every rule and boundary;
    - decoy-user isolation;
    - static SQL test;
    - golden expected values from the reference calculator.
- Status: Integrated (2026-10-18); golden set still open
- Completed: built and verified on `feature/analytics-agent` (ee976b4; analytics worker's Verifier FAIL → FAIL → PASS); merged with real B-05; Integration Verifier PASS on `integration/analytics` (round 1 FAIL: mobile table overflow; fixed in b6ecca8; round 2 PASS), released to `main` at 32d849a.
- Not completed / reason: offline golden set on the 30-day fixture not run. T5 `relate_metrics` has no filters, so "steps on aerobic days" is not expressible (any-workout split only). Validator D-6/V4 rejects a comparison where one period has 0 events, so "friends 4 → 0" falls back (weekly summary works). Both are design questions for the Owner.
- Verification: integration Verifier drove the real tools on real B-05 over the demo fixture with a scripted model: sleep halves 7.4615 vs 6.5714 h equal to B-05 and reference.py; untracked domain → verbatim fallback; non-comparable period → fallback; wrong literal number → FREE_NUMBER; "because" → CAUSAL_LANGUAGE degrade.
- Evidence / files: `app/analytics/*`, `tests/unit/analytics/*`, `docs/analytics/HANDOFF.md`, `docs/analytics/worker-notes.md`; LOG-022.

### B-14 — Demo Mode
- Owner: Coder; Reviewer: Verifier
- Slice: see vertical-slice order (ADR-005)
- Acceptance:
  - **Configuration:** `APP_MODE=demo` constraints (localhost only, TEST_AUTH refused, Google disabled, ephemeral secret allowed).
  - **Isolation:**
    - content-based mode exclusivity in both directions;
    - per-mode cookie, key and session claims (B-02 tests stay green);
    - local-only middleware;
    - `POST /demo/login` loopback-only.
  - **Fixture:**
    - JSON fixture plus SHA-256, verified before import;
    - offset dates;
    - import only through B-04 repositories;
    - fixture content check test.
  - **Working copy:** generation working copies under `data/demo/`; reset by route, CLI and restart; guarded cleanup.
  - **AI and messaging:** banner; verbatim no-key message when AI is disabled; `start.ps1 -Demo` (no tunnel).
  - **Tests:**
    - unit and integration tests: personal DB refused in demo; demo DB refused in personal; tunnel-style headers get 403; demo cookie rejected in personal;
    - Playwright: enter, browse, edit, reset.
  - **Dataset approval:** the draft fixture is marked unapproved until O-8.
  - **Dataset spec (Owner, 2026-10-18, LOG-019):** about 30 days of fictional data per ADR-005 §7 (replaces the 90-day spec), with a reference calculator in `tests/support/reference.py` asserting the §7 patterns and insufficient cases at anchor 2026-10-17. No intake notice (B-06 deferred).
- Status: Done (2026-10-18, Verifier PASS round 1)
- Completed:
  - `APP_MODE=demo` with startup refusals (TEST_AUTH, https/tunnel/non-local URL, placeholder secret; empty secret becomes a per-process random one); Google routes return 404 in demo.
  - application_id demo marker written only by `app.db.stamp_demo_application_id`; mode exclusivity both ways (path, marker and `demo:` subs; checked before migrate and on every request connection).
  - Per-mode cookie name, per-mode signing key, `mode` and `generation` claims; outermost local-only middleware (403 for proxy/tunnel headers, non-local Host or peer).
  - 30-day fixture `demo/synthetic_v1.json` + SHA-256 (verified before parsing), deterministic generator with `--check`, Pydantic strict schema, import only through B-04 repositories, manifest/integrity checks; `approved_by_owner: false`.
  - Fresh working copy under `data/demo/` on every start; reset by confirmed route (CSRF + `confirm=reset`), CLI and restart; marker-checked cleanup.
  - Demo banner with Reset; Analyst shows the verbatim O-5 messages; no OpenAI or Google client in demo.
  - `tests/support/reference.py` asserts the §7 patterns and insufficient cases at anchor 2026-10-17.
- Not completed / reason: `start.ps1 -Demo` not run (no PowerShell here). Non-blocking polish carried into B-07: friendly page when the live copy is tampered with (bare 500 today); checksum-missing message names the wrong file; demo https message wording; reset page lacks the signed-in header; tall mobile banner; favicon 404; unmarked partial copies after a failed build are not cleaned. Fixture names that may match real firms are listed for the Owner's O-8 review.
- Verification: Verifier PASS. ruff and mypy --strict clean (58 files); 853 passed, 1 skipped (no `data/private_terms.txt`) ×3; fixture checksum recomputed. Real uvicorn probes: personal DB bytes and mtime unchanged under demo mode; personal mode refuses demo copies (incl. symlink, case variants); cross-mode and forged cookies rejected; old generation signs out after reset; 403 for 8 proxy headers, foreign Host and a real non-loopback peer; `env -i` start with network/import spy showing 0 connects, 0 DNS lookups, no openai/authlib import; tampered fixture aborts before creating `data/demo`; cleanup decoys survive; 8 readers/writers during 8 resets with no 500s. Independent recompute of the fixture patterns matches the reference calculator. Playwright 1440×900 and 390×844: enter, all tabs, history window, add, edit, soft delete, restore, reset, re-entry, keyboard to reset, no mobile overflow (26 screenshots kept outside the repo, synthetic data only). Not run: PowerShell, a real tunnel, Windows file semantics, live OpenAI/Google.
- Evidence / files: `app/demo.py`, `app/web/demo.py`, `app/db.py`, `app/config.py`, `app/auth.py`, `app/main.py`, `demo/`, `scripts/build_demo_fixture.py`, `scripts/demo_reset.py`, `tests/support/reference.py`, `tests/integration/test_demo.py`, `tests/integration/test_demo_fixture.py`; DEVELOPMENT_LOG LOG-020.

### B-15 — Runtime analytics agent and Analyst UI
- Owner: Coder; Reviewer: Verifier
- **Parallel workstream:** same protocol as B-13; starts after B-13 is integrated.
- Slice: see vertical-slice order (ADR-005)
- Acceptance:
  - **Orchestration:** Responses API function-calling loop through ModelClient, with `store=false`, the limits in ADR-005 §3.5, and the error table in §3.7.
  - **Rate limits:** per-user hourly cap and 1 in flight.
  - **Logging:** privacy-safe.
  - **Analyst UI:** answer card per §5, `aria-live`, double-submit guard; no-key and error states.
  - **Config:** `.env.example` and README updated (AI enable/disable, cost formula, pricing link).
  - **Tests:**
    - scripted-model integration including the full adversarial list;
    - offline golden set at 100%;
    - Playwright at both viewports (answer, partial, fallback, no-key).
  - **Live evaluation:** only with O-9; results recorded as live and labelled.
- Status: Integrated (2026-10-18); live evaluation not run (O-9)
- Completed: verified on `feature/analytics-agent` (41f5388) including the worker's demo-isolation fix; integration patches (router in `create_app` with test injection, AI settings fields, `.env.example`, Analyst form when AI is on, fail-closed demo check); Integration Verifier PASS on `integration/analytics` (round 1 FAIL: mobile table overflow; fixed in b6ecca8; round 2 PASS), released to `main` at 32d849a.
- Not completed / reason: no live OpenAI call (needs Owner approval and key, O-9); golden set open (see B-13); rate gates are per process.
- Verification: demo isolation probed on real uvicorn: only the active working copy opened, before and after reset; personal DB sha256 and mtime unchanged; old-generation cookie rejected; CSRF 403 on /analyst/ask; personal mode opens only DATABASE_PATH; decoy user never shown. Playwright answer card (tiles, finding, chart, evidence) at both viewports with a scripted model; no-key message verbatim.
- Evidence / files: `app/ai_client.py`, `app/analytics/{agent,prompts}.py`, `app/web/analyst.py`, `tests/integration/test_analyst_demo_isolation.py`; LOG-022.

### B-10 — E2E browser verification via Playwright MCP
- Owner: Verifier; Fixes: Coder
- Slice: see vertical-slice order
- Acceptance:
  - **Coverage:** demo-mode and personal TEST_AUTH flows at 1440×900 and 390×844.
  - **Journeys:**
    - demo entry and reset;
    - create, duplicate, edit and delete;
    - history placeholders;
    - aggregates and charts;
    - highlighted applications;
    - 15-day tasks and colours;
    - Analyst answered, partial, fallback and no-key;
    - intake with clarification (mocked);
    - auth protection;
    - keyboard path;
    - empty, loading and error states.
  - **Hygiene:** screenshots and defects contain synthetic data only.
- Status: Pending
- Completed: —
- Not completed / reason: —
- Verification: —
- Evidence / files: —
- *Superseded (ADR-005), original acceptance:* Actually exercised desktop/mobile responsive flows, login or mock-auth boundaries, insert/duplicate rejection, history, aggregates, manual edits, active job statuses, 15-day tasks; logs screenshots/defects without personal data.

### B-11 — Temporary Cloudflare access and safe shutdown (optional, ADR-005 O-11)
- *Original title (before ADR-005):* Temporary Cloudflare demo and safe shutdown
- Owner: Coder; Reviewer: Verifier
- Slice: see vertical-slice order
- Acceptance:
  - personal mode only;
  - demo mode is proven to refuse tunnel exposure;
  - per-start Google callback;
  - HTTPS phone and desktop check;
  - tunnel and server stopped and key revoked;
  - restart steps documented.
- Status: Pending (optional)
- Completed: —
- Not completed / reason: —
- Verification: —
- Evidence / files: —
- *Superseded (ADR-005), original acceptance:* User-approved ephemeral exposure, stable OAuth callback configured, Google identity restricted, secrets server-side, HTTPS mobile/desktop tested, tunnel/server stopped and OpenAI credentials deactivated/revoked, restart instructions documented.

### B-12 — Submission: public repository, PDF report, demo video
- *Original title (before ADR-005):* Evidence, demo, one PDF submission
- Owner: Main Claude; Reviewer: Verifier + User
- Slice: see vertical-slice order
- Acceptance:
  1. **Public repo** (approach per O-12; no history rewrite or visibility change in the private repo), containing:
     - source;
     - approved synthetic fixture;
     - architecture overview;
     - README with exact install and start steps, demo with and without a key, tests, reset, optional Google and OpenAI setup;
     - placeholder-only `.env.example`;
     - honest status and limitations.
     A secret and PII scan of the tree **and** its history must be clean.
  2. **PDF** covering the directive's §5B topics, including the direction change framed as deliberate, Architect/Coder/Verifier roles, real defects, test results with tested / manually reviewed / unverified separated, and the evaluation metrics.
  3. **Video plan** following the storyboard: synthetic data, AI answer, insufficient-evidence case, shutdown.
  4. **Owner review** before sending.
- Status: In Progress (public artifacts awaiting the Owner's approval, LOG-024)
- Completed: public repository `DorGM/personal-ai-dashboard-submission` published with the Owner's approval (single-commit snapshot, 2026-10-18); report v2.0 with Appendix B (curated prompts) and Appendix C; sanitized log, plan and evidence index; README top section; demo video validated and cropped for privacy.
- Not completed / reason: the synthetic dataset is not owner-approved (O-8); the golden set and live OpenAI evaluation (O-9) were not run; B-10 committed E2E suite not built; Windows re-run of the test fixes not done.
- Verification: see LOG-024 (independent Verifier on the public documents, the PDF and a fresh clone).
- Evidence / files: public `docs/submission/`, `README.md`, release `submission-v1`.
- *Superseded (ADR-005), original acceptance:* Accurate executive summary, architecture, prompts/iterations, critical reflection, test pyramid, skipped tests/risks, real caught defect if any, recorded/live demo reference, single polished PDF. User reviews before external submission.

## Definition of done / risk handling
- All applicable automated test results included; defects fixed and retested; no misleading claims of full production deployment.
- External search (deferred), Google login, paid OpenAI calls (intake, analytics, live evaluation), and Cloudflare access are separately enabled only when authorized and configured.
- Submission = public repository (approach per ADR-005 O-12), PDF technical report, recorded local demo video (Owner directive 2026-10-17). The app need not stay hosted.
- Tests not run or unsupported external integrations are named explicitly, with risk and mitigation.
- Final demo should use synthetic data. Application availability stops when the local server/tunnel stop; local secrets must be revoked deliberately.
