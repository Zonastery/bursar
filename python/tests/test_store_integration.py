"""PostgreSQL integration coverage for the public v1 Bursar configuration."""

from decimal import Decimal

import psycopg2
import pytest

from bursar.credits_service import CreditsService
from bursar.metrics import UsageMetrics
from bursar.stores.base import StoreError
from bursar.stores.postgres import PostgresStore, run_migrations

pytestmark = [pytest.mark.integration]
USER_ID = "00000000-0000-0000-0000-000000000901"
REPLAY_USER_ID = "00000000-0000-0000-0000-000000000911"

CONFIG = {
    "version": 1,
    "usage": {
        "operations": {"completion": {"measures": ["input_tokens", "output_tokens"], "dimensions": ["model"]}},
        "rate_cards": {
            "standard": {
                "prices": {
                    "completion": [
                        {"match": {"model": {"prefix": "premium-"}}, "formula": "input_tokens * 2 + output_tokens * 3"},
                        {"default": True, "formula": "input_tokens + output_tokens"},
                    ]
                }
            }
        },
    },
    "credits": {
        "buckets": {"grant": {"expires_after": {"unit": "day", "count": 7}}, "purchased": {}},
        "spend_order": ["grant", "purchased"],
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
    _ensure_user(store)
    service.add_credits(USER_ID, Decimal("100"), "purchase", bucket="purchased")

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
    assert loaded.config["usage"]["operations"]["completion"]


def test_spend_order_is_applied_by_postgres_store(store: PostgresStore) -> None:
    service = CreditsService(store=store)
    service.publish_pricing_from_dict(CONFIG)
    _ensure_user(store)
    service.add_credits(USER_ID, Decimal("10"), "purchase", bucket="grant")
    service.add_credits(USER_ID, Decimal("10"), "purchase", bucket="purchased")

    service.deduct(
        USER_ID,
        UsageMetrics(operation="completion", measures={"input_tokens": 5, "output_tokens": 0}),
        idempotency_key="new-schema-charge-2",
    )
    buckets = {row.bucket_key: row.balance for row in service.get_bucket_balances(USER_ID).buckets}
    assert buckets["grant"] == Decimal("5")
    assert buckets["purchased"] == Decimal("10")
