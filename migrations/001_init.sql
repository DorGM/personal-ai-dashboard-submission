-- 001_init: schema approved in Phase A (A-03 v3) with the data-preservation safeguards of
-- docs/architecture/ADR-004 (no cascading deletes, deletes only via app/deletion.py grants,
-- immutable record identity).
-- Conventions: STRICT tables; dates are 'YYYY-MM-DD' (Asia/Jerusalem calendar date);
-- created_at/updated_at are UTC ISO-8601. Business uniqueness uses canonical_key computed
-- by app/canonical.py. Child rows carry (parent_id, user_id) composite FKs so a row can
-- never point at another user's parent.

CREATE TABLE users (
    id           INTEGER PRIMARY KEY,
    google_sub   TEXT NOT NULL UNIQUE,
    email        TEXT NOT NULL,
    display_name TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;

-- ---------------------------------------------------------------- Food & health
CREATE TABLE weight_records (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    record_date TEXT NOT NULL CHECK (date(record_date) IS record_date),
    weight_kg   REAL NOT NULL CHECK (weight_kg > 0 AND weight_kg < 500),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, record_date)
) STRICT;

CREATE TABLE meals (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    record_date TEXT NOT NULL CHECK (date(record_date) IS record_date),
    meal_type   TEXT NOT NULL CHECK (meal_type IN ('breakfast', 'lunch', 'dinner', 'night', 'snacks')),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, record_date, meal_type),
    UNIQUE (id, user_id)
) STRICT;

-- Append-only (A-03 E2 correction): every add/edit of a meal inserts a new estimate row;
-- a meal's current calories are its newest row. Updates are blocked by trigger.
CREATE TABLE meal_calorie_estimates (
    id                 INTEGER PRIMARY KEY,
    user_id            INTEGER NOT NULL,
    meal_id            INTEGER NOT NULL,
    estimated_calories INTEGER CHECK (estimated_calories IS NULL OR estimated_calories BETWEEN 0 AND 10000),
    calorie_basis      TEXT NOT NULL CHECK (calorie_basis IN
                           ('user_provided', 'reference_based_estimate', 'rough_estimate', 'unknown')),
    calorie_notes      TEXT,
    model              TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (calorie_basis <> 'unknown' OR estimated_calories IS NULL),
    CHECK (calorie_basis = 'unknown' OR estimated_calories IS NOT NULL),
    FOREIGN KEY (meal_id, user_id) REFERENCES meals(id, user_id) ON DELETE RESTRICT
) STRICT;
CREATE INDEX idx_meal_calorie_estimates_meal ON meal_calorie_estimates (meal_id, id);

CREATE TRIGGER meal_calorie_estimates_no_update
BEFORE UPDATE ON meal_calorie_estimates
BEGIN
    SELECT RAISE(ABORT, 'meal_calorie_estimates is append-only');
END;


CREATE TABLE workouts (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    record_date      TEXT NOT NULL CHECK (date(record_date) IS record_date),
    workout_type     TEXT NOT NULL CHECK (workout_type IN ('aerobic', 'weights', 'bodyweight', 'stick_spinning')),
    intensity        TEXT CHECK (intensity IS NULL OR
                         (workout_type = 'stick_spinning' AND intensity IN ('light', 'medium', 'fast'))),
    duration_minutes INTEGER CHECK (duration_minutes IS NULL OR duration_minutes BETWEEN 1 AND 1440),
    description      TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, record_date, workout_type)
) STRICT;

-- record_date = wake-up date (A-03 E5).
CREATE TABLE sleep_records (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    record_date TEXT NOT NULL CHECK (date(record_date) IS record_date),
    hours_slept REAL NOT NULL CHECK (hours_slept > 0 AND hours_slept <= 24),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, record_date)
) STRICT;

CREATE TABLE step_records (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    record_date TEXT NOT NULL CHECK (date(record_date) IS record_date),
    steps_count INTEGER NOT NULL CHECK (steps_count >= 0),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, record_date)
) STRICT;

-- Daily total = sum of each meal's newest estimate for that date (no stored copy to drift).
-- coverage (A-03 E3): unknown = no meal has calories; complete = breakfast, lunch and dinner all
-- have calories; otherwise partial.
CREATE VIEW daily_calories AS
WITH latest AS (
    SELECT e.meal_id, e.estimated_calories
    FROM meal_calorie_estimates AS e
    WHERE e.id = (SELECT MAX(i.id) FROM meal_calorie_estimates AS i WHERE i.meal_id = e.meal_id)
)
SELECT
    m.user_id,
    m.record_date,
    SUM(l.estimated_calories)   AS total_calories,
    COUNT(*)                    AS meal_count,
    COUNT(l.estimated_calories) AS meals_with_calories,
    CASE
        WHEN COUNT(l.estimated_calories) = 0 THEN 'unknown'
        WHEN SUM(m.meal_type IN ('breakfast', 'lunch', 'dinner') AND l.estimated_calories IS NOT NULL) = 3
             THEN 'estimated_complete'
        ELSE 'estimated_partial'
    END AS coverage_status
