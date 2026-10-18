from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from app.analytics.ledger import Ledger
from app.analytics.validator import (
    DEGRADE_LIMITATION,
    FALLBACK_TOPIC,
    ValidationReport,
    decide,
    derived_ok,
    repair_message,
    validate,
)
from tests.unit.analytics import evidence_samples as s

STEPS_SPLIT = ("Steps were higher by {{E5.difference}} on days with workouts recorded ({{E5.with_x.y_mean}}) "
               "than on days with no workouts recorded ({{E5.x_not_recorded.y_mean}}).")


NEUTRAL = "Here is what your records show."


def run(data: Mapping[str, Any], ledger: Ledger | None = None) -> ValidationReport:
    return validate(s.dumps(data), ledger if ledger is not None else s.standard_ledger(), s.REGISTRY)


def codes(report: ValidationReport, location: str | None = None) -> set[str]:
    return {e.code for e in report.errors if location is None or e.location == location}


def one(text: str = "Average sleep was {{E2.mean}}.", ledger: Ledger | None = None,
        **kwargs: Any) -> ValidationReport:
    """Validate an answer with a single finding and a summary without placeholders."""
    return run(s.answer(summary_text=NEUTRAL, findings=[s.finding(text, **kwargs)]), ledger)


def test_standard_answer_is_clean_and_accepted() -> None:
    report = run(s.answer())
    assert report.errors == ()
    assert report.finding_ok == (True,) and report.summary_ok and report.topic == "sleep"
    decision = decide(report, repair_used=False)
    assert decision.action == "accept" and decision.status == "answered"


def test_every_standard_item_passes_v8() -> None:
    ledger = s.standard_ledger()
    assert all(derived_ok(item, s.REGISTRY) for item in ledger)


# --- V1 SCHEMA ---

@pytest.mark.parametrize("raw", ["", "not json", "[]", '{"status": "answered"}'])
def test_v1_unparsable(raw: str) -> None:
    report = validate(raw, s.standard_ledger(), s.REGISTRY)
    assert report.answer is None and codes(report) == {"SCHEMA"} and report.blocking


def test_v1_chart_with_extra_field_is_schema_error_at_chart() -> None:
    data = s.answer(charts=[{"type": "line", "evidence_id": "E4", "points": [9, 9, 9]}])
    report = run(data)
    assert report.answer is None
    assert any(e.code == "SCHEMA" and e.location == "chart" and e.index == 0 for e in report.errors)
    assert all(e.token_class for e in report.errors)


def test_blank_finding_text_is_rejected() -> None:
    assert "SCHEMA" in codes(one("   "))


# --- V2 evidence, placeholders, records ---

def test_v2_unknown_and_missing_evidence() -> None:
    assert "UNKNOWN_EVIDENCE" in codes(one(evidence_ids=["E8"]))
    assert "UNKNOWN_EVIDENCE" in codes(one(evidence_ids=[]))
    assert "UNKNOWN_EVIDENCE" not in codes(one())


@pytest.mark.parametrize(("text", "code"), [
    ("Average sleep was {{E2.nope}}.", "UNKNOWN_PLACEHOLDER"),
    ("Average sleep was {{E2.change}}.", "UNKNOWN_PLACEHOLDER"),          # null leaf
    ("Average sleep was {{E9.mean}}.", "UNKNOWN_PLACEHOLDER"),            # malformed id
    ("Average sleep was {{ E2.mean }}.", "UNKNOWN_PLACEHOLDER"),          # malformed spacing
    ("Average sleep was {{E2.mean}.", "UNKNOWN_PLACEHOLDER"),
    ("Average sleep was {{E3.b.mean}}.", "PLACEHOLDER_NOT_CITED"),        # E3 exists but not cited
    ("Average sleep was {{E2.assessment.low_coverage}}.", "UNKNOWN_PLACEHOLDER"),
])
def test_v2_placeholders(text: str, code: str) -> None:
    report = one(text)
    assert code in codes(report)
    assert all(e.blocking for e in report.errors if e.code == code)


def test_v2_placeholder_to_cited_missing_evidence() -> None:
    report = one("Average sleep was {{E8.mean}}.", evidence_ids=["E2", "E8"])
    assert {"UNKNOWN_EVIDENCE", "UNKNOWN_PLACEHOLDER"} <= codes(report)


def test_v2_record_refs() -> None:
    ok = one("A lunch was logged on {{E6.records.101.date}}.", kind="records", evidence_ids=["E6"], metric=None,
             record_refs=["E6#101"])
    assert ok.errors == ()
    bad = one("A lunch was logged.", kind="records", evidence_ids=["E6"], metric=None, record_refs=["E6#999"])
    assert {e.token_class for e in bad.errors if e.code == "UNKNOWN_RECORD"} == {"record_id"}
    uncited = one(record_refs=["E6#101"])
    assert {e.token_class for e in uncited.errors if e.code == "UNKNOWN_RECORD"} == {"uncited_evidence"}
    not_t6 = one(record_refs=["E2#101"])
    assert "UNKNOWN_RECORD" in codes(not_t6)


# --- V3 FREE_NUMBER ---

@pytest.mark.parametrize(("text", "token"), [
    ("You slept 9.5 hours on average, {{E2.mean}}.", "digit"),
    ("Average sleep was {{E2.mean}} on 7 days.", "digit"),
    ("Average sleep was {{E2.mean}} on ٧ days.", "digit"),         # Arabic-Indic digit
    ("Average sleep was {{E2.mean}}, about ½ an hour more.", "numeral"),
    ("Average sleep was seven hours, {{E2.mean}}.", "number_word"),
    ("Average sleep was {{E2.mean}}, SEVENTY percent of nights.", "number_word"),
    ("Average sleep was {{E2.mean}}, twice the minimum.", "number_word"),
    ("Average sleep was {{E2.mean}} for a dozen nights.", "number_word"),
])
def test_v3_free_numbers(text: str, token: str) -> None:
    report = one(text)
    assert any(e.code == "FREE_NUMBER" and e.token_class == token and e.blocking for e in report.errors)


def test_v3_whole_words_and_placeholders_are_fine() -> None:
    report = one("Someone often slept well; the average was {{E2.mean}} across {{E2.coverage}}.")
    assert "FREE_NUMBER" not in codes(report)


