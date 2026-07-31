"""Ledger types — mirrors JS SDK's ``credits/types/ledger.ts``."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class LedgerEntry(BaseModel):
    entry_id: str
    account_id: str
    actor_user_id: str | None
    amount: Decimal
    entry_type: str
    operation: str
    reference_entry_id: str | None
    idempotency_key: str | None
    metadata: dict[str, Any] | None
    created_at: str


class LedgerCursor(BaseModel):
    created_at: str
    entry_id: str


class LedgerPage(BaseModel):
    items: list[LedgerEntry]
    next_cursor: LedgerCursor | None


class ListLedgerEntriesOptions(BaseModel):
    entry_types: list[str] | None = None
    from_date: datetime | None = None
    to_date: datetime | None = None
    limit: int | None = None
    cursor: LedgerCursor | None = None


class ListUsageEntriesOptions(BaseModel):
    from_date: datetime | None = None
    to_date: datetime | None = None
    limit: int | None = None
    cursor: LedgerCursor | None = None


class UsageCharge(BaseModel):
    usage_id: str
    account_id: str
    operation: str
    requested: Decimal
    charged: Decimal
    allowance_requested: Decimal
    allowance_covered: Decimal
    feature: str | None
    model: str | None
    region: str | None
    event_at: str
    idempotency_key: str
    metadata: dict[str, Any] | None
    created_at: str


class UsageChargeCursor(BaseModel):
    event_at: str
    usage_id: str


class UsageChargePage(BaseModel):
    items: list[UsageCharge]
    next_cursor: UsageChargeCursor | None


class ListUsageChargesOptions(BaseModel):
    from_date: datetime | None = None
    to_date: datetime | None = None
    limit: int | None = None
    cursor: UsageChargeCursor | None = None
