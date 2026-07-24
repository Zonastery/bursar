"""PostgreSQL integration coverage for the public v1 Bursar configuration."""

from decimal import Decimal

import psycopg2
import pytest

from bursar.credits_service import CreditsService
from bursar.interface.postgres import PostgresStore
from bursar.metrics import UsageMetrics

pytestmark = [pytest.mark.integration]
USER_ID = "00000000-0000-0000-0000-000000000901"

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
    result = PostgresStore(pg_database_url).setup()
    assert result.success
    return PostgresStore(pg_database_url)


def _ensure_user(store: PostgresStore) -> None:
    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute("INSERT INTO auth.users (id) VALUES (%s) ON CONFLICT DO NOTHING", [USER_ID])
        cursor.execute('INSERT INTO public."user" (id) VALUES (%s) ON CONFLICT DO NOTHING', [USER_ID])


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
