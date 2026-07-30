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
    max_concurrent: int | None = Field(default=None, gt=0)
    overdraft_floor: Decimal | None = None

    @model_validator(mode="after")
    def validate_overdraft_floor(self) -> OperationPolicy:
        if self.overdraft_floor is not None and (not self.overdraft_floor.is_finite() or self.overdraft_floor > 0):
            raise ValueError("overdraft_floor must be finite and <= 0")
        return self


class FeatureLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any = None
    max_calls: int | None
    period: Literal["daily", "weekly", "monthly", "yearly"]
    on_exceed: Literal["deny", "warn", "notify"]


class CheckFeatureResult(BaseModel):
    user_id: str
    feature: str
    value: Any
    has_feature: bool


class SweepResult(BaseModel):
    expired_count: int
    expired_amount: Decimal
    dry_run: bool
    expired_by_bucket: dict[str, Decimal] | None = None


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
    limit: int | None = Field(default=None, ge=1, le=500)
    idempotency_key: str | None = None


class FeatureLimitResult(BaseModel):
    user_id: str
    feature: str
    limited: bool
    limit: int
    used: int
    remaining: int
    period_start: str
    period_end: str
    action: Literal["deny", "warn", "notify"] | None


class Entitlement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any | None = None
    max_calls: int | None = Field(default=None, ge=0)
    period: str = "monthly"
    on_exceed: str = "deny"

    @model_validator(mode="before")
    @classmethod
    def normalize_boolean_entitlement(cls, value: Any) -> Any:
        if isinstance(value, bool):
            return {"value": value}
        if isinstance(value, dict):
            normalized = dict(value)
            period = normalized.get("period")
            if isinstance(period, dict):
                unit = period.get("unit")
                count = period.get("count")
                if count == 1 and unit in {"day", "week", "month", "year"}:
                    period_map = {"day": "daily", "week": "weekly", "month": "monthly", "year": "yearly"}
                    normalized["period"] = period_map[unit]
            if "on_exceed" not in normalized and "action" in normalized:
                normalized["on_exceed"] = normalized["action"]
            normalized.pop("action", None)
            return normalized
        return value


class Allowance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Decimal = Field(ge=0)
    period: Literal["calendar_month", "rolling_30d", "anniversary"]

    @model_validator(mode="after")
    def validate_amount(self) -> Allowance:
        if not self.amount.is_finite():
            raise ValueError("allowance amount must be finite")
        return self


class PlanSafety(BaseModel):
    model_config = ConfigDict(extra="forbid")
    billing_mode: Literal["strict", "overdraft"]
    max_concurrent: int | None = Field(default=None, gt=0)
    overdraft_floor: Decimal | None = None
    per_operation: dict[str, OperationPolicy] | None = None

    @model_validator(mode="after")
    def validate_overdraft_floor(self) -> PlanSafety:
        if self.overdraft_floor is not None and (not self.overdraft_floor.is_finite() or self.overdraft_floor > 0):
            raise ValueError("overdraft_floor must be finite and <= 0")
        return self


class PlanDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    tier: int | None = Field(default=None, ge=0)
    allowance: Allowance
    rate_card: str | None = None
    safety: PlanSafety
    entitlements: dict[str, Entitlement] | None = None


class GetUserPlanResult(BaseModel):
    user_id: str
    plan_id: str | None
    plan_key: str | None
    plan_label: str | None
    allowance_amount: Decimal
    allowance: PlanAllowancePolicy | None
    allowance_period: Literal["calendar_month", "rolling_30d", "anniversary"] | None
    entitlements: dict[str, Entitlement]
    rate_card: str | None = None
    billing_mode: Literal["strict", "overdraft"]
    credit_policy: PlanCreditPolicy | None
    admission: PlanAdmissionPolicy | None
    allowed_operations: list[str]
    per_operation: dict[str, OperationPolicy] = Field(default_factory=dict)
    max_concurrent: int | None = None
    overdraft_floor: Decimal | None = None
    plan_assigned_at: datetime | None = None
    assignment_source_type: str | None = None
    assignment_source_id: str | None = None
    revision_policy: str | None = None
    config_version: int | None = None
    catalog_version: int | None = None
