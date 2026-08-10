"""Credit operations — mirrors JS SDK's ``credits/`` subpackage."""

from __future__ import annotations

from bursar.credits.events import CreditEvent, CreditEventEmitter, CreditEventType
from bursar.credits.service import CreditsService
from bursar.credits.service_types import (
    BeginBilledOperationOptions,
    CanAffordOptions,
    CreditsServiceOptions,
    ExactAmount,
    GrantSubscriptionCycleOptions,
    LowBalanceConfig,
    MetricsOrAmount,
    PolicyPreset,
    PostDeductionContext,
    ReserveOptions,
    RunBilledAsyncOptions,
    RunBilledOptions,
    SettleOptions,
)
from bursar.credits.store import CreditStore

__all__ = [
    "CanAffordOptions",
    "BeginBilledOperationOptions",
    "CreditsService",
    "CreditsServiceOptions",
    "ExactAmount",
    "CreditStore",
    "CreditEvent",
    "CreditEventEmitter",
    "CreditEventType",
    "GrantSubscriptionCycleOptions",
    "LowBalanceConfig",
    "MetricsOrAmount",
    "PolicyPreset",
    "PostDeductionContext",
    "ReserveOptions",
    "RunBilledOptions",
    "RunBilledAsyncOptions",
    "SettleOptions",
]
