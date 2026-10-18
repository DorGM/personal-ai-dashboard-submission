"""Deterministic Evidence Validation of the model's final answer (ADR-005 §4, worker notes §4).

`validate` applies V1–V14 to the raw final-answer JSON against the evidence ledger and the metric
registry and returns a `ValidationReport`; `decide` turns a report into accept / repair / degrade /
fallback (§4.4). Errors carry only a code, a location, an index and a token class — never the
offending text — so a repair message cannot echo data back to the model.

The lexical rules (V3 number words, V6, V9, V10) are English-only and best-effort (D-10).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Final, Literal

import pydantic

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
    get_list,
    get_map,
    get_num,
    get_str,
    item_metric,
    parse_record_ref,
)
from app.analytics.ports import MetricDefLike
from app.analytics.schemas import Chart, FinalAnswer, Finding, Limitation, Status

ErrorCode = Literal[
    "SCHEMA", "UNKNOWN_EVIDENCE", "UNKNOWN_PLACEHOLDER", "PLACEHOLDER_NOT_CITED", "UNKNOWN_RECORD",
    "FREE_NUMBER", "NO_DATA", "INSUFFICIENT_N", "LOW_COVERAGE_TREND", "ABSOLUTE_LANGUAGE", "ABSENCE_CLAIM",
    "NOT_COMPARABLE", "DERIVED_MISMATCH", "DIRECTION_MISMATCH", "CAUSAL_LANGUAGE", "KIND_TOOL_MISMATCH",
    "METRIC_MISMATCH", "MARKUP_OR_URL", "TOPIC_INVALID", "STATUS_INCONSISTENT", "CHART_INCOMPATIBLE",
]
Location = Literal["summary", "finding", "limitation", "chart", "follow_up", "topic", "status"]
Action = Literal["accept", "repair", "degrade", "fallback"]
FallbackReason = Literal["insufficient", "no_valid_finding", "unparsable"]

FALLBACK_TOPIC: Final = "this question"
DEGRADE_LIMITATION: Final = "Some parts of the analysis could not be verified and were omitted."
# D-1: errors at these locations drop the item and never trigger a repair turn.
_DROPPABLE: Final = frozenset({"chart", "follow_up", "topic"})

# --- Lexical lists (whole word, case-insensitive) ---
NUMBER_WORDS: Final = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
    "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "million", "dozen", "half",
    "twice", "double", "triple", "percent",
    # D-12 (e): inflected forms
    "doubled", "doubles", "doubling", "tripled", "triples", "quadrupled", "halved", "halves", "hundreds",
    "thousands", "millions", "dozens",
)
ABSOLUTE_PHRASES: Final = ("always", "never", "every day", "each day", "all days", "none", "zero")
ABSENCE_PHRASES: Final = ("didn't", "didnt", "did not", "skipped", "missed", "rest day", "rest days",
                          "no workout", "no workouts")
CAUSAL_PHRASES: Final = (
    "because", "cause", "causes", "caused", "due to", "leads to", "led to", "results in", "drives", "makes you",
    "improves", "boosts", "effect of", "impact of", "thanks to", "therefore", "proves", "guarantees", "will",
)
# D-13: stem-based direction lexicon (regex fragments; matched case-insensitively from a word start).
INCREASE_STEMS: Final = (
    r"increas\w*", "rise", "rises", "rising", "rose", "risen", r"climb\w*", r"gain\w*", r"grow\w*", "grew",
    "grown", "longer", "higher", "more", "up", r"upward\w*", r"improv\w*", "better", r"jump\w*", r"surg\w*",
    r"soar\w*", r"rais\w*", r"spik\w*", r"expand\w*", r"boost\w*",
)
DECREASE_STEMS: Final = (
    r"decreas\w*", r"drop\w*", r"fall\w*", "fell", "fallen", r"declin\w*", "lose", "loses", "losing", "lost",
    r"loss\w*", r"shed\w*", r"shrink\w*", "shrank", "shrunk", r"reduc\w*", r"slip\w*", r"worse\w*",
    r"worsen\w*", "shorter", r"lower\w*", "less", "fewer", r"dip\w*", "sank", r"sink\w*", "sunk",
    r"plummet\w*", r"slump\w*", "down", r"downward\w*",
)


def _phrase_re(phrases: Iterable[str]) -> re.Pattern[str]:
    parts = sorted((re.escape(p).replace(r"\ ", r"\s+") for p in phrases), key=len, reverse=True)
    return re.compile(r"(?<![\w'])(?:" + "|".join(parts) + r")(?![\w'])", re.IGNORECASE)


_NUMBER_WORD_RE: Final = _phrase_re(NUMBER_WORDS)
_FOLD_RE: Final = re.compile(r"\b\w+fold\b", re.IGNORECASE)
_ROMAN_RE: Final = re.compile(r"\b[IVXLCDM]{2,}\b")  # case-sensitive: uppercase Roman numerals
_ABSOLUTE_RE: Final = _phrase_re(ABSOLUTE_PHRASES)
_ABSENCE_RE: Final = _phrase_re(ABSENCE_PHRASES)
_CAUSAL_RE: Final = _phrase_re(CAUSAL_PHRASES)
_INCREASE_RE: Final = re.compile(r"(?<![\w'])(?:" + "|".join(INCREASE_STEMS) + r")(?![\w'])", re.IGNORECASE)
_DECREASE_RE: Final = re.compile(r"(?<![\w'])(?:" + "|".join(DECREASE_STEMS) + r")(?![\w'])", re.IGNORECASE)
_URL_RE: Final = re.compile(r"https?|www\.|://|\b(?:javascript|vbscript|mailto):", re.IGNORECASE)
_HTML_RE: Final = re.compile(r"<\s*[A-Za-z!/?]|&#?[A-Za-z0-9]+;")
_MD_LINK_RE: Final = re.compile(r"\[[^\]]*\]\([^)]*\)")
_APOSTROPHES: Final = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "′": "'", "`": "'"})

_KIND_TOOLS: Final[Mapping[str, frozenset[str]]] = {
    "trend": frozenset({T_SERIES}),
    "comparison": frozenset({T_COMPARE}),
    "relationship": frozenset({T_RELATE}),
    "value": frozenset({T_SUMMARY, T_COMPARE, T_SERIES}),
    "records": frozenset({T_RECORDS}),
}
_DIRECTIONAL_KINDS: Final = frozenset({"comparison", "trend", "relationship"})
_WEIGHT_METRIC: Final = "weight.kg"
_WEIGHT_CHANGE_PATHS: Final = frozenset({"first", "last", "change"})
_WEIGHT_META_PATHS: Final = frozenset({"period", "coverage", "n_observations"})


@dataclass(frozen=True)
class ValidationIssue:
    """One rule violation. `token_class` names the kind of offending token, never its text."""

    code: ErrorCode
    location: Location
    index: int | None
    blocking: bool
    token_class: str | None = None


@dataclass(frozen=True)
class ValidationReport:
    """Result of `validate`: the parsed answer (None on V1 failure), issues and per-item validity."""

    answer: FinalAnswer | None
    errors: tuple[ValidationIssue, ...]
    finding_ok: tuple[bool, ...] = ()
    limitation_ok: tuple[bool, ...] = ()
    chart_ok: tuple[bool, ...] = ()
    follow_up_ok: tuple[bool, ...] = ()
    summary_ok: bool = False
    topic: str = FALLBACK_TOPIC

    @property
    def blocking(self) -> bool:
        """True when any issue requires a repair turn (D-1)."""
        return any(e.blocking for e in self.errors)

    @property
    def codes(self) -> tuple[str, ...]:
        """Distinct error codes in first-seen order (for logging)."""
        return tuple(dict.fromkeys(e.code for e in self.errors))


@dataclass(frozen=True)
class Decision:
    """What to do with a validated answer (ADR-005 §4.4).

    `status` is the status to display; `extra_limitation` is the backend limitation added on
    degrade; `fallback_reason` lets the orchestrator map `unparsable` to its §3.7 error message.
    """

    action: Action
    status: Status
    repair_message: str | None = None
    extra_limitation: str | None = None
    fallback_reason: FallbackReason | None = None


# --- Small helpers ---

def normalize_text(text: str) -> str:
    """NFKC with curly apostrophes folded to ASCII, for lexical matching only."""
    return unicodedata.normalize("NFKC", text).translate(_APOSTROPHES)


def strip_placeholders(text: str) -> str:
    """The text with every well-formed placeholder replaced by a space."""
    return PLACEHOLDER_RE.sub(" ", text)


def min_delta(metric_id: str | None, unit: str | None, registry: Mapping[str, MetricDefLike]) -> float:
    """Per-metric `min_delta` from the registry, else the §4.2 default for the unit."""
    if metric_id is not None and metric_id in registry:
        return registry[metric_id].min_delta
    return policy.DEFAULT_MIN_DELTA.get(unit or "", 0.0)


def _metric_kind(metric_id: str | None, summary: Mapping[str, Any], registry: Mapping[str, MetricDefLike]) -> str:
    if metric_id is not None and metric_id in registry:
        return registry[metric_id].kind
    return get_str(summary, "kind") or ""


def _close(actual: float, expected: float) -> bool:
    return math.isclose(actual, expected, rel_tol=policy.DERIVED_TOLERANCE, abs_tol=policy.DERIVED_TOLERANCE)


def coverage_fraction(cov: Mapping[str, Any]) -> Fraction | None:
    """Exact documented/total of a coverage object (None when malformed).

    Policy boundaries (0.4, 0.5, the 0.3 gap) are compared exactly, as the tools do, so a value
    on a boundary can never be judged differently by the tool and by the validator.
    """
    days, total = get_num(cov, "documented_days"), get_num(cov, "total_days")
    if days is None or total is None or days < 0 or total < 0:
        return None
    return Fraction(int(days), int(total)) if total else Fraction(0)


def coverage_below(cov: Mapping[str, Any], threshold: float) -> bool:
    """True when coverage is below `threshold` (or unknown)."""
    fraction = coverage_fraction(cov)
    return fraction is None or fraction < Fraction(str(threshold))


def item_coverages(item: EvidenceItem) -> list[Mapping[str, Any]]:
    """Every coverage object of an item (T1: per domain; T3: both periods; T5: x and y)."""
    r = item.result
    if item.tool == T_COVERAGE:
        return [get_map(v, "coverage") for v in get_map(r, "domains").values() if isinstance(v, Mapping)]
    if item.tool == T_COMPARE:
        return [get_map(get_map(r, "a"), "coverage"), get_map(get_map(r, "b"), "coverage")]
    if item.tool == T_RELATE:
        return [get_map(r, "x_coverage"), get_map(r, "y_coverage")]
    if item.tool in (T_SUMMARY, T_SERIES):
        return [get_map(r, "coverage")]
    return []


def min_coverage(items: Sequence[EvidenceItem]) -> float:
    """D-5: the minimum coverage ratio over items; items without coverage (T6) count as 0."""
    ratios: list[Fraction] = []
    for item in items:
        covs = item_coverages(item)
        if not covs:
            ratios.append(Fraction(0))
        for cov in covs:
            ratios.append(coverage_fraction(cov) or Fraction(0))
    return float(min(ratios)) if ratios else 0.0


def is_event_split(item: EvidenceItem) -> bool:
    """True for a T5 result in event mode (has `with_x` / `x_not_recorded` groups)."""
    r = item.result
    return item.tool == T_RELATE and get_str(r, "mode") == "event" and bool(get_map(r, "x_not_recorded"))


def has_data(item: EvidenceItem) -> bool:
    """V4 per tool (D-6)."""
    r = item.result
    if item.tool == T_COVERAGE:
        return any((get_num(get_map(v, "coverage"), "documented_days") or 0) > 0
                   for v in get_map(r, "domains").values())
    if item.tool == T_SUMMARY:
        return (get_num(r, "n_observations") or 0) > 0
    if item.tool == T_COMPARE:
        return all((get_num(get_map(r, p), "n_observations") or 0) > 0 for p in ("a", "b"))
    if item.tool == T_SERIES:
        return (get_num(get_map(r, "trend"), "n_points") or 0) > 0 or any(
            isinstance(p, Mapping) and p.get("documented") is True for p in get_list(r, "points"))
    if item.tool == T_RELATE:
        if is_event_split(item):
            return all((get_num(get_map(r, g), "n_days") or 0) > 0 for g in ("with_x", "x_not_recorded"))
        return (get_num(r, "pairs") or 0) > 0
    if item.tool == T_RECORDS:
        return (get_num(r, "returned") or 0) > 0
    return False


def _value_n(summary: Mapping[str, Any], kind: str) -> int:
    """V5 n for a T2-shaped summary: documented days for daily values, observations otherwise."""
    if kind == "daily_value":
        return int(get_num(get_map(summary, "coverage"), "documented_days") or 0)
    return int(get_num(summary, "n_observations") or 0)


def trend_issue(item: EvidenceItem) -> tuple[ErrorCode, str] | None:
    """V5/V6 trend rule for a T4 item; None when a trend claim is allowed (D-3 for weight)."""
    r = item.result
    trend = get_map(r, "trend")
    n_points = get_num(trend, "n_points") or 0
    span = get_num(trend, "span_days") or 0
    is_weight = get_str(r, "metric") == _WEIGHT_METRIC
    if get_num(trend, "slope_per_week") is None or get_num(trend, "change_over_period") is None:
        return "INSUFFICIENT_N", "points"
    if n_points < (policy.WEIGHT_TREND_MIN_POINTS if is_weight else policy.TREND_MIN_POINTS):
        return "INSUFFICIENT_N", "points"
    if span < policy.TREND_MIN_SPAN_DAYS:
        return "INSUFFICIENT_N", "span"
    if not is_weight and coverage_below(get_map(r, "coverage"), policy.TREND_MIN_COVERAGE):
        return "LOW_COVERAGE_TREND", "coverage"
    return None


def trend_allowed(item: EvidenceItem) -> bool:
    """True when a T4 item supports a trend claim / trend line (V5)."""
    return item.tool == T_SERIES and trend_issue(item) is None


def comparable(item: EvidenceItem) -> bool:
    """V7 + D-4 for a T3 item: tool flag and recomputed rule, each period coverage >= LOW_COVERAGE."""
    r = item.result
    if r.get("comparable") is not True:
        return False
    for p in ("a", "b"):
        cov = get_map(get_map(r, p), "coverage")
        if (get_num(cov, "documented_days") or 0) < policy.COMPARE_MIN_DAYS:
            return False
        if coverage_below(cov, policy.COMPARE_MIN_COVERAGE) or coverage_below(cov, policy.LOW_COVERAGE):
            return False
    return True


def gap_too_large(item: EvidenceItem) -> bool:
    """V7: |coverage_a - coverage_b| > COUNT_COVERAGE_GAP for a T3 item (or unknown): rates only."""
    ra = coverage_fraction(get_map(get_map(item.result, "a"), "coverage"))
    rb = coverage_fraction(get_map(get_map(item.result, "b"), "coverage"))
    return ra is None or rb is None or abs(ra - rb) > Fraction(str(policy.COUNT_COVERAGE_GAP))


def _primary_delta_field(kind: str) -> str:
    return {"event_count": "rate_abs", "ratio": "pct_points"}.get(kind, "mean_abs")


def expected_direction(item: EvidenceItem, registry: Mapping[str, MetricDefLike]) -> str:
    """The backend direction recomputed from base fields (V9); not_applicable for other tools."""
    r = item.result
    if item.tool == T_COMPARE:
        metric = get_str(r, "metric")
        kind = _metric_kind(metric, r, registry)
        delta = get_num(get_map(r, "difference"), _primary_delta_field(kind))
        return policy.direction(delta, min_delta(metric, get_str(r, "unit"), registry))
    if item.tool == T_SERIES:
        metric = get_str(r, "metric")
        return policy.direction(get_num(get_map(r, "trend"), "change_over_period"),
                                min_delta(metric, get_str(r, "unit"), registry))
    if item.tool == T_RELATE:
        if is_event_split(item):
            y_metric = get_str(r, "y_metric")
            return policy.direction(get_num(r, "difference"), min_delta(y_metric, None, registry))
        return policy.r_direction(get_num(r, "pearson_r"))
    return "not_applicable"


# D-12 (a): sign-bearing "delta leaves" and the only finding kind that may use them.
DELTA_LEAF_KIND: Final[Mapping[str, str]] = {T_COMPARE: "comparison", T_SERIES: "trend", T_RELATE: "relationship"}
_TREND_DELTAS: Final = (["trend", "slope_per_week"], ["trend", "change_over_period"])
# D-12 (c): the only leaf kinds a limitation may render.
LIMITATION_LEAF_KINDS: Final = frozenset({"count", "date", "period", "coverage"})


def is_delta_leaf(item: EvidenceItem, segments: Sequence[str]) -> bool:
    """T3 `difference.*` (and per-period `change`), T4 trend slope/change, T5 `difference` and `pearson_r`."""
    segs = list(segments)
    if item.tool == T_COMPARE:
        return bool(segs) and (segs[0] == "difference" or segs[-1] == "change")
    if item.tool == T_SERIES:
        return segs in _TREND_DELTAS
    if item.tool == T_RELATE:
        return segs in (["difference"], ["pearson_r"])
    return False


def placeholder_direction(item: EvidenceItem, segments: Sequence[str],
                          registry: Mapping[str, MetricDefLike]) -> str | None:
    """Direction carried by a sign-bearing placeholder (delta leaves and T2 `change`), else None (D-12 b)."""
    r = item.result
    segs = list(segments)
    md = min_delta(get_str(r, "metric"), get_str(r, "unit"), registry)
    if item.tool == T_SUMMARY:
        return policy.direction(get_num(r, "change"), md) if segs == ["change"] else None
    if not is_delta_leaf(item, segs):
        return None
    if item.tool == T_COMPARE:
        if segs[0] != "difference":
            return policy.direction(get_num(get_map(r, segs[0]), "change"), md)
        diff = get_map(r, "difference")
        name = segs[1] if len(segs) > 1 else ""
        if name == "count_abs":
            return policy.direction(get_num(diff, name), 1.0)
        if name == "mean_rel_pct":
            name = "mean_abs"
        return policy.direction(get_num(diff, name), md)
    if item.tool == T_RELATE and segs == ["pearson_r"]:
        return policy.r_direction(get_num(r, "pearson_r"))
    return expected_direction(item, registry)


def _check_direction_words(text: str, direction: str, sink: _Sink, token: str | None = None) -> None:
    """Reject words of the opposite direction set; for no_clear_change, both sets (D-8)."""
    norm = normalize_text(strip_placeholders(text))
    if direction in ("decrease", "no_clear_change") and _INCREASE_RE.search(norm):
        sink.add("DIRECTION_MISMATCH", token or "increase_word")
    if direction in ("increase", "no_clear_change") and _DECREASE_RE.search(norm):
        sink.add("DIRECTION_MISMATCH", token or "decrease_word")


def direction_words(text: str) -> tuple[bool, bool]:
    """(has increase words, has decrease words) outside placeholders (D-13 lexicon)."""
    norm = normalize_text(strip_placeholders(text))
    return bool(_INCREASE_RE.search(norm)), bool(_DECREASE_RE.search(norm))


def _check_direction_text(text: str, directions: Iterable[str | None], sink: _Sink, *,
                          unbacked_ok: bool = False) -> tuple[bool, bool, bool]:
    """D-13: direction words must be backed by sign-bearing placeholders of the same direction.

    - words from both sets, or placeholders of different directions → `mixed_direction`;
    - direction words without any sign-bearing placeholder → `unsupported_direction` (unless
      `unbacked_ok`, used by the summary, which applies its own finding-based rule);
    - increase words need every placeholder direction to be increase, decrease words decrease;
      `no_clear_change` placeholders allow no direction words at all.
    Returns (increase words, decrease words, has a sign-bearing placeholder).
    """
    inc, dec = direction_words(text)
    found = [d for d in directions if d is not None]
    distinct = set(found) - {"not_applicable"}
    if (inc and dec) or len(distinct) > 1:
        sink.add("DIRECTION_MISMATCH", "mixed_direction")
    if not (inc or dec):
        return inc, dec, bool(found)
    if not found:
        if not unbacked_ok:
            sink.add("DIRECTION_MISMATCH", "unsupported_direction")
        return inc, dec, False
    for direction in distinct:
        if inc and direction != "increase":
            sink.add("DIRECTION_MISMATCH", "increase_word")
        if dec and direction != "decrease":
            sink.add("DIRECTION_MISMATCH", "decrease_word")
    return inc, dec, True


def chart_compatible(chart_type: str, item: EvidenceItem) -> bool:
    """V14: line ← T4 with a documented point; bar ← T2 with groups; comparison ← T3 or T5 event split."""
    if chart_type == "line":
        return item.tool == T_SERIES and any(
            isinstance(p, Mapping) and p.get("documented") is True and get_num(p, "value") is not None
            for p in get_list(item.result, "points"))
    if chart_type == "bar":
        return item.tool == T_SUMMARY and len(get_list(item.result, "groups")) > 0
    if chart_type == "comparison":
        return item.tool == T_COMPARE or is_event_split(item)
    return False


# --- V8: derived-field recomputation (D-11) ---

def _check_pair(actual: float | None, expected: float | None) -> bool:
    """A non-null derived value must equal its recomputation (nulls are not dangerous: they never render)."""
    if actual is None:
        return True
    return expected is not None and _close(actual, expected)


def _summary_derived_ok(summary: Mapping[str, Any]) -> bool:
    ok = _coverage_derived_ok(get_map(summary, "coverage"))
    n = get_num(summary, "n_observations")
    days = get_num(get_map(summary, "coverage"), "documented_days")
    rate = get_num(summary, "rate_per_documented_day")
    ok &= _check_pair(rate, n / days if n is not None and days else None)
    ratio = get_map(summary, "ratio")
    num, den, pct = get_num(ratio, "numerator"), get_num(ratio, "denominator"), get_num(ratio, "pct")
    ok &= _check_pair(pct, num / den * 100 if num is not None and den else None)
    first, last = get_num(get_map(summary, "first"), "value"), get_num(get_map(summary, "last"), "value")
    ok &= _check_pair(get_num(summary, "change"), last - first if first is not None and last is not None else None)
    return ok


def _coverage_derived_ok(cov: Mapping[str, Any]) -> bool:
    if not cov:
        return True
    days, total = get_num(cov, "documented_days"), get_num(cov, "total_days")
    expected = (days / total if total else 0.0) if days is not None and total is not None else None
    return _check_pair(get_num(cov, "ratio"), expected)


def derived_ok(item: EvidenceItem, registry: Mapping[str, MetricDefLike]) -> bool:
    """V8: recompute derived fields of an item from its base fields (tolerance 1e-9)."""
    r = item.result
    ok = all(_coverage_derived_ok(cov) for cov in item_coverages(item))
    if item.tool == T_SUMMARY:
        ok &= _summary_derived_ok(r)
    elif item.tool == T_COMPARE:
        a, b, diff = get_map(r, "a"), get_map(r, "b"), get_map(r, "difference")
        ok &= _summary_derived_ok(a) and _summary_derived_ok(b)
        a_mean, b_mean = get_num(a, "mean"), get_num(b, "mean")
        mean_abs = b_mean - a_mean if a_mean is not None and b_mean is not None else None
        ok &= _check_pair(get_num(diff, "mean_abs"), mean_abs)
        rel = mean_abs / abs(a_mean) * 100 if mean_abs is not None and a_mean else None
        ok &= _check_pair(get_num(diff, "mean_rel_pct"), rel)
        a_rate, b_rate = get_num(a, "rate_per_documented_day"), get_num(b, "rate_per_documented_day")
        ok &= _check_pair(get_num(diff, "rate_abs"), b_rate - a_rate if a_rate is not None and b_rate is not None
                          else None)
        a_pct, b_pct = get_num(get_map(a, "ratio"), "pct"), get_num(get_map(b, "ratio"), "pct")
        ok &= _check_pair(get_num(diff, "pct_points"), b_pct - a_pct if a_pct is not None and b_pct is not None
                          else None)
        count_abs = get_num(diff, "count_abs")
        if count_abs is not None and gap_too_large(item):
            ok = False
        a_n, b_n = get_num(a, "n_observations"), get_num(b, "n_observations")
        ok &= _check_pair(count_abs, b_n - a_n if a_n is not None and b_n is not None else None)
        ok &= get_str(r, "direction") == expected_direction(item, registry)
    elif item.tool == T_SERIES:
        trend = get_map(r, "trend")
        slope, span = get_num(trend, "slope_per_week"), get_num(trend, "span_days")
        ok &= _check_pair(get_num(trend, "change_over_period"),
                          slope / 7 * span if slope is not None and span is not None else None)
        ok &= get_str(r, "direction") in (None, expected_direction(item, registry))
    elif item.tool == T_RELATE:
        if is_event_split(item):
            with_mean = get_num(get_map(r, "with_x"), "y_mean")
            none_mean = get_num(get_map(r, "x_not_recorded"), "y_mean")
            ok &= _check_pair(get_num(r, "difference"),
                              with_mean - none_mean if with_mean is not None and none_mean is not None else None)
        ok &= get_str(r, "direction") in (None, expected_direction(item, registry))
    return bool(ok)


# --- Text rules ---

@dataclass
class _Sink:
    """Collects issues for one location/index, de-duplicated by (code, token_class)."""

    location: Location
    index: int | None
    issues: list[ValidationIssue] = field(default_factory=list)

    def add(self, code: ErrorCode, token_class: str | None = None) -> None:
        issue = ValidationIssue(code, self.location, self.index, self.location not in _DROPPABLE, token_class)
        if issue not in self.issues:
            self.issues.append(issue)


def _check_free_numbers(text: str, sink: _Sink) -> None:
    """V3 on text with placeholders removed (D-9): any Unicode numeral or English number word."""
    bare = strip_placeholders(text)
    for ch in bare:
        category = unicodedata.category(ch)
        if category == "Nd":
            sink.add("FREE_NUMBER", "digit")
            break
        if category.startswith("N"):
            sink.add("FREE_NUMBER", "numeral")
            break
    norm = normalize_text(bare)
    if _NUMBER_WORD_RE.search(norm) or _FOLD_RE.search(norm):
        sink.add("FREE_NUMBER", "number_word")
    if _ROMAN_RE.search(norm):
        sink.add("FREE_NUMBER", "roman_numeral")


def _check_safe_text(text: str, sink: _Sink) -> None:
    """V12: no URLs, HTML, Markdown links or control/format characters."""
    norm = normalize_text(text)
    if _URL_RE.search(norm):
        sink.add("MARKUP_OR_URL", "url")
    if _HTML_RE.search(norm):
        sink.add("MARKUP_OR_URL", "html")
    if _MD_LINK_RE.search(norm):
        sink.add("MARKUP_OR_URL", "markdown_link")
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in text):
        sink.add("MARKUP_OR_URL", "control_char")


def _check_causal(text: str, sink: _Sink) -> None:
    """V10: causal and predictive language."""
    if _CAUSAL_RE.search(normalize_text(strip_placeholders(text))):
        sink.add("CAUSAL_LANGUAGE", "causal_word")


def _check_absolutes(text: str, coverage: float, absence_allowed: bool, sink: _Sink) -> None:
    """V6: absolute words when coverage < 1; absence claims unless a T5 `x_not_recorded` group is cited."""
    norm = normalize_text(strip_placeholders(text))
    if coverage < policy.FULL_COVERAGE and _ABSOLUTE_RE.search(norm):
        sink.add("ABSOLUTE_LANGUAGE", "absolute_word")
    if not absence_allowed and _ABSENCE_RE.search(norm):
        sink.add("ABSENCE_CLAIM", "absence_word")


def _check_placeholders(text: str, allowed: set[str], ledger: Ledger, registry: Mapping[str, MetricDefLike],
                        sink: _Sink) -> list[tuple[str, list[str]]]:
    """V2 placeholders: well-formed, resolvable, and citing only `allowed` evidence ids.

    Returns the (evidence_id, path segments) of every valid placeholder.
    """
    valid: list[tuple[str, list[str]]] = []
    rest = PLACEHOLDER_RE.sub(" ", text)
    if "{{" in rest or "}}" in rest:
        sink.add("UNKNOWN_PLACEHOLDER", "malformed")
    for match in PLACEHOLDER_RE.finditer(text):
        path = match.group(1)
        evidence_id, *segments = path.split(".")
        if evidence_id not in allowed:
            sink.add("PLACEHOLDER_NOT_CITED" if evidence_id in ledger else "UNKNOWN_PLACEHOLDER", "evidence_id")
        elif ledger.resolve(path, registry) is None:
            sink.add("UNKNOWN_PLACEHOLDER", "path")
        else:
            valid.append((evidence_id, segments))
    return valid


def _known_items(ids: Iterable[str], ledger: Ledger) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    for evidence_id in dict.fromkeys(ids):
        item = ledger.get(evidence_id)
        if item is not None:
            items.append(item)
    return items


class _Validator:
    """Holds the ledger, registry and a per-item V8 cache for one `validate` call."""

    def __init__(self, ledger: Ledger, registry: Mapping[str, MetricDefLike]) -> None:
        self.ledger = ledger
        self.registry = registry
        self._derived: dict[str, bool] = {}

    def derived_ok(self, item: EvidenceItem) -> bool:
        if item.id not in self._derived:
            self._derived[item.id] = derived_ok(item, self.registry)
        return self._derived[item.id]

    def _check_derived(self, items: Iterable[EvidenceItem], sink: _Sink) -> None:
        if not all(self.derived_ok(item) for item in items):
            sink.add("DERIVED_MISMATCH", "tool_result")

    # -- findings --
    def finding(self, finding: Finding, index: int) -> list[ValidationIssue]:
        sink = _Sink("finding", index)
        cited = list(dict.fromkeys(finding.evidence_ids))
        if not cited:
            sink.add("UNKNOWN_EVIDENCE", "missing")
        if any(e not in self.ledger for e in cited):
            sink.add("UNKNOWN_EVIDENCE", "evidence_id")
        if not finding.text.strip():
            sink.add("SCHEMA", "blank")
        placeholders = _check_placeholders(finding.text, set(cited), self.ledger, self.registry, sink)
        for ref in finding.record_refs:
            parsed = parse_record_ref(ref)
            if parsed is None or parsed[0] not in cited:
                sink.add("UNKNOWN_RECORD", "uncited_evidence")
            elif self.ledger.record(ref) is None:
                sink.add("UNKNOWN_RECORD", "record_id")
        _check_free_numbers(finding.text, sink)
        _check_causal(finding.text, sink)
        _check_safe_text(finding.text, sink)

        items = _known_items(cited, self.ledger)
        if not items:
            return sink.issues
        self._check_derived(items, sink)
        if not all(has_data(item) for item in items):
            sink.add("NO_DATA", "evidence")
        kind_items = self._check_kind_and_metric(finding, items, sink)
        directions: list[str | None] = []
        for evidence_id, segments in placeholders:
            item = self.ledger.get(evidence_id)
            if item is None:
                continue
            if is_delta_leaf(item, segments) and DELTA_LEAF_KIND[item.tool] != finding.kind:
                sink.add("KIND_TOOL_MISMATCH", "delta_leaf")
            directions.append(placeholder_direction(item, segments, self.registry))
        self._check_sufficiency(finding, kind_items, placeholders, sink)
        _check_absolutes(finding.text, min_coverage(items), any(is_event_split(i) for i in items), sink)
        self._check_direction(finding, kind_items, sink)
        _check_direction_text(finding.text, directions, sink)
        return sink.issues

    def _check_kind_and_metric(self, finding: Finding, items: list[EvidenceItem], sink: _Sink) -> list[EvidenceItem]:
        """V11 (D-7). Returns the cited items of the kind's tool."""
        tools = _KIND_TOOLS[finding.kind]
        kind_items = [i for i in items if i.tool in tools]
        if not kind_items or any(i.tool not in tools and i.tool != T_COVERAGE for i in items):
            sink.add("KIND_TOOL_MISMATCH", "tool")
        if finding.kind == "records":
            if finding.metric is not None:
                sink.add("METRIC_MISMATCH", "metric")
            return kind_items
        for item in items:
            if item.tool == T_RELATE:
                metrics = {get_str(item.result, "x_metric"), get_str(item.result, "y_metric")}
            elif item_metric(item) is not None:
                metrics = {item_metric(item)}
            else:
                continue
            if finding.metric is None or finding.metric not in metrics:
                sink.add("METRIC_MISMATCH", "metric")
        if finding.metric is None:
            sink.add("METRIC_MISMATCH", "metric")
        return kind_items

    def _check_sufficiency(self, finding: Finding, kind_items: list[EvidenceItem],
                           placeholders: list[tuple[str, list[str]]], sink: _Sink) -> None:
        """V5 per kind, V6/V7 comparison coverage (D-4, D-6)."""
        for item in kind_items:
            r = item.result
            if finding.kind == "value":
                self._check_value_n(item, [p for e, p in placeholders if e == item.id], sink)
            elif finding.kind == "trend":
                issue = trend_issue(item)
                if issue is not None:
                    sink.add(*issue)
            elif finding.kind == "comparison":
                if not comparable(item):
                    sink.add("NOT_COMPARABLE", "comparable")
                kind = _metric_kind(get_str(r, "metric"), r, self.registry)
                raw_counts = {"n_observations", "sum", "count_abs"}
                uses_counts = any(set(p) & raw_counts for e, p in placeholders if e == item.id)
                if kind == "event_count" and uses_counts and gap_too_large(item):
                    sink.add("NOT_COMPARABLE", "rates_only")
            elif finding.kind == "relationship":
                if is_event_split(item):
                    groups = [get_map(r, g) for g in ("with_x", "x_not_recorded")]
                    if any((get_num(g, "n_days") or 0) < policy.SPLIT_MIN_DAYS or get_num(g, "y_mean") is None
                           for g in groups):
                        sink.add("INSUFFICIENT_N", "group_days")
                elif (get_num(r, "pairs") or 0) < policy.CORRELATION_MIN_PAIRS or get_num(r, "pearson_r") is None:
                    sink.add("INSUFFICIENT_N", "pairs")

    def _check_value_n(self, item: EvidenceItem, paths: list[list[str]], sink: _Sink) -> None:
        r = item.result
        kind = "daily_value" if item.tool == T_SERIES else _metric_kind(get_str(r, "metric"), r, self.registry)
        if item.tool == T_COMPARE:
            sides = {p[0] for p in paths if p and p[0] in ("a", "b")} or {"a", "b"}
            n = min(_value_n(get_map(r, side), kind) for side in sides)
        elif item.tool == T_SERIES:
            n = int(get_num(get_map(r, "coverage"), "documented_days") or 0)
        else:
            n = _value_n(r, kind)
        threshold = policy.MIN_N_VALUE
        heads = {p[0] for p in paths if p}
        if item.tool == T_SUMMARY and get_str(r, "metric") == _WEIGHT_METRIC and heads & _WEIGHT_CHANGE_PATHS \
                and heads <= _WEIGHT_CHANGE_PATHS | _WEIGHT_META_PATHS:
            threshold = policy.MIN_N_WEIGHT_CHANGE
            n = int(get_num(r, "n_observations") or 0)
        if n < threshold:
            sink.add("INSUFFICIENT_N", "observations")

    def _check_direction(self, finding: Finding, kind_items: list[EvidenceItem], sink: _Sink) -> None:
        """V9 (D-8): declared direction equals the recomputed one; no opposite-direction words."""
        if finding.kind not in _DIRECTIONAL_KINDS:
            if finding.direction != "not_applicable":
                sink.add("DIRECTION_MISMATCH", "direction")
            return
        if finding.direction == "not_applicable" or any(
                expected_direction(item, self.registry) != finding.direction for item in kind_items):
            sink.add("DIRECTION_MISMATCH", "direction")
        if finding.direction != "not_applicable":
            _check_direction_words(finding.text, finding.direction, sink)

    # -- other parts --
    def summary(self, text: str, finding_ids: set[str],
                findings: Sequence[tuple[Finding, bool]] = ()) -> list[ValidationIssue]:
        """D-2: V2 (ids cited by a finding), V3, V6, V8, V10, V12; D-12 (a)/(b) delta leaves and direction words.

        `findings` pairs each finding with its validity; a delta leaf is allowed only when its evidence is
        cited by a valid finding of the matching kind.
        """
        sink = _Sink("summary", None)
        placeholders = _check_placeholders(text, finding_ids, self.ledger, self.registry, sink)
        directions: list[str | None] = []
        for evidence_id, segments in placeholders:
            item = self.ledger.get(evidence_id)
            if item is None:
                continue
            if is_delta_leaf(item, segments) and not any(
                    ok and f.kind == DELTA_LEAF_KIND[item.tool] and evidence_id in f.evidence_ids
                    for f, ok in findings):
                sink.add("KIND_TOOL_MISMATCH", "delta_leaf")
            directions.append(placeholder_direction(item, segments, self.registry))
        inc, dec, backed = _check_direction_text(text, directions, sink, unbacked_ok=True)
        if (inc or dec) and not backed:
            # Unbacked words are allowed only when every valid directional finding has exactly that direction.
            kept = [f.direction for f, ok in findings if ok and f.kind in _DIRECTIONAL_KINDS]
            wanted = "increase" if inc else "decrease"
            if (inc and dec) or not kept or any(d != wanted for d in kept):
                sink.add("DIRECTION_MISMATCH", "summary_direction")
        _check_free_numbers(text, sink)
        _check_causal(text, sink)
        _check_safe_text(text, sink)
        universe = _known_items(sorted(finding_ids), self.ledger)
        _check_absolutes(text, min_coverage(universe), any(is_event_split(i) for i in universe), sink)
        self._check_derived(_known_items((e for e, _ in placeholders), self.ledger), sink)
        return sink.issues

    def limitation(self, limitation: Limitation, index: int) -> list[ValidationIssue]:
        """D-2: V2 against its own evidence ids, V3, V6, V8, V10, V12; D-12 (c) count/date/period/coverage only."""
        sink = _Sink("limitation", index)
        cited = set(limitation.evidence_ids)
        if any(e not in self.ledger for e in cited):
            sink.add("UNKNOWN_EVIDENCE", "evidence_id")
        if not limitation.text.strip():
            sink.add("SCHEMA", "blank")
        placeholders = _check_placeholders(limitation.text, cited, self.ledger, self.registry, sink)
        directions: list[str | None] = []
        for evidence_id, segments in placeholders:
            item = self.ledger.get(evidence_id)
            leaf = self.ledger.resolve(".".join([evidence_id, *segments]), self.registry)
            if item is None or leaf is None:
                continue
            if is_delta_leaf(item, segments):
                sink.add("KIND_TOOL_MISMATCH", "delta_leaf")
            elif leaf.leaf_kind not in LIMITATION_LEAF_KINDS:
                sink.add("KIND_TOOL_MISMATCH", "limitation_value")
            directions.append(placeholder_direction(item, segments, self.registry))
        _check_free_numbers(limitation.text, sink)
        _check_causal(limitation.text, sink)
        _check_safe_text(limitation.text, sink)
        items = _known_items(sorted(cited), self.ledger)
        _check_absolutes(limitation.text, min_coverage(items), any(is_event_split(i) for i in items), sink)
        _check_direction_text(limitation.text, directions, sink)
        self._check_derived(items, sink)
        return sink.issues

    def chart(self, chart: Chart, index: int, earlier: list[tuple[str, str]]) -> list[ValidationIssue]:
        """V14 (droppable)."""
        sink = _Sink("chart", index)
        item = self.ledger.get(chart.evidence_id)
        if item is None:
            sink.add("UNKNOWN_EVIDENCE", "evidence_id")
        elif not chart_compatible(chart.type, item):
            sink.add("CHART_INCOMPATIBLE", "chart_type")
        elif not self.derived_ok(item):
            sink.add("DERIVED_MISMATCH", "tool_result")
        elif (chart.type, chart.evidence_id) in earlier:
            sink.add("CHART_INCOMPATIBLE", "duplicate")
        return sink.issues

    @staticmethod
    def follow_up(text: str, index: int) -> list[ValidationIssue]:
        """V3 and V12 (droppable). Follow-ups carry no placeholders."""
        sink = _Sink("follow_up", index)
        if "{{" in text or "}}" in text:
            sink.add("UNKNOWN_PLACEHOLDER", "placeholder")
        _check_free_numbers(text, sink)
        _check_safe_text(text, sink)
        return sink.issues

    @staticmethod
    def topic(raw: str) -> tuple[str, list[ValidationIssue]]:
        """V12 topic: letters, spaces and hyphens, <= 60 chars, no number words; else "this question"."""
        sink = _Sink("topic", None)
        topic = " ".join(raw.split())
        if not topic:
            sink.add("TOPIC_INVALID", "blank")
        elif not all(ch.isalpha() or ch in " -" for ch in topic):
            sink.add("TOPIC_INVALID", "charset")
        _check_free_numbers(topic, sink)
        _check_safe_text(topic, sink)
        return (topic if not sink.issues else FALLBACK_TOPIC), sink.issues


