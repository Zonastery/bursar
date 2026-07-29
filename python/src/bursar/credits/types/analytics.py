"""Analytics types — mirrors JS SDK's ``credits/types/analytics.ts``."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel


class SpendByUserRow(BaseModel):
    user_id: str = ""
    total_spend: Decimal = Decimal(0)
    entry_count: int = 0


class SpendByModelRow(BaseModel):
    model: str = ""
    total_spend: Decimal = Decimal(0)
    entry_count: int = 0


class TopUserRow(BaseModel):
    user_id: str = ""
    total_spend: Decimal = Decimal(0)


class DailySpendRow(BaseModel):
    date: str = ""
    total_spend: Decimal = Decimal(0)
    entry_count: int = 0


class AggregateStatsRow(BaseModel):
    total_credits_consumed: Decimal = Decimal(0)
    active_users: int = 0
    avg_daily_spend: Decimal = Decimal(0)
    top_model: str = ""
    top_user: str = ""


class UsageAnalyticsStore(Protocol):
    """Read-only usage analytics backend.

    PostgreSQL implements this protocol by default. High-volume deployments
    can provide ClickHouse without moving transactional state out of Postgres.
    """

    def spend_by_user(self, start: datetime, end: datetime) -> list[SpendByUserRow]: ...

    def spend_by_model(self, start: datetime, end: datetime) -> list[SpendByModelRow]: ...

    def top_users(self, limit: int, start: datetime, end: datetime) -> list[TopUserRow]: ...

    def daily_spend(self, start: datetime, end: datetime) -> list[DailySpendRow]: ...

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStatsRow: ...
