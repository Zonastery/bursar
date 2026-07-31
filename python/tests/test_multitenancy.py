"""End-to-end shared-table tenant isolation regressions."""

import json
from copy import deepcopy
from decimal import Decimal

import psycopg2
import pytest
from psycopg2 import sql

from bursar.billing.postgres.store import PostgresBillingStore
from bursar.credits.postgres.store import PostgresStore
from bursar.credits.store import StoreError
from tests.conftest import TEST_TENANT_ID, TEST_TENANT_SLUG
from tests.test_store_integration import CONFIG

pytestmark = [pytest.mark.integration]

SECOND_TENANT_ID = "00000000-0000-0000-0000-000000000002"
SHARED_SUBJECT_ID = "00000000-0000-0000-0000-000000000099"

OPERATOR_FUNCTIONS = (
    "bursar.get_storage_settings()",
    (
        "bursar.configure_storage(integer,integer,integer,integer,integer,"
        "integer,integer,integer,integer,integer,integer,integer,integer,"
        "integer,integer)"
    ),
    "bursar.claim_outbox_events(integer,integer,text[])",
    "bursar.claim_outbox_events(uuid,integer,integer,text[])",
    "bursar.export_usage_charge(uuid)",
    "bursar.export_billing_event_payload(uuid)",
    "bursar.complete_outbox_event(bigint,uuid)",
    "bursar.archive_billing_event_payload(uuid,text,text,boolean)",
    "bursar.fail_outbox_event(bigint,uuid,text,integer,integer)",
    "bursar.drop_expired_storage_partitions(text,timestamptz,integer,integer)",
    "bursar.run_storage_partition_maintenance(text,timestamptz)",
    "bursar.run_storage_maintenance(timestamptz)",
    "bursar.maybe_run_storage_maintenance(timestamptz)",
    "bursar.secure_tenant_partition(regclass)",
    "bursar.create_tenant(uuid,text,text)",
    "bursar.set_tenant_status(uuid,text)",
)


def _ensure_second_tenant(pg_database_url: str) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT bursar.create_tenant(%s, %s, %s)",
            (SECOND_TENANT_ID, "second-tenant", "Second tenant"),
        )


def test_tenant_provisioning_rejects_slug_reuse_with_another_id(
    pg_database_url: str,
) -> None:
    with (
        psycopg2.connect(pg_database_url) as connection,
        connection.cursor() as cursor,
        pytest.raises(
            psycopg2.errors.UniqueViolation,
            match="tenant slug is already assigned to another id",
        ),
    ):
        cursor.execute(
            "SELECT bursar.create_tenant(%s, %s, %s)",
            (SECOND_TENANT_ID, TEST_TENANT_SLUG, "Wrong identity"),
        )


def test_runtime_role_is_fail_closed(pg_database_url: str) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                rolcanlogin,
                rolsuper,
                rolcreatedb,
                rolcreaterole,
                rolinherit,
                rolreplication,
                rolbypassrls
            FROM pg_roles
            WHERE rolname = 'bursar_runtime'
            """
        )
        assert cursor.fetchone() == (False, False, False, False, False, False, False)

        cursor.execute(
            """
            SELECT count(*)
            FROM pg_auth_members AS membership
            JOIN pg_roles AS member_role
              ON member_role.oid = membership.member
            WHERE member_role.rolname = 'bursar_runtime'
            """
        )
        assert cursor.fetchone() == (0,)


def test_tenant_aware_host_trigger_assigns_default_plan_and_signup_grants(
    pg_database_url: str,
) -> None:
    subject_id = "00000000-0000-0000-0000-000000000101"
    config = deepcopy(CONFIG)
    config["catalog"] = {"default_plan": "pro"}
    config["credits"]["grant_programs"] = {
        "welcome": {
            "trigger": "account_created",
            "awards": [
                {
                    "recipient": "subject",
                    "amount": "2",
                    "bucket": "purchased",
                }
            ],
            "max_awards_per_subject": 1,
            "idempotency_scope": "subject",
        }
    }
    store = PostgresStore(pg_database_url, tenant_id=TEST_TENANT_ID)
    try:
        store.set_active_pricing(config, "host-trigger")
    finally:
        store.close()

    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                CREATE TEMP TABLE host_trigger_users (
                    id uuid PRIMARY KEY
                );
                CREATE TRIGGER host_trigger_account_created
                AFTER INSERT ON host_trigger_users
                FOR EACH ROW
                EXECUTE FUNCTION
                    bursar.provision_subject_account_on_insert({});
                """
            ).format(sql.Literal(TEST_TENANT_SLUG))
        )
        cursor.execute(
            "INSERT INTO host_trigger_users(id) VALUES (%s)",
            (subject_id,),
        )
        cursor.execute(
            """
            SELECT plan.plan_key, account.balance
            FROM bursar.credit_accounts AS account
            JOIN bursar.account_plan_assignments AS assignment
              ON assignment.account_id = account.id
             AND assignment.tenant_id = account.tenant_id
            JOIN bursar.catalog_plans AS plan
              ON plan.id = assignment.plan_id
             AND plan.tenant_id = assignment.tenant_id
            WHERE account.tenant_id = %s
              AND account.subject_id = %s
              AND account.account_kind = 'personal'
            """,
            (TEST_TENANT_ID, subject_id),
        )
        assert cursor.fetchone() == ("pro", Decimal("2"))

        cursor.execute(
            """
            SELECT count(*)
            FROM bursar.grant_award_executions AS execution
            JOIN bursar.grant_program_events AS event
              ON event.id = execution.grant_event_id
            WHERE execution.tenant_id = %s
              AND event.subject_id = %s
            """,
            (TEST_TENANT_ID, subject_id),
        )
        assert cursor.fetchone() == (1,)


