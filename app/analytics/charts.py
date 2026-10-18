"""Chart view-models built from ledger data only (ADR-005 §5, V14).

The model chooses only a chart type and an evidence id; every number, label and coordinate here
comes from the evidence item and the metric registry. Geometry is precomputed for an SVG viewBox
of `0 0 320 160` (coordinates rounded to 1 dp) so the Jinja macros only place attributes.

- line ← T4: documented days only; consecutive documented days form a segment and gaps stay
  visible (never drawn as zero). A trend line is added only when the series passes V5.
- bar ← T2 groups.
- comparison ← T3 (period A vs B) or a T5 event split ("with X" vs "no X recorded").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from app.analytics.ledger import (
    T_COMPARE,
    T_RELATE,
    EvidenceItem,
    get_list,
    get_map,
    get_num,
    get_str,
    is_iso_date,
)
from app.analytics.ports import MetricDefLike
from app.analytics.render import format_coverage, format_date, format_period, format_value, metric_label
from app.analytics.validator import chart_compatible, trend_allowed

VIEW_W: Final = 320.0
VIEW_H: Final = 160.0
PAD_LEFT: Final = 44.0
PAD_RIGHT: Final = 8.0
PAD_TOP: Final = 10.0
PAD_BOTTOM: Final = 26.0
PLOT_W: Final = VIEW_W - PAD_LEFT - PAD_RIGHT
PLOT_H: Final = VIEW_H - PAD_TOP - PAD_BOTTOM
VIEW_BOX: Final = f"0 0 {VIEW_W:g} {VIEW_H:g}"

ChartKind = Literal["line", "bar", "comparison"]


@dataclass(frozen=True)
class Tick:
    """An axis label at a position along its axis (SVG user units)."""

    position: float
    label: str


@dataclass(frozen=True)
class Bar:
    """One bar: SVG rectangle plus its value label and an n/coverage note."""

    x: float
    y: float
    width: float
    height: float
    label: str
    value_label: str
    note: str


@dataclass(frozen=True)
class LineGeometry:
    """Polyline segments (`"x,y x,y"`), one dot per documented day, optional trend line (x1, y1, x2, y2)."""

    segments: tuple[str, ...]
    dots: tuple[tuple[float, float], ...]
    trend: tuple[float, float, float, float] | None


@dataclass(frozen=True)
class ChartView:
    """A chart ready for the SVG macros: titles, accessible label, data table and geometry."""

    type: ChartKind
    evidence_id: str
    title: str
    x_label: str
    y_label: str
    aria_label: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    view_box: str
    y_ticks: tuple[Tick, ...]
    x_ticks: tuple[Tick, ...]
    line: LineGeometry | None
    bars: tuple[Bar, ...]


def _r1(value: float) -> float:
    return round(value, 1)


def _unit(metric_id: str | None, registry: Mapping[str, MetricDefLike], fallback: str = "") -> str:
    if metric_id is not None and metric_id in registry:
        return registry[metric_id].unit
    return fallback


def _axis_label(metric_id: str | None, registry: Mapping[str, MetricDefLike], unit: str) -> str:
    label = metric_label(metric_id, registry)
    return f"{label} ({unit})" if unit else label


def _y_of(value: float, lo: float, hi: float) -> float:
    return _r1(PAD_TOP + (1 - (value - lo) / (hi - lo)) * PLOT_H)


def _pct(cov: Mapping[str, Any]) -> str:
    ratio = get_num(cov, "ratio")
    return f"{(ratio or 0.0) * 100:.0f}% coverage"


# --- line ← T4 ---

def _line(item: EvidenceItem, registry: Mapping[str, MetricDefLike]) -> ChartView | None:
    r = item.result
    metric = get_str(r, "metric")
    unit = _unit(metric, registry, get_str(r, "unit") or "")
    points = [p for p in get_list(r, "points") if isinstance(p, Mapping) and is_iso_date(p.get("date"))]
    documented = [(i, get_num(p, "value")) for i, p in enumerate(points) if p.get("documented") is True]
    values = [(i, v) for i, v in documented if v is not None]
    if not values:
        return None
    lo, hi = min(v for _, v in values), max(v for _, v in values)

    trend_ends: tuple[tuple[int, float], tuple[int, float]] | None = None
    slope_week = get_num(get_map(r, "trend"), "slope_per_week")
    if trend_allowed(item) and slope_week is not None and len(values) >= 2:
        # OLS passes through the mean point; x is the day index from the period start.
        x_mean = sum(i for i, _ in values) / len(values)
        y_mean = sum(v for _, v in values) / len(values)
        slope_day = slope_week / 7
        first, last = values[0][0], values[-1][0]
        trend_ends = ((first, y_mean + slope_day * (first - x_mean)), (last, y_mean + slope_day * (last - x_mean)))
        lo = min(lo, trend_ends[0][1], trend_ends[1][1])
        hi = max(hi, trend_ends[0][1], trend_ends[1][1])
    if hi - lo < 1e-9:
        pad = max(abs(hi) * 0.05, 1.0)
        lo, hi = lo - pad, hi + pad

    count = len(points)

    def x_of(i: int) -> float:
        return _r1(PAD_LEFT + (i / (count - 1) * PLOT_W if count > 1 else PLOT_W / 2))

    segments: list[str] = []
    current: list[str] = []
    dots: list[tuple[float, float]] = []
    for i, p in enumerate(points):
        value = get_num(p, "value")
        if p.get("documented") is True and value is not None:
            xy = (x_of(i), _y_of(value, lo, hi))
            dots.append(xy)
            current.append(f"{xy[0]:g},{xy[1]:g}")
            continue
        if len(current) >= 2:
            segments.append(" ".join(current))
        current = []
    if len(current) >= 2:
        segments.append(" ".join(current))
    trend = None
    if trend_ends is not None:
        (i1, v1), (i2, v2) = trend_ends
        trend = (x_of(i1), _y_of(v1, lo, hi), x_of(i2), _y_of(v2, lo, hi))

    rows = tuple((format_date(str(p["date"])),
                  format_value(v, unit) if p.get("documented") is True and (v := get_num(p, "value")) is not None
                  else "No record entered") for p in points)
    label = metric_label(metric, registry)
    period = format_period(get_map(r, "period"))
    aria = (f"Line chart of {label} per day, {period}: {format_coverage(get_map(r, 'coverage'))}. "
            "Days without a record are gaps, not zero.")
    if trend is not None:
        aria += " A trend line is shown."
    return ChartView(
        type="line", evidence_id=item.id, title=f"{label} per day", x_label="Date",
        y_label=_axis_label(metric, registry, unit), aria_label=aria, columns=("Date", label), rows=rows,
        view_box=VIEW_BOX,
        y_ticks=(Tick(_y_of(hi, lo, hi), format_value(hi, unit)), Tick(_y_of(lo, lo, hi), format_value(lo, unit))),
        x_ticks=(Tick(x_of(0), format_date(str(points[0]["date"]))),
                 Tick(x_of(count - 1), format_date(str(points[-1]["date"])))),
        line=LineGeometry(segments=tuple(segments), dots=tuple(dots), trend=trend), bars=(),
    )


# --- bars (shared by bar and comparison) ---

def _bars(entries: Sequence[tuple[str, float, str, str]]) -> tuple[tuple[Bar, ...], tuple[Tick, ...]]:
    """Bars from zero for (label, value, value_label, note) entries; values below zero are clamped."""
    top = max((v for _, v, _, _ in entries), default=0.0)
    top = top if top > 0 else 1.0
    slot = PLOT_W / len(entries)
    bars = []
    for k, (label, value, value_label, note) in enumerate(entries):
        height = max(value, 0.0) / top * PLOT_H
        bars.append(Bar(x=_r1(PAD_LEFT + k * slot + slot * 0.15), y=_r1(PAD_TOP + PLOT_H - height),
                        width=_r1(slot * 0.7), height=_r1(height), label=label, value_label=value_label, note=note))
    top_label = max(entries, key=lambda e: e[1])[2]
    return tuple(bars), (Tick(_r1(PAD_TOP), top_label), Tick(_r1(PAD_TOP + PLOT_H), "0"))


def _summary_value(summary: Mapping[str, Any], kind: str, unit: str) -> tuple[float | None, str]:
    """Kind-appropriate bar value of a T2-shaped summary or group and its label."""
    if kind == "event_count":
        rate = get_num(summary, "rate_per_documented_day")
        return rate, (f"{rate:.2f} per documented day" if rate is not None else "")
    if kind == "ratio":
        pct = get_num(summary, "pct")
        if pct is None:
            pct = get_num(get_map(summary, "ratio"), "pct")
        return pct, (f"{pct:.0f}%" if pct is not None else "")
    mean = get_num(summary, "mean")
    return mean, (format_value(mean, unit) if mean is not None else "")


def _kind(metric_id: str | None, result: Mapping[str, Any], registry: Mapping[str, MetricDefLike]) -> str:
    if metric_id is not None and metric_id in registry:
        return registry[metric_id].kind
    return get_str(result, "kind") or ""


def _value_axis(kind: str, label: str, unit: str) -> str:
    if kind == "event_count":
        return f"{label} per documented day"
    if kind == "ratio":
        return f"{label} (%)"
    return f"{label} ({unit})" if unit else label


def _bar(item: EvidenceItem, registry: Mapping[str, MetricDefLike]) -> ChartView | None:
    r = item.result
    metric = get_str(r, "metric")
    unit = _unit(metric, registry, get_str(r, "unit") or "")
    kind = _kind(metric, r, registry)
    label = metric_label(metric, registry)
    entries: list[tuple[str, float, str, str]] = []
    rows: list[tuple[str, ...]] = []
    for group in get_list(r, "groups"):
        key = get_str(group, "key")
        value, value_label = _summary_value(group, kind, unit)
        if key is None or value is None:
            continue
        name = key.replace("_", " ")
        n, days = int(get_num(group, "n") or 0), int(get_num(group, "documented_days") or 0)
        note = f"n = {n} · {days} documented day{'' if days == 1 else 's'}"
        entries.append((name, value, value_label, note))
        rows.append((name, value_label, str(n), str(days)))
    if not entries:
        return None
    group_by = get_str(r, "group_by") or "subtype"
    if group_by == "subtype":
        field = registry[metric].subtype_field if metric is not None and metric in registry else None
        group_word = field.replace("_", " ") if field else "type"
    else:
        group_word = group_by
    bars, ticks = _bars(entries)
    period = format_period(get_map(r, "period"))
    aria = f"Bar chart of {label} by {group_word}, {period}: " + "; ".join(f"{n} {v}" for n, _, v, _ in entries)
    return ChartView(
        type="bar", evidence_id=item.id, title=f"{label} by {group_word}", x_label=group_word.capitalize(),
        y_label=_value_axis(kind, label, unit), aria_label=aria,
        columns=(group_word.capitalize(), _value_axis(kind, label, unit), "Observations", "Documented days"),
        rows=tuple(rows), view_box=VIEW_BOX, y_ticks=ticks, x_ticks=(), line=None, bars=bars,
    )


def _comparison_periods(item: EvidenceItem, registry: Mapping[str, MetricDefLike]) -> ChartView | None:
    r = item.result
    metric = get_str(r, "metric")
    unit = _unit(metric, registry, get_str(r, "unit") or "")
    kind = _kind(metric, r, registry)
    label = metric_label(metric, registry)
    entries: list[tuple[str, float, str, str]] = []
    rows: list[tuple[str, ...]] = []
    for part_key in ("a", "b"):
        part = get_map(r, part_key)
        value, value_label = _summary_value(part, kind, unit)
        if value is None:
            return None
        cov = get_map(part, "coverage")
        n = int(get_num(part, "n_observations") or 0)
        name = f"Period {part_key.upper()}"
        period = format_period(get_map(part, "period"))
        entries.append((name, value, value_label, f"n = {n} · {_pct(cov)}"))
        rows.append((name, period, value_label, str(n), format_coverage(cov)))
    bars, ticks = _bars(entries)
    aria = (f"Comparison of {label}: " +
            "; ".join(f"{name} {value_label}, {note}" for name, _, value_label, note in entries))
    return ChartView(
        type="comparison", evidence_id=item.id, title=f"{label}: period A vs period B", x_label="Period",
        y_label=_value_axis(kind, label, unit), aria_label=aria,
        columns=("Period", "Dates", _value_axis(kind, label, unit), "Observations", "Coverage"),
        rows=tuple(rows), view_box=VIEW_BOX, y_ticks=ticks, x_ticks=(), line=None, bars=bars,
    )


def _comparison_split(item: EvidenceItem, registry: Mapping[str, MetricDefLike]) -> ChartView | None:
    r = item.result
    x_metric, y_metric = get_str(r, "x_metric"), get_str(r, "y_metric")
    x_word = metric_label(x_metric, registry).lower()
    y_label = metric_label(y_metric, registry)
    unit = _unit(y_metric, registry)
    entries: list[tuple[str, float, str, str]] = []
    rows: list[tuple[str, ...]] = []
    for key, name in (("with_x", f"With {x_word} recorded"), ("x_not_recorded", f"No {x_word} recorded")):
        group = get_map(r, key)
        mean = get_num(group, "y_mean")
        if mean is None:
            return None
        days = int(get_num(group, "n_days") or 0)
        value_label = format_value(mean, unit)
        entries.append((name, mean, value_label, f"{days} day{'' if days == 1 else 's'}"))
        rows.append((name, value_label, str(days)))
    bars, ticks = _bars(entries)
    lag = " on the next day" if get_num(r, "lag_days") == 1 else ""
    aria = (f"Comparison of average {y_label}{lag} on days with {x_word} recorded and days with no {x_word} "
            f"recorded: " + "; ".join(f"{name} {value_label}, {note}" for name, _, value_label, note in entries))
    return ChartView(
        type="comparison", evidence_id=item.id, title=f"{y_label}{lag}: days with {x_word} vs no {x_word} recorded",
        x_label="Days", y_label=_axis_label(y_metric, registry, unit), aria_label=aria,
        columns=("Days", f"Average {y_label}", "Days"), rows=tuple(rows), view_box=VIEW_BOX, y_ticks=ticks,
        x_ticks=(), line=None, bars=bars,
    )


def build_chart(chart_type: str, item: EvidenceItem, registry: Mapping[str, MetricDefLike]) -> ChartView | None:
    """The chart view-model for a (type, evidence) pair, or None when incompatible or nothing to draw (V14)."""
    if not chart_compatible(chart_type, item):
        return None
    if chart_type == "line":
        return _line(item, registry)
    if chart_type == "bar":
        return _bar(item, registry)
    if item.tool == T_COMPARE:
        return _comparison_periods(item, registry)
    if item.tool == T_RELATE:
        return _comparison_split(item, registry)
    return None

