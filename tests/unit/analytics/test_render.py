from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any

import pytest

from app.analytics.ledger import ResolvedLeaf
from app.analytics.render import (
    AI_LABEL,
    ASSOCIATION_CAVEAT,
    FALLBACK_TEMPLATE,
    STATUS_LABELS,
    AnswerView,
    coverage_lines,
    evidence_line,
    format_leaf,
    format_value,
    record_chip,
    render_answer,
    render_fallback,
    render_text,
)
from app.analytics.validator import Decision, decide, validate
from tests.unit.analytics import evidence_samples as s

STEPS_SPLIT = ("Steps were higher by {{E5.difference}} on days with workouts recorded ({{E5.with_x.y_mean}}) "
               "than on days with no workouts recorded ({{E5.x_not_recorded.y_mean}}).")
VERBATIM = ("The available records do not provide sufficient reliable information to analyze sleep for the "
            "selected period. You can add relevant records or expand the date range and try again.")


@pytest.mark.parametrize(("value", "unit", "expected"), [
    (7.26, "h", "7.3 h"), (7.0, "h", "7.0 h"), (12345.6, "steps", "12,346 steps"), (0.0, "steps", "0 steps"),
    (78.44, "kg", "78.4 kg"), (620.4, "kcal", "620 kcal (est.)"), (2150.0, "kcal", "2,150 kcal (est.)"),
    (40.0, "min", "40 min"), (66.6, "%", "67%"), (3.0, "items", "3 items"), (2.5, "meals", "2.5 meals"),
])
def test_format_value(value: float, unit: str, expected: str) -> None:
    assert format_value(value, unit) == expected


@pytest.mark.parametrize(("leaf", "expected"), [
    (ResolvedLeaf(-0.6, "delta", "h"), "\u22120.6 h"),
    (ResolvedLeaf(0.6, "delta", "h"), "+0.6 h"),
    (ResolvedLeaf(0.0, "delta", "h"), "0.0 h"),
    (ResolvedLeaf(0.1, "delta", "h"), "+0.1 h"),          # below min_delta: sign kept, nothing appended
    (ResolvedLeaf(-2000.0, "delta", "steps"), "\u22122,000 steps"),
    (ResolvedLeaf(1.5, "delta", "kg"), "+1.5 kg"),
    (ResolvedLeaf(-0.35, "slope", "kg"), "\u22120.3 kg/week"),
    (ResolvedLeaf(-8.8, "rel_pct", "h"), "\u22129%"),
    (ResolvedLeaf(8.8, "rel_pct", "h"), "+9%"),
    (ResolvedLeaf(5.0, "pct_points", "%"), "+5 pp"),
    (ResolvedLeaf(-12.4, "pct_points", "%"), "\u221212 pp"),
    (ResolvedLeaf(-0.333, "rate_delta", "workouts"), "\u22120.33"),
    (ResolvedLeaf(-3, "count_delta", ""), "\u22123"),
    (ResolvedLeaf(27, "count", "h"), "27"),
    (ResolvedLeaf(0.4667, "rate", "workouts"), "0.47"),
    (ResolvedLeaf(60.0, "pct", "%"), "60%"),
    (ResolvedLeaf(0.9, "coverage_ratio", ""), "90%"),
    (ResolvedLeaf(0.35, "r", ""), "+0.35"),
    (ResolvedLeaf(-0.351, "r", ""), "-0.35"),
    (ResolvedLeaf("2026-10-05", "date", ""), "05.10.2026"),
    (ResolvedLeaf({"start": "2026-09-01", "end": "2026-09-30"}, "period", ""), "01.09.2026–30.09.2026"),
    (ResolvedLeaf({"documented_days": 27, "total_days": 30}, "coverage", ""), "27 documented days of 30"),
    (ResolvedLeaf({"documented_days": 1, "total_days": 7}, "coverage", ""), "1 documented day of 7"),
    (ResolvedLeaf(2019, "number", ""), "2019"),
    (ResolvedLeaf(620, "value", "kcal"), "620 kcal (est.)"),
])
def test_format_leaf(leaf: ResolvedLeaf, expected: str) -> None:
    assert format_leaf(leaf) == expected


def test_render_text_replaces_placeholders_and_rejects_unvalidated() -> None:
    ledger = s.standard_ledger()
    text = "Sleep {{E2.mean}} over {{E2.period}} ({{E2.coverage}}); change {{E3.difference.mean_abs}}."
    assert render_text(text, ledger, s.REGISTRY) == (
        "Sleep 7.2 h over 01.09.2026–30.09.2026 (27 documented days of 30); change +0.6 h.")
    with pytest.raises(ValueError):
        render_text("{{E6.records.101.untrusted_text.description}}", ledger, s.REGISTRY)


