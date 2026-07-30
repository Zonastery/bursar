"""Typed options for :mod:`bursar.credits.service`.

These Pydantic schemas mirror ``javascript/src/credits/service-types.ts`` while
using Python naming conventions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from bursar.credits.events import CreditEvent
from bursar.credits.types import (
    BillingMode,
    CreditMetadata,
    DeductionResult,
    UsageAnalyticsStore,
)
from bursar.metrics import UsageMetrics
from bursar.shared.logger import Logger

PolicyPreset = Literal["strict_prepaid", "overdraft"]
PostDeductionSource = Literal["deduct", "settle", "raw"]
MetricsOrAmount = UsageMetrics | Decimal | int


class _CreditsServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class PostDeductionContext(_CreditsServiceModel):
    user_id: str
    source: PostDeductionSource
    deduction: DeductionResult


class LowBalanceConfig(_CreditsServiceModel):
    thresholds: list[Decimal] | None = None
    on_trigger: Callable[[CreditEvent], None | Awaitable[None]] | None = None
    max_tracked_users: int = Field(default=100_000, ge=1)


class CreditsServiceOptions(_CreditsServiceModel):
    logger: SkipValidation[Logger] | None = None
    analytics: UsageAnalyticsStore | None = None
    policy: PolicyPreset = "strict_prepaid"
    overdraft_floor: Decimal | None = None
    max_concurrent: int | None = None
    low_balance: LowBalanceConfig | None = None
    default_ttl_seconds: int = Field(default=600, ge=1)
    lazy_expiry: bool = False
    pricing_ttl: int = Field(default=300_000, ge=0)
    post_deduction: Callable[[PostDeductionContext], None | Awaitable[None]] | None = None


class ReserveOptions(_CreditsServiceModel):
    idempotency_key: str | None = None
    operation_type: str | None = None
    billing_mode: BillingMode | None = None
    required_feature: str | None = None
    ttl: int | None = Field(default=None, ge=1)
    metadata: CreditMetadata | None = None
    feature: str | None = None
    model: str | None = None


class SettleOptions(_CreditsServiceModel):
    idempotency_key: str | None = None
    metadata: CreditMetadata | None = None
    feature: str | None = None


class CanAffordOptions(_CreditsServiceModel):
    feature: str | None = None
    required_feature: str | None = None
    billing_mode: BillingMode | None = None
    operation_type: str = "usage"


class GrantSubscriptionCycleOptions(_CreditsServiceModel):
    bucket: str = "subscription"
    expires_at: datetime | None = None
    ttl_days: int | None = Field(default=None, ge=1)
    replace_prior: bool = True
    plan_key: str | None = None
    idempotency_key: str | None = None
    metadata: CreditMetadata | None = None


class RunBilledOptions(_CreditsServiceModel):
    estimate: MetricsOrAmount
    do_work: Callable[[], tuple[Any, MetricsOrAmount]]
    operation_type: str = "usage"
    billing_mode: BillingMode | None = None
    required_feature: str | None = None
    idempotency_key: str | None = None
    ttl: int | None = Field(default=None, ge=1)
    feature: str | None = None


__all__ = [
    "CanAffordOptions",
    "CreditsServiceOptions",
    "GrantSubscriptionCycleOptions",
    "LowBalanceConfig",
    "MetricsOrAmount",
    "PolicyPreset",
    "PostDeductionContext",
    "PostDeductionSource",
    "ReserveOptions",
    "RunBilledOptions",
    "SettleOptions",
]
