"""Quota types — mirrors JS SDK's ``credits/types/quotas.ts``."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OperationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    billing_mode: str = "strict"
    max_concurrent: int | None = Field(default=None, gt=0)
    overdraft_floor: Decimal | None = None

    @model_validator(mode="after")
    def validate_overdraft_floor(self) -> OperationPolicy:
        if self.overdraft_floor is not None and (not self.overdraft_floor.is_finite() or self.overdraft_floor > 0):
            raise ValueError("overdraft_floor must be finite and <= 0")
        return self


class FeatureLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_calls: int
    period: Literal["daily", "weekly", "monthly", "yearly"] = "monthly"
    action: Literal["deny", "warn", "notify"] = "deny"


class CheckFeatureResult(BaseModel):
    user_id: str
    feature: str
    value: Any = None
    has_feature: bool = False


class SweepResult(BaseModel):
    expired_count: int = 0


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
    amount: Decimal = Field(default=Decimal(0), ge=0)
    period: str = "calendar_month"

    @model_validator(mode="after")
    def validate_amount(self) -> Allowance:
        if not self.amount.is_finite():
            raise ValueError("allowance amount must be finite")
        return self


class PlanSafety(BaseModel):
    model_config = ConfigDict(extra="forbid")
    billing_mode: str = "strict"
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
    allowance: Allowance | None = None
    rate_overrides: dict[str, str] | None = None
    safety: PlanSafety | None = None
    entitlements: dict[str, Entitlement] | None = None


class GetUserPlanResult(BaseModel):
    user_id: str
    plan_id: str | None = None
    plan_key: str | None = None
    plan_label: str | None = None
    allowance_amount: Decimal = Decimal(0)
    allowance_period: str = "calendar_month"
    entitlements: dict[str, Entitlement] = Field(default_factory=dict)
    rate_overrides: dict[str, str] = Field(default_factory=dict)
    billing_mode: str = "strict"
    per_operation: dict[str, OperationPolicy] = Field(default_factory=dict)
    max_concurrent: int | None = None
    overdraft_floor: Decimal | None = None
    plan_assigned_at: datetime | None = None
    config_version: int | None = None
    catalog_version: int | None = None
