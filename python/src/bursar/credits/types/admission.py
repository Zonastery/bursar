"""Admission/lease types — mirrors JS SDK's ``credits/types/admission.ts``."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class LeaseResult(BaseModel):
    lease_id: str
    user_id: str
    amount: Decimal
    available: Decimal
    reserved_total: Decimal
    minimum_balance: Decimal
    billing_mode: Literal["strict", "overdraft"]
    expires_at: str
    error: str | None = None


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


class CapCheckResult(BaseModel):
    capped: bool
    current_spend: Decimal
    limit: Decimal
    action: Literal["deny", "warn", "notify"] | None
    model: str | None = None