FROM meals AS m
LEFT JOIN latest AS l ON l.meal_id = m.id
GROUP BY m.user_id, m.record_date;

-- ---------------------------------------------------------------- Content consumption
CREATE TABLE articles (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    record_date   TEXT NOT NULL CHECK (date(record_date) IS record_date),
    title         TEXT NOT NULL CHECK (length(trim(title)) > 0),
    source        TEXT,
    url           TEXT,
    canonical_key TEXT NOT NULL CHECK (length(canonical_key) > 0),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, record_date, canonical_key)
) STRICT;

CREATE TABLE podcasts (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    record_date      TEXT NOT NULL CHECK (date(record_date) IS record_date),
    podcast_name     TEXT NOT NULL CHECK (length(trim(podcast_name)) > 0),
    episode_title    TEXT,
    duration_minutes INTEGER CHECK (duration_minutes IS NULL OR duration_minutes > 0),
    canonical_key    TEXT NOT NULL CHECK (length(canonical_key) > 0),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, record_date, canonical_key)
) STRICT;

CREATE TABLE movies (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    record_date   TEXT NOT NULL CHECK (date(record_date) IS record_date),
    title         TEXT NOT NULL CHECK (length(trim(title)) > 0),
    release_year  INTEGER CHECK (release_year IS NULL OR release_year BETWEEN 1880 AND 2100),
    canonical_key TEXT NOT NULL CHECK (length(canonical_key) > 0),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, record_date, canonical_key)
) STRICT;

CREATE TABLE series (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    record_date    TEXT NOT NULL CHECK (date(record_date) IS record_date),
    series_name    TEXT NOT NULL CHECK (length(trim(series_name)) > 0),
    season_number  INTEGER CHECK (season_number IS NULL OR season_number > 0),
    episode_number INTEGER CHECK (episode_number IS NULL OR episode_number > 0),
    canonical_key  TEXT NOT NULL CHECK (length(canonical_key) > 0),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, record_date, canonical_key)
) STRICT;

-- ---------------------------------------------------------------- Career
CREATE TABLE job_applications (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    application_date TEXT NOT NULL CHECK (date(application_date) IS application_date),
    company_name     TEXT NOT NULL CHECK (length(trim(company_name)) > 0),
    job_title        TEXT NOT NULL CHECK (length(trim(job_title)) > 0),
    company_location TEXT NOT NULL,
    work_model       TEXT NOT NULL CHECK (work_model IN ('onsite', 'hybrid', 'remote', 'unknown')),
    industry         TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('applied', 'interviewing', 'accepted', 'rejected')),
    interview_stage  INTEGER CHECK (interview_stage IS NULL OR interview_stage >= 1),
    job_url          TEXT,
    external_id      TEXT,
    canonical_key    TEXT NOT NULL CHECK (length(canonical_key) > 0),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, canonical_key)
) STRICT;
CREATE INDEX idx_job_applications_status ON job_applications (user_id, status);

CREATE TABLE job_search_profile (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
    preferences_text TEXT NOT NULL,
    cv_file_ref      TEXT,  -- server-side path under gitignored data/, never served
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;

-- One row per Run now. run_number 1..3 makes a 4th run on the same date impossible (A-03 E8).
CREATE TABLE job_reports (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    report_date    TEXT NOT NULL CHECK (date(report_date) IS report_date),
    run_number     INTEGER NOT NULL CHECK (run_number BETWEEN 1 AND 3),
    status         TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
    generated_at   TEXT,
    summary        TEXT,
    model          TEXT,
    schema_version TEXT,
    error          TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, report_date, run_number),
    UNIQUE (id, user_id)
) STRICT;
CREATE INDEX idx_job_reports_latest ON job_reports (user_id, report_date DESC, run_number DESC);

-- A run's slot can't be moved (A-03 E8); deletion is only possible via account deletion (ADR-004 D7).

CREATE TRIGGER job_reports_key_immutable
BEFORE UPDATE ON job_reports
WHEN NEW.user_id IS NOT OLD.user_id OR NEW.report_date IS NOT OLD.report_date
     OR NEW.run_number IS NOT OLD.run_number
