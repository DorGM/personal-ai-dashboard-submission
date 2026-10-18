"""Registry of user-owned tables and the exact guard triggers each must have (ADR-004 D2, D4, D6).

The migration runner compares every registered table's triggers with the templates below
before it commits, so a migration can't drop, weaken or mistype a guard. Adding a user-owned
table means registering it here and creating its triggers with `guard_triggers(table)`'s SQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Guard:
    # (grant table, column of the row holding the granted id): a DELETE is allowed only if a
    # deletion_grants row exists for one of these.
    grants: tuple[tuple[str, str], ...]
    # columns that may never change; None means the table is append-only (no UPDATE at all)
    immutable: tuple[str, ...] | None
    # further required triggers, by name → exact SQL
    extra: tuple[tuple[str, str], ...] = ()


# Grants exist only inside app/deletion.py's savepoint, so the table is always empty at rest.
GRANTS_TABLE_SQL = (
    "CREATE TABLE deletion_grants (\n    table_name TEXT NOT NULL,\n    row_id     INTEGER NOT NULL,\n"
    "    PRIMARY KEY (table_name, row_id)\n) STRICT"
)

_OWN = (("users", "user_id"),)


def _own(table: str) -> tuple[tuple[str, str], ...]:
    return ((table, "id"), *_OWN)


_TOP_LEVEL = ("weight_records", "meals", "workouts", "sleep_records", "step_records", "articles", "podcasts",
              "movies", "series", "job_applications", "job_search_profile", "tasks")

GUARDED: dict[str, Guard] = {
    "users": Guard(grants=(("users", "id"),), immutable=("id", "google_sub")),
    **{t: Guard(grants=_own(t), immutable=("id", "user_id")) for t in _TOP_LEVEL},
    "meal_calorie_estimates": Guard(
        grants=(("meals", "meal_id"), *_OWN),
        immutable=None,
    ),
    "job_reports": Guard(
        grants=_OWN,
        immutable=("id", "user_id"),
        extra=((
            "job_reports_key_immutable",
            "CREATE TRIGGER job_reports_key_immutable\nBEFORE UPDATE ON job_reports\n"
            "WHEN NEW.user_id IS NOT OLD.user_id OR NEW.report_date IS NOT OLD.report_date\n"
            "     OR NEW.run_number IS NOT OLD.run_number\n"
            "BEGIN\n    SELECT RAISE(ABORT, 'job_reports run slot is immutable');\nEND",
        ),),
    ),
    "job_recommendations": Guard(grants=(("job_reports", "report_id"), *_OWN),
                                 immutable=("id", "user_id", "report_id")),
    "industry_trends": Guard(grants=(("job_reports", "report_id"), *_OWN), immutable=("id", "user_id", "report_id")),
}


def _names(columns: tuple[str, ...]) -> str:
    return columns[0] if len(columns) == 1 else ", ".join(columns[:-1]) + " and " + columns[-1]


def guard_triggers(table: str) -> dict[str, str]:
    """The required triggers for a registered table: name → CREATE TRIGGER statement (no ';')."""
    guard = GUARDED[table]
    # Names come from the GUARDED registry above, never from input.
    checks = "\n          OR ".join(
        f"EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = '{g}' AND row_id = OLD.{c})"  # noqa: S608
        for g, c in guard.grants
    )
    message = f"{table}: delete requires an explicit authorized deletion (ADR-004)"
    triggers = {
        f"{table}_delete_requires_grant": (
            f"CREATE TRIGGER {table}_delete_requires_grant\nBEFORE DELETE ON {table}\nWHEN NOT ({checks})\n"
            f"BEGIN\n    SELECT RAISE(ABORT, '{message}');\nEND"
        )
    }
    if guard.immutable is None:
        triggers[f"{table}_no_update"] = (
            f"CREATE TRIGGER {table}_no_update\nBEFORE UPDATE ON {table}\n"
            f"BEGIN\n    SELECT RAISE(ABORT, '{table} is append-only');\nEND"
        )
    else:
        when = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in guard.immutable)
        verb = "is" if len(guard.immutable) == 1 else "are"
        triggers[f"{table}_identity_immutable"] = (
            f"CREATE TRIGGER {table}_identity_immutable\nBEFORE UPDATE ON {table}\nWHEN {when}\n"
            f"BEGIN\n    SELECT RAISE(ABORT, '{table}: {_names(guard.immutable)} {verb} immutable');\nEND"
        )
    triggers.update(dict(guard.extra))
    return triggers


# One pass, so a "--" inside a string literal or quoted identifier is not mistaken for a
# comment (and vice versa). Identifier quoting: "x", [x], `x`.
_TOKENS = re.compile(
    r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\[[^\]]*\]|`(?:[^`]|``)*`|--[^\n]*|/\*.*?\*/", re.DOTALL
)


def normalize_sql(sql: str, *, keep_literals: bool) -> str:
    """Comments removed, whitespace collapsed, keywords and bare names upper-cased.

    keep_literals=True (exact template comparison): string literals and quoted tokens are
    kept verbatim, case included. Quoting is never removed, because SQLite may read a
    double-quoted token as a string (so `WHEN "NOT (...)"` would be a constant condition).
    keep_literals=False (keyword searches): literals become '' and quoted names "".
    """
    parts: list[str] = []
    pos = 0
    for match in _TOKENS.finditer(sql):
        parts.append(" ".join(sql[pos:match.start()].upper().split()))
        text = match.group(0)
        if text.startswith(("--", "/*")):
            parts.append(" ")
        elif keep_literals:
            parts.append(f"\0{text}\0")  # sentinel-wrapped so punctuation rules leave it alone
        else:
            parts.append("''" if text.startswith("'") else '""')
        pos = match.end()
    parts.append(" ".join(sql[pos:].upper().split()))
    out = " ".join(" ".join(parts).split(" "))
    chunks = out.split("\0")
    for i in range(0, len(chunks), 2):  # even chunks are outside literals
        chunks[i] = re.sub(r"\s*([(),;=])\s*", r"\1", " ".join(chunks[i].split()))
    return "".join(chunks).strip().rstrip(";")