def test_v3_applies_to_summary_limitations_follow_ups_and_topic() -> None:
    report = run(s.answer(summary_text="You slept 7 hours.",
                          limitations=[{"text": "Three days are missing.", "evidence_ids": []}],
                          follow_ups=["What about 2025?", "How did my steps change?"], topic="sleep in"
                          " twenty days"))
    assert {"summary", "limitation", "follow_up", "topic"} <= {e.location for e in report.errors
                                                                if e.code == "FREE_NUMBER"}
    assert report.follow_up_ok == (False, True)
    assert report.topic == FALLBACK_TOPIC
    assert all(not e.blocking for e in report.errors if e.location in ("follow_up", "topic"))


# --- V4 NO_DATA ---

def test_v4_no_data() -> None:
    ledger = s.make_ledger(s.t2("sleep.hours", n=0, documented=0, mean=None))
    report = one("Average sleep was not recorded.", ledger, evidence_ids=["E1"])
    assert "NO_DATA" in codes(report)
    split = s.make_ledger(s.t5_event("workouts.count", "steps.count", with_n=0, with_mean=None, none_n=20,
                                     none_mean=7000.0))
    assert "NO_DATA" in codes(one(STEPS_SPLIT.replace("E5", "E1"), split, kind="relationship",
                                  evidence_ids=["E1"], metric="steps.count", direction="increase"))
    assert "NO_DATA" not in codes(one())


# --- V5 INSUFFICIENT_N / LOW_COVERAGE_TREND ---

@pytest.mark.parametrize(("documented", "ok"), [(2, False), (3, True)])
def test_v5_value_daily_needs_three_documented_days(documented: int, ok: bool) -> None:
    ledger = s.make_ledger(s.t2("sleep.hours", n=documented, documented=documented, mean=7.0))
    report = one("Average sleep was {{E1.mean}}.", ledger, evidence_ids=["E1"])
    assert ("INSUFFICIENT_N" not in codes(report)) is ok


@pytest.mark.parametrize(("n", "ok"), [(2, False), (3, True)])
def test_v5_value_event_needs_three_observations(n: int, ok: bool) -> None:
    ledger = s.make_ledger(s.t2("movies.count", n=n, documented=n))
    report = one("Movies watched: {{E1.n_observations}}.", ledger, evidence_ids=["E1"], metric="movies.count")
    assert ("INSUFFICIENT_N" not in codes(report)) is ok


def test_v5_weight_change_needs_two_measurements_but_mean_needs_three() -> None:
    ledger = s.make_ledger(s.t2("weight.kg", n=2, documented=2, mean=78.0, first=78.4, last=77.6))
    change = one("Weight went from {{E1.first.value}} to {{E1.last.value}}, a change of {{E1.change}}.", ledger,
                 evidence_ids=["E1"], metric="weight.kg")
    assert change.errors == ()
    mean = one("Average weight was {{E1.mean}}.", ledger, evidence_ids=["E1"], metric="weight.kg")
    assert "INSUFFICIENT_N" in codes(mean)
    one_point = s.make_ledger(s.t2("weight.kg", n=1, documented=1, mean=78.0, first=78.0, last=78.0))
    assert "UNKNOWN_PLACEHOLDER" in codes(one("Change {{E1.change}}.", one_point, evidence_ids=["E1"],
                                              metric="weight.kg"))


def trend_finding(ledger: Ledger, *, direction: str = "increase", metric: str = "sleep.hours") -> ValidationReport:
    return one("Sleep rose by {{E1.trend.change_over_period}} over {{E1.period}}.", ledger, kind="trend",
               evidence_ids=["E1"], metric=metric, direction=direction)


def series(days: int, documented_idx: Sequence[int], start_value: float = 6.0, step: float = 0.1,
           metric: str = "sleep.hours") -> Ledger:
    values: list[float | None] = [None] * days
    for i in documented_idx:
        values[i] = start_value + step * i
    return s.make_ledger(s.t4(metric, values))


def test_v5_trend_rules() -> None:
    assert trend_finding(series(15, range(0, 15, 2))).errors == ()          # 8 points, span 14, cov 8/15
    four = trend_finding(series(15, (0, 5, 10, 14)))
    assert {e.token_class for e in four.errors if e.code == "INSUFFICIENT_N"} == {"points"}
    six = trend_finding(series(15, (0, 2, 4, 6, 8, 14)))
    assert "INSUFFICIENT_N" in codes(six)
    seven = trend_finding(series(15, (0, 2, 4, 6, 8, 10, 14)))
    # 7 points spanning 14 days pass the n rule; 7/15 coverage is then below 0.5 (V5 coverage rule)
    assert "INSUFFICIENT_N" not in codes(seven) and "LOW_COVERAGE_TREND" in codes(seven)
    short = trend_finding(series(14, range(14)))                            # span 13
    assert {e.token_class for e in short.errors if e.code == "INSUFFICIENT_N"} == {"span"}


@pytest.mark.parametrize(("documented", "ok"), [(49, False), (50, True)])
def test_v5_trend_coverage_boundary(documented: int, ok: bool) -> None:
    idx = list(range(0, 2 * documented, 2))  # every other day of 100
    report = trend_finding(series(100, idx, step=0.05))
    assert ("LOW_COVERAGE_TREND" not in codes(report)) is ok


def test_v5_weight_trend_uses_weight_rule_without_coverage_floor() -> None:
    ok = trend_finding(series(15, (0, 4, 9, 14), start_value=80.0, step=-0.1, metric="weight.kg"),
                       direction="decrease", metric="weight.kg")
    assert "INSUFFICIENT_N" not in codes(ok) and "LOW_COVERAGE_TREND" not in codes(ok)
    three = trend_finding(series(15, (0, 7, 14), start_value=80.0, step=-0.1, metric="weight.kg"),
                          direction="decrease", metric="weight.kg")
    assert "INSUFFICIENT_N" in codes(three)


def relationship(ledger: Ledger, direction: str, text: str = "Sleep and steps moved together, r {{E1.pearson_r}}.",
                 metric: str = "steps.count") -> ValidationReport:
    return one(text, ledger, kind="relationship", evidence_ids=["E1"], metric=metric, direction=direction)


