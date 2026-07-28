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
        return [SpendByUserRow.model_validate(_to_dict(row, fields)) for row in rows]

    def spend_by_model(self, start: str, end: str) -> list[SpendByModelRow]:
        rows = self._callproc("spend_by_model", [start, end]) or []
        fields = ["model", "total_spend", "entry_count"]
        return [SpendByModelRow.model_validate(_to_dict(row, fields)) for row in rows]

    def top_users(self, limit: int, start: str, end: str) -> list[TopUserRow]:
        validate_non_negative(limit, "limit")
        rows = self._callproc("spend_by_user", [start, end]) or []
        return [TopUserRow.model_validate(_to_dict(row, ["user_id", "total_spend"])) for row in rows]

    def daily_spend(self, start: str, end: str) -> list[DailySpendRow]:
        rows = self._callproc("daily_spend", [start, end]) or []
        fields = ["date", "total_spend", "entry_count"]
        return [DailySpendRow.model_validate(_to_dict(row, fields)) for row in rows]

    def aggregate_stats(self, start: str, end: str) -> AggregateStatsRow:
        rows = []
        if not rows:
            return AggregateStatsRow()
        fields = [
            "total_credits_consumed",
            "active_users",
            "avg_daily_spend",
            "top_model",
            "top_user",
        ]
        return AggregateStatsRow.model_validate(_to_dict(rows[0], fields))

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
        del entry_types, from_date, to_date, usage_only
        rows = (
            self._callproc(
                "list_ledger",
                [user_id, cursor_created_at, cursor_entry_id, limit],
            )
            or []
        )
        fields = [
            "id",
            "account_id",
            "kind",
            "amount",
            "balance_after",
            "reference_entry_id",
            "catalog_revision_id",
            "idempotency_key",
            "request_digest",
            "operation",
            "metadata",
            "created_at",
        ]
        entries: list[LedgerEntry] = []
        for row in rows:
            data = _to_dict(row, fields)
            data["entry_id"] = data.pop("id", "")
            data["entry_type"] = data.pop("kind", "")
            entries.append(LedgerEntry.model_validate(data))
        return entries
