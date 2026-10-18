"""Answer rendering: placeholders → backend-formatted values, caveats, fallback (ADR-005 §4.1, §4.5, §5).

Every number, date, period and coverage value shown to the user is formatted here from the evidence
ledger with fixed formats (worker notes §3). Only validated text is rendered; `untrusted_text` is
never read. Output is plain strings in frozen dataclasses — HTML escaping is the template's job.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Final

from app.analytics import policy
from app.analytics.ledger import (
    PLACEHOLDER_RE,
    T_COMPARE,
    T_COVERAGE,
    T_RECORDS,
    T_RELATE,
    T_SERIES,
    T_SUMMARY,
    EvidenceItem,
    Ledger,
    ResolvedLeaf,
    get_map,
    get_num,
    get_str,
    is_iso_date,
    item_metric,
    parse_record_ref,
)
from app.analytics.ports import MetricDefLike
from app.analytics.schemas import Finding
from app.analytics.validator import Decision, ValidationReport, coverage_below, is_event_split

if TYPE_CHECKING:
    from app.analytics.charts import ChartView

STATUS_LABELS: Final[Mapping[str, str]] = {
    "answered": "Answered", "partial": "Partial answer", "insufficient": "Not enough data",
}
AI_LABEL: Final = "AI-generated analysis · every number computed from your records"
ASSOCIATION_CAVEAT: Final = "Association only; this does not show cause."
LOW_COVERAGE_LABEL: Final = "Low coverage"
# Owner's template, verbatim (ADR-005 §4.5).
FALLBACK_TEMPLATE: Final = (
    "The available records do not provide sufficient reliable information to analyze {topic} for the "
    "selected period. You can add relevant records or expand the date range and try again."
)
MAX_TILES: Final = 4

# Plural domain names (fallback, evidence lines) and singular chip labels.
DOMAIN_NAMES: Final[Mapping[str, str]] = {
    "weight": "Weight", "sleep": "Sleep", "steps": "Steps", "meals": "Meals", "workouts": "Workouts",
    "articles": "Articles", "podcasts": "Podcasts", "movies": "Movies", "series": "Series",
    "job_applications": "Job applications", "tasks": "Tasks",
}
CHIP_LABELS: Final[Mapping[str, str]] = {
    "weight": "Weight", "sleep": "Sleep", "steps": "Steps", "meals": "Meal", "workouts": "Workout",
    "articles": "Article", "podcasts": "Podcast", "movies": "Movie", "series": "Series",
    "job_applications": "Job application", "tasks": "Task",
}
# The enum field shown on a record chip, per domain (never free text).
CHIP_FIELDS: Final[Mapping[str, str]] = {
    "meals": "meal_type", "workouts": "workout_type", "tasks": "category", "job_applications": "status",
}
_ENUM_VALUE_RE: Final = re.compile(r"^[a-z][a-z_]{0,39}$")
_TOOL_WORDS: Final[Mapping[str, str]] = {
    T_COVERAGE: "Data coverage", T_SUMMARY: "Summary", T_COMPARE: "Period comparison",
    T_SERIES: "Daily series", T_RELATE: "Relationship", T_RECORDS: "Record list",
}
_LEAF_DETAIL: Final[Mapping[str, str]] = {
    "mean": "average", "median": "median", "min": "lowest", "max": "highest", "sum": "total",
    "change": "change", "mean_abs": "difference in average", "mean_rel_pct": "relative difference",
    "rate_abs": "difference per documented day", "pct_points": "difference", "count_abs": "difference in count",
    "slope_per_week": "trend per week", "change_over_period": "trend over the period", "pearson_r": "correlation",
    "n_observations": "observations", "documented_days": "documented days", "total_days": "days",
    "rate_per_documented_day": "per documented day", "pct": "share", "x_mean": "average", "y_mean": "average",
    "difference": "difference", "pairs": "paired days", "n_days": "days", "count": "count", "n": "count",
    "numerator": "count", "denominator": "total", "total_matching": "matching records", "returned": "records",
    "n_points": "documented points", "span_days": "days spanned",
}
_TILE_KINDS: Final = frozenset({"value", "number", "delta", "slope", "rel_pct", "pct_points", "rate_delta",
                                "count", "count_delta", "rate", "pct", "r"})


@dataclass(frozen=True)
class TileView:
    """A key-number tile: metric label, formatted value, short description of the value."""

    label: str
    value: str
    detail: str


@dataclass(frozen=True)
class FindingView:
    """A rendered finding with backend-attached coverage lines and caveats."""

    id: str
    kind: str
    text: str
    coverage_lines: tuple[str, ...]
    low_coverage: bool
    caveat: str | None
    record_chips: tuple[str, ...]


@dataclass(frozen=True)
class FallbackView:
    """The verbatim insufficient-evidence message plus what the evidence shows is missing."""

    message: str
    missing: tuple[str, ...]


@dataclass(frozen=True)
class AnswerView:
    """Everything the answer card shows (plain strings; the template escapes them)."""

    question: str
    status: str
    status_label: str
    ai_label: str
    topic: str
    summary: str | None
    tiles: tuple[TileView, ...]
    findings: tuple[FindingView, ...]
    charts: tuple[ChartView, ...]
    limitations: tuple[str, ...]
    evidence: tuple[str, ...]
    follow_ups: tuple[str, ...]
    fallback: FallbackView | None
    tokens_used: int | None


# --- Formats ---

def format_date(iso: str) -> str:
    """`2026-10-05` → `05.10.2026`."""
    return date.fromisoformat(iso).strftime("%d.%m.%Y")


def format_short_date(iso: str) -> str:
    """`2026-10-05` → `05.10`."""
    return date.fromisoformat(iso).strftime("%d.%m")


def format_period(period: Mapping[str, Any], *, short: bool = False) -> str:
    """`DD.MM.YYYY–DD.MM.YYYY` (or `DD.MM–DD.MM`); empty when the period is malformed."""
    start, end = period.get("start"), period.get("end")
    if not (is_iso_date(start) and is_iso_date(end)):
        return ""
    fmt = format_short_date if short else format_date
    return f"{fmt(str(start))}–{fmt(str(end))}"


def _days(n: int, noun: str = "documented day") -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def format_coverage(cov: Mapping[str, Any]) -> str:
    """`N documented days of M`."""
    return f"{_days(int(get_num(cov, 'documented_days') or 0))} of {int(get_num(cov, 'total_days') or 0)}"


def _plain(value: float) -> str:
    return str(int(round(value))) if float(value).is_integer() else f"{value:.1f}"


def format_value(value: float, unit: str) -> str:
    """A metric value in its unit (h/kg 1 dp, steps with thousands separator, kcal estimated, min int)."""
    if unit == "h":
        return f"{value:.1f} h"
    if unit == "kg":
        return f"{value:.1f} kg"
    if unit == "steps":
        return f"{round(value):,} steps"
    if unit == "kcal":
        return f"{round(value):,} kcal (est.)"
    if unit == "min":
        return f"{round(value)} min"
    if unit == "%":
        return f"{value:.0f}%"
    return f"{_plain(value)} {unit}".rstrip()


MINUS: Final = "\u2212"


def _signed(number: float, text: str) -> str:
    """Prefix a formatted magnitude with the true sign of `number` (U+2212 for minus, none for zero)."""
    if number > 0:
        return f"+{text}"
    if number < 0:
        return f"{MINUS}{text}"
    return text


def format_leaf(leaf: ResolvedLeaf) -> str:
    """Format a resolved placeholder leaf.

    Sign-bearing leaves (deltas, slopes, relative/pp differences, count differences) always show their
    sign, so the rendered number states the true direction whatever the surrounding words (D-13).
    """
    kind, value, unit = leaf.leaf_kind, leaf.value, leaf.unit
    if kind == "date":
        return format_date(str(value))
    if kind == "period":
        return format_period(value)
    if kind == "coverage":
        return format_coverage(value)
    number = float(value)
    if kind == "value":
        return format_value(number, unit)
    if kind == "number":
        return _plain(number)
    if kind == "delta":
        return _signed(number, format_value(abs(number), unit))
    if kind == "slope":
        return _signed(number, f"{format_value(abs(number), unit)}/week")
    if kind == "rel_pct":
        return _signed(number, f"{abs(number):.0f}%")
    if kind == "pct_points":
        return _signed(number, f"{abs(number):.0f} pp")
    if kind == "rate_delta":
        return _signed(number, f"{abs(number):.2f}")
    if kind == "count_delta":
        return _signed(number, str(abs(int(round(number)))))
    if kind == "count":
        return str(int(round(number)))
    if kind == "rate":
        return f"{number:.2f}"
    if kind == "pct":
        return f"{number:.0f}%"
    if kind == "coverage_ratio":
        return f"{number * 100:.0f}%"
    if kind == "r":
        return f"{number:+.2f}"
    raise ValueError(f"unknown leaf kind: {kind}")


def render_text(text: str, ledger: Ledger, registry: Mapping[str, MetricDefLike]) -> str:
    """Replace every placeholder in validated text with its formatted value.

    Raises:
        ValueError: a placeholder does not resolve (the text was not validated).
    """
    def substitute(match: re.Match[str]) -> str:
        leaf = ledger.resolve(match.group(1), registry)
        if leaf is None:
            raise ValueError("unresolvable placeholder in rendered text")
        return format_leaf(leaf)
    return PLACEHOLDER_RE.sub(substitute, text)


# --- Labels ---

def metric_label(metric_id: str | None, registry: Mapping[str, MetricDefLike]) -> str:
    """Registry label for a metric id, or the domain name, or a neutral word."""
    if metric_id is None:
        return "Metric"
    if metric_id in registry:
        return registry[metric_id].label
    return DOMAIN_NAMES.get(metric_id.split(".")[0], "Metric")


def _metric_with_unit(metric_id: str | None, registry: Mapping[str, MetricDefLike]) -> str:
    unit = registry[metric_id].unit if metric_id is not None and metric_id in registry else ""
    label = metric_label(metric_id, registry)
    return f"{label} ({unit})" if unit else label


def _enum_word(value: Any) -> str | None:
    if isinstance(value, str) and _ENUM_VALUE_RE.fullmatch(value):
        return value.replace("_", " ")
    return None


# --- Coverage lines ---

def _coverage_line(cov: Mapping[str, Any], period: Mapping[str, Any]) -> str:
    line = f"Based on {format_coverage(cov)}"
    span = format_period(period, short=True)
    return f"{line} · {span}" if span else line


def coverage_lines(item: EvidenceItem, registry: Mapping[str, MetricDefLike]) -> tuple[list[str], bool]:
    """Coverage lines for one cited item and whether any coverage is below `policy.LOW_COVERAGE`."""
    r = item.result
    pairs: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
    if item.tool in (T_SUMMARY, T_SERIES):
        pairs.append(("", get_map(r, "coverage"), get_map(r, "period")))
    elif item.tool == T_COMPARE:
        for label in ("a", "b"):
            part = get_map(r, label)
            pairs.append((f"{label.upper()}: ", get_map(part, "coverage"), get_map(part, "period")))
    elif item.tool == T_RELATE:
        period = get_map(r, "period")
        pairs.append((f"{metric_label(get_str(r, 'x_metric'), registry)}: ", get_map(r, "x_coverage"), period))
        pairs.append((f"{metric_label(get_str(r, 'y_metric'), registry)}: ", get_map(r, "y_coverage"), period))
    elif item.tool == T_RECORDS:
        returned, total = int(get_num(r, "returned") or 0), int(get_num(r, "total_matching") or 0)
        line = f"Based on {returned} of {total} matching records"
        span = format_period(get_map(r, "period"), short=True)
        return [f"{line} · {span}" if span else line], False
    lines = [prefix + _coverage_line(cov, period) for prefix, cov, period in pairs]
    low = any(coverage_below(cov, policy.LOW_COVERAGE) for _, cov, _ in pairs)
    return lines, low


def _finding_items(finding: Finding, ledger: Ledger) -> list[EvidenceItem]:
    items = [ledger.get(e) for e in dict.fromkeys(finding.evidence_ids)]
    known = [i for i in items if i is not None]
    non_t1 = [i for i in known if i.tool != T_COVERAGE]
    return non_t1 or known


def record_chip(record: Mapping[str, Any], domain: str) -> str:
    """`Meal · lunch · 05.10.2026` from the domain label, one enum field and the date (never free text)."""
    parts = [CHIP_LABELS.get(domain, "Record")]
    field_name = CHIP_FIELDS.get(domain)
    if field_name is not None:
        word = _enum_word(get_map(record, "fields").get(field_name))
        if word:
            parts.append(word)
    raw_date = record.get("date")
    if is_iso_date(raw_date):
        parts.append(format_date(str(raw_date)))
    return " · ".join(parts)


def render_finding(finding: Finding, number: int, ledger: Ledger, registry: Mapping[str, MetricDefLike]) -> FindingView:
    """Render one validated finding as `F<number>`."""
    lines: list[str] = []
    low = False
    for item in _finding_items(finding, ledger):
        item_lines, item_low = coverage_lines(item, registry)
        low |= item_low
        lines.extend(line for line in item_lines if line not in lines)
    chips: list[str] = []
    for ref in finding.record_refs:
        parsed = parse_record_ref(ref)
        record = ledger.record(ref)
        source = ledger.get(parsed[0]) if parsed else None
        if record is not None and source is not None:
            chip = record_chip(record, get_str(source.result, "domain") or "")
            if chip not in chips:
                chips.append(chip)
    return FindingView(
        id=f"F{number}", kind=finding.kind, text=render_text(finding.text, ledger, registry),
        coverage_lines=tuple(lines), low_coverage=low,
        caveat=ASSOCIATION_CAVEAT if finding.kind == "relationship" else None, record_chips=tuple(chips),
    )


# --- Tiles ---

def _tile(path: str, ledger: Ledger, registry: Mapping[str, MetricDefLike]) -> TileView | None:
    leaf = ledger.resolve(path, registry)
    if leaf is None or leaf.leaf_kind not in _TILE_KINDS:
        return None
    evidence_id, *segments = path.split(".")
    item = ledger.get(evidence_id)
    if item is None or item.tool == T_COVERAGE:
        return None
    r = item.result
    name = segments[-1]
    detail = _LEAF_DETAIL.get(name, name.replace("_", " "))
    if item.tool == T_RELATE:
        x_label = metric_label(get_str(r, "x_metric"), registry)
        y_label = metric_label(get_str(r, "y_metric"), registry)
        if name == "pearson_r":
            label = f"{x_label} and {y_label}"
        else:
            label = x_label if name == "x_mean" else y_label
            if segments[0] == "with_x":
                detail = f"average with {x_label.lower()} recorded"
            elif segments[0] == "x_not_recorded":
                detail = f"average, no {x_label.lower()} recorded"
    elif item.tool == T_RECORDS:
        label = CHIP_LABELS.get(get_str(r, "domain") or "", "Record")
    else:
        label = metric_label(item_metric(item), registry)
        if item.tool == T_COMPARE and segments[0] in ("a", "b"):
            detail = f"{detail}, period {segments[0].upper()}"
        elif segments[0] in ("first", "last", "min_record", "max_record"):
            detail = {"first": "first", "last": "latest", "min_record": "lowest day",
                      "max_record": "highest day"}[segments[0]]
    return TileView(label=label, value=format_leaf(leaf), detail=detail)


def metric_tiles(texts: Sequence[str], ledger: Ledger, registry: Mapping[str, MetricDefLike]) -> tuple[TileView, ...]:
    """Up to `MAX_TILES` tiles from the numeric placeholders of the shown texts, in order of appearance."""
    tiles: list[TileView] = []
    seen: set[str] = set()
    for text in texts:
        for match in PLACEHOLDER_RE.finditer(text):
            path = match.group(1)
            if path in seen:
                continue
            seen.add(path)
            tile = _tile(path, ledger, registry)
            if tile is not None:
                tiles.append(tile)
            if len(tiles) >= MAX_TILES:
                return tuple(tiles)
    return tuple(tiles)


# --- Evidence disclosure ---

def evidence_line(item: EvidenceItem, registry: Mapping[str, MetricDefLike]) -> str:
    """One ledger item in plain words: tool, metric, period, n and coverage."""
    r = item.result
    parts = [item.id, _TOOL_WORDS.get(item.tool, item.tool)]
    if item.tool == T_COVERAGE:
        for domain, entry in get_map(r, "domains").items():
            if isinstance(entry, Mapping):
                parts.append(f"{DOMAIN_NAMES.get(domain, 'Domain')}: {format_coverage(get_map(entry, 'coverage'))}"
                             f" ({format_period(get_map(entry, 'period'))})")
    elif item.tool == T_SUMMARY:
        parts += [_metric_with_unit(get_str(r, "metric"), registry), format_period(get_map(r, "period")),
                  _days(int(get_num(r, "n_observations") or 0), "observation"),
                  format_coverage(get_map(r, "coverage"))]
    elif item.tool == T_COMPARE:
        parts.append(_metric_with_unit(get_str(r, "metric"), registry))
        for label in ("a", "b"):
            part = get_map(r, label)
            parts.append(f"{label.upper()} {format_period(get_map(part, 'period'))}, "
                         f"{_days(int(get_num(part, 'n_observations') or 0), 'observation')}, "
                         f"{format_coverage(get_map(part, 'coverage'))}")
    elif item.tool == T_SERIES:
        parts += [_metric_with_unit(get_str(r, "metric"), registry), format_period(get_map(r, "period")),
                  format_coverage(get_map(r, "coverage"))]
    elif item.tool == T_RELATE:
        lag = " (next day)" if get_num(r, "lag_days") == 1 else ""
        parts += [f"{metric_label(get_str(r, 'x_metric'), registry)} and "
                  f"{metric_label(get_str(r, 'y_metric'), registry)}{lag}", format_period(get_map(r, "period"))]
        if is_event_split(item):
            parts.append(f"{_days(int(get_num(get_map(r, 'with_x'), 'n_days') or 0), 'day')} with, "
                         f"{_days(int(get_num(get_map(r, 'x_not_recorded'), 'n_days') or 0), 'day')} not recorded")
        else:
            parts.append(_days(int(get_num(r, "pairs") or 0), "paired day"))
    elif item.tool == T_RECORDS:
        parts += [DOMAIN_NAMES.get(get_str(r, "domain") or "", "Records"), format_period(get_map(r, "period")),
                  f"{int(get_num(r, 'returned') or 0)} of {int(get_num(r, 'total_matching') or 0)} matching records"]
    return " · ".join(p for p in parts if p)


# --- Fallback ---

def render_fallback(topic: str, ledger: Ledger) -> FallbackView:
    """The verbatim §4.5 message with `topic`, plus per-domain coverage lines from T1/T2 evidence."""
    missing: list[str] = []

    def add(domain: str, cov: Mapping[str, Any], period: Mapping[str, Any]) -> None:
        span = format_period(period)
        line = f"{DOMAIN_NAMES.get(domain, 'Records')}: {_days(int(get_num(cov, 'documented_days') or 0))}"
        line = f"{line} in {span}" if span else line
        if line not in missing:
            missing.append(line)

    for item in ledger:
        r = item.result
        if item.tool == T_COVERAGE:
            for domain, entry in get_map(r, "domains").items():
                if isinstance(entry, Mapping):
                    add(domain, get_map(entry, "coverage"), get_map(entry, "period"))
        elif item.tool == T_SUMMARY:
            add((get_str(r, "metric") or "").split(".")[0], get_map(r, "coverage"), get_map(r, "period"))
    return FallbackView(message=FALLBACK_TEMPLATE.replace("{topic}", topic), missing=tuple(missing))


# --- Answer ---

def _placeholder_ids(text: str) -> set[str]:
    return {m.group(1).split(".")[0] for m in PLACEHOLDER_RE.finditer(text)}


def render_answer(report: ValidationReport, decision: Decision, ledger: Ledger, registry: Mapping[str, MetricDefLike],
                  question: str, *, tokens_used: int | None = None) -> AnswerView:
    """Build the answer-card view-model for an accept, degrade or fallback decision.

    Only validated parts are rendered: valid findings, limitations, charts and follow-ups. On
    degrade the summary is kept only if it is valid and cites only evidence of kept findings.

    Raises:
        ValueError: `decision.action` is `repair` (nothing to render yet).
    """
    from app.analytics import charts  # charts imports this module's formatters; import here avoids a cycle

    evidence = tuple(evidence_line(item, registry) for item in ledger)
    answer = report.answer
    if decision.action == "repair":
        raise ValueError("a repair decision has no rendered answer")
    if decision.action == "fallback" or answer is None:
        return AnswerView(
            question=question, status="insufficient", status_label=STATUS_LABELS["insufficient"], ai_label=AI_LABEL,
            topic=report.topic, summary=None, tiles=(), findings=(), charts=(), limitations=(), evidence=evidence,
            follow_ups=(), fallback=render_fallback(report.topic, ledger), tokens_used=tokens_used,
        )

    kept = [f for f, ok in zip(answer.findings, report.finding_ok, strict=True) if ok]
    kept_ids = {e for f in kept for e in f.evidence_ids}
    summary: str | None = None
    if report.summary_ok and answer.summary.strip() and _placeholder_ids(answer.summary) <= kept_ids:
        summary = render_text(answer.summary, ledger, registry)
    findings = tuple(render_finding(f, n, ledger, registry) for n, f in enumerate(kept, start=1))
    limitations = [render_text(lim.text, ledger, registry)
                   for lim, ok in zip(answer.limitations, report.limitation_ok, strict=True) if ok]
    if decision.extra_limitation:
        limitations.append(decision.extra_limitation)
    chart_views: list[ChartView] = []
    for chart, ok in zip(answer.charts, report.chart_ok, strict=True):
        item = ledger.get(chart.evidence_id)
        if ok and item is not None:
            view = charts.build_chart(chart.type, item, registry)
            if view is not None:
                chart_views.append(view)
    follow_ups = tuple(" ".join(q.split()) for q, ok in zip(answer.follow_up_questions, report.follow_up_ok,
                                                             strict=True) if ok)
    tile_texts = ([answer.summary] if summary is not None else []) + [f.text for f in kept]
    return AnswerView(
        question=question, status=decision.status, status_label=STATUS_LABELS[decision.status], ai_label=AI_LABEL,
        topic=report.topic, summary=summary, tiles=metric_tiles(tile_texts, ledger, registry), findings=findings,
        charts=tuple(chart_views), limitations=tuple(limitations), evidence=evidence, follow_ups=follow_ups,
        fallback=None, tokens_used=tokens_used,
    )
