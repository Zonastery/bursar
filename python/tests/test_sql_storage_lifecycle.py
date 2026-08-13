"""PostgreSQL-first bounded event-storage contract tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extras import Json

from tests.conftest import TEST_TENANT_ID

pytestmark = [pytest.mark.integration]
type ManagedPartition = tuple[str, str]
type ManagedPartitionFactory = Callable[
    [psycopg2.extensions.cursor, str, datetime],
    ManagedPartition,
]


def _create_account(cursor: psycopg2.extensions.cursor) -> tuple[str, str]:
    cursor.execute(
        "SELECT set_config('bursar.tenant_id', %s, true)",
        (TEST_TENANT_ID,),
    )
    cursor.execute("SELECT set_config('bursar.provider_environment', 'test', true)")
    cursor.execute("INSERT INTO bursar.subjects DEFAULT VALUES RETURNING id")
    subject_id = str(cursor.fetchone()[0])  # type: ignore[reportOptionalSubscript]
    cursor.execute(
        """
        INSERT INTO bursar.credit_accounts(subject_id, account_kind)
        VALUES (%s, 'personal')
        RETURNING id
        """,
        (subject_id,),
    )
    return subject_id, str(cursor.fetchone()[0])  # type: ignore[reportOptionalSubscript]


def _create_managed_partition(
    cursor: psycopg2.extensions.cursor,
    parent_table: str,
    partition_at: datetime,
) -> ManagedPartition:
    """Create a test partition and apply Bursar's production hardening hook."""
    # Migration 030 hardened partitions for the private partition owner:
    # hardening functions are SECURITY DEFINER owned by bursar_partition_runtime
    # and require ownership of the child table, which pg_partman grants to the
    # role that creates it.  Create under that role exactly like real partman
    # maintenance does.
    cursor.execute("SET ROLE bursar_partition_runtime")
    try:
        cursor.execute(
            """
            SELECT partman.create_partition_time(
                %s,
                ARRAY[%s::timestamptz]
            )
            """,
            (parent_table, partition_at),
        )
        cursor.execute(
            """
            SELECT
                partition_schema,
                partition_table,
                bursar.secure_tenant_partition(
                    format(
                        '%%I.%%I',
                        partition_schema,
                        partition_table
                    )::regclass
                )
            FROM partman.show_partition_name(
                %s,
                %s::timestamptz::text
            )
            WHERE table_exists
            """,
            (parent_table, partition_at),
        )
        row = cursor.fetchone()
    finally:
        cursor.execute("RESET ROLE")
    assert row is not None
    return str(row[0]), str(row[1])


@pytest.fixture
def committed_partition_factory(
    pg_database_url: str,
) -> Iterator[ManagedPartitionFactory]:
    """Create partitions whose setup transaction commits, then always drop them."""
    committed_partitions: list[ManagedPartition] = []

    def create(
        cursor: psycopg2.extensions.cursor,
        parent_table: str,
        partition_at: datetime,
    ) -> ManagedPartition:
        partition = _create_managed_partition(cursor, parent_table, partition_at)
        committed_partitions.append(partition)
        return partition

    yield create

    if not committed_partitions:
        return
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        for partition_schema, partition_table in reversed(committed_partitions):
            cursor.execute(
                sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
                    sql.Identifier(partition_schema),
                    sql.Identifier(partition_table),
                )
            )
            cursor.execute(
                "SELECT to_regclass(%s)",
                (f"{partition_schema}.{partition_table}",),
            )
            assert cursor.fetchone() == (None,)