@pytest.mark.parametrize(("pairs", "ok"), [(9, False), (10, True)])
def test_v5_correlation_pairs(pairs: int, ok: bool) -> None:
    ledger = s.make_ledger(s.t5_daily("sleep.hours", "steps.count", pairs=pairs, r=0.5))
    assert ("INSUFFICIENT_N" not in codes(relationship(ledger, "increase"))) is ok


@pytest.mark.parametrize(("days", "ok"), [(4, False), (5, True)])
def test_v5_condition_split_group_days(days: int, ok: bool) -> None:
    ledger = s.make_ledger(s.t5_event("workouts.count", "steps.count", with_n=days, with_mean=9000.0, none_n=20,
                                      none_mean=7000.0))
    report = relationship(ledger, "increase", STEPS_SPLIT.replace("E5", "E1"))
    assert ("INSUFFICIENT_N" not in codes(report)) is ok


# --- V6 absolutes and absence claims ---

@pytest.mark.parametrize("word", ["never", "Always", "every day", "each  day", "all days", "none", "zero"])
def test_v6_absolutes_rejected_below_full_coverage(word: str) -> None:
    report = one(f"You {word} logged sleep; average {{{{E2.mean}}}}.")
    assert "ABSOLUTE_LANGUAGE" in codes(report)


def test_v6_absolutes_allowed_at_full_coverage() -> None:
    ledger = s.make_ledger(s.t2("sleep.hours", n=30, documented=30, mean=7.0))
    report = one("You logged sleep every day; average {{E1.mean}}.", ledger, evidence_ids=["E1"])
    assert "ABSOLUTE_LANGUAGE" not in codes(report)


@pytest.mark.parametrize("text", [
    "You didn't work out on some days; average {{E2.mean}}.",
    "You didn’t work out on some days; average {{E2.mean}}.",
    "You did not exercise; average {{E2.mean}}.",
    "You skipped workouts; average {{E2.mean}}.",
    "You missed sleep logging; average {{E2.mean}}.",
    "On rest days the average was {{E2.mean}}.",
    "On days with no workout the average was {{E2.mean}}.",
])
def test_v6_absence_claims(text: str) -> None:
    assert "ABSENCE_CLAIM" in codes(one(text))


def test_v6_absence_wording_allowed_with_t5_x_not_recorded() -> None:
    report = one(STEPS_SPLIT, kind="relationship", evidence_ids=["E5"], metric="steps.count", direction="increase")
    assert report.errors == ()


def test_v6_summary_absolutes() -> None:
    report = run(s.answer(summary_text="You never slept less than {{E2.mean}}."))
    assert "ABSOLUTE_LANGUAGE" in codes(report, "summary") and not report.summary_ok


# --- V7 comparison ---

def compare_ledger(doc_a: int, doc_b: int, total: int = 20, metric: str = "sleep.hours",
                   mean_a: float | None = 6.8, mean_b: float | None = 7.4, n_a: int | None = None,
                   n_b: int | None = None, comparable: bool | None = None) -> Ledger:
    a = s.summary(metric, n=doc_a if n_a is None else n_a, documented=doc_a, total=total, mean=mean_a)
    b = s.summary(metric, n=doc_b if n_b is None else n_b, documented=doc_b, total=total, mean=mean_b,
                  start=s.iso_plus(s.START, total))
    return s.make_ledger(s.t3(metric, a, b, comparable=comparable))


def comparison(ledger: Ledger, direction: str = "increase", text: str | None = None,
               metric: str = "sleep.hours") -> ValidationReport:
    text = text or "Average sleep was higher later by {{E1.difference.mean_abs}} ({{E1.b.mean}})."
    return one(text, ledger, kind="comparison", evidence_ids=["E1"], metric=metric, direction=direction)


@pytest.mark.parametrize(("doc_a", "ok"), [(7, False), (8, False), (9, False), (10, True)])
def test_v7_coverage_boundaries(doc_a: int, ok: bool) -> None:
    # 7/20 = 0.35 and 8/20 = 0.40: V7; 9/20 = 0.45: D-4 (< 0.5); 10/20 = 0.50 passes
    assert ("NOT_COMPARABLE" not in codes(comparison(compare_ledger(doc_a, 18)))) is ok


def test_v7_tool_flag_false_and_recomputed_rule() -> None:
    assert "NOT_COMPARABLE" in codes(comparison(compare_ledger(15, 15, comparable=False)))
    # flag says comparable but only 2 documented days per period (recomputed rule)
    assert "NOT_COMPARABLE" in codes(comparison(compare_ledger(2, 2, total=3, comparable=True)))


def test_v7_count_metric_with_coverage_gap_uses_rates_only() -> None:
    ledger = compare_ledger(18, 10, metric="workouts.count", mean_a=None, mean_b=None, n_a=9, n_b=9)
    rate_text = "Workouts per documented day were higher later ({{E1.difference.rate_abs}} more)."
    assert comparison(ledger, metric="workouts.count", text=rate_text).errors == ()
    count_text = "Workouts were higher later ({{E1.b.n_observations}} vs {{E1.a.n_observations}})."
    report = comparison(ledger, metric="workouts.count", text=count_text)
    assert any(e.code == "NOT_COMPARABLE" and e.token_class == "rates_only" for e in report.errors)


# --- V8 DERIVED_MISMATCH ---

def tampered(index: int, path: Sequence[str], value: Any) -> Ledger:
    results = s.standard_results()
    target: Any = results[index]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return s.make_ledger(*results)


@pytest.mark.parametrize(("index", "path", "value"), [
    (1, ["coverage", "ratio"], 0.95),
    (2, ["difference", "mean_abs"], 0.9),
    (2, ["difference", "mean_rel_pct"], 50.0),
    (2, ["difference", "count_abs"], 5),
    (2, ["direction"], "decrease"),
    (2, ["a", "coverage", "ratio"], 0.5),
    (3, ["trend", "change_over_period"], 3.0),
    (3, ["direction"], "decrease"),
    (4, ["difference"], 500.0),
    (4, ["direction"], "no_clear_change"),
    (6, ["rate_per_documented_day"], 2.0),
    (0, ["domains", "sleep", "coverage", "ratio"], 1.0),
])
def test_v8_tampered_derived_fields(index: int, path: Sequence[str], value: Any) -> None:
    ledger = tampered(index, path, value)
    item = ledger.get(f"E{index + 1}")
    assert item is not None and not derived_ok(item, s.REGISTRY)


