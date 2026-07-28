"""Ledger types — mirrors JS SDK's ``credits/types/ledger.ts``."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class LedgerEntry(BaseModel):
    entry_id: str = ""
    account_id: str = ""
    actor_user_id: str | None = None
    amount: Decimal = Decimal(0)
    entry_type: str = ""
    reference_entry_id: str | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: str = ""


class LedgerCursor(BaseModel):
    created_at: str
    entry_id: str


class LedgerPage(BaseModel):
    items: list[LedgerEntry] = Field(default_factory=list)
    next_cursor: LedgerCursor | None = None
