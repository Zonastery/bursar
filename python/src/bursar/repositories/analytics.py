from __future__ import annotations

from typing import Any

from bursar.repositories._types import DbQuery
from bursar.repositories._utils import validate_non_empty, validate_non_negative
from bursar.repositories.schemas import (
    AggregateStatsRow,
    DailySpendRow,
    SpendByModelRow,
    SpendByUserRow,
    TopUserRow,
    TransactionRow,
)


def _to_dict(row: Any, fields: list[str]) -> dict[str, Any]:
    """Convert a tuple row from a TABLE-returning RPC to a field-name-keyed dict."""
    if isinstance(row, dict):
        return row
    if isinstance(row, (list, tuple)):
        assert len(row) == len(fields), f"expected {len(fields)} columns, got {len(row)}: {row}"
        return dict(zip(fields, row, strict=True))
    return {}


class AnalyticsRepository:
    """Repository for usage analytics queries.

    All methods call Postgres RPCs via the callproc function.
    Returns typed Pydantic models for each result row.
    """

    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    def spend_by_user(self, start: str, end: str) -> list[SpendByUserRow]:
        rows = self._callproc("spend_by_user", [start, end]) or []
        return [
            SpendByUserRow.model_validate(_to_dict(r, ["user_id", "total_spend", "transaction_count"])) for r in rows
        ]

    def spend_by_model(self, start: str, end: str) -> list[SpendByModelRow]:
        rows = self._callproc("spend_by_model", [start, end]) or []
        return [
            SpendByModelRow.model_validate(_to_dict(r, ["model", "total_spend", "transaction_count"])) for r in rows
        ]

    def top_users(self, limit: int, start: str, end: str) -> list[TopUserRow]:
        validate_non_negative(limit, "limit")
        rows = self._callproc("top_users", [limit, start, end]) or []
        return [TopUserRow.model_validate(_to_dict(r, ["user_id", "total_spend"])) for r in rows]

    def daily_spend(self, start: str, end: str) -> list[DailySpendRow]:
        rows = self._callproc("daily_spend", [start, end]) or []
        return [DailySpendRow.model_validate(_to_dict(r, ["date", "total_spend", "transaction_count"])) for r in rows]

    def aggregate_stats(self, start: str, end: str) -> AggregateStatsRow | None:
        """Get aggregate usage statistics for a date range.

        Args:
            start: ISO date string for the range start.
            end: ISO date string for the range end.

        Returns:
            AggregateStatsRow if data exists, None otherwise.
        """
        rows = self._callproc("aggregate_stats", [start, end]) or []
        if not rows:
            return None
        return AggregateStatsRow.model_validate(rows[0])

    def _list_transactions(
        self,
        rpc_name: str,
        params: list[Any],
        user_id: str,
        limit: int,
        offset: int,
    ) -> list[TransactionRow]:
        validate_non_empty(user_id, "user_id")
        validate_non_negative(limit, "limit")
        validate_non_negative(offset, "offset")
        rows = self._callproc(rpc_name, params) or []
        fields = [
            "id",
            "user_id",
            "amount",
            "type",
            "reference_type",
            "reference_id",
            "metadata",
            "created_at",
            "total_count",
        ]
        return [TransactionRow.model_validate(_to_dict(r, fields)) for r in rows]

    def list_user_transactions(
        self,
        user_id: str,
        types: list[str] | None,
        from_date: str | None,
        to_date: str | None,
        limit: int,
        offset: int,
    ) -> list[TransactionRow]:
        """List credit transactions for a user with optional filters.

        Args:
            user_id: The user ID.
            types: Filter by transaction types, or None for all.
            from_date: ISO date string for the start of the range, or None.
            to_date: ISO date string for the end of the range, or None.
            limit: Maximum number of rows to return.
            offset: Number of rows to skip.

        Returns:
            List of TransactionRow (may be empty).
        """
        return self._list_transactions(
            "list_user_transactions",
            [user_id, types, from_date, to_date, limit, offset],
            user_id,
            limit,
            offset,
        )

    def list_usage_events(
        self,
        user_id: str,
        from_date: str | None,
        to_date: str | None,
        limit: int,
        offset: int,
    ) -> list[TransactionRow]:
        """List usage events for a user with optional date filters.

        Args:
            user_id: The user ID.
            from_date: ISO date string for the start of the range, or None.
            to_date: ISO date string for the end of the range, or None.
            limit: Maximum number of rows to return.
            offset: Number of rows to skip.

        Returns:
            List of TransactionRow (may be empty).
        """
        return self._list_transactions(
            "list_usage_events",
            [user_id, from_date, to_date, limit, offset],
            user_id,
            limit,
            offset,
        )
