"""ClickHouse usage projection mirroring JavaScript adapter logic."""

from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, cast
from uuid import UUID

from bursar.credits.types import (
    AggregateStatsRow,
    DailySpendRow,
    SpendByModelRow,
    SpendByUserRow,
    TopUserRow,
)
from bursar.storage.ports import UsageChargeExport


class ClickHouseQueryResult(Protocol):
    @property
    def result_rows(self) -> Sequence[Sequence[Any]]: ...

    @property
    def column_names(self) -> Sequence[str]: ...


class ClickHouseClient(Protocol):
    def command(self, query: str) -> Any: ...

    def insert(
        self,
        table: str,
        data: Sequence[Sequence[Any]],
        *,
        column_names: Sequence[str],
    ) -> Any: ...

    def query(
        self,
        query: str,
        *,
        parameters: dict[str, Any] | None = None,
    ) -> ClickHouseQueryResult: ...


@dataclass(frozen=True, slots=True)
class ClickHouseUsageStoreOptions:
    client: ClickHouseClient
    table: str = "bursar_usage_events"
    create_table: bool = True
    retention_days: int | None = None


def _validate_range(start: datetime, end: datetime) -> None:
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        msg = "analytics requires end after start"
        raise ValueError(msg)
    try:
        valid = end > start
    except TypeError as error:
        msg = "analytics requires compatible timezone-aware datetimes"
        raise ValueError(msg) from error
    if not valid:
        msg = "analytics requires end after start"
        raise ValueError(msg)


def _validate_table_name(table: str) -> str:
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)?", table):
        msg = "ClickHouse table must be an identifier or database.identifier"
        raise ValueError(msg)
    return table


def _quote_table(table: str) -> str:
    return ".".join(f'"{part}"' for part in table.split("."))


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        msg = f"Invalid usage timestamp: {value}"
        raise ValueError(msg)
    return parsed.astimezone(UTC)


def _optional_uuid(value: str | None) -> UUID | None:
    return UUID(value) if value is not None else None


