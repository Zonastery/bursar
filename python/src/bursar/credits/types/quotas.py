"""Quota types — mirrors JS SDK's ``credits/types/quotas.ts``."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bursar.credits.types.catalog import (
    PlanAdmissionPolicy,
    PlanAllowancePolicy,
    PlanCreditPolicy,
)


class OperationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    billing_mode: Literal["strict", "overdraft"]
    max_concurrent: int | None = Field(default=None, strict=True, gt=0)
    overdraft_floor: Decimal | None = None

    @model_validator(mode="after")
    def validate_overdraft_floor(self) -> OperationPolicy:
        if self.overdraft_floor is not None and (not self.overdraft_floor.is_finite() or self.overdraft_floor > 0):
            raise ValueError("overdraft_floor must be finite and <= 0")
        return self


class CheckFeatureResult(BaseModel):
    user_id: str
    feature: str
    value: Any
    has_feature: bool


class SweepResult(BaseModel):
    expired_count: int
    expired_amount: Decimal
    dry_run: bool
    expired_by_bucket: dict[str, Decimal]


class QuotaState(BaseModel):
    user_id: str
    quota_key: str
    operation: str
    measure: str
    limit: Decimal
    consumed: Decimal
    reserved: Decimal
    remaining: Decimal
    overage: Decimal
    enforcement: Literal["block", "allow"]
    window_start: str
    window_end: str
    emit_at_percent: list[float]


class QuotaEvent(BaseModel):
    event_id: str
    quota_key: str
    operation: str
    measure: str
    event_type: Literal["threshold", "blocked"]
    threshold_percent: float | None
    idempotency_key: str
    usage_charge_id: str | None
    created_at: str


class ListQuotaEventsOptions(BaseModel):
    after: datetime | None = None
    after_id: str | None = None
    limit: int | None = Field(default=None, strict=True, ge=1, le=500)
    idempotency_key: str | None = None


class Entitlement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any


class GetUserPlanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    plan_id: str | None
    plan_key: str | None
    plan_label: str | None
    allowance: PlanAllowancePolicy | None
    entitlements: dict[str, Entitlement]
    rate_card: str | None
    credit_policy: PlanCreditPolicy | None
    admission: PlanAdmissionPolicy | None
    allowed_operations: list[str]
    plan_assigned_at: datetime | None
    plan_assignment_ends_at: datetime | None
    assignment_source_type: Literal["manual", "subscription", "migration", "system"] | None
    assignment_source_id: str | None
    catalog_revision_pinned: bool
    catalog_version: int | None