def test_tenant_aware_host_trigger_rejects_unknown_tenant(
    pg_database_url: str,
) -> None:
    with (
        psycopg2.connect(pg_database_url) as connection,
        connection.cursor() as cursor,
        pytest.raises(
            psycopg2.errors.ObjectNotInPrerequisiteState,
            match="Bursar tenant is not provisioned or active",
        ),
    ):
        cursor.execute(
            """
            CREATE TEMP TABLE unknown_tenant_trigger_users (
                id uuid PRIMARY KEY
            );
            CREATE TRIGGER unknown_tenant_account_created
            AFTER INSERT ON unknown_tenant_trigger_users
            FOR EACH ROW
            EXECUTE FUNCTION
                bursar.provision_subject_account_on_insert('missing-tenant');
            INSERT INTO unknown_tenant_trigger_users(id)
            VALUES ('00000000-0000-0000-0000-000000000102');
            """
        )


def test_tenants_isolate_catalog_credit_and_provider_idempotency(
    pg_database_url: str,
) -> None:
    _ensure_second_tenant(pg_database_url)

    first = PostgresStore(pg_database_url, tenant_id=TEST_TENANT_ID)
    second = PostgresStore(pg_database_url, tenant_id=SECOND_TENANT_ID)
    first_billing = PostgresBillingStore(
        pg_database_url,
        tenant_id=TEST_TENANT_ID,
    )
    second_billing = PostgresBillingStore(
        pg_database_url,
        tenant_id=SECOND_TENANT_ID,
    )
    try:
        first_catalog_id = first.set_active_pricing(CONFIG, "first")
        assert second.get_active_pricing() is None
        second_catalog_id = second.set_active_pricing(CONFIG, "second")

        assert first_catalog_id != second_catalog_id
        first_active = first.get_active_pricing()
        second_active = second.get_active_pricing()
        assert first_active is not None
        assert second_active is not None
        assert first_active.version == 1
        assert second_active.version == 1

        first.add_credits(
            SHARED_SUBJECT_ID,
            Decimal("10"),
            idempotency_key="same-credit-key",
        )
        second.add_credits(
            SHARED_SUBJECT_ID,
            Decimal("20"),
            idempotency_key="same-credit-key",
        )
        assert first.get_balance(SHARED_SUBJECT_ID).balance == Decimal("10")
        assert second.get_balance(SHARED_SUBJECT_ID).balance == Decimal("20")

        first_claim = first_billing.claim_billing_event(
            "stripe",
            "same-provider-event",
            "invoice.paid",
        )
        second_claim = second_billing.claim_billing_event(
            "stripe",
            "same-provider-event",
            "invoice.paid",
        )
        assert first_claim.status == "claimed"
        assert second_claim.status == "claimed"
        assert first_claim.billing_event_id != second_claim.billing_event_id

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT bursar.set_tenant_status(%s, 'suspended')",
                (SECOND_TENANT_ID,),
            )
        assert second.get_balance(SHARED_SUBJECT_ID).balance == Decimal("0")
        with pytest.raises(StoreError):
            second.add_credits(
                SHARED_SUBJECT_ID,
                Decimal("1"),
                idempotency_key="blocked-while-suspended",
            )
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT bursar.set_tenant_status(%s, 'active')",
                (SECOND_TENANT_ID,),
            )
        assert second.get_balance(SHARED_SUBJECT_ID).balance == Decimal("20")
    finally:
        first.close()
        second.close()
        first_billing.close()
        second_billing.close()


