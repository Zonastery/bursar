"""Analytics types — mirrors JS SDK's ``credits/types/analytics.ts``."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from bursar.credits.types.ledger import UsageChargeCursor, UsageChargePage


class SpendByUserRow(BaseModel):
    user_id: str
    total_spend: Decimal
    entry_count: int


class SpendByModelRow(BaseModel):
    model: str
    total_spend: Decimal
    entry_count: int


class TopUserRow(BaseModel):
    user_id: str
    total_spend: Decimal


class DailySpendRow(BaseModel):
    date: str
    total_spend: Decimal
    entry_count: int


class AggregateStats(BaseModel):
    total_credits_consumed: Decimal
    active_users: int
    avg_daily_spend: Decimal
    top_model: str
    top_user: str


@runtime_checkable
class UsageAnalyticsStore(Protocol):
    """Read-only usage analytics backend.

    PostgreSQL implements this protocol by default. High-volume deployments
    can provide ClickHouse without moving balances or compact accounting
    receipts out of PostgreSQL.
    """

    def spend_by_user(self, start: datetime, end: datetime) -> list[SpendByUserRow]: ...

    def spend_by_model(self, start: datetime, end: datetime) -> list[SpendByModelRow]: ...

    def top_users(self, limit: int, start: datetime, end: datetime) -> list[TopUserRow]: ...

    def daily_spend(self, start: datetime, end: datetime) -> list[DailySpendRow]: ...

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStats: ...


@runtime_checkable
class UsageChargeStore(Protocol):
    """Read-only usage history backend selected with the analytics backend."""

    def list_usage_charges(
        self,
        user_id: str,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        cursor: UsageChargeCursor | None = None,
        include_record_only: bool = True,
    ) -> UsageChargePage: ...
