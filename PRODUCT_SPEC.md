# Personal AI Dashboard — Product Contract (v0.2, Personal AI Analytics — ADR-005)

v0.1 text superseded by ADR-005 (Owner directive 2026-10-17) is kept and marked **Deferred (ADR-005)** or **Superseded (ADR-005)**. Verbatim directive: `docs/architecture/owner-directive-2026-10-17-personal-ai-analytics.md`.

**Purpose:** Private, polished, mobile/desktop **Personal AI Dashboard with agentic data analytics**. It tracks daily activities and personal records, shows deterministic history, metrics and trends, and answers free-form natural-language questions about the user's own data with grounded, evidence-validated insights. The local backend owns the SQL data store and all OpenAI calls (the analytics agent; free-text intake is Deferred, see §3). A local Demo Mode with synthetic data needs no Google account or OpenAI key. The app can be temporarily reachable over Cloudflare Tunnel in personal mode only. **No direct ChatGPT conversation-history access.**

*Superseded (ADR-005), v0.1 purpose:* "Private, polished, mobile/desktop personal assistant UI. Local backend and SQL data store; temporarily accessible off-LAN over Cloudflare Tunnel for development/testing/demo. Google identity maps to an individual user's records. OpenAI API on backend processes open-text updates and powers scheduled job/trend insights. **No direct ChatGPT conversation-history access.**"

This document captures **agreed behavior**, not a chosen implementation stack. Architect must propose details and seek user approval during Phase A before code. Implementation stack approved in docs/phase-a A-02; analytics design in ADR-005.

## 1. UI and behavior
- Responsive desktop/mobile website reached by URL, with four independent tabs: **Food & Health**, **Content Consumption**, **Jobs & Career**, **Upcoming Tasks**, plus an **Analyst** view (placement: ADR-005 O-3). Clean, presentation-grade UI; accessible keyboard/focus, labels, contrast and error/empty/loading states.
- Each tracked category has a daily/history view and an aggregate view where meaningful. History displays a selected window **up to 15 calendar days**. Default range = trailing 15 calendar days ending today, clipped to the earliest stored relevant record; if there are no records, show an empty state. Historical windows may be moved within existing-data-start → today.
- Aggregate range is user-selectable within the **first available recorded date through today**; default to trailing 15 days, clipped to start. Date range must be valid (start <= end <= today) and never produce out-of-range database errors. Do not artificially require a custom range to start at the first ever record.
- Missing date/category renders **"לא הוכנס תיעוד עבור יום זה"** ("No record entered for this day") as an **UI placeholder**, never persisted as fact; missing dates are **not zero** in averages.
- Display a small parenthesized **actual record date** next to each visible record/value. For latest available metrics, do not misrepresent an older value as measured today. Store both `record_date` (event) and `created_at` (entry time).
- Aggregation denominator = number of distinct dates **with records in the relevant domain** within selected range. E.g. 7 workouts across 5 documented workout days → 1.4 workouts/day; absent days do not enter denominator. For per-subtype (e.g. breakfast) use documented days of its *parent domain* (meals) unless Architect proposes a clearer definition and user explicitly approves it.
- All historical/aggregate retrieval and calculations use deterministic, parameterized **SQL**, not generated SQL from an LLM.
- The 15-day limit applies to history views only; aggregates and analytics may use ranges from the first record (analytics ≤ 366 days per query).
- Charts are predefined server-rendered components fed only by validated backend data (ADR-005 §5).

## 2. Data model and keys
- Relational SQL DB; proposed 17 tables below. Actual DDL, indices, types and migration strategy are **Phase A decisions**. Each table has `id` as unique PK; each user-owned record has `user_id` referencing **`users.id`** (FK). Relevant tables also have `created_at` and `updated_at` (timestamps) and a dated event field. User-scoped business uniqueness is enforced with DB constraints and/or explicitly verified normalization policy.
- API authorization obtains `user_id` from validated session, **never** trusts client-provided user IDs. Referential integrity must be enabled (including SQLite FK enforcement if selected).
- Google OAuth identity uses stable `google_sub` (not email as identity). Store minimal profile. Personal mode: only allowlisted Google accounts. Demo Mode uses a local synthetic identity (§5A). *Superseded (ADR-005):* "For demo, restrict access to explicitly approved accounts." Correctly secure callback, session, CSRF/state, cookies and auth boundaries.
- Manual edits and deletions of persisted records are permitted; OpenAI free-text automation may **insert new entries only** and must never overwrite an existing entry, even if asked to do so. Changes to derived computed values after legitimate manual edits are allowed as recalculation, not autonomous edits to a source record.

