"""Evidence thresholds (ADR-005 §4.2, approved as tested heuristics by the Owner, O-7).

These are pragmatic heuristics, not statistical significance tests. Per-metric `min_delta`
values come from the B-05 registry (`MetricDef.min_delta`); `DEFAULT_MIN_DELTA` mirrors the
§4.2 V9 defaults by unit and is used only when a registry entry is unavailable (tests).
Changing any value here needs Owner approval (ADR-005 §7: thresholds are never changed silently).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

# V5 — sufficient observations
MIN_N_VALUE = 3                 # value/mean: >= 3 documented days (daily_value) or observations (events, ratio)
MIN_N_WEIGHT_CHANGE = 2         # weight change: >= 2 measurements
TREND_MIN_POINTS = 7            # trend: >= 7 documented points ...
WEIGHT_TREND_MIN_POINTS = 4     # ... weight trend: >= 4 measurements ...
TREND_MIN_SPAN_DAYS = 14        # ... spanning >= 14 days
TREND_MIN_COVERAGE = 0.5        # ... with coverage >= 0.5 (not applied to weight; see worker notes D-3)
CORRELATION_MIN_PAIRS = 10      # relationship with a daily x: >= 10 pairs
SPLIT_MIN_DAYS = 5              # relationship with an event x: each group >= 5 days

# V6 — coverage
LOW_COVERAGE = 0.5              # below: "Low coverage" badge; trend and comparison kinds forbidden
FULL_COVERAGE = 1.0             # below: absolute words are rejected

# V7 — comparison
COMPARE_MIN_DAYS = 3            # each period >= 3 documented days
COMPARE_MIN_COVERAGE = 0.4      # each period coverage >= 0.4
COUNT_COVERAGE_GAP = 0.3        # count metrics: |coverage_a - coverage_b| <= 0.3, otherwise rates only

# V9 — direction
MIN_ABS_R = 0.3                 # |r| below this is "no_clear_change"

DEFAULT_MIN_DELTA: Mapping[str, float] = MappingProxyType({
    "h": 0.25, "steps": 500.0, "kg": 0.5, "kcal": 100.0, "min": 5.0, "%": 5.0,
    # event counts compare per-documented-day rates
    "meals": 0.1, "workouts": 0.1, "items": 0.1, "tasks": 0.1, "applications": 0.1,
})

# V8 — derived-field recomputation tolerance
DERIVED_TOLERANCE = 1e-9

# Agent limits (ADR-005 §3.5)
MAX_QUESTION_CHARS = 500
MAX_TOOL_CALLS = 8
MAX_TOOL_CALLS_PER_TURN = 4
MAX_MODEL_TURNS = 6
MAX_OUTPUT_TOKENS = 4000
DEFAULT_MAX_TOKENS_PER_QUESTION = 40_000
MODEL_CALL_TIMEOUT_S = 30.0
QUESTION_DEADLINE_S = 75.0
SQL_STATEMENT_TIMEOUT_S = 2.0
MAX_TOOL_RESULT_BYTES = 8 * 1024
MAX_SPAN_DAYS = 366
MAX_SERIES_DAYS = 92
MAX_LIST_LIMIT = 50
MAX_GROUPS = 20
MAX_RANGES = 10
DEFAULT_QUESTIONS_PER_HOUR = 20


def direction(delta: float | None, min_delta: float) -> str:
    """Backend direction of a signed delta: increase, decrease or no_clear_change (V9)."""
    if delta is None:
        return "not_applicable"
    if abs(delta) < min_delta:
        return "no_clear_change"
    return "increase" if delta > 0 else "decrease"


def r_direction(r: float | None) -> str:
    """Direction of a Pearson r: |r| < MIN_ABS_R is no_clear_change (V9)."""
    if r is None:
        return "not_applicable"
    if abs(r) < MIN_ABS_R:
        return "no_clear_change"
    return "increase" if r > 0 else "decrease"
