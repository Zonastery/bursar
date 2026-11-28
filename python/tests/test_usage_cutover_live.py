"""Opt-in real ClickHouse/PostgreSQL cutover regression.

Run with BURSAR_TEST_CLICKHOUSE=1, bursar[postgres,clickhouse,test], and Docker.
The normal PostgreSQL fixture requires a disposable database.
"""

from __future__ import annotations

import importlib
import os
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg2
import pytest

from bursar.storage.adapters.clickhouse import ClickHouseUsageStore, ClickHouseUsageStoreOptions
from bursar.storage.cutover import _clickhouse_page, run_cutover
from bursar.storage.ports import UsageChargeExport
from bursar.storage.runtime import BursarRuntimeOptions, create_bursar_runtime
from tests.conftest import TEST_TENANT_ID
from tests.test_storage_integration import _operator_database_url, _seed_usage_row

pytestmark = pytest.mark.integration


@pytest.fixture
def clickhouse():
    if os.getenv("BURSAR_TEST_CLICKHOUSE") != "1":
        pytest.skip("set BURSAR_TEST_CLICKHOUSE=1 to run real ClickHouse cutover regressions")
    driver = importlib.import_module("clickhouse_connect")
    DockerContainer = importlib.import_module("testcontainers.core.container").DockerContainer
    container = (
        DockerContainer("clickhouse/clickhouse-server:25.8.33.6")
        .with_env("CLICKHOUSE_USER", "cutover")
        .with_env("CLICKHOUSE_PASSWORD", "disposable")
        .with_env("CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT", "1")
        .with_command("-- --background_schedule_pool_size=16")
        .with_exposed_ports(8123)
    )
    with container:
        for attempt in range(60):
            try:
                client = driver.get_client(
                    host=container.get_container_host_ip(),
                    port=int(container.get_exposed_port(8123)),
                    username="cutover",
                    password="disposable",
                )
                break
            except Exception:
                if attempt == 59:
                    raise
                time.sleep(1)
        try:
            yield client
        finally:
            client.close()


def _operator(dsn):
    connection = psycopg2.connect(dsn)
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE bursar_operator")
    return connection


def test_round_trip_retry_and_tenant_isolation(pg_database_url, clickhouse):
    charge_id, _ = _seed_usage_row(pg_database_url)
    with psycopg2.connect(pg_database_url) as seeded, seeded.cursor() as cursor:
        cursor.execute(
            "UPDATE bursar.usage_charge_payloads "
            "SET metadata = '{\"precision\":0.123456789012345678901234567890123456789}'::jsonb "
            "WHERE charge_id = %s::uuid",
            (charge_id,),
        )
    cutoff = datetime.now(UTC) - timedelta(days=1)
    store = ClickHouseUsageStore(
        ClickHouseUsageStoreOptions(
            client=clickhouse,
            tenant_id=UUID(TEST_TENANT_ID),
            create_table=True,
        )
    )
    store.initialize_schema()

    class FailOnce:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.failed = False

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def insert(self, *args, **kwargs):
            if not self.failed:
                self.failed = True
                raise RuntimeError("injected interruption")
            return self.wrapped.insert(*args, **kwargs)

    with closing(_operator(pg_database_url)) as conn:
        with pytest.raises(RuntimeError, match="injected interruption"):
            run_cutover(conn, FailOnce(clickhouse), TEST_TENANT_ID, "clickhouse", cutoff, "bursar_usage_events")
        assert run_cutover(conn, clickhouse, TEST_TENANT_ID, "clickhouse", cutoff, "bursar_usage_events") == 1
        assert run_cutover(conn, clickhouse, TEST_TENANT_ID, "postgres", cutoff, "bursar_usage_events") == 1
        original = _clickhouse_page(store, cutoff, None, None)[0]
        assert str(original["metadata"]["precision"]) == "0.123456789012345678901234567890123456789"
        other_tenant = uuid4()
        other_store = ClickHouseUsageStore(ClickHouseUsageStoreOptions(client=clickhouse, tenant_id=other_tenant))
        other_store.write_usage(
            UsageChargeExport.model_validate(original | {"tenant_id": str(other_tenant), "charge_id": str(uuid4())}),
            "0",
        )
        assert run_cutover(conn, clickhouse, TEST_TENANT_ID, "clickhouse", cutoff, "bursar_usage_events") == 1
        assert run_cutover(conn, clickhouse, TEST_TENANT_ID, "postgres", cutoff, "bursar_usage_events") == 1
        store.write_usage(UsageChargeExport.model_validate(original | {"charge_id": str(uuid4())}), "0")
        with pytest.raises(RuntimeError, match="extra retained usage records"):
            run_cutover(conn, clickhouse, TEST_TENANT_ID, "clickhouse", cutoff, "bursar_usage_events")
        with conn.cursor() as cursor:
            cursor.execute("SELECT bursar.abort_usage_backend_cutover(%s::uuid)", (TEST_TENANT_ID,))
    with psycopg2.connect(pg_database_url) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM bursar.usage_charge_payloads WHERE tenant_id = %s", (TEST_TENANT_ID,))
        assert cursor.fetchone()[0] == 1