def test_tenant_rpc_fails_closed_without_context(pg_database_url: str) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SET ROLE service_role")
        with pytest.raises(psycopg2.Error) as exc_info:
            cursor.execute(
                """
                SELECT * FROM bursar.post_credit(
                    %s,
                    'adjustment',
                    1,
                    'tenant-context-test',
                    'tenant-context-test'
                )
                """,
                (SHARED_SUBJECT_ID,),
            )
        assert exc_info.value.pgcode == "28000"


def test_tenant_outbox_claim_does_not_take_other_tenant_work(
    pg_database_url: str,
) -> None:
    _ensure_second_tenant(pg_database_url)
    first_billing = PostgresBillingStore(
        pg_database_url,
        tenant_id=TEST_TENANT_ID,
    )
    second_billing = PostgresBillingStore(
        pg_database_url,
        tenant_id=SECOND_TENANT_ID,
    )
    first_event = first_billing.claim_billing_event(
        "stripe",
        "storage-first",
        "invoice.paid",
        {"tenant": "first"},
    )
    second_event = second_billing.claim_billing_event(
        "stripe",
        "storage-second",
        "invoice.paid",
        {"tenant": "second"},
    )
    assert first_event.billing_event_id is not None
    assert first_event.claim_token is not None
    assert second_event.billing_event_id is not None

    first_billing.complete_billing_event(
        "stripe",
        "storage-first",
        first_event.claim_token,
    )

    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        for tenant_id, suffix in (
            (TEST_TENANT_ID, "first"),
            (SECOND_TENANT_ID, "second"),
        ):
            cursor.execute(
                "SELECT set_config('bursar.tenant_id', %s, true)",
                (tenant_id,),
            )
            cursor.execute(
                """
                INSERT INTO bursar.event_outbox(
                    topic,
                    aggregate_type,
                    aggregate_id,
                    idempotency_key,
                    payload
                )
                VALUES (
                    'tenant-test',
                    'tenant-test',
                    %s,
                    %s,
                    '{}'::jsonb
                )
                """,
                (tenant_id, f"tenant-test-{suffix}"),
            )

        cursor.execute("SET ROLE service_role")
        cursor.execute(
            """
            SELECT event_id, tenant_id, claim_token
            FROM bursar.claim_outbox_events(
                %s,
                10,
                60,
                ARRAY['tenant-test']
            )
            """,
            (TEST_TENANT_ID,),
        )
        claimed = cursor.fetchone()
        assert claimed is not None
        event_id, tenant_id, claim_token = claimed
        assert str(tenant_id) == TEST_TENANT_ID

        cursor.execute(
            "SELECT bursar.complete_outbox_event(%s, %s)",
            (event_id, claim_token),
        )
        assert cursor.fetchone() == (True,)

        cursor.execute(
            """
            SELECT event_id, tenant_id, claim_token
            FROM bursar.claim_outbox_events(
                %s,
                10,
                60,
                ARRAY['tenant-test']
            )
            """,
            (SECOND_TENANT_ID,),
        )
        second_claimed = cursor.fetchone()
        assert second_claimed is not None
        second_outbox_id, second_tenant_id, second_claim_token = second_claimed
        assert str(second_tenant_id) == SECOND_TENANT_ID
        cursor.execute(
            "SELECT bursar.fail_outbox_event(%s, %s, %s, 0, 10)",
            (second_outbox_id, second_claim_token, "retry"),
        )
        assert cursor.fetchone() == (True,)

        cursor.execute(
            "SELECT bursar.export_billing_event_payload(%s)",
            (first_event.billing_event_id,),
        )
        exported = cursor.fetchone()
        assert exported is not None
        assert exported[0]["tenant_id"] == TEST_TENANT_ID
        assert exported[0]["envelope"]["tenant"] == "first"

        cursor.execute(
            "SELECT bursar.archive_billing_event_payload(%s, %s, %s, false)",
            (
                first_event.billing_event_id,
                "tenants/first/billing-events/storage-first.json",
                "v1",
            ),
        )
        assert cursor.fetchone() == (True,)

    first_billing.close()
    second_billing.close()


