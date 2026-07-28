"""PostgreSQL integration coverage for the public v1 Bursar configuration."""

from copy import deepcopy
from decimal import Decimal

import psycopg2
import pytest

from bursar.credits.postgres.store import PostgresStore, run_migrations
from bursar.credits.service import CreditsService
from bursar.credits.store import StoreError
from bursar.metrics import UsageMetrics

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
    "plans": {"pro": {"display_name": "Pro", "rate_card": "standard"}},
}


@pytest.fixture
def store(pg_database_url: str) -> PostgresStore:
    return PostgresStore(pg_database_url)


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


def test_plan_policies_project_from_typed_references(
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
            SELECT spending, limits, quotas
            FROM bursar.catalog_plans
            WHERE plan_key = 'pro'
              AND catalog_revision_id = (
                SELECT id
                FROM bursar.catalog_revisions
                WHERE status = 'active'
              )
            """
        )
        spending, limits, quotas = cursor.fetchone()

    assert spending["mode"] == "overdraft"
    assert spending["overdraft_limit"] == "20"
    assert spending["max_concurrent"] == "2"
    assert spending["operations"]["completion"]["max_concurrent"] == 1
    assert limits["completion_calls"]["max_calls"] == 5
    assert limits["completion_calls"]["action"] == "deny"
    assert quotas["completion_calls"]["measure"] == "calls"
