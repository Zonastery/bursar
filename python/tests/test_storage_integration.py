"""DB-backed storage repository integration tests for the Python SDK."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import psycopg2
import pytest
from psycopg2.extras import Json

from bursar.billing.postgres.store import PostgresBillingStore
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
    BursarRuntimeStartOptions,
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
        cursor.execute("SELECT set_config('bursar.provider_environment', 'test', true)")
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
            (Json({"accountId": str(subject_id), "kind": "invoice.paid"}),),
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
    client = PostgresClient(
        pg_database_url,
        tenant_id=TEST_TENANT_ID,
        access_role="bursar_operator",
        max_connections=2,
    )
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
        # Inspect the private outbox as the migration owner. The operator-facing
        # repository intentionally exposes RPCs, not direct table access.
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM bursar.event_outbox WHERE aggregate_id = %s::uuid",
                (charge_id,),
            )
            usage_outbox_row = cursor.fetchone()
        assert usage_outbox_row is not None
        usage_outbox = usage_outbox_row[0]
        assert usage_outbox == {
            "delivery_required": False,
            "tenant_id": TEST_TENANT_ID,
            "charge_id": charge_id,
            "account_id": usage.account_id,
            "event_at": usage.event_at,
            "created_at": usage.created_at,
        }

        billing_payload = repository.get_billing_event_payload(billing_event_id)
        assert billing_payload is not None
        assert billing_payload.event_id == billing_event_id
        assert billing_payload.provider == "stripe"
        assert billing_payload.provider_event_id == "evt-storage-repo-1"
        assert billing_payload.event_type == "invoice.paid"
        assert billing_payload.envelope is not None
        assert billing_payload.envelope["kind"] == "invoice.paid"
        assert isinstance(billing_payload.envelope["accountId"], str)
        assert "userId" not in billing_payload.envelope

        assert repository.archive_billing_event_payload(
            billing_event_id,
            "billing/stripe/evt-storage-repo-1.json",
            "version-1",
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
        assert repository.fail(billing_event, "outbox_delivery_failed:RuntimeError", 0, 3) is True
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
            provider_environment="test",
            clickhouse=clickhouse,
            s3=archive,
            outbox=OutboxWorkerOptions(batch_size=10, poll_interval_ms=60_000),
        )
    )
    try:
        assert runtime.health().started is False
        assert runtime.flush() == OutboxRunResult(claimed=2, delivered=2, failed=0, claim_lost=0)
        assert clickhouse.writes[0][0].operation == "completion"
        assert archive.events[0].event_id == billing_event_id

        client = PostgresClient(
            pg_database_url,
            tenant_id=TEST_TENANT_ID,
            access_role="bursar_operator",
            max_connections=2,
        )
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
            provider_environment="test",
            clickhouse=clickhouse,
            outbox=False,
        )
    )
    try:
        runtime.start(BursarRuntimeStartOptions(load_catalog=False))
        assert clickhouse.initialized is True
        assert runtime.health().started is True
        assert runtime.flush() == OutboxRunResult(claimed=0, delivered=0, failed=0, claim_lost=0)
    finally:
        runtime.close()


def test_clickhouse_usage_mode_keeps_only_receipt_and_outbox_payload(
    pg_database_url: str,
) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT set_config('bursar.tenant_id', %s, true)", (TEST_TENANT_ID,))
        cursor.execute("SELECT set_config('bursar.usage_backend', 'clickhouse', true)")
        cursor.execute(
            """
            WITH subject AS (
                INSERT INTO bursar.subjects DEFAULT VALUES RETURNING id
            ), account AS (
                INSERT INTO bursar.credit_accounts(subject_id, account_kind)
                SELECT id, 'personal' FROM subject
                RETURNING subject_id
            )
            SELECT subject_id FROM account
            """
        )
        subject_row = cursor.fetchone()
        assert subject_row is not None
        subject_id = subject_row[0]
        cursor.execute(
            """
            SELECT charge_id, error_code
            FROM bursar.charge_usage(
                %s::uuid, 'completion', 0, 'clickhouse-mode-usage-1',
                p_measures => '{"tokens": 1}'::jsonb,
                p_dimensions => '{"workspace": "one"}'::jsonb,
                p_metadata => '{"trace_id": "trace-ch"}'::jsonb
            )
            """,
            (subject_id,),
        )
        charge_row = cursor.fetchone()
        assert charge_row is not None
        charge_id, error_code = charge_row
        assert error_code is None
        cursor.execute(
            "SELECT count(*) FROM bursar.usage_charge_payloads WHERE charge_id = %s",
            (charge_id,),
        )
        payload_count = cursor.fetchone()
        assert payload_count is not None
        assert payload_count[0] == 0
        cursor.execute(
            """
            SELECT count(*)
            FROM bursar.usage_daily_rollups
            WHERE account_id = (
                SELECT account_id
                FROM bursar.credit_usage_charges
                WHERE id = %s
            )
            """,
            (charge_id,),
        )
        rollup_count = cursor.fetchone()
        assert rollup_count is not None
        assert rollup_count[0] == 0
        cursor.execute(
            """
            SELECT payload->'metadata'->>'trace_id', payload->'dimensions'
            FROM bursar.event_outbox
            WHERE aggregate_id = %s
            """,
            (charge_id,),
        )
        payload = cursor.fetchone()
        assert payload is not None
        assert payload[0] == "trace-ch"
        assert "workspace" in payload[1]


def test_s3_billing_mode_keeps_envelope_only_in_outbox(
    pg_database_url: str,
) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT set_config('bursar.tenant_id', %s, true)", (TEST_TENANT_ID,))
        cursor.execute("SELECT set_config('bursar.provider_environment', 'test', true)")
        cursor.execute("SELECT set_config('bursar.billing_payload_backend', 's3', true)")
        cursor.execute(
            """
            SELECT result, event_id
            FROM bursar.claim_billing_event(
                'stripe', 'evt-storage-s3-mode-1', 'invoice.paid',
                '{"id": "evt-storage-s3-mode-1", "amount": 1200}'::jsonb
            )
            """
        )
        event_row = cursor.fetchone()
        assert event_row is not None
        result, event_id = event_row
        assert result == "claimed"
        cursor.execute(
            "SELECT count(*) FROM bursar.billing_event_payloads WHERE event_id = %s",
            (event_id,),
        )
        payload_count = cursor.fetchone()
        assert payload_count is not None
        assert payload_count[0] == 0
        cursor.execute(
            """
            SELECT payload->'envelope'->>'id'
            FROM bursar.event_outbox
            WHERE aggregate_id = %s AND topic = 'billing.webhook_received'
            """,
            (event_id,),
        )
        envelope_row = cursor.fetchone()
        assert envelope_row is not None
        assert envelope_row[0] == "evt-storage-s3-mode-1"


def test_s3_runtime_archives_received_outbox_payload(
    pg_database_url: str,
) -> None:
    billing_store = PostgresBillingStore(
        pg_database_url,
        tenant_id=TEST_TENANT_ID,
        provider_environment="test",
        billing_payload_backend="s3",
    )
    archive = RecordingBillingArchive()
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pg_database_url,
            tenant_id=UUID(TEST_TENANT_ID),
            provider_environment="test",
            s3=archive,
            outbox=OutboxWorkerOptions(batch_size=10, poll_interval_ms=60_000),
        )
    )
    try:
        claim = billing_store.claim_billing_event(
            "stripe",
            "evt-storage-s3-runtime-1",
            "invoice.paid",
            {"id": "evt-storage-s3-runtime-1", "amount": 1200},
        )
        assert claim.status == "claimed"
        assert runtime.flush() == OutboxRunResult(claimed=1, delivered=1, failed=0, claim_lost=0)
        assert archive.events[0].envelope == {"id": "evt-storage-s3-runtime-1", "amount": 1200}
    finally:
        billing_store.close()
        runtime.close()
