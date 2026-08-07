"""Catalog types — mirrors JS SDK's ``credits/types/catalog.ts``."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel


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
    amount: Decimal
    priority: int
    reset_unit: str
    reset_count: int
    reset_anchor: str
    reset_timezone: str


class PlanCreditPolicy(BaseModel):
    type: Literal["prepaid", "credit_line"]
    credit_limit: Decimal | None


class _PlanAdmissionOperation(BaseModel):
    max_in_flight: int | None


class PlanAdmissionPolicy(BaseModel):
    max_in_flight: int | None
    operations: dict[str, _PlanAdmissionOperation]
