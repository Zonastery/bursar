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

    def _list_offset_compat(
        self,
        user_id: str,
        types: list[str] | None,
        from_date: str | None,
        to_date: str | None,
        limit: int,
        offset: int,
        *,
        usage_only: bool,
    ) -> list[TransactionRow]:
        validate_non_empty(user_id, "user_id")
        validate_non_negative(limit, "limit")
        validate_non_negative(offset, "offset")
        if limit == 0:
            return []

        cursor_created_at: str | None = None
        cursor_id: str | None = None
        remaining = offset
        result: list[TransactionRow] = []
        total_count = 0
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
            "next_cursor_created_at",
            "next_cursor_id",
        ]
        rpc_name = "list_usage_events_cursor" if usage_only else "list_transactions_cursor_with_total"
        while True:
            params = (
                [user_id, from_date, to_date, min(max(limit + remaining, 1), 200), cursor_created_at, cursor_id]
                if usage_only
                else [
                    user_id,
                    types,
                    from_date,
                    to_date,
                    min(max(limit + remaining, 1), 200),
                    cursor_created_at,
                    cursor_id,
                ]
            )
            rows = self._callproc(rpc_name, params) or []
            parsed = [TransactionRow.model_validate(_to_dict(r, fields)) for r in rows]
            if parsed:
                total_count = parsed[0].total_count
            if remaining < len(parsed):
                result.extend(parsed[remaining : remaining + limit])
                break
            remaining -= len(parsed)
            marker = parsed[-1] if parsed else None
            if marker is None or marker.next_cursor_created_at is None or marker.next_cursor_id is None:
                break
            cursor_created_at = str(marker.next_cursor_created_at)
            cursor_id = str(marker.next_cursor_id)

        for row in result:
            row.total_count = total_count
        return result

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
        return self._list_offset_compat(user_id, types, from_date, to_date, limit, offset, usage_only=False)

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
        return self._list_offset_compat(user_id, None, from_date, to_date, limit, offset, usage_only=True)

    def list_transactions_cursor(
        self,
        user_id: str,
        types: list[str] | None,
        from_date: str | None,
        to_date: str | None,
        limit: int,
        cursor_created_at: str | None,
        cursor_id: str | None,
    ) -> list[TransactionRow]:
        validate_non_empty(user_id, "user_id")
        validate_non_negative(limit, "limit")
        if (cursor_created_at is None) != (cursor_id is None):
            raise ValueError("transaction cursor requires both created_at and id")
        rows = (
            self._callproc(
                "list_transactions_cursor",
                [user_id, types, from_date, to_date, limit, cursor_created_at, cursor_id],
            )
            or []
        )
        fields = [
            "id",
            "user_id",
            "amount",
            "type",
            "reference_type",
            "reference_id",
            "metadata",
            "created_at",
            "next_cursor_created_at",
            "next_cursor_id",
        ]
        return [TransactionRow.model_validate(_to_dict(r, fields)) for r in rows]
