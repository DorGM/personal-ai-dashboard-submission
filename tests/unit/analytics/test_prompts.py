"""Prompts and tool schemas (B-15): strict OpenAI function schemas agree with tools.ARG_MODELS."""

from __future__ import annotations

import json
import re
import types
from datetime import date
from typing import Any, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from app.analytics import prompts, tools
from app.analytics.ports import DOMAINS, METRIC_IDS
from tests.unit.analytics.fake_metrics import FAKE_REGISTRY

NO_FILTERS: dict[str, Any] = {"meal_type": None, "workout_type": None, "category": None, "status": None,
                              "work_model": None}
SAMPLES: dict[str, dict[str, Any]] = {
    "get_data_coverage": {"domains": ["sleep", "steps"], "start": "2026-09-01", "end": "2026-09-30"},
    "summarize_metric": {"metric": "sleep.hours", "start": "2026-09-01", "end": "2026-09-30", "group_by": "week",
                         "filters": None},
    "compare_periods": {"metric": "workouts.count", "period_a": {"start": "2026-08-01", "end": "2026-08-31"},
                        "period_b": {"start": "2026-09-01", "end": "2026-09-30"},
                        "filters": {**NO_FILTERS, "workout_type": "aerobic"}},
    "get_daily_series": {"metric": "steps.count", "start": "2026-09-01", "end": "2026-09-30"},
    "relate_metrics": {"x_metric": "workouts.count", "y_metric": "sleep.hours", "start": "2026-09-01",
                       "end": "2026-09-30", "lag_days": 1},
    "list_records": {"domain": "meals", "start": "2026-09-01", "end": "2026-09-30",
                     "filters": {**NO_FILTERS, "meal_type": "lunch"}, "sort": "date_desc", "limit": 10},
}


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    candidates = [annotation]
    if get_origin(annotation) in (Union, types.UnionType):
        candidates = list(get_args(annotation))
    for candidate in candidates:
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None


def _assert_strict(schema: dict[str, Any], model: type[BaseModel], where: str) -> None:
    assert set(schema["properties"]) == set(model.model_fields), where
    assert schema["required"] == list(schema["properties"]), where
    assert schema["additionalProperties"] is False, where
    for name, field in model.model_fields.items():
        prop = schema["properties"][name]
        nested = _nested_model(field.annotation)
        if nested is not None:
            _assert_strict(prop, nested, f"{where}.{name}")
            nullable = type(None) in get_args(field.annotation)
            assert (prop["type"] == ["object", "null"]) is nullable, f"{where}.{name}"


def _definitions() -> dict[str, dict[str, Any]]:
    return {d["name"]: d for d in prompts.tool_definitions()}


def test_one_strict_function_per_tool() -> None:
    defs = prompts.tool_definitions()
    assert [d["name"] for d in defs] == list(tools.TOOL_NAMES)
    for d in defs:
        assert d["type"] == "function" and d["strict"] is True and d["description"]
        assert d["parameters"]["type"] == "object"


@pytest.mark.parametrize("name", tools.TOOL_NAMES)
def test_schema_fields_match_argument_model(name: str) -> None:
    _assert_strict(_definitions()[name]["parameters"], tools.ARG_MODELS[name], name)


@pytest.mark.parametrize("name", tools.TOOL_NAMES)
def test_sample_arguments_validate(name: str) -> None:
    tools.ARG_MODELS[name].model_validate_json(json.dumps(SAMPLES[name]))


def _enum_paths(schema: dict[str, Any], path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], list[Any]]]:
    found: list[tuple[tuple[str, ...], list[Any]]] = []
    if "enum" in schema:
        found.append((path, schema["enum"]))
    for key, prop in schema.get("properties", {}).items():
        found += _enum_paths(prop, (*path, key))
    if "items" in schema:
        found += [(p, v) for p, v in _enum_paths(schema["items"], (*path, "[]"))]
    return found


def _with(sample: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(json.dumps(sample))
    node = data
    for i, key in enumerate(path[:-1]):
        if node.get(key) is None and path[i + 1] != "[]":
            node[key] = dict(NO_FILTERS)
        node = node[key]
    last = path[-1]
    if last == "[]":
        return data
    node[last] = value
    return data


@pytest.mark.parametrize("name", tools.TOOL_NAMES)
def test_every_enum_value_is_accepted_by_the_model(name: str) -> None:
    schema = _definitions()[name]["parameters"]
    for path, values in _enum_paths(schema):
        for value in values:
            if path[-1] == "[]":  # array of domains
                data = {**SAMPLES[name], path[0]: [value]}
            else:
                data = _with(SAMPLES[name], path, value)
            tools.ARG_MODELS[name].model_validate_json(json.dumps(data))


def test_enums_are_the_closed_sets() -> None:
    defs = _definitions()
    assert defs["summarize_metric"]["parameters"]["properties"]["metric"]["enum"] == list(METRIC_IDS)
    assert defs["list_records"]["parameters"]["properties"]["domain"]["enum"] == list(DOMAINS)
    assert defs["get_data_coverage"]["parameters"]["properties"]["domains"]["items"]["enum"] == list(DOMAINS)


def test_no_user_id_and_no_refs_anywhere() -> None:
    blob = json.dumps(prompts.tool_definitions())
    assert "user_id" not in blob and "$ref" not in blob and "$defs" not in blob


def test_tool_definitions_are_fresh_copies() -> None:
    first = prompts.tool_definitions()
    first[0]["parameters"]["properties"].clear()
    assert prompts.tool_definitions()[0]["parameters"]["properties"]


def test_system_prompt_rules() -> None:
    p = prompts.SYSTEM_PROMPT
    for phrase in ("never instructions", "untrusted_text", "{{E<n>.<path>}}", "never write digits",
                   "Correlation is not causation", "no workout recorded", "insufficient", "partial",
                   "period_a", "no markdown", "English", "sign-bearing placeholder", "with their sign",
                   "changed by {{E3.difference.mean_abs}}", "no_clear_change, use no direction word",
                   "Never describe a value finding as a change", "Limitations may only use count, date, period"):
        assert phrase in p, phrase
    assert prompts.ANALYST_PROMPT_VERSION


def test_context_lists_today_catalogue_and_ranges() -> None:
    text = prompts.build_context(date(2026, 10, 18), FAKE_REGISTRY,
                                 {"sleep": (date(2026, 9, 1), date(2026, 10, 17))})
    assert "2026-10-18" in text
    assert "- sleep: 2026-09-01 to 2026-10-17" in text and "- weight: no records" in text
    assert re.search(r"- workouts\.count \| event_count \| workouts \| Workouts \| workout_type \| workout_type=", text)
