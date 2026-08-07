"""Admission/lease types — mirrors JS SDK's ``credits/types/admission.ts``."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, model_validator


class LeaseResult(BaseModel):
    lease_id: str | None
    user_id: str
    amount: Decimal | None
    available: Decimal
    reserved_total: Decimal
    minimum_balance: Decimal | None
    billing_mode: Literal["strict", "overdraft"]
    expires_at: str | None
    error: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.error is None and (
            self.lease_id is None or self.amount is None or self.minimum_balance is None or self.expires_at is None
        ):
            raise ValueError("successful leases require identity, amount, policy, and expiry fields")
        if self.error is not None and self.lease_id is not None:
            raise ValueError("failed leases cannot expose a committed lease_id")
        return self


class LeasePricingContext(BaseModel):
    """Immutable pricing references captured when an operation lease is admitted."""

    catalog_version: int
    plan_id: str | None
    plan_key: str | None
    rate_card: str | None


class ReleaseResult(BaseModel):
    lease_id: str
    user_id: str
    released: bool
    reason: str | None = None
