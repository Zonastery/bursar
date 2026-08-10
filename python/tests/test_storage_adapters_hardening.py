"""Focused parity tests for optional S3 and ClickHouse adapter hardening."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from types import ModuleType
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest

from bursar.storage.adapters.clickhouse import ClickHouseUsageStore, ClickHouseUsageStoreOptions
from bursar.storage.adapters.s3 import S3BillingArchive, S3BillingArchiveOptions
from bursar.storage.ports import BillingEventPayloadExport, UsageChargeExport

TENANT_ID = "00000000-0000-0000-0000-000000000001"


class FakeClickHouseResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.column_names: Sequence[str] = tuple(rows[0]) if rows else ()
        self.result_rows: Sequence[Sequence[Any]] = [tuple(row.values()) for row in rows]


class FakeClickHouseClient:
    def __init__(self, schema_rows: list[dict[str, Any]] | None = None) -> None:
        self.commands: list[str] = []
        self.inserts: list[tuple[str, Sequence[Sequence[Any]], Sequence[str]]] = []
        self.queries: list[tuple[str, dict[str, Any] | None]] = []
        self.schema_rows = schema_rows or []

    def command(self, query: str) -> None:
        self.commands.append(query)

    def insert(
        self,
        table: str,
        data: Sequence[Sequence[Any]],
        *,
        column_names: Sequence[str],
    ) -> None:
        self.inserts.append((table, data, column_names))

    def query(
        self,
        query: str,
        *,
        parameters: dict[str, Any] | None = None,
    ) -> FakeClickHouseResult:
        self.queries.append((query, parameters))
        return FakeClickHouseResult(self.schema_rows)


def _billing_event() -> BillingEventPayloadExport:
    return BillingEventPayloadExport(
        tenant_id=TENANT_ID,
        event_id="00000000-0000-0000-0000-000000000011",
        provider="stripe",
        provider_environment="live",
        provider_event_id="evt_11",
        event_type="invoice.paid",
        status="completed",
        received_at="2026-07-29T12:30:00.000Z",
        completed_at="2026-07-29T12:30:01.000Z",
        envelope={"id": "evt_11"},
    )


def _usage_event(charge_id: str = "00000000-0000-0000-0000-000000000042") -> UsageChargeExport:
    return UsageChargeExport(
        tenant_id=TENANT_ID,
        charge_id=charge_id,
        account_id="00000000-0000-0000-0000-000000000006",
        subject_id="00000000-0000-0000-0000-000000000007",
        operation="generate",
        feature="chat",
        model="gpt",
        region=None,
        measures={"tokens": 10},
        dimensions={"workspace": "one"},
        metadata={"source": "test"},
        requested="15.000000",
        charged="12.500000",
        allowance_requested="2.500000",
        allowance_covered="2.500000",
        catalog_revision_id=None,
        plan_id=None,
        rate_card_key="standard",
        pricing_snapshot={},
        ledger_entry_id=None,
        correction_of_charge_id=None,
        idempotency_key=f"usage:{charge_id}",
        request_digest="\\x1234",
        event_at="2026-07-29T12:00:00.000Z",
        created_at="2026-07-29T12:00:00.000Z",
    )


def _install_boto_modules(monkeypatch: pytest.MonkeyPatch, client: Mock) -> Mock:
    boto3_client = Mock(return_value=client)
    boto3 = ModuleType("boto3")
    boto3.client = boto3_client  # type: ignore[attr-defined]
    botocore = ModuleType("botocore")
    botocore_config = ModuleType("botocore.config")
    botocore_config.Config = Mock  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config)
    return boto3_client


def _compatible_schema_rows() -> list[dict[str, Any]]:
    columns = {
        "tenant_id": "UUID",
        "outbox_event_id": "UInt64",
        "charge_id": "UUID",
        "account_id": "UUID",
        "subject_id": "UUID",
        "operation": "LowCardinality(String)",
        "feature": "Nullable(String)",
        "model": "Nullable(String)",
        "region": "Nullable(String)",
        "measures": "String",
        "dimensions": "String",
        "metadata": "String",
        "requested": "Decimal(20, 6)",
        "charged": "Decimal(20, 6)",
        "allowance_requested": "Decimal(20, 6)",
        "allowance_covered": "Decimal(20, 6)",
        "billing_disposition": "LowCardinality(String)",
        "catalog_revision_id": "Nullable(UUID)",
        "plan_id": "Nullable(UUID)",
        "rate_card_key": "Nullable(String)",
        "pricing_snapshot": "String",
        "ledger_entry_id": "Nullable(UUID)",
        "correction_of_charge_id": "Nullable(UUID)",
        "idempotency_key": "String",
        "request_digest": "String",
        "event_at": "DateTime64(6, 'UTC')",
        "created_at": "DateTime64(6, 'UTC')",
    }
    return [
        {
            "name": name,
            "type": column_type,
            "engine": "ReplicatedReplacingMergeTree",
            "engine_full": "ReplicatedReplacingMergeTree('/tables/x', 'replica', outbox_event_id)",
            "sorting_key": "(tenant_id, event_at, charge_id)",
        }
        for name, column_type in columns.items()
    ]


def test_s3_uses_default_chains_and_safe_request_options(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Mock()
    client.put_object.return_value = {"VersionId": "v1"}
    boto3_client = _install_boto_modules(monkeypatch, client)
    archive = S3BillingArchive(
        S3BillingArchiveOptions(
            bucket="billing-archive",
            put_object={
                "server_side_encryption": "aws:kms",
                "sse_kms_key_id": "alias/bursar",
                "bucket_key_enabled": True,
                "checksum_algorithm": "SHA256",
            },
        )
    )

    archive.archive(_billing_event())

    client_kwargs = boto3_client.call_args.kwargs
    assert "aws_access_key_id" not in client_kwargs
    assert "aws_secret_access_key" not in client_kwargs
    assert "region_name" not in client_kwargs
    request = client.put_object.call_args.kwargs
    assert request["ServerSideEncryption"] == "aws:kms"
    assert request["SSEKMSKeyId"] == "alias/bursar"
    assert request["BucketKeyEnabled"] is True
    assert request["ChecksumAlgorithm"] == "SHA256"
    archive.close()
    client.close.assert_called_once_with()


def test_s3_does_not_close_an_injected_client_by_default() -> None:
    client = Mock()
    client.put_object.return_value = {}
    archive = S3BillingArchive(S3BillingArchiveOptions(bucket="billing-archive", client=client))

    archive.archive(_billing_event())
    archive.close()

    client.close.assert_not_called()


def test_s3_lazily_creates_and_closes_a_factory_owned_client() -> None:
    client = Mock()
    client.put_object.return_value = {}
    client_factory = Mock(return_value=client)
    archive = S3BillingArchive(S3BillingArchiveOptions(bucket="billing-archive", client_factory=client_factory))

    client_factory.assert_not_called()
    archive.archive(_billing_event())
    archive.close()

    client_factory.assert_called_once_with()
    client.close.assert_called_once_with()


def test_clickhouse_batches_rows_and_preserves_outbox_event_ids() -> None:
    client = FakeClickHouseClient()
    store = ClickHouseUsageStore(ClickHouseUsageStoreOptions(client=client, tenant_id=UUID(TENANT_ID)))
    first = _usage_event()
    second = _usage_event("00000000-0000-0000-0000-000000000043")

    store.write_usage_batch(((first, "99"), (second, "100")))

    assert client.commands == []
    assert len(client.inserts) == 1
    _, rows, columns = client.inserts[0]
    projected = [dict(zip(columns, row, strict=True)) for row in rows]
    assert [row["outbox_event_id"] for row in projected] == [99, 100]
    assert [str(row["charge_id"]) for row in projected] == [first.charge_id, second.charge_id]


def test_clickhouse_schema_lifecycle_is_explicit_and_topology_neutral() -> None:
    client = FakeClickHouseClient()
    store = ClickHouseUsageStore(ClickHouseUsageStoreOptions(client=client, tenant_id=UUID(TENANT_ID)))

    store.initialize()
    assert client.commands == []

    store.initialize_schema()
    store.initialize_schema()
    assert len(client.commands) == 1
    assert "ENGINE = ReplacingMergeTree(outbox_event_id)" in client.commands[0]
    assert "ReplicatedReplacingMergeTree" not in client.commands[0]


def test_clickhouse_checks_schema_without_prescribing_replica_topology() -> None:
    rows = _compatible_schema_rows()
    client = FakeClickHouseClient(rows)
    store = ClickHouseUsageStore(
        ClickHouseUsageStoreOptions(
            client=client,
            tenant_id=UUID(TENANT_ID),
            table="analytics.bursar_usage_events",
        )
    )

    store.check_schema_compatibility()
    assert client.queries[-1][1] == {
        "database": "analytics",
        "table_name": "bursar_usage_events",
    }

    next(row for row in rows if row["name"] == "outbox_event_id")["type"] = "String"
    with pytest.raises(RuntimeError, match="outbox_event_id is String"):
        store.check_schema_compatibility()


def test_clickhouse_single_write_delegates_to_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    store = ClickHouseUsageStore(ClickHouseUsageStoreOptions(client=FakeClickHouseClient(), tenant_id=UUID(TENANT_ID)))
    write_batch = Mock()
    monkeypatch.setattr(store, "write_usage_batch", write_batch)
    event = _usage_event()

    store.write_usage(event, "101")

    write_batch.assert_called_once_with(((event, "101"),))
