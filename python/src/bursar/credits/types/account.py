"""Credit account types — mirrors JS SDK's ``credits/types/account.ts``."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator


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
    balance: Decimal
    lifetime_purchased: Decimal


class AddCreditsResult(BaseModel):
    entry_id: str
    user_id: str
    amount: Decimal
    new_balance: Decimal
    lifetime_purchased: Decimal
    bucket: str
    idempotent: bool = False


class AvailableResult(BaseModel):
    user_id: str
    balance: Decimal
    reserved: Decimal
    available: Decimal


class DeductionResult(BaseModel):
    entry_id: str
    user_id: str
    amount: Decimal
    balance_after: Decimal
    allowance_consumed: Decimal
    idempotent: bool
    error: str | None = None
    bucket_breakdown: dict[str, Decimal] | None = None


class RefundResult(BaseModel):
    refund_entry_id: str
    original_entry_id: str
    user_id: str
    amount: Decimal
    new_balance: Decimal
    error: str | None = None
    bucket_breakdown: dict[str, Decimal] | None = None


class CanAffordResult(BaseModel):
    affordable: bool
    spendable: Decimal
    worst_case: Decimal
    reason: str | None = None


class AllowanceResult(BaseModel):
    plan_id: str
    allowance_remaining: Decimal
    period_start: str
    period_end: str


class BucketBalance(BaseModel):
    bucket_key: str
    label: str
    priority: int
    expires: bool
    balance: Decimal


class BucketBalancesResult(BaseModel):
    user_id: str
    buckets: list[BucketBalance]
    total_balance: Decimal


BillingMode = Literal["strict", "overdraft"]


class BucketDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    priority: int
    expires: bool
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


class DeductWithAllowanceOptions(BaseModel):
    idempotency_key: str | None = None
    operation: str | None = None
    feature: str | None = None
    model: str | None = None
    region: str | None = None
    measures: dict[str, Any] | None = None
    dimensions: dict[str, Any] | None = None
    metadata: CreditMetadata | None = None