BEGIN
    SELECT RAISE(ABORT, 'job_reports run slot is immutable');
END;

CREATE TABLE job_recommendations (
    id                   INTEGER PRIMARY KEY,
    user_id              INTEGER NOT NULL,
    report_id            INTEGER NOT NULL,
    company_name         TEXT NOT NULL,
    job_title            TEXT NOT NULL,
    location             TEXT NOT NULL,
    work_model           TEXT NOT NULL CHECK (work_model IN ('onsite', 'hybrid', 'remote', 'unknown')),
    industry             TEXT NOT NULL,
    job_url              TEXT NOT NULL,
    source_name          TEXT NOT NULL,
    retrieved_at         TEXT NOT NULL,
    match_reason         TEXT NOT NULL,
    external_id          TEXT,
    canonical_key        TEXT NOT NULL CHECK (length(canonical_key) > 0),
    first_recommended_at TEXT NOT NULL,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, canonical_key),
    FOREIGN KEY (report_id, user_id) REFERENCES job_reports(id, user_id) ON DELETE RESTRICT
) STRICT;
CREATE INDEX idx_job_recommendations_report ON job_recommendations (report_id);

CREATE TABLE industry_trends (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    report_id     INTEGER NOT NULL,
    snapshot_date TEXT NOT NULL CHECK (date(snapshot_date) IS snapshot_date),
    region        TEXT NOT NULL CHECK (region IN ('israel', 'global')),
    trend_type    TEXT NOT NULL CHECK (trend_type IN ('company', 'technology')),
    name          TEXT NOT NULL CHECK (length(trim(name)) > 0),
    description   TEXT NOT NULL,
    source_url    TEXT,
    rank          INTEGER CHECK (rank IS NULL OR rank >= 1),
    canonical_key TEXT NOT NULL CHECK (length(canonical_key) > 0),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, report_id, region, trend_type, canonical_key),
    FOREIGN KEY (report_id, user_id) REFERENCES job_reports(id, user_id) ON DELETE RESTRICT
) STRICT;
CREATE INDEX idx_industry_trends_report ON industry_trends (report_id, region, trend_type);

-- ---------------------------------------------------------------- Tasks
CREATE TABLE tasks (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    task_date     TEXT NOT NULL CHECK (date(task_date) IS task_date),
    start_time    TEXT CHECK (start_time IS NULL OR
                      (start_time GLOB '[0-2][0-9]:[0-5][0-9]' AND start_time < '24:00')),
    title         TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description   TEXT,
    category      TEXT NOT NULL CHECK (category IN ('family', 'career', 'friends', 'housework', 'other')),
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed')),
    canonical_key TEXT NOT NULL CHECK (length(canonical_key) > 0),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, task_date, canonical_key)
) STRICT;

-- ---------------------------------------------------------------- Data preservation (ADR-004)
-- A row can be deleted only while app/deletion.py holds a grant for it (or for its owning
-- parent, or for the whole user account) inside the same transaction. Connections enable
-- recursive_triggers, so the implicit delete of INSERT OR REPLACE is blocked too.
CREATE TABLE deletion_grants (
    table_name TEXT NOT NULL,
    row_id     INTEGER NOT NULL,
    PRIMARY KEY (table_name, row_id)
) STRICT;

CREATE TRIGGER users_delete_requires_grant
BEFORE DELETE ON users
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.id))
BEGIN
    SELECT RAISE(ABORT, 'users: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER weight_records_delete_requires_grant
BEFORE DELETE ON weight_records
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'weight_records' AND row_id = OLD.id)
          OR EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'weight_records: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER meals_delete_requires_grant
BEFORE DELETE ON meals
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'meals' AND row_id = OLD.id)
          OR EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'meals: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER workouts_delete_requires_grant
BEFORE DELETE ON workouts
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'workouts' AND row_id = OLD.id)
          OR EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'workouts: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER sleep_records_delete_requires_grant
BEFORE DELETE ON sleep_records
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'sleep_records' AND row_id = OLD.id)
          OR EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'sleep_records: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER step_records_delete_requires_grant
BEFORE DELETE ON step_records
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'step_records' AND row_id = OLD.id)
          OR EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'step_records: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER articles_delete_requires_grant
BEFORE DELETE ON articles
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'articles' AND row_id = OLD.id)
          OR EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'articles: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER podcasts_delete_requires_grant
BEFORE DELETE ON podcasts
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'podcasts' AND row_id = OLD.id)
          OR EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'podcasts: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER movies_delete_requires_grant
BEFORE DELETE ON movies
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'movies' AND row_id = OLD.id)
          OR EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'movies: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER series_delete_requires_grant
