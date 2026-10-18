"""B-04 record repositories: create, edit, duplicates, date rules, soft delete, restore,
permanent delete and history, for every MVP domain (synthetic data only)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.db import connect, migrate, transaction
from app.records import DOMAINS, meals, recently_deleted, repository, steps, tasks
from app.records.errors import DUPLICATE_MESSAGE, DeletedDuplicate, DuplicateRecord, InvalidRecord, NotFound
from app.users import get_or_create_user

T = date(2026, 10, 18)
D = "2026-10-10"
D2 = "2026-10-11"

# domain → (sample, a different record on the same date, an edit of the sample)
SAMPLES: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {
    "weight": ({"record_date": D, "weight_kg": 72.4}, {"record_date": D2, "weight_kg": 72.0},
               {"record_date": D, "weight_kg": 71.9}),
    "meals": ({"record_date": D, "meal_type": "lunch", "description": "Lentil soup"},
              {"record_date": D, "meal_type": "dinner", "description": "Rice"},
              {"record_date": D, "meal_type": "lunch", "description": "Lentil soup and bread"}),
    "workouts": ({"record_date": D, "workout_type": "aerobic", "duration_minutes": 30},
                 {"record_date": D, "workout_type": "weights"},
                 {"record_date": D, "workout_type": "aerobic", "duration_minutes": 45}),
    "sleep": ({"record_date": D, "hours_slept": 7.5}, {"record_date": D2, "hours_slept": 6},
              {"record_date": D, "hours_slept": 8}),
    "steps": ({"record_date": D, "steps_count": 9000}, {"record_date": D2, "steps_count": 0},
              {"record_date": D, "steps_count": 9100}),
    "articles": ({"record_date": D, "title": "Example piece", "url": "https://www.example.com/a?utm_source=x"},
                 {"record_date": D, "title": "Another piece", "source": "Example News"},
                 {"record_date": D, "title": "Example piece (updated)", "url": "https://example.com/a"}),
    "podcasts": ({"record_date": D, "podcast_name": "Example Cast", "episode_title": "Ep 1"},
                 {"record_date": D, "podcast_name": "Example Cast", "episode_title": "Ep 2"},
                 {"record_date": D, "podcast_name": "Example Cast", "episode_title": "Ep 1", "duration_minutes": 40}),
    "movies": ({"record_date": D, "title": "Example Movie", "release_year": 1999},
               {"record_date": D, "title": "Example Movie", "release_year": 2005},
               {"record_date": D, "title": "Example Movie (Director's cut)", "release_year": 1999}),
    "series": ({"record_date": D, "series_name": "Example Show", "season_number": 1, "episode_number": 1},
               {"record_date": D, "series_name": "Example Show", "season_number": 1, "episode_number": 2},
               {"record_date": D, "series_name": "Example Show", "season_number": 1, "episode_number": 3}),
    "job_applications": (
        {"application_date": D, "company_name": "Example Ltd", "job_title": "Backend Engineer",
         "company_location": "Tel Aviv", "industry": "Software", "job_url": "https://jobs.example.com/1"},
        {"application_date": D, "company_name": "Sample Inc", "job_title": "Data Engineer",
         "company_location": "Haifa", "industry": "Software"},
        {"application_date": D, "company_name": "Example Ltd", "job_title": "Backend Engineer",
         "company_location": "Tel Aviv", "industry": "Software", "job_url": "https://jobs.example.com/1",
         "status": "interviewing", "interview_stage": 2}),
    "tasks": ({"task_date": D, "title": "Call the bank", "category": "career"},
              {"task_date": D, "title": "Laundry", "category": "housework"},
              {"task_date": D, "title": "Call the bank", "category": "career", "status": "completed"}),
}
ALL = sorted(DOMAINS)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "r.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def users(conn: sqlite3.Connection) -> tuple[int, int]:
    return (get_or_create_user(conn, "test-a", "a@example.com", "A"),
            get_or_create_user(conn, "test-b", "b@example.com", "B"))


def model(domain: str, values: dict[str, Any]) -> Any:
    return DOMAINS[domain].SPEC.model.model_validate(values)


def meal(**values: Any) -> meals.MealIn:
    return meals.MealIn.model_validate(values)


def add(conn: sqlite3.Connection, uid: int, domain: str, values: dict[str, Any]) -> int:
    return int(DOMAINS[domain].create(conn, uid, model(domain, values), today=T))


def snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in names}


def healthy(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT COUNT(*) FROM deletion_grants").fetchone()[0] == 0


def test_every_mvp_domain_is_registered() -> None:
    assert set(SAMPLES) == set(DOMAINS) and len(DOMAINS) == 11
    tables = {m.SPEC.table for m in DOMAINS.values()}
    assert not tables & {"job_search_profile", "job_reports", "job_recommendations", "industry_trends"}


# ---------------------------------------------------------------- create / read / edit
@pytest.mark.parametrize("domain", ALL)
def test_create_get_and_edit(conn: sqlite3.Connection, users: tuple[int, int], domain: str) -> None:
    uid, _ = users
    sample, _, edited = SAMPLES[domain]
    rid = add(conn, uid, domain, sample)
    row = DOMAINS[domain].get(conn, uid, rid)
    assert row["user_id"] == uid and row["deleted_at"] is None
    DOMAINS[domain].update(conn, uid, rid, model(domain, edited), today=T)
    row = DOMAINS[domain].get(conn, uid, rid)
    for key, value in edited.items():
        assert row[key] == value, key
    assert row["updated_at"] >= row["created_at"]


@pytest.mark.parametrize("domain", ALL)
def test_duplicate_is_rejected_with_spec_message(conn: sqlite3.Connection, users: tuple[int, int],
                                                 domain: str) -> None:
    uid, other = users
    sample, distinct, _ = SAMPLES[domain]
    first = add(conn, uid, domain, sample)
    add(conn, uid, domain, distinct)  # a different record on the same day is fine
    before = snapshot(conn)
    with pytest.raises(DuplicateRecord) as err:
        add(conn, uid, domain, sample)
    assert str(err.value) == DUPLICATE_MESSAGE == "נתון זה כבר הוכנס, אם תרצה להוסיף אליו נתונים תכניס זאת ידנית"
    assert snapshot(conn) == before
    assert DOMAINS[domain].get(conn, uid, first)  # the existing record is untouched
    add(conn, other, domain, sample)  # uniqueness is per user


@pytest.mark.parametrize("domain", ALL)
def test_edit_into_another_records_key_is_rejected(conn: sqlite3.Connection, users: tuple[int, int],
                                                   domain: str) -> None:
    uid, _ = users
    sample, distinct, _ = SAMPLES[domain]
    add(conn, uid, domain, sample)
    second = add(conn, uid, domain, distinct)
    before = snapshot(conn)
    with pytest.raises(DuplicateRecord):
        DOMAINS[domain].update(conn, uid, second, model(domain, sample), today=T)
    assert snapshot(conn) == before


def test_canonical_identity_catches_cosmetic_variants(conn: sqlite3.Connection, users: tuple[int, int]) -> None:
    uid, _ = users
    add(conn, uid, "articles", {"record_date": D, "title": "X", "url": "https://www.example.com/a/"})
    with pytest.raises(DuplicateRecord):
        add(conn, uid, "articles", {"record_date": D, "title": "Y", "url": "http://example.com/a#top"})
    add(conn, uid, "tasks", {"task_date": D, "title": "Call  the BANK", "category": "other"})
    with pytest.raises(DuplicateRecord):
        add(conn, uid, "tasks", {"task_date": D, "title": "call the bank", "category": "family"})
    add(conn, uid, "tasks", {"task_date": D, "title": "call the bank", "category": "family", "start_time": "09:00"})


def test_edit_recomputes_canonical_key(conn: sqlite3.Connection, users: tuple[int, int]) -> None:
    uid, _ = users
    rid = add(conn, uid, "movies", {"record_date": D, "title": "Old title"})
    DOMAINS["movies"].update(conn, uid, rid, model("movies", {"record_date": D, "title": "New title"}), today=T)
    assert "new title" in DOMAINS["movies"].get(conn, uid, rid)["canonical_key"]
    add(conn, uid, "movies", {"record_date": D, "title": "Old title"})  # the old key is free again


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize(("domain", "bad"), [
    ("weight", {"weight_kg": 0}), ("weight", {"weight_kg": 500}), ("weight", {"weight_kg": "nan"}),
    ("sleep", {"hours_slept": 24.5}), ("steps", {"steps_count": -1}), ("steps", {"steps_count": 1.5}),
    ("meals", {"meal_type": "brunch"}), ("meals", {"description": "   "}), ("meals", {"estimated_calories": 10001}),
    ("workouts", {"intensity": "fast"}), ("workouts", {"duration_minutes": 0}),
    ("articles", {"url": "javascript:alert(1)"}), ("articles", {"url": "https://user:pw@example.com/"}),
    ("movies", {"release_year": 1800}), ("series", {"season_number": 0}),
    ("job_applications", {"status": "ghosted"}), ("job_applications", {"work_model": "space"}),
    ("job_applications", {"interview_stage": 0}), ("job_applications", {"job_url": "ftp://example.com/x"}),
    ("tasks", {"category": "school"}), ("tasks", {"start_time": "24:00"}), ("tasks", {"status": "done"}),
    ("weight", {"user_id": 2}),
])
def test_invalid_input_is_rejected_before_sql(domain: str, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model(domain, {**SAMPLES[domain][0], **bad})


TEXT_FIELDS = [
    ("meals", "description"), ("workouts", "description"), ("articles", "title"), ("articles", "source"),
    ("podcasts", "podcast_name"), ("podcasts", "episode_title"), ("movies", "title"), ("series", "series_name"),
    ("job_applications", "company_name"), ("job_applications", "job_title"),
    ("job_applications", "company_location"), ("job_applications", "industry"), ("job_applications", "external_id"),
    ("tasks", "title"), ("tasks", "description"),
]


@pytest.mark.parametrize(("domain", "name"), TEXT_FIELDS)
@pytest.mark.parametrize("invisible", ["\u200b", " \u200f\u200b\u2060 ", "\ufeff", "\x00\x07"])
def test_text_without_visible_characters_is_rejected(domain: str, name: str, invisible: str) -> None:
    with pytest.raises(ValidationError, match="visible text"):
        model(domain, {**SAMPLES[domain][0], name: invisible})


def test_every_text_field_is_covered() -> None:
    text_fields = {(d, n) for d, m in DOMAINS.items() for n, f in m.SPEC.model.model_fields.items()
                   if "str" in str(f.annotation) and n not in {"url", "job_url", "start_time"}}
    assert text_fields == set(TEXT_FIELDS)


def test_estimate_text_fields_need_visible_characters() -> None:
    for name in ("calorie_notes", "model"):
        with pytest.raises(ValidationError, match="visible text"):
            meals.EstimateIn.model_validate({"calorie_basis": "unknown", name: "\u200b"})


def test_visible_text_with_invisible_marks_is_kept() -> None:
    assert model("movies", {"record_date": D, "title": "\u200fסרט"}).title == "\u200fסרט"


def test_workout_intensity_allowed_for_stick_spinning() -> None:
    assert model("workouts", {"record_date": D, "workout_type": "stick_spinning", "intensity": "fast"})


@pytest.mark.parametrize("domain", ALL)
def test_date_rules(conn: sqlite3.Connection, users: tuple[int, int], domain: str) -> None:
    uid, _ = users
    sample = SAMPLES[domain][0]
    field = DOMAINS[domain].SPEC.date_column
    ahead = DOMAINS[domain].SPEC.days_ahead
    add(conn, uid, domain, {**SAMPLES[domain][1], field: (T + timedelta(days=ahead)).isoformat()})
    with pytest.raises(InvalidRecord):
        add(conn, uid, domain, {**sample, field: (T + timedelta(days=ahead + 1)).isoformat()})
    rid = add(conn, uid, domain, sample)
    with pytest.raises(InvalidRecord):
        DOMAINS[domain].update(conn, uid, rid, model(domain, {**sample, field: "2027-01-01"}), today=T)


def test_tasks_allow_14_days_ahead_others_none() -> None:
    assert tasks.SPEC.days_ahead == 14
    assert all(m.SPEC.days_ahead == 0 for n, m in DOMAINS.items() if n != "tasks")


# ---------------------------------------------------------------- isolation
@pytest.mark.parametrize("domain", ALL)
def test_other_users_records_are_not_found(conn: sqlite3.Connection, users: tuple[int, int], domain: str) -> None:
    uid, other = users
    sample, _, edited = SAMPLES[domain]
    rid = add(conn, uid, domain, sample)
    m = DOMAINS[domain]
    before = snapshot(conn)
    with pytest.raises(NotFound):
        m.get(conn, other, rid)
    with pytest.raises(NotFound):
        m.update(conn, other, rid, model(domain, edited), today=T)
    with pytest.raises(NotFound):
        m.soft_delete(conn, other, rid, confirm="delete")
    assert snapshot(conn) == before
    m.soft_delete(conn, uid, rid, confirm="delete")
    with pytest.raises(NotFound):
        m.restore(conn, other, rid)
    with pytest.raises(NotFound):
        m.permanent_delete(conn, other, rid, confirm="permanent")
    assert m.history(conn, other, today=T) is None and m.list_deleted(conn, other) == []
    assert m.get_deleted(conn, uid, rid)


# ---------------------------------------------------------------- soft delete, restore, purge
@pytest.mark.parametrize("domain", ALL)
def test_soft_delete_hides_reserves_key_and_restores(conn: sqlite3.Connection, users: tuple[int, int],
                                                     domain: str) -> None:
    uid, _ = users
    sample, _, _ = SAMPLES[domain]
    m = DOMAINS[domain]
    rid = add(conn, uid, domain, sample)
    with pytest.raises(InvalidRecord):
        m.soft_delete(conn, uid, rid, confirm="")
    m.soft_delete(conn, uid, rid, confirm="delete")
    with pytest.raises(NotFound):
        m.get(conn, uid, rid)
    with pytest.raises(NotFound):
        m.soft_delete(conn, uid, rid, confirm="delete")
    assert m.history(conn, uid, today=T) is None
    assert m.earliest(conn, uid) is None
    assert [i.row["id"] for i in recently_deleted(conn, uid)] == [rid]
    with pytest.raises(DeletedDuplicate) as err:
        add(conn, uid, domain, sample)
    assert (err.value.domain, err.value.record_id) == (domain, rid)
    assert str(err.value) != DUPLICATE_MESSAGE and "Restore" in str(err.value)
    m.restore(conn, uid, rid)
    assert m.get(conn, uid, rid)["deleted_at"] is None
    with pytest.raises(NotFound):
        m.restore(conn, uid, rid)
    assert recently_deleted(conn, uid) == []
    healthy(conn)


@pytest.mark.parametrize("domain", ALL)
def test_permanent_delete_only_after_soft_delete_and_confirmation(conn: sqlite3.Connection, users: tuple[int, int],
                                                                  domain: str) -> None:
    uid, other = users
    sample, distinct, _ = SAMPLES[domain]
    m = DOMAINS[domain]
    rid = add(conn, uid, domain, sample)
    keep = add(conn, uid, domain, distinct)
    add(conn, other, domain, sample)
    with pytest.raises(NotFound):
        m.permanent_delete(conn, uid, rid, confirm="permanent")  # still active
    m.soft_delete(conn, uid, rid, confirm="delete")
    before = snapshot(conn)
    for bad in ("", "delete", "PERMANENT", "yes"):
        with pytest.raises(InvalidRecord):
            m.permanent_delete(conn, uid, rid, confirm=bad)
    assert snapshot(conn) == before
    removed = m.permanent_delete(conn, uid, rid, confirm="permanent")
    after = snapshot(conn)
    table = m.SPEC.table
    assert [r for r in before[table] if r[0] != rid] == after[table]
    for name, rows in before.items():
        if name not in {table, "meal_calorie_estimates"}:
            assert after[name] == rows, name
    assert removed >= 1 and m.get(conn, uid, keep)
    with pytest.raises(NotFound):
        m.get_deleted(conn, uid, rid)
    add(conn, uid, domain, sample)  # the key is free again
    healthy(conn)


def test_permanent_delete_of_meal_removes_its_estimates_only(conn: sqlite3.Connection,
                                                             users: tuple[int, int]) -> None:
    uid, _ = users
    gone = meals.create(conn, uid, meal(record_date=D, meal_type="lunch", description="Soup",
                                                estimated_calories=300), today=T)
    meals.update(conn, uid, gone, meal(record_date=D, meal_type="lunch", description="Soup, bread"), today=T)
    kept = meals.create(conn, uid, meal(record_date=D, meal_type="dinner", description="Rice"), today=T)
    assert len(meals.estimates(conn, uid, gone)) == 2
    meals.soft_delete(conn, uid, gone, confirm="delete")
    assert meals.permanent_delete(conn, uid, gone, confirm="permanent") == 3
    assert meals.estimates(conn, uid, gone) == [] and len(meals.estimates(conn, uid, kept)) == 1
    healthy(conn)


def test_permanent_delete_failure_inside_caller_transaction_leaves_no_trace(conn: sqlite3.Connection,
                                                                            users: tuple[int, int]) -> None:
    uid, _ = users
    rid = add(conn, uid, "weight", SAMPLES["weight"][0])
    DOMAINS["weight"].soft_delete(conn, uid, rid, confirm="delete")
    before = snapshot(conn)
    with pytest.raises(RuntimeError), transaction(conn):
        DOMAINS["weight"].permanent_delete(conn, uid, rid, confirm="permanent")
        raise RuntimeError("caller fails after the delete")
    assert snapshot(conn) == before
    healthy(conn)


def test_write_inside_caller_transaction_uses_savepoint(conn: sqlite3.Connection, users: tuple[int, int]) -> None:
    """A rejected item does not undo earlier items of the same caller transaction (B-06, B-14)."""
    uid, _ = users
    with transaction(conn):
        add(conn, uid, "steps", SAMPLES["steps"][0])
        with pytest.raises(DuplicateRecord):
            add(conn, uid, "steps", SAMPLES["steps"][0])
        add(conn, uid, "sleep", SAMPLES["sleep"][0])
    assert conn.execute("SELECT COUNT(*) FROM step_records").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sleep_records").fetchone()[0] == 1


def test_repository_rejects_wrong_model(conn: sqlite3.Connection, users: tuple[int, int]) -> None:
    with pytest.raises(TypeError):
        repository("weight").create(conn, users[0], model("sleep", SAMPLES["sleep"][0]), today=T)


# ---------------------------------------------------------------- meals and calories
def test_meal_calorie_estimates_are_append_only(conn: sqlite3.Connection, users: tuple[int, int]) -> None:
    uid, _ = users
    typed = meals.create(conn, uid, meal(record_date=D, meal_type="lunch", description="Soup",
                                                 estimated_calories=350), today=T)
    unknown = meals.create(conn, uid, meal(record_date=D, meal_type="dinner", description="Rice"), today=T)
    assert [(e["estimated_calories"], e["calorie_basis"]) for e in meals.estimates(conn, uid, typed)] == [
        (350, "user_provided")]
    assert [(e["estimated_calories"], e["calorie_basis"]) for e in meals.estimates(conn, uid, unknown)] == [
        (None, "unknown")]
    first = tuple(meals.estimates(conn, uid, typed)[0])
    # edit without a description change or kcal → no new estimate
    meals.update(conn, uid, typed, meal(record_date=D, meal_type="lunch", description="Soup"), today=T)
    assert len(meals.estimates(conn, uid, typed)) == 1
    # description changed, no kcal → new unknown row
    meals.update(conn, uid, typed, meal(record_date=D, meal_type="lunch", description="Soup, bread"), today=T)
    # kcal typed → new user_provided row
    meals.update(conn, uid, typed, meal(record_date=D, meal_type="lunch", description="Soup, bread",
                                                estimated_calories=500), today=T)
    rows = meals.estimates(conn, uid, typed)
    assert [(e["estimated_calories"], e["calorie_basis"]) for e in rows] == [
        (350, "user_provided"), (None, "unknown"), (500, "user_provided")]
    assert tuple(rows[0]) == first  # earlier rows unchanged
    assert meals.get(conn, uid, typed)["estimated_calories"] == 500


def test_explicit_estimate_for_importers(conn: sqlite3.Connection, users: tuple[int, int]) -> None:
    uid, other = users
    est = meals.EstimateIn(estimated_calories=420, calorie_basis="reference_based_estimate",
                           calorie_notes="synthetic", model="synthetic-fixture")
    mid = meals.create(conn, uid, meal(record_date=D, meal_type="breakfast", description="Oats"),
                       today=T, estimate=est)
    row = meals.get(conn, uid, mid)
    assert (row["estimated_calories"], row["calorie_basis"], row["estimate_model"]) == (
        420, "reference_based_estimate", "synthetic-fixture")
    with pytest.raises(NotFound):
        meals.add_estimate(conn, other, mid, est)
    with pytest.raises(ValidationError):
        meals.EstimateIn(calorie_basis="rough_estimate")
    with pytest.raises(ValidationError):
        meals.EstimateIn(calorie_basis="unknown", estimated_calories=100)


def test_daily_calories_excludes_soft_deleted_meals(conn: sqlite3.Connection, users: tuple[int, int]) -> None:
    uid, _ = users
    ids = [meals.create(conn, uid, meal(record_date=D, meal_type=t, description=t, estimated_calories=k),
                        today=T) for t, k in (("breakfast", 300), ("lunch", 500), ("dinner", 700))]
    totals = meals.day_totals(conn, uid, date(2026, 10, 1), T)
    assert (totals[D]["total_calories"], totals[D]["coverage_status"]) == (1500, "estimated_complete")
    meals.soft_delete(conn, uid, ids[2], confirm="delete")
    totals = meals.day_totals(conn, uid, date(2026, 10, 1), T)
    assert (totals[D]["total_calories"], totals[D]["coverage_status"]) == (800, "estimated_partial")
    meals.soft_delete(conn, uid, ids[0], confirm="delete")
    meals.soft_delete(conn, uid, ids[1], confirm="delete")
    assert meals.day_totals(conn, uid, date(2026, 10, 1), T) == {}


# ---------------------------------------------------------------- history
def test_history_buckets_with_placeholders_and_explicit_zero(conn: sqlite3.Connection, users: tuple[int, int]) -> None:
    uid, other = users
    steps.create(conn, uid, steps.StepsIn(record_date=T - timedelta(days=3), steps_count=0), today=T)
    steps.create(conn, uid, steps.StepsIn(record_date=T - timedelta(days=1), steps_count=8000), today=T)
    steps.create(conn, other, steps.StepsIn(record_date=T - timedelta(days=10), steps_count=1), today=T)
    h = steps.history(conn, uid, today=T)
    assert h is not None
    assert (h.window.start, h.window.end) == (T - timedelta(days=3), T)  # clipped to earliest
    assert [d.day for d in h.days] == [T - timedelta(days=i) for i in range(4)]
    counts = {d.day: [r["steps_count"] for r in d.records] for d in h.days}
    assert counts == {T: [], T - timedelta(days=1): [8000], T - timedelta(days=2): [], T - timedelta(days=3): [0]}
    assert conn.execute("SELECT COUNT(*) FROM step_records WHERE user_id = ?", (uid,)).fetchone()[0] == 2


def test_history_default_is_15_days_and_ignores_soft_deleted_earliest(conn: sqlite3.Connection,
                                                                      users: tuple[int, int]) -> None:
    uid, _ = users
    old = steps.create(conn, uid, steps.StepsIn(record_date=T - timedelta(days=40), steps_count=1), today=T)
    steps.create(conn, uid, steps.StepsIn(record_date=T - timedelta(days=20), steps_count=2), today=T)
    h = steps.history(conn, uid, today=T)
    assert h is not None and len(h.days) == 15 and h.window.lower == T - timedelta(days=40)
    steps.soft_delete(conn, uid, old, confirm="delete")
    h = steps.history(conn, uid, today=T, end=T - timedelta(days=30))
    assert h is not None and h.window.lower == T - timedelta(days=20)
    assert (h.window.start, h.window.end) == (T - timedelta(days=20), T - timedelta(days=6))


def test_task_history_is_ascending_today_plus_14(conn: sqlite3.Connection, users: tuple[int, int]) -> None:
    uid, _ = users
    tasks.create(conn, uid, tasks.TaskIn(task_date=T + timedelta(days=2), title="B", category="other",
                                         start_time="10:00"), today=T)
    tasks.create(conn, uid, tasks.TaskIn(task_date=T + timedelta(days=2), title="A", category="family"), today=T)
    tasks.create(conn, uid, tasks.TaskIn(task_date=T + timedelta(days=2), title="C", category="friends",
                                         start_time="08:00"), today=T)
    h = tasks.history(conn, uid, today=T)
    assert h is not None and h.days[0].day == T and h.days[-1].day == T + timedelta(days=14)
    assert [r["title"] for r in h.days[2].records] == ["C", "B", "A"]


def test_task_status_toggle(conn: sqlite3.Connection, users: tuple[int, int]) -> None:
    uid, other = users
    rid = tasks.create(conn, uid, tasks.TaskIn(task_date=T, title="A", category="other"), today=T)
    tasks.set_status(conn, uid, rid, "completed")
    assert tasks.get(conn, uid, rid)["status"] == "completed"
    tasks.set_status(conn, uid, rid, "pending")
    assert tasks.get(conn, uid, rid)["status"] == "pending"
    with pytest.raises(NotFound):
        tasks.set_status(conn, other, rid, "completed")
    with pytest.raises(ValueError):
        tasks.set_status(conn, uid, rid, "done")


def test_open_applications_list(conn: sqlite3.Connection, users: tuple[int, int]) -> None:
    uid, other = users
    ja = DOMAINS["job_applications"]
    base = SAMPLES["job_applications"][1]
    for i, status in enumerate(("applied", "interviewing", "accepted", "rejected")):
        add(conn, uid, "job_applications", {**base, "job_title": f"Role {i}", "status": status,
                                            "application_date": "2026-01-0" + str(i + 1)})
    add(conn, other, "job_applications", base)
    assert sorted(r["status"] for r in ja.list_open(conn, uid)) == ["applied", "interviewing"]