def _schema_issues(exc: pydantic.ValidationError) -> tuple[ValidationIssue, ...]:
    locations: Mapping[str, Location] = {
        "findings": "finding", "limitations": "limitation", "charts": "chart",
        "follow_up_questions": "follow_up", "topic": "topic", "summary": "summary",
    }
    issues: list[ValidationIssue] = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False)[:10]:
        loc = err.get("loc", ())
        location: Location = locations.get(str(loc[0]), "status") if loc else "status"
        index = loc[1] if len(loc) > 1 and isinstance(loc[1], int) else None
        issue = ValidationIssue("SCHEMA", location, index, True, str(err.get("type", "invalid")))
        if issue not in issues:
            issues.append(issue)
    return tuple(issues)


def validate(raw_json: str, ledger: Ledger, registry: Mapping[str, MetricDefLike]) -> ValidationReport:
    """Apply V1–V14 to the model's raw final-answer JSON.

    Args:
        raw_json: the final structured output text, untrusted.
        ledger: this question's evidence ledger.
        registry: metric id -> definition (units, kinds, `min_delta`).
    Returns:
        A report; `answer` is None when the strict schema parse (V1) fails.
    """
    try:
        answer = FinalAnswer.model_validate_json(raw_json)
    except pydantic.ValidationError as exc:
        return ValidationReport(answer=None, errors=_schema_issues(exc))

    v = _Validator(ledger, registry)
    errors: list[ValidationIssue] = []

    finding_issues = [v.finding(f, i) for i, f in enumerate(answer.findings)]
    finding_ids = {e for f in answer.findings for e in f.evidence_ids if e in ledger}
    finding_validity = [(f, not issues) for f, issues in zip(answer.findings, finding_issues, strict=True)]
    summary_issues = v.summary(answer.summary, finding_ids, finding_validity)
    limitation_issues = [v.limitation(lim, i) for i, lim in enumerate(answer.limitations)]
    seen_charts: list[tuple[str, str]] = []
    chart_issues: list[list[ValidationIssue]] = []
    for i, chart in enumerate(answer.charts):
        chart_issues.append(v.chart(chart, i, seen_charts))
        seen_charts.append((chart.type, chart.evidence_id))
    follow_issues = [v.follow_up(q, i) for i, q in enumerate(answer.follow_up_questions)]
    topic, topic_issues = v.topic(answer.topic)

    errors.extend(summary_issues)
    for group in (finding_issues, limitation_issues, chart_issues, follow_issues):
        for issues in group:
            errors.extend(issues)
    errors.extend(topic_issues)

    # V13 status consistency
    if answer.status in ("answered", "partial") and not answer.findings:
        errors.append(ValidationIssue("STATUS_INCONSISTENT", "status", None, True, "no_findings"))
    if answer.status == "partial" and not answer.limitations:
        errors.append(ValidationIssue("STATUS_INCONSISTENT", "status", None, True, "no_limitations"))

    return ValidationReport(
        answer=answer,
        errors=tuple(errors),
        finding_ok=tuple(not issues for issues in finding_issues),
        limitation_ok=tuple(not issues for issues in limitation_issues),
        chart_ok=tuple(not issues for issues in chart_issues),
        follow_up_ok=tuple(not issues and bool(q.strip())
                           for issues, q in zip(follow_issues, answer.follow_up_questions, strict=True)),
        summary_ok=not summary_issues,
        topic=topic,
    )


