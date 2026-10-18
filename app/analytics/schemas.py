"""Final-answer schema of the analytics agent (ADR-005 §4.3).

Two views of the same contract:
- Pydantic models (strict, `extra="forbid"`) — the authoritative V1 parse with all caps.
- `FINAL_ANSWER_TEXT_FORMAT` — the OpenAI Responses `text.format` object (strict JSON schema).
  It is built from the same Literal types, uses only basic keywords (type, properties, required,
  additionalProperties, enum, items, description) and states the length/count caps in
  descriptions; the Pydantic parse enforces them. A unit test asserts both views agree.
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Status = Literal["answered", "partial", "insufficient"]
FindingKind = Literal["value", "comparison", "trend", "relationship", "records"]
Direction = Literal["increase", "decrease", "no_clear_change", "not_applicable"]
ChartType = Literal["line", "bar", "comparison"]
# Must equal app.analytics.ports.METRIC_IDS (asserted by a test).
MetricId = Literal[
    "sleep.hours", "steps.count", "weight.kg", "calories.daily_total", "meals.count", "workouts.count",
    "workouts.duration_minutes", "articles.count", "podcasts.count", "movies.count", "series.count",
    "podcasts.duration_minutes", "tasks.count", "tasks.completion_rate", "job_applications.count",
    "job_applications.progress_rate",
]

EvidenceId = Annotated[str, StringConstraints(pattern=r"^E[1-8]$")]
RecordRef = Annotated[str, StringConstraints(pattern=r"^E[1-8]#[0-9]+$")]

MAX_TOPIC = 60
MAX_SUMMARY = 600
MAX_FINDING_TEXT = 400
MAX_LIMITATION_TEXT = 300
MAX_FOLLOW_UP = 120
MAX_FINDINGS = 6
MAX_LIMITATIONS = 5
MAX_CHARTS = 2
MAX_FOLLOW_UPS = 3
MAX_EVIDENCE_IDS = 8
MAX_RECORD_REFS = 20

_STRICT = ConfigDict(extra="forbid", strict=True, frozen=True)


class Finding(BaseModel):
    """One claim. Numbers appear only as `{{E<n>.<path>}}` placeholders."""

    model_config = _STRICT
    kind: FindingKind
    text: str = Field(max_length=MAX_FINDING_TEXT)
    evidence_ids: list[EvidenceId] = Field(max_length=MAX_EVIDENCE_IDS)
    metric: MetricId | None
    direction: Direction
    record_refs: list[RecordRef] = Field(max_length=MAX_RECORD_REFS)


class Limitation(BaseModel):
    """A stated gap in the data, optionally citing evidence."""

    model_config = _STRICT
    text: str = Field(max_length=MAX_LIMITATION_TEXT)
    evidence_ids: list[EvidenceId] = Field(max_length=MAX_EVIDENCE_IDS)


class Chart(BaseModel):
    """A chart request: type and evidence id only — the model supplies no chart data."""

    model_config = _STRICT
    type: ChartType
    evidence_id: EvidenceId


class FinalAnswer(BaseModel):
    """The model's final structured answer (ADR-005 §4.3)."""

    model_config = _STRICT
    status: Status
    topic: str = Field(max_length=MAX_TOPIC)
    summary: str = Field(max_length=MAX_SUMMARY)
    findings: list[Finding] = Field(max_length=MAX_FINDINGS)
    limitations: list[Limitation] = Field(max_length=MAX_LIMITATIONS)
    charts: list[Chart] = Field(max_length=MAX_CHARTS)
    follow_up_questions: list[Annotated[str, Field(max_length=MAX_FOLLOW_UP)]] = Field(max_length=MAX_FOLLOW_UPS)


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def _str(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _enum(values: tuple[Any, ...], *, nullable: bool = False) -> dict[str, Any]:
    if nullable:
        return {"type": ["string", "null"], "enum": [*values, None]}
    return {"type": "string", "enum": list(values)}


def _array(items: dict[str, Any], description: str) -> dict[str, Any]:
    return {"type": "array", "items": items, "description": description}


_EVIDENCE_IDS = _array(_str("Evidence id E1…E8"), f"At most {MAX_EVIDENCE_IDS} evidence ids")

FINAL_ANSWER_SCHEMA: Final[dict[str, Any]] = _obj({
    "status": _enum(get_args(Status)),
    "topic": _str(f"Short topic, letters, spaces and hyphens only, at most {MAX_TOPIC} characters"),
    "summary": _str(f"At most {MAX_SUMMARY} characters; numbers only as {{{{E<n>.<path>}}}} placeholders"),
    "findings": _array(_obj({
        "kind": _enum(get_args(FindingKind)),
        "text": _str(f"At most {MAX_FINDING_TEXT} characters; numbers only as placeholders"),
        "evidence_ids": _EVIDENCE_IDS,
        "metric": _enum(get_args(MetricId), nullable=True),
        "direction": _enum(get_args(Direction)),
        "record_refs": _array(_str("Record reference E<n>#<record_id>"), f"At most {MAX_RECORD_REFS} refs"),
    }), f"At most {MAX_FINDINGS} findings"),
    "limitations": _array(_obj({
        "text": _str(f"At most {MAX_LIMITATION_TEXT} characters"),
        "evidence_ids": _EVIDENCE_IDS,
    }), f"At most {MAX_LIMITATIONS} limitations"),
    "charts": _array(_obj({
        "type": _enum(get_args(ChartType)),
        "evidence_id": _str("Evidence id E1…E8"),
    }), f"At most {MAX_CHARTS} charts"),
    "follow_up_questions": _array(_str(f"At most {MAX_FOLLOW_UP} characters, no numbers"),
                                  f"At most {MAX_FOLLOW_UPS} questions"),
})

FINAL_ANSWER_TEXT_FORMAT: Final[dict[str, Any]] = {
    "type": "json_schema", "name": "analysis_answer", "strict": True, "schema": FINAL_ANSWER_SCHEMA,
}
# Alias used by the task description / B-15.
FINAL_ANSWER_JSON_SCHEMA: Final = FINAL_ANSWER_TEXT_FORMAT


def final_answer_text_format() -> dict[str, Any]:
    """A fresh deep copy of the `text.format` object, safe to hand to an SDK that may mutate it."""
    return copy.deepcopy(FINAL_ANSWER_TEXT_FORMAT)
