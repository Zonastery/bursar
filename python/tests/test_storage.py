"""Parity tests for JavaScript ``tests/storage.test.ts``."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest

from bursar import PricingNotLoadedError
from bursar.storage import (
    BillingEventPayloadExport,
    BursarRuntimeOptions,
    BursarRuntimeStartOptions,
    ClickHouseUsageStore,
    ClickHouseUsageStoreOptions,
    OutboxEvent,
    OutboxRunResult,
    OutboxWorker,
    OutboxWorkerOptions,
    S3BillingArchive,
    S3BillingArchiveOptions,
    S3Credentials,
    UsageChargeExport,
    create_bursar_runtime,
)

TENANT_ID = "00000000-0000-0000-0000-000000000001"

OUTBOX_EVENT = OutboxEvent(
    event_id="42",
    tenant_id=TENANT_ID,
    topic="usage.charge_recorded",
    aggregate_type="credit_usage_charge",
    aggregate_id="00000000-0000-0000-0000-000000000042",
    payload_version=1,
    payload={},
    claim_token="00000000-0000-0000-0000-000000000099",
    attempt_count=2,
    created_at="2026-07-29T00:00:00.000Z",
)


class FakeOutboxStore:
    def __init__(self, events: list[OutboxEvent]) -> None:
        self.events = events
        self.claim_calls: list[tuple[Sequence[str], int, int]] = []
        self.completed: list[OutboxEvent] = []
        self.failed: list[tuple[OutboxEvent, str, int, int]] = []

    def claim(
        self,
        topics: Sequence[str],
        limit: int,
        lease_seconds: int,
    ) -> list[OutboxEvent]:
        self.claim_calls.append((topics, limit, lease_seconds))
        return self.events

    def complete(self, event: OutboxEvent) -> bool:
        self.completed.append(event)
        return True

    def fail(
        self,
        event: OutboxEvent,
        error: str,
        retry_delay_seconds: int,
        attempt_limit: int,
    ) -> bool:
        self.failed.append((event, error, retry_delay_seconds, attempt_limit))
        return True


@dataclass(frozen=True)
class FakeHandler:
    topics: Sequence[str]
    callback: Any

    def handle(self, event: OutboxEvent) -> None:
        self.callback(event)


class FakeClickHouseResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.column_names: Sequence[str] = tuple(rows[0]) if rows else ()
        self.result_rows: Sequence[Sequence[Any]] = [tuple(row.values()) for row in rows]


class FakeClickHouseClient:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.inserts: list[tuple[str, Sequence[Sequence[Any]], Sequence[str]]] = []
        self.queries: list[str] = []
        self.query_rows = [
            {
                "key": "00000000-0000-0000-0000-000000000007",
                "total_spend": "12.5",
                "entry_count": "2",
            }
        ]

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
        assert query
        assert parameters is not None
        self.queries.append(query)
        return FakeClickHouseResult(self.query_rows)


class FakePool:
    def __init__(self) -> None:
        self.closeall = Mock()

    def getconn(self) -> Any:
        msg = "PostgreSQL should not be queried in this test"
        raise AssertionError(msg)

    def putconn(self, conn: Any) -> None:
        del conn
        return None


def _usage_export() -> UsageChargeExport:
    return UsageChargeExport(
        tenant_id=TENANT_ID,
        charge_id="00000000-0000-0000-0000-000000000042",
        account_id="00000000-0000-0000-0000-000000000006",
        subject_id="00000000-0000-0000-0000-000000000007",
        operation="generate",
        feature="chat",
        model="gpt",
        region=None,
        measures={"tokens": 10},
        dimensions={"workspace": "one"},
        metadata={},
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
        idempotency_key="job:42",
        request_digest="\\x1234",
        event_at="2026-07-29T12:00:00.000Z",
        created_at="2026-07-29T12:00:00.000Z",
    )


def test_outbox_worker_claims_registered_topics_and_acknowledges() -> None:
    store = FakeOutboxStore([OUTBOX_EVENT])
    handled: list[OutboxEvent] = []
    worker = OutboxWorker(
        store,
        [FakeHandler(("usage.charge_recorded",), handled.append)],
    )

    assert worker.run_once() == OutboxRunResult(claimed=1, delivered=1, failed=0)
    assert store.claim_calls == [(["usage.charge_recorded"], 100, 60)]
    assert handled == [OUTBOX_EVENT]
    assert store.completed == [OUTBOX_EVENT]
    assert store.failed == []


def test_outbox_worker_releases_failure_with_bounded_backoff() -> None:
    store = FakeOutboxStore([OUTBOX_EVENT])

    def fail_handler(_event: OutboxEvent) -> None:
        msg = "ClickHouse unavailable"
        raise RuntimeError(msg)

    worker = OutboxWorker(
        store,
        [FakeHandler(("usage.charge_recorded",), fail_handler)],
        OutboxWorkerOptions(
            retry_delay_seconds=5,
            max_retry_delay_seconds=20,
            attempt_limit=7,
        ),
    )

    assert worker.run_once() == OutboxRunResult(claimed=1, delivered=0, failed=1)
    assert store.completed == []
    assert store.failed == [
        (
            OUTBOX_EVENT,
            "RuntimeError: ClickHouse unavailable",
            10,
            7,
        )
    ]


def test_s3_archive_uses_deterministic_key_and_preserves_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Mock()
    client.put_object.return_value = {"VersionId": "v1"}
    boto3_client = Mock(return_value=client)
    monkeypatch.setattr("boto3.client", boto3_client)
    archive = S3BillingArchive(
        S3BillingArchiveOptions(
            bucket="billing-archive",
            region="us-east-1",
            credentials=S3Credentials(
                access_key_id="access-key",
                secret_access_key="secret-key",
            ),
            prefix="/tenant-a/",
        )
    )
    result = archive.archive(
        BillingEventPayloadExport(
            tenant_id=TENANT_ID,
            event_id="00000000-0000-0000-0000-000000000001",
            provider="stripe",
            provider_environment="live",
            provider_event_id="evt_1",
            event_type="invoice.paid",
            status="completed",
            received_at="2026-07-29T12:30:00.000Z",
            completed_at="2026-07-29T12:30:01.000Z",
            envelope={"id": "evt_1", "data": {"amount": 1200}},
            object_key=None,
            object_version=None,
            archived_at=None,
        )
    )

    assert result.key == (
        "tenant-a/tenants/00000000-0000-0000-0000-000000000001/"
        "billing-events/2026/07/29/00000000-0000-0000-0000-000000000001.json"
    )
    assert result.version_id == "v1"
    boto3_client.assert_called_once()
    request = client.put_object.call_args.kwargs
    assert request["Bucket"] == "billing-archive"
    assert request["ContentType"] == "application/json"
    assert json.loads(request["Body"])["envelope"] == {
        "id": "evt_1",
        "data": {"amount": 1200},
    }
    archive.close()
    client.close.assert_called_once_with()


def test_clickhouse_writes_projection_and_serves_analytics() -> None:
    client = FakeClickHouseClient()
    store = ClickHouseUsageStore(
        ClickHouseUsageStoreOptions(
            client=client,
            tenant_id=UUID(TENANT_ID),
            create_table=False,
        )
    )
    usage = _usage_export()

    store.write_usage(usage, "99")
    table, rows, columns = client.inserts[0]
    projected = dict(zip(columns, rows[0], strict=True))
    assert table == "bursar_usage_events"
    assert str(projected["tenant_id"]) == TENANT_ID
    assert projected["outbox_event_id"] == 99
    assert str(projected["charge_id"]) == usage.charge_id
    assert str(projected["charged"]) == usage.charged

    analytics = store.spend_by_user(
        datetime(2026, 7, 1, tzinfo=UTC),
        datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert analytics[0].user_id == usage.subject_id
    assert str(analytics[0].total_spend) == "12.5"
    assert analytics[0].entry_count == 2
    assert "billing_disposition = 'billable'" in client.queries[-1]


def test_clickhouse_rejects_usage_timestamps_without_a_timezone() -> None:
    client = FakeClickHouseClient()
    store = ClickHouseUsageStore(
        ClickHouseUsageStoreOptions(
            client=client,
            tenant_id=UUID(TENANT_ID),
            create_table=False,
        )
    )

    with pytest.raises(ValueError, match="Invalid usage timestamp"):
        store.write_usage(
            _usage_export().model_copy(update={"event_at": "2026-07-29T12:00:00"}),
            "99",
        )


def test_clickhouse_serves_usage_history_with_a_cursor() -> None:
    client = FakeClickHouseClient()
    client.query_rows = [
        {
            "usage_id": "00000000-0000-0000-0000-000000000042",
            "account_id": "00000000-0000-0000-0000-000000000006",
            "operation": "completion",
            "requested": "15.000000",
            "charged": "12.500000",
            "allowance_requested": "2.500000",
            "allowance_covered": "2.500000",
            "feature": "chat",
            "model": "gpt",
            "region": None,
            "event_at": "2026-07-29 12:00:00.000000",
            "idempotency_key": "job:42",
            "metadata": '{"trace_id":"trace-42"}',
            "created_at": "2026-07-29 12:00:00.000000",
        }
    ]
    store = ClickHouseUsageStore(
        ClickHouseUsageStoreOptions(
            client=client,
            tenant_id=UUID(TENANT_ID),
            create_table=False,
        )
    )

    page = store.list_usage_charges(
        "00000000-0000-0000-0000-000000000007",
        limit=10,
        include_record_only=False,
    )

    assert page.next_cursor is None
    assert page.items[0].usage_id == _usage_export().charge_id
    assert page.items[0].metadata == {"trace_id": "trace-42"}
    assert str(page.items[0].charged) == "12.500000"
    assert "billing_disposition = 'billable'" in client.queries[-1]


def test_runtime_postgres_only_has_no_worker_or_external_dependency() -> None:
    pool = FakePool()
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pool,
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        )
    )

    assert runtime.worker is None
    assert runtime.clickhouse is None
    assert runtime.s3 is None
    runtime.start()
    assert runtime.flush() == OutboxRunResult(claimed=0, delivered=0, failed=0)
    runtime.close()
    pool.closeall.assert_not_called()


def test_runtime_retries_a_catalog_that_has_not_been_published_yet() -> None:
    pool = FakePool()
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pool,
            tenant_id=UUID(TENANT_ID),
        )
    )
    load_catalog = Mock(side_effect=[PricingNotLoadedError("catalog pending"), None])
    runtime.bursar.load_catalog = load_catalog

    runtime.start(
        BursarRuntimeStartOptions(
            load_catalog=True,
            max_attempts=2,
            retry_delay_seconds=0,
        )
    )

    assert load_catalog.call_count == 2
    assert runtime.health().started is True
    runtime.close()


def test_runtime_routes_analytics_through_clickhouse_without_changing_store() -> None:
    pool = FakePool()
    client = FakeClickHouseClient()
    client.query_rows = [
        {
            "key": "00000000-0000-0000-0000-000000000009",
            "total_spend": "4",
            "entry_count": "1",
        }
    ]
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pool,
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            clickhouse=ClickHouseUsageStoreOptions(
                client=client,
                tenant_id=UUID(TENANT_ID),
                create_table=False,
            ),
            outbox=False,
        )
    )

    rows = runtime.bursar.credits.spend_by_user(
        datetime(2026, 7, 1, tzinfo=UTC),
        datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert str(rows[0].total_spend) == "4"
    runtime.close()
    pool.closeall.assert_not_called()


def test_runtime_routes_usage_history_through_clickhouse() -> None:
    pool = FakePool()
    client = FakeClickHouseClient()
    client.query_rows = [
        {
            "usage_id": "00000000-0000-0000-0000-000000000042",
            "account_id": "00000000-0000-0000-0000-000000000006",
            "operation": "completion",
            "requested": "15.000000",
            "charged": "12.500000",
            "allowance_requested": "2.500000",
            "allowance_covered": "2.500000",
            "feature": "chat",
            "model": "gpt",
            "region": None,
            "event_at": "2026-07-29 12:00:00.000000",
            "idempotency_key": "job:42",
            "metadata": '{"trace_id":"trace-42"}',
            "created_at": "2026-07-29 12:00:00.000000",
        }
    ]
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pool,
            tenant_id=UUID(TENANT_ID),
            clickhouse=ClickHouseUsageStoreOptions(
                client=client,
                tenant_id=UUID(TENANT_ID),
                create_table=False,
            ),
            outbox=False,
        )
    )

    page = runtime.bursar.credits.list_usage_charges(
        "00000000-0000-0000-0000-000000000007",
        limit=10,
    )

    assert page.items[0].metadata == {"trace_id": "trace-42"}
    runtime.close()
    pool.closeall.assert_not_called()