_HINTS: Final[Mapping[str, str]] = {
    "SCHEMA": "return JSON that matches the final-answer schema exactly",
    "UNKNOWN_EVIDENCE": "cite only evidence ids returned by tool calls",
    "UNKNOWN_PLACEHOLDER": "use only placeholders that name an existing numeric or date field",
    "PLACEHOLDER_NOT_CITED": "add the placeholder's evidence id to the finding's evidence_ids",
    "UNKNOWN_RECORD": "reference only records listed in a cited list_records result",
    "FREE_NUMBER": "write every number as a placeholder; no digits or number words",
    "NO_DATA": "do not make findings from evidence without observations",
    "INSUFFICIENT_N": "there are too few observations for this kind of finding",
    "LOW_COVERAGE_TREND": "coverage is too low for a trend",
    "ABSOLUTE_LANGUAGE": "avoid absolute words; some days are undocumented",
    "ABSENCE_CLAIM": "do not claim something did not happen on undocumented days",
    "NOT_COMPARABLE": "these periods are not comparable",
    "DERIVED_MISMATCH": "do not use this evidence item",
    "DIRECTION_MISMATCH": "set direction to the evidence direction and use matching wording",
    "CAUSAL_LANGUAGE": "describe associations only; no causal or predictive words",
    "KIND_TOOL_MISMATCH": "cite evidence from the tool that matches the finding kind",
    "METRIC_MISMATCH": "set metric to the metric of the cited evidence",
    "MARKUP_OR_URL": "plain text only; no links, markup or control characters",
    "TOPIC_INVALID": "topic must be letters, spaces and hyphens only",
    "STATUS_INCONSISTENT": "status must match the findings and limitations",
    "CHART_INCOMPATIBLE": "chart type must match its evidence tool",
}
_FIELD_NAMES: Final[Mapping[str, str]] = {
    "finding": "findings", "limitation": "limitations", "chart": "charts", "follow_up": "follow_up_questions",
}


