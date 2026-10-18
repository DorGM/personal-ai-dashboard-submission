# A-03 — SQL schema, uniqueness, aggregates and lifecycle (Architect proposal v3)

**Status:** APPROVED 2026-10-16 by Owner: Option A, with amendments to E2, E5, E8 (see "Approved amendments" at the end; they override the table rows below).
**Builds on:** A-01 (Option B; Run now only, max 3 runs/day, no 09:00 schedule), A-02 (Python/FastAPI/SQLite, hand-written SQL migrations).
**Does not decide:** OpenAI prompts/models, search source, OAuth (A-04); visuals (A-05).

## 1. Main structural decision — where business uniqueness is enforced

### Option A — Database-enforced canonical keys (recommended)
Every table whose spec identity uses nullable or free-text fields gets a deterministic `canonical_key TEXT NOT NULL` computed in one tested Python function (`canonical.py`) at insert/edit, and a `UNIQUE` index on it. All enum, range and positivity rules are `CHECK`s. Tables are `STRICT`; `PRAGMA foreign_keys=ON` on every connection.
- **Pro:** the DB is the authority (spec: "Database uniqueness is authoritative"); avoids SQLite's rule that `NULL`s never collide in a UNIQUE index (e.g. two articles with no URL and same title would otherwise both insert); one place to unit-test normalization.
- **Con:** a stored derived column must be recomputed on manual edit (done in the repository layer, same transaction).

### Option B — Plain column UNIQUE constraints + app-level duplicate checks
- **Pro:** simpler DDL.
- **Con:** nullable columns silently defeat uniqueness; races and code paths that skip the check can insert duplicates; contradicts the spec's DB-authoritative rule.