def test_v8_ratio_pct_and_weight_change() -> None:
    ratio = s.t2("tasks.completion_rate", n=10, documented=8, ratio=(6, 10))
    assert derived_ok(s.make_ledger(ratio).get("E1"), s.REGISTRY)  # type: ignore[arg-type]
    ratio["ratio"]["pct"] = 70.0
    assert not derived_ok(s.make_ledger(ratio).get("E1"), s.REGISTRY)  # type: ignore[arg-type]
    weight = s.t2("weight.kg", n=3, documented=3, mean=78.0, first=78.4, last=77.6)
    weight["change"] = -0.3
    assert not derived_ok(s.make_ledger(weight).get("E1"), s.REGISTRY)  # type: ignore[arg-type]


def test_v8_count_abs_present_despite_gap() -> None:
    results = s.standard_results()
    results[2]["a"]["coverage"].update(documented_days=4, total_days=15, ratio=4 / 15)
    results[2]["difference"]["count_abs"] = results[2]["b"]["n_observations"] - results[2]["a"]["n_observations"]
    ledger = s.make_ledger(*results)
    assert not derived_ok(ledger.get("E3"), s.REGISTRY)  # type: ignore[arg-type]


def test_v8_mismatch_blocks_citing_finding_summary_and_drops_chart() -> None:
    ledger = tampered(1, ["coverage", "ratio"], 0.95)
    report = run(s.answer(charts=[{"type": "line", "evidence_id": "E4"}]), ledger)
    assert {e.location for e in report.errors if e.code == "DERIVED_MISMATCH"} == {"finding", "summary"}
    assert report.blocking
    report = run(s.answer(charts=[{"type": "line", "evidence_id": "E4"}]), tampered(3, ["direction"], "decrease"))
    assert any(e.code == "DERIVED_MISMATCH" and e.location == "chart" and not e.blocking for e in report.errors)


# --- V9 direction ---

def test_v9_comparison_direction() -> None:
    assert comparison(compare_ledger(15, 15)).errors == ()
    report = comparison(compare_ledger(15, 15), direction="decrease")
    assert "DIRECTION_MISMATCH" in codes(report)
    lower = comparison(compare_ledger(15, 15), text="Sleep was lower before ({{E1.a.mean}}).")
    assert any(e.token_class == "decrease_word" for e in lower.errors)


@pytest.mark.parametrize(("delta", "expected"), [(0.24, "no_clear_change"), (0.25, "increase"),
                                                 (-0.25, "decrease")])
def test_v9_min_delta_edges_sleep(delta: float, expected: str) -> None:
    ledger = compare_ledger(15, 15, mean_a=7.0, mean_b=7.0 + delta)
    text = "Average sleep was {{E1.b.mean}} later and {{E1.a.mean}} before."
    assert "DIRECTION_MISMATCH" not in codes(comparison(ledger, expected, text))
    wrong = "increase" if expected != "increase" else "no_clear_change"
    assert "DIRECTION_MISMATCH" in codes(comparison(ledger, wrong, text))


@pytest.mark.parametrize(("diff", "expected"), [(499.0, "no_clear_change"), (500.0, "increase")])
def test_v9_min_delta_edges_steps_split(diff: float, expected: str) -> None:
    ledger = s.make_ledger(s.t5_event("workouts.count", "steps.count", with_n=8, with_mean=7000.0 + diff,
                                      none_n=12, none_mean=7000.0))
    text = "Steps averaged {{E1.with_x.y_mean}} with workouts recorded and {{E1.x_not_recorded.y_mean}} otherwise."
    assert "DIRECTION_MISMATCH" not in codes(relationship(ledger, expected, text))


@pytest.mark.parametrize(("r", "expected"), [(0.29, "no_clear_change"), (0.30, "increase"), (-0.30, "decrease"),
                                             (-0.29, "no_clear_change")])
def test_v9_r_threshold(r: float, expected: str) -> None:
    ledger = s.make_ledger(s.t5_daily("sleep.hours", "steps.count", pairs=12, r=r))
    text = "Sleep and steps: r {{E1.pearson_r}}."
    assert relationship(ledger, expected, text).errors == ()
    assert "DIRECTION_MISMATCH" in codes(relationship(ledger, "decrease" if expected == "increase" else "increase",
                                                      text))


def test_v9_no_clear_change_rejects_both_word_sets_and_kinds_need_right_direction() -> None:
    ledger = s.make_ledger(s.t5_daily("sleep.hours", "steps.count", pairs=12, r=0.1))
    report = relationship(ledger, "no_clear_change", "Steps were higher after more sleep, r {{E1.pearson_r}}.")
    assert {"increase_word"} <= {e.token_class for e in report.errors if e.code == "DIRECTION_MISMATCH"}
    assert "DIRECTION_MISMATCH" in codes(one(direction="increase"))                      # value kind
    assert "DIRECTION_MISMATCH" in codes(comparison(compare_ledger(15, 15), "not_applicable"))


# --- V10 causal ---

@pytest.mark.parametrize("phrase", ["because", "caused", "due to", "leads  to", "results in", "makes you",
                                    "improves", "thanks to", "therefore", "will", "Impact of", "boosts"])
def test_v10_causal_language(phrase: str) -> None:
    report = one(f"Average sleep was {{{{E2.mean}}}} {phrase} workouts.")
    assert any(e.code == "CAUSAL_LANGUAGE" and e.token_class == "causal_word" for e in report.errors)


def test_v10_whole_words_only_and_summary_limitations() -> None:
    assert "CAUSAL_LANGUAGE" not in codes(one("Average sleep near the willow was {{E2.mean}}."))
    report = run(s.answer(summary_text="Sleep drives steps.",
                          limitations=[{"text": "Missing days cause gaps.", "evidence_ids": []}]))
    assert {"summary", "limitation"} <= {e.location for e in report.errors if e.code == "CAUSAL_LANGUAGE"}