def test_usage_payload_cleanup_is_batched_and_keeps_recent_data(
    pg_database_url: str,
) -> None:
    maintenance_now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    expired_at = maintenance_now - timedelta(days=91)
    recent_at = maintenance_now - timedelta(days=1)

    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        subject_id, account_id = _create_account(cursor)
        cursor.execute(
            """
            SELECT bursar.configure_storage(
                p_maintenance_batch_size => 2
            )
            """
        )

        charge_ids: list[str] = []
        for index, event_at in enumerate(
            [expired_at, expired_at + timedelta(minutes=1), expired_at + timedelta(minutes=2), recent_at]
        ):
            cursor.execute(
                """
                SELECT *
                FROM bursar.charge_usage(
                    %s::uuid,
                    'completion',
                    0,
                    %s,
                    p_model => 'small-model',
                    p_region => 'in',
                    p_event_at => %s,
                    p_measures => %s::jsonb,
                    p_dimensions => %s::jsonb,
                    p_metadata => %s::jsonb
                )
                """,
                (
                    subject_id,
                    f"bounded-usage-{index}",
                    event_at,
                    Json({"input_tokens": 12}),
                    Json({"tenant_tier": "starter"}),
                    Json({"trace_id": f"trace-{index}"}),
                ),
            )
            charge_ids.append(str(cursor.fetchone()[0]))  # type: ignore[reportOptionalSubscript]

        cursor.execute(
            """
            SELECT
                array_agg(DISTINCT usage_day ORDER BY usage_day),
                sum(charge_count)
            FROM bursar.usage_daily_rollups
            WHERE account_id = %s::uuid
              AND operation = 'completion'
              AND model_key = 'small-model'
            """,
            (account_id,),
        )
        assert cursor.fetchone() == ([expired_at.date(), recent_at.date()], 4)

        cursor.execute(
            "SELECT bursar.run_storage_maintenance(%s)",
            (maintenance_now,),
        )
        first_pass = cursor.fetchone()[0]  # type: ignore[reportOptionalSubscript]
        assert first_pass["batch_size"] == 2
        assert first_pass["usage_payloads_purged"] == 2
        assert first_pass["has_more"] is True

        cursor.execute(
            """
            SELECT count(*)
            FROM bursar.usage_charge_payloads
            WHERE charge_id = ANY(%s::uuid[])
            """,
            (charge_ids,),
        )
        assert cursor.fetchone() == (2,)

        cursor.execute(
            "SELECT bursar.run_storage_maintenance(%s)",
            (maintenance_now,),
        )
        second_pass = cursor.fetchone()[0]  # type: ignore[reportOptionalSubscript]
        assert second_pass["usage_payloads_purged"] == 1
        assert second_pass["has_more"] is False

        cursor.execute(
            """
            SELECT charge_id::text
            FROM bursar.usage_charge_payloads
            WHERE charge_id = ANY(%s::uuid[])
            """,
            (charge_ids,),
        )
        assert cursor.fetchall() == [(charge_ids[-1],)]

        cursor.execute(
            """
            SELECT count(*)
            FROM bursar.credit_usage_charges
            WHERE id = ANY(%s::uuid[])
            """,
            (charge_ids,),
        )
        assert cursor.fetchone() == (4,)


def test_fully_expired_payload_partition_is_dropped_without_deleting_core(
    pg_database_url: str,
) -> None:
    maintenance_now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    expired_at = datetime(2025, 1, 15, 12, tzinfo=UTC)

    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        subject_id, _ = _create_account(cursor)
        partition_schema, partition_table = _create_managed_partition(
            cursor,
            "bursar.usage_charge_payloads",
            expired_at,
        )
        cursor.execute(
            """
            SELECT *
            FROM bursar.charge_usage(
                %s::uuid,
                'completion',
                0,
                'partition-drop-usage',
                p_event_at => %s
            )
            """,
            (subject_id, expired_at),
        )
        charge_id = str(cursor.fetchone()[0])  # type: ignore[reportOptionalSubscript]

        cursor.execute(
            """
            SELECT bursar.configure_storage(
                p_usage_payload_retention_days => 90
            )
            """
        )
        cursor.execute(
            """
            SELECT bursar.run_storage_partition_maintenance(
                'usage_charge_payloads',
                %s
            )
            """,
            (maintenance_now,),
        )
        result = cursor.fetchone()[0]  # type: ignore[reportOptionalSubscript]

        assert result["partitions_dropped"] >= 1
        assert result["partition_lock_timeouts"] == 0

        cursor.execute(
            "SELECT to_regclass(%s)",
            (f"{partition_schema}.{partition_table}",),
        )
        assert cursor.fetchone() == (None,)

        cursor.execute(
            """
            SELECT count(*)
            FROM bursar.credit_usage_charges
            WHERE id = %s::uuid
            """,
            (charge_id,),
        )
        assert cursor.fetchone() == (1,)


