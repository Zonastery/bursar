"""PostgreSQL integration coverage for the public v1 Bursar configuration."""

import time
from copy import deepcopy
from decimal import Decimal

import psycopg2
import pytest

from bursar.credits.postgres.store import PostgresStore, run_migrations
from bursar.credits.service import CreditsService
from bursar.credits.service_types import ReserveOptions, SettleOptions
from bursar.credits.store import CreateLeaseOptions, StoreError
from bursar.credits.types import ExecuteGrantProgramRequest
from bursar.metrics import UsageMetrics
from tests.conftest import TEST_TENANT_ID

pytestmark = [pytest.mark.integration]
USER_ID = "00000000-0000-0000-0000-000000000901"
REPLAY_USER_ID = "00000000-0000-0000-0000-000000000911"

CONFIG = {
    "version": 1,
    "pricing": {
        "operations": {
            "completion": {
                "measures": {
                    "input_tokens": {"unit": "token"},
                    "output_tokens": {"unit": "token"},
                },
                "dimensions": {"model": {"type": "string"}},
            }
        },
        "rate_cards": {
            "standard": {
                "operations": {
                    "completion": {
                        "rules": [
                            {
                                "when": {
                                    "model": {
                                        "op": "prefix",
                                        "value": "premium-",
                                    }
                                },
                                "charge": {
                                    "type": "expression",
                                    "formula": "input_tokens * 2 + output_tokens * 3",
                                },
                            }
                        ],
                        "unmatched": {
                            "action": "charge",
                            "charge": {
                                "type": "expression",
                                "formula": "input_tokens + output_tokens",
                            },
                        },
                    }
                }
            }
        },
    },
    "credits": {
        "accounting": {"unit": "credit", "scale": 6, "rounding": "half_up"},
        "buckets": {
            "grant": {
                "priority": 10,
                "expiry": {
                    "type": "after_grant",
                    "interval": {"unit": "day", "count": 7},
                    "timezone": "UTC",
                },
            },
            "purchased": {"priority": 20, "expiry": {"type": "never"}},
        },
        "default_bucket": "purchased",
    },
    "plans": {"pro": {"display_name": "Pro", "rank": 0, "rate_card": "standard"}},
}


@pytest.fixture
def store(pg_database_url: str) -> PostgresStore:
    return PostgresStore(pg_database_url, tenant_id=TEST_TENANT_ID)


def _ensure_user(store: PostgresStore) -> None:
    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute("INSERT INTO auth.users (id) VALUES (%s) ON CONFLICT DO NOTHING", [USER_ID])
        cursor.execute('INSERT INTO public."user" (id) VALUES (%s) ON CONFLICT DO NOTHING', [USER_ID])


def test_migrations_are_idempotent_and_detect_checksum_mismatch(
    pg_database_url: str,
) -> None:
    run_migrations(pg_database_url)
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version, checksum FROM bursar.schema_migrations ORDER BY version LIMIT 1")
        version, checksum = cursor.fetchone()
        cursor.execute(
            "UPDATE bursar.schema_migrations SET checksum = 'tampered' WHERE version = %s",
            [version],
        )

    try:
        with pytest.raises(StoreError, match="checksum mismatch"):
            run_migrations(pg_database_url)
    finally:
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE bursar.schema_migrations SET checksum = %s WHERE version = %s",
                [checksum, version],
            )

    run_migrations(pg_database_url)


def test_post_migration_sql_is_ordered_and_transactional(
    pg_database_url: str,
) -> None:
    table = "public.bursar_post_migration_sql_test"
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS {table}")

    try:
        run_migrations(
            pg_database_url,
            post_migration_sql=[
                ("create.sql", f"CREATE TABLE {table} (value integer NOT NULL);"),
                ("insert.sql", f"INSERT INTO {table} (value) VALUES (42);"),
            ],
        )
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT value FROM {table}")
            assert cursor.fetchone() == (42,)

        with pytest.raises(StoreError, match="post-migration SQL failed for broken.sql"):
            run_migrations(
                pg_database_url,
                post_migration_sql=[
                    ("update.sql", f"UPDATE {table} SET value = 99;"),
                    ("broken.sql", "SELECT * FROM public.table_that_does_not_exist;"),
                ],
            )

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT value FROM {table}")
            assert cursor.fetchone() == (42,)
    finally:
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")


