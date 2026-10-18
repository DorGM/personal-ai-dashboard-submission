"""Meals (one per user, date and meal type) and their append-only calorie estimates.

A-03 E2 (as corrected by the Owner): calories live in `meal_calorie_estimates`; rows are only
ever inserted, never updated or deleted on their own. A meal's current calories are its newest
estimate row; the day's total comes from the `daily_calories` view (soft-deleted meals excluded).

- Create: a typed kcal value → a `user_provided` estimate; none → an `unknown` estimate.
  Callers such as the Demo Mode importer or AI intake may pass an explicit `EstimateIn`.
- Manual edit: a typed kcal → a new `user_provided` row; a changed description without kcal →
  a new `unknown` row (B-06 will re-estimate it); otherwise no new row.
- Permanent delete removes the meal together with its estimates (app.deletion).
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from app.records._fields import LongText, Opt, OptLongText, OptName, RecordIn
from app.records.core import DomainSpec, Repository, atomic
from app.records.dates import RecordDate

MealType = Literal["breakfast", "lunch", "dinner", "night", "snacks"]
CalorieBasis = Literal["user_provided", "reference_based_estimate", "rough_estimate", "unknown"]
MEAL_TYPES: tuple[str, ...] = ("breakfast", "lunch", "dinner", "night", "snacks")
Kcal = Opt[int]


class MealIn(RecordIn):
    """A meal as entered by the user; `estimated_calories` is the optional typed kcal value."""

    record_date: RecordDate
    meal_type: MealType
    description: LongText
    estimated_calories: Kcal = Field(default=None, ge=0, le=10_000)


class EstimateIn(RecordIn):
    """One calorie estimate row. `unknown` has no value; every other basis needs one."""

    estimated_calories: Kcal = Field(default=None, ge=0, le=10_000)
    calorie_basis: CalorieBasis
    calorie_notes: OptLongText = None
    model: OptName = None

    @model_validator(mode="after")
    def _value_matches_basis(self) -> EstimateIn:
        if (self.calorie_basis == "unknown") != (self.estimated_calories is None):
            raise ValueError("A calorie value is required unless the basis is 'unknown'.")
        return self


_LATEST = ("LEFT JOIN meal_calorie_estimates AS e ON e.id = (SELECT MAX(i.id) FROM meal_calorie_estimates AS i"
           " WHERE i.meal_id = r.id AND i.user_id = r.user_id)")
_ORDER = ("CASE r.meal_type WHEN 'breakfast' THEN 1 WHEN 'lunch' THEN 2 WHEN 'dinner' THEN 3"
          " WHEN 'night' THEN 4 ELSE 5 END")
_INSERT_ESTIMATE = ("INSERT INTO meal_calorie_estimates (user_id, meal_id, estimated_calories, calorie_basis,"
                    " calorie_notes, model) VALUES (?, ?, ?, ?, ?, ?)")
_DAY_TOTALS = ("SELECT record_date, total_calories, coverage_status, meal_count, meals_with_calories"
               " FROM daily_calories WHERE user_id = ? AND record_date BETWEEN ? AND ?")

SPEC = DomainSpec(
    name="meals", table="meals", model=MealIn, date_column="record_date",
    columns=("record_date", "meal_type", "description"), unique=("record_date", "meal_type"),
    order=_ORDER, joins=_LATEST,
    select="r.*, e.estimated_calories, e.calorie_basis, e.calorie_notes, e.model AS estimate_model",
)


def _typed_or_unknown(data: MealIn) -> EstimateIn:
    if data.estimated_calories is not None:
        return EstimateIn(estimated_calories=data.estimated_calories, calorie_basis="user_provided")
    return EstimateIn(calorie_basis="unknown")


class MealRepository(Repository[MealIn]):
    def create(self, conn: sqlite3.Connection, user_id: int, data: MealIn, *, today: date | None = None,
               estimate: EstimateIn | None = None) -> int:
        """Insert the meal and its first estimate (explicit, typed kcal, or unknown) atomically."""
        self._check(data, today)
        with atomic(conn):
            meal_id = self._insert_row(conn, user_id, data)
            self._insert_estimate(conn, user_id, meal_id, estimate or _typed_or_unknown(data))
        return meal_id

    def update(self, conn: sqlite3.Connection, user_id: int, record_id: int, data: MealIn, *,
               today: date | None = None, estimate: EstimateIn | None = None) -> None:
        """Manual edit; appends a new estimate row per the module rules (never edits one)."""
        self._check(data, today)
        with atomic(conn):
            before = self.get(conn, user_id, record_id)
            self._update_row(conn, user_id, record_id, data)
            if estimate is None and (data.estimated_calories is not None or data.description != before["description"]):
                estimate = _typed_or_unknown(data)
            if estimate is not None:
                self._insert_estimate(conn, user_id, record_id, estimate)

    def add_estimate(self, conn: sqlite3.Connection, user_id: int, meal_id: int, estimate: EstimateIn) -> int:
        """Append an estimate to an active meal of this user. Raises NotFound."""
        with atomic(conn):
            self.get(conn, user_id, meal_id)
            return self._insert_estimate(conn, user_id, meal_id, estimate)

    @staticmethod
    def _insert_estimate(conn: sqlite3.Connection, user_id: int, meal_id: int, estimate: EstimateIn) -> int:
        cursor = conn.execute(_INSERT_ESTIMATE, (user_id, meal_id, estimate.estimated_calories,
                                                 estimate.calorie_basis, estimate.calorie_notes, estimate.model))
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


repo = MealRepository(SPEC)
create, get, get_deleted, history, update = repo.create, repo.get, repo.get_deleted, repo.history, repo.update
soft_delete, restore, permanent_delete, list_deleted = repo.soft_delete, repo.restore, repo.permanent_delete, \
    repo.list_deleted
earliest = repo.earliest
add_estimate = repo.add_estimate


def estimates(conn: sqlite3.Connection, user_id: int, meal_id: int) -> list[sqlite3.Row]:
    """All estimate rows of one meal of this user, oldest first."""
    return list(conn.execute(
        "SELECT * FROM meal_calorie_estimates WHERE meal_id = ? AND user_id = ? ORDER BY id", (meal_id, user_id)))


def day_totals(conn: sqlite3.Connection, user_id: int, start: date, end: date) -> dict[str, sqlite3.Row]:
    """`daily_calories` rows (estimates) keyed by ISO date, for days with active meals."""
    rows = conn.execute(_DAY_TOTALS, (user_id, start.isoformat(), end.isoformat()))
    return {r["record_date"]: r for r in rows}


def summary(row: sqlite3.Row) -> str:
    return f"{str(row['meal_type']).capitalize()}: {row['description']}"