### 2.1 Food & health — 6 tables

**`weight_records`** — `id`, `user_id`, `record_date`, `weight_kg`; unique (`user_id`, `record_date`); positive value. Aggregate: first and last available measurement *inside range*, difference end − start, and latest recorded weight overall with its actual date (including if no measurement today). Handle fewer than 2 points as undefined trend, not false zero change.

**`meals`** — `id`, `user_id`, `record_date`, `meal_type` [breakfast, lunch, dinner, night, snacks], `description`, `estimated_calories` nullable, `calorie_basis` [user_provided, reference_based_estimate, rough_estimate, unknown], optionally `calorie_notes`; unique (`user_id`, `record_date`, `meal_type`). At most one **meal-type record per day**. Within a *single new input*, merge repeated descriptions of that same new day/type into one consolidated entry. Later attempted inserts for an existing day/type must fail with the user-facing message **"נתון זה כבר הוכנס, אם תרצה להוסיף אליו נתונים תכניס זאת ידנית"**. No LLM-driven addition to an existing meal record. Aggregate: meals/day by meal type over distinct documented meal dates.

**`daily_calories`** — `id`, `user_id`, `record_date`, `total_calories` nullable, `coverage_status` [estimated_partial, estimated_complete, unknown], `calculated_at`; unique (`user_id`, `record_date`). Derive from recorded meals, **at day end** as requested; for incomplete meal/cals evidence explicitly mark total as partial/unknown and never equate nonrecording with 0. Manual source edits require a consistent derived-data recalculation strategy approved by user; avoid presenting estimates as precise nutritional values. Aggregate: mean of valid documented daily values with coverage clearly labeled; exclude unknown totals, surface sample count.

**`workouts`** — `id`, `user_id`, `record_date`, `workout_type` [aerobic, weights, bodyweight, stick_spinning], `intensity` nullable [light, medium, fast; applies to stick spinning], `duration_minutes` nullable, `description` nullable; unique (`user_id`, `record_date`, `workout_type`) as earlier proposed, pending user approval of multiple same-type sessions handling. Aggregate mean number/day by workout type over documented workout days.

**`sleep_records`** — `id`, `user_id`, `record_date`, `hours_slept`; unique (`user_id`, `record_date`); validate plausible positive duration. Agree which date labels an overnight sleep session at architecture review. Aggregate average hours per documented night.

**`step_records`** — `id`, `user_id`, `record_date`, `steps_count`; unique (`user_id`, `record_date`); integer >= 0. Aggregate mean steps per documented day.

### 2.2 Content consumption — 4 tables

**`articles`** — `id`, `user_id`, `record_date`, `title`, `source` nullable, `url` nullable; prefer canonical URL for item identity, fallback normalized title+source+date with clarification if ambiguous.

**`podcasts`** — `id`, `user_id`, `record_date`, `podcast_name`, `episode_title` nullable, `duration_minutes` nullable; item identity podcast+episode+date; ask clarification if insufficient.

**`movies`** — `id`, `user_id`, `record_date`, `title`, `release_year` nullable; item identity title+year+date when possible.

**`series`** — `id`, `user_id`, `record_date`, `series_name`, `season_number` nullable, `episode_number` nullable; item identity series+season+episode+date. If user mentions several episodes in one message, create distinct episode-level rows; ask if numbering is missing and duplication might occur.

Unlike meals, **multiple distinct content items on the same day are allowed**. Prevent *identical item/date* repeats, but do not block other items of the same content type. Aggregates are **total counts** per category for the range (not arithmetic averages of titles).

### 2.3 Career and job search — 5 tables (MVP: `job_applications` only)

**Jobs & Career tab in MVP:** manual job applications only, with open applications (applied/interviewing) highlighted and status/stage manually updated. Job recommendations, the search profile/CV and industry trends are **Deferred (ADR-005)**. Their tables remain in the schema with no UI, routes or writes, and are never queried by the analytics agent.

**`job_applications`** — `id`, `user_id`, `application_date`, `company_name`, `job_title`, `company_location`, `work_model` [onsite, hybrid, remote, unknown], `industry`, `status` [applied, interviewing, accepted, rejected], `interview_stage` nullable integer, `job_url` nullable, optional stable external identifier. Uniqueness by user+canonical job identity (URL/posting ID preferred; company/role/location fallback with manual disambiguation). `applied`/`interviewing` are **visually highlighted**; user manually updates stage/status, particularly terminal states. Multiple jobs submitted on same day are allowed.