def test_driver_lock_blocks_concurrent_cutover(pg_database_url, clickhouse):
    store = ClickHouseUsageStore(
        ClickHouseUsageStoreOptions(
            client=clickhouse,
            tenant_id=UUID(TEST_TENANT_ID),
            table="lock_test",
            create_table=True,
        )
    )
    store.initialize_schema()
    with closing(_operator(pg_database_url)) as first, closing(_operator(pg_database_url)) as second:
        with first.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_lock(hashtextextended('bursar.usage.cutover.driver:' || %s, 0))", (TEST_TENANT_ID,)
            )
        with pytest.raises(RuntimeError, match="another cutover driver"):
            run_cutover(second, clickhouse, TEST_TENANT_ID, "clickhouse", datetime.now(UTC), "lock_test")


def test_live_worker_preserves_precise_usage_json_through_reverse_cutover(pg_database_url, clickhouse):
    cutoff = datetime.now(UTC) - timedelta(days=1)
    options = ClickHouseUsageStoreOptions(client=clickhouse, tenant_id=UUID(TEST_TENANT_ID), create_table=True)
    store = ClickHouseUsageStore(options)
    store.initialize_schema()
    with closing(_operator(pg_database_url)) as operator:
        assert run_cutover(operator, clickhouse, TEST_TENANT_ID, "clickhouse", cutoff, "bursar_usage_events") == 0
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT set_config('bursar.tenant_id', %s, true)", (TEST_TENANT_ID,))
            cursor.execute("SELECT set_config('bursar.provider_environment', 'test', true)")
            cursor.execute("SELECT set_config('bursar.usage_backend', 'clickhouse', true)")
            cursor.execute("INSERT INTO bursar.subjects DEFAULT VALUES RETURNING id")
            subject = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO bursar.credit_accounts(subject_id, account_kind) VALUES (%s, 'personal')", (subject,)
            )
            cursor.execute(
                """
                SELECT charge_id FROM bursar.charge_usage(
                    %s, 'completion', 0, 'precise-worker',
                    p_measures => '{"tokens":0.123456789012345678901234567890123456789}'::jsonb,
                    p_metadata => '{"precision":0.123456789012345678901234567890123456789}'::jsonb
                )
            """,
                (subject,),
            )
            charge = cursor.fetchone()[0]
            cursor.execute("INSERT INTO bursar.subjects DEFAULT VALUES RETURNING id")
            other_subject = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO bursar.credit_accounts(subject_id, account_kind) VALUES (%s, 'personal')", (other_subject,)
            )
            cursor.execute(
                "SELECT charge_id FROM bursar.record_usage(%s, 'completion', 5, 'record-only')", (other_subject,)
            )
            assert cursor.fetchone()[0] is not None
        runtime = create_bursar_runtime(
            BursarRuntimeOptions(
                postgres=pg_database_url,
                operator_postgres=_operator_database_url(pg_database_url),
                tenant_id=UUID(TEST_TENANT_ID),
                provider_environment="test",
                clickhouse=options,
            )
        )
        try:
            result = runtime.flush()
            assert result.claimed == 2 and result.delivered == 2 and result.failed == 0
            stats = store.aggregate_stats(cutoff, datetime.now(UTC) + timedelta(minutes=1))
            assert stats.active_users == 1
            assert stats.top_user == str(subject)
        finally:
            runtime.close()
        assert run_cutover(operator, clickhouse, TEST_TENANT_ID, "postgres", cutoff, "bursar_usage_events") == 2
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT measures::text, metadata::text FROM bursar.usage_charge_payloads WHERE charge_id=%s", (charge,)
        )
        measures, metadata = cursor.fetchone()
        assert "0.123456789012345678901234567890123456789" in measures
        assert "0.123456789012345678901234567890123456789" in metadata


def test_live_usage_history_cursor_keeps_microseconds(pg_database_url, clickhouse):
    _seed_usage_row(pg_database_url)
    cutoff = datetime.now(UTC) - timedelta(days=1)
    store = ClickHouseUsageStore(
        ClickHouseUsageStoreOptions(client=clickhouse, tenant_id=UUID(TEST_TENANT_ID), create_table=True)
    )
    store.initialize_schema()
    with closing(_operator(pg_database_url)) as operator:
        assert run_cutover(operator, clickhouse, TEST_TENANT_ID, "clickhouse", cutoff, "bursar_usage_events") == 1
    original = _clickhouse_page(store, cutoff, None, None)[0]
    base = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
    first_id, second_id = str(uuid4()), str(uuid4())
    for charge_id, micros in ((first_id, 900), (second_id, 100)):
        item = original | {"charge_id": charge_id, "event_at": base.replace(microsecond=micros).isoformat()}
        store.write_usage(UsageChargeExport.model_validate(item), "0")
    first = store.list_usage_charges(original["subject_id"], limit=1)
    assert first.items[0].usage_id == first_id
    assert first.next_cursor is not None
    second = store.list_usage_charges(original["subject_id"], limit=1, cursor=first.next_cursor)
    assert second.items[0].usage_id == second_id