def test_signed_deltas_show_true_direction_whatever_the_words() -> None:
    gain = s.make_ledger(s.t2("weight.kg", n=3, documented=3, mean=76.8, first=76.0, last=77.5))
    assert render_text("Change {{E1.change}}.", gain, s.REGISTRY) == "Change +1.5 kg."
    falling = s.make_ledger(s.t3("sleep.hours", s.summary("sleep.hours", n=14, documented=14, total=15, mean=7.4),
                                 s.summary("sleep.hours", n=14, documented=14, total=15, mean=6.8,
                                           start="2026-09-16")))
    text = "{{E1.difference.mean_abs}} {{E1.difference.mean_rel_pct}} {{E1.difference.count_abs}}"
    assert render_text(text, falling, s.REGISTRY) == "−0.6 h −8% 0"
    trend = s.standard_ledger()
    assert render_text("{{E4.trend.slope_per_week}}", trend, s.REGISTRY) == "+0.2 h/week"


def test_coverage_lines_per_tool() -> None:
    ledger = s.standard_ledger()

    def lines(evidence_id: str) -> tuple[list[str], bool]:
        item = ledger.get(evidence_id)
        assert item is not None
        return coverage_lines(item, s.REGISTRY)

    assert lines("E2") == (["Based on 27 documented days of 30 · 01.09–30.09"], False)
    assert lines("E3") == (["A: Based on 14 documented days of 15 · 01.09–15.09",
                            "B: Based on 13 documented days of 15 · 16.09–30.09"], False)
    t5_lines, t5_low = lines("E5")
    assert t5_lines == ["Workouts: Based on 12 documented days of 30 · 01.09–30.09",
                        "Steps: Based on 28 documented days of 30 · 01.09–30.09"]
    assert t5_low  # workouts coverage 0.4 < 0.5
    assert lines("E6") == (["Based on 2 of 2 matching records · 01.10–17.10"], False)


@pytest.mark.parametrize(("documented", "low"), [(49, True), (50, False)])
def test_low_coverage_boundary(documented: int, low: bool) -> None:
    ledger = s.make_ledger(s.t2("sleep.hours", n=documented, documented=documented, total=100, mean=7.0))
    item = ledger.get("E1")
    assert item is not None
    assert coverage_lines(item, s.REGISTRY)[1] is low


def test_record_chip_uses_enum_and_date_only() -> None:
    record = s.meal_records()[0]
    assert record_chip(record, "meals") == "Meal · lunch · 05.10.2026"
    odd = {"record_id": 1, "date": "2026-10-05", "fields": {"meal_type": "Lunch <b>"}, "untrusted_text": {}}
    assert record_chip(odd, "meals") == "Meal · 05.10.2026"
    assert record_chip({"date": "2026-10-05", "fields": {"workout_type": "stick_spinning"}}, "workouts") == (
        "Workout · stick spinning · 05.10.2026")


def test_evidence_lines_in_plain_words() -> None:
    ledger = s.standard_ledger()
    lines = [evidence_line(item, s.REGISTRY) for item in ledger]
    assert lines[0].startswith("E1 · Data coverage · Sleep: 27 documented days of 30")
    assert lines[1] == ("E2 · Summary · Sleep (h) · 01.09.2026–30.09.2026 · 27 observations · "
                        "27 documented days of 30")
    assert "A 01.09.2026–15.09.2026, 14 observations" in lines[2]
    assert lines[3].startswith("E4 · Daily series · Sleep (h)")
    assert "Workouts and Steps" in lines[4] and "10 days with, 15 days not recorded" in lines[4]
    assert lines[5] == "E6 · Record list · Meals · 01.10.2026–17.10.2026 · 2 of 2 matching records"


def test_fallback_is_verbatim_with_missing_lines() -> None:
    ledger = s.make_ledger(s.t1({"sleep": (2, 30)}), s.t2("steps.count", n=1, documented=1, mean=8000.0))
    view = render_fallback("sleep", ledger)
    assert FALLBACK_TEMPLATE.replace("{topic}", "sleep") == view.message == VERBATIM
    assert view.missing == ("Sleep: 2 documented days in 01.09.2026–30.09.2026",
                            "Steps: 1 documented day in 01.09.2026–30.09.2026")
    assert render_fallback("this question", s.make_ledger()).missing == ()


def view_for(data: dict[str, Any], *, repair_used: bool = False) -> tuple[AnswerView, Decision]:
    ledger = s.standard_ledger()
    report = validate(s.dumps(data), ledger, s.REGISTRY)
    decision = decide(report, repair_used=repair_used)
    return render_answer(report, decision, ledger, s.REGISTRY, "How did I sleep?", tokens_used=1234), decision


