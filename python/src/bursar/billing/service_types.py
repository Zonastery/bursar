"""Pydantic service options mirroring the JavaScript billing service types."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from bursar.billing.types import BillingEventHandler, BillingEventType
from bursar.credits.types import CreditMetadata
from bursar.shared.logger import Logger

ResolveUser = Callable[[str, str | None, str | None], str | None]


@runtime_checkable
class BillingProvisioningPort(Protocol):
    def set_user_plan(
        self,
        user_id: str,
        plan_key: str,
        *,
        plan_assigned_at: datetime | None = None,
    ) -> object: ...

    def unset_user_plan(self, user_id: str) -> None: ...

    def add_credits(
        self,
        user_id: str,
        amount: Decimal | int,
        *,
        entry_type: str = "adjustment",
        metadata: CreditMetadata | None = None,
        expires_at: datetime | None = None,
        bucket: str | None = None,
        idempotency_key: str | None = None,
    ) -> object: ...

    def deduct_credits(
        self,
        user_id: str,
        amount: Decimal | int,
        *,
        entry_type: str = "adjustment",
        bucket: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> object: ...

    def revoke_credits_by_entry_type(self, user_id: str, entry_type: str) -> object: ...


class BillingServiceOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    provisioning: BillingProvisioningPort | None = None
    resolve_user: ResolveUser | None = None
    event_handlers: dict[BillingEventType, BillingEventHandler] = Field(default_factory=dict)
    auto_select_entitlement_source: bool = True
    past_due_grace_period_ms: float = Field(
        default=7 * 24 * 60 * 60 * 1_000,
        ge=0,
    )
    terminal_plan_key: str | None = None
    logger: SkipValidation[Logger] | None = None


__all__ = [
    "BillingProvisioningPort",
    "BillingServiceOptions",
    "ResolveUser",
]