class ClickHouseUsageStore:
    """Idempotent ClickHouse projection and usage analytics read port."""

    _INSERT_COLUMNS = (
        "outbox_event_id",
        "charge_id",
        "account_id",
        "subject_id",
        "operation",
        "feature",
        "model",
        "region",
        "measures",
        "dimensions",
        "metadata",
        "requested",
        "charged",
        "allowance_requested",
        "allowance_covered",
        "catalog_revision_id",
        "plan_id",
        "rate_card_key",
        "pricing_snapshot",
        "ledger_entry_id",
        "correction_of_charge_id",
        "idempotency_key",
        "request_digest",
        "event_at",
        "created_at",
    )

    def __init__(self, options: ClickHouseUsageStoreOptions) -> None:
        self._client = options.client
        self._table = _validate_table_name(options.table)
        self._quoted_table = _quote_table(self._table)
        self._create_table = options.create_table
        self._retention_days = options.retention_days
        if self._retention_days is not None and (
            isinstance(self._retention_days, bool)
            or not isinstance(self._retention_days, int)
            or not 1 <= self._retention_days <= 36_500
        ):
            msg = "ClickHouse retention_days must be between 1 and 36500"
            raise ValueError(msg)
        self._initialize_lock = threading.Lock()
        self._initialized = not self._create_table

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self._create_projection_table()
            self._initialized = True

    def write_usage(self, event: UsageChargeExport, outbox_event_id: str) -> None:
        self.initialize()
        row = (
            int(outbox_event_id),
            UUID(event.charge_id),
            UUID(event.account_id),
            UUID(event.subject_id),
            event.operation,
            event.feature,
            event.model,
            event.region,
            json.dumps(event.measures, separators=(",", ":")),
            json.dumps(event.dimensions, separators=(",", ":")),
            json.dumps(event.metadata, separators=(",", ":")),
            Decimal(event.requested),
            Decimal(event.charged),
            Decimal(event.allowance_requested),
            Decimal(event.allowance_covered),
            _optional_uuid(event.catalog_revision_id),
            _optional_uuid(event.plan_id),
            event.rate_card_key,
            json.dumps(event.pricing_snapshot, separators=(",", ":")),
            _optional_uuid(event.ledger_entry_id),
            _optional_uuid(event.correction_of_charge_id),
            event.idempotency_key,
            event.request_digest,
            _timestamp(event.event_at),
            _timestamp(event.created_at),
        )
        self._client.insert(
            self._table,
            [row],
            column_names=self._INSERT_COLUMNS,
        )

    def spend_by_user(self, start: datetime, end: datetime) -> list[SpendByUserRow]:
        return [
            SpendByUserRow(
                user_id=str(row["key"]),
                total_spend=Decimal(str(row["total_spend"])),
                entry_count=int(row["entry_count"]),
            )
            for row in self._spend_rows("subject_id", start, end)
        ]

    def spend_by_model(self, start: datetime, end: datetime) -> list[SpendByModelRow]:
        return [
            SpendByModelRow(
                model=str(row["key"]),
                total_spend=Decimal(str(row["total_spend"])),
                entry_count=int(row["entry_count"]),
            )
            for row in self._spend_rows("coalesce(model, 'unknown')", start, end)
        ]

    def top_users(
        self,
        limit: int,
        start: datetime,
        end: datetime,
    ) -> list[TopUserRow]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            msg = "top_users limit must be between 1 and 10000"
            raise ValueError(msg)
        return [
            TopUserRow(
                user_id=str(row["key"]),
                total_spend=Decimal(str(row["total_spend"])),
            )
            for row in self._spend_rows("subject_id", start, end, limit)
        ]

    def daily_spend(self, start: datetime, end: datetime) -> list[DailySpendRow]:
        _validate_range(start, end)
        rows = self._query_rows(
            f"""
            SELECT
                formatDateTime(toStartOfDay(event_at), '%F') AS key,
                toString(sum(charged)) AS total_spend,
                toString(count()) AS entry_count
            FROM {self._quoted_table} FINAL
            WHERE event_at >= parseDateTime64BestEffort({{start:String}})
              AND event_at < parseDateTime64BestEffort({{end:String}})
            GROUP BY key
            ORDER BY key
            """,
            start,
            end,
        )
        return [
            DailySpendRow(
                date=str(row["key"]),
                total_spend=Decimal(str(row["total_spend"])),
                entry_count=int(row["entry_count"]),
            )
            for row in rows
        ]

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStatsRow:
        _validate_range(start, end)
        with ThreadPoolExecutor(max_workers=3) as executor:
            totals_future = executor.submit(
                self._query_rows,
                f"""
                SELECT
                    toString(sum(charged)) AS total_spend,
                    toString(uniqExact(subject_id)) AS active_users
                FROM {self._quoted_table} FINAL
                WHERE event_at >= parseDateTime64BestEffort({{start:String}})
                  AND event_at < parseDateTime64BestEffort({{end:String}})
                """,
                start,
                end,
            )
            models_future = executor.submit(
                self._spend_rows,
                "coalesce(model, 'unknown')",
                start,
                end,
                1,
            )
            users_future = executor.submit(self._spend_rows, "subject_id", start, end, 1)
            totals = totals_future.result()
            models = models_future.result()
            users = users_future.result()
        total = Decimal(str(totals[0]["total_spend"])) if totals else Decimal(0)
        seconds = (end - start).total_seconds()
        days = max(math.ceil(seconds / 86_400), 1)
        return AggregateStatsRow(
            total_credits_consumed=total,
            active_users=int(totals[0]["active_users"]) if totals else 0,
            avg_daily_spend=total / Decimal(days),
            top_model=str(models[0]["key"]) if models else "",
            top_user=str(users[0]["key"]) if users else "",
        )

    def _create_projection_table(self) -> None:
        ttl = "" if self._retention_days is None else f"\nTTL event_at + toIntervalDay({self._retention_days}) DELETE"
        self._client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self._quoted_table} (
                outbox_event_id UInt64,
                charge_id UUID,
                account_id UUID,
                subject_id UUID,
                operation LowCardinality(String),
                feature Nullable(String),
                model Nullable(String),
                region Nullable(String),
                measures String,
                dimensions String,
                metadata String,
                requested Decimal(20, 6),
                charged Decimal(20, 6),
                allowance_requested Decimal(20, 6),
                allowance_covered Decimal(20, 6),
                catalog_revision_id Nullable(UUID),
                plan_id Nullable(UUID),
                rate_card_key Nullable(String),
                pricing_snapshot String,
                ledger_entry_id Nullable(UUID),
                correction_of_charge_id Nullable(UUID),
                idempotency_key String,
                request_digest String,
                event_at DateTime64(6, 'UTC'),
                created_at DateTime64(6, 'UTC'),
                ingested_at DateTime64(6, 'UTC') DEFAULT now64(6)
            )
            ENGINE = ReplacingMergeTree(outbox_event_id)
            PARTITION BY toYYYYMM(event_at)
            ORDER BY (event_at, charge_id){ttl}
            """
        )

    def _spend_rows(
        self,
        key_expression: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        _validate_range(start, end)
        limit_sql = "" if limit is None else f"\nLIMIT {limit}"
        return self._query_rows(
            f"""
            SELECT
                toString({key_expression}) AS key,
                toString(sum(charged)) AS total_spend,
                toString(count()) AS entry_count
            FROM {self._quoted_table} FINAL
            WHERE event_at >= parseDateTime64BestEffort({{start:String}})
              AND event_at < parseDateTime64BestEffort({{end:String}})
            GROUP BY key
            ORDER BY sum(charged) DESC, key{limit_sql}
            """,
            start,
            end,
        )

    def _query_rows(
        self,
        query: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        self.initialize()
        result = self._client.query(
            query,
            parameters={"start": start.isoformat(), "end": end.isoformat()},
        )
        named_results = getattr(result, "named_results", None)
        if callable(named_results):
            rows = cast(Iterable[Mapping[str, Any]], named_results())
            return [dict(row) for row in rows]
        columns = list(result.column_names)
        return [dict(zip(columns, row, strict=True)) for row in result.result_rows]
