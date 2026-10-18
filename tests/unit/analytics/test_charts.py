from __future__ import annotations

import pytest

from app.analytics.charts import PAD_TOP, PLOT_H, VIEW_BOX, VIEW_H, VIEW_W, ChartView, build_chart
from app.analytics.ledger import EvidenceItem, Ledger
from tests.unit.analytics import evidence_samples as s


def item(ledger: Ledger, evidence_id: str = "E1") -> EvidenceItem:
    found = ledger.get(evidence_id)
    assert found is not None
    return found


def chart(chart_type: str, evidence_id: str, ledger: Ledger | None = None) -> ChartView | None:
    return build_chart(chart_type, item(ledger or s.standard_ledger(), evidence_id), s.REGISTRY)


def _in_box(x: float, y: float) -> bool:
    return 0 <= x <= VIEW_W and 0 <= y <= VIEW_H and round(x, 1) == x and round(y, 1) == y


def test_line_breaks_at_undocumented_days_and_never_draws_zero() -> None:
    values: list[float | None] = [7.0, 7.2, None, 6.8, 7.1, 7.3, None, None, 7.0]
    view = build_chart("line", item(s.make_ledger(s.t4("sleep.hours", values))), s.REGISTRY)
    assert view is not None and view.line is not None
    assert view.view_box == VIEW_BOX == "0 0 320 160"
    assert len(view.line.dots) == 6                       # one per documented day
    assert len(view.line.segments) == 2                   # [0,1] and [3,4,5]; day 8 is isolated
    assert [len(seg.split()) for seg in view.line.segments] == [2, 3]
    assert all(_in_box(x, y) for x, y in view.line.dots)
    baseline = PAD_TOP + PLOT_H
    assert max(y for _, y in view.line.dots) <= baseline   # lowest value is at the bottom, not below
    assert view.rows[2] == ("03.09.2026", "No record entered")
    assert view.rows[0] == ("01.09.2026", "7.0 h")
    assert view.title == "Sleep per day" and view.y_label == "Sleep (h)"
    assert "gaps, not zero" in view.aria_label
    assert view.line.trend is None                        # 6 points: V5 forbids a trend line


def test_line_trend_only_when_v5_allows() -> None:
    view = chart("line", "E4")
    assert view is not None and view.line is not None and view.line.trend is not None
    x1, y1, x2, y2 = view.line.trend
    assert all(_in_box(x, y) for x, y in ((x1, y1), (x2, y2)))
    assert y2 < y1  # rising values are drawn upwards
    assert "trend line" in view.aria_label


def test_flat_and_single_point_series() -> None:
    flat = build_chart("line", item(s.make_ledger(s.t4("steps.count", [8000.0, 8000.0, None]))), s.REGISTRY)
    assert flat is not None and flat.line is not None and len(flat.line.segments) == 1
    single = build_chart("line", item(s.make_ledger(s.t4("weight.kg", [78.0]))), s.REGISTRY)
    assert single is not None and single.line is not None and single.line.segments == ()
    assert single.line.dots and _in_box(*single.line.dots[0])
    empty = build_chart("line", item(s.make_ledger(s.t4("sleep.hours", [None, None]))), s.REGISTRY)
    assert empty is None


def test_bar_from_t2_groups() -> None:
    view = chart("bar", "E7")
    assert view is not None and view.line is None
    assert [b.label for b in view.bars] == ["aerobic", "weights"]
    aerobic, weights = view.bars
    assert aerobic.height == pytest.approx(PLOT_H, abs=0.1)
    assert weights.height == pytest.approx(PLOT_H * (4 / 4) / (8 / 7), abs=0.1)
    assert aerobic.value_label == "1.14 per documented day"
    assert view.title == "Workouts by workout type"
    assert view.rows[0] == ("aerobic", "1.14 per documented day", "8", "7")
    assert all(_in_box(b.x, b.y) and b.x + b.width <= VIEW_W for b in view.bars)


def test_comparison_from_t3_and_t5_event_split() -> None:
    periods = chart("comparison", "E3")
    assert periods is not None
    assert [b.label for b in periods.bars] == ["Period A", "Period B"]
    assert periods.bars[0].note == "n = 14 · 93% coverage"
    assert periods.bars[1].value_label == "7.4 h"
    split = chart("comparison", "E5")
    assert split is not None
    assert [b.label for b in split.bars] == ["With workouts recorded", "No workouts recorded"]
    assert split.bars[0].note == "10 days" and split.bars[0].value_label == "9,000 steps"
    assert "no workouts recorded" in split.title


@pytest.mark.parametrize(("chart_type", "evidence_id"), [
    ("line", "E2"), ("bar", "E2"), ("bar", "E3"), ("comparison", "E2"), ("comparison", "E4"), ("line", "E6"),
    ("pie", "E4"),
])
def test_incompatible_chart_is_none(chart_type: str, evidence_id: str) -> None:
    assert chart(chart_type, evidence_id) is None


def test_t5_daily_relation_has_no_comparison_chart() -> None:
    ledger = s.make_ledger(s.t5_daily("sleep.hours", "steps.count", pairs=12, r=0.4))
    assert build_chart("comparison", item(ledger), s.REGISTRY) is None
