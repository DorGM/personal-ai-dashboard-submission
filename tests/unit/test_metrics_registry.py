"""B-05 registry and fixed-SQL rules (ADR-005 §3.3, §4.2 V9; contract §4)."""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest

from app.analytics import metrics
from app.analytics.metrics import REGISTRY, MetricDef

ROOT = Path(__file__).resolve().parents[2]
ANALYTICS_B05 = [ROOT / "app" / "analytics" / "metrics.py"]

EXPECTED: dict[str, tuple[str, str, str, float, str | None]] = {
    # id: (kind, unit, domain, min_delta, subtype_field) — ADR-005 §3.3 table and V9 defaults
    "sleep.hours": ("daily_value", "h", "sleep", 0.25, None),
    "steps.count": ("daily_value", "steps", "steps", 500, None),
    "weight.kg": ("daily_value", "kg", "weight", 0.5, None),
    "calories.daily_total": ("daily_value", "kcal", "meals", 100, None),
    "meals.count": ("event_count", "meals", "meals", 0.1, "meal_type"),
    "workouts.count": ("event_count", "workouts", "workouts", 0.1, "workout_type"),
    "workouts.duration_minutes": ("event_value", "min", "workouts", 5, "workout_type"),
    "articles.count": ("event_count", "items", "articles", 0.1, None),
    "podcasts.count": ("event_count", "items", "podcasts", 0.1, None),
    "podcasts.duration_minutes": ("event_value", "min", "podcasts", 5, None),
    "movies.count": ("event_count", "items", "movies", 0.1, None),
    "series.count": ("event_count", "items", "series", 0.1, None),
    "tasks.count": ("event_count", "tasks", "tasks", 0.1, "category"),
    "tasks.completion_rate": ("ratio", "%", "tasks", 5, "category"),
    "job_applications.count": ("event_count", "applications", "job_applications", 0.1, "status"),
    "job_applications.progress_rate": ("ratio", "%", "job_applications", 5, None),
}


def test_registry_is_the_closed_adr_set() -> None:
    assert set(REGISTRY) == set(EXPECTED)
    for metric_id, (kind, unit, domain, min_delta, subtype) in EXPECTED.items():
        m = REGISTRY[metric_id]
        assert (m.id, m.kind, m.unit, m.domain, m.min_delta, m.subtype_field) == \
            (metric_id, kind, unit, domain, min_delta, subtype)
        assert m.label and m.label.isascii()


