"""Typed options for :mod:`bursar.credits.service`.

These Pydantic schemas mirror ``javascript/src/credits/service-types.ts`` while
using Python naming conventions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SkipValidation, field_validator

from bursar.credits.events import CreditEvent
from bursar.credits.types import (
    BillingMode,
    CreditMetadata,
    DeductionResult,
    UsageAnalyticsStore,
    UsageChargeStore,
)
from bursar.metrics import UsageMetrics
from bursar.shared.idempotency import StableKey
from bursar.shared.logger import Logger

PolicyPreset = Literal["strict_prepaid", "overdraft"]
PostDeductionSource = Literal["deduct", "settle", "raw"]
ExactAmount = Decimal | int | str
MetricsOrAmount = UsageMetrics | ExactAmount


ReplayKey = StableKey


def _parse_exact_amount(value: object, field_name: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{field_name} must be a Decimal, integer, or decimal string")
    try:
        return Decimal(value)
    except ArithmeticError as error:
        raise ValueError(f"{field_name} must be a valid decimal string") from error


class _CreditsServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class PostDeductionContext(_CreditsServiceModel):
    user_id: str
    source: PostDeductionSource
    deduction: DeductionResult


class LowBalanceConfig(_CreditsServiceModel):
    thresholds: list[ExactAmount] | None = None
    on_trigger: Callable[[CreditEvent], None | Awaitable[None]] | None = None
    max_tracked_users: int = Field(default=100_000, strict=True, ge=1)

    @field_validator("thresholds", mode="before")
    @classmethod
    def validate_thresholds(cls, values: object) -> object:
        if values is None or not isinstance(values, (list, tuple)):
            return values
        normalized = [_parse_exact_amount(value, "low-balance threshold") for value in values]
        if any(not value.is_finite() or value < 0 for value in normalized):
            raise ValueError("low-balance thresholds must be finite non-negative amounts")
        return normalized


class CreditsServiceOptions(_CreditsServiceModel):
    logger: SkipValidation[Logger] | None = None
    analytics: UsageAnalyticsStore | None = None
    usage_store: UsageChargeStore | None = None
    policy: PolicyPreset = "strict_prepaid"
    overdraft_floor: ExactAmount | None = None
    max_concurrent: int | None = Field(default=None, strict=True, ge=1)
    low_balance: LowBalanceConfig | None = None
    default_ttl_seconds: int = Field(default=600, strict=True, ge=1)
    lazy_expiry: bool = Field(default=False, strict=True)
    catalog_cache_ttl_ms: int = Field(default=300_000, strict=True, ge=0)
    post_deduction: Callable[[PostDeductionContext], None | Awaitable[None]] | None = None

    @field_validator("overdraft_floor", mode="before")
    @classmethod
    def validate_overdraft_floor(cls, value: object) -> object:
        if value is None:
            return None
        normalized = _parse_exact_amount(value, "overdraft_floor")
        if not normalized.is_finite() or normalized > 0:
            raise ValueError("overdraft_floor must be finite and <= 0")
        return normalized


class ReserveOptions(_CreditsServiceModel):
    idempotency_key: ReplayKey
    operation_type: str | None = None
    billing_mode: BillingMode | None = None
    ttl: int | None = Field(default=None, strict=True, ge=1)
    metadata: CreditMetadata | None = None
    feature: str | None = None
    model: str | None = None


class SettleOptions(_CreditsServiceModel):
    idempotency_key: ReplayKey | None = None
    metadata: CreditMetadata | None = None
    feature: str | None = None


class CanAffordOptions(_CreditsServiceModel):
    feature: str | None = None
    billing_mode: BillingMode | None = None
    operation_type: str = "usage"


class GrantSubscriptionCycleOptions(_CreditsServiceModel):
    bucket: str = "subscription"
    expires_at: datetime | None = None
    ttl_days: int | None = Field(default=None, strict=True, ge=1)
    plan_key: str | None = None
    idempotency_key: ReplayKey
    metadata: CreditMetadata | None = None


class RunBilledOptions(_CreditsServiceModel):
    estimate: MetricsOrAmount
    do_work: Callable[[], tuple[Any, MetricsOrAmount]]
    operation_type: str = "usage"
    billing_mode: BillingMode | None = None
    operation_key: ReplayKey
    ttl: int | None = Field(default=None, strict=True, ge=1)
    feature: str | None = None
    metadata: CreditMetadata | None = None
    settlement_attempts: int = Field(default=3, strict=True, ge=1)


class RunBilledAsyncOptions(_CreditsServiceModel):
    estimate: MetricsOrAmount
    do_work: Callable[[], Awaitable[tuple[Any, MetricsOrAmount]]]
    operation_type: str = "usage"
    billing_mode: BillingMode | None = None
    operation_key: ReplayKey
    ttl: int | None = Field(default=None, strict=True, ge=1)
    feature: str | None = None
    metadata: CreditMetadata | None = None
    settlement_attempts: int = Field(default=3, strict=True, ge=1)


class BeginBilledOperationOptions(_CreditsServiceModel):
    estimate: MetricsOrAmount
    operation_key: ReplayKey
    operation_type: str = "usage"
    billing_mode: BillingMode | None = None
    ttl: int | None = Field(default=None, strict=True, ge=1)
    feature: str | None = None
    metadata: CreditMetadata | None = None


__all__ = [
    "CanAffordOptions",
    "BeginBilledOperationOptions",
    "CreditsServiceOptions",
    "ExactAmount",
    "GrantSubscriptionCycleOptions",
    "LowBalanceConfig",
    "MetricsOrAmount",
    "PolicyPreset",
    "PostDeductionContext",
    "PostDeductionSource",
    "ReserveOptions",
    "RunBilledOptions",
    "RunBilledAsyncOptions",
    "SettleOptions",
]
