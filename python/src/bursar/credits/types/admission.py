"""Admission/lease types — mirrors JS SDK's ``credits/types/admission.ts``."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class LeaseResult(BaseModel):
    lease_id: str
    user_id: str
    amount: Decimal = Decimal(0)
    available: Decimal = Decimal(0)
    reserved_total: Decimal = Decimal(0)
    billing_mode: str = "strict"
    expires_at: datetime | None = None
    error: str | None = None


class ReleaseResult(BaseModel):
    lease_id: str
    user_id: str
    released: bool = False
    reason: str | None = None


class CapCheckResult(BaseModel):
    capped: bool = False
    current_spend: Decimal = Decimal(0)
    cap_limit: Decimal = Decimal(0)
    action: Literal["deny", "warn", "notify"] | None = None
    model: str | None = None
