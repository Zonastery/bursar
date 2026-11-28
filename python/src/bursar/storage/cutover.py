"""Tenant-scoped, resumable usage storage cutover operator.

A failed run deliberately leaves the tenant paused. Replaying is idempotent;
verification is rebuilt from the source on every run.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from bursar.storage.adapters.clickhouse import ClickHouseUsageStore, ClickHouseUsageStoreOptions
from bursar.storage.json_numbers import json_dump as _json_dump
from bursar.storage.json_numbers import json_load as _json_load
from bursar.storage.ports import UsageChargeExport
from bursar.storage.postgres_repository import PostgresStorageRepository

_JSON_FIELDS = {"measures", "dimensions", "metadata", "pricing_snapshot"}
_DECIMAL_FIELDS = {"requested", "charged", "allowance_requested", "allowance_covered"}
_TIME_FIELDS = {"event_at", "created_at"}


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key in UsageChargeExport.model_fields:
        value = row[key]
        if key in _JSON_FIELDS and isinstance(value, str):
            value = _json_load(value)
        elif key in _DECIMAL_FIELDS:
            value = str(Decimal(str(value)).normalize())
        elif key in _TIME_FIELDS:
            parsed = datetime.fromisoformat(str(value))
            value = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
            value = value.isoformat(timespec="microseconds")
        elif isinstance(value, UUID):
            value = str(value)
        result[key] = value
    return result


def _digest(row: dict[str, Any]) -> str:
    encoded = _json_dump(_normalize(row)).encode()
    return hashlib.sha256(encoded).hexdigest()


def run_cutover(
    connection: Any,
    clickhouse: Any,
    tenant: str,
    target: str,
    cutoff: datetime,
    table: str,
    provider_environment: str = "test",
) -> int:
    """Copy, independently verify every retained record, then atomically flip routing."""
    tenant = str(UUID(tenant))
    store = ClickHouseUsageStore(ClickHouseUsageStoreOptions(client=clickhouse, tenant_id=UUID(tenant), table=table))
    store.check_schema_compatibility()
    from psycopg2.extras import register_default_json, register_default_jsonb

    register_default_json(connection, loads=_json_load)
    register_default_jsonb(connection, loads=_json_load)
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE bursar_operator")
        cursor.execute(
            "SELECT pg_try_advisory_lock(hashtextextended('bursar.usage.cutover.driver:' || %s, 0))", (tenant,)
        )
        if not cursor.fetchone()[0]:
            raise RuntimeError("another cutover driver is running for this tenant")
        try:
            # The bind RPC verifies both tenant state and its fixed provider
            # environment before the cutover can pause usage routing.
            cursor.execute("SELECT bursar.bind_tenant_context(%s::uuid, %s)", (tenant, provider_environment))
            cursor.execute("SELECT bursar.begin_usage_backend_cutover(%s::uuid, %s, %s)", (tenant, target, cutoff))
            status = cursor.fetchone()[0]
            cutoff = datetime.fromisoformat(status["cutoff"])
            source = status["backend"]
            _drain(connection, store, tenant, provider_environment)
            if _clickhouse_has_earlier_rows(store, cutoff):
                raise RuntimeError(
                    "ClickHouse contains retained usage before the cutoff; "
                    "abort the cutover and restart with an earlier cutoff"
                )
            count = 0
            after_event = after_charge = None
            while True:
                if source == "postgres":
                    cursor.execute(
                        "SELECT * FROM bursar.list_usage_cutover_postgres(%s::uuid,%s::timestamptz,%s::uuid,%s)",
                        (tenant, after_event, after_charge, 500),
                    )
                    rows = [row[0] for row in cursor.fetchall()]
                else:
                    rows = _clickhouse_page(store, cutoff, after_event, after_charge)
                if not rows:
                    break
                for row in rows:
                    if row.pop("payload_available", True) is not True:
                        raise RuntimeError("retained PostgreSQL usage payload is missing; restore it before cutover")
                    item = UsageChargeExport.model_validate(row).model_dump()
                    if source == "postgres":
                        store.write_usage(UsageChargeExport.model_validate(item), "0")
                        matching = _clickhouse_item(store, item)
                    else:
                        cursor.execute(
                            "SELECT bursar.import_usage_cutover_item(%s::uuid,%s::jsonb)", (tenant, _json_dump(item))
                        )
                        matching = cursor.fetchone()[0]
                        if matching and matching.pop("payload_available", True) is not True:
                            raise RuntimeError("imported PostgreSQL usage payload is missing")
                    if not matching:
                        raise RuntimeError(f"usage verification failed for charge {item['charge_id']}: missing target")
                    expected, actual = _normalize(item), _normalize(matching)
                    differing = [key for key in expected if expected[key] != actual[key]]
                    if differing:
                        raise RuntimeError(f"usage verification failed for charge {item['charge_id']}: {differing}")
                    cursor.execute(
                        "SELECT bursar.verify_usage_cutover_item(%s::uuid,%s::uuid,%s::timestamptz,%s)",
                        (tenant, item["charge_id"], item["event_at"], _digest(item)),
                    )
                    count += 1
                after_event, after_charge = rows[-1]["event_at"], rows[-1]["charge_id"]
            if source == "postgres" and _clickhouse_count(store, cutoff) != count:
                raise RuntimeError(
                    "ClickHouse contains extra retained usage records; reconcile the target before retrying"
                )
            cursor.execute(
                "SELECT bursar.finish_usage_backend_cutover(%s::uuid,%s,%s::bigint)", (tenant, target, count)
            )
            return count
        finally:
            cursor.execute(
                "SELECT pg_advisory_unlock(hashtextextended('bursar.usage.cutover.driver:' || %s, 0))", (tenant,)
            )


def _clickhouse_has_earlier_rows(store: ClickHouseUsageStore, cutoff: datetime) -> bool:
    result = store._client.query(
        f"SELECT count() FROM {store._quoted_table} FINAL "  # noqa: S608
        "WHERE tenant_id = {tenant:UUID} AND event_at < {cutoff:DateTime64(6, 'UTC')}",
        parameters={"tenant": str(store._tenant_id), "cutoff_64": cutoff},
    )
    return int(result.result_rows[0][0]) > 0


def _clickhouse_count(store: ClickHouseUsageStore, cutoff: datetime) -> int:
    result = store._client.query(
        f"SELECT count() FROM {store._quoted_table} FINAL "  # noqa: S608
        "WHERE tenant_id = {tenant:UUID} AND event_at >= {cutoff:DateTime64(6, 'UTC')}",
        parameters={"tenant": str(store._tenant_id), "cutoff_64": cutoff},
    )
    return int(result.result_rows[0][0])


def _clickhouse_page(
    store: ClickHouseUsageStore, cutoff: datetime, after_event: Any, after_charge: Any
) -> list[dict[str, Any]]:
    columns = ", ".join(name for name in store._INSERT_COLUMNS if name != "outbox_event_id")
    result = store._client.query(
        f"SELECT {columns} FROM {store._quoted_table} FINAL "  # noqa: S608
        "WHERE tenant_id = {tenant:UUID} AND event_at >= {cutoff:DateTime64(6, 'UTC')} "
        "AND ({first:UInt8} = 1 OR (event_at, charge_id) > "
        "({event:DateTime64(6, 'UTC')}, {charge:UUID})) ORDER BY event_at, charge_id LIMIT 500",
        parameters={
            "tenant": str(store._tenant_id),
            "cutoff_64": cutoff,
            "first": int(after_event is None),
            "event_64": datetime.fromisoformat(after_event) if after_event else cutoff,
            "charge": after_charge or "00000000-0000-0000-0000-000000000000",
        },
    )
    return [_normalize(row) for row in store._result_rows(result)]


def _clickhouse_item(store: ClickHouseUsageStore, item: dict[str, Any]) -> dict[str, Any] | None:
    columns = ", ".join(name for name in store._INSERT_COLUMNS if name != "outbox_event_id")
    result = store._client.query(
        f"SELECT {columns} FROM {store._quoted_table} FINAL "  # noqa: S608
        "WHERE tenant_id = {tenant:UUID} AND event_at = {event:DateTime64(6, 'UTC')} "
        "AND charge_id = {charge:UUID}",
        parameters={
            "tenant": str(store._tenant_id),
            "event_64": datetime.fromisoformat(item["event_at"]),
            "charge": item["charge_id"],
        },
    )
    rows = store._result_rows(result)
    if len(rows) > 1:
        raise RuntimeError("duplicate ClickHouse usage identity")
    return rows[0] if rows else None


def _drain(connection: Any, store: ClickHouseUsageStore, tenant: str, provider_environment: str) -> None:
    from psycopg2.extras import RealDictCursor

    def query(text: str, params: list[Any] | None = None) -> list[Any]:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("BEGIN")
            try:
                cursor.execute("SELECT bursar.bind_tenant_context(%s::uuid, %s)", (tenant, provider_environment))
                cursor.execute(text, params)
                rows = cursor.fetchall()
                cursor.execute("COMMIT")
                return rows
            except BaseException:
                cursor.execute("ROLLBACK")
                raise

    repository = PostgresStorageRepository(query, tenant)
    while events := repository.claim(["usage.charge_recorded"], 100, 300):
        for event in events:
            if event.payload_version != 1:
                raise RuntimeError("unsupported usage outbox payload version")
            if event.payload.get("delivery_required") is False:
                # PostgreSQL-mode notifications carry no required external
                # payload. The verified source scan copies retained details;
                # an expired optional pointer must not block the cutover.
                if not repository.complete(event):
                    raise RuntimeError("usage outbox claim was lost; retry cutover")
                continue
            payload = {key: value for key, value in event.payload.items() if key != "delivery_required"}
            usage = (
                UsageChargeExport.model_validate(payload)
                if UsageChargeExport.model_fields.keys() <= payload.keys()
                else repository.get_usage_charge(event.aggregate_id)
            )
            if usage is None or usage.tenant_id != tenant or usage.charge_id != event.aggregate_id:
                raise RuntimeError("usage outbox event has no matching tenant charge")
            if usage.request_digest.startswith("\\x"):
                usage = usage.model_copy(update={"request_digest": usage.request_digest[2:]})
            store.write_usage(usage, event.event_id)
            if not repository.complete(event):
                raise RuntimeError("usage outbox claim was lost; retry cutover")
