"""Independent reference calculator over the Demo Mode fixture JSON (pure Python, no SQL).

Used by the B-14 pattern tests and meant for B-13's golden tests: it recomputes the expected
values straight from `demo/synthetic_v1.json` at a fixed anchor, so tool results computed by
SQL can be compared with numbers derived a completely different way.

Conventions (ADR-005 §3.3, §4.2; A-03): a record's date is `anchor + day`; periods are
inclusive date ranges; a *documented day* is a day with at least one record in the domain;
missing days are never zero; an explicit `steps_count` of 0 is a value. Thresholds below are
the Owner-approved defaults (O-7), copied here on purpose (not imported from app code).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import fmean, median
from typing import Any

FIXTURE = Path(__file__).resolve().parents[2] / "demo" / "synthetic_v1.json"
HISTORY_DAYS = (-29, 0)  # 30 days of history (Owner dataset spec, 2026-10-18)
WEEK_BLOCKS = ((-29, -23), (-22, -16), (-15, -9), (-8, -2))  # W1…W4: 7-day blocks from the first day
INTERVIEW_PREFIX = "Interview — "
ANCHOR = date(2026, 10, 17)  # golden-test anchor (ADR-005 §6.7)

# ADR-005 §4.2 defaults (O-7)
MIN_DELTA = {"sleep.hours": 0.25, "steps.count": 500, "weight.kg": 0.5, "calories.daily_total": 100,
             "minutes": 5, "rate_per_day": 0.1, "percent_points": 5}
MIN_R = 0.3
MIN_OBSERVATIONS = 3  # mean/value: documented days (daily values) or observations (events, ratios)
WEIGHT_CHANGE_MIN = 2
WEIGHT_TREND_MIN_POINTS, WEIGHT_TREND_MIN_SPAN = 4, 14
TREND_MIN_POINTS, TREND_MIN_SPAN, TREND_MIN_COVERAGE = 7, 14, 0.5
CORRELATION_MIN_PAIRS = 10
SPLIT_MIN_DAYS = 5
COMPARE_MIN_DAYS, COMPARE_MIN_COVERAGE, COMPARE_MAX_COVERAGE_GAP = 3, 0.4, 0.3

DATE_FIELD = {"tasks": "task_date", "job_applications": "application_date"}
DAILY_VALUE = {"sleep.hours": ("sleep_records", "hours_slept"), "steps.count": ("step_records", "steps_count"),
               "weight.kg": ("weight_records", "weight_kg")}


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def __contains__(self, d: object) -> bool:
        return isinstance(d, date) and self.start <= d <= self.end


@dataclass(frozen=True)
class Summary:
    """A daily-value summary over a period (T2-shaped, reduced)."""

    period: Period
    documented_days: int
    coverage: float
    mean: float | None
    median: float | None
    first: tuple[date, float] | None
    last: tuple[date, float] | None


def load(path: Path = FIXTURE) -> dict[str, Any]:
    with open(path, "rb") as f:
        data: dict[str, Any] = json.loads(f.read().decode("utf-8"))
    return data


class Reference:
    """Expected values for one fixture at one anchor date."""

    def __init__(self, data: dict[str, Any], anchor: date = ANCHOR) -> None:
        self.data = data
        self.anchor = anchor

    # ------------------------------------------------------------ basics
    def on(self, day: int) -> date:
        return self.anchor + timedelta(days=day)

    def period(self, first_day: int, last_day: int) -> Period:
        """Inclusive period from day offsets, e.g. period(-29, 0) = the last 30 days."""
        return Period(self.on(first_day), self.on(last_day))

    def rows(self, table: str) -> list[tuple[date, dict[str, Any]]]:
        return sorted(((self.on(r["day"]), r) for r in self.data["records"][table]), key=lambda x: x[0])

    def documented(self, table: str, period: Period) -> set[date]:
        return {d for d, _ in self.rows(table) if d in period}

    def coverage(self, table: str, period: Period) -> float:
        return len(self.documented(table, period)) / period.days

    def daily(self, metric: str) -> dict[date, float]:
        table, field = DAILY_VALUE[metric]
        return {d: float(r[field]) for d, r in self.rows(table)}

    # ------------------------------------------------------------ daily values
    def summarize(self, metric: str, period: Period) -> Summary:
        points = sorted((d, v) for d, v in self.daily(metric).items() if d in period)
        values = [v for _, v in points]
        return Summary(period, len(points), len(points) / period.days, fmean(values) if values else None,
                       median(values) if values else None, points[0] if points else None,
                       points[-1] if points else None)

    def comparable(self, a: Summary, b: Summary, *, count_metric: bool = False) -> bool:
        """V7: each period ≥ 3 documented days and coverage ≥ 0.4 (counts: coverage gap ≤ 0.3)."""
        ok = all(s.documented_days >= COMPARE_MIN_DAYS and s.coverage >= COMPARE_MIN_COVERAGE for s in (a, b))
        if count_metric:
            ok = ok and abs(a.coverage - b.coverage) <= COMPARE_MAX_COVERAGE_GAP
        return ok

    def trend(self, metric: str, period: Period) -> tuple[int, int, float | None]:
        """(n_points, span_days, OLS slope per week) over documented points only."""
        points = sorted((d, v) for d, v in self.daily(metric).items() if d in period)
        if len(points) < 2:
            return len(points), 0, None
        xs = [(d - points[0][0]).days for d, _ in points]
        ys = [v for _, v in points]
        mx, my = fmean(xs), fmean(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
        return len(points), xs[-1], slope * 7

    def pairs(self, x_metric: str, y_metric: str, period: Period, lag_days: int) -> list[tuple[float, float]]:
        """(x on day d, y on day d + lag) where both are documented and d is in the period."""
        x, y = self.daily(x_metric), self.daily(y_metric)
        return [(xv, y[d + timedelta(days=lag_days)]) for d, xv in sorted(x.items())
                if d in period and d + timedelta(days=lag_days) in y]

    @staticmethod
    def pearson(pairs: list[tuple[float, float]]) -> float | None:
        if len(pairs) < 2:
            return None
        xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
        mx, my = fmean(xs), fmean(ys)
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        sy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if sx == 0 or sy == 0:
            return None
        return sum((x - mx) * (y - my) for x, y in pairs) / (sx * sy)

    def split(self, y_metric: str, event_days: set[date], period: Period) -> tuple[list[float], list[float]]:
        """y values on documented y days with an event recorded, and on those with none recorded."""
        y = {d: v for d, v in self.daily(y_metric).items() if d in period}
        return [v for d, v in y.items() if d in event_days], [v for d, v in y.items() if d not in event_days]

    # ------------------------------------------------------------ events, ratios, calories
    def event_days(self, table: str, **where: Any) -> set[date]:
        return {d for d, r in self.rows(table) if all(r.get(k) == v for k, v in where.items())}

    def count(self, table: str, period: Period, **where: Any) -> int:
        return sum(1 for d, r in self.rows(table) if d in period and all(r.get(k) == v for k, v in where.items()))

    def weeks(self) -> list[Period]:
        """The four 7-day blocks W1…W4 (days −29…−2); days −1 and 0 follow them."""
        return [self.period(a, b) for a, b in WEEK_BLOCKS]

    def tasks_where(self, period: Period, *, category: str, title_prefix: str = "",
                    status: str | None = None) -> list[tuple[date, dict[str, Any]]]:
        return [(d, r) for d, r in self.rows("tasks") if d in period and r["category"] == category
                and str(r["title"]).startswith(title_prefix) and (status is None or r["status"] == status)]

    def per_week(self, count: int, period: Period) -> float:
        return count / period.days * 7

    def completion_rate(self, category: str, period: Period) -> tuple[int, int]:
        """(completed, all) tasks of a category dated in the period and not after the anchor."""
        rows = [r for d, r in self.rows("tasks") if d in period and d <= self.anchor and r["category"] == category]
        return sum(r["status"] == "completed" for r in rows), len(rows)

    def daily_calories(self, period: Period) -> dict[date, int | None]:
        """Per meal day: the sum of known kcal, or None when every meal's calories are unknown."""
        totals: dict[date, list[int]] = {}
        for d, r in self.rows("meals"):
            if d in period:
                kcal = r["calories"]["kcal"]
                totals.setdefault(d, [])
                if kcal is not None:
                    totals[d].append(kcal)
        return {d: (sum(v) if v else None) for d, v in totals.items()}
