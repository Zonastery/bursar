"""Provider-neutral usage input for the Bursar v1 pricing engine."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UsageMetrics(BaseModel):
    """One billable operation.

    ``operation`` selects the configured operation.  ``measures`` are
    non-negative numeric quantities used by price formulas; ``dimensions``
    select an ordered price rule.  This keeps provider model names, images,
    audio seconds and future AI workload attributes out of Bursar's schema.
    """

    model_config = ConfigDict(extra="forbid")

    operation: str
    measures: dict[str, Decimal] = Field(default_factory=dict)
    dimensions: dict[str, str | Decimal | bool] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("operation")
    @classmethod
    def validate_operation(cls, value: str) -> str:
        if not value:
            raise ValueError("operation must be non-empty")
        return value

    @field_validator("measures")
    @classmethod
    def validate_measures(cls, values: dict[str, Decimal]) -> dict[str, Decimal]:
        for key, value in values.items():
            if not key or not value.is_finite() or value < 0:
                raise ValueError("usage measures must have non-empty names and finite non-negative values")
        return values

    @field_validator("dimensions")
    @classmethod
    def validate_dimensions(cls, values: dict[str, str | Decimal | bool]) -> dict[str, str | Decimal | bool]:
        for key, value in values.items():
            if not key:
                raise ValueError("usage dimensions must have non-empty names")
            if isinstance(value, str) and not value:
                raise ValueError("string usage dimensions must be non-empty")
            if isinstance(value, Decimal) and not value.is_finite():
                raise ValueError("numeric usage dimensions must be finite")
        return values


METRIC_VARIABLES: frozenset[str] = frozenset()