BEFORE DELETE ON series
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'series' AND row_id = OLD.id)
          OR EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'series: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER job_applications_delete_requires_grant
BEFORE DELETE ON job_applications
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'job_applications' AND row_id = OLD.id)
          OR EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'job_applications: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER job_search_profile_delete_requires_grant
BEFORE DELETE ON job_search_profile
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'job_search_profile' AND row_id = OLD.id)
          OR EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'job_search_profile: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER tasks_delete_requires_grant
BEFORE DELETE ON tasks
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'tasks' AND row_id = OLD.id)
          OR EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'tasks: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER meal_calorie_estimates_delete_requires_grant
BEFORE DELETE ON meal_calorie_estimates
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'meals' AND row_id = OLD.meal_id)
          OR EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'meal_calorie_estimates: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER job_reports_delete_requires_grant
BEFORE DELETE ON job_reports
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'job_reports: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER job_recommendations_delete_requires_grant
BEFORE DELETE ON job_recommendations
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'job_reports' AND row_id = OLD.report_id)
          OR EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'job_recommendations: delete requires an explicit authorized deletion (ADR-004)');
END;

CREATE TRIGGER industry_trends_delete_requires_grant
BEFORE DELETE ON industry_trends
WHEN NOT (EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'job_reports' AND row_id = OLD.report_id)
          OR EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'users' AND row_id = OLD.user_id))
BEGIN
    SELECT RAISE(ABORT, 'industry_trends: delete requires an explicit authorized deletion (ADR-004)');
END;

-- Record identity is stable: ids, owners and parent links never change (ADR-004 D4).
-- The triggers fire on every UPDATE (no column list) so that updates through the rowid
-- aliases (rowid, _rowid_, oid) are caught too.
CREATE TRIGGER users_identity_immutable
BEFORE UPDATE ON users
WHEN NEW.id IS NOT OLD.id OR NEW.google_sub IS NOT OLD.google_sub
BEGIN
    SELECT RAISE(ABORT, 'users: id and google_sub are immutable');
END;

CREATE TRIGGER weight_records_identity_immutable
BEFORE UPDATE ON weight_records
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT, 'weight_records: id and user_id are immutable');
END;

CREATE TRIGGER meals_identity_immutable
BEFORE UPDATE ON meals
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT, 'meals: id and user_id are immutable');
END;

CREATE TRIGGER workouts_identity_immutable
BEFORE UPDATE ON workouts
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT, 'workouts: id and user_id are immutable');
END;

CREATE TRIGGER sleep_records_identity_immutable
BEFORE UPDATE ON sleep_records
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT, 'sleep_records: id and user_id are immutable');
END;

CREATE TRIGGER step_records_identity_immutable
BEFORE UPDATE ON step_records
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT, 'step_records: id and user_id are immutable');
END;

CREATE TRIGGER articles_identity_immutable
BEFORE UPDATE ON articles
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT, 'articles: id and user_id are immutable');
END;

CREATE TRIGGER podcasts_identity_immutable
BEFORE UPDATE ON podcasts
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT, 'podcasts: id and user_id are immutable');
END;

CREATE TRIGGER movies_identity_immutable
BEFORE UPDATE ON movies
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT, 'movies: id and user_id are immutable');
END;

CREATE TRIGGER series_identity_immutable
BEFORE UPDATE ON series
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT, 'series: id and user_id are immutable');
END;

CREATE TRIGGER job_applications_identity_immutable
BEFORE UPDATE ON job_applications
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT, 'job_applications: id and user_id are immutable');
END;

CREATE TRIGGER job_search_profile_identity_immutable
BEFORE UPDATE ON job_search_profile
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT, 'job_search_profile: id and user_id are immutable');
END;

CREATE TRIGGER tasks_identity_immutable
BEFORE UPDATE ON tasks
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT, 'tasks: id and user_id are immutable');
END;

CREATE TRIGGER job_reports_identity_immutable
BEFORE UPDATE ON job_reports
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT, 'job_reports: id and user_id are immutable');
END;

CREATE TRIGGER job_recommendations_identity_immutable
BEFORE UPDATE ON job_recommendations
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id OR NEW.report_id IS NOT OLD.report_id
BEGIN
    SELECT RAISE(ABORT, 'job_recommendations: id, user_id and report_id are immutable');
END;

CREATE TRIGGER industry_trends_identity_immutable
BEFORE UPDATE ON industry_trends
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id OR NEW.report_id IS NOT OLD.report_id
BEGIN
    SELECT RAISE(ABORT, 'industry_trends: id, user_id and report_id are immutable');
END;