**Deferred (ADR-005):** **`job_search_profile`** — `id`, `user_id` unique, `preferences_text`, `cv_file_path` nullable or another server-controlled CV reference, `updated_at`; free-text desired roles/constraints + uploaded CV. CV never publicly exposed or written to Git. Editing preferences/profile manually allowed.

**Deferred (ADR-005):** **`job_reports`** — `id`, `user_id`, `report_date`, `generated_at`, `summary` nullable; unique (`user_id`, `report_date`); persisted daily report (no hidden duplicate runs).

**Deferred (ADR-005):** **`job_recommendations`** — `id`, `user_id`, `report_id` FK, `company_name`, `job_title`, `location`, `work_model`, `industry`, `job_url`, `match_reason`, `first_recommended_at`, optional stable posting identity. Uniqueness across reports by (`user_id`, canonical position identity), not by report date. Exclude previously recommended *and* already applied jobs from new recommendations. Show sources, relevant criteria and when searched. Multiple recommendations per report.

**Deferred (ADR-005):** **`industry_trends`** — `id`, `user_id`, `snapshot_date`, `region` [israel, global], `trend_type` [company, technology], `name`, `description`, `source_url` nullable, `rank` nullable; uniqueness on user+snapshot date+region+type+normalized name. Company/topic and Israel/global filters; source-backed **daily snapshots stored locally**. Page reads DB snapshot rather than querying API on every view. Clearly display last update timestamp and stale-data behavior.

### 2.4 Tasks / 15-day schedule — 1 table

**`tasks`** — `id`, `user_id`, `task_date`, `start_time` nullable, `title`, `description` nullable, `category` [family, career, friends, housework, other], `status` [pending, completed]. Categories and UI colors: **family=blue; career=yellow; friends=green; housework=red; other=white** (white badge with visible border on light background). Display today and next 14 days (15 calendar days total). Multiple distinct tasks/category/day allowed; duplicate exact task/date/time/title is rejected. Aggregate average tasks/day by category over documented task dates.

### 2.5 User — 1 table

**`users`** — `id` INTEGER PK, `google_sub` TEXT UNIQUE, `email`, `display_name`, `created_at`. `users.id` is the FK target for `user_id` in all other tables. Email is descriptive, not the stable OAuth principal.

## 3. Free-text runtime OpenAI workflow (NOT the Claude Code agents) — **Deferred / Out of MVP (Owner, 2026-10-18, LOG-019)**
**Deferred:** "B-06 (AI-powered free-text data intake) is now Deferred / Out of MVP. Manual data entry from B-04 is sufficient for this submission." Rationale: concentrate the product's runtime AI on personal analytics and evidence-grounded insights. The text below is kept for history and is not built; in the MVP, records (including meal calorie estimates) are entered manually or come from the demo fixture. Verbatim: `docs/architecture/owner-directive-2026-10-18-priorities.md`.

1. User enters a text statement in a dedicated input field; it must contain an **explicit, valid event date** such as `05.10.2026` or `החמישי לאוקטובר 2026`. Do not infer dates from "yesterday" or unstated chronology. Reject invalid, future, or ambiguous dates with clarification as appropriate. A statement can describe multiple categories/entries with dates.
2. Backend calls the OpenAI API with a **fixed structured output schema**, not free-form SQL. Extract domain/subtype/date, values/units, source text, confidence/ambiguity and nutrient estimation provenance. Independently validate schema, business rules, date, types and ownership.
3. If ambiguous or incomplete for safe insertion (uncertain date, meal type, content identity, calories without basis), runtime **Clarification Agent** asks a concise follow-up in the same input/conversation panel; reprocess the new details until every *attempted saved item* is unambiguous or explicitly omitted. It is acceptable not to record everything; do not invent missing data. Default/no-record is a UI placeholder, not a row.
4. Before insert check existing DB records using user-scoped **business keys**; let DB uniqueness enforce. If same day/type meal or same date/unique domain key already exists, reject that item with the specified message; never auto-upsert or silently merge across separate submissions. In the **same incoming statement**, consolidate repeated items representing one new unique record rather than double inserting.
5. For multi-item statements, preserve a per-item receipt: inserted / duplicate-rejected / needs-clarification / not-recorded. **Architect must propose explicit atomic-vs-partial transaction policy for user approval.** Do not make hidden destructive changes.
6. Calorie estimates: extract ingredients and quantities; prioritize user-provided kcal, then defensible nutrient reference values accessible to the model, then explicitly labeled rough estimates. Provide concise note/provenance; mark uncertainty, never fabricate exact reference citations or pretend model memory is a verified nutrition database. Derived daily summary updates from meal records by end-of-day policy.
7. Only the backend holds OpenAI API key. Rate-limit and bound requests; no arbitrary SQL/execution, secret leakage, or cross-user access. Model and schema version are configurable/logged for reproducible evaluation. Support mocks in tests; paid live calls only with authorization.