def test_removed_compatibility_objects_are_absent(pg_database_url: str) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                to_regclass('bursar.credit_transactions'),
                to_regclass('bursar.user_credit_buckets'),
                to_regclass('bursar.user_credits'),
                to_regclass('bursar.credit_reservations')
            """
        )
        assert cursor.fetchone() == (None, None, None, None)

        cursor.execute(
            """
            SELECT proname
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'bursar'
              AND p.proname = ANY(%s)
            """,
            [
                [
                    "project_credit_transaction",
                    "list_transactions",
                    "list_transactions_cursor_with_total",
                ]
            ],
        )
        assert cursor.fetchall() == []


def test_add_credits_idempotent_replay_uses_one_ledger_entry(store: PostgresStore) -> None:
    service = CreditsService(store=store)
    service.publish_pricing_from_dict(CONFIG)
    first = service.add_credits(
        REPLAY_USER_ID,
        Decimal("25"),
        entry_type="purchase",
        idempotency_key="integration:add-replay",
    )
    replay = service.add_credits(
        REPLAY_USER_ID,
        Decimal("25"),
        entry_type="purchase",
        idempotency_key="integration:add-replay",
    )

    assert replay.entry_id == first.entry_id
    assert service.get_balance(REPLAY_USER_ID).balance == Decimal("25")


def test_public_config_round_trips_and_prices_generic_usage(store: PostgresStore) -> None:
    service = CreditsService(store=store)
    service.publish_pricing_from_dict(CONFIG)
    service.add_credits(
        USER_ID,
        Decimal("100"),
        "purchase",
        bucket="purchased",
        idempotency_key="new-schema-grant-1",
    )

    deduction = service.deduct(
        USER_ID,
        UsageMetrics(
            operation="completion", measures={"input_tokens": 2, "output_tokens": 4}, dimensions={"model": "premium-x"}
        ),
        idempotency_key="new-schema-charge-1",
    )

    assert deduction.amount == Decimal("16")
    assert service.get_balance(USER_ID).balance == Decimal("84")
    loaded = store.get_active_pricing()
    assert loaded is not None
    assert loaded.config["pricing"]["operations"]["completion"]


def test_lease_settlement_and_refund_follow_revamped_rpc_contracts(store: PostgresStore) -> None:
    config = deepcopy(CONFIG)
    config["entitlements"] = {
        "features": {
            "tutor_chat": {
                "type": "boolean",
                "default": False,
            }
        }
    }
    config["plans"]["pro"].update(
        {
            "allowed_operations": ["completion"],
            "features": {"tutor_chat": True},
            "quotas": {
                "completion_input": {
                    "operation": "completion",
                    "measure": "input_tokens",
                    "limit": "5",
                    "window": {
                        "type": "calendar",
                        "unit": "month",
                        "count": 1,
                        "timezone": "UTC",
                    },
                    "enforcement": "block",
                }
            },
        }
    )
    service = CreditsService(store=store)
    service.publish_pricing_from_dict(config)
    service.set_user_plan(USER_ID, "pro")
    service.add_credits(
        USER_ID,
        Decimal("100"),
        "purchase",
        bucket="purchased",
        idempotency_key="lease-contract-grant",
    )
    estimate = UsageMetrics(
        operation="completion",
        measures={"input_tokens": 5, "output_tokens": 2},
        dimensions={"model": "standard"},
    )
    actual = UsageMetrics(
        operation="completion",
        measures={"input_tokens": 3, "output_tokens": 1},
        dimensions={"model": "standard"},
    )

    lease = service.reserve(
        USER_ID,
        estimate,
        ReserveOptions(
            operation_type="completion",
            feature="tutor_chat",
            idempotency_key="lease-contract-reserve",
        ),
    )
    renewed = service.renew(USER_ID, lease.lease_id, ttl=300)

    # A plan/catalog change after admission must not reprice work already in
    # flight. The lease's immutable revision and rate card own settlement.
    changed_config = deepcopy(config)
    changed_config["pricing"]["rate_cards"]["standard"]["operations"]["completion"]["unmatched"]["charge"][
        "formula"
    ] = "input_tokens * 100 + output_tokens * 100"
    service.publish_pricing_from_dict(changed_config)
    service.set_user_plan(USER_ID, "pro")

    deduction = service.settle(
        USER_ID,
        lease.lease_id,
        actual,
        SettleOptions(
            feature="tutor_chat",
            idempotency_key="lease-contract-settle",
        ),
    )
    refund = service.refund_credits(
        deduction.entry_id,
        reason="integration_test",
        idempotency_key="lease-contract-refund",
    )
    replay = service.refund_credits(
        deduction.entry_id,
        reason="integration_test",
        idempotency_key="lease-contract-refund",
    )

    assert lease.expires_at is not None
    assert renewed.expires_at is not None
    assert deduction.amount == Decimal("4")
    assert deduction.balance_after == Decimal("96")
    assert refund.amount == Decimal("4")
    assert refund.new_balance == Decimal("100")
    assert replay.refund_entry_id == refund.refund_entry_id
    assert service.get_balance(USER_ID).balance == Decimal("100")

    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                COALESCE(sum(event.amount), 0),
                COALESCE(max(quota_window.consumed), 0)
            FROM bursar.credit_accounts AS account
            LEFT JOIN bursar.quota_usage_events AS event
              ON event.account_id = account.id
             AND event.quota_key = 'completion_input'
            LEFT JOIN bursar.quota_windows AS quota_window
              ON quota_window.account_id = account.id
             AND quota_window.quota_key = 'completion_input'
            WHERE account.subject_id = %s
            """,
            [USER_ID],
        )
        usage_total, cached_consumed = cursor.fetchone()

    assert usage_total == Decimal("0")
    assert cached_consumed == Decimal("0")


