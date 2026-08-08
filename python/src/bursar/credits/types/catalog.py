"""Catalog types — mirrors JS SDK's ``credits/types/catalog.ts``."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CatalogRevision(BaseModel):
    id: str
    config: dict[str, Any]
    version: int


class CatalogRevisionSummary(BaseModel):
    id: str
    version: int
    label: str | None
    active: bool
    created_at: str


class PlanAllowancePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal
    priority: int = Field(ge=0)
    reset_unit: Literal["second", "minute", "hour", "day", "week", "month", "year"]
    reset_count: int = Field(gt=0)
    reset_anchor: Literal["calendar", "plan_assignment", "rolling"]
    reset_timezone: str


class PlanCreditPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["prepaid", "credit_line"]
    credit_limit: Decimal | None


class _PlanAdmissionOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_in_flight: int | None = Field(default=None, gt=0)


class PlanAdmissionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_in_flight: int | None = Field(default=None, gt=0)
    operations: dict[str, _PlanAdmissionOperation]