def test_cutover_rejects_clickhouse_rows_older_than_cutoff(pg_database_url, clickhouse):
    _seed_usage_row(pg_database_url)
    cutoff = datetime.now(UTC) - timedelta(days=1)
    store = ClickHouseUsageStore(
        ClickHouseUsageStoreOptions(client=clickhouse, tenant_id=UUID(TEST_TENANT_ID), create_table=True)
    )
    store.initialize_schema()
    with closing(_operator(pg_database_url)) as operator:
        assert run_cutover(operator, clickhouse, TEST_TENANT_ID, "clickhouse", cutoff, "bursar_usage_events") == 1
        original = _clickhouse_page(store, cutoff, None, None)[0]
        old_id = str(uuid4())
        old = original | {"charge_id": old_id, "event_at": (cutoff - timedelta(days=1)).isoformat()}
        store.write_usage(UsageChargeExport.model_validate(old), "0")
        with pytest.raises(RuntimeError, match="retained usage before the cutoff"):
            run_cutover(operator, clickhouse, TEST_TENANT_ID, "postgres", cutoff, "bursar_usage_events")
        clickhouse.command(
            f"ALTER TABLE bursar_usage_events DELETE WHERE charge_id = toUUID('{old_id}') SETTINGS mutations_sync=1"
        )
        assert run_cutover(operator, clickhouse, TEST_TENANT_ID, "postgres", cutoff, "bursar_usage_events") == 1
        store.write_usage(UsageChargeExport.model_validate(old), "0")
        with pytest.raises(RuntimeError, match="retained usage before the cutoff"):
            run_cutover(operator, clickhouse, TEST_TENANT_ID, "clickhouse", cutoff, "bursar_usage_events")
        with operator.cursor() as cursor:
            cursor.execute("SELECT bursar.abort_usage_backend_cutover(%s::uuid)", (TEST_TENANT_ID,))


def test_wrong_provider_environment_does_not_pause_tenant(pg_database_url, clickhouse):
    store = ClickHouseUsageStore(
        ClickHouseUsageStoreOptions(client=clickhouse, tenant_id=UUID(TEST_TENANT_ID), create_table=True)
    )
    store.initialize_schema()
    with closing(_operator(pg_database_url)) as operator:
        with pytest.raises(psycopg2.Error):
            run_cutover(
                operator,
                clickhouse,
                TEST_TENANT_ID,
                "clickhouse",
                datetime.now(UTC),
                "bursar_usage_events",
                provider_environment="live",
            )
        with operator.cursor() as cursor:
            cursor.execute("SELECT bursar.get_usage_backend_status(%s::uuid)", (TEST_TENANT_ID,))
            assert cursor.fetchone()[0] == {"backend": "postgres", "target": None, "cutoff": None}


@pytest.mark.parametrize("required", [False, True])
def test_expired_optional_notification_does_not_block_but_required_does(pg_database_url, clickhouse, required):
    store = ClickHouseUsageStore(
        ClickHouseUsageStoreOptions(client=clickhouse, tenant_id=UUID(TEST_TENANT_ID), create_table=True)
    )
    store.initialize_schema()
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT set_config('bursar.tenant_id', %s, true)", (TEST_TENANT_ID,))
        cursor.execute("SELECT set_config('bursar.provider_environment', 'test', true)")
        cursor.execute(
            """
            INSERT INTO bursar.event_outbox(topic, aggregate_type, aggregate_id, idempotency_key, payload)
            VALUES ('usage.charge_recorded', 'credit_usage_charge', %s, 'expired-pointer',
                    jsonb_build_object('delivery_required', %s))
            RETURNING id
        """,
            (str(uuid4()), required),
        )
        event_id = cursor.fetchone()[0]
    with closing(_operator(pg_database_url)) as operator:
        if required:
            with pytest.raises(RuntimeError, match=r"unavailable|expired|matching tenant charge"):
                run_cutover(
                    operator, clickhouse, TEST_TENANT_ID, "clickhouse", datetime.now(UTC), "bursar_usage_events"
                )
        else:
            assert (
                run_cutover(
                    operator, clickhouse, TEST_TENANT_ID, "clickhouse", datetime.now(UTC), "bursar_usage_events"
                )
                == 0
            )
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM bursar.event_outbox WHERE id=%s", (event_id,))
            status = cursor.fetchone()[0]
            assert (status == "delivered") is not required
        if required:
            with operator.cursor() as cursor:
                cursor.execute("SELECT bursar.abort_usage_backend_cutover(%s::uuid)", (TEST_TENANT_ID,))