def test_service_role_and_jwt_context_preserve_tenant_isolation(
    pg_database_url: str,
) -> None:
    _ensure_second_tenant(pg_database_url)
    first = PostgresStore(pg_database_url, tenant_id=TEST_TENANT_ID)
    second = PostgresStore(pg_database_url, tenant_id=SECOND_TENANT_ID)
    try:
        first.set_active_pricing(CONFIG, "jwt-first")
        second.set_active_pricing(CONFIG, "jwt-second")
        first.add_credits(
            SHARED_SUBJECT_ID,
            Decimal("10"),
            idempotency_key="jwt-first",
        )
        second.add_credits(
            SHARED_SUBJECT_ID,
            Decimal("20"),
            idempotency_key="jwt-second",
        )

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT set_config('bursar.tenant_id', '', true)")
            cursor.execute(
                "SELECT set_config('request.jwt.claims', %s, true)",
                (
                    json.dumps(
                        {
                            "app_metadata": {"tenant_id": TEST_TENANT_ID},
                            "user_metadata": {"tenant_id": SECOND_TENANT_ID},
                        }
                    ),
                ),
            )
            cursor.execute("SELECT bursar.current_tenant_id()")
            assert str(cursor.fetchone()[0]) == TEST_TENANT_ID

            cursor.execute("SET ROLE service_role")
            cursor.execute(
                "SELECT balance FROM bursar.get_credit_state(%s)",
                (SHARED_SUBJECT_ID,),
            )
            assert cursor.fetchone()[0] == Decimal("10")

            cursor.execute(
                "SELECT set_config('request.jwt.claims', %s, true)",
                (json.dumps({"app_metadata": {"tenant_id": SECOND_TENANT_ID}}),),
            )
            cursor.execute(
                "SELECT balance FROM bursar.get_credit_state(%s)",
                (SHARED_SUBJECT_ID,),
            )
            assert cursor.fetchone()[0] == Decimal("20")

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT set_config('bursar.tenant_id', '', true)")
            cursor.execute(
                "SELECT set_config('request.jwt.claims', %s, true)",
                (json.dumps({"user_metadata": {"tenant_id": SECOND_TENANT_ID}}),),
            )
            cursor.execute("SELECT bursar.current_tenant_id()")
            assert cursor.fetchone() == (None,)
            cursor.execute("SET ROLE service_role")
            cursor.execute(
                "SELECT balance FROM bursar.get_credit_state(%s)",
                (SHARED_SUBJECT_ID,),
            )
            assert cursor.fetchone() is None
            with pytest.raises(psycopg2.Error) as exc_info:
                cursor.execute(
                    """
                    SELECT * FROM bursar.post_credit(
                        %s,
                        'adjustment',
                        1,
                        'user-metadata-is-not-authority',
                        'user-metadata-is-not-authority'
                    )
                    """,
                    (SHARED_SUBJECT_ID,),
                )
            assert exc_info.value.pgcode == "28000"
    finally:
        first.close()
        second.close()


