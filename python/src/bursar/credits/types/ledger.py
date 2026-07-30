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
