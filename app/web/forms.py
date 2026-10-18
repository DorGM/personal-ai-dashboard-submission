"""Form definitions for the record domains and form → Pydantic parsing (UI layer only).

The repositories own validation; this module only describes how each domain's fields are
rendered and turns submitted form text into the domain's input model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from types import ModuleType
from typing import Any

from pydantic import BaseModel, ValidationError

from app.records import DOMAINS
from app.records.job_applications import STATUSES, WORK_MODELS
from app.records.meals import MEAL_TYPES
from app.records.tasks import CATEGORIES
from app.records.tasks import STATUSES as TASK_STATUSES
from app.records.workouts import INTENSITIES, WORKOUT_TYPES


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str = "text"  # text | textarea | number | date | time | url | select
    required: bool = False
    options: tuple[str, ...] = ()
    step: str | None = None
    min: str | None = None
    max: str | None = None
    hint: str = ""


@dataclass(frozen=True)
class DomainView:
    domain: str
    tab: str
    title: str
    singular: str
    fields: tuple[Field, ...]
    module: ModuleType = field(repr=False)

    @property
    def date_field(self) -> str:
        return str(self.module.SPEC.date_column)

    @property
    def days_ahead(self) -> int:
        return int(self.module.SPEC.days_ahead)


def _date(label: str = "Date") -> Field:
    return Field("record_date", label, "date", required=True)


_FIELDS: dict[str, tuple[str, str, str, tuple[Field, ...]]] = {
    "weight": ("health", "Weight", "weight entry", (
        _date(), Field("weight_kg", "Weight (kg)", "number", True, step="0.1", min="0.1", max="499.9"))),
    "meals": ("health", "Meals", "meal", (
        _date(), Field("meal_type", "Meal", "select", True, MEAL_TYPES),
        Field("description", "What did you eat?", "textarea", True),
        Field("estimated_calories", "Calories (kcal, optional)", "number", step="1", min="0", max="10000",
              hint="Only if you know the value; it is saved as user-provided."))),
    "workouts": ("health", "Workouts", "workout", (
        _date(), Field("workout_type", "Type", "select", True, WORKOUT_TYPES),
        Field("intensity", "Intensity (stick spinning only)", "select", options=INTENSITIES),
        Field("duration_minutes", "Duration (minutes)", "number", step="1", min="1", max="1440"),
        Field("description", "Notes", "textarea"))),
    "sleep": ("health", "Sleep", "sleep entry", (
        _date("Wake-up date"), Field("hours_slept", "Hours slept", "number", True, step="0.25", min="0.25",
                                     max="24"))),
    "steps": ("health", "Steps", "steps entry", (
        _date(), Field("steps_count", "Steps", "number", True, step="1", min="0", max="1000000"))),
    "articles": ("content", "Articles", "article", (
        _date(), Field("title", "Title", required=True), Field("source", "Source"),
        Field("url", "Link", "url", hint="http:// or https:// (the link is not opened by the app)."))),
    "podcasts": ("content", "Podcasts", "podcast episode", (
        _date(), Field("podcast_name", "Podcast", required=True), Field("episode_title", "Episode"),
        Field("duration_minutes", "Duration (minutes)", "number", step="1", min="1", max="1440"))),
    "movies": ("content", "Movies", "movie", (
        _date(), Field("title", "Title", required=True),
        Field("release_year", "Release year", "number", step="1", min="1880", max="2100"))),
    "series": ("content", "Series", "series episode", (
        _date(), Field("series_name", "Series", required=True),
        Field("season_number", "Season", "number", step="1", min="1", max="10000"),
        Field("episode_number", "Episode", "number", step="1", min="1", max="10000"))),
    "job_applications": ("jobs", "Applications", "application", (
        Field("application_date", "Application date", "date", True),
        Field("company_name", "Company", required=True), Field("job_title", "Job title", required=True),
        Field("company_location", "Location", required=True),
        Field("work_model", "Work model", "select", True, WORK_MODELS),
        Field("industry", "Industry", required=True), Field("status", "Status", "select", True, STATUSES),
        Field("interview_stage", "Interview stage", "number", step="1", min="1", max="100"),
        Field("job_url", "Job link", "url"), Field("external_id", "Posting ID"))),
    "tasks": ("tasks", "Tasks", "task", (
        Field("task_date", "Date", "date", True), Field("start_time", "Start time", "time"),
        Field("title", "Title", required=True), Field("description", "Details", "textarea"),
        Field("category", "Category", "select", True, CATEGORIES),
        Field("status", "Status", "select", True, TASK_STATUSES))),
}

VIEWS: dict[str, DomainView] = {
    name: DomainView(name, tab, title, singular, fields, DOMAINS[name])
    for name, (tab, title, singular, fields) in _FIELDS.items()
}
TABS: tuple[tuple[str, str], ...] = (
    ("health", "Food & Health"), ("content", "Content"), ("jobs", "Jobs & Career"), ("tasks", "Tasks"),
    ("analyst", "Analyst"),
)
TAB_TITLES = dict(TABS)


def views_for(tab: str) -> list[DomainView]:
    return [v for v in VIEWS.values() if v.tab == tab]


def parse(view: DomainView, form: dict[str, str]) -> BaseModel:
    """Build the domain's input model from submitted text. Raises pydantic.ValidationError."""
    values = {f.name: form[f.name] for f in view.fields if f.name in form}  # absent → model default
    model: type[BaseModel] = view.module.SPEC.model
    return model.model_validate(values)


def error_messages(view: DomainView, exc: ValidationError) -> list[str]:
    """Readable "Label: problem" lines (no input values echoed)."""
    labels = {f.name: f.label for f in view.fields}
    out = []
    for err in exc.errors(include_input=False, include_url=False):
        loc = next((str(p) for p in err["loc"] if isinstance(p, str) and p in labels), None)
        msg = str(err["msg"]).removeprefix("Value error, ")
        out.append(f"{labels[loc]}: {msg}" if loc else msg)
    return out


def form_values(view: DomainView, row: Any) -> dict[str, str]:
    """Current values for an edit form (the optional kcal field starts empty)."""
    values = {}
    for f in view.fields:
        value = row[f.name] if f.name in row.keys() and f.name != "estimated_calories" else None
        values[f.name] = "" if value is None else str(value)
    return values


def date_limit(view: DomainView, today: date) -> str:
    """Latest date the domain accepts (today, or today + 14 for tasks), for the date input's `max`."""
    return (today + timedelta(days=view.days_ahead)).isoformat()
