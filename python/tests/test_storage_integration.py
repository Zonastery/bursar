"""DB-backed storage repository integration tests for the Python SDK."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import psycopg2
import pytest
from psycopg2.extras import Json

from bursar.credits.types import (
    AggregateStats,
    DailySpendRow,
    SpendByModelRow,
    SpendByUserRow,
    TopUserRow,
)
from bursar.shared.postgres_client import PostgresClient
from bursar.storage import (
    BillingEventPayloadExport,
    BillingPayloadArchiveResult,
    BursarRuntimeOptions,
    OutboxRunResult,
    OutboxWorkerOptions,
    UsageChargeExport,
    create_bursar_runtime,
)
from bursar.storage.postgres_repository import PostgresStorageRepository
from tests.conftest import TEST_TENANT_ID

pytestmark = [pytest.mark.integration]


class RecordingUsageSink:
    def __init__(self) -> None:
        self.initialized = False
        self.writes: list[tuple[UsageChargeExport, str]] = []

    def initialize(self) -> None:
        self.initialized = True

    def write_usage(self, event: UsageChargeExport, outbox_event_id: str) -> None:
        self.writes.append((event, outbox_event_id))

    def spend_by_user(self, start: datetime, end: datetime) -> list[SpendByUserRow]:
        del start, end
        return []

    def spend_by_model(self, start: datetime, end: datetime) -> list[SpendByModelRow]:
        del start, end
        return []

    def top_users(self, limit: int, start: datetime, end: datetime) -> list[TopUserRow]:
        del limit, start, end
        return []

    def daily_spend(self, start: datetime, end: datetime) -> list[DailySpendRow]:
        del start, end
        return []

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStats:
        del start, end
        return AggregateStats(
            total_credits_consumed=Decimal(0),
            active_users=0,
            avg_daily_spend=Decimal(0),
            top_model="",
            top_user="",
        )


class RecordingBillingArchive:
    purge_postgres_payload = True

    def __init__(self) -> None:
        self.events: list[BillingEventPayloadExport] = []
        self.closed = False

    def archive(self, event: BillingEventPayloadExport) -> BillingPayloadArchiveResult:
        self.events.append(event)
        return BillingPayloadArchiveResult(
            key=f"archive/{event.provider_event_id}.json",
            version_id="version-runtime",
        )

    def close(self) -> None:
        self.closed = True


def _seed_storage_rows(pg_database_url: str) -> tuple[str, str]:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('bursar.tenant_id', %s, true)",
            (TEST_TENANT_ID,),
        )
        cursor.execute(
            """
            WITH subject AS (
                INSERT INTO bursar.subjects DEFAULT VALUES
                RETURNING id
            ), account AS (
                INSERT INTO bursar.credit_accounts(subject_id, account_kind)
                SELECT id, 'personal' FROM subject
                RETURNING id, subject_id
            )
            SELECT id, subject_id FROM account
            """
        )
        _account_id, subject_id = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]
        cursor.execute(
            """
            SELECT charge_id, error_code
            FROM bursar.charge_usage(
                %s::uuid,
                'completion',
                0,
                'storage-repo-usage-1',
                p_model => 'small-model',
                p_region => 'in',
                p_measures => '{"input_tokens":12}'::jsonb,
                p_dimensions => '{"tenant_tier":"starter"}'::jsonb,
                p_metadata => '{"trace_id":"trace-1"}'::jsonb
            )
            """,
            (subject_id,),
        )
        charge_id, error_code = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]
        assert error_code is None

        cursor.execute(
            """
            SELECT *
            FROM bursar.claim_billing_event(
                'stripe',
                'evt-storage-repo-1',
                'invoice.paid',
                %s::jsonb
            )
            """,
            (Json({"userId": str(subject_id), "kind": "invoice.paid"}),),
        )
        _claim_status, billing_event_id, billing_claim_token = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]
        cursor.execute(
            """
            SELECT bursar.complete_billing_event(
                'stripe',
                'evt-storage-repo-1',
                %s::uuid
            )
            """,
            (billing_claim_token,),
        )
        assert cursor.fetchone() == (True,)
        return str(charge_id), str(billing_event_id)


def test_postgres_storage_repository_exports_archives_and_acknowledges_outbox(
    pg_database_url: str,
) -> None:
    charge_id, billing_event_id = _seed_storage_rows(pg_database_url)
    client = PostgresClient(pg_database_url, tenant_id=TEST_TENANT_ID, max_connections=2)
    repository = PostgresStorageRepository(client.query, TEST_TENANT_ID)
    try:
        usage = repository.get_usage_charge(charge_id)
        assert usage is not None
        assert usage.charge_id == charge_id
        assert usage.operation == "completion"
        assert usage.model == "small-model"
        assert usage.region == "in"
        assert usage.dimensions["tenant_tier"] == "starter"
        assert usage.metadata == {"trace_id": "trace-1"}

        billing_payload = repository.get_billing_event_payload(billing_event_id)
        assert billing_payload is not None
        assert billing_payload.event_id == billing_event_id
        assert billing_payload.provider == "stripe"
        assert billing_payload.provider_event_id == "evt-storage-repo-1"
        assert billing_payload.event_type == "invoice.paid"
        assert billing_payload.envelope is not None
        assert billing_payload.envelope["kind"] == "invoice.paid"
        assert isinstance(billing_payload.envelope["userId"], str)

        assert repository.archive_billing_event_payload(
            billing_event_id,
            "billing/stripe/evt-storage-repo-1.json",
            "version-1",
            True,
        )
        archived = repository.get_billing_event_payload(billing_event_id)
        assert archived is not None
        assert archived.envelope is None
        assert archived.object_key == "billing/stripe/evt-storage-repo-1.json"
        assert archived.object_version == "version-1"

        claimed = repository.claim(
            ["usage.charge_recorded", "billing.webhook_completed"],
            10,
            60,
        )
        assert sorted(event.topic for event in claimed) == [
            "billing.webhook_completed",
            "usage.charge_recorded",
        ]
        usage_event = next(event for event in claimed if event.topic == "usage.charge_recorded")
        billing_event = next(event for event in claimed if event.topic == "billing.webhook_completed")
        assert repository.complete(usage_event) is True
        assert repository.fail(billing_event, "archive unavailable", 0, 3) is True
    finally:
        client.close()


def test_bursar_runtime_flushes_usage_and_billing_outbox_handlers(
    pg_database_url: str,
) -> None:
    _charge_id, billing_event_id = _seed_storage_rows(pg_database_url)
    clickhouse = RecordingUsageSink()
    archive = RecordingBillingArchive()
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pg_database_url,
            tenant_id=UUID(TEST_TENANT_ID),
            clickhouse=clickhouse,
            s3=archive,
            outbox=OutboxWorkerOptions(batch_size=10, poll_interval_ms=60_000),
        )
    )
    try:
        assert runtime.health().started is False
        assert runtime.flush() == OutboxRunResult(claimed=2, delivered=2, failed=0)
        assert clickhouse.writes[0][0].operation == "completion"
        assert archive.events[0].event_id == billing_event_id

        client = PostgresClient(pg_database_url, tenant_id=TEST_TENANT_ID, max_connections=2)
        try:
            repository = PostgresStorageRepository(client.query, TEST_TENANT_ID)
            archived = repository.get_billing_event_payload(billing_event_id)
            assert archived is not None
            assert archived.envelope is None
            assert archived.object_key == "archive/evt-storage-repo-1.json"
            assert archived.object_version == "version-runtime"
        finally:
            client.close()
    finally:
        runtime.close()
    assert archive.closed is True
    with pytest.raises(RuntimeError, match="closed"):
        runtime.flush()


def test_bursar_runtime_start_health_and_no_worker_flush(
    pg_database_url: str,
) -> None:
    clickhouse = RecordingUsageSink()
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pg_database_url,
            tenant_id=UUID(TEST_TENANT_ID),
            clickhouse=clickhouse,
            outbox=False,
        )
    )
    try:
        runtime.start()
        assert clickhouse.initialized is True
        assert runtime.health().started is True
        assert runtime.flush() == OutboxRunResult(claimed=0, delivered=0, failed=0)
    finally:
        runtime.close()