def test_bucket_priority_is_applied_by_postgres_store(store: PostgresStore) -> None:
    service = CreditsService(store=store)
    service.publish_pricing_from_dict(CONFIG)
    service.add_credits(
        USER_ID,
        Decimal("10"),
        "purchase",
        bucket="grant",
        idempotency_key="spend-order-grant",
    )
    service.add_credits(
        USER_ID,
        Decimal("10"),
        "purchase",
        bucket="purchased",
        idempotency_key="spend-order-purchased",
    )

    service.deduct(
        USER_ID,
        UsageMetrics(
            operation="completion",
            measures={"input_tokens": 5, "output_tokens": 0},
            dimensions={"model": "standard"},
        ),
        idempotency_key="new-schema-charge-2",
    )
    buckets = {row.bucket_key: row.balance for row in service.get_bucket_balances(USER_ID).buckets}
    assert buckets["grant"] == Decimal("5")
    assert buckets["purchased"] == Decimal("10")


def test_account_created_grant_program_posts_every_award(
    store: PostgresStore,
) -> None:
    config = deepcopy(CONFIG)
    config["credits"]["grant_programs"] = {
        "welcome": {
            "trigger": "account_created",
            "awards": [
                {
                    "recipient": "subject",
                    "amount": "2",
                    "bucket": "purchased",
                },
                {
                    "recipient": "subject",
                    "amount": "3",
                    "bucket": "purchased",
                },
            ],
            "max_awards_per_subject": 1,
            "idempotency_scope": "subject",
        }
    }
    service = CreditsService(store=store)
    service.publish_pricing_from_dict(config)

    service.add_credits(
        REPLAY_USER_ID,
        Decimal("1"),
        entry_type="purchase",
        idempotency_key="grant-program-trigger",
    )

    assert service.get_balance(REPLAY_USER_ID).balance == Decimal("6")
    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)
            FROM bursar.account_creation_grants
            WHERE subject_id = %s
            """,
            [REPLAY_USER_ID],
        )
        assert cursor.fetchone() == (2,)


def test_manual_grant_program_is_exposed_by_python_sdk(store: PostgresStore) -> None:
    config = deepcopy(CONFIG)
    config["credits"]["grant_programs"] = {
        "manual_bonus": {
            "trigger": "manual",
            "awards": [
                {
                    "recipient": "subject",
                    "amount": "4",
                    "bucket": "purchased",
                }
            ],
            "max_awards_per_subject": 1,
            "idempotency_scope": "event",
        }
    }
    service = CreditsService(store=store)
    service.publish_pricing_from_dict(config)
    request = ExecuteGrantProgramRequest(
        trigger="manual",
        program_key="manual_bonus",
        subject_id=USER_ID,
        event_key="manual-event-1",
        metadata={"campaign": "summer"},
    )

    awards = service.execute_grant_program(request)
    replay = service.execute_grant_program(request)

    assert len(awards) == 1
    assert awards[0].amount == Decimal("4")
    assert awards[0].recipient_subject_id == USER_ID
    assert awards[0].replayed is False
    assert len(replay) == 1
    assert replay[0].replayed is True
    assert service.get_balance(USER_ID).balance == Decimal("4")


def test_expire_leases_is_exposed_by_python_store(store: PostgresStore) -> None:
    service = CreditsService(store=store)
    service.publish_pricing_from_dict(CONFIG)
    service.add_credits(USER_ID, Decimal("10"), idempotency_key="lease-expiry-credit")
    lease = store.create_lease(
        USER_ID,
        Decimal("2"),
        "completion",
        CreateLeaseOptions(
            idempotency_key="lease-expiry-reserve",
            ttl_seconds=1,
        ),
    )
    time.sleep(1.1)

    assert store.expire_leases(25) == 1
    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT status FROM bursar.credit_leases WHERE id = %s", [lease.lease_id])
        assert cursor.fetchone() == ("expired",)


def test_remove_team_member_is_exposed_by_python_store(store: PostgresStore) -> None:
    team = store.create_team(USER_ID, "SDK team")
    store.add_team_member(team.team_id, REPLAY_USER_ID)

    assert store.remove_team_member(team.team_id, REPLAY_USER_ID) is True
    assert store.remove_team_member(team.team_id, REPLAY_USER_ID) is False
    assert store.remove_team_member(team.team_id, USER_ID) is False


def test_plan_policies_persist_as_typed_references(
    store: PostgresStore,
) -> None:
    config = deepcopy(CONFIG)
    config["pricing"]["operations"]["completion"]["measures"]["calls"] = {"unit": "call"}
    config["credits"]["policies"] = {"line": {"type": "credit_line", "limit": "20"}}
    config["admission"] = {
        "policies": {
            "pro": {
                "max_in_flight": 2,
                "operations": {"completion": {"max_in_flight": 1}},
            }
        }
    }
    config["plans"]["pro"].update(
        {
            "credit_policy": "line",
            "admission_policy": "pro",
            "quotas": {
                "completion_calls": {
                    "operation": "completion",
                    "measure": "calls",
                    "limit": "5",
                    "window": {
                        "type": "rolling",
                        "duration": {"unit": "hour", "count": 1},
                    },
                    "enforcement": "block",
                }
            },
        }
    )

    CreditsService(store=store).publish_pricing_from_dict(config)

    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT credit_policy_key, admission_policy_key
            FROM bursar.catalog_plans
            WHERE plan_key = 'pro'
              AND catalog_revision_id = (
                SELECT id
                FROM bursar.catalog_revisions
                WHERE status = 'active'
              )
            """
        )
        assert cursor.fetchone() == ("line", "pro")

        cursor.execute(
            """
            SELECT operation_key, measure_key, quota_limit, window_policy, enforcement
            FROM bursar.catalog_plan_quotas
            WHERE plan_key = 'pro'
              AND quota_key = 'completion_calls'
              AND catalog_revision_id = (
                SELECT id
                FROM bursar.catalog_revisions
                WHERE status = 'active'
              )
            """
        )
        operation, measure, quota_limit, window, enforcement = cursor.fetchone()

    assert operation == "completion"
    assert measure == "calls"
    assert quota_limit == Decimal("5")
    assert window == {
        "type": "rolling",
        "duration": {"unit": "hour", "count": 1},
    }
    assert enforcement == "block"
