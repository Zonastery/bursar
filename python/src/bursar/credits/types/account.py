"""Credit account types — mirrors JS SDK's ``credits/types/account.ts``."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CreditMetadata(BaseModel, extra="allow"):
    operation: str | None = None
    measures: dict[str, Any] | None = None
    dimensions: dict[str, str] | None = None
    breakdown_total: str | None = None
    reference_type: str | None = None
    reference_id: str | None = None
    idempotency_key: str | None = None


class BalanceResult(BaseModel):
    user_id: str
    balance: Decimal = Decimal(0)
    lifetime_purchased: Decimal = Decimal(0)


class AddCreditsResult(BaseModel):
    entry_id: str
    user_id: str
    amount: Decimal
    new_balance: Decimal
    lifetime_purchased: Decimal = Decimal(0)
    bucket: str = "default"


class AvailableResult(BaseModel):
    user_id: str
    balance: Decimal = Decimal(0)
    reserved: Decimal = Decimal(0)
    available: Decimal = Decimal(0)


class DeductionResult(BaseModel):
    entry_id: str
    user_id: str
    amount: Decimal
    balance_after: Decimal
    allowance_consumed: Decimal = Decimal(0)
    idempotent: bool = False
    cap_warning: str | None = None
    feature_limit_warning: str | None = None
    error: str | None = None
    bucket_breakdown: dict[str, Decimal] | None = None


class RefundResult(BaseModel):
    refund_entry_id: str
    original_entry_id: str
    user_id: str
    amount: Decimal = Decimal(0)
    new_balance: Decimal = Decimal(0)
    error: str | None = None
    bucket_breakdown: dict[str, Decimal] | None = None


class CanAffordResult(BaseModel):
    affordable: bool = False
    spendable: Decimal = Decimal(0)
    worst_case: Decimal = Decimal(0)
    reason: str | None = None


class AllowanceResult(BaseModel):
    plan_id: str
    allowance_remaining: Decimal
    period_start: date | None = None
    period_end: date | None = None


class BucketBalance(BaseModel):
    bucket_key: str
    label: str = ""
    priority: int = 0
    expires: bool = False
    balance: Decimal = Decimal(0)


class BucketBalancesResult(BaseModel):
    user_id: str
    buckets: list[BucketBalance]
    total_balance: Decimal


BillingMode = str


class BucketDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = ""
    priority: int = 0
    expires: bool = False
    ttl_days: int | None = None
    default: bool = False
    allow_overdraft: bool = False

    @model_validator(mode="after")
    def _derive_expires(self) -> BucketDefinition:
        if self.ttl_days is not None and self.ttl_days <= 0:
            raise ValueError("ttl_days must be greater than zero")
        if self.ttl_days is not None and self.expires is False:
            raise ValueError("expires cannot be false when ttl_days is set")
        if self.ttl_days is not None:
            self.expires = True
        return self


class SetUserPlanResult(BaseModel):
    user_id: str
    plan_id: str
    plan_assigned_at: datetime | None = None


class PlanMigrationStartResult(BaseModel):
    migration_id: str


class PlanMigrationBatchResult(BaseModel):
    migrated: int
    done: bool
    next_cursor: str | None = None


class SpendCap(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    user_id: str = ""
    cap_type: str = Field(default="daily", alias="type")
    model: str | None = None
    limit: Decimal = Field(default=Decimal(0), ge=0)
    action: str = "deny"
