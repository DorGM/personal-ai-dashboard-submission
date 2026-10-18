"""Aggregates view of the four data tabs (B-07): range selection, metric tiles, period
comparison and chart geometry.

Every number comes from `app.analytics.metrics` (`summarize`, `daily_points`, `coverage`)
called with the session user's id; this module runs no SQL of its own. Values are formatted
here (server-side) and the templates only place them, so `charts.html` stays logic-free.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.analytics import metrics
from app.analytics.metrics import REGISTRY, DailyPoint, MetricSummary, Period

PRESETS = (7, 14, 30)
DEFAULT_DAYS = 7
MAX_DAYS = metrics.MAX_SPAN_DAYS
MIN_COMPARE_DAYS = 3  # fewer documented days in either period -> "not enough data"


class RangeError(ValueError):
    """The requested range is invalid; the message is fixed text safe to show to the user."""


# ---------------------------------------------------------------- range selection
@dataclass(frozen=True)
class Selection:
    """The selected period, its preset (None for a custom range) and the previous equal-length period."""

    period: Period
    preset: int | None
    previous: Period

    @property
    def days(self) -> int:
        return (self.period.end - self.period.start).days + 1


def _previous(period: Period) -> Period:
    length = (period.end - period.start).days + 1
    return Period(period.start - timedelta(days=length), period.start - timedelta(days=1))


def _parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise RangeError("Enter dates as YYYY-MM-DD.") from None


def select_range(params: Mapping[str, str], today: date) -> Selection:
    """Selection from the query string: `days` in PRESETS, or custom `start` and `end`.

    Without parameters the last DEFAULT_DAYS days are selected. Raises RangeError when the
    custom range is not `start <= end <= today` or spans more than MAX_DAYS days, or when
    `days` is not a preset.
    """
    start_text, end_text = params.get("start", ""), params.get("end", "")
    if start_text or end_text:
        if not (start_text and end_text):
            raise RangeError("Choose both a start date and an end date.")
        start, end = _parse_day(start_text), _parse_day(end_text)
        if start > end:
            raise RangeError("The start date must not be after the end date.")
        if end > today:
            raise RangeError("The end date must not be after today.")
        if (end - start).days + 1 > MAX_DAYS:
            raise RangeError(f"A range may span at most {MAX_DAYS} days.")
        period = Period(start, end)
        return Selection(period, None, _previous(period))
    days_text = params.get("days", "")
    if days_text and days_text not in {str(n) for n in PRESETS}:
        raise RangeError("Choose 7, 14 or 30 days, or enter your own dates.")
    days = int(days_text) if days_text else DEFAULT_DAYS
    period = Period(today - timedelta(days=days - 1), today)
    return Selection(period, days, _previous(period))


def default_selection(today: date) -> Selection:
    return select_range({}, today)


# ---------------------------------------------------------------- formatting
_DECIMALS = {"h": 1, "kg": 1, "steps": 0, "kcal": 0, "min": 0, "%": 0}


def fmt(value: float | None, unit: str, *, decimals: int | None = None) -> str:
    """Display text of a value in its unit, e.g. "7.4 h", "8,250 steps", "62%"; "—" for None."""
    if value is None:
        return "—"
    places = decimals if decimals is not None else _DECIMALS.get(unit, 1)
    number = f"{value:,.{places}f}"
    if unit == "%":
        return f"{number}%"
    return f"{number} {unit}"


def fmt_signed(value: float, unit: str, decimals: int | None = None) -> str:
    text = fmt(abs(value), unit, decimals=decimals)
    return ("+" if value > 0 else "−" if value < 0 else "±") + text


# ---------------------------------------------------------------- chart geometry
WIDTH, HEIGHT, PAD_X, PAD_Y = 600, 180, 48, 20


@dataclass(frozen=True)
class Row:
    """One row of a chart's "Show data" table."""

    label: str
    value: str


@dataclass(frozen=True)
class LineChart:
    """A line with gaps: one polyline per run of documented days; undocumented days draw nothing."""

    title: str
    segments: tuple[str, ...]  # SVG `points` attribute values, runs of >= 2 points
    dots: tuple[tuple[float, float], ...]  # every documented point
    y_labels: tuple[tuple[float, str], ...]  # (y, text) for the min and max gridlines
    x_labels: tuple[tuple[float, str], ...]  # first and last day
    rows: tuple[Row, ...]
    documented: int
    total: int
    kind: str = "line"
    width: int = WIDTH
    height: int = HEIGHT


