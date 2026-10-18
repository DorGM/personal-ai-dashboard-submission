"""Job applications: identity = canonical job URL, else posting id, else company + title +
location (A-03 §3). Status and interview stage are edited manually; applied/interviewing are
"open" and highlighted in the UI. Several applications on one day are allowed."""

from __future__ import annotations

import sqlite3
from typing import Literal

from pydantic import Field

from app import canonical
from app.records._fields import Name, Opt, OptName, RecordIn
from app.records.core import DomainSpec, Repository
from app.records.dates import RecordDate
from app.urls import HttpUrl

WorkModel = Literal["onsite", "hybrid", "remote", "unknown"]
Status = Literal["applied", "interviewing", "accepted", "rejected"]
WORK_MODELS: tuple[str, ...] = ("onsite", "hybrid", "remote", "unknown")
STATUSES: tuple[str, ...] = ("applied", "interviewing", "accepted", "rejected")
OPEN_STATUSES: tuple[str, ...] = ("applied", "interviewing")


class JobApplicationIn(RecordIn):
    application_date: RecordDate
    company_name: Name
    job_title: Name
    company_location: Name
    work_model: WorkModel = "unknown"
    industry: Name
    status: Status = "applied"
    interview_stage: Opt[int] = Field(default=None, ge=1, le=100)
    job_url: Opt[HttpUrl] = None
    external_id: OptName = None


SPEC = DomainSpec(
    name="job_applications", table="job_applications", model=JobApplicationIn, date_column="application_date",
    columns=("application_date", "company_name", "job_title", "company_location", "work_model", "industry",
             "status", "interview_stage", "job_url", "external_id"),
    unique=("canonical_key",),
    key=lambda j: canonical.job_key(j.job_url, j.external_id, j.company_name, j.job_title, j.company_location),
)
repo = Repository(SPEC)
create, get, get_deleted, history, update = repo.create, repo.get, repo.get_deleted, repo.history, repo.update
soft_delete, restore, permanent_delete, list_deleted = repo.soft_delete, repo.restore, repo.permanent_delete, \
    repo.list_deleted
earliest = repo.earliest

_OPEN_SQL = ("SELECT * FROM job_applications WHERE user_id = ? AND deleted_at IS NULL"
             " AND status IN ('applied', 'interviewing') ORDER BY application_date DESC, id DESC")


def list_open(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """All active open applications (applied/interviewing), whatever their date; newest first."""
    return list(conn.execute(_OPEN_SQL, (user_id,)))


def is_open(row: sqlite3.Row) -> bool:
    return row["status"] in OPEN_STATUSES


def summary(row: sqlite3.Row) -> str:
    text = f"{row['job_title']} at {row['company_name']} · {row['status']}"
    if row["interview_stage"]:
        text += f" (stage {row['interview_stage']})"
    return text
