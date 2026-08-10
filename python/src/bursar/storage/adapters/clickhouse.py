"""ClickHouse usage projection mirroring JavaScript adapter logic."""

from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from bursar.credits.types import (
    AggregateStats,
    DailySpendRow,
    SpendByModelRow,
    SpendByUserRow,
    TopUserRow,
    UsageCharge,
    UsageChargeCursor,
    UsageChargePage,
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


class ClickHouseUsageStoreOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    client: SkipValidation[ClickHouseClient]
    tenant_id: UUID
    table: str = "bursar_usage_events"
    create_table: bool = False
    retention_days: int | None = Field(
        default=None,
        description="TTL used only by SDK-generated DDL when create_table/initialize_schema is used.",
    )


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
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        msg = f"Invalid usage timestamp: {value}"
        raise ValueError(msg)
    return parsed.astimezone(UTC)


def _optional_uuid(value: str | None) -> UUID | None:
    return UUID(value) if value is not None else None


class ClickHouseUsageStore:
    """Idempotent ClickHouse projection and usage analytics read port."""

    _INSERT_COLUMNS = (
        "tenant_id",
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
        "billing_disposition",
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
    _EXPECTED_SCHEMA_COLUMNS: dict[str, tuple[str, ...]] = {
        "tenant_id": ("UUID",),
        "outbox_event_id": ("UInt64",),
        "charge_id": ("UUID",),
        "account_id": ("UUID",),
        "subject_id": ("UUID",),
        "operation": ("String", "LowCardinality(String)"),
        "feature": ("Nullable(String)", "LowCardinality(Nullable(String))"),
        "model": ("Nullable(String)", "LowCardinality(Nullable(String))"),
        "region": ("Nullable(String)", "LowCardinality(Nullable(String))"),
        "measures": ("String",),
        "dimensions": ("String",),
        "metadata": ("String",),
        "requested": ("Decimal(20,6)",),
        "charged": ("Decimal(20,6)",),
        "allowance_requested": ("Decimal(20,6)",),
        "allowance_covered": ("Decimal(20,6)",),
        "billing_disposition": ("String", "LowCardinality(String)"),
        "catalog_revision_id": ("Nullable(UUID)",),
        "plan_id": ("Nullable(UUID)",),
        "rate_card_key": ("Nullable(String)",),
        "pricing_snapshot": ("String",),
        "ledger_entry_id": ("Nullable(UUID)",),
        "correction_of_charge_id": ("Nullable(UUID)",),
        "idempotency_key": ("String",),
        "request_digest": ("String",),
        "event_at": ("DateTime64(6,'UTC')",),
        "created_at": ("DateTime64(6,'UTC')",),
    }

    def __init__(self, options: ClickHouseUsageStoreOptions) -> None:
        self._client = options.client
        self._tenant_id = options.tenant_id
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
        self._initialized = False

    def initialize(self) -> None:
        if not self._create_table:
            return
        self.initialize_schema()

    def initialize_schema(self) -> None:
        """Explicitly create Bursar's standalone projection schema."""

        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self._create_projection_table()
            self._initialized = True

    def write_usage(self, event: UsageChargeExport, outbox_event_id: str) -> None:
        self.write_usage_batch(((event, outbox_event_id),))

    def write_usage_batch(self, entries: Sequence[tuple[UsageChargeExport, str]]) -> None:
        """Insert a batch in one request while retaining each outbox identity."""

        if not entries:
            return
        rows = [self._project_usage(event, outbox_event_id) for event, outbox_event_id in entries]
        self.initialize()
        self._client.insert(
            self._table,
            rows,
            column_names=self._INSERT_COLUMNS,
        )

    def check_schema_compatibility(self) -> None:
        """Validate the existing schema without creating or modifying it."""

        if "." in self._table:
            database, table_name = self._table.split(".", maxsplit=1)
        else:
            database, table_name = "", self._table
        result = self._client.query(
            """
            SELECT
                c.name,
                c.type,
                t.engine,
                t.engine_full,
                t.sorting_key
            FROM system.columns AS c
            INNER JOIN system.tables AS t
              ON t.database = c.database AND t.name = c.table
            WHERE c.database = if(empty({database:String}), currentDatabase(), {database:String})
              AND c.table = {table_name:String}
            ORDER BY c.position
            """,
            parameters={"database": database, "table_name": table_name},
        )
        rows = self._result_rows(result)
        if not rows:
            msg = f"ClickHouse table {self._table} does not exist"
            raise RuntimeError(msg)

        actual = {str(row["name"]): self._normalize_type(str(row["type"])) for row in rows}
        mismatches: list[str] = []
        for name, expected_types in self._EXPECTED_SCHEMA_COLUMNS.items():
            actual_type = actual.get(name)
            accepted = tuple(self._normalize_type(expected) for expected in expected_types)
            if actual_type is None:
                mismatches.append(f"missing {name}")
            elif actual_type not in accepted:
                mismatches.append(f"{name} is {actual_type}, expected {' or '.join(accepted)}")

        schema = rows[0]
        engine = str(schema.get("engine", ""))
        engine_full = str(schema.get("engine_full", ""))
        sorting_key = str(schema.get("sorting_key", ""))
        if not engine.endswith("ReplacingMergeTree"):
            mismatches.append(f"engine {engine or 'unknown'} is not a ReplacingMergeTree")
        elif not re.search(r"\boutbox_event_id\b", engine_full):
            mismatches.append("ReplacingMergeTree does not use outbox_event_id as its version column")
        for key_column in ("tenant_id", "event_at", "charge_id"):
            if not re.search(rf"\b{key_column}\b", sorting_key):
                mismatches.append(f"sorting key does not include {key_column}")
        if mismatches:
            msg = f"ClickHouse table {self._table} is incompatible: {'; '.join(mismatches)}"
            raise RuntimeError(msg)

    def _project_usage(self, event: UsageChargeExport, outbox_event_id: str) -> tuple[Any, ...]:
        if UUID(event.tenant_id) != self._tenant_id:
            msg = "Usage event tenant_id does not match ClickHouse store tenant_id"
            raise ValueError(msg)
        return (
            UUID(event.tenant_id),
            self._outbox_event_id(outbox_event_id),
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
            event.billing_disposition,
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
            WHERE tenant_id = {{tenant_id:UUID}}
              AND billing_disposition = 'billable'
              AND event_at >= parseDateTime64BestEffort({{start:String}})
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

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStats:
        _validate_range(start, end)
        with ThreadPoolExecutor(max_workers=3) as executor:
            totals_future = executor.submit(
                self._query_rows,
                f"""
                SELECT
                    toString(sum(charged)) AS total_spend,
                    toString(uniqExact(subject_id)) AS active_users
                FROM {self._quoted_table} FINAL
                WHERE tenant_id = {{tenant_id:UUID}}
                  AND billing_disposition = 'billable'
                  AND event_at >= parseDateTime64BestEffort({{start:String}})
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
        return AggregateStats(
            total_credits_consumed=total,
            active_users=int(totals[0]["active_users"]) if totals else 0,
            avg_daily_spend=total / Decimal(days),
            top_model=str(models[0]["key"]) if models else "",
            top_user=str(users[0]["key"]) if users else "",
        )

    def list_usage_charges(
        self,
        user_id: str,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        cursor: UsageChargeCursor | None = None,
        include_record_only: bool = True,
    ) -> UsageChargePage:
        if not user_id:
            raise ValueError("user_id must not be empty")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if from_date is not None and from_date.tzinfo is None:
            raise ValueError("from_date must include timezone")
        if to_date is not None and to_date.tzinfo is None:
            raise ValueError("to_date must include timezone")
        if cursor is not None and (not cursor.event_at or not cursor.usage_id):
            raise ValueError("usage charge cursor requires event_at and usage_id")

        predicates = [
            "tenant_id = {tenant_id:UUID}",
            "subject_id = {subject_id:UUID}",
        ]
        parameters: dict[str, Any] = {
            "tenant_id": str(self._tenant_id),
            "subject_id": user_id,
        }
        if from_date is not None:
            predicates.append("event_at >= parseDateTime64BestEffort({from_date:String})")
            parameters["from_date"] = from_date.isoformat()
        if to_date is not None:
            predicates.append("event_at < parseDateTime64BestEffort({to_date:String})")
            parameters["to_date"] = to_date.isoformat()
        if not include_record_only:
            predicates.append("billing_disposition = 'billable'")
        if cursor is not None:
            predicates.append(
                "(event_at, charge_id) < (parseDateTime64BestEffort({cursor_event_at:String}), {cursor_usage_id:UUID})"
            )
            parameters["cursor_event_at"] = cursor.event_at
            parameters["cursor_usage_id"] = cursor.usage_id

        rows = self._query_rows_with_parameters(
            f"""
            SELECT
                toString(charge_id) AS usage_id,
                toString(account_id) AS account_id,
                operation,
                toString(requested) AS requested,
                toString(charged) AS charged,
                toString(allowance_requested) AS allowance_requested,
                toString(allowance_covered) AS allowance_covered,
                billing_disposition,
                feature,
                model,
                region,
                toString(event_at) AS event_at,
                idempotency_key,
                metadata,
                toString(created_at) AS created_at
            FROM {self._quoted_table} FINAL
            WHERE {" AND ".join(predicates)}
            ORDER BY event_at DESC, charge_id DESC
            LIMIT {limit + 1}
            """,
            parameters,
        )
        has_more = len(rows) > limit
        visible = rows[:limit]
        items = [
            UsageCharge(
                usage_id=str(row["usage_id"]),
                account_id=str(row["account_id"]),
                operation=str(row["operation"]),
                requested=Decimal(str(row["requested"])),
                charged=Decimal(str(row["charged"])),
                allowance_requested=Decimal(str(row["allowance_requested"])),
                allowance_covered=Decimal(str(row["allowance_covered"])),
                billing_disposition=("record_only" if row.get("billing_disposition") == "record_only" else "billable"),
                feature=str(row["feature"]) if row.get("feature") is not None else None,
                model=str(row["model"]) if row.get("model") is not None else None,
                region=str(row["region"]) if row.get("region") is not None else None,
                event_at=self._read_timestamp(str(row["event_at"])).isoformat(),
                idempotency_key=str(row["idempotency_key"]),
                metadata=self._json_object(row.get("metadata")),
                created_at=self._read_timestamp(str(row["created_at"])).isoformat(),
            )
            for row in visible
        ]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = UsageChargeCursor(event_at=last.event_at, usage_id=last.usage_id)
        return UsageChargePage(items=items, next_cursor=next_cursor)

    def _create_projection_table(self) -> None:
        ttl = "" if self._retention_days is None else f"\nTTL event_at + toIntervalDay({self._retention_days}) DELETE"
        self._client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self._quoted_table} (
                tenant_id UUID,
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
                billing_disposition LowCardinality(String) DEFAULT 'billable',
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
            ORDER BY (tenant_id, event_at, charge_id){ttl}
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
            WHERE tenant_id = {{tenant_id:UUID}}
              AND billing_disposition = 'billable'
              AND event_at >= parseDateTime64BestEffort({{start:String}})
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
        return self._query_rows_with_parameters(
            query,
            {
                "tenant_id": str(self._tenant_id),
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )

    def _query_rows_with_parameters(
        self,
        query: str,
        parameters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        self.initialize()
        result = self._client.query(
            query,
            parameters=parameters,
        )
        return self._result_rows(result)

    @staticmethod
    def _result_rows(result: ClickHouseQueryResult) -> list[dict[str, Any]]:
        named_results = getattr(result, "named_results", None)
        if callable(named_results):
            rows = cast(Iterable[Mapping[str, Any]], named_results())
            return [dict(row) for row in rows]
        columns = list(result.column_names)
        return [dict(zip(columns, row, strict=True)) for row in result.result_rows]

    @staticmethod
    def _normalize_type(value: str) -> str:
        return re.sub(r"\s+", "", value)

    @staticmethod
    def _outbox_event_id(value: str) -> int:
        if not re.fullmatch(r"(?:0|[1-9]\d*)", value):
            msg = "ClickHouse outbox_event_id must be an unsigned integer string"
            raise ValueError(msg)
        parsed = int(value)
        if parsed > 18_446_744_073_709_551_615:
            msg = "ClickHouse outbox_event_id exceeds UInt64"
            raise ValueError(msg)
        return parsed

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @classmethod
    def _decimal_object(cls, value: Any) -> dict[str, Decimal]:
        return {
            key: Decimal(str(item))
            for key, item in cls._json_object(value).items()
            if isinstance(item, (int, float, str)) and not isinstance(item, bool)
        }

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return str(value) if value not in (None, "") else None

    @staticmethod
    def _read_timestamp(value: str) -> datetime:
        normalized = value.replace(" ", "T", 1)
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
