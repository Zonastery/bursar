"""Analytics types — mirrors JS SDK's ``credits/types/analytics.ts``."""

from __future__ import annotations

from decimal import Decimal

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
