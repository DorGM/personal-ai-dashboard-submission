"""Record repositories: the only write paths for the 11 MVP record tables (B-04).

One module per domain (weight, meals, workouts, sleep, steps, articles, podcasts, movies,
series, job_applications, tasks), each with a strict Pydantic input model and the functions
`create`, `get`, `history`, `update`, `soft_delete`, `restore`, `permanent_delete` and
`list_deleted`, all called as `(conn, user_id, …)` without a request. See app/records/core.py
for the shared rules (user scoping, canonical keys, duplicates, soft delete).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from app.records import (
    articles,
    job_applications,
    meals,
    movies,
    podcasts,
    series,
    sleep,
    steps,
    tasks,
    weight,
    workouts,
)
from app.records.core import Repository

DOMAINS: dict[str, ModuleType] = {m.SPEC.name: m for m in (
    weight, meals, workouts, sleep, steps, articles, podcasts, movies, series, job_applications, tasks)}
"""Domain name (ADR-005 Domain enum) → repository module."""


def repository(domain: str) -> Repository[Any]:
    """The repository of a domain name; KeyError for an unknown domain."""
    repo: Repository[Any] = DOMAINS[domain].repo
    return repo


@dataclass(frozen=True)
class DeletedItem:
    domain: str
    row: sqlite3.Row
    summary: str
    record_date: str
    deleted_at: str


def recently_deleted(conn: sqlite3.Connection, user_id: int) -> list[DeletedItem]:
    """The user's soft-deleted records across all domains, most recently deleted first."""
    items: list[DeletedItem] = []
    for name, module in DOMAINS.items():
        summarize: Callable[[sqlite3.Row], str] = module.summary
        for row in module.list_deleted(conn, user_id):
            items.append(DeletedItem(name, row, summarize(row), row[module.SPEC.date_column], row["deleted_at"]))
    items.sort(key=lambda i: i.deleted_at, reverse=True)
    return items
