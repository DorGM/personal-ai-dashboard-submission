from __future__ import annotations

import json
from typing import Any, get_args

import pydantic
import pytest

from app.analytics.ports import METRIC_IDS
from app.analytics.schemas import (
    FINAL_ANSWER_JSON_SCHEMA,
    FINAL_ANSWER_SCHEMA,
    Chart,
    FinalAnswer,
    Finding,
    Limitation,
    MetricId,
    final_answer_text_format,
)
from tests.unit.analytics import evidence_samples as s


def parse(value: dict[str, Any]) -> FinalAnswer:
    return FinalAnswer.model_validate_json(json.dumps(value))


def test_valid_answer_parses() -> None:
    answer = parse(s.answer(charts=[{"type": "line", "evidence_id": "E4"}],
                            limitations=[{"text": "Some days are missing.", "evidence_ids": ["E1"]}]))
    assert answer.status == "answered" and answer.findings[0].metric == "sleep.hours"
    assert parse(s.answer(findings=[s.finding(metric=None)])).findings[0].metric is None


def test_metric_literal_equals_registry_ids() -> None:
    assert get_args(MetricId) == METRIC_IDS


def _mutate(path: list[Any], value: Any) -> dict[str, Any]:
    data = s.answer(charts=[{"type": "line", "evidence_id": "E4"}],
                    limitations=[{"text": "Gap.", "evidence_ids": []}])
    target: Any = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


@pytest.mark.parametrize(("path", "value"), [
    (["status"], "done"),
    (["status"], 1),
    (["findings", 0, "kind"], "story"),
    (["findings", 0, "direction"], "up"),
    (["findings", 0, "metric"], "mood.score"),
    (["findings", 0, "evidence_ids"], ["E9"]),
    (["findings", 0, "evidence_ids"], ["E0"]),
    (["findings", 0, "evidence_ids"], ["e2"]),
    (["findings", 0, "evidence_ids"], "E2"),
    (["findings", 0, "record_refs"], ["E6-101"]),
    (["findings", 0, "record_refs"], ["E6#"]),
    (["findings", 0, "extra"], "x"),
    (["charts", 0, "type"], "pie"),
    (["charts", 0, "data"], [1, 2, 3]),           # chart carries no data from the model
    (["charts", 0, "title"], "My chart"),
    (["limitations", 0, "severity"], "high"),
    (["unexpected"], True),
])
def test_schema_rejects_invalid_values_and_extra_fields(path: list[Any], value: Any) -> None:
    with pytest.raises(pydantic.ValidationError):
        parse(_mutate(path, value))


@pytest.mark.parametrize("missing", ["status", "topic", "summary", "findings", "limitations", "charts",
                                     "follow_up_questions"])
def test_every_top_level_field_is_required(missing: str) -> None:
    data = s.answer()
    del data[missing]
    with pytest.raises(pydantic.ValidationError):
        parse(data)


def test_finding_fields_required_including_nullable_metric() -> None:
    data = s.answer()
    del data["findings"][0]["metric"]
    with pytest.raises(pydantic.ValidationError):
        parse(data)


@pytest.mark.parametrize(("path", "limit"), [
    (["topic"], 60), (["summary"], 600), (["findings", 0, "text"], 400), (["limitations", 0, "text"], 300),
    (["follow_up_questions", 0], 120),
])
def test_string_length_caps(path: list[Any], limit: int) -> None:
    parse(_mutate(path, "a" * limit))
    with pytest.raises(pydantic.ValidationError):
        parse(_mutate(path, "a" * (limit + 1)))


@pytest.mark.parametrize(("key", "item", "cap"), [
    ("findings", s.finding(), 6),
    ("limitations", {"text": "Gap.", "evidence_ids": []}, 5),
    ("charts", {"type": "line", "evidence_id": "E4"}, 2),
    ("follow_up_questions", "Why?", 3),
])
def test_list_caps(key: str, item: Any, cap: int) -> None:
    data = s.answer()
    data[key] = [item] * cap
    parse(data)
    data[key] = [item] * (cap + 1)
    with pytest.raises(pydantic.ValidationError):
        parse(data)


# --- JSON schema for OpenAI agrees with the Pydantic models ---

def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in node:
        return _resolve(defs[node["$ref"].split("/")[-1]], defs)
    return node


def _enum(node: dict[str, Any], defs: dict[str, Any]) -> set[Any] | None:
    node = _resolve(node, defs)
    if "enum" in node:
        return set(node["enum"])
    if "anyOf" in node:
        values: set[Any] = set()
        for option in node["anyOf"]:
            option = _resolve(option, defs)
            if option.get("type") == "null":
                values.add(None)
            elif "enum" in option:
                values |= set(option["enum"])
            else:
                return None
        return values
    return None


def _compare(ours: dict[str, Any], model: type[pydantic.BaseModel]) -> None:
    pyd = model.model_json_schema()
    defs = pyd.get("$defs", {})
    assert ours["type"] == "object" and ours["additionalProperties"] is False
    assert list(ours["properties"]) == list(model.model_fields)
    assert sorted(ours["required"]) == sorted(pyd["required"]) == sorted(ours["properties"])
    for name, prop in ours["properties"].items():
        assert _enum(prop, {}) == _enum(pyd["properties"][name], defs), name
        if prop.get("enum") is not None and None in prop["enum"]:
            assert prop["type"] == ["string", "null"]


def test_json_schema_agrees_with_models() -> None:
    _compare(FINAL_ANSWER_SCHEMA, FinalAnswer)
    props = FINAL_ANSWER_SCHEMA["properties"]
    _compare(props["findings"]["items"], Finding)
    _compare(props["limitations"]["items"], Limitation)
    _compare(props["charts"]["items"], Chart)
    assert set(props["charts"]["items"]["properties"]) == {"type", "evidence_id"}
    assert props["follow_up_questions"]["items"]["type"] == "string"
    assert props["findings"]["items"]["properties"]["metric"]["enum"] == [*METRIC_IDS, None]


def test_every_object_is_closed_and_fully_required() -> None:
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert node["required"] == list(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(FINAL_ANSWER_SCHEMA)


def test_text_format_wrapper() -> None:
    fmt = final_answer_text_format()
    assert fmt == FINAL_ANSWER_JSON_SCHEMA
    assert (fmt["type"], fmt["name"], fmt["strict"]) == ("json_schema", "analysis_answer", True)
    fmt["schema"]["properties"].clear()
    assert FINAL_ANSWER_JSON_SCHEMA["schema"]["properties"]  # a deep copy
    json.dumps(FINAL_ANSWER_JSON_SCHEMA)  # serialisable