def test_pg_partman_configuration_and_generated_children_are_hardened(
    pg_database_url: str,
) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT extension_info.extversion LIKE '5.%%', namespace_info.nspname
            FROM pg_extension AS extension_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = extension_info.extnamespace
            WHERE extension_info.extname = 'pg_partman'
            """
        )
        assert cursor.fetchone() == (True, "partman")

        cursor.execute(
            """
            SELECT
                parent_table,
                control,
                partition_interval::interval = interval '1 month',
                premake = 4,
                automatic_maintenance = 'off',
                retention IS NULL,
                retention_schema IS NULL,
                NOT retention_keep_table,
                NOT retention_keep_index,
                infinite_time_partitions,
                ignore_default_data,
                NOT inherit_privileges,
                NOT jobmon
            FROM partman.part_config
            WHERE parent_table IN (
                'bursar.usage_charge_payloads',
                'bursar.billing_event_payloads'
            )
            ORDER BY parent_table
            """
        )
        configurations = cursor.fetchall()
        assert [(row[0], row[1]) for row in configurations] == [
            ("bursar.billing_event_payloads", "received_at"),
            ("bursar.usage_charge_payloads", "event_at"),
        ]
        assert all(all(row[2:]) for row in configurations)

        cursor.execute(
            """
            SELECT
                count(*) > 0,
                count(*) FILTER (
                    WHERE pg_get_expr(child.relpartbound, child.oid) = 'DEFAULT'
                ) = 1,
                bool_and(child.relrowsecurity AND child.relforcerowsecurity),
                bool_and(obj_description(child.oid, 'pg_class') IS NOT NULL)
            FROM pg_inherits AS inheritance
            JOIN pg_class AS parent
              ON parent.oid = inheritance.inhparent
            JOIN pg_namespace AS parent_schema
              ON parent_schema.oid = parent.relnamespace
            JOIN pg_class AS child
              ON child.oid = inheritance.inhrelid
            WHERE parent_schema.nspname = 'bursar'
              AND parent.relname = 'usage_charge_payloads'
            """
        )
        assert cursor.fetchone() == (True, True, True, True)

        cursor.execute("SAVEPOINT pg_partman_drift")
        cursor.execute(
            """
            UPDATE partman.part_config
            SET premake = 1
            WHERE parent_table = 'bursar.usage_charge_payloads'
            """
        )
        with pytest.raises(psycopg2.Error) as error:
            cursor.execute(
                """
                SELECT bursar.run_storage_partition_maintenance(
                    'usage_charge_payloads'
                )
                """
            )
        assert error.value.pgcode == "55000"
        cursor.execute("ROLLBACK TO SAVEPOINT pg_partman_drift")


def test_maybe_run_storage_maintenance_respects_interval(
    pg_database_url: str,
) -> None:
    maintenance_now = datetime(2026, 7, 15, 12, tzinfo=UTC)

    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT bursar.configure_storage(
                p_maintenance_interval_seconds => 300
            )
            """
        )
        cursor.execute(
            "SELECT bursar.maybe_run_storage_maintenance(%s)",
            (maintenance_now,),
        )
        assert cursor.fetchone()[0]["status"] == "completed"  # type: ignore[reportOptionalSubscript]

        cursor.execute(
            "SELECT bursar.maybe_run_storage_maintenance(%s)",
            (maintenance_now + timedelta(seconds=60),),
        )
        skipped = cursor.fetchone()[0]  # type: ignore[reportOptionalSubscript]
        assert skipped["status"] == "not_due"

        cursor.execute(
            "SELECT bursar.maybe_run_storage_maintenance(%s)",
            (maintenance_now + timedelta(seconds=301),),
        )
        assert cursor.fetchone()[0]["status"] == "completed"  # type: ignore[reportOptionalSubscript]