# --- V11 kind/tool and metric ---

def test_v11_kind_tool_and_metric() -> None:
    assert "KIND_TOOL_MISMATCH" in codes(one(kind="trend", direction="no_clear_change"))     # trend from T2
    assert "KIND_TOOL_MISMATCH" in codes(one("Lunch {{E6.records.101.date}}.", evidence_ids=["E6"]))
    assert "METRIC_MISMATCH" in codes(one(metric="steps.count"))
    assert "METRIC_MISMATCH" in codes(one(metric=None))
    assert "METRIC_MISMATCH" in codes(one("Lunch {{E6.records.101.date}}.", kind="records", evidence_ids=["E6"],
                                          metric="sleep.hours"))
    with_t1 = one("Average sleep was {{E2.mean}} across {{E1.domains.sleep.coverage}}.", evidence_ids=["E2", "E1"])
    assert with_t1.errors == ()
    x_metric = one(STEPS_SPLIT, kind="relationship", evidence_ids=["E5"], metric="workouts.count",
                   direction="increase")
    assert x_metric.errors == ()


# --- V12 safe text and topic ---

@pytest.mark.parametrize(("text", "token"), [
    ("See https://example.com for {{E2.mean}}.", "url"),
    ("Visit www.example.org, average {{E2.mean}}.", "url"),
    ("Average {{E2.mean}} ftp://host.", "url"),
    ("Average {{E2.mean}} javascript:alert.", "url"),
    ("Average <b>{{E2.mean}}</b>.", "html"),
    ("Average <script>x</script> {{E2.mean}}.", "html"),
    ("Average [here](somewhere) {{E2.mean}}.", "markdown_link"),
    ("Average {{E2.mean}}.\x07", "control_char"),
    ("Average​ {{E2.mean}}.", "control_char"),
    ("Average {{E2.mean}}.\nNext line.", "control_char"),
])
def test_v12_markup_urls_and_control_chars(text: str, token: str) -> None:
    report = one(text)
    assert any(e.code == "MARKUP_OR_URL" and e.token_class == token for e in report.errors)


@pytest.mark.parametrize(("topic", "valid"), [
    ("sleep", True), ("sleep-quality trends", True), ("  sleep   and steps ", True),
    ("sleep & steps", False), ("sleep.", False), ("", False), ("http", False), ("sleep in Sept", True),
])
def test_v12_topic(topic: str, valid: bool) -> None:
    report = run(s.answer(topic=topic))
    if valid:
        assert report.topic == " ".join(topic.split()) and not codes(report, "topic")
    else:
        assert report.topic == FALLBACK_TOPIC and codes(report, "topic")
        assert not report.blocking  # D-1: topic issues are droppable


def test_v12_follow_up_markup_is_dropped_not_blocking() -> None:
    report = run(s.answer(follow_ups=["<img src=x>", "Is {{E2.mean}} typical?", "How did my steps change?"]))
    assert report.follow_up_ok == (False, False, True) and not report.blocking


# --- V13 status ---

def test_v13_status_consistency() -> None:
    report = run(s.answer(summary_text=NEUTRAL, findings=[]))
    assert codes(report) == {"STATUS_INCONSISTENT"} and report.blocking
    partial = run(s.answer(status="partial"))
    assert any(e.token_class == "no_limitations" for e in partial.errors)
    ok = run(s.answer(status="partial", limitations=[{"text": "Steps were not analysed.", "evidence_ids": []}]))
    assert ok.errors == () and decide(ok, repair_used=False).status == "partial"
    insufficient = run(s.answer(status="insufficient", findings=[]))
    assert "STATUS_INCONSISTENT" not in codes(insufficient)


# --- V14 charts ---

@pytest.mark.parametrize(("chart_type", "evidence_id", "ok"), [
    ("line", "E4", True), ("line", "E2", False), ("bar", "E7", True), ("bar", "E2", False),
    ("comparison", "E3", True), ("comparison", "E5", True), ("comparison", "E2", False), ("line", "E6", False),
])
def test_v14_chart_compatibility(chart_type: str, evidence_id: str, ok: bool) -> None:
    report = run(s.answer(charts=[{"type": chart_type, "evidence_id": evidence_id}]))
    assert report.chart_ok == (ok,)
    if not ok:
        assert codes(report) == {"CHART_INCOMPATIBLE"} and not report.blocking
        assert decide(report, repair_used=False).action == "accept"


def test_v14_daily_relation_unknown_evidence_and_duplicate() -> None:
    ledger = s.make_ledger(*s.standard_results(), s.t5_daily("sleep.hours", "steps.count", pairs=12, r=0.4))
    report = run(s.answer(charts=[{"type": "comparison", "evidence_id": "E8"}]), ledger)
    assert report.chart_ok == (False,)
    unknown = run(s.answer(charts=[{"type": "line", "evidence_id": "E8"}]))
    assert codes(unknown) == {"UNKNOWN_EVIDENCE"} and not unknown.blocking
    dup = run(s.answer(charts=[{"type": "line", "evidence_id": "E4"}, {"type": "line", "evidence_id": "E4"}]))
    assert dup.chart_ok == (True, False)


# --- decide() and repair message ---

def test_decide_repair_then_degrade_then_fallback() -> None:
    bad = s.finding("You slept 9 hours because of workouts.")
    mixed = run(s.answer(findings=[s.finding(), bad]))
    assert mixed.finding_ok == (True, False) and mixed.blocking
    first = decide(mixed, repair_used=False)
    assert first.action == "repair" and first.repair_message is not None
    second = decide(mixed, repair_used=True)
    assert (second.action, second.status, second.extra_limitation) == ("degrade", "partial", DEGRADE_LIMITATION)
    assert DEGRADE_LIMITATION == "Some parts of the analysis could not be verified and were omitted."
    only_bad = run(s.answer(findings=[bad]))
    final = decide(only_bad, repair_used=True)
    assert (final.action, final.fallback_reason) == ("fallback", "no_valid_finding")


def test_decide_insufficient_falls_back_immediately() -> None:
    report = run(s.answer(status="insufficient", findings=[s.finding("Sleep was 7 hours.")]))
    decision = decide(report, repair_used=False)
    assert (decision.action, decision.status, decision.fallback_reason) == ("fallback", "insufficient",
                                                                            "insufficient")


