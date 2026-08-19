"""DB-backed storage repository integration tests for the Python SDK."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic, sleep
from typing import Any, cast
from uuid import UUID

import psycopg2
import pytest
from psycopg2.extras import Json

from bursar.billing.postgres.store import PostgresBillingStore
from bursar.credits.service_types import ReserveOptions
from bursar.credits.types import (
    AggregateStats,
    DailySpendRow,
    SpendByModelRow,
    SpendByUserRow,
    TopUserRow,
)
from bursar.errors import ConfigError, StoreClosedError
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
from bursar.storage.adapters.clickhouse import ClickHouseUsageStoreOptions
from bursar.storage.maintenance import MaintenanceRunOptions, OperatorMaintenanceRunOptions
from bursar.storage.outbox_worker import OutboxEventOutcome
from bursar.storage.ports import OutboxDeadLetterListOptions
from bursar.storage.postgres_repository import PostgresStorageRepository
from tests.conftest import TEST_TENANT_ID, TEST_TENANT_SLUG
from tests.test_store_integration import CONFIG

pytestmark = [pytest.mark.integration]


def _operator_database_url(database_url: str) -> str:
    # These repository tests use the disposable migration principal for both
    # pools; caller-role routing is exercised by test_cli_integration.py.
    separator = "&" if "?" in database_url else "?"
    return f"{database_url}{separator}application_name=bursar-operator-test"


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


class RejectingBatchUsageSink(RecordingUsageSink):
    def write_usage_batch(
        self,
        events: list[tuple[UsageChargeExport, str]],
    ) -> None:
        assert len(events) == 1
        raise RuntimeError("provider-secret-must-not-be-persisted")


class FailOnceBatchUsageSink(RecordingUsageSink):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = True

    def write_usage_batch(
        self,
        events: list[tuple[UsageChargeExport, str]],
    ) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("transient usage projection failure")
        self.writes.extend(events)


class ExpiringClaimUsageSink(RecordingUsageSink):
    def __init__(self, database_url: str) -> None:
        super().__init__()
        self.database_url = database_url
        self.expire_next = True

    def write_usage_batch(
        self,
        events: list[tuple[UsageChargeExport, str]],
    ) -> None:
        if self.expire_next:
            self.expire_next = False
            with psycopg2.connect(self.database_url) as connection, connection.cursor() as cursor:
                cursor.execute("SELECT set_config('bursar.tenant_id', %s, true)", (TEST_TENANT_ID,))
                cursor.execute(
                    """
                    UPDATE bursar.event_outbox
                    SET claim_expires_at = now() - interval '1 second'
                    WHERE id = %s::bigint AND status = 'processing'
                    """,
                    (events[0][1],),
                )
            sleep(0.5)
            return
        self.writes.extend(events)


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


def _seed_usage_row(pg_database_url: str) -> tuple[str, str]:
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
        return str(charge_id), str(subject_id)


def _seed_storage_rows(pg_database_url: str) -> tuple[str, str]:
    charge_id, subject_id = _seed_usage_row(pg_database_url)
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT set_config('bursar.tenant_id', %s, true)", (TEST_TENANT_ID,))
        cursor.execute("SELECT set_config('bursar.provider_environment', 'test', true)")
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
        return charge_id, str(billing_event_id)


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
            operator_postgres=_operator_database_url(pg_database_url),
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


def test_bursar_runtime_dead_letters_failed_usage_batch_without_leaking_details(
    pg_database_url: str,
) -> None:
    _seed_storage_rows(pg_database_url)
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pg_database_url,
            operator_postgres=_operator_database_url(pg_database_url),
            tenant_id=UUID(TEST_TENANT_ID),
            provider_environment="test",
            clickhouse=RejectingBatchUsageSink(),
            outbox=OutboxWorkerOptions(
                attempt_limit=1,
                batch_size=10,
                poll_interval_ms=60_000,
            ),
        )
    )
    try:
        assert runtime.flush() == OutboxRunResult(claimed=1, delivered=0, failed=1, claim_lost=0)

        dead_letters = runtime.outbox_recovery.list_dead_letters().items
        assert len(dead_letters) == 1
        assert dead_letters[0].topic == "usage.charge_recorded"
        assert dead_letters[0].attempt_count == 1
        assert dead_letters[0].last_error == "outbox_delivery_failed:RuntimeError"
        assert runtime.outbox_recovery.stats().dead_letter_count == 1
    finally:
        runtime.close()


def test_bursar_runtime_start_health_and_no_worker_flush(
    pg_database_url: str,
) -> None:
    clickhouse = RecordingUsageSink()
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pg_database_url,
            operator_postgres=_operator_database_url(pg_database_url),
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


def test_bursar_runtime_context_loads_catalog_and_reports_worker_dependencies(
    pg_database_url: str,
) -> None:
    clickhouse = RecordingUsageSink()
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pg_database_url,
            operator_postgres=_operator_database_url(pg_database_url),
            tenant_id=UUID(TEST_TENANT_ID),
            tenant_slug=f"  {TEST_TENANT_SLUG.upper()}  ",
            provider_environment="test",
            clickhouse=clickhouse,
            outbox=False,
        )
    )
    runtime.credit_store.publish_and_activate_catalog(deepcopy(CONFIG))

    with runtime as active:
        health = active.health()
        assert clickhouse.initialized is True
        assert health.ready is True
        assert health.financial_ready is True
        assert health.projection_ready is True
        assert health.catalog_loaded is True
        assert active.state().worker.lifecycle == "not_configured"

        diagnostics = active.check_dependencies()
        assert diagnostics.ready is True
        assert diagnostics.postgres.status == "ok"
        assert diagnostics.catalog.status == "ok"
        assert diagnostics.catalog.current_revision is not None
        assert diagnostics.outbox.status == "ok"
        assert diagnostics.outbox.snapshot is not None
        assert diagnostics.outbox.snapshot.dead_letter_count == 0

    assert runtime.health().closed is True
    assert runtime.state().worker.lifecycle == "not_configured"


def test_bursar_runtime_maintenance_expires_composed_credit_store_state(
    pg_database_url: str,
) -> None:
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pg_database_url,
            operator_postgres=_operator_database_url(pg_database_url),
            tenant_id=UUID(TEST_TENANT_ID),
            provider_environment="test",
            outbox=False,
        )
    )
    lease_user_id = "00000000-0000-0000-0000-000000000991"
    credit_user_id = "00000000-0000-0000-0000-000000000992"
    try:
        service = runtime.bursar.credits
        runtime.credit_store.publish_and_activate_catalog(deepcopy(CONFIG))
        runtime.bursar.catalog.load()
        service.add_credits(
            lease_user_id,
            Decimal("5"),
            idempotency_key="runtime-maintenance-lease-funding",
        )
        lease = service.reserve(
            lease_user_id,
            Decimal("2"),
            ReserveOptions(idempotency_key="runtime-maintenance-lease", ttl=60),
        )
        assert lease.lease_id is not None
        expiring = service.add_credits(
            credit_user_id,
            Decimal("3"),
            bucket="grant",
            expires_at=datetime.now(UTC) + timedelta(days=1),
            idempotency_key="runtime-maintenance-credit-funding",
        )

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT set_config('bursar.tenant_id', %s, true)", (TEST_TENANT_ID,))
            cursor.execute("SELECT set_config('bursar.mutation_context', 'internal', true)")
            cursor.execute(
                """
                UPDATE bursar.credit_leases
                SET created_at = created_at - interval '2 minutes',
                    expires_at = now() - interval '1 second'
                WHERE id = %s
                """,
                (lease.lease_id,),
            )
            assert cursor.rowcount == 1
            cursor.execute(
                """
                UPDATE bursar.credit_lots
                SET expires_at = now() - interval '1 second'
                WHERE source_entry_id = %s::uuid
                """,
                (expiring.entry_id,),
            )
            assert cursor.rowcount == 1

        result = runtime.maintenance.run_once(MaintenanceRunOptions(limit=10))

        assert result.count >= 2
        assert result.tasks["expired_leases"].count == 1
        assert result.tasks["expired_credits"].count == 1
        assert result.tasks["expired_leases"].status == "completed"
        assert result.tasks["expired_credits"].status == "completed"
        assert service.get_available(lease_user_id).available == Decimal("5")
        assert service.get_available(credit_user_id).available == Decimal("0")
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
            operator_postgres=_operator_database_url(pg_database_url),
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


def test_operator_maintenance_runs_storage_and_partition_reconciliation(
    pg_database_url: str,
) -> None:
    with pytest.raises(ValueError, match="distinct connections"):
        create_bursar_runtime(
            BursarRuntimeOptions(
                postgres=pg_database_url,
                operator_postgres=pg_database_url,
                tenant_id=UUID(TEST_TENANT_ID),
                provider_environment="test",
                outbox=False,
            )
        )
    with pytest.raises(ValueError, match="must not be empty"):
        create_bursar_runtime(
            BursarRuntimeOptions(
                postgres="",
                operator_postgres=_operator_database_url(pg_database_url),
                tenant_id=UUID(TEST_TENANT_ID),
                provider_environment="test",
                outbox=False,
            )
        )
    with pytest.raises(ValueError, match="tenant_id must match"):
        create_bursar_runtime(
            BursarRuntimeOptions(
                postgres=pg_database_url,
                operator_postgres=_operator_database_url(pg_database_url),
                tenant_id=UUID(TEST_TENANT_ID),
                provider_environment="test",
                clickhouse=ClickHouseUsageStoreOptions(
                    client=cast(Any, object()),
                    tenant_id=UUID("00000000-0000-0000-0000-000000000099"),
                ),
                outbox=False,
            )
        )
    with pytest.raises(TypeError, match="clickhouse"):
        create_bursar_runtime(
            BursarRuntimeOptions(
                postgres=pg_database_url,
                operator_postgres=_operator_database_url(pg_database_url),
                tenant_id=UUID(TEST_TENANT_ID),
                provider_environment="test",
                clickhouse=cast(Any, object()),
                outbox=False,
            )
        )
    with pytest.raises(TypeError, match="s3"):
        create_bursar_runtime(
            BursarRuntimeOptions(
                postgres=pg_database_url,
                operator_postgres=_operator_database_url(pg_database_url),
                tenant_id=UUID(TEST_TENANT_ID),
                provider_environment="test",
                s3=cast(Any, object()),
                outbox=False,
            )
        )

    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pg_database_url,
            operator_postgres=_operator_database_url(pg_database_url),
            tenant_id=UUID(TEST_TENANT_ID),
            provider_environment="test",
            outbox=False,
        )
    )
    try:
        storage = runtime.operator_maintenance.run_once(
            OperatorMaintenanceRunOptions(mode="force", now=datetime.now(UTC))
        )
        assert storage.status == "completed"
        assert storage.count >= 0
        assert storage.count == sum(storage.counts.model_dump().values())
        not_due = runtime.operator_maintenance.run_once(
            OperatorMaintenanceRunOptions(mode="if_due", now=datetime.now(UTC))
        )
        assert not_due.status == "not_due"
        assert not_due.count == 0

        partition = runtime.operator_maintenance.run_partition_once(
            "usage_charge_payloads",
            now=datetime.now(UTC),
        )
        assert partition.parent_table == "usage_charge_payloads"
        assert partition.status in {"completed", "busy"}
        assert partition.count == partition.partitions_created + partition.partitions_dropped

        runtime.start(BursarRuntimeStartOptions(load_catalog=False))
        runtime.start(BursarRuntimeStartOptions(load_catalog=False))
    finally:
        runtime.close()
    runtime.close()
    with pytest.raises(StoreClosedError, match="closed"):
        runtime.start(BursarRuntimeStartOptions(load_catalog=False))

    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT bursar.create_tenant(%s::uuid, %s, %s)",
            ("00000000-0000-0000-0000-000000000099", "other-tenant", "Other tenant"),
        )
    mismatched = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pg_database_url,
            operator_postgres=_operator_database_url(pg_database_url),
            tenant_id=UUID(TEST_TENANT_ID),
            tenant_slug="other-tenant",
            provider_environment="test",
            outbox=False,
        )
    )
    try:
        with pytest.raises(ConfigError, match="resolves to a different tenant"):
            mismatched.start(BursarRuntimeStartOptions(load_catalog=False))
    finally:
        mismatched.close()


def test_runtime_worker_delivers_real_outbox_event_in_background(
    pg_database_url: str,
) -> None:
    charge_id, _subject_id = _seed_usage_row(pg_database_url)
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT set_config('bursar.tenant_id', %s, true)", (TEST_TENANT_ID,))
        cursor.execute("SELECT set_config('bursar.mutation_context', 'internal', true)")
        cursor.execute(
            "UPDATE bursar.credit_usage_charges SET billing_disposition = 'record_only' WHERE id = %s::uuid",
            (charge_id,),
        )
    clickhouse = ExpiringClaimUsageSink(pg_database_url)
    outcomes: list[OutboxEventOutcome] = []
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pg_database_url,
            operator_postgres=_operator_database_url(pg_database_url),
            tenant_id=UUID(TEST_TENANT_ID),
            provider_environment="test",
            clickhouse=clickhouse,
            outbox=OutboxWorkerOptions(
                batch_size=1,
                lease_seconds=1,
                poll_interval_ms=10,
                on_event_outcome=outcomes.append,
            ),
        )
    )
    try:
        runtime.start(BursarRuntimeStartOptions(load_catalog=False))
        deadline = monotonic() + 2
        while (
            not clickhouse.writes or not any(outcome.status == "delivered" for outcome in outcomes)
        ) and monotonic() < deadline:
            sleep(0.02)
        assert runtime.health().started is True
        assert len(clickhouse.writes) == 1
        assert clickhouse.writes[0][0].charge_id
        assert clickhouse.writes[0][0].billing_disposition == "record_only"
        assert {outcome.status for outcome in outcomes} == {"claim_lost", "delivered"}
    finally:
        runtime.close()


def test_runtime_requeues_transient_usage_projection_failure(
    pg_database_url: str,
) -> None:
    charge_id, _subject_id = _seed_usage_row(pg_database_url)
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT set_config('bursar.tenant_id', %s, true)", (TEST_TENANT_ID,))
        cursor.execute("SELECT set_config('bursar.mutation_context', 'internal', true)")
        cursor.execute(
            "UPDATE bursar.event_outbox SET payload_version = 2 WHERE aggregate_id = %s::uuid",
            (charge_id,),
        )
    clickhouse = FailOnceBatchUsageSink()
    runtime = create_bursar_runtime(
        BursarRuntimeOptions(
            postgres=pg_database_url,
            operator_postgres=_operator_database_url(pg_database_url),
            tenant_id=UUID(TEST_TENANT_ID),
            provider_environment="test",
            clickhouse=clickhouse,
            outbox=OutboxWorkerOptions(attempt_limit=1, batch_size=10, poll_interval_ms=60_000),
        )
    )
    try:
        assert runtime.flush() == OutboxRunResult(claimed=1, delivered=0, failed=1, claim_lost=0)
        dead_letters = runtime.outbox_recovery.list_dead_letters(OutboxDeadLetterListOptions(limit=1))
        assert len(dead_letters.items) == 1
        dead_letter = dead_letters.items[0]
        assert dead_letter.topic == "usage.charge_recorded"
        assert dead_letter.attempt_count == 1

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT set_config('bursar.tenant_id', %s, true)", (TEST_TENANT_ID,))
            cursor.execute("SELECT set_config('bursar.mutation_context', 'internal', true)")
            cursor.execute(
                "UPDATE bursar.event_outbox SET payload_version = 1 WHERE id = %s::bigint",
                (dead_letter.event_id,),
            )
        assert runtime.outbox_recovery.requeue(dead_letter.event_id) is True

        assert runtime.flush() == OutboxRunResult(claimed=1, delivered=0, failed=1, claim_lost=0)
        retry_dead_letter = runtime.outbox_recovery.list_dead_letters(OutboxDeadLetterListOptions(limit=1)).items[0]
        assert retry_dead_letter.last_error == "outbox_delivery_failed:RuntimeError"
        assert runtime.outbox_recovery.requeue(retry_dead_letter.event_id) is True

        assert runtime.flush() == OutboxRunResult(claimed=1, delivered=1, failed=0, claim_lost=0)
        assert len(clickhouse.writes) == 1
        assert runtime.outbox_recovery.stats().dead_letter_count == 0
    finally:
        runtime.close()