def test_registry_and_definitions_are_read_only() -> None:
    with pytest.raises(TypeError):
        REGISTRY["x"] = REGISTRY["sleep.hours"]  # type: ignore[index]
    m = REGISTRY["tasks.count"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.unit = "x"  # type: ignore[misc]
    with pytest.raises(TypeError):
        m.allowed_filters["status"] = frozenset()  # type: ignore[index]
    assert isinstance(m.allowed_filters["status"], frozenset)


def test_filters_are_enum_typed_and_include_the_subtype() -> None:
    f = {k: v.allowed_filters for k, v in REGISTRY.items()}
    assert f["tasks.count"] == {"status": {"pending", "completed"},
                                "category": {"family", "career", "friends", "housework", "other"}}
    assert f["job_applications.count"] == {"status": {"applied", "interviewing", "accepted", "rejected"},
                                           "work_model": {"onsite", "hybrid", "remote", "unknown"}}
    assert f["meals.count"] == {"meal_type": {"breakfast", "lunch", "dinner", "night", "snacks"}}
    assert set(f["tasks.completion_rate"]) == {"category"}
    assert set(f["job_applications.progress_rate"]) == {"work_model"}
    assert f["sleep.hours"] == {} and f["calories.daily_total"] == {}
    for m in REGISTRY.values():
        if m.subtype_field is not None:
            assert m.subtype_field in m.allowed_filters


def test_every_metric_has_fixed_sql_with_matching_placeholders() -> None:
    for metric_id, m in REGISTRY.items():
        sql = metrics._OBS_SQL[metric_id]
        names = metrics._OBS_FILTERS.get(metric_id, ())
        assert set(names) == set(m.allowed_filters), metric_id
        assert sql.count("?") == 3 + 2 * len(names), metric_id
        if m.kind in ("daily_value", "event_value"):
            assert metrics._LATEST_SQL[metric_id].count("?") == 1 + 2 * len(names), metric_id
        else:
            assert metric_id not in metrics._LATEST_SQL
    assert set(metrics._DAYS_SQL) == set(metrics._BOUNDS_SQL) == set(metrics.DOMAINS)


def _sql_constants(path: Path) -> list[str]:
    return [n.value for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and re.search(r"\bSELECT\b", n.value)]


@pytest.mark.parametrize("path", ANALYTICS_B05, ids=lambda p: p.name)
def test_no_string_formatting_in_sql_module(path: Path) -> None:
    """Fixed SQL only: no f-strings, `%` formatting or `.format` anywhere in the module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        assert not isinstance(node, ast.JoinedStr), f"f-string at line {node.lineno}"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            assert not (isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)), node.lineno
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "format", f".format at line {node.lineno}"
            if node.func.attr in ("execute", "executemany", "executescript"):
                # the SQL argument is a constant name or a lookup in a constant dict
                assert isinstance(node.args[0], ast.Name | ast.Subscript), node.lineno


@pytest.mark.parametrize("path", ANALYTICS_B05, ids=lambda p: p.name)
def test_every_query_is_user_scoped_and_excludes_soft_deleted_rows(path: Path) -> None:
    queries = _sql_constants(path)
    assert len(queries) >= 40
    for sql in queries:
        assert "user_id = ?" in sql, sql
        assert "deleted_at IS NULL" in sql or "FROM daily_calories" in sql, sql
        if "FROM meals AS m" in sql:
            assert "m.deleted_at IS NULL" in sql
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE|PRAGMA|ATTACH|CREATE|DROP)\b", sql), sql


def test_queries_are_module_level_constants() -> None:
    """Every SQL literal lives in a module-level assignment, not inside a function."""
    tree = ast.parse(ANALYTICS_B05[0].read_text(encoding="utf-8"))
    for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(func):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not re.search(r"\bSELECT\b", node.value), f"{func.name}: inline SQL"


def test_metric_error_carries_code_and_message() -> None:
    err = metrics.MetricError("unknown_metric", "Unknown metric.")
    assert (err.code, err.message, str(err)) == ("unknown_metric", "Unknown metric.", "Unknown metric.")


def _fields(cls: Any) -> list[str]:
    return [f.name for f in dataclasses.fields(cls)]


def test_dataclass_fields_match_the_contract() -> None:
    assert _fields(MetricDef) == ["id", "kind", "unit", "domain", "label", "min_delta", "subtype_field",
                                  "allowed_filters"]
    assert _fields(metrics.Period) == ["start", "end"]
    assert _fields(metrics.ResolvedPeriod) == ["period", "requested_start", "clipped"]
    assert _fields(metrics.Coverage) == ["earliest_record", "latest_record", "documented_days", "total_days",
                                         "ratio", "undocumented_ranges", "truncated"]
    assert _fields(metrics.DailyPoint) == ["day", "value", "documented", "record_ids"]
    assert _fields(metrics.RecordRef) == ["day", "value", "record_id"]
    assert _fields(metrics.Group) == ["key", "n", "documented_days", "mean", "count", "rate_per_documented_day",
                                      "pct"]
    assert _fields(metrics.MetricSummary) == [
        "metric", "period", "coverage", "n_observations", "mean", "median", "min", "max", "sum", "min_record",
        "max_record", "first", "last", "change", "rate_per_documented_day", "ratio_numerator",
        "ratio_denominator", "ratio_pct", "groups", "groups_truncated", "latest_overall",
        "calorie_complete_days", "calorie_partial_days"]
    for cls in (MetricDef, metrics.Period, metrics.ResolvedPeriod, metrics.Coverage, metrics.DailyPoint,
                metrics.RecordRef, metrics.Group, metrics.MetricSummary):
        assert getattr(cls, "__dataclass_params__").frozen  # noqa: B009


def test_package_init_is_the_agreed_single_line() -> None:
    text = (ROOT / "app" / "analytics" / "__init__.py").read_text(encoding="utf-8")
    assert text == '"""Personal analytics: metric registry (B-05) and analytics agent (B-13/B-15)."""\n'
