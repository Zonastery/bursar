"""Catalog types — mirrors JS SDK's ``credits/types/catalog.ts``."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel


class BursarConfigResult(BaseModel):
    id: str
    config: dict[str, Any]
    version: int


class BursarConfigHistoryItem(BaseModel):
    id: str
    version: int
    label: str | None
    active: bool
    created_at: str


class PlanAllowancePolicy(BaseModel):
    amount: Decimal | None
    reset_unit: str | None
    reset_count: int | None
    reset_anchor: str | None
    reset_timezone: str | None


class PlanCreditPolicy(BaseModel):
    type: Literal["prepaid", "credit_line"]
    credit_limit: Decimal | None


class _PlanAdmissionOperation(BaseModel):
    max_in_flight: int | None


class PlanAdmissionPolicy(BaseModel):
    max_in_flight: int | None
    operations: dict[str, _PlanAdmissionOperation]