def full_answer() -> dict[str, Any]:
    return s.answer(
        summary_text="Sleep averaged {{E2.mean}}; it was higher later by {{E3.difference.mean_abs}}.",
        findings=[
            s.finding(),
            s.finding("Average sleep was higher later by {{E3.difference.mean_abs}} ({{E3.b.mean}} vs "
                      "{{E3.a.mean}}).", kind="comparison", evidence_ids=["E3"], direction="increase"),
            s.finding(STEPS_SPLIT, kind="relationship", evidence_ids=["E5"], metric="steps.count",
                      direction="increase"),
            s.finding("A lunch was logged on {{E6.records.101.date}}.", kind="records", evidence_ids=["E6"],
                      metric=None, record_refs=["E6#101"]),
        ],
        limitations=[{"text": "Some nights are undocumented ({{E1.domains.sleep.coverage}}).",
                      "evidence_ids": ["E1"]}],
        charts=[{"type": "line", "evidence_id": "E4"}, {"type": "comparison", "evidence_id": "E3"}],
        follow_ups=["How did my steps change?"],
    )


def test_render_answer_accept() -> None:
    view, decision = view_for(full_answer())
    assert decision.action == "accept"
    assert (view.status, view.status_label, view.ai_label) == ("answered", "Answered", AI_LABEL)
    assert view.summary == "Sleep averaged 7.2 h; it was higher later by +0.6 h."
    assert [f.id for f in view.findings] == ["F1", "F2", "F3", "F4"]
    assert view.findings[0].text == "Average sleep was 7.2 h."
    assert view.findings[0].coverage_lines == ("Based on 27 documented days of 30 · 01.09–30.09",)
    assert view.findings[1].caveat is None
    assert view.findings[2].caveat == ASSOCIATION_CAVEAT == "Association only; this does not show cause."
    assert view.findings[2].low_coverage is True
    assert "9,000 steps" in view.findings[2].text and "7,000 steps" in view.findings[2].text
    assert view.findings[3].record_chips == ("Meal · lunch · 05.10.2026",)
    assert view.limitations == ("Some nights are undocumented (27 documented days of 30).",)
    assert [c.type for c in view.charts] == ["line", "comparison"]
    assert view.follow_ups == ("How did my steps change?",)
    assert view.fallback is None and view.tokens_used == 1234 and len(view.evidence) == 7
    assert [(t.label, t.value) for t in view.tiles] == [("Sleep", "7.2 h"), ("Sleep", "+0.6 h"),
                                                        ("Sleep", "7.4 h"), ("Sleep", "6.8 h")]
    assert view.tiles[0].detail == "average" and view.tiles[2].detail == "average, period B"


def test_render_answer_degrade_omits_invalid_parts() -> None:
    data = s.answer(
        summary_text="Later sleep was {{E3.b.mean}}.",
        findings=[s.finding(), s.finding("Later sleep was {{E3.b.mean}} because of workouts.", kind="comparison",
                                         evidence_ids=["E3"], direction="increase")],
        charts=[{"type": "bar", "evidence_id": "E2"}],
    )
    view, decision = view_for(data, repair_used=True)
    assert decision.action == "degrade"
    assert (view.status, view.status_label) == ("partial", "Partial answer")
    assert [f.text for f in view.findings] == ["Average sleep was 7.2 h."]
    assert view.summary is None  # cites evidence only the dropped finding used
    assert view.limitations == ("Some parts of the analysis could not be verified and were omitted.",)
    assert view.charts == ()


def test_render_answer_fallback_and_repair() -> None:
    view, decision = view_for(s.answer(status="insufficient", topic="mood", findings=[]))
    assert decision.action == "fallback"
    assert (view.status, view.status_label) == ("insufficient", "Not enough data")
    assert view.fallback is not None and "analyze mood for the selected period" in view.fallback.message
    assert view.findings == () and view.summary is None
    assert STATUS_LABELS == {"answered": "Answered", "partial": "Partial answer", "insufficient": "Not enough data"}
    ledger = s.standard_ledger()
    report = validate(s.dumps(s.answer(findings=[s.finding("Sleep 7 h.")])), ledger, s.REGISTRY)
    with pytest.raises(ValueError):
        render_answer(report, decide(report, repair_used=False), ledger, s.REGISTRY, "q")


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            yield from _strings(getattr(value, f.name))
    elif isinstance(value, tuple | list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)


def test_untrusted_text_never_reaches_any_view_string() -> None:
    view, decision = view_for(full_answer())
    assert decision.action == "accept"
    text = "\n".join(_strings(view))
    assert s.INJECTION not in text and "ignore previous" not in text and "Pasta" not in text
    assert "lost" not in text
    # an attempt to pull the injected text through a placeholder is rejected, and fallback shows no text either
    attempt = s.answer(findings=[s.finding("Note: {{E6.records.101.untrusted_text.description}}", kind="records",
                                           evidence_ids=["E6"], metric=None, record_refs=["E6#101"])])
    for repair_used in (False, True):
        report = validate(s.dumps(attempt), s.standard_ledger(), s.REGISTRY)
        decision = decide(report, repair_used=repair_used)
        if decision.action != "repair":
            rendered = render_answer(report, decision, s.standard_ledger(), s.REGISTRY, "q")
            assert s.INJECTION not in "\n".join(_strings(rendered))
