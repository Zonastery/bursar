"""Credit operations — mirrors JS SDK's ``credits/`` subpackage."""

from __future__ import annotations

from bursar.credits.events import CreditEvent, CreditEventEmitter, CreditEventType
from bursar.credits.service import CreditsService
from bursar.credits.service_types import (
    CanAffordOptions,
    CreditsServiceOptions,
    GrantSubscriptionCycleOptions,
    LowBalanceConfig,
    MetricsOrAmount,
    PolicyPreset,
    PostDeductionContext,
    ReserveOptions,
    RunBilledOptions,
    SettleOptions,
)
from bursar.credits.store import CreditStore

__all__ = [
    "CanAffordOptions",
    "CreditsService",
    "CreditsServiceOptions",
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
    "SettleOptions",
]
