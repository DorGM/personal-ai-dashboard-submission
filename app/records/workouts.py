"""Workouts: one per user, date and type (A-03 E4); intensity only for stick spinning."""

from __future__ import annotations

import sqlite3
from typing import Literal

from pydantic import Field, model_validator

from app.records._fields import Opt, OptLongText, RecordIn
from app.records.core import DomainSpec, Repository
from app.records.dates import RecordDate

WorkoutType = Literal["aerobic", "weights", "bodyweight", "stick_spinning"]
Intensity = Literal["light", "medium", "fast"]
WORKOUT_TYPES: tuple[str, ...] = ("aerobic", "weights", "bodyweight", "stick_spinning")
INTENSITIES: tuple[str, ...] = ("light", "medium", "fast")


class WorkoutIn(RecordIn):
    record_date: RecordDate
    workout_type: WorkoutType
    intensity: Opt[Intensity] = None
    duration_minutes: Opt[int] = Field(default=None, ge=1, le=1440)
    description: OptLongText = None

    @model_validator(mode="after")
    def _intensity_only_for_stick_spinning(self) -> WorkoutIn:
        if self.intensity is not None and self.workout_type != "stick_spinning":
            raise ValueError("Intensity applies only to stick spinning.")
        return self


SPEC = DomainSpec(name="workouts", table="workouts", model=WorkoutIn, date_column="record_date",
                  columns=("record_date", "workout_type", "intensity", "duration_minutes", "description"),
                  unique=("record_date", "workout_type"))
repo = Repository(SPEC)
create, get, get_deleted, history, update = repo.create, repo.get, repo.get_deleted, repo.history, repo.update
soft_delete, restore, permanent_delete, list_deleted = repo.soft_delete, repo.restore, repo.permanent_delete, \
    repo.list_deleted
earliest = repo.earliest


def summary(row: sqlite3.Row) -> str:
    parts = [row["workout_type"].replace("_", " ").capitalize()]
    if row["intensity"]:
        parts.append(row["intensity"])
    if row["duration_minutes"]:
        parts.append(f"{row['duration_minutes']} min")
    return " · ".join(parts)
