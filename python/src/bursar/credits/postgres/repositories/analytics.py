from __future__ import annotations

from typing import Any

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import validate_non_empty, validate_non_negative
from bursar.credits.postgres.repositories.schemas import (
    AggregateStatsRow,
    DailySpendRow,
    LedgerEntry,
    SpendByModelRow,
    SpendByUserRow,
    TopUserRow,
)


def _to_dict(row: Any, fields: list[str]) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    if isinstance(row, (list, tuple)):
        if len(row) != len(fields):
            raise ValueError(f"expected {len(fields)} columns, got {len(row)}")
        return dict(zip(fields, row, strict=True))
    return {}


class AnalyticsRepository:
    """Read-only analytics and canonical ledger queries."""

    def __init__(self, callproc: DbQuery) -> None:
        self._callproc = callproc

    def spend_by_user(self, start: str, end: str) -> list[SpendByUserRow]:
        rows = self._callproc("spend_by_user", [start, end]) or []
        fields = ["user_id", "total_spend", "entry_count"]
        normalized = []
        for row in rows:
            data = _to_dict(row, fields)
            data["user_id"] = data.get("subject_id", data.get("user_id"))
            data["entry_count"] = data.get("charge_count", data.get("entry_count"))
            normalized.append(SpendByUserRow.model_validate(data))
        return normalized

    def spend_by_model(self, start: str, end: str) -> list[SpendByModelRow]:
        rows = self._callproc("spend_by_model", [start, end]) or []
        fields = ["model", "total_spend", "entry_count"]
        normalized = []
        for row in rows:
            data = _to_dict(row, fields)
            data["entry_count"] = data.get("charge_count", data.get("entry_count"))
            normalized.append(SpendByModelRow.model_validate(data))
        return normalized

    def top_users(self, limit: int, start: str, end: str) -> list[TopUserRow]:
        validate_non_negative(limit, "limit")
        rows = self._callproc("spend_by_user", [start, end]) or []
        normalized = []
        for row in rows[:limit]:
            data = _to_dict(
                row,
                ["user_id", "total_spend", "entry_count"],
            )
            data["user_id"] = data.get("subject_id", data.get("user_id"))
            normalized.append(TopUserRow.model_validate(data))
        return normalized

    def daily_spend(self, start: str, end: str) -> list[DailySpendRow]:
        rows = self._callproc("daily_spend", [start, end]) or []
        fields = ["date", "total_spend", "entry_count"]
        normalized = []
        for row in rows:
            data = _to_dict(row, fields)
            data["date"] = data.get("day", data.get("date"))
            data["entry_count"] = data.get("charge_count", data.get("entry_count"))
            normalized.append(DailySpendRow.model_validate(data))
        return normalized

    def aggregate_stats(self, start: str, end: str) -> AggregateStatsRow:
        rows = self._callproc("aggregate_usage_stats", [start, end]) or []
        fields = [
            "total_credits_consumed",
            "active_users",
            "avg_daily_spend",
            "top_model",
            "top_user",
        ]
        return AggregateStatsRow.model_validate(_to_dict(rows[0], fields) if rows else {})

    def list_ledger_entries(
        self,
        user_id: str,
        entry_types: list[str] | None,
        from_date: str | None,
        to_date: str | None,
        limit: int,
        cursor_created_at: str | None,
        cursor_entry_id: str | None,
        *,
        usage_only: bool = False,
    ) -> list[LedgerEntry]:
        validate_non_empty(user_id, "user_id")
        validate_non_negative(limit, "limit")
        if (cursor_created_at is None) != (cursor_entry_id is None):
            raise ValueError("ledger cursor requires both created_at and entry_id")
        rows = (
            self._callproc(
                "list_ledger",
                [
                    user_id,
                    cursor_created_at,
                    cursor_entry_id,
                    limit,
                    entry_types,
                    from_date,
                    to_date,
                    usage_only,
                ],
            )
            or []
        )
        fields = [
            "entry_id",
            "account_id",
            "actor_user_id",
            "amount",
            "entry_type",
            "reference_entry_id",
            "idempotency_key",
            "metadata",
            "created_at",
        ]
        return [LedgerEntry.model_validate(_to_dict(row, fields)) for row in rows]

    def get_ledger_entry(self, user_id: str, entry_id: str) -> LedgerEntry | None:
        validate_non_empty(user_id, "user_id")
        validate_non_empty(entry_id, "entry_id")
        rows = self._callproc("get_ledger_entry", [user_id, entry_id]) or []
        if not rows:
            return None
        fields = [
            "entry_id",
            "account_id",
            "actor_user_id",
            "amount",
            "entry_type",
            "reference_entry_id",
            "idempotency_key",
            "metadata",
            "created_at",
        ]
        return LedgerEntry.model_validate(_to_dict(rows[0], fields))
