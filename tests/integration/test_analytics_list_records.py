"""T6 list_records against a real migrated database through a hardened read-only connection.

Rows are created with the B-04 repositories; a decoy second user with distinctive values must
never appear; soft-deleted rows are excluded (synthetic data only).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.analytics import policy
from app.analytics.readonly import is_read_only, open_read_only
from app.analytics.tools import ToolContext, ToolOutcome, execute_tool
from app.db import connect, migrate
from app.records import DOMAINS, meals
from app.users import get_or_create_user
from tests.unit.analytics.fake_metrics import FakeMetrics

T = date(2026, 10, 18)
DECOY_TEXT = "DECOY-UNIQUE-TEXT"


def no_filters(**set_: str) -> dict[str, str | None]:
    return {"meal_type": None, "workout_type": None, "category": None, "status": None, "work_model": None,
            **set_}


@dataclass
class World:
    path: Path
    uid: int
    decoy: int
    ids: dict[str, int]


def _add(conn: sqlite3.Connection, uid: int, domain: str, values: dict[str, Any]) -> int:
    model = DOMAINS[domain].SPEC.model.model_validate(values)
    return int(DOMAINS[domain].create(conn, uid, model, today=T))


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    path = tmp_path / "t6.db"
    conn = connect(path)
    migrate(conn)
    uid = get_or_create_user(conn, "t6-user", "user@example.com", "User")
    decoy = get_or_create_user(conn, "t6-decoy", "decoy@example.com", "Decoy")
    ids: dict[str, int] = {}
    for day, kg in (("2026-10-01", 80.0), ("2026-10-03", 79.0), ("2026-10-05", 81.0), ("2026-10-07", 78.5)):
        ids[f"weight {day}"] = _add(conn, uid, "weight", {"record_date": day, "weight_kg": kg})
    ids["meal lunch"] = _add(conn, uid, "meals", {"record_date": "2026-10-02", "meal_type": "lunch",
                                                  "description": "L" * 1500, "estimated_calories": 300})
    meals.add_estimate(conn, uid, ids["meal lunch"], meals.EstimateIn(estimated_calories=650,
                                                                       calorie_basis="rough_estimate"))
    ids["meal dinner"] = _add(conn, uid, "meals", {"record_date": "2026-10-02", "meal_type": "dinner",
                                                   "description": "Rice"})
    ids["task pending"] = _add(conn, uid, "tasks", {"task_date": "2026-10-20", "title": "Future call",
                                                    "category": "career", "start_time": "09:30"})
    ids["task done"] = _add(conn, uid, "tasks", {"task_date": "2026-10-10", "title": "Laundry",
                                                 "category": "housework", "status": "completed"})
    ids["job"] = _add(conn, uid, "job_applications", {
        "application_date": "2026-10-04", "company_name": "Example Ltd", "job_title": "Engineer",
        "company_location": "Haifa", "industry": "Software", "work_model": "remote", "status": "interviewing",
        "job_url": "https://jobs.example.com/secret-path"})
    ids["article"] = _add(conn, uid, "articles", {"record_date": "2026-10-06", "title": "A piece",
                                                  "source": "Example News", "url": "https://example.com/hidden"})
    ids["deleted weight"] = _add(conn, uid, "weight", {"record_date": "2026-10-09", "weight_kg": 70.0})
    DOMAINS["weight"].soft_delete(conn, uid, ids["deleted weight"], confirm="delete")
    # Decoy: same dates, distinctive values, in every domain used above.
    for day in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"):
        _add(conn, decoy, "weight", {"record_date": day, "weight_kg": 444.4})
    _add(conn, decoy, "meals", {"record_date": "2026-10-02", "meal_type": "lunch", "description": DECOY_TEXT,
                                "estimated_calories": 9999})
    _add(conn, decoy, "tasks", {"task_date": "2026-10-10", "title": DECOY_TEXT, "category": "career"})
    _add(conn, decoy, "job_applications", {"application_date": "2026-10-04", "company_name": DECOY_TEXT,
                                           "job_title": "X", "company_location": "Y", "industry": "Z"})
    conn.close()
    yield World(path, uid, decoy, ids)


@pytest.fixture
def ctx(world: World) -> Iterator[ToolContext]:
    conn = open_read_only(world.path)
    yield ToolContext(world.uid, conn, T, FakeMetrics())
    conn.close()


def call(ctx: ToolContext, domain: str, start: str = "2026-10-01", end: str = "2026-10-18", *,
         filters: dict[str, str | None] | None = None, sort: str = "date_desc", limit: int = 50) -> ToolOutcome:
    return execute_tool(ctx, "list_records", json.dumps({"domain": domain, "start": start, "end": end,
                                                         "filters": filters, "sort": sort, "limit": limit}))


def records(ctx: ToolContext, domain: str, **kwargs: Any) -> dict[str, Any]:
    outcome = call(ctx, domain, **kwargs)
    assert outcome.ok, outcome.result
    assert len(json.dumps(outcome.result, separators=(",", ":")).encode()) <= policy.MAX_TOOL_RESULT_BYTES
    return outcome.result


def test_connection_is_read_only(ctx: ToolContext, world: World) -> None:
    assert is_read_only(ctx.conn)
    writable = connect(world.path)
    try:
        with pytest.raises(ValueError, match="hardened"):
            call(ToolContext(world.uid, writable, T, FakeMetrics()), "weight")
    finally:
        writable.close()


def test_weight_records_exclude_decoy_and_soft_deleted(ctx: ToolContext, world: World) -> None:
    r = records(ctx, "weight")
    assert r["total_matching"] == 4 and r["returned"] == 4 and r["truncated"] is False
    assert [x["date"] for x in r["records"]] == ["2026-10-07", "2026-10-05", "2026-10-03", "2026-10-01"]
    assert r["records"][0] == {"record_id": world.ids["weight 2026-10-07"], "date": "2026-10-07",
                               "fields": {"weight_kg": 78.5}, "untrusted_text": {}}
    blob = json.dumps(r)
    assert "444.4" not in blob and "70.0" not in blob
    assert r["period"] == {"start": "2026-10-01", "end": "2026-10-18", "requested_start": "2026-10-01",
                           "clipped": False} and r["filters"] == {}


def test_start_is_clipped_to_the_earliest_live_record(ctx: ToolContext, world: World) -> None:
    r = records(ctx, "weight", start="2026-09-01")
    assert r["period"] == {"start": "2026-10-01", "end": "2026-10-18", "requested_start": "2026-09-01",
                           "clipped": True}
    assert r["total_matching"] == 4
    # The decoy's and soft-deleted rows never decide the clip: jobs start at the user's 2026-10-04.
    jobs = records(ctx, "job_applications", start="2026-09-01")
    assert jobs["period"]["start"] == "2026-10-04" and jobs["period"]["clipped"] is True
    # No records at all, or earliest record after the period: the requested period is kept.
    movies = records(ctx, "movies", start="2026-09-01")
    assert movies["period"] == {"start": "2026-09-01", "end": "2026-10-18", "requested_start": "2026-09-01",
                                "clipped": False}
    before = records(ctx, "weight", start="2026-09-01", end="2026-09-30")
    assert before["period"]["clipped"] is False and before["total_matching"] == 0


def test_soft_deleted_earliest_record_does_not_clip(world: World) -> None:
    conn = connect(world.path)
    early = _add(conn, world.uid, "sleep", {"record_date": "2026-09-10", "hours_slept": 7})
    _add(conn, world.uid, "sleep", {"record_date": "2026-10-12", "hours_slept": 8})
    DOMAINS["sleep"].soft_delete(conn, world.uid, early, confirm="delete")
    conn.close()
    ro = open_read_only(world.path)
    try:
        r = records(ToolContext(world.uid, ro, T, FakeMetrics()), "sleep", start="2026-09-01")
    finally:
        ro.close()
    assert r["period"]["start"] == "2026-10-12" and r["period"]["clipped"] is True


@pytest.mark.parametrize(("sort", "expected"), [
    ("date_asc", [80.0, 79.0, 81.0, 78.5]),
    ("date_desc", [78.5, 81.0, 79.0, 80.0]),
    ("value_desc", [81.0, 80.0, 79.0, 78.5]),
    ("value_asc", [78.5, 79.0, 80.0, 81.0]),
])
def test_sort_keys(ctx: ToolContext, sort: str, expected: list[float]) -> None:
    assert [x["fields"]["weight_kg"] for x in records(ctx, "weight", sort=sort)["records"]] == expected


def test_limit_detects_truncation(ctx: ToolContext) -> None:
    r = records(ctx, "weight", limit=3)
    assert (r["total_matching"], r["returned"], r["truncated"]) == (4, 3, True)
    r = records(ctx, "weight", limit=4)
    assert (r["returned"], r["truncated"]) == (4, False)
    for bad in (0, policy.MAX_LIST_LIMIT + 1):
        assert call(ctx, "weight", limit=bad).result["error"]["code"] == "invalid_arguments"


def test_date_range_is_inclusive(ctx: ToolContext) -> None:
    r = records(ctx, "weight", start="2026-10-03", end="2026-10-05")
    assert [x["date"] for x in r["records"]] == ["2026-10-05", "2026-10-03"]


def test_meals_expose_latest_estimate_and_truncate_text(ctx: ToolContext, world: World) -> None:
    r = records(ctx, "meals", sort="value_desc")
    lunch, dinner = r["records"]
    assert lunch["record_id"] == world.ids["meal lunch"]
    assert lunch["fields"] == {"meal_type": "lunch", "calories": 650, "calorie_basis": "rough_estimate"}
    assert lunch["untrusted_text"]["description"] == "L" * 200
    assert dinner["fields"] == {"meal_type": "dinner", "calories": None, "calorie_basis": "unknown"}
    assert DECOY_TEXT not in json.dumps(r) and "9999" not in json.dumps(r)
    filtered = records(ctx, "meals", filters=no_filters(meal_type="dinner"))
    assert [x["record_id"] for x in filtered["records"]] == [world.ids["meal dinner"]]
    assert filtered["filters"] == {"meal_type": "dinner"} and filtered["total_matching"] == 1


def test_tasks_may_look_14_days_ahead_and_filter(ctx: ToolContext, world: World) -> None:
    r = records(ctx, "tasks", end="2026-11-01", sort="date_asc")
    assert [x["record_id"] for x in r["records"]] == [world.ids["task done"], world.ids["task pending"]]
    assert r["records"][1]["fields"] == {"category": "career", "status": "pending", "start_time": "09:30"}
    assert r["records"][1]["untrusted_text"] == {"title": "Future call", "description": None}
    assert call(ctx, "tasks", end="2026-11-02").result["error"]["code"] == "invalid_range"
    done = records(ctx, "tasks", end="2026-11-01", filters=no_filters(status="completed", category="housework"))
    assert [x["record_id"] for x in done["records"]] == [world.ids["task done"]]
    assert records(ctx, "tasks", end="2026-11-01", filters=no_filters(category="family"))["total_matching"] == 0


def test_job_applications_never_include_urls(ctx: ToolContext, world: World) -> None:
    r = records(ctx, "job_applications", filters=no_filters(status="interviewing", work_model="remote"))
    (job,) = r["records"]
    assert job["fields"] == {"work_model": "remote", "status": "interviewing", "interview_stage": None}
    assert job["untrusted_text"] == {"company_name": "Example Ltd", "job_title": "Engineer",
                                     "company_location": "Haifa", "industry": "Software"}
    assert "secret-path" not in json.dumps(r) and DECOY_TEXT not in json.dumps(r)
    article = records(ctx, "articles")["records"][0]
    assert article["untrusted_text"] == {"title": "A piece", "source": "Example News"}
    assert "hidden" not in json.dumps(article)


@pytest.mark.parametrize(("domain", "kwargs", "code"), [
    ("weight", {"end": "2026-10-19"}, "invalid_range"),
    ("weight", {"start": "2025-10-17"}, "invalid_range"),          # 367 days
    ("weight", {"start": "2026-10-05", "end": "2026-10-04"}, "invalid_range"),
    ("articles", {"sort": "value_desc"}, "not_supported"),
    ("movies", {"sort": "value_asc"}, "not_supported"),
    ("series", {"sort": "value_asc"}, "not_supported"),
    ("job_applications", {"sort": "value_desc"}, "not_supported"),
    ("tasks", {"sort": "value_desc"}, "not_supported"),
    ("weight", {"filters": no_filters(meal_type="lunch")}, "invalid_filter"),
    ("tasks", {"filters": no_filters(status="applied")}, "invalid_filter"),
    ("job_applications", {"filters": no_filters(status="completed")}, "invalid_filter"),
    ("meals", {"filters": no_filters(category="family")}, "invalid_filter"),
    ("job_reports", {}, "invalid_arguments"),
])
def test_rejections(ctx: ToolContext, domain: str, kwargs: dict[str, Any], code: str) -> None:
    outcome = call(ctx, domain, **kwargs)
    assert not outcome.ok and set(outcome.result) == {"error"}
    assert outcome.result["error"]["code"] == code


@pytest.mark.parametrize("domain", sorted(DOMAINS))
def test_every_domain_query_runs_on_the_real_schema(ctx: ToolContext, domain: str) -> None:
    r = records(ctx, domain, sort="date_asc")
    assert r["domain"] == domain and r["returned"] == len(r["records"])


def test_large_text_results_are_trimmed_to_the_size_limit(world: World) -> None:
    conn = connect(world.path)
    for day in range(1, 19):
        for meal_type in ("breakfast", "snacks", "night"):
            _add(conn, world.uid, "meals", {"record_date": f"2026-10-{day:02d}", "meal_type": meal_type,
                                            "description": "é" * 300})
    conn.close()
    ro = open_read_only(world.path)
    try:
        r = records(ToolContext(world.uid, ro, T, FakeMetrics()), "meals", limit=50)
    finally:
        ro.close()
    assert r["total_matching"] == 56 and r["truncated"] is True
    assert 0 < r["returned"] == len(r["records"]) < 50