@dataclass(frozen=True)
class Bar:
    label: str
    value: float
    display: str
    x: float
    y: float
    w: float
    h: float
    css: str = ""


@dataclass(frozen=True)
class BarChart:
    title: str
    bars: tuple[Bar, ...]
    rows: tuple[Row, ...]
    kind: str = "bar"
    width: int = WIDTH
    height: int = HEIGHT


@dataclass(frozen=True)
class CompareBar:
    label: str
    previous: str
    current: str
    prev_w: float  # bar widths in a 0..100 % track (a pair shares one scale)
    cur_w: float
    verdict: str


@dataclass(frozen=True)
class CompareChart:
    title: str
    items: tuple[CompareBar, ...]
    rows: tuple[Row, ...]
    kind: str = "compare"


def _dmy(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def line_chart(title: str, points: list[DailyPoint], unit: str, *, from_zero: bool = False) -> LineChart:
    """Geometry of a line chart. A None value (undocumented day) ends the current segment."""
    values = [p.value for p in points if p.value is not None]
    total = len(points)
    lo = min(values) if values else 0.0
    hi = max(values) if values else 1.0
    if from_zero:
        lo = min(lo, 0.0)
    if hi == lo:
        lo, hi = lo - 1.0, hi + 1.0
    inner_w, inner_h = WIDTH - 2 * PAD_X, HEIGHT - 2 * PAD_Y

    def xy(i: int, v: float) -> tuple[float, float]:
        x = PAD_X + (inner_w * i / (total - 1) if total > 1 else inner_w / 2)
        y = PAD_Y + inner_h * (hi - v) / (hi - lo)
        return round(x, 1), round(y, 1)

    segments: list[str] = []
    run: list[tuple[float, float]] = []
    dots: list[tuple[float, float]] = []
    for i, p in enumerate(points):
        if p.value is None:
            if len(run) >= 2:
                segments.append(" ".join(f"{x},{y}" for x, y in run))
            run = []
            continue
        pt = xy(i, p.value)
        run.append(pt)
        dots.append(pt)
    if len(run) >= 2:
        segments.append(" ".join(f"{x},{y}" for x, y in run))
    rows = tuple(Row(_dmy(p.day), fmt(p.value, unit) if p.value is not None else "not recorded") for p in points)
    x_labels = ((float(PAD_X), _dmy(points[0].day)), (float(WIDTH - PAD_X), _dmy(points[-1].day))) if points else ()
    y_labels = ((float(PAD_Y), fmt(hi, unit)), (float(HEIGHT - PAD_Y), fmt(lo, unit))) if values else ()
    return LineChart(title, tuple(segments), tuple(dots), y_labels, x_labels, rows, len(values), total)


def bar_chart(title: str, items: list[tuple[str, float, str, str]]) -> BarChart:
    """Vertical bars from (label, value, display, css class) items; heights scale to the largest value."""
    top = max((v for _, v, _, _ in items), default=0.0) or 1.0
    inner_w, inner_h = WIDTH - 2 * PAD_X, HEIGHT - 2 * PAD_Y - 16
    slot = inner_w / max(len(items), 1)
    bars = []
    for i, (label, value, display, css) in enumerate(items):
        h = round(inner_h * value / top, 1)
        bars.append(Bar(label, value, display, round(PAD_X + i * slot + slot * 0.15, 1),
                        round(PAD_Y + inner_h - h, 1), round(slot * 0.7, 1), h, css))
    return BarChart(title, tuple(bars), tuple(Row(label, display) for label, _, display, _ in items))


# ---------------------------------------------------------------- tiles and comparison
@dataclass(frozen=True)
class Tile:
    metric: str
    label: str
    headline: str  # mean, total or rate
    headline_kind: str  # "Average", "Total", "Rate"
    detail: str  # secondary statistic, may be ""
    coverage: str  # "N of M days recorded"
    latest: str  # "Latest: 7.5 h on 16.10.2026" or "No records yet"


@dataclass(frozen=True)
class Comparison:
    metric: str
    label: str
    current: str
    previous: str
    difference: str  # signed difference, or "" when not computed
    verdict: str  # "higher", "lower", "no clear change", "not enough data"
    current_value: float | None
    previous_value: float | None


def coverage_text(documented: int, total: int) -> str:
    return f"{documented} of {total} days recorded"


def _main_value(metric: metrics.MetricDef, s: MetricSummary) -> float | None:
    if metric.kind == "daily_value":
        return s.mean
    if metric.kind == "ratio":
        return s.ratio_pct
    return s.rate_per_documented_day


def _rate_unit(metric: metrics.MetricDef) -> str:
    return f"{metric.unit}/day"


def tile(metric_id: str, s: MetricSummary, points: list[DailyPoint]) -> Tile:
    """Tile text of one metric's summary (`points` are the same period's daily points)."""
    m = REGISTRY[metric_id]
    cov = coverage_text(s.coverage.documented_days, s.coverage.total_days)
    if m.kind == "daily_value":
        headline, kind = fmt(s.mean, m.unit), "Average"
        detail = f"Range {fmt(s.min, m.unit)} – {fmt(s.max, m.unit)}" if s.min is not None else ""
        if s.change is not None:
            detail += f" · change {fmt_signed(s.change, m.unit)}"
        latest = s.latest_overall
        latest_text = f"Latest: {fmt(latest.value, m.unit)} on {_dmy(latest.day)}" if latest else "No records yet"
        return Tile(metric_id, m.label, headline, kind, detail, cov, latest_text)
    if m.kind == "ratio":
        headline, kind = fmt(s.ratio_pct, "%"), "Rate"
        detail = f"{s.ratio_numerator} of {s.ratio_denominator}" if s.ratio_denominator else "No rows in this period"
    else:
        total = s.sum if s.sum is not None else 0.0
        headline, kind = fmt(total, m.unit, decimals=0), "Total"
        detail = f"{fmt(s.rate_per_documented_day, _rate_unit(m), decimals=1)} on recorded days" \
            if s.rate_per_documented_day is not None else ""
    last = next((p for p in reversed(points) if p.documented and p.value is not None), None)
    if last is not None:
        unit = "%" if m.kind == "ratio" else m.unit
        latest_text = f"Latest: {fmt(last.value, unit, decimals=0)} on {_dmy(last.day)}"
    elif s.coverage.latest_record is not None:
        latest_text = f"Last recorded on {_dmy(s.coverage.latest_record)}"
    else:
        latest_text = "No records yet"
    return Tile(metric_id, m.label, headline, kind, detail, cov, latest_text)


def compare(metric_id: str, current: MetricSummary, previous: MetricSummary) -> Comparison:
    """Selected vs previous period on the metric's main value (mean, rate per recorded day, or %)."""
    m = REGISTRY[metric_id]
    unit = "%" if m.kind == "ratio" else m.unit if m.kind == "daily_value" else _rate_unit(m)
    decimals = None if m.kind != "event_count" else 1
    cur, prev = _main_value(m, current), _main_value(m, previous)
    if (current.coverage.documented_days < MIN_COMPARE_DAYS or previous.coverage.documented_days < MIN_COMPARE_DAYS
            or cur is None or prev is None):
        verdict, diff = "not enough data", ""
    else:
        delta = cur - prev
        diff = fmt_signed(delta, unit, decimals)
        verdict = "no clear change" if abs(delta) < m.min_delta else "higher" if delta > 0 else "lower"
    return Comparison(metric_id, m.label, fmt(cur, unit, decimals=decimals), fmt(prev, unit, decimals=decimals),
                      diff, verdict, cur, prev)


def compare_chart(items: list[Comparison]) -> CompareChart:
    bars = []
    for c in items:
        top = max(abs(c.current_value or 0.0), abs(c.previous_value or 0.0)) or 1.0
        bars.append(CompareBar(c.label, c.previous, c.current,
                               round(100 * abs(c.previous_value or 0.0) / top, 1),
                               round(100 * abs(c.current_value or 0.0) / top, 1), c.verdict))
    rows = tuple(Row(c.label, f"previous {c.previous} · selected {c.current} · {c.difference or c.verdict}"
                     + (f" ({c.verdict})" if c.difference else "")) for c in items)
    return CompareChart("Selected period vs previous period", tuple(bars), rows)


# ---------------------------------------------------------------- per-tab content
# Tiles per tab, in display order; the first ones are also compared with the previous period.
TAB_TILES: Mapping[str, tuple[str, ...]] = {
    "health": ("sleep.hours", "steps.count", "weight.kg", "calories.daily_total", "meals.count", "workouts.count"),
    "content": ("articles.count", "podcasts.count", "movies.count", "series.count"),
    "jobs": ("job_applications.count", "job_applications.progress_rate"),
    "tasks": ("tasks.count", "tasks.completion_rate"),
}
TAB_COMPARE: Mapping[str, tuple[str, ...]] = {
    "health": ("sleep.hours", "steps.count", "weight.kg", "calories.daily_total", "workouts.count"),
    "content": ("articles.count", "podcasts.count", "movies.count", "series.count"),
    "jobs": ("job_applications.count", "job_applications.progress_rate"),
    "tasks": ("tasks.count", "tasks.completion_rate"),
}
_LINES = {"sleep.hours": False, "steps.count": True, "weight.kg": False, "calories.daily_total": True}
TABS = tuple(TAB_TILES)


@dataclass
class Aggregates:
    """Everything the Aggregates view of one tab shows."""

    tiles: list[Tile] = field(default_factory=list)
    comparisons: list[Comparison] = field(default_factory=list)
    charts: list[LineChart | BarChart | CompareChart] = field(default_factory=list)


def _label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def build(conn: sqlite3.Connection, user_id: int, tab: str, sel: Selection) -> Aggregates:
    """The Aggregates view model of `tab` for `user_id` over `sel` (MetricError is not expected:
    every request here uses registry ids and a validated period)."""
    out = Aggregates()
    summaries: dict[str, MetricSummary] = {}
    for metric_id in TAB_TILES[tab]:
        s = metrics.summarize(conn, user_id, metric_id, sel.period)
        points = metrics.daily_points(conn, user_id, metric_id, sel.period)
        summaries[metric_id] = s
        out.tiles.append(tile(metric_id, s, points))
        if metric_id in _LINES:
            out.charts.append(line_chart(f"{REGISTRY[metric_id].label} per day", points, REGISTRY[metric_id].unit,
                                         from_zero=_LINES[metric_id]))
    for metric_id in TAB_COMPARE[tab]:
        prev = metrics.summarize(conn, user_id, metric_id, sel.previous)
        out.comparisons.append(compare(metric_id, summaries[metric_id], prev))
    out.charts.extend(_bars(conn, user_id, tab, sel, summaries))
    out.charts.append(compare_chart(out.comparisons))
    return out


def _bars(conn: sqlite3.Connection, user_id: int, tab: str, sel: Selection,
          summaries: Mapping[str, MetricSummary]) -> list[BarChart]:
    def counts(metric_id: str, group_by: metrics.GroupBy, css_prefix: str = "") -> BarChart:
        s = metrics.summarize(conn, user_id, metric_id, sel.period, group_by=group_by)
        unit = REGISTRY[metric_id].unit
        items = [(g.key if group_by == "week" else _label(g.key), float(g.count or 0),
                  fmt(float(g.count or 0), unit, decimals=0), f"{css_prefix}{g.key}" if css_prefix else "")
                 for g in s.groups]
        by = "ISO week" if group_by == "week" else _label(REGISTRY[metric_id].subtype_field or "").lower()
        return bar_chart(f"{REGISTRY[metric_id].label} by {by}", items)

    if tab == "health":
        return [counts("meals.count", "subtype"), counts("workouts.count", "week")]
    if tab == "content":
        items = [(REGISTRY[m].label, s.sum or 0.0, fmt(s.sum or 0.0, REGISTRY[m].unit, decimals=0), "")
                 for m, s in summaries.items()]
        return [bar_chart("Items in the selected period", items)]
    if tab == "jobs":
        return [counts("job_applications.count", "subtype")]
    return [counts("tasks.count", "subtype", "cat-")]