def test_default_partition_preserves_out_of_horizon_ingestion(
    pg_database_url: str,
) -> None:
    maintenance_now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    far_future_at = maintenance_now + timedelta(days=3650)

    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        subject_id, _ = _create_account(cursor)
        cursor.execute(
            """
            SELECT charge_id, error_code
            FROM bursar.charge_usage(
                %s::uuid,
                'completion',
                0,
                'default-partition-fallback',
                p_event_at => %s
            )
            """,
            (subject_id, far_future_at),
        )
        charge_id, error_code = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]
        assert error_code is None

        cursor.execute(
            """
            SELECT
                payload.tableoid::regclass::text,
                child.oid::regclass::text
            FROM bursar.usage_charge_payloads AS payload
            JOIN pg_inherits AS inheritance
              ON inheritance.inhparent = 'bursar.usage_charge_payloads'::regclass
            JOIN pg_class AS child
              ON child.oid = inheritance.inhrelid
             AND pg_get_expr(child.relpartbound, child.oid) = 'DEFAULT'
            WHERE payload.charge_id = %s::uuid
            """,
            (charge_id,),
        )
        payload_table, default_table = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]
        assert payload_table == default_table

        cursor.execute(
            """
            SELECT bursar.run_storage_partition_maintenance(
                'usage_charge_payloads',
                %s
            )
            """,
            (maintenance_now,),
        )
        result = cursor.fetchone()[0]  # type: ignore[reportOptionalSubscript]
        assert result["default_partition_has_rows"] is True
        assert result["has_more"] is True


def test_partition_maintenance_does_not_wait_behind_ingestion(
    pg_database_url: str,
    committed_partition_factory: ManagedPartitionFactory,
) -> None:
    maintenance_now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    expired_at = datetime(2025, 1, 15, 12, tzinfo=UTC)

    with psycopg2.connect(pg_database_url) as setup, setup.cursor() as cursor:
        subject_id, _ = _create_account(cursor)
        committed_partition_factory(
            cursor,
            "bursar.usage_charge_payloads",
            expired_at,
        )
        cursor.execute(
            """
            SELECT *
            FROM bursar.charge_usage(
                %s::uuid,
                'completion',
                0,
                'partition-lock-timeout',
                p_event_at => %s
            )
            """,
            (subject_id, expired_at),
        )
        cursor.execute(
            """
            SELECT bursar.configure_storage(
                p_usage_payload_retention_days => 90,
                p_maintenance_lock_timeout_ms => 50
            )
            """
        )

    locker = psycopg2.connect(pg_database_url)
    worker = psycopg2.connect(pg_database_url)
    try:
        with locker.cursor() as cursor:
            cursor.execute(
                """
                LOCK TABLE bursar.usage_charge_payloads
                IN ROW EXCLUSIVE MODE
                """
            )

        with worker.cursor() as cursor:
            cursor.execute("SET statement_timeout = '2s'")
            cursor.execute(
                """
                SELECT bursar.run_storage_partition_maintenance(
                    'usage_charge_payloads',
                    %s
                )
                """,
                (maintenance_now,),
            )
            result = cursor.fetchone()[0]  # type: ignore[reportOptionalSubscript]
            assert result["partitions_dropped"] == 0
            assert result["partition_lock_timeouts"] >= 1
            assert result["has_more"] is True
    finally:
        worker.rollback()
        locker.rollback()
        worker.close()
        locker.close()