8. Model requirement: structured outputs (no web search). With AI disabled, free-text entry shows a notice and manual forms remain (ADR-005 §6.8, O-5).

## 3A. Natural-language analytics agent and Evidence Validation (runtime OpenAI; NOT the Claude Code agents)
- One runtime agent using the OpenAI Responses API with function calling. No embeddings, vector database, LangGraph or multi-agent framework (Owner, 2026-10-17).
- The model chooses among six bounded, read-only, user-scoped, parameterized tools (coverage, metric summary, period comparison, daily series, metric relationship, record listing). It never writes or runs SQL. `user_id` always comes from the server-side session.
- The backend computes every count, average, difference, percentage, trend and correlation. The model's final answer cites evidence ids and uses placeholders; the backend renders every number.
- Limits on tool calls, turns, tokens, timeouts, result size, date ranges and questions per hour (ADR-005 §3.5).
- Deterministic Evidence Validation (not a second agent) checks: data exists; enough observations; missing/undocumented days and coverage; comparison coverage; recomputed values; every claim supported by cited evidence; no causation or claims beyond the data. Undocumented days are never zeros.
- Partial answers give the supported findings and explain what is missing. If nothing grounded remains, the Owner's fallback is shown: "The available records do not provide sufficient reliable information to analyze {topic} for the selected period. You can add relevant records or expand the date range and try again."
- Answers may include validated metric tiles, comparisons and up to two charts drawn only by trusted predefined components.
- Details, thresholds and acceptance criteria: ADR-005 §3–§5 and §10.

## 4. Autonomous runtime intelligence — **Deferred (ADR-005, Owner directive 2026-10-17)**
*Out of MVP as a deliberate product focus on the user's own data; earlier amendments (Run now, 3/day) are also deferred. Text below kept for history.*
- **Job Recommendation Agent:** Each day at **09:00 Asia/Jerusalem** *while local application/scheduler is running*, retrieve live job opportunities using an **approved source or OpenAI API tool capable of accessing current external data** (model text alone is not a live jobs feed). Filter against user free-text role preferences and uploaded CV, then remove prior recommendations and submitted positions via SQL identity lookup. Persist report + recommendations; sources/URLs/retrieval time are required; no invented or unverified jobs.
- **Trend Refresh Agent:** Refresh Israeli/global tech industry companies and hot fields daily while runtime is active using verifiable recent external sources; persist dated trends and citations, show cached snapshot in UI. Rate-limited, idempotent per day and tolerant to partial failure.
- When the local process is shut down, agents do **not** run. Do not promise 09:00 delivery or trends updates when offline; on restart show staleness and follow an explicitly approved catch-up/skip policy.
- Exact runtime agent orchestration (separate model calls vs shared model plus deterministic tools), search provider, job listing legal/technical viability, failure/retry behavior and costs are **Architecture Phase A approval items**. Avoid unnecessary orchestration frameworks.

## 5. SQL semantics, security and operations
- History SQL filters by authenticated `user_id`, `record_date BETWEEN start/end`, ordered by date; 15-day maximum for history views. Aggregates use `GROUP BY` and `COUNT(DISTINCT record_date)` for denominator; numeric `AVG` over only recorded valid numeric values, nonnumeric content `COUNT(*)`, weight `first/last` within requested range. Date limits enforced on backend; server/API calculates earliest date per user/domain appropriately.
- If no rows in range: show placeholder/empty aggregate and sample count 0; do not return NaN/divide by zero. For recorded true zero (0 steps/0 entries where explicitly recorded), distinguish from missing rows.
- Enforce user scoping, parameterized queries, validation, FK checks, PK/UNIQUE/CHECK, transactional writes; canonicalization of URL/names must be deterministic and tested. Avoid using global mutable frontend state as DB source of truth.
- Secure OAuth sign-in with Google: server validates Google ID token/provider state, session cookie secured for HTTPS, explicit demo-user allowlist, logout. Google sign-in **does not** grant ChatGPT history access, Google Drive access, or free OpenAI API calls.
- Access from desktop/mobile via **temporary Cloudflare Tunnel to local server** (personal mode only; Demo Mode refuses tunnel exposure; optional per ADR-005 O-11). Stable redirect origin is required for Google OAuth; Architect must propose how to provision stable hostname/callback, Cloudflare DNS/domain requirements and how to secure the demo before activation. Server/tunnel must be launched manually for testing and stopped after demo; disable/revoke OpenAI key separately and document reactivation. Do not place OAuth/API secrets in repository or frontend.
- Display and test with **synthetic/demo data** during presentation (Demo Mode, §5A); no real medical data, applicant details, CV or secrets in screenshots, logs, error traces or submitted PDF.