def test_decide_unparsable() -> None:
    report = validate("{", s.standard_ledger(), s.REGISTRY)
    assert decide(report, repair_used=False).action == "repair"
    decision = decide(report, repair_used=True)
    assert (decision.action, decision.fallback_reason) == ("fallback", "unparsable")


def test_repair_message_lists_codes_without_echoing_text() -> None:
    secret = "Zanzibar"
    report = run(s.answer(summary_text=f"{secret} seventy because https://x.example",
                          findings=[s.finding(f"{secret} slept 9 hours on {{{{E3.b.mean}}}}.")]))
    message = repair_message(report)
    assert "FREE_NUMBER at findings[0] (digit)" in message
    assert "PLACEHOLDER_NOT_CITED at findings[0] (evidence_id)" in message
    assert "CAUSAL_LANGUAGE at summary (causal_word)" in message
    for leaked in (secret, "seventy", "because", "https", "x.example", "9 hours", "E3.b.mean"):
        assert leaked not in message


# --- ADR-005 §10.2 adversarial final answers ---

def _adversarial_cases() -> list[tuple[str, dict[str, Any], str]]:
    return [
        ("literal wrong number", s.answer(findings=[s.finding("Average sleep was 9.5 h.")]), "FREE_NUMBER"),
        ("number word", s.answer(findings=[s.finding("Average sleep was nine hours.")]), "FREE_NUMBER"),
        ("invented evidence id", s.answer(findings=[s.finding(evidence_ids=["E2", "E8"])]), "UNKNOWN_EVIDENCE"),
        ("invented record id", s.answer(findings=[s.finding("Lunch logged.", kind="records", evidence_ids=["E6"],
                                                            metric=None, record_refs=["E6#4242"])]),
         "UNKNOWN_RECORD"),
        ("uncited placeholder", s.answer(findings=[s.finding("Later sleep was {{E3.b.mean}}.")]),
         "PLACEHOLDER_NOT_CITED"),
        ("causal", s.answer(findings=[s.finding("Workouts improve sleep, so it will rise: {{E2.mean}}.")]),
         "CAUSAL_LANGUAGE"),
        ("never on undocumented days", s.answer(findings=[s.finding("You never slept under {{E2.mean}}.")]),
         "ABSOLUTE_LANGUAGE"),
        ("didn't work out", s.answer(findings=[s.finding("You didn't work out; sleep was {{E2.mean}}.")]),
         "ABSENCE_CLAIM"),
        ("trend from four points", {}, "INSUFFICIENT_N"),
        ("non-comparable comparison", {}, "NOT_COMPARABLE"),
        ("direction contradicting sign", s.answer(findings=[s.finding(
            "Sleep fell from {{E3.a.mean}} to {{E3.b.mean}}.", kind="comparison", evidence_ids=["E3"],
            direction="decrease")]), "DIRECTION_MISMATCH"),
        ("url", s.answer(findings=[s.finding("Average {{E2.mean}}, see http://evil.example.")]), "MARKUP_OR_URL"),
        ("html", s.answer(findings=[s.finding("Average <img src=x onerror=y> {{E2.mean}}.")]), "MARKUP_OR_URL"),
        ("injected record text", s.answer(findings=[s.finding(
            "You lost {{E6.records.101.untrusted_text.description}}.", kind="records", evidence_ids=["E6"],
            metric=None)]), "UNKNOWN_PLACEHOLDER"),
        ("answered with zero findings", s.answer(findings=[]), "STATUS_INCONSISTENT"),
    ]


@pytest.mark.parametrize(("name", "data", "code"), _adversarial_cases(), ids=[c[0] for c in _adversarial_cases()])
def test_adversarial_answers_are_rejected(name: str, data: dict[str, Any], code: str) -> None:
    ledger = s.standard_ledger()
    if name == "trend from four points":
        ledger = series(30, (0, 9, 19, 29))
        data = s.answer(summary_text="Sleep trend.", findings=[s.finding(
            "Sleep rose by {{E1.trend.change_over_period}}.", kind="trend", evidence_ids=["E1"],
            direction="increase")])
    elif name == "non-comparable comparison":
        ledger = compare_ledger(6, 18)
        data = s.answer(summary_text="Sleep comparison.", findings=[s.finding(
            "Sleep was higher later: {{E1.b.mean}} vs {{E1.a.mean}}.", kind="comparison", evidence_ids=["E1"],
            direction="increase")])
    report = run(copy.deepcopy(data), ledger)
    assert code in codes(report)
    assert report.blocking
    decision = decide(report, repair_used=True)
    assert decision.action in ("degrade", "fallback")


def test_adversarial_wrong_chart_evidence_is_dropped_and_extra_field_rejected() -> None:
    dropped = run(s.answer(charts=[{"type": "line", "evidence_id": "E6"}]))
    assert dropped.chart_ok == (False,) and decide(dropped, repair_used=False).action == "accept"
    rejected = run(s.answer(charts=[{"type": "bar", "evidence_id": "E7", "values": [10, 20]}]))
    assert rejected.answer is None and "SCHEMA" in codes(rejected)


# --- D-12 regressions (Verifier B1-B3) ---

SLEEP_COMPARISON = s.finding("Average sleep was higher later by {{E3.difference.mean_abs}} ({{E3.b.mean}}).",
                             kind="comparison", evidence_ids=["E3"], direction="increase")


def tokens(report: ValidationReport, code: str, location: str | None = None) -> set[str | None]:
    return {e.token_class for e in report.errors if e.code == code and (location is None or e.location == location)}


def test_b1_summary_sign_inverted_delta_rejected() -> None:
    report = run(s.answer(summary_text="Your sleep decreased by {{E3.difference.mean_abs}}.",
                          findings=[s.finding(), SLEEP_COMPARISON]))
    assert report.finding_ok == (True, True)
    assert "decrease_word" in tokens(report, "DIRECTION_MISMATCH", "summary") and not report.summary_ok
    ok = run(s.answer(summary_text="Your sleep increased by {{E3.difference.mean_abs}}.",
                      findings=[s.finding(), SLEEP_COMPARISON]))
    assert ok.errors == ()


