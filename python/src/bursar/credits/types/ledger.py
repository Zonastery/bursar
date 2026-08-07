"""Ledger types — mirrors JS SDK's ``credits/types/ledger.ts``."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, model_validator


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
    billing_disposition: Literal["billable", "record_only"]
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


class UsageRecordResult(BaseModel):
    """Result of appending usage without creating another balance debit."""

    usage_id: str | None
    user_id: str
    requested: Decimal
    idempotent: bool
    error: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.error is None and self.usage_id is None:
            raise ValueError("successful usage records require a usage_id")
        if self.error is not None and (self.usage_id is not None or self.idempotent):
            raise ValueError("failed usage records cannot be committed or idempotent replays")
        return self


class ListUsageChargesOptions(BaseModel):
    from_date: datetime | None = None
    to_date: datetime | None = None
    limit: int | None = None
    cursor: UsageChargeCursor | None = None
    include_record_only: bool = True