## 5A. Demo Mode (ADR-005 §6–§7)
- Purpose: any reviewer can install, start and explore the product locally, reproducibly, with no Google account and no paid API.
- A version-controlled synthetic JSON fixture (entirely fictional; content supplied or approved by the Owner) is committed read-only with a SHA-256 checksum. On startup or reset the app builds a local working copy from it; the committed fixture never changes.
- Strict separation: a demo process cannot open a personal database and personal mode cannot open a demo database or use the demo identity. Demo Mode is localhost-only, refuses tunnel/https base URLs and remote requests, and uses its own session cookie.
- Without an OpenAI key: dashboard, history, filters, statistics and charts work; AI analysis is disabled and shows exactly: "AI-powered analysis is not available in the current demo configuration. You can still explore the sample data, metrics, and trends. To enable the analytical agent, configure your own OpenAI API key using the setup instructions." No AI answer is ever simulated.
- With the reviewer's own key and model in local environment variables: the same synthetic data, the analytics agent works, and every validation and integrity rule applies.

## 6. Verification contract
- Unit: aggregation denominators/zero-vs-missing, date parsing (Hebrew & `DD.MM.YYYY`), calorie flags, canonical identity, record collision; metric registry vs an independent reference calculator; analytics tool bounds; Evidence Validation rules V1–V14; number rendering formats.
- DB integration: correct PK/FK/UNIQUE/CHECK, user isolation, write transactions, manual edit/delete vs denied AI overwrite, latest-value dates; read-only analytics connection; decoy-user isolation; demo/personal mode exclusivity; fixture checksum and import.
- *Deferred (ADR-005):* snapshot staleness; report/job dedupe.
- API with mocked OpenAI: valid/invalid structured responses, clarification, prompt injection as untrusted text, error/timeout/rate-limit, multi-item partial outcomes, exact duplicate error; analytics agent with scripted tool-call sequences, adversarial final answers (wrong numbers, invented evidence, causal language, claims about undocumented days, bad chart data), limits/timeouts/token budget, AI-disabled verbatim message, offline golden question set over the synthetic fixture.
- Browser via Playwright MCP: 2 viewport families (desktop/mobile), navigation and tab layouts, history/aggregate toggles and limits, data entry with clarification, duplicate rejection, manual update, task colors including white Other, highlighted open applications, auth protection, empty/loading/error states, and keyboard navigation; Demo Mode entry and reset; charts; Analyst answered / partial / fallback / no-key states.
- Optional live analytics evaluation only with Owner authorization and key. Live external APIs, scheduled background behavior (deferred), OAuth production setup and tunnel exposure require explicit configuration and separate real-world checks; name omitted tests and associated risks. Log at least one genuine defect if discovered, never manufacture one.

## 7. Open architecture questions — MUST be approved before implementation
**Status (v0.2):** Q1–Q3 and Q5 resolved by A-01…A-05, ADR-004 and ADR-005 (synthetic fixtures: ADR-005 §6–7). Q4 resolved: job/trend sources and scheduler deferred (ADR-005); model and tools in ADR-005 §3. **Open now:** ADR-005 §11 O-0…O-12.
1. Backend/frontend stack, package budget, UI mockups and accessibility standard; database engine and migrations.
2. Detailed table DDL, daily calorie derivation at day boundary, timestamps/timezone, meal/workout uniqueness edge cases, partial AI insert strategy, user-domain documented-day denominator semantics.
3. Auth callback hostname, Google OAuth redirect provisioning, allowed accounts and secure cookie policy through tunnel.
4. Job search data provider and current source verification, OpenAI model/tool selection, trend data sources, daily scheduler's offline and catch-up behavior, costs.
5. Deployment/start/stop procedure, secrets and synthetic demo fixtures; minimum viable feature scope for take-home timeline.

**Rule:** Do not silently reinterpret an open issue as an approval. Record proposed options/tradeoffs and user acceptance in `DEVELOPMENT_LOG.md` and `PROJECT_PLAN.md`.