def test_runtime_role_cannot_execute_operator_functions(
    pg_database_url: str,
) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT function_name
            FROM unnest(%s::text[]) AS denied(function_name)
            WHERE has_function_privilege(
                'bursar_runtime',
                function_name,
                'EXECUTE'
            )
            ORDER BY function_name
            """,
            (list(OPERATOR_FUNCTIONS),),
        )
        assert cursor.fetchall() == []

        for allowed_function in (
            "bursar.ensure_storage_partition(text,timestamptz,boolean)",
            "bursar.is_nonempty_text(text)",
            "bursar.get_credit_state(uuid)",
            "bursar.resolve_active_tenant_for_trigger(text)",
        ):
            cursor.execute(
                "SELECT has_function_privilege('bursar_runtime', %s, 'EXECUTE')",
                (allowed_function,),
            )
            assert cursor.fetchone() == (True,)

        cursor.execute(
            """
            SELECT pg_get_userbyid(function_info.proowner)
            FROM pg_proc AS function_info
            WHERE function_info.oid = 'bursar.get_credit_state(uuid)'::regprocedure
            """
        )
        assert cursor.fetchone() == ("bursar_runtime",)

        cursor.execute(
            """
            SELECT
                pg_get_userbyid(resolver.proowner) <> 'bursar_runtime',
                pg_get_userbyid(trigger_hook.proowner) = 'bursar_runtime',
                has_function_privilege(
                    current_user,
                    trigger_hook.oid,
                    'EXECUTE'
                ),
                has_function_privilege(
                    'service_role',
                    trigger_hook.oid,
                    'EXECUTE'
                )
            FROM pg_proc AS resolver
            CROSS JOIN pg_proc AS trigger_hook
            WHERE resolver.oid =
                'bursar.resolve_active_tenant_for_trigger(text)'::regprocedure
              AND trigger_hook.oid =
                'bursar.provision_subject_account_on_insert()'::regprocedure
            """
        )
        assert cursor.fetchone() == (True, True, True, False)


def test_partition_children_are_forced_rls_and_not_service_accessible(
    pg_database_url: str,
) -> None:
    billing = PostgresBillingStore(
        pg_database_url,
        tenant_id=TEST_TENANT_ID,
    )
    try:
        billing.claim_billing_event(
            "stripe",
            "partition-boundary",
            "invoice.paid",
            {"tenant": "first"},
        )
    finally:
        billing.close()

    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT partition.partition_table
            FROM bursar.storage_partitions AS partition
            WHERE partition.parent_table = 'billing_event_payloads'
              AND partition.range_start <= now()
              AND partition.range_end > now()
            """
        )
        partition_row = cursor.fetchone()
        assert partition_row is not None
        partition_table = partition_row[0]

        cursor.execute(
            """
            SELECT
                table_info.relrowsecurity,
                table_info.relforcerowsecurity,
                EXISTS (
                    SELECT 1
                    FROM pg_policy AS policy
                    WHERE policy.polrelid = table_info.oid
                      AND policy.polname LIKE 'tenant_isolation_%%'
                )
            FROM pg_class AS table_info
            WHERE table_info.oid = %s::regclass
            """,
            (f"bursar.{partition_table}",),
        )
        assert cursor.fetchone() == (True, True, True)

        cursor.execute("SET ROLE service_role")
        with pytest.raises(psycopg2.Error) as exc_info:
            cursor.execute(sql.SQL("SELECT count(*) FROM bursar.{}").format(sql.Identifier(partition_table)))
        assert exc_info.value.pgcode == "42501"


def test_catalog_activation_and_plan_migration_are_tenant_scoped(
    pg_database_url: str,
) -> None:
    _ensure_second_tenant(pg_database_url)
    first = PostgresStore(pg_database_url, tenant_id=TEST_TENANT_ID)
    second = PostgresStore(pg_database_url, tenant_id=SECOND_TENANT_ID)
    try:
        first.set_active_pricing(CONFIG, "lock-first")
        second.set_active_pricing(CONFIG, "lock-second")
    finally:
        first.close()
        second.close()

    with (
        psycopg2.connect(pg_database_url) as first_connection,
        psycopg2.connect(pg_database_url) as second_connection,
        first_connection.cursor() as first_cursor,
        second_connection.cursor() as second_cursor,
    ):
        first_cursor.execute(
            "SELECT set_config('bursar.tenant_id', %s, true)",
            (TEST_TENANT_ID,),
        )
        second_cursor.execute(
            "SELECT set_config('bursar.tenant_id', %s, true)",
            (SECOND_TENANT_ID,),
        )
        second_cursor.execute("SET LOCAL statement_timeout = '2s'")

        first_cursor.execute("SELECT bursar.activate_catalog_revision(1)")
        second_cursor.execute("SELECT bursar.activate_catalog_revision(1)")

        first_cursor.execute(
            """
            SELECT plan.id
            FROM bursar.catalog_plans AS plan
            JOIN bursar.catalog_revisions AS revision
              ON revision.id = plan.catalog_revision_id
            WHERE plan.plan_key = 'pro'
              AND revision.status = 'active'
            """
        )
        first_target = first_cursor.fetchone()[0]
        second_cursor.execute(
            """
            SELECT plan.id
            FROM bursar.catalog_plans AS plan
            JOIN bursar.catalog_revisions AS revision
              ON revision.id = plan.catalog_revision_id
            WHERE plan.plan_key = 'pro'
              AND revision.status = 'active'
            """
        )
        second_target = second_cursor.fetchone()[0]

        first_cursor.execute(
            "SELECT bursar.start_plan_migration(NULL, %s::uuid)",
            (first_target,),
        )
        first_migration = first_cursor.fetchone()[0]
        second_cursor.execute(
            "SELECT bursar.start_plan_migration(NULL, %s::uuid)",
            (second_target,),
        )
        second_migration = second_cursor.fetchone()[0]

        first_cursor.execute(
            "SELECT * FROM bursar.migrate_plan_batch(%s::uuid, 100)",
            (first_migration,),
        )
        second_cursor.execute(
            "SELECT * FROM bursar.migrate_plan_batch(%s::uuid, 100)",
            (second_migration,),
        )