### Option C — Expression indexes (`COALESCE(col,'')`, `lower(trim(title))`) instead of a stored key
- **Pro:** no extra column.
- **Con:** normalization split between SQL and Python (URL canonicalization can't be done in SQLite), harder to test and explain.

**Recommendation: A.**

## 2. Edge-case decisions (each needs your OK; my default in bold)
| # | Question | Options | Default |
|---|---|---|---|
| E1 | Partial vs atomic save for multi-item AI input | atomic (all or nothing) / **per-item: each item in its own savepoint, receipt shows inserted / duplicate_rejected / needs_clarification / not_recorded** | **per-item** — one duplicate meal shouldn't discard a valid workout in the same sentence |
| E2 | Daily calories "at day end" (no scheduler any more) | nightly job / **recompute that date's `daily_calories` row in the same transaction as any meal insert, edit or delete** / compute on read | **recompute on write** — always consistent, no scheduler needed |
| E3 | `coverage_status` rule | **`unknown` if no meal of that date has calories; `estimated_complete` if breakfast, lunch and dinner all exist with calories; else `estimated_partial`** | as bolded; UI labels totals "estimate" always |
| E4 | Two workouts of the same type on one day | **keep spec: one row per (date, type); second AI insert rejected; user edits duration manually** / allow multiple sessions with `session_no` | **keep spec** — mirrors meals, simpler aggregates |
| E5 | Which date labels overnight sleep | **wake-up date** / bedtime date | **wake-up date** — "last night's sleep" belongs to today |
| E6 | Breakfast-per-day denominator | **documented meal days (any meal type)** (spec default) / days with that subtype | **spec default** |
| E7 | "Today", date validity, timezone | **`today` = Asia/Jerusalem calendar date; `record_date` is `YYYY-MM-DD` TEXT; `created_at`/`updated_at` are UTC ISO-8601; future dates rejected except `tasks.task_date` (today … today+14)** | as bolded |
| E8 | Run-now cap of 3/day enforcement | app counter only / **DB: `job_reports.run_number` CHECK 1–3 and UNIQUE (user, report_date, run_number)** | **DB-enforced** — a 4th run can't be stored even under a double-click race |
| E9 | Trends under multiple runs per day | **trends belong to a run (`report_id` FK); UI shows the latest run's snapshot; uniqueness per run** / one snapshot per day overwritten | **per run** — no overwrites, matches "no silent merge" |
| E10 | Job recommendation dedupe | **UNIQUE (user, canonical position key) across all runs; also excluded if a `job_applications` row has the same key** | as bolded (spec) |
| E11 | Delete behavior | **hard delete for manual deletes; `ON DELETE CASCADE` only from `users` and from `job_reports` → its recommendations/trends** | as bolded; no soft-delete in MVP |
| E12 | Exact-duplicate task | **UNIQUE (user, task_date, COALESCE(start_time,''), normalized title)** via `canonical_key` | as bolded |

## 3. Canonical keys (deterministic, unit-tested)
Normalization `norm(s)`: Unicode NFKC → trim → collapse whitespace → casefold. Hebrew is kept as-is (no transliteration).
- `articles`: `url:` + canonical URL (lowercase scheme/host, drop `www.`, fragment, `utm_*`/tracking params, trailing slash) if URL present; else `t:` + norm(title) + `|` + norm(source). Unique with `record_date`.
- `podcasts`: norm(podcast_name) + `|` + norm(episode_title or '').
- `movies`: norm(title) + `|` + (release_year or '').
- `series`: norm(series_name) + `|S` + (season or '') + `|E` + (episode or '').
- `job_applications` / `job_recommendations`: `url:` canonical job URL, else `ext:` posting id, else `cr:` norm(company)+norm(title)+norm(location).
- `industry_trends`: norm(name).
- `tasks`: start_time or '' + `|` + norm(title).

## 4. DDL (SQLite, STRICT). Common columns abbreviated as `…std` = `created_at TEXT NOT NULL, updated_at TEXT NOT NULL`; `…own` = `user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE`.
```sql
users(id INTEGER PRIMARY KEY, google_sub TEXT NOT NULL UNIQUE, email TEXT NOT NULL,
      display_name TEXT, created_at TEXT NOT NULL);

weight_records(id PK, …own, record_date TEXT NOT NULL, weight_kg REAL NOT NULL CHECK(weight_kg > 0 AND weight_kg < 500), …std,
      UNIQUE(user_id, record_date));
meals(id PK, …own, record_date, meal_type TEXT NOT NULL CHECK(meal_type IN ('breakfast','lunch','dinner','night','snacks')),
      description TEXT NOT NULL, estimated_calories INTEGER CHECK(estimated_calories IS NULL OR estimated_calories BETWEEN 0 AND 10000),
      calorie_basis TEXT NOT NULL CHECK(calorie_basis IN ('user_provided','reference_based_estimate','rough_estimate','unknown')),
      calorie_notes TEXT, …std, UNIQUE(user_id, record_date, meal_type));
daily_calories(id PK, …own, record_date, total_calories INTEGER, coverage_status TEXT NOT NULL
      CHECK(coverage_status IN ('estimated_partial','estimated_complete','unknown')), calculated_at TEXT NOT NULL, …std,
      UNIQUE(user_id, record_date));
workouts(id PK, …own, record_date, workout_type TEXT NOT NULL CHECK(workout_type IN ('aerobic','weights','bodyweight','stick_spinning')),
      intensity TEXT CHECK(intensity IS NULL OR (workout_type='stick_spinning' AND intensity IN ('light','medium','fast'))),
      duration_minutes INTEGER CHECK(duration_minutes IS NULL OR duration_minutes BETWEEN 1 AND 1440), description TEXT, …std,
      UNIQUE(user_id, record_date, workout_type));
sleep_records(id PK, …own, record_date, hours_slept REAL NOT NULL CHECK(hours_slept > 0 AND hours_slept <= 24), …std,
      UNIQUE(user_id, record_date));
step_records(id PK, …own, record_date, steps_count INTEGER NOT NULL CHECK(steps_count >= 0), …std, UNIQUE(user_id, record_date));

articles(id PK, …own, record_date, title TEXT NOT NULL, source TEXT, url TEXT, canonical_key TEXT NOT NULL, …std,
      UNIQUE(user_id, record_date, canonical_key));
podcasts(… podcast_name TEXT NOT NULL, episode_title TEXT, duration_minutes INTEGER CHECK(… > 0), canonical_key, UNIQUE(user_id, record_date, canonical_key));
movies(… title TEXT NOT NULL, release_year INTEGER CHECK(release_year IS NULL OR release_year BETWEEN 1880 AND 2100), canonical_key, UNIQUE(…));
series(… series_name TEXT NOT NULL, season_number INTEGER CHECK(>0), episode_number INTEGER CHECK(>0), canonical_key, UNIQUE(…));

job_applications(id PK, …own, application_date TEXT NOT NULL, company_name, job_title, company_location TEXT NOT NULL,
      work_model CHECK IN ('onsite','hybrid','remote','unknown'), industry TEXT NOT NULL,
      status CHECK IN ('applied','interviewing','accepted','rejected'), interview_stage INTEGER CHECK(interview_stage IS NULL OR interview_stage >= 1),
      job_url TEXT, external_id TEXT, canonical_key TEXT NOT NULL, …std, UNIQUE(user_id, canonical_key));
job_search_profile(id PK, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE, preferences_text TEXT NOT NULL,
      cv_file_ref TEXT, …std);   -- server-side path under gitignored data/, never served statically
job_reports(id PK, …own, report_date TEXT NOT NULL, run_number INTEGER NOT NULL CHECK(run_number BETWEEN 1 AND 3),
      status TEXT NOT NULL CHECK(status IN ('running','succeeded','partial','failed')), generated_at TEXT, summary TEXT,
      model TEXT, schema_version TEXT, error TEXT, …std, UNIQUE(user_id, report_date, run_number));
job_recommendations(id PK, …own, report_id INTEGER NOT NULL REFERENCES job_reports(id) ON DELETE CASCADE, company_name, job_title,
      location, work_model, industry, job_url TEXT NOT NULL, source_name TEXT NOT NULL, retrieved_at TEXT NOT NULL, match_reason TEXT NOT NULL,
      external_id TEXT, canonical_key TEXT NOT NULL, first_recommended_at TEXT NOT NULL, …std, UNIQUE(user_id, canonical_key));
industry_trends(id PK, …own, report_id INTEGER NOT NULL REFERENCES job_reports(id) ON DELETE CASCADE, snapshot_date TEXT NOT NULL,
      region CHECK IN ('israel','global'), trend_type CHECK IN ('company','technology'), name TEXT NOT NULL, description TEXT NOT NULL,
      source_url TEXT, rank INTEGER CHECK(rank IS NULL OR rank >= 1), canonical_key TEXT NOT NULL, …std,
      UNIQUE(user_id, report_id, region, trend_type, canonical_key));

tasks(id PK, …own, task_date TEXT NOT NULL, start_time TEXT CHECK(start_time IS NULL OR start_time GLOB '[0-2][0-9]:[0-5][0-9]'),
      title TEXT NOT NULL, description TEXT, category CHECK IN ('family','career','friends','housework','other'),
      status CHECK IN ('pending','completed') DEFAULT 'pending', canonical_key TEXT NOT NULL, …std,
      UNIQUE(user_id, task_date, canonical_key));
```
Every dated table also gets `CHECK(record_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]')`; true date validity is checked in Pydantic.
Still 17 tables: run tracking reuses `job_reports` (one row per Run now), so no new table.

## 5. Indexes
The UNIQUE constraints above lead with `user_id, <date>` and serve history range scans. Added: `job_applications(user_id, status)`, `job_reports(user_id, report_date DESC, run_number DESC)`, `industry_trends(report_id, region, trend_type)`, `job_recommendations(report_id)`.

## 6. Aggregate SQL semantics (all parameterized, user-scoped)
- Range validation in Python first: `earliest <= start <= end <= today`, history span ≤ 15 days; `earliest = MIN(record_date)` for that user+domain. No rows → empty state, `sample_days = 0`, no division.
- Numeric mean (sleep, steps, calories): `SELECT AVG(v), COUNT(*) FROM t WHERE user_id=? AND record_date BETWEEN ? AND ?` (calories: `AND total_calories IS NOT NULL`, and show the excluded-day count).
- Per-day rates (meals by type, workouts by type, tasks by category): `COUNT(*) per type / COUNT(DISTINCT record_date)` over the whole domain in range — e.g. 7 workouts on 5 workout days → 1.4/day.
- Weight: first and last row by `record_date` in range; delta only when ≥ 2 rows, otherwise "not enough data"; plus the latest weight overall with its date.
- Content: `COUNT(*)` per table in range.
- Explicit zero (0 steps) is a row and counts; a missing day is not a row and never counts.

## 7. Lifecycle
- AI intake: INSERT only. A UNIQUE violation is caught and turned into the spec's Hebrew duplicate message for that item; never UPDATE.
- Manual edit/delete: authenticated, `WHERE id=? AND user_id=?`, recomputes `canonical_key` and (for meals) that day's `daily_calories`; an edit that collides with another row is rejected with a clear message.
- Run now: inserts `job_reports` with the next `run_number` (the CHECK blocks a 4th), status moves running → succeeded/partial/failed; recommendations that hit the cross-run UNIQUE are skipped, not errors.

## What I need from you
Approve Option A plus defaults E1–E12, or tell me which items to change (e.g. "approve, but E4 allow multiple sessions").

## Approved amendments (2026-10-16, Owner)
the owner's words: "approve A, but E2 needed to be treated more deeply - i want the LLM to calculate the calorie each time a meal is added, edited or deleted on fly, which means that the LLM gets the prompt, extract items, and if it found a meal within the prompt, it calculate the calories and it will be added to the relevant field. also, E5 can be added anytime, the same as the other fields, and also, it can be added once by the LLM or can be edited manually. in E8 make sure that the database cannot be changed more than 3 times a day in the Jobs or trends only! it can be changed many times manually or multiple times by adding with the LLM (under the specifications of only one time per category in each item)"

- **E2 (revised).** Calories are estimated by the LLM on the fly:
  - *Add via free text:* the same intake call that extracts items also estimates calories for every meal it finds, filling `estimated_calories`, `calorie_basis`, `calorie_notes`. User-stated kcal always wins (`user_provided`).
  - *Manual edit of a meal:* if the description changed and the user did not type a calorie value, the backend calls the LLM to re-estimate that meal and writes the result to the same row's calorie fields, in the same transaction as the edit. If the user typed a value, it is stored as `user_provided` with no LLM call. This is a deliberate, user-initiated exception to "LLM never writes to an existing record": it is limited to the calorie fields of the meal the user is editing.
  - *Delete / any change:* the day's `daily_calories` row is recomputed in the same transaction as a deterministic SQL sum of that date's meal estimates (no LLM call needed to add numbers), with `coverage_status` per E3.
  - If the LLM call fails or times out, the meal is still saved with `calorie_basis = 'unknown'` and the UI offers "re-estimate".
- **E5 (revised).** Sleep can be entered at any time of day, like any other field: once per date by the LLM (a second LLM insert for that date is rejected with the duplicate message), unlimited manual edits. The date it is stored under stays the wake-up date.
- **E8 (revised).** The 3-per-day limit applies **only** to Run now writes (job recommendations and trends), enforced by `job_reports.run_number` CHECK 1–3. Manual adds/edits/deletes (including job applications) and free-text LLM inserts are **not** rate-capped; they are limited only by the per-item uniqueness rules (e.g. one meal per type per day). A failed run still uses one of the 3 slots.

## E2 correction (2026-10-16 14:13, Owner) — supersedes the E2 amendment above
the owner's words: "E2 isn't deliberate exception to the "AI never edits a saved record" rule, because that if a meal is added/changed or whatever, the llm opens a new record with the changed calories, which mean, each day's total calorie is represented by sum of calories per date."

Implementation — **confirmed by Owner 2026-10-16 14:15 ("Estimates table" on decision card)**:
- Calorie fields move out of `meals` into a new append-only table `meal_calorie_estimates(id, user_id, meal_id FK → meals ON DELETE CASCADE, estimated_calories, calorie_basis, calorie_notes, model, created_at)`. The LLM only ever INSERTs here; no row is updated.
- Meal added via free text → meal row + first estimate row. Meal description edited manually → meal row edited by the user; LLM inserts a **new** estimate row (user-typed kcal → new row with `user_provided`). Meal deleted → its estimates go with it.
- A meal's current calories = its newest estimate row. The day's total = SQL sum of the current estimates of that date's meals.
- `daily_calories` stops being a stored table and becomes a SQL view computing total + coverage_status (E3) per date, so there is nothing to keep in sync. Table count stays 17 (one stored table swapped for another).
- The "LLM never writes to an existing record" rule therefore holds with no exception.
