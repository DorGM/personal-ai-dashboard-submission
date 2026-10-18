-- 002_soft_delete: recoverable soft deletion (Owner decision O-1, option a; ADR-005 §11, ADR-004).
-- Each top-level user-owned MVP table gains a nullable deleted_at (UTC ISO-8601 timestamp).
-- A soft-deleted row is hidden from every list and history view but keeps its UNIQUE business
-- key reserved, so re-adding the same key offers Restore or Delete permanently instead.
-- meal_calorie_estimates follows its meal (no column of its own). The deferred tables
-- (job_search_profile, job_reports, job_recommendations, industry_trends) are unchanged:
-- they have no routes or writes in the MVP.
-- ADD COLUMN keeps every guard trigger untouched and needs no table rebuild (ADR-004 known limits).

ALTER TABLE weight_records ADD COLUMN deleted_at TEXT;
ALTER TABLE meals ADD COLUMN deleted_at TEXT;
ALTER TABLE workouts ADD COLUMN deleted_at TEXT;
ALTER TABLE sleep_records ADD COLUMN deleted_at TEXT;
ALTER TABLE step_records ADD COLUMN deleted_at TEXT;
ALTER TABLE articles ADD COLUMN deleted_at TEXT;
ALTER TABLE podcasts ADD COLUMN deleted_at TEXT;
ALTER TABLE movies ADD COLUMN deleted_at TEXT;
ALTER TABLE series ADD COLUMN deleted_at TEXT;
ALTER TABLE job_applications ADD COLUMN deleted_at TEXT;
ALTER TABLE tasks ADD COLUMN deleted_at TEXT;

-- The daily total must not count soft-deleted meals; otherwise unchanged from 001_init.
DROP VIEW daily_calories;

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
WHERE m.deleted_at IS NULL
GROUP BY m.user_id, m.record_date;