def repair_message(report: ValidationReport) -> str:
    """The repair-turn text: code, location, index and token class per issue — no data echoed."""
    lines = ["Your final answer failed validation. Return a corrected final answer. Issues:"]
    for issue in dict.fromkeys(report.errors):
        where = _FIELD_NAMES.get(issue.location, issue.location)
        if issue.index is not None:
            where = f"{where}[{issue.index}]"
        token = f" ({issue.token_class})" if issue.token_class else ""
        lines.append(f"- {issue.code} at {where}{token}: {_HINTS[issue.code]}")
    return "\n".join(lines)


def decide(report: ValidationReport, *, repair_used: bool) -> Decision:
    """Accept, repair once, degrade to partial, or fall back (ADR-005 §4.4).

    - unparsable: repair, then fallback (`fallback_reason="unparsable"`);
    - model status `insufficient`: fallback immediately, no repair;
    - no blocking issue: accept with the model's status;
    - blocking and repair unused: repair;
    - blocking after repair: degrade to `partial` when a valid finding remains, else fallback.
    """
    if report.answer is None:
        if not repair_used:
            return Decision("repair", "insufficient", repair_message=repair_message(report))
        return Decision("fallback", "insufficient", fallback_reason="unparsable")
    if report.answer.status == "insufficient":
        return Decision("fallback", "insufficient", fallback_reason="insufficient")
    if not report.blocking:
        return Decision("accept", report.answer.status)
    if not repair_used:
        return Decision("repair", report.answer.status, repair_message=repair_message(report))
    if any(report.finding_ok):
        return Decision("degrade", "partial", extra_limitation=DEGRADE_LIMITATION)
    return Decision("fallback", "insufficient", fallback_reason="no_valid_finding")


def items_for(ids: Iterable[str], ledger: Ledger) -> list[EvidenceItem]:
    """The ledger items for `ids` (unknown ids skipped, duplicates removed)."""
    return _known_items(ids, ledger)