def test_b1_summary_delta_needs_valid_finding_of_matching_kind() -> None:
    value_only = run(s.answer(summary_text="Your sleep increased by {{E3.difference.mean_abs}}.",
                              findings=[s.finding("Later sleep was {{E3.b.mean}}.", evidence_ids=["E3"])]))
    assert "delta_leaf" in tokens(value_only, "KIND_TOOL_MISMATCH", "summary")
    invalid_cmp = s.finding("Sleep was higher later ({{E3.b.mean}}) because of workouts.", kind="comparison",
                            evidence_ids=["E3"], direction="increase")
    report = run(s.answer(summary_text="Your sleep increased by {{E3.difference.mean_abs}}.",
                          findings=[invalid_cmp]))
    assert "delta_leaf" in tokens(report, "KIND_TOOL_MISMATCH", "summary")


def test_b1_value_finding_with_t3_delta_rejected() -> None:
    report = one("Sleep fell by {{E3.difference.mean_abs}}.", evidence_ids=["E3"])
    assert "delta_leaf" in tokens(report, "KIND_TOOL_MISMATCH")
    assert "decrease_word" in tokens(report, "DIRECTION_MISMATCH")


def test_b1_value_finding_with_t4_slope_rejected() -> None:
    report = one("Sleep is dropping by {{E4.trend.slope_per_week}}.", evidence_ids=["E4"])
    assert "delta_leaf" in tokens(report, "KIND_TOOL_MISMATCH")


def test_b1_limitation_with_delta_rejected() -> None:
    report = run(s.answer(limitations=[{"text": "Sleep fell by {{E3.difference.mean_abs}}.", "evidence_ids": ["E3"]}]))
    assert "delta_leaf" in tokens(report, "KIND_TOOL_MISMATCH", "limitation")
    assert "decrease_word" in tokens(report, "DIRECTION_MISMATCH", "limitation")
    assert report.limitation_ok == (False,) and report.blocking


def test_b1_mixed_directions_in_one_text() -> None:
    # E3: mean_abs +0.6 h (increase) but count_abs -1 (decrease)
    report = one("Sleep was {{E3.difference.mean_abs}} longer over {{E3.difference.count_abs}} nights.",
                 kind="comparison", evidence_ids=["E3"], direction="increase")
    assert "mixed_direction" in tokens(report, "DIRECTION_MISMATCH")


def test_b1_summary_words_without_placeholder_vs_directional_findings() -> None:
    bad = run(s.answer(summary_text="Your sleep decreased.", findings=[s.finding(), SLEEP_COMPARISON]))
    assert "summary_direction" in tokens(bad, "DIRECTION_MISMATCH", "summary")
    good = run(s.answer(summary_text="Your sleep increased.", findings=[s.finding(), SLEEP_COMPARISON]))
    assert good.errors == ()


def test_b1_weight_change_sign_checked_in_value_finding() -> None:
    ledger = s.make_ledger(s.t2("weight.kg", n=3, documented=3, mean=78.0, first=78.4, last=77.6))
    wrong = one("Weight rose by {{E1.change}}.", ledger, evidence_ids=["E1"], metric="weight.kg")
    assert "increase_word" in tokens(wrong, "DIRECTION_MISMATCH")
    right = one("Weight fell by {{E1.change}}.", ledger, evidence_ids=["E1"], metric="weight.kg")
    assert right.errors == ()


def test_b2_value_finding_cannot_bypass_comparison_gates() -> None:
    ledger = compare_ledger(4, 14, total=15)  # A coverage 4/15 = 0.27: not comparable
    delta = one("Sleep was {{E1.difference.mean_abs}} higher recently.", ledger, evidence_ids=["E1"])
    assert "delta_leaf" in tokens(delta, "KIND_TOOL_MISMATCH")
    side_b = one("Recent sleep averaged {{E1.b.mean}}.", ledger, evidence_ids=["E1"])
    assert side_b.errors == ()  # (d): a/b leaves allowed; V5 on the side used (14 days)
    thin = compare_ledger(2, 14, total=15)
    side_a = one("Earlier sleep averaged {{E1.a.mean}}.", thin, evidence_ids=["E1"])
    assert "INSUFFICIENT_N" in codes(side_a)


def test_b2_value_finding_cannot_bypass_trend_gates() -> None:
    sparse = series(30, range(0, 30, 4))  # 8 documented of 30
    report = one("Sleep rose by {{E1.trend.change_over_period}}.", sparse, evidence_ids=["E1"])
    assert "delta_leaf" in tokens(report, "KIND_TOOL_MISMATCH")
    meta = one("Sleep was recorded on {{E1.coverage}} in {{E1.period}}.", sparse, evidence_ids=["E1"])
    assert meta.errors == ()


def test_b3_limitation_metric_value_rejected() -> None:
    ledger = s.make_ledger(s.t1({"sleep": (27, 30)}), s.t2("sleep.hours", n=1, documented=1, mean=5.0),
                           s.t2("sleep.hours", n=27, documented=27, mean=7.2))
    report = run(s.answer(summary_text=NEUTRAL, findings=[s.finding("Average sleep was {{E3.mean}}.",
                                                                    evidence_ids=["E3"])],
                          limitations=[{"text": "Last week you averaged only {{E2.mean}}.", "evidence_ids": ["E2"]}]),
                 ledger)
    assert "limitation_value" in tokens(report, "KIND_TOOL_MISMATCH", "limitation")
    ratio = run(s.answer(limitations=[{"text": "Coverage was {{E1.domains.sleep.coverage.ratio}}.",
                                       "evidence_ids": ["E1"]}]))
    assert "limitation_value" in tokens(ratio, "KIND_TOOL_MISMATCH", "limitation")
    ok = run(s.answer(limitations=[{"text": "Sleep has {{E1.domains.sleep.coverage}} in {{E1.domains.sleep.period}}.",
                                    "evidence_ids": ["E1"]}]))
    assert ok.errors == ()


