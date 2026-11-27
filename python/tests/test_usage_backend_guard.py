"""Usage routing rejects stale SDK configuration during a cutover."""

from datetime import UTC, datetime, timedelta

import psycopg2
import pytest
from psycopg2.extras import Json

pytestmark = pytest.mark.integration
TENANT = "00000000-0000-0000-0000-000000000001"


def test_mode_guard_and_resumable_begin(pg_database_url: str) -> None:
    with psycopg2.connect(pg_database_url) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE bursar_client")
        cur.execute("SELECT bursar.bind_tenant_context(%s::uuid, 'test')", (TENANT,))
        cur.execute("SELECT bursar.check_usage_backend('postgres')")
        assert cur.fetchone() == ("postgres",)
        cur.execute("RESET ROLE")
    cutoff = datetime.now(UTC) - timedelta(days=1)
    with psycopg2.connect(pg_database_url) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE bursar_operator")
        cur.execute("SELECT bursar.begin_usage_backend_cutover(%s::uuid, 'clickhouse', %s)", (TENANT, cutoff))
        first = cur.fetchone()[0]
        cur.execute("SELECT bursar.begin_usage_backend_cutover(%s::uuid, 'clickhouse', now())", (TENANT,))
        assert cur.fetchone()[0] == first
    with psycopg2.connect(pg_database_url) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE bursar_client")
        cur.execute("SELECT bursar.bind_tenant_context(%s::uuid, 'test')", (TENANT,))
        with pytest.raises(psycopg2.Error) as error:
            cur.execute("SELECT bursar.check_usage_backend('postgres')")
        assert error.value.pgcode == "PZ002"
    with psycopg2.connect(pg_database_url) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE bursar_operator")
        cur.execute("SELECT bursar.abort_usage_backend_cutover(%s::uuid)", (TENANT,))


