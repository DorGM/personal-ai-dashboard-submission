"""Synthetic test seed (B-03). Contains no real personal information.

Used by the schema, migration and data-preservation tests and by scripts/seed_synthetic.py.
It is NOT Demo Mode: it writes with raw INSERTs and its user is an ordinary (non-`demo:`)
identity. Demo Mode is app/demo.py, which imports demo/synthetic_v1.json through the B-04
repositories into a marked working copy.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from app import canonical
from app.db import transaction

DEMO_SUB = "demo-google-sub"
DEMO_EMAIL = "demo.user@example.com"


def seed(conn: sqlite3.Connection, today: date) -> int:
    """Insert a demo user with ~2 weeks of gappy data ending `today`. Returns the user id."""

    def d(days_ago: int) -> str:
        return (today - timedelta(days=days_ago)).isoformat()

    with transaction(conn):
        uid = conn.execute(
            "INSERT INTO users (google_sub, email, display_name) VALUES (?, ?, ?)",
            (DEMO_SUB, DEMO_EMAIL, "Demo User"),
        ).lastrowid
        assert uid is not None
        for ago, kg in [(13, 74.1), (9, 73.6), (4, 73.2), (1, 72.9)]:
            conn.execute(
                "INSERT INTO weight_records (user_id, record_date, weight_kg) VALUES (?, ?, ?)",
                (uid, d(ago), kg),
            )
        meals = [
            (3, "breakfast", "Oatmeal with banana", 350, "reference_based_estimate"),
            (3, "lunch", "Chicken salad", 520, "rough_estimate"),
            (3, "dinner", "Pasta with tomato sauce", 700, "rough_estimate"),
            (2, "breakfast", "Two eggs and toast", 400, "user_provided"),
            (2, "snacks", "Handful of almonds", None, "unknown"),
            (0, "lunch", "Hummus and pita", 610, "reference_based_estimate"),
        ]
        for ago, mtype, desc, kcal, basis in meals:
            mid = conn.execute(
                "INSERT INTO meals (user_id, record_date, meal_type, description) VALUES (?, ?, ?, ?)",
                (uid, d(ago), mtype, desc),
            ).lastrowid
            conn.execute(
                "INSERT INTO meal_calorie_estimates"
                " (user_id, meal_id, estimated_calories, calorie_basis, calorie_notes)"
                " VALUES (?, ?, ?, ?, ?)",
                (uid, mid, kcal, basis, "synthetic"),
            )
        for ago, wtype, intensity, minutes in [
            (5, "aerobic", None, 30), (5, "weights", None, 45), (3, "stick_spinning", "fast", 20),
            (1, "bodyweight", None, 25),
        ]:
            conn.execute(
                "INSERT INTO workouts (user_id, record_date, workout_type, intensity, duration_minutes)"
                " VALUES (?, ?, ?, ?, ?)",
                (uid, d(ago), wtype, intensity, minutes),
            )
        for ago, hours in [(6, 7.0), (3, 6.5), (2, 8.0), (0, 7.25)]:
            conn.execute(
                "INSERT INTO sleep_records (user_id, record_date, hours_slept) VALUES (?, ?, ?)",
                (uid, d(ago), hours),
            )
        for ago, steps in [(6, 8400), (5, 0), (2, 11200), (1, 9100)]:
            conn.execute(
                "INSERT INTO step_records (user_id, record_date, steps_count) VALUES (?, ?, ?)",
                (uid, d(ago), steps),
            )
        conn.execute(
            "INSERT INTO articles (user_id, record_date, title, source, url, canonical_key)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (uid, d(2), "Example article about databases", "Example News",
             "https://example.com/articles/db", canonical.article_key(
                 "Example article about databases", "Example News", "https://example.com/articles/db")),
        )
        conn.execute(
            "INSERT INTO series (user_id, record_date, series_name, season_number, episode_number,"
            " canonical_key) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, d(1), "Example Show", 1, 3, canonical.series_key("Example Show", 1, 3)),
        )
        conn.execute(
            "INSERT INTO job_applications (user_id, application_date, company_name, job_title,"
            " company_location, work_model, industry, status, interview_stage, job_url, canonical_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, d(7), "Example Ltd", "Backend Engineer", "Tel Aviv", "hybrid", "Software",
             "interviewing", 2, "https://jobs.example.com/123",
             canonical.job_key("https://jobs.example.com/123", None, "", "", "")),
        )
        for ago, title, cat in [
            (0, "Call parents", "family"), (1, "Prepare portfolio", "career"),
            (2, "Dinner with friends", "friends"), (3, "Laundry", "housework"), (5, "Renew ID", "other"),
        ]:
            when = (today + timedelta(days=ago)).isoformat()
            conn.execute(
                "INSERT INTO tasks (user_id, task_date, title, category, canonical_key)"
                " VALUES (?, ?, ?, ?, ?)",
                (uid, when, title, cat, canonical.task_key(None, title)),
            )
    return int(uid)
