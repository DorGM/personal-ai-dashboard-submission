"""B-14 fixture: checksum and reproducibility, schema, content check (no personal data), and
the analytics demo patterns of the Owner's 30-day dataset spec (2026-10-18), recomputed by the
independent reference calculator (tests/support/reference.py) at the golden anchor 2026-10-17.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from statistics import fmean
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import pytest

from app import demo
from tests.support.reference import (
    ANCHOR,
    COMPARE_MIN_DAYS,
    CORRELATION_MIN_PAIRS,
    INTERVIEW_PREFIX,
    MIN_DELTA,
    MIN_OBSERVATIONS,
    MIN_R,
    SPLIT_MIN_DAYS,
    TREND_MIN_COVERAGE,
    TREND_MIN_POINTS,
    TREND_MIN_SPAN,
    WEIGHT_TREND_MIN_POINTS,
    Reference,
    load,
)

ROOT = Path(__file__).resolve().parents[2]


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_demo_fixture", ROOT / "scripts" / "build_demo_fixture.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def data() -> dict[str, Any]:
    return load()


@pytest.fixture(scope="module")
def ref(data: dict[str, Any]) -> Reference:
    return Reference(data, ANCHOR)


# ---------------------------------------------------------------- integrity and schema
def test_checksum_matches_the_committed_fixture() -> None:
    expected = demo.CHECKSUM_PATH.read_text(encoding="ascii").split()
    assert expected[1] == "synthetic_v1.json"
    assert hashlib.sha256(demo.FIXTURE_PATH.read_bytes()).hexdigest() == expected[0]


def test_generator_reproduces_the_committed_files_byte_for_byte() -> None:
    gen = _generator()
    raw = gen.render(gen.build())
    assert raw == demo.FIXTURE_PATH.read_bytes()
    assert gen.checksum_line(raw) == demo.CHECKSUM_PATH.read_bytes()
    assert gen.render(gen.build()) == raw  # deterministic within a run too


def test_fixture_validates_with_zero_errors_and_manifest_counts() -> None:
    fixture = demo.load_fixture()
    assert fixture.synthetic is True and fixture.approved_by_owner is False
    assert fixture.counts == demo.record_counts(fixture)
    assert set(fixture.counts) == {*demo.TABLES, "meal_calorie_estimates"}


def test_fixture_is_marked_synthetic_and_not_owner_approved(data: dict[str, Any]) -> None:
    assert data["synthetic"] is True and data["approved_by_owner"] is False
    assert "not yet approved by the Owner" in data["description"]


@pytest.mark.parametrize("change", ["extra_top", "extra_field", "wrong_count", "future_day", "too_old",
                                    "unknown_with_kcal", "approved_true"])
def test_schema_rejects_malformed_fixtures(data: dict[str, Any], change: str) -> None:
    bad = json.loads(json.dumps(data))
    if change == "extra_top":
        bad["mood"] = []
    elif change == "extra_field":
        bad["records"]["sleep_records"][0]["quality"] = "good"
    elif change == "wrong_count":
        bad["counts"]["sleep_records"] += 1
    elif change == "future_day":
        bad["records"]["sleep_records"][0]["day"] = 1
    elif change == "too_old":
        bad["records"]["weight_records"][0]["day"] = -30
    elif change == "unknown_with_kcal":
        bad["records"]["meals"][0]["calories"] = {"kcal": None, "basis": "user_provided", "notes": None}
        # schema accepts the shape; the repository model refuses it at import
        demo.Fixture.model_validate(bad)
        return
    elif change == "approved_true":
        bad["approved_by_owner"] = True
    with pytest.raises(ValueError):
        demo.Fixture.model_validate(bad)


# ---------------------------------------------------------------- content check (ADR-005 §7)
def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _allowed_host(host: str) -> bool:
    host = host.lower()
    return host.endswith((".example", ".test")) or any(
        host == base or host.endswith("." + base) for base in ("example.com", "example.org", "example.net"))


def test_urls_use_reserved_example_hosts_only(data: dict[str, Any]) -> None:
    text = "\n".join(_strings(data))
    urls = re.findall(r"[a-z][a-z0-9+.-]*://[^\s\"']+", text, re.IGNORECASE)
    assert urls, "the fixture should contain example links"
    assert [u for u in urls if not _allowed_host(urlsplit(u).hostname or "")] == []
    assert not re.search(r"\bwww\.", text, re.IGNORECASE)


def test_emails_only_on_invalid_or_example_domains(data: dict[str, Any]) -> None:
    emails = re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", "\n".join(_strings(data)))
    assert [d for d in emails if not (d.lower().endswith(".invalid") or _allowed_host(d))] == []


def test_no_phone_numbers_or_national_id_like_numbers(data: dict[str, Any]) -> None:
    text = "\n".join(_strings(data))
    assert not re.search(r"\b\d{9}\b", text)
    assert not re.search(r"\+?\d[\d ().-]{7,}\d", text)
    numbers = [v for v in _numbers(data) if isinstance(v, int) and v >= 100_000_000]
    assert numbers == []


def _numbers(value: Any) -> Iterator[Any]:
    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _numbers(v)
    elif isinstance(value, list):
        for v in value:
            yield from _numbers(v)


def test_no_allowlisted_email_appears(data: dict[str, Any]) -> None:
    configured = [e.strip().casefold() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()]
    text = "\n".join(_strings(data)).casefold()
    for email in configured:
        local = email.split("@")[0]
        assert email not in text
        assert len(local) < 4 or local not in text


def test_no_private_terms_appear(data: dict[str, Any]) -> None:
    terms_file = ROOT / "data" / "private_terms.txt"  # optional, gitignored, one term per line
    if not terms_file.is_file():
        pytest.skip("no data/private_terms.txt on this machine")
    text = "\n".join(_strings(data)).casefold()
    terms = [t.strip().casefold() for t in terms_file.read_text(encoding="utf-8").splitlines() if t.strip()]
    assert [t for t in terms if t in text] == []


def test_no_untracked_or_deferred_domains(data: dict[str, Any]) -> None:
    """I1: mood, heart rate and water are not tracked; deferred tables are absent."""
    assert set(data["records"]) == set(demo.TABLES)
    keys = " ".join(k for k in _strings(data["records"]) if k in {k2 for r in data["records"].values()
                                                                   for row in r for k2 in row}).casefold()
    for word in ("mood", "heart", "water", "job_report", "recommendation", "trend"):
        assert word not in keys


# ---------------------------------------------------------------- shape of the 30-day spec
def test_history_is_thirty_days_and_tasks_reach_two_weeks_ahead(data: dict[str, Any]) -> None:
    days = [r["day"] for table, rows in data["records"].items() if table != "tasks" for r in rows]
    assert min(days) == -29 and max(days) == 0
    task_days = [t["day"] for t in data["records"]["tasks"]]
    assert min(task_days) >= -29 and max(task_days) <= 14 and any(d > 7 for d in task_days)
    assert all(t["status"] == "pending" for t in data["records"]["tasks"] if t["day"] > 0)


def test_about_ten_percent_missing_per_daily_domain(ref: Reference) -> None:
    history = ref.period(-29, 0)
    for table in ("sleep_records", "step_records", "meals", "tasks"):
        missing = history.days - len(ref.documented(table, history))
        assert 2 <= missing <= 4, (table, missing)


def test_missing_days_are_never_stored_as_zero(data: dict[str, Any]) -> None:
    r = data["records"]
    assert all(s["hours_slept"] > 0 for s in r["sleep_records"])
    assert all(s["steps_count"] > 0 for s in r["step_records"])  # no explicit zero days in this dataset
    assert all(w["weight_kg"] > 0 for w in r["weight_records"])


def test_meals_are_two_or_three_per_logged_day_with_a_few_unknown(ref: Reference) -> None:
    per_day: dict[Any, int] = {}
    for d, _ in ref.rows("meals"):
        per_day[d] = per_day.get(d, 0) + 1
    assert set(per_day.values()) <= {2, 3}
    unknown = [r for _, r in ref.rows("meals") if r["calories"]["basis"] == "unknown"]
    assert 2 <= len(unknown) <= 6 and all(r["calories"]["kcal"] is None for r in unknown)
    known = [r["calories"]["kcal"] for _, r in ref.rows("meals") if r["calories"]["kcal"] is not None]
    assert all(250 <= k <= 700 for k in known)


def test_workouts_are_aerobic_and_weights_three_to_four_a_week(ref: Reference) -> None:
    history = ref.period(-29, 0)
    types = {r["workout_type"] for _, r in ref.rows("workouts")}
    assert {"aerobic", "weights"} <= types <= {"aerobic", "weights", "bodyweight"}
    assert 3 <= ref.per_week(ref.count("workouts", history), history) <= 4


def test_family_three_to_four_times_a_week(ref: Reference) -> None:
    for week in ref.weeks():
        assert 3 <= len(ref.tasks_where(week, category="family")) <= 4


def test_chores_and_pet_care_are_regular(ref: Reference) -> None:
    for week in ref.weeks():
        assert len(ref.tasks_where(week, category="housework")) >= 2
        assert len(ref.tasks_where(week, category="other", title_prefix="Walk the dog")) >= 5
    assert ref.tasks_where(ref.period(-29, 14), category="other", title_prefix="Vet appointment")


def test_interviews_match_the_job_applications(ref: Reference) -> None:
    interviews = ref.tasks_where(ref.period(-29, 14), category="career", title_prefix=INTERVIEW_PREFIX)
    apps = {r["company_name"]: (d, r) for d, r in ref.rows("job_applications")}
    held: dict[str, int] = {}
    for d, task in interviews:
        company = task["title"].removeprefix(INTERVIEW_PREFIX)
        assert company in apps, company
        applied_on, app = apps[company]
        assert applied_on < d
        assert task["status"] == ("completed" if d <= ANCHOR else "pending")
        assert app["status"] in {"interviewing", "accepted", "rejected"}
        if d <= ANCHOR:
            held[company] = held.get(company, 0) + 1
    for company, (_, app) in apps.items():
        assert app["interview_stage"] == held.get(company), company
        if company not in held:
            assert app["status"] in {"applied", "rejected"}
    assert {r["status"] for _, r in apps.values()} == {"applied", "interviewing", "rejected", "accepted"}
    assert sum(r["status"] == "accepted" for _, r in apps.values()) == 1
    # Pending upcoming interviews belong to open applications only.
    for d, task in interviews:
        if d > ANCHOR:
            assert apps[task["title"].removeprefix(INTERVIEW_PREFIX)][1]["status"] == "interviewing"


def test_no_weekday_patterns(ref: Reference) -> None:
    """Weekdays move with the anchor, so per-weekday means must not differ by min_delta or more."""
    for metric in ("sleep.hours", "steps.count"):
        by_weekday: dict[int, list[float]] = {}
        for d, v in ref.daily(metric).items():
            by_weekday.setdefault(d.weekday(), []).append(v)
        means = [fmean(v) for v in by_weekday.values()]
        assert max(means) - min(means) < MIN_DELTA[metric], metric
    per_weekday: dict[int, int] = {}
    for d, _ in ref.rows("workouts"):
        per_weekday[d.weekday()] = per_weekday.get(d.weekday(), 0) + 1
    assert len(per_weekday) == 7 and max(per_weekday.values()) - min(per_weekday.values()) <= 1


# ---------------------------------------------------------------- analytics demo patterns
def test_friends_twice_a_week_then_none(ref: Reference) -> None:
    w1, w2, w3, w4 = ref.weeks()
    assert [len(ref.tasks_where(w, category="friends")) for w in (w1, w2, w3, w4)] == [2, 2, 0, 0]
    assert not ref.tasks_where(ref.period(-15, 14), category="friends")
    assert all(t["status"] == "completed" for _, t in ref.tasks_where(ref.period(-29, 0), category="friends"))
    # "No friend meetings recorded" is an observation: task days stay documented in both halves.
    first, second = ref.period(-29, -16), ref.period(-15, 0)
    assert ref.coverage("tasks", first) >= 0.8 and ref.coverage("tasks", second) >= 0.8


def test_interviews_increase_week_by_week(ref: Reference) -> None:
    counts = [len(ref.tasks_where(w, category="career", title_prefix=INTERVIEW_PREFIX)) for w in ref.weeks()]
    assert all(a < b for a, b in zip(counts, counts[1:], strict=False)), counts
    assert counts[0] <= 1 and counts[-1] >= 3


def test_steps_are_higher_on_aerobic_days(ref: Reference) -> None:
    history = ref.period(-29, 0)
    with_aerobic, without = ref.split("steps.count", ref.event_days("workouts", workout_type="aerobic"), history)
    assert len(with_aerobic) >= SPLIT_MIN_DAYS and len(without) >= SPLIT_MIN_DAYS
    assert fmean(with_aerobic) - fmean(without) > MIN_DELTA["steps.count"]


def test_control_sleep_and_next_day_steps_are_unrelated(ref: Reference) -> None:
    pairs = ref.pairs("sleep.hours", "steps.count", ref.period(-29, 0), lag_days=1)
    r = ref.pearson(pairs)
    assert len(pairs) >= CORRELATION_MIN_PAIRS and r is not None and abs(r) < MIN_R


def test_control_weight_is_flat(ref: Reference) -> None:
    history = ref.period(-29, 0)
    s = ref.summarize("weight.kg", history)
    n, span, slope = ref.trend("weight.kg", history)
    assert n >= WEIGHT_TREND_MIN_POINTS and span >= 14 and slope is not None
    assert s.first is not None and s.last is not None
    assert abs(s.last[1] - s.first[1]) < MIN_DELTA["weight.kg"]
    assert abs(slope / 7 * span) < MIN_DELTA["weight.kg"]  # change over the period by the OLS line


def test_sleep_first_fifteen_days_vs_last_fifteen(ref: Reference) -> None:
    first, last = ref.summarize("sleep.hours", ref.period(-29, -15)), ref.summarize("sleep.hours", ref.period(-14, 0))
    assert ref.comparable(first, last)
    assert first.mean is not None and last.mean is not None
    assert first.mean - last.mean > MIN_DELTA["sleep.hours"]  # sleep dips in the interview-heavy weeks


# ---------------------------------------------------------------- insufficient-evidence cases
def test_movie_trend_has_too_few_points(ref: Reference) -> None:
    history = ref.period(-29, 0)
    documented = ref.documented("movies", history)
    assert len(documented) < TREND_MIN_POINTS and ref.coverage("movies", history) < TREND_MIN_COVERAGE
    assert 1 <= len(documented) <= 3


def test_a_specific_undocumented_day(ref: Reference) -> None:
    day = ref.on(-17)
    for table in ("sleep_records", "step_records", "meals"):
        assert day not in ref.documented(table, ref.period(-29, 0)), table


def test_short_or_sparse_trends_are_insufficient(ref: Reference) -> None:
    n, span, _ = ref.trend("sleep.hours", ref.period(-9, 0))  # under 14 days
    assert span < TREND_MIN_SPAN
    n, span, _ = ref.trend("weight.kg", ref.period(-6, 0))  # weight over the last week
    assert n < TREND_MIN_POINTS and n < WEIGHT_TREND_MIN_POINTS


def test_comparison_with_too_little_coverage_is_not_comparable(ref: Reference) -> None:
    w1, _, _, w4 = ref.weeks()
    a, b = ref.summarize("weight.kg", w1), ref.summarize("weight.kg", w4)
    assert a.documented_days < COMPARE_MIN_DAYS or b.documented_days < COMPARE_MIN_DAYS
    assert not ref.comparable(a, b)
    assert ref.summarize("weight.kg", ref.period(-3, 0)).documented_days < MIN_OBSERVATIONS
