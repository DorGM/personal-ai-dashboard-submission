"""Shared Pydantic field types and model base for record input schemas."""

from __future__ import annotations

from typing import Annotated, TypeVar

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, StringConstraints

from app.canonical import norm

T = TypeVar("T")


def _blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _visible(value: str) -> str:
    # Text that normalizes to nothing (only zero-width, direction or control characters) would
    # produce an empty identity key and look blank in lists, so it is refused.
    if not norm(value):
        raise ValueError("Enter visible text, not only spaces or invisible characters.")
    return value


class RecordIn(BaseModel):
    """Strict input schema: unknown fields rejected, strings trimmed, NaN/inf refused, immutable."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False, frozen=True)


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
                 AfterValidator(_visible)]
"""A required short text (title, name), trimmed, with visible characters."""
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
                     AfterValidator(_visible)]
"""A required free text (e.g. a meal description)."""
OptName = Annotated[
    Annotated[str, StringConstraints(strip_whitespace=True, max_length=200), AfterValidator(_visible)] | None,
    BeforeValidator(_blank_to_none),
]
"""An optional short text; blank input becomes None, invisible-only text is refused."""
OptLongText = Annotated[
    Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000), AfterValidator(_visible)] | None,
    BeforeValidator(_blank_to_none),
]
"""An optional free text; blank input becomes None, invisible-only text is refused."""
Opt = Annotated[T | None, BeforeValidator(_blank_to_none)]
"""An optional value of type T (number, enum, URL); an empty form value means "not given"."""