@pytest.mark.parametrize(("text", "code"), [
    ("You never slept well.", "ABSOLUTE_LANGUAGE"),
    ("You always logged late.", "ABSOLUTE_LANGUAGE"),
    ("You skipped logging on some days.", "ABSENCE_CLAIM"),
])
def test_b3_limitation_absolutes_and_absence(text: str, code: str) -> None:
    report = run(s.answer(limitations=[{"text": text, "evidence_ids": []}]))
    assert code in codes(report, "limitation") and report.limitation_ok == (False,)


def test_b3_limitation_absolute_allowed_at_full_coverage_of_cited_evidence() -> None:
    ledger = s.make_ledger(s.t1({"sleep": (30, 30)}), s.t2("sleep.hours", n=30, documented=30, mean=7.0))
    report = run(s.answer(limitations=[{"text": "Sleep was logged every day.", "evidence_ids": ["E2"]}]), ledger)
    assert "ABSOLUTE_LANGUAGE" not in codes(report)


@pytest.mark.parametrize(("text", "token"), [
    ("Sleep {{E2.mean}} doubled.", "number_word"),
    ("Sleep {{E2.mean}}, halved later.", "number_word"),
    ("Sleep {{E2.mean}}; steps went threefold.", "number_word"),
    ("Sleep {{E2.mean}} across thousands of minutes.", "number_word"),
    ("Sleep {{E2.mean}} in week IV.", "roman_numeral"),
    ("Sleep {{E2.mean}} in week XII.", "roman_numeral"),
])
def test_e_inflected_number_words_and_roman_numerals(text: str, token: str) -> None:
    assert token in tokens(one(text), "FREE_NUMBER")


R1_SLEEP_TEXTS = [
    "Sleep was decreasing by {{E3.difference.mean_abs}}.",
    "Sleep is dropping by {{E3.difference.mean_abs}}.",
    "Sleep is falling by {{E3.difference.mean_abs}}.",
    "Sleep is declining by {{E3.difference.mean_abs}}.",
    "Sleep shrank by {{E3.difference.mean_abs}}.",
    "Sleep slipped by {{E3.difference.mean_abs}}.",
    "Sleep reduced by {{E3.difference.mean_abs}}.",
    "Sleep worsened by {{E3.difference.mean_abs}}.",
    "Sleep got shorter by {{E3.difference.mean_abs}}.",
    "You lost {{E3.difference.mean_abs}} of sleep.",
]


@pytest.mark.parametrize("text", R1_SLEEP_TEXTS)
def test_r1_decrease_stems_against_increasing_comparison(text: str) -> None:
    as_finding = one(text, kind="comparison", evidence_ids=["E3"], direction="increase")
    assert "DIRECTION_MISMATCH" in codes(as_finding)
    as_summary = run(s.answer(summary_text=text, findings=[s.finding(), SLEEP_COMPARISON]))
    assert "DIRECTION_MISMATCH" in codes(as_summary, "summary") and not as_summary.summary_ok


@pytest.mark.parametrize("text", ["You lost {{E1.change}}.", "You shed {{E1.change}}.",
                                  "Your weight is decreasing: {{E1.change}}."])
def test_r1_weight_gain_described_as_loss(text: str) -> None:
    ledger = s.make_ledger(s.t2("weight.kg", n=3, documented=3, mean=76.8, first=76.0, last=77.5))
    assert "DIRECTION_MISMATCH" in codes(one(text, ledger, evidence_ids=["E1"], metric="weight.kg"))
    gained = one("You gained {{E1.change}}.", ledger, evidence_ids=["E1"], metric="weight.kg")
    assert gained.errors == ()


def test_r1_no_clear_change_comparison_rejects_any_direction_word() -> None:
    ledger = compare_ledger(15, 15, mean_a=7.0, mean_b=7.1)
    climbed = comparison(ledger, "no_clear_change", "Sleep climbed by {{E1.difference.mean_abs}}.")
    assert "DIRECTION_MISMATCH" in codes(climbed)
    neutral = comparison(ledger, "no_clear_change", "Sleep changed by {{E1.difference.mean_abs}}.")
    assert neutral.errors == ()


@pytest.mark.parametrize("text", ["Sleep decreased to {{E2.mean}}.", "Sleep improved to {{E2.mean}}.",
                                  "Sleep was higher at {{E2.mean}}."])
def test_r2_value_finding_direction_words_need_a_signed_placeholder(text: str) -> None:
    assert "unsupported_direction" in tokens(one(text), "DIRECTION_MISMATCH")


def test_r2_limitation_direction_words_need_a_signed_placeholder() -> None:
    report = run(s.answer(limitations=[{"text": "Sleep improved recently.", "evidence_ids": []}]))
    assert "unsupported_direction" in tokens(report, "DIRECTION_MISMATCH", "limitation")


@pytest.mark.parametrize("summary_text", ["Your sleep dropped this month.", "Your sleep decreased to {{E2.mean}}."])
def test_r2_summary_words_with_only_a_value_finding(summary_text: str) -> None:
    report = run(s.answer(summary_text=summary_text))
    assert "summary_direction" in tokens(report, "DIRECTION_MISMATCH", "summary")


def test_r2_summary_words_next_to_no_clear_change_comparison() -> None:
    ledger = s.make_ledger(*s.standard_results(), s.t3(
        "steps.count", s.summary("steps.count", n=14, documented=14, total=15, mean=8000.0),
        s.summary("steps.count", n=14, documented=14, total=15, mean=8100.0, start="2026-09-16")))
    flat = s.finding("Steps changed by {{E8.difference.mean_abs}}.", kind="comparison", evidence_ids=["E8"],
                     metric="steps.count", direction="no_clear_change")
    report = run(s.answer(summary_text="Sleep increased.", findings=[SLEEP_COMPARISON, flat]), ledger)
    assert report.finding_ok == (True, True)
    assert "summary_direction" in tokens(report, "DIRECTION_MISMATCH", "summary")


def test_r2_mixed_word_sets_in_one_text() -> None:
    report = run(s.answer(summary_text="Sleep rose while steps fell.", findings=[s.finding(), SLEEP_COMPARISON]))
    assert "mixed_direction" in tokens(report, "DIRECTION_MISMATCH", "summary")


def test_e_single_letters_and_ordinary_words_pass() -> None:
    assert "FREE_NUMBER" not in codes(one("I think average sleep was {{E2.mean}}, a vivid result."))