def test_retention_scans_have_time_leading_indexes(
    pg_database_url: str,
) -> None:
    expected_indexes = {
        "event_outbox_retention_idx",
        "event_outbox_delivered_retention_idx",
        "quota_usage_events_retention_idx",
        "quota_usage_events_correction_idx",
        "quota_events_retention_idx",
        "terminal_lease_payload_retention_idx",
    }

    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'bursar'
              AND indexname = ANY(%s)
            """,
            (list(expected_indexes),),
        )
        actual_indexes = {row[0] for row in cursor.fetchall()}

    assert actual_indexes == expected_indexes


def test_billing_claim_stores_bounded_payload_separately(
    pg_database_url: str,
) -> None:
    envelope = {"accountId": "account-1", "kind": "invoice.paid"}

    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('bursar.tenant_id', %s, true)",
            (TEST_TENANT_ID,),
        )
        cursor.execute("SELECT set_config('bursar.provider_environment', 'test', true)")
        cursor.execute(
            """
            SELECT *
            FROM bursar.claim_billing_event(
                'stripe',
                'evt-bounded-1',
                'invoice.paid',
                %s::jsonb
            )
            """,
            (Json(envelope),),
        )
        result, event_id, claim_token = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]
        assert result == "claimed"

        cursor.execute(
            """
            SELECT payload.envelope
            FROM bursar.billing_event_payloads AS payload
            JOIN bursar.billing_events AS event
              ON event.id = payload.event_id
             AND event.payload_received_at = payload.received_at
            WHERE event.id = %s::uuid
            """,
            (event_id,),
        )
        assert cursor.fetchone()[0] == envelope  # type: ignore[reportOptionalSubscript]

        cursor.execute(
            """
            SELECT bursar.complete_billing_event(
                'stripe',
                'evt-bounded-1',
                %s::uuid
            )
            """,
            (claim_token,),
        )
        assert cursor.fetchone() == (True,)
        cursor.execute(
            """
            SELECT status
            FROM bursar.event_outbox
            WHERE aggregate_id = %s::uuid
              AND topic = 'billing.webhook_completed'
            """,
            (event_id,),
        )
        assert cursor.fetchone() == ("pending",)

        cursor.execute(
            "SELECT bursar.export_billing_event_payload(%s::uuid)",
            (event_id,),
        )
        exported = cursor.fetchone()[0]  # type: ignore[reportOptionalSubscript]
        assert exported["event_id"] == str(event_id)
        assert exported["provider"] == "stripe"
        assert exported["envelope"] == envelope
        assert exported["object_key"] is None

        cursor.execute(
            """
            SELECT bursar.archive_billing_event_payload(
                %s::uuid,
                'billing/stripe/evt-bounded-1.json',
                'version-1'
            )
            """,
            (event_id,),
        )
        assert cursor.fetchone() == (True,)
        cursor.execute(
            """
            SELECT payload_object_key, payload_object_version
            FROM bursar.billing_events
            WHERE id = %s::uuid
            """,
            (event_id,),
        )
        object_key, object_version = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]
        assert object_key == "billing/stripe/evt-bounded-1.json"
        assert object_version == "version-1"
        cursor.execute(
            """
            SELECT count(*)
            FROM bursar.billing_event_payloads
            WHERE event_id = %s::uuid
            """,
            (event_id,),
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            "SELECT bursar.export_billing_event_payload(%s::uuid)",
            (event_id,),
        )
        exported = cursor.fetchone()[0]  # type: ignore[reportOptionalSubscript]
        assert exported["envelope"] is None
        assert exported["object_key"] == "billing/stripe/evt-bounded-1.json"


def test_financial_subject_pseudonymization_redacts_attributed_event_copies(
    pg_database_url: str,
) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        subject_id, _ = _create_account(cursor)
        envelope = {
            "customer": {"email": "private@example.com"},
        }
        assert subject_id not in str(envelope)

        cursor.execute(
            """
            SELECT *
            FROM bursar.claim_billing_event(
                'stripe',
                'evt-pseudonymize-account-envelope',
                'customer.updated',
                %s::jsonb
            )
            """,
            (Json(envelope),),
        )
        _status, postgres_event_id, _claim_token = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]

        cursor.execute(
            "SELECT bursar.attribute_billing_event_subject(%s::uuid, %s::uuid)",
            (postgres_event_id, subject_id),
        )
        assert cursor.fetchone() == (True,)

        cursor.execute("SET LOCAL bursar.billing_payload_backend = 's3'")
        cursor.execute(
            """
            SELECT *
            FROM bursar.claim_billing_event(
                'stripe',
                'evt-pseudonymize-outbox-envelope',
                'customer.updated',
                %s::jsonb
            )
            """,
            (Json(envelope),),
        )
        _status, outbox_event_id, _claim_token = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]

        cursor.execute(
            "SELECT bursar.attribute_billing_event_subject(%s::uuid, %s::uuid)",
            (outbox_event_id, subject_id),
        )
        assert cursor.fetchone() == (True,)

        cursor.execute(
            "SELECT bursar.pseudonymize_financial_subject(%s::uuid)",
            (subject_id,),
        )
        assert cursor.fetchone() == (True,)

        cursor.execute(
            """
            SELECT payload.envelope
            FROM bursar.billing_event_payloads AS payload
            JOIN bursar.billing_events AS event
              ON event.id = payload.event_id
             AND event.payload_received_at = payload.received_at
            WHERE event.id = %s::uuid
            """,
            (postgres_event_id,),
        )
        assert cursor.fetchone() == ({"pseudonymized": True},)

        cursor.execute(
            """
            SELECT event.subject_id, outbox.payload->'envelope'
            FROM bursar.billing_events AS event
            JOIN bursar.event_outbox AS outbox
              ON outbox.aggregate_type = 'billing_event'
             AND outbox.aggregate_id = event.id
             AND outbox.topic = 'billing.webhook_received'
            WHERE event.id = %s::uuid
            """,
            (outbox_event_id,),
        )
        assert cursor.fetchone() == (subject_id, {"pseudonymized": True})


def test_outbox_claim_acknowledgement_and_payload_bounds(
    pg_database_url: str,
) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        subject_id, _ = _create_account(cursor)
        cursor.execute(
            """
            SELECT error_code
            FROM bursar.charge_usage(
                %s::uuid,
                'completion',
                0,
                'oversized-metadata',
                p_metadata => jsonb_build_object(
                    'value',
                        repeat('x', 1100000)
                )
            )
            """,
            (subject_id,),
        )
        assert cursor.fetchone() == ("invalid_request",)

        cursor.execute(
            """
            SELECT *
            FROM bursar.claim_billing_event(
                'stripe',
                'evt-outbox-1',
                'invoice.paid',
                '{}'::jsonb
            )
            """
        )
        _, _, billing_claim_token = cursor.fetchone()  # type: ignore[reportGeneralTypeIssues]
        cursor.execute(
            """
            SELECT bursar.complete_billing_event(
                'stripe',
                'evt-outbox-1',
                %s::uuid
            )
            """,
            (billing_claim_token,),
        )
        assert cursor.fetchone() == (True,)

        cursor.execute("SELECT * FROM bursar.claim_outbox_events(10, 60)")
        claimed = cursor.fetchall()
        assert claimed
        for row in claimed:
            cursor.execute(
                "SELECT bursar.complete_outbox_event(%s, %s::uuid)",
                (row[0], row[7]),
            )
            assert cursor.fetchone() == (True,)

        cursor.execute("SELECT count(*) FROM bursar.event_outbox WHERE status = 'delivered'")
        assert cursor.fetchone()[0] == len(claimed)  # type: ignore[reportOptionalSubscript]


def test_usage_export_and_topic_filtered_outbox_claim(
    pg_database_url: str,
) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        subject_id, _ = _create_account(cursor)
        cursor.execute(
            """
            SELECT charge_id, error_code
            FROM bursar.charge_usage(
                %s::uuid,
                'completion',
                0,
                'usage-export-1',
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

        cursor.execute("SELECT bursar.export_usage_charge(%s::uuid)", (charge_id,))
        exported = cursor.fetchone()[0]  # type: ignore[reportOptionalSubscript]
        assert exported["charge_id"] == str(charge_id)
        assert exported["subject_id"] == subject_id
        assert exported["charged"] == "0.000000"
        assert exported["model"] == "small-model"
        assert exported["dimensions"]["tenant_tier"] == "starter"
        assert exported["metadata"]["trace_id"] == "trace-1"

        cursor.execute(
            """
            SELECT *
            FROM bursar.claim_outbox_events(
                10,
                60,
                ARRAY['billing.webhook_completed']::text[]
            )
            """
        )
        assert cursor.fetchall() == []

        cursor.execute(
            """
            SELECT *
            FROM bursar.claim_outbox_events(
                10,
                60,
                ARRAY['usage.charge_recorded']::text[]
            )
            """
        )
        claimed = cursor.fetchall()
        assert len(claimed) == 1
        assert claimed[0][2] == "usage.charge_recorded"
        assert str(claimed[0][4]) == str(charge_id)


def test_catalog_rejects_rolling_quota_beyond_retention(
    pg_database_url: str,
) -> None:
    connection = psycopg2.connect(pg_database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('bursar.tenant_id', %s, true)",
                (TEST_TENANT_ID,),
            )
            cursor.execute(
                """
                SELECT bursar.configure_storage(
                    p_quota_event_retention_days => 10,
                    p_quota_max_lateness_seconds => 0,
                    p_quota_correction_window_days => 0,
                    p_quota_retention_safety_days => 1
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO bursar.catalog_revisions(
                    yaml_schema_version,
                    source_document,
                    digest
                )
                VALUES (
                    1,
                    '{"version":1,"credits":{}}'::jsonb,
                    extensions.digest('{"version":1,"credits":{}}', 'sha256')
                )
                RETURNING id
                """
            )
            revision_id = cursor.fetchone()[0]  # type: ignore[reportOptionalSubscript]
            cursor.execute(
                """
                INSERT INTO bursar.catalog_operations(
                    catalog_revision_id,
                    operation_key,
                    measures,
                    dimensions,
                    definition
                )
                VALUES (
                    %s::uuid,
                    'completion',
                    '{"calls":{"unit":"call"}}'::jsonb,
                    '{}'::jsonb,
                    '{
                        "measures":{"calls":{"unit":"call"}},
                        "dimensions":{}
                    }'::jsonb
                )
                """,
                (revision_id,),
            )
            cursor.execute(
                """
                INSERT INTO bursar.catalog_plans(
                    catalog_revision_id,
                    plan_key,
                    display_name,
                    definition
                )
                VALUES (
                    %s::uuid,
                    'pro',
                    'Pro',
                    '{"display_name":"Pro"}'::jsonb
                )
                """,
                (revision_id,),
            )

            with pytest.raises(
                psycopg2.errors.CheckViolation,
                match="retention horizon",
            ):
                cursor.execute(
                    """
                    INSERT INTO bursar.catalog_plan_quotas(
                        catalog_revision_id,
                        plan_key,
                        quota_key,
                        operation_key,
                        measure_key,
                        quota_limit,
                        window_policy,
                        enforcement,
                        definition
                    )
                    VALUES (
                        %s::uuid,
                        'pro',
                        'monthly_calls',
                        'completion',
                        'calls',
                        100,
                        '{
                            "type":"rolling",
                            "duration":{"unit":"day","count":30}
                        }'::jsonb,
                        'block',
                        '{
                            "operation":"completion",
                            "measure":"calls",
                            "limit":"100",
                            "window":{
                                "type":"rolling",
                                "duration":{"unit":"day","count":30}
                            },
                            "enforcement":"block"
                        }'::jsonb
                    )
                    """,
                    (revision_id,),
                )
    finally:
        connection.rollback()
        connection.close()