def test_cutover_replay_and_rollup_deduplication(pg_database_url: str) -> None:
    with psycopg2.connect(pg_database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('bursar.tenant_id', %s, true)", (TENANT,))
        cur.execute("SELECT set_config('bursar.provider_environment', 'test', true)")
        cur.execute("INSERT INTO bursar.subjects DEFAULT VALUES RETURNING id")
        subject = cur.fetchone()[0]
        cur.execute("INSERT INTO bursar.credit_accounts(subject_id, account_kind) VALUES (%s, 'personal')", (subject,))
        cur.execute("SELECT id FROM bursar.credit_accounts WHERE subject_id=%s", (subject,))
        account = cur.fetchone()[0]
        cur.execute(
            "SELECT * FROM bursar.charge_usage(%s, 'completion', 0, 'cutover-test', p_model => 'model')", (subject,)
        )
        charge = cur.fetchone()[0]
        cur.execute("SELECT sum(charge_count) FROM bursar.usage_daily_rollups WHERE account_id=%s", (account,))
        assert cur.fetchone()[0] == 1
    with psycopg2.connect(pg_database_url) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE bursar_operator")
        cur.execute("SELECT bursar.begin_usage_backend_cutover(%s, 'clickhouse', now()-interval '1 day')", (TENANT,))
        cur.execute("SELECT * FROM bursar.list_usage_cutover_postgres(%s, NULL, NULL, 100)", (TENANT,))
        item = cur.fetchone()[0]
        assert item["payload_available"] is True
        cur.execute(
            "SELECT bursar.verify_usage_cutover_item(%s,%s,%s,%s)", (TENANT, charge, item["event_at"], "a" * 64)
        )
        cur.execute("SELECT bursar.finish_usage_backend_cutover(%s,'clickhouse',1)", (TENANT,))
    with psycopg2.connect(pg_database_url) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE bursar_operator")
        cur.execute("SELECT bursar.begin_usage_backend_cutover(%s, 'postgres', now()-interval '1 day')", (TENANT,))
        cur.execute("RESET ROLE")
        cur.execute("SELECT set_config('bursar.tenant_id', %s, true)", (TENANT,))
        cur.execute("SELECT set_config('bursar.provider_environment', 'test', true)")
        cur.execute("DELETE FROM bursar.usage_charge_payloads WHERE charge_id=%s", (charge,))
        cur.execute("SET LOCAL ROLE bursar_operator")
        cur.execute("SELECT bursar.import_usage_cutover_item(%s,%s::jsonb)", (TENANT, Json(item)))
        assert cur.fetchone()[0] == item
        cur.execute("SELECT bursar.import_usage_cutover_item(%s,%s::jsonb)", (TENANT, Json(item)))
        assert cur.fetchone()[0] == item
        cur.execute(
            "SELECT bursar.verify_usage_cutover_item(%s,%s,%s,%s)", (TENANT, charge, item["event_at"], "a" * 64)
        )
        cur.execute("SELECT bursar.finish_usage_backend_cutover(%s,'postgres',1)", (TENANT,))
        cur.execute("RESET ROLE")
        cur.execute("SELECT sum(charge_count) FROM bursar.usage_daily_rollups WHERE account_id=%s", (account,))
        assert cur.fetchone()[0] == 1

    from bursar.credits.postgres.store import PostgresStore
    from bursar.credits.service import CreditsService

    with PostgresStore(pg_database_url, tenant_id=TENANT, provider_environment="test") as store:
        service = CreditsService(store=store)
        assert any(row.usage_id == str(charge) for row in service.list_usage_charges(str(subject)).items)
        now = datetime.now(UTC)
        assert (
            sum(row.entry_count for row in service.daily_spend(now - timedelta(days=1), now + timedelta(days=1))) == 1
        )


def test_cutover_retention_fences_only_its_tenant(pg_database_url: str) -> None:
    other = "00000000-0000-0000-0000-000000000002"
    with psycopg2.connect(pg_database_url) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE bursar_operator")
        cur.execute("SELECT bursar.create_tenant(%s, 'other-cutover', 'Other', 'test')", (other,))
        cur.execute("RESET ROLE")
        charges = []
        for tenant in (TENANT, other):
            cur.execute("SELECT set_config('bursar.tenant_id', %s, true)", (tenant,))
            cur.execute("SELECT set_config('bursar.provider_environment', 'test', true)")
            cur.execute("INSERT INTO bursar.subjects DEFAULT VALUES RETURNING id")
            subject = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO bursar.credit_accounts(subject_id, account_kind) VALUES (%s, 'personal')", (subject,)
            )
            cur.execute("SELECT * FROM bursar.charge_usage(%s, 'completion', 0, 'cutover-retention')", (subject,))
            charges.append(cur.fetchone()[0])
    with psycopg2.connect(pg_database_url) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE bursar_operator")
        cur.execute("SELECT bursar.begin_usage_backend_cutover(%s, 'clickhouse', now()-interval '1 day')", (TENANT,))
        original = cur.fetchone()[0]
        cur.execute("SELECT bursar.begin_usage_backend_cutover(%s, 'clickhouse', now()-interval '100 days')", (TENANT,))
        assert cur.fetchone()[0] == original
        cur.execute("SELECT bursar.run_storage_maintenance(now()+interval '100 days')")
        assert cur.fetchone()[0]["status"] == "completed"
        cur.execute(
            "SELECT bursar.run_storage_partition_maintenance('usage_charge_payloads',now()+interval '100 days')"
        )
        assert cur.fetchone()[0]["status"] == "completed"
        cur.execute(
            "SELECT bursar.run_storage_partition_maintenance('billing_event_payloads',now()+interval '100 days')"
        )
        assert cur.fetchone()[0]["status"] == "completed"
        cur.execute("RESET ROLE")
        cur.execute("SELECT set_config('bursar.tenant_id', %s, true)", (TENANT,))
        cur.execute("SELECT set_config('bursar.provider_environment', 'test', true)")
        cur.execute("SELECT count(*) FROM bursar.usage_charge_payloads WHERE charge_id=%s", (charges[0],))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT set_config('bursar.tenant_id', %s, true)", (other,))
        cur.execute("SELECT count(*) FROM bursar.usage_charge_payloads WHERE charge_id=%s", (charges[1],))
        assert cur.fetchone()[0] == 0
        cur.execute("SET LOCAL ROLE bursar_operator")
        cur.execute("SELECT bursar.abort_usage_backend_cutover(%s)", (TENANT,))
