from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bursar.repositories.schemas import (
    AggregateStatsRow,
    DailySpendRow,
    SpendByModelRow,
    SpendByUserRow,
    TopUserRow,
    TransactionRow,
)

CallProc = Callable[[str, list[Any]], list[Any]]


class AnalyticsRepository:
    def __init__(self, callproc: CallProc) -> None:
        self._callproc = callproc

    def spend_by_user(self, start: str, end: str) -> list[SpendByUserRow]:
        rows = self._callproc("spend_by_user", [start, end]) or []
        return [SpendByUserRow.model_validate(r) for r in rows if isinstance(r, dict)]

    def spend_by_model(self, start: str, end: str) -> list[SpendByModelRow]:
        rows = self._callproc("spend_by_model", [start, end]) or []
        return [SpendByModelRow.model_validate(r) for r in rows if isinstance(r, dict)]

    def top_users(self, limit: int, start: str, end: str) -> list[TopUserRow]:
        rows = self._callproc("top_users", [limit, start, end]) or []
        return [TopUserRow.model_validate(r) for r in rows if isinstance(r, dict)]

    def daily_spend(self, start: str, end: str) -> list[DailySpendRow]:
        rows = self._callproc("daily_spend", [start, end]) or []
        return [DailySpendRow.model_validate(r) for r in rows if isinstance(r, dict)]

    def aggregate_stats(self, start: str, end: str) -> AggregateStatsRow:
        rows = self._callproc("aggregate_stats", [start, end])
        return AggregateStatsRow.model_validate(rows[0] if rows else {})

    def list_user_transactions(
        self,
        user_id: str,
        types: list[str] | None,
        from_date: str | None,
        to_date: str | None,
        limit: int,
        offset: int,
    ) -> list[TransactionRow]:
        rows = (
            self._callproc(
                "list_user_transactions",
                [
                    user_id,
                    types,
                    from_date,
                    to_date,
                    limit,
                    offset,
                ],
            )
            or []
        )
        return [TransactionRow.model_validate(r) for r in rows if isinstance(r, dict)]

    def list_usage_events(
        self,
        user_id: str,
        from_date: str | None,
        to_date: str | None,
        limit: int,
        offset: int,
    ) -> list[TransactionRow]:
        rows = (
            self._callproc(
                "list_usage_events",
                [
                    user_id,
                    from_date,
                    to_date,
                    limit,
                    offset,
                ],
            )
            or []
        )
        return [TransactionRow.model_validate(r) for r in rows if isinstance(r, dict)]
